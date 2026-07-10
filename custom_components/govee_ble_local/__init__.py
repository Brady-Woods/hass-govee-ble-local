"""The Govee BLE Local integration."""
from __future__ import annotations

import asyncio
import logging
import zlib
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from govee_ble_local import Capability, Device, GoveeBleNotSupported, create_device
from govee_ble_local.identify import identify
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_extract_config_entry_ids

from .capture import LogCapture, async_run_self_test
from .const import CONF_SECRET, DOMAIN, SERVICE_CAPTURE_SESSION
from .coordinator import GoveeBleLocalCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
]


@dataclass
class GoveeBleLocalRuntimeData:
    """Runtime objects shared between the platforms for one config entry."""

    device: Device
    coordinator: GoveeBleLocalCoordinator
    log_capture: LogCapture


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

    _remove_legacy_zone_entities(hass, address, device)

    # Always-on WARNING+ capture so unrecognised frames / rejections surface in the
    # downloadable diagnostics without the user having to enable debug logging.
    log_capture = LogCapture(address)
    entry.async_on_unload(log_capture.detach)

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
        # Device-info (wifi MAC, hardware/firmware version, serial) lives on the
        # DeviceState in the v3 library, populated by read-back where supported.
        state = device.state
        connections = {(dr.CONNECTION_BLUETOOTH, address)}
        if state.ble_mac:
            connections.add((dr.CONNECTION_BLUETOOTH, dr.format_mac(state.ble_mac)))
        if state.wifi_mac:
            connections.add((dr.CONNECTION_NETWORK_MAC, dr.format_mac(state.wifi_mac)))
        registry = dr.async_get(hass)
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, address)},
            connections=connections,
            manufacturer="Govee",
            model=device.sku,
            hw_version=state.hardware_version,
            sw_version=state.firmware_version,
            serial_number=state.serial_number,
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
        # PASSIVELY from the advertisement (no connection/slot). The library
        # parses on/off out of the Govee manufacturer data and reports whether it
        # changed; push any change straight to the entities so on/off is live
        # between polls.
        coordinator.note_advertisement_seen()
        device.update_ble_device(service_info.device)
        if device.ingest_advertisement(service_info):
            coordinator.async_set_updated_data(device.state)
        else:
            # Presence/last-seen diagnostics still moved even if on/off didn't.
            coordinator.async_update_listeners()

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            bluetooth.BluetoothCallbackMatcher(address=address),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )

    @callback
    def _async_unavailable(_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        # The device stopped advertising; push so the presence/connectivity
        # sensor flips to "disconnected" promptly.
        coordinator.async_update_listeners()

    entry.async_on_unload(
        bluetooth.async_track_unavailable(
            hass, _async_unavailable, address, connectable=False
        )
    )

    entry.runtime_data = GoveeBleLocalRuntimeData(
        device=device, coordinator=coordinator, log_capture=log_capture
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)
    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the integration-wide services once."""
    if hass.services.has_service(DOMAIN, SERVICE_CAPTURE_SESSION):
        return

    async def _async_capture_session(call: ServiceCall) -> ServiceResponse:
        """Run the device self-test against each targeted device and return the
        captured session(s)."""
        entry_ids = await async_extract_config_entry_ids(hass, call)
        results: list[Any] = []
        for entry_id in entry_ids:
            target = hass.config_entries.async_get_entry(entry_id)
            if (
                target is None
                or target.domain != DOMAIN
                or target.state is not ConfigEntryState.LOADED
            ):
                continue
            data: GoveeBleLocalRuntimeData = target.runtime_data
            report = await async_run_self_test(data.device)
            data.coordinator.last_self_test = report
            results.append(report)
        if not results:
            raise HomeAssistantError(
                "No loaded Govee BLE Local device matched the service target"
            )
        return {"results": results}

    hass.services.async_register(
        DOMAIN,
        SERVICE_CAPTURE_SESSION,
        _async_capture_session,
        # Target-only service (device/entity/area); allow the target keys through.
        schema=vol.Schema({}, extra=vol.ALLOW_EXTRA),
        supports_response=SupportsResponse.ONLY,
    )


@callback
def _remove_legacy_zone_entities(hass: HomeAssistant, address: str, device: Device) -> None:
    """Remove orphaned zone-switch entities left by earlier entity schemes.

    1. Pre-v0.11 zone switches were keyed by integer index ({address}_zone_0 /
       _zone_1); they are now keyed by zone name (_zone_main / _zone_background).
    2. A colour-controllable zone (one with segments) is now a light, not a
       switch, so its old switch entity ({address}_zone_{name}) is orphaned.

    Drop both so only the current entities remain."""
    registry = er.async_get(hass)
    stale_unique_ids = [f"{address}_zone_{i}" for i in ("0", "1")]
    stale_unique_ids += [
        f"{address}_zone_{zone.name}" for zone in device.zones if zone.segments
    ]
    for unique_id in stale_unique_ids:
        entity_id = registry.async_get_entity_id("switch", DOMAIN, unique_id)
        if entity_id is not None:
            _LOGGER.debug("Removing orphaned zone switch %s", entity_id)
            registry.async_remove(entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: GoveeBleLocalConfigEntry) -> bool:
    """Unload a config entry, disconnecting the BLE device."""
    _LOGGER.debug("Unloading entry for %s", entry.data["address"])
    await entry.runtime_data.device.stop()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # Drop the shared service once the last device is gone.
    other_loaded = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id and e.state is ConfigEntryState.LOADED
    ]
    if not other_loaded and hass.services.has_service(DOMAIN, SERVICE_CAPTURE_SESSION):
        hass.services.async_remove(DOMAIN, SERVICE_CAPTURE_SESSION)
    return unloaded
