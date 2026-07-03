"""The Govee H60A6 integration."""
from __future__ import annotations

import asyncio
import logging
import zlib
from dataclasses import dataclass

from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .client import GoveeH60A6Client
from .const import DOMAIN
from .coordinator import GoveeH60A6Coordinator
from .entity import CONNECTION_BLE
from .scene_library import SceneData, async_fetch_scene_library

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SWITCH]
SCENE_LIBRARY_SKU = "H60A6"


@dataclass
class GoveeH60A6RuntimeData:
    """Runtime objects shared between the platforms for one config entry."""

    client: GoveeH60A6Client
    coordinator: GoveeH60A6Coordinator
    scene_library: dict[str, SceneData]
    serial_number: str | None


type GoveeH60A6ConfigEntry = ConfigEntry[GoveeH60A6RuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: GoveeH60A6ConfigEntry) -> bool:
    """Set up a Govee H60A6 light from a config entry."""
    address: str = entry.data["address"]
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

    # Serial number is static (queried once, not part of the regular poll
    # cycle - no reason to re-fetch it every 30s). A "nice to have," not
    # essential to the integration working, so a failure here is logged
    # and setup continues rather than blocking on it - matches the same
    # graceful-degradation pattern as the scene library fetch above.
    try:
        serial_number = await client.get_serial_number()
    except BleakError as err:
        _LOGGER.debug("Could not fetch serial number for %s: %s", address, err)
        serial_number = None
    if serial_number:
        _LOGGER.debug("Serial number for %s: %s", address, serial_number)

    @callback
    def _sync_device_registry() -> None:
        # device_info on entities is only applied once, at initial entity
        # registration. Re-syncing on every successful poll is meant to
        # let a bad value from one flaky update (e.g. two lights' BLE
        # traffic briefly cross-contaminating at startup, or an old
        # parsing bug) get corrected on the next good one - but
        # async_get_or_create()'s `connections` argument only ever ADDS to
        # the existing set (merge semantics), it never removes anything.
        # A bad connection written once therefore stuck around forever
        # sitting alongside the correct one, silently defeating the
        # self-healing this was meant to provide (confirmed live: real
        # device registry entries were found with a stale, garbled MAC
        # connection alongside the correct one, months after the bad
        # value was first written). async_update_device's
        # `new_connections` does a full replace instead of a merge, so use
        # that for the actual self-healing correction.
        status = coordinator.data
        if status is None:
            return
        connections = {(CONNECTION_BLE, address)}
        if status.wifi_mac:
            connections.add((dr.CONNECTION_NETWORK_MAC, status.wifi_mac))

        registry = dr.async_get(hass)
        device_entry = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, address)},
            connections=connections,
            manufacturer="Govee",
            model="H60A6",
            hw_version=status.hardware_version,
            serial_number=serial_number,
        )
        if device_entry.connections != connections:
            registry.async_update_device(device_entry.id, new_connections=connections)

    _sync_device_registry()
    entry.async_on_unload(coordinator.async_add_listener(_sync_device_registry))

    @callback
    def _async_update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        client.update_ble_device(service_info.device)

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            bluetooth.BluetoothCallbackMatcher(address=address),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )

    entry.runtime_data = GoveeH60A6RuntimeData(
        client=client,
        coordinator=coordinator,
        scene_library=scene_library,
        serial_number=serial_number,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GoveeH60A6ConfigEntry) -> bool:
    """Unload a config entry, disconnecting the BLE client."""
    _LOGGER.debug("Unloading entry for %s", entry.data["address"])
    await entry.runtime_data.client.disconnect()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
