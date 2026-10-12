"""The Govee BLE Local integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import zlib

from govee_ble_local import Capability, Device, GoveeBleNotSupported, create_device
from govee_ble_local.identify import identify
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.typing import UNDEFINED, ConfigType

from .capture import LogCapture
from .const import CONF_SECRET, CONF_SKU, DOMAIN, FIRST_POLL_STAGGER_SECONDS, MANUFACTURER
from .coordinator import GoveeBleLocalCoordinator
from .helpers import clean_mac, clean_text, parse_secret
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class GoveeBleLocalRuntimeData:
    """Runtime objects shared between the platforms for one config entry."""

    device: Device
    coordinator: GoveeBleLocalCoordinator
    log_capture: LogCapture
    first_poll: asyncio.Task[None] | None = field(default=None, repr=False)


type GoveeBleLocalConfigEntry = ConfigEntry[GoveeBleLocalRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register integration-wide service actions (once, independent of entries)."""
    async_register_services(hass)
    return True


def _resolve_sku(
    entry: GoveeBleLocalConfigEntry, service_info: bluetooth.BluetoothServiceInfoBleak | None
) -> str:
    """Return the SKU stored at config time, else identify it from the advertisement."""
    if sku := entry.data.get(CONF_SKU):
        return str(sku)
    if service_info is not None and (
        adv := identify(service_info.name, service_info.manufacturer_data)
    ):
        return adv.sku
    raise ConfigEntryNotReady(
        translation_domain=DOMAIN,
        translation_key="sku_unknown",
        translation_placeholders={"address": entry.data[CONF_ADDRESS]},
    )


def _stored_secret(entry: GoveeBleLocalConfigEntry) -> bytes | None:
    """Return the entry's secret key, starting reauth if the stored value is malformed."""
    raw = entry.data.get(CONF_SECRET)
    if not raw:
        return None
    try:
        secret_hex = parse_secret(str(raw))
    except ValueError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="secret_malformed",
            translation_placeholders={"device_name": entry.title},
        ) from err
    return bytes.fromhex(secret_hex) if secret_hex else None


