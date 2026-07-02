"""The Govee H60A6 integration."""
from __future__ import annotations

import asyncio
import logging
import zlib

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .client import GoveeH60A6Client
from .const import DOMAIN
from .coordinator import GoveeH60A6Coordinator
from .entity import CONNECTION_BLE
from .scene_library import async_fetch_scene_library

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["light", "switch"]
SCENE_LIBRARY_SKU = "H60A6"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    address = entry.data["address"]
    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(f"Could not find Govee light with address {address}")

    client = GoveeH60A6Client(ble_device)
    coordinator = GoveeH60A6Coordinator(hass, client, address)

    scene_library = await async_fetch_scene_library(hass, SCENE_LIBRARY_SKU)
    if scene_library:
        _LOGGER.debug("Using %d scenes from Govee's online library", len(scene_library))
    else:
        _LOGGER.warning(
            "Could not fetch Govee's online scene library; falling back to bare "
            "scene activation (may not work for scenes never used via the app)"
        )

    # Stagger multiple lights' poll schedules so they don't stay in lockstep
    # and repeatedly fight over the adapter's limited BLE connection slots.
    stagger = zlib.crc32(address.encode()) % 8
    _LOGGER.debug("Waiting %ds stagger before first poll of %s", stagger, address)
    await asyncio.sleep(stagger)

    _LOGGER.debug("Fetching initial status for %s", address)
    await coordinator.async_config_entry_first_refresh()

    @callback
    def _sync_device_registry() -> None:
        # device_info on entities is only applied once, at initial entity
        # registration. Re-syncing on every successful poll means a bad
        # value from one flaky update (e.g. two lights' BLE traffic briefly
        # cross-contaminating at startup) gets corrected on the next good
        # one, instead of silently sticking in the registry forever.
        status = coordinator.data
        if status is None:
            return
        connections = {(CONNECTION_BLE, address)}
        if status.wifi_mac:
            connections.add((dr.CONNECTION_NETWORK_MAC, status.wifi_mac))
        dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, address)},
            connections=connections,
            manufacturer="Govee",
            model="H60A6",
            hw_version=status.hardware_version,
        )

    _sync_device_registry()
    entry.async_on_unload(coordinator.async_add_listener(_sync_device_registry))

    @callback
    def _async_update_ble(service_info: bluetooth.BluetoothServiceInfoBleak, change) -> None:
        client.update_ble_device(service_info.device)

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            bluetooth.BluetoothCallbackMatcher(address=address),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "scene_library": scene_library,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _LOGGER.debug("Unloading entry for %s", entry.data["address"])
    data = hass.data[DOMAIN][entry.entry_id]
    await data["client"].disconnect()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
