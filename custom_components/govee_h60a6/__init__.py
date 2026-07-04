"""The Govee BLE Local integration."""
from __future__ import annotations

import asyncio
import logging
import zlib
from dataclasses import dataclass

from bleak.exc import BleakError
from govee_ble_local import GoveeBleClient, profile as govee_profile
from govee_ble_local.profile import DeviceProfile
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import GoveeH60A6Coordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SWITCH]
# Fallback profile when the advertised name isn't available to match on. This
# integration is currently H60A6-scoped by its bluetooth manifest matcher.
DEFAULT_SKU = "H60A6"


@dataclass
class GoveeH60A6RuntimeData:
    """Runtime objects shared between the platforms for one config entry."""

    client: GoveeBleClient
    coordinator: GoveeH60A6Coordinator
    profile: DeviceProfile
    serial_number: str | None


type GoveeH60A6ConfigEntry = ConfigEntry[GoveeH60A6RuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: GoveeH60A6ConfigEntry) -> bool:
    """Set up a Govee BLE light from a config entry."""
    address: str = entry.data["address"]
    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(f"Could not find Govee light with address {address}")

    # Resolve the device profile (capabilities + scene catalog). Prefer the SKU
    # stored at config time, then fall back to matching the advertised name,
    # then to the default SKU. Loading YAML is blocking, so use the executor.
    sku = entry.data.get("sku")
    profile = None
    if sku:
        profile = await hass.async_add_executor_job(govee_profile.load_by_sku, sku)
    if profile is None:
        profile = await hass.async_add_executor_job(
            govee_profile.match_local_name, ble_device.name
        )
    if profile is None:
        profile = await hass.async_add_executor_job(govee_profile.load_by_sku, DEFAULT_SKU)
    if profile is None:
        raise ConfigEntryNotReady(
            f"No device profile for {ble_device.name!r} (address {address})"
        )
    _LOGGER.debug("Using profile %s (%d scenes) for %s", profile.sku, len(profile.scenes), address)

    client = GoveeBleClient(ble_device)
    coordinator = GoveeH60A6Coordinator(hass, client, address)

    # Stagger multiple lights' poll schedules so they don't stay in lockstep
    # and repeatedly fight over the adapter's limited BLE connection slots.
    stagger = zlib.crc32(address.encode()) % 8
    _LOGGER.debug("Waiting %ds stagger before first poll of %s", stagger, address)
    await asyncio.sleep(stagger)

    _LOGGER.debug("Fetching initial status for %s", address)
    await coordinator.async_config_entry_first_refresh()

    # Serial number is static (queried once, not part of the regular poll
    # cycle). A "nice to have," not essential - a failure here is logged and
    # setup continues rather than blocking on it.
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
        # registration. Re-syncing on every successful poll lets a bad value
        # from one flaky update get corrected on the next good one - but
        # async_get_or_create()'s `connections` only ADDS (merge), never
        # removes, so a bad connection written once stuck around forever.
        # async_update_device's `new_connections` does a full replace instead.
        # coordinator.data is guaranteed populated here: async_config_entry_
        # first_refresh() above raises ConfigEntryNotReady on failure, so this
        # callback is only ever registered/invoked after a successful poll.
        status = coordinator.data
        connections = {(dr.CONNECTION_BLUETOOTH, address)}
        if status.wifi_mac:
            connections.add((dr.CONNECTION_NETWORK_MAC, status.wifi_mac))

        registry = dr.async_get(hass)
        device_entry = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, address)},
            connections=connections,
            manufacturer="Govee",
            model=profile.name,
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
        profile=profile,
        serial_number=serial_number,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GoveeH60A6ConfigEntry) -> bool:
    """Unload a config entry, disconnecting the BLE client."""
    _LOGGER.debug("Unloading entry for %s", entry.data["address"])
    await entry.runtime_data.client.disconnect()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