async def async_setup_entry(hass: HomeAssistant, entry: GoveeBleLocalConfigEntry) -> bool:
    """Set up a Govee BLE device from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders={"address": address},
        )

    # The raw advertisement drives the library's encryption decision (the app's
    # `encrypt` flag), so pass it through when we have it.
    service_info = bluetooth.async_last_service_info(hass, address, connectable=True)
    advertisement = service_info.advertisement if service_info else None
    sku = _resolve_sku(entry, service_info)
    # Some devices (smart plugs) gate commands behind an 8-byte secret key,
    # stored as hex in the config entry (see config_flow's secret steps).
    secret = _stored_secret(entry)

    try:
        device = create_device(ble_device, sku, advertisement, secret=secret)
    except GoveeBleNotSupported as err:
        # Permanent: retrying can't make an unsupported model supported.
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="model_not_supported",
            translation_placeholders={"sku": sku},
        ) from err

    # Warm the (blocking, cached) scene-catalog read off the event loop so the
    # light platform's effect_list access doesn't do file IO in the loop.
    if Capability.SCENES in device.capabilities:
        await hass.async_add_executor_job(lambda: device.scene_names)

    _remove_legacy_zone_entities(hass, address, device)

    # Always-on WARNING+ capture so unrecognised frames / rejections surface in the
    # downloadable diagnostics without the user having to enable debug logging.
    log_capture = LogCapture(address)
    entry.async_on_unload(log_capture.detach)

    coordinator = GoveeBleLocalCoordinator(hass, entry, device)
    entry.runtime_data = GoveeBleLocalRuntimeData(
        device=device, coordinator=coordinator, log_capture=log_capture
    )

    # The device is advertising (async_ble_device_from_address above found it),
    # so mark the entry ready from passive state instead of blocking setup on a
    # full connect+handshake for every device on every restart - that startup
    # storm is what exhausts connection slots. The first real poll runs in the
    # background, staggered per device; a failure there is handled by the
    # coordinator's backoff, not ConfigEntryNotReady.
    coordinator.async_set_updated_data(device.state)
    stagger = zlib.crc32(address.encode()) % FIRST_POLL_STAGGER_SECONDS

    async def _staggered_first_poll() -> None:
        _LOGGER.debug("Waiting %ds stagger before first poll of %s", stagger, address)
        await asyncio.sleep(stagger)
        await coordinator.async_refresh()

    entry.runtime_data.first_poll = entry.async_create_background_task(
        hass, _staggered_first_poll(), name=f"{DOMAIN}_first_poll_{address}"
    )

    _async_track_device_registry(hass, entry)
    _async_track_bluetooth(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


@callback
def _async_track_device_registry(hass: HomeAssistant, entry: GoveeBleLocalConfigEntry) -> None:
    """Keep the device-registry entry enriched with the read-back device info.

    Device-info (wifi MAC, hardware/firmware version, serial) is read back on the
    first good poll. Only known values are written: passing None would CLEAR
    what the registry already holds (e.g. after a restart before the first poll).
    """
    address: str = entry.data[CONF_ADDRESS]
    device = entry.runtime_data.device
    last_info: tuple[str | None, ...] | None = None

    @callback
    def _sync() -> None:
        nonlocal last_info
        state = device.state
        info = (
            clean_mac(state.ble_mac),
            clean_mac(state.wifi_mac),
            clean_text(state.hardware_version),
            clean_text(state.firmware_version),
            clean_text(state.serial_number),
        )
        if info == last_info:
            return  # fires on every coordinator update; skip the no-op case
        last_info = info
        ble_mac, wifi_mac, hw_version, sw_version, serial = info
        connections = {(dr.CONNECTION_BLUETOOTH, address)}
        if ble_mac:
            # Same notation as HA's own bluetooth connections (upper-case).
            connections.add((dr.CONNECTION_BLUETOOTH, dr.format_mac(ble_mac).upper()))
        if wifi_mac:
            connections.add((dr.CONNECTION_NETWORK_MAC, dr.format_mac(wifi_mac)))
        # UNDEFINED leaves a field as the registry has it; None would clear it.
        dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, address)},
            connections=connections,
            manufacturer=MANUFACTURER,
            model=device.sku,
            name=entry.title,
            hw_version=hw_version or UNDEFINED,
            sw_version=sw_version or UNDEFINED,
            serial_number=serial or UNDEFINED,
        )

    _sync()
    entry.async_on_unload(entry.runtime_data.coordinator.async_add_listener(_sync))


@callback
def _async_track_bluetooth(hass: HomeAssistant, entry: GoveeBleLocalConfigEntry) -> None:
    """Wire the passive advertisement and unavailability callbacks to the coordinator."""
    address: str = entry.data[CONF_ADDRESS]
    coordinator = entry.runtime_data.coordinator

    @callback
    def _async_update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        coordinator.async_handle_advertisement(service_info)

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            bluetooth.BluetoothCallbackMatcher(address=address),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )

    # Two signals, matching the two presence checks: entity availability uses
    # the connectable history (commands need a connectable scanner), the
    # connectivity sensor any scanner at all. Track the loss of each.
    @callback
    def _async_connectable_lost(_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        coordinator.async_handle_unavailable(connectable=True)

    @callback
    def _async_all_lost(_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        coordinator.async_handle_unavailable(connectable=False)

    entry.async_on_unload(
        bluetooth.async_track_unavailable(hass, _async_connectable_lost, address, connectable=True)
    )
    entry.async_on_unload(
        bluetooth.async_track_unavailable(hass, _async_all_lost, address, connectable=False)
    )


@callback
def _remove_legacy_zone_entities(hass: HomeAssistant, address: str, device: Device) -> None:
    """Remove orphaned zone-switch entities left by earlier entity schemes.

    1. Pre-v0.11 zone switches were keyed by integer index ({address}_zone_0 /
       _zone_1); they are now keyed by zone name (_zone_main / _zone_background).
    2. A colour-controllable zone (one with segments) is now a light, not a
       switch, so its old switch entity ({address}_zone_{name}) is orphaned.

    Drop both so only the current entities remain.
    """
    registry = er.async_get(hass)
    stale_unique_ids = [f"{address}_zone_{i}" for i in ("0", "1")]
    stale_unique_ids += [
        f"{address}_zone_{zone.name}" for zone in device.zones if zone.segments
    ]
    for unique_id in stale_unique_ids:
        entity_id = registry.async_get_entity_id(Platform.SWITCH, DOMAIN, unique_id)
        if entity_id is not None:
            _LOGGER.debug("Removing orphaned zone switch %s", entity_id)
            registry.async_remove(entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: GoveeBleLocalConfigEntry) -> bool:
    """Unload a config entry: platforms, then polling, then the BLE link.

    Polling is stopped BEFORE the link is closed: HA only runs the entry's
    on-unload callbacks (which also shut the coordinator down) after this
    returns, and a poll firing while ``stop()`` waits for the connection lock
    would otherwise fail against the closing link.
    """
    _LOGGER.debug("Unloading entry for %s", entry.data[CONF_ADDRESS])
    # The capture_session service is registered in async_setup (integration-wide),
    # so it is intentionally left in place across entry unloads.
    if not (unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS)):
        return False
    data = entry.runtime_data
    await data.coordinator.async_shutdown()
    if data.first_poll is not None and not data.first_poll.done():
        data.first_poll.cancel()
    await data.device.stop()
    return unload_ok
