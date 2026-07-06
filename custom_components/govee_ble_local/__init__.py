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

from .const import DOMAIN
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

    try:
        device = create_device(ble_device, sku, advertisement)
    except GoveeBleNotSupported as err:
        raise ConfigEntryNotReady(str(err)) from err

    # Warm the (blocking) scene-catalog read off the event loop so the light
    # platform's effect_list access doesn't do file IO in the loop.
    if Capability.SCENES in device.capabilities:
        await hass.async_add_executor_job(lambda: device.scene_names)

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
        registry = dr.async_get(hass)
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, address)},
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            manufacturer="Govee",
            model=device.model,
        )

    _sync_device_registry()

    @callback
    def _async_update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        device.update_ble_device(service_info.device, service_info.advertisement)

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


async def async_unload_entry(hass: HomeAssistant, entry: GoveeBleLocalConfigEntry) -> bool:
    """Unload a config entry, disconnecting the BLE device."""
    _LOGGER.debug("Unloading entry for %s", entry.data["address"])
    await entry.runtime_data.device.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
