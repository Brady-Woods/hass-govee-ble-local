"""The Govee BLE Local integration."""
from __future__ import annotations

import asyncio
import logging
import zlib
from dataclasses import dataclass

from govee_ble_local import Capability, GoveeBleNotSupported, GoveeDevice, create_device
from govee_ble_local.identify import identify
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import CONF_SECRET, DOMAIN
from .coordinator import GoveeBleLocalCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SWITCH]


@dataclass
class GoveeBleLocalRuntimeData:
    """Runtime objects shared between the platforms for one config entry."""

    device: GoveeDevice
    coordinator: GoveeBleLocalCoordinator


type GoveeBleLocalConfigEntry = ConfigEntry[GoveeBleLocalRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: GoveeBleLocalConfigEntry) -> bool:
    """Set up a Govee BLE device from a config entry."""
    address: str = entry.data["address"]
    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(f"Could not find Govee device with address {address}")

    # The raw advertisement drives the library's encryption decision (the app's
    # `encrypt` flag), so pass it through when we have it.
    service_info = bluetooth.async_last_service_info(hass, address, connectable=True)
    advertisement = service_info.advertisement if service_info else None

    # SKU: prefer the value stored at config time; otherwise identify it from
    # the current advertisement.
    sku = entry.data.get("sku")
    if not sku and service_info is not None:
        adv = identify(service_info.name, service_info.manufacturer_data)
        sku = adv.sku if adv is not None else None
    if not sku:
        raise ConfigEntryNotReady(f"Could not determine SKU for {address}")

    # Some devices (smart plugs) gate commands behind an 8-byte secret key,
    # stored as hex in the config entry (see config_flow's secret step).
    secret_hex = entry.data.get(CONF_SECRET)
    secret = bytes.fromhex(secret_hex) if secret_hex else None

    try:
        device = create_device(ble_device, sku, advertisement, secret=secret)
    except GoveeBleNotSupported as err:
        raise ConfigEntryNotReady(str(err)) from err

    # Warm the (blocking) scene-catalog read off the event loop so the light
    # platform's effect_list access doesn't do file IO in the loop.
    if Capability.SCENES in device.capabilities:
        await hass.async_add_executor_job(lambda: device.scene_names)

    _remove_legacy_zone_entities(hass, address)

    coordinator = GoveeBleLocalCoordinator(hass, device, address)

    # Stagger multiple devices' poll schedules so they don't stay in lockstep
    # and repeatedly fight over the adapter's limited BLE connection slots.
    stagger = zlib.crc32(address.encode()) % 8
    _LOGGER.debug("Waiting %ds stagger before first poll of %s", stagger, address)
    await asyncio.sleep(stagger)

    _LOGGER.debug("Establishing initial connection for %s", address)
    await coordinator.async_config_entry_first_refresh()

    @callback
    def _sync_device_registry() -> None:
        connections = {(dr.CONNECTION_BLUETOOTH, address)}
        if device.wifi_mac:
            connections.add((dr.CONNECTION_NETWORK_MAC, device.wifi_mac))
        registry = dr.async_get(hass)
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, address)},
            connections=connections,
            manufacturer="Govee",
            model=device.model,
            hw_version=device.hardware_version,
            sw_version=device.firmware_version,
            serial_number=device.serial_number,
        )

    _sync_device_registry()
    # Device-info (wifi MAC, hardware version, serial) is read back on the first
    # poll(s); re-sync when it arrives so the registry entry gets enriched.
    entry.async_on_unload(coordinator.async_add_listener(_sync_device_registry))

    @callback
    def _async_update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        # Refresh the BLEDevice handle for future connections, and update on/off
        # PASSIVELY from the advertisement (no connection/slot). Push the change
        # straight to the entities so on/off is live between polls.
        device.update_ble_device(service_info.device)
        if device.ingest_advertisement(service_info):
            coordinator.async_set_updated_data(device.state)

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            bluetooth.BluetoothCallbackMatcher(address=address),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )

    entry.runtime_data = GoveeBleLocalRuntimeData(device=device, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


@callback
def _remove_legacy_zone_entities(hass: HomeAssistant, address: str) -> None:
    """Remove orphaned zone-switch entities from before v0.11.

    Zone switches used to be keyed by integer index ({address}_zone_0 /
    _zone_1); they are now keyed by zone name (_zone_main / _zone_background).
    The scheme change left the old entities behind as duplicate "unavailable"
    switches. Drop them so only the current, correctly-named switches remain."""
    registry = er.async_get(hass)
    for legacy_index in ("0", "1"):
        entity_id = registry.async_get_entity_id(
            "switch", DOMAIN, f"{address}_zone_{legacy_index}"
        )
        if entity_id is not None:
            _LOGGER.debug("Removing legacy zone entity %s", entity_id)
            registry.async_remove(entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: GoveeBleLocalConfigEntry) -> bool:
    """Unload a config entry, disconnecting the BLE device."""
    _LOGGER.debug("Unloading entry for %s", entry.data["address"])
    await entry.runtime_data.device.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
