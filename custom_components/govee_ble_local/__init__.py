"""The Govee BLE Local integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import zlib

from govee_ble_local import (
    Capability,
    Device,
    GoveeBleNotSupported,
    create_device,
    is_supported_sku,
)
from govee_ble_local.identify import identify
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
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
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import UNDEFINED, ConfigType

from .capture import LogCapture, SelfTestReport
from .const import (
    CONF_ALLOW_MODELLED,
    CONF_SECRET,
    CONF_SKU,
    DOMAIN,
    FIRST_POLL_STAGGER_SECONDS,
    LAST_SEEN_RESOLUTION_SECONDS,
    MANUFACTURER,
    POLL_IDLE_DISCONNECT_SECONDS,
    SELF_TEST_STOP_TIMEOUT_SECONDS,
    SELF_TEST_UNLOAD_TIMEOUT_SECONDS,
    STOP_CLOSE_TIMEOUT_SECONDS,
)
from .coordinator import GoveeBleLocalCoordinator
from .helpers import clean_mac, clean_text, is_colour_zone, parse_secret
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
    # The running (or last) self-test, so an unload or HA stop can let it restore the device.
    self_test: asyncio.Task[SelfTestReport] | None = field(default=None, repr=False)
    # Most recent self-test report (button / capture_session service), for diagnostics.
    last_self_test: SelfTestReport | None = field(default=None, repr=False)


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

    # Set by the config flow when the user opted in to a "modelled" model.
    allow_modelled = bool(entry.data.get(CONF_ALLOW_MODELLED, False))

    try:
        device = create_device(
            ble_device,
            sku,
            advertisement,
            secret=secret,
            idle_disconnect=POLL_IDLE_DISCONNECT_SECONDS,
            allow_modelled=allow_modelled,
        )
    except GoveeBleNotSupported as err:
        adv = identify(service_info.name, service_info.manufacturer_data) if service_info else None
        if (
            adv is not None
            and adv.pact is not None
            and is_supported_sku(sku, allow_modelled=allow_modelled)
        ):
            # The library serves the model, only not at the pact the device advertises now.
            # A later advert can carry another, so retry instead of giving up for good.
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="protocol_not_supported",
                translation_placeholders={"sku": sku, "protocol": "{},{}".format(*adv.pact)},
            ) from err
        # Permanent: retrying can't make an unsupported model supported.
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="model_not_supported",
            translation_placeholders={"sku": sku},
        ) from err
    # Close the link whenever the entry goes away (a failed setup included).
    # HA runs on-unload jobs last in, first out, so registering this before the
    # coordinator exists means polling is shut down first. HA cancels only entry
    # background tasks - the scheduled, first and return polls - which frees the
    # connection for the close. An entity command, a manual update_entity
    # refresh or an overrunning self-test is not cancelled, and the close waits
    # for it: HA stops waiting after 10 s, logs that the task "did not complete
    # in time" and finishes the unload while the close completes in the
    # background.
    entry.async_on_unload(device.stop)

    # Warm the (blocking, cached) scene-catalog read off the event loop so the
    # light platform's effect_list access doesn't do file IO in the loop.
    if Capability.SCENES in device.capabilities:
        await hass.async_add_executor_job(lambda: device.scene_names)

    _async_remove_legacy_zone_entities(hass, address, device)

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
    # coordinator's backoff, not ConfigEntryNotReady. HA cancels it (like every
    # entry background task) on unload.
    coordinator.async_set_updated_data(device.state)
    stagger = zlib.crc32(address.encode()) % FIRST_POLL_STAGGER_SECONDS

    async def _staggered_first_poll() -> None:
        _LOGGER.debug("Waiting %ds stagger before first poll of %s", stagger, address)
        await asyncio.sleep(stagger)
        await coordinator.async_refresh()

    entry.async_create_background_task(
        hass, _staggered_first_poll(), name=f"{DOMAIN}_first_poll_{address}"
    )

    _async_track_device_registry(hass, entry)
    _async_track_bluetooth(hass, entry)
    _async_close_on_stop(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


@callback
def _async_close_on_stop(hass: HomeAssistant, entry: GoveeBleLocalConfigEntry) -> None:
    """Close the link when Home Assistant stops, after a running self-test's restore.

    HA doesn't unload config entries when it stops, so this is the only close
    of a link still open at shutdown: one a command keeps warm, or one a
    command or self-test is still using (HA cancels background polls just
    before it fires the event). HA waits for the listener, but gives its whole
    stop stage only 100 s, so both waits are bounded: a running self-test gets
    SELF_TEST_STOP_TIMEOUT_SECONDS to finish and restore the device (less than
    an unload gives it), then the close gets STOP_CLOSE_TIMEOUT_SECONDS. A
    self-test still running after that has its remaining steps and restore
    refused: the device stays in its test state and the restore failure is
    logged.

    An unload after the event closes the link again, which is harmless.
    async_listen, not async_listen_once as core led_ble uses: removing a
    one-time listener that has already fired, as an unload after the event
    would, logs an error (core esphome avoids it the same way).
    """
    data = entry.runtime_data

    async def _async_stop(_event: Event) -> None:
        if (self_test := data.self_test) is not None and not self_test.done():
            await asyncio.wait({self_test}, timeout=SELF_TEST_STOP_TIMEOUT_SECONDS)
        # The close waits for an operation still using the link, up to the
        # library's 45 s operation deadline. Giving up early is safe: the device
        # refuses new I/O from the moment the close starts, and HA cancels the
        # task still using the link in its last shutdown stage.
        try:
            async with asyncio.timeout(STOP_CLOSE_TIMEOUT_SECONDS):
                await data.device.stop()
        except TimeoutError:
            _LOGGER.debug(
                "%s: link still busy at shutdown; not waiting to close it",
                entry.data[CONF_ADDRESS],
            )

    entry.async_on_unload(hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, _async_stop))


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
            return  # fires on every push to the entities; skip the no-op case
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
    # A state listener, not a coordinator listener: HA polls while the coordinator
    # has listeners, and this one would keep a device polled with every entity
    # disabled.
    entry.async_on_unload(entry.runtime_data.coordinator.async_add_state_listener(_sync))


@callback
def _async_track_bluetooth(hass: HomeAssistant, entry: GoveeBleLocalConfigEntry) -> None:
    """Wire the passive advertisement, presence and unavailability tracking to the coordinator."""
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
            # Any scanner: a passive one reports on/off and presence just as well.
            bluetooth.BluetoothCallbackMatcher(address=address, connectable=False),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )
    # That callback only fires when an advertisement's content changes, so last
    # seen / RSSI are read from HA's advertisement history: once now (aged by
    # the history, not stamped with startup time), then on a timer.
    coordinator.async_refresh_presence()
    entry.async_on_unload(
        async_track_time_interval(
            hass,
            coordinator.async_refresh_presence,
            timedelta(seconds=LAST_SEEN_RESOLUTION_SECONDS),
            name=f"{DOMAIN}_presence_{address}",
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
def _async_remove_legacy_zone_entities(hass: HomeAssistant, address: str, device: Device) -> None:
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
        f"{address}_zone_{zone.name}" for zone in device.zones if is_colour_zone(zone)
    ]
    for unique_id in stale_unique_ids:
        entity_id = registry.async_get_entity_id(Platform.SWITCH, DOMAIN, unique_id)
        if entity_id is not None:
            _LOGGER.debug("Removing orphaned zone switch %s", entity_id)
            registry.async_remove(entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: GoveeBleLocalConfigEntry) -> bool:
    """Unload a config entry: platforms, then let a running self-test finish.

    The self-test gets up to SELF_TEST_UNLOAD_TIMEOUT_SECONDS so its restore can
    land before the link closes. Polling and the link itself are torn down by
    the on-unload jobs HA runs after this returns: the coordinator's shutdown
    first, then ``device.stop()`` (see async_setup_entry). HA waits at most 10 s
    for those jobs: a command or manual refresh still holding the link (HA
    cancels only background polls) can make the close finish after the unload,
    or a reload, has completed.
    """
    _LOGGER.debug("Unloading entry for %s", entry.data[CONF_ADDRESS])
    # The capture_session service is registered in async_setup (integration-wide),
    # so it is intentionally left in place across entry unloads.
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    if (self_test := entry.runtime_data.self_test) is not None:
        # Closing the link mid-test would leave the device in a test state.
        await asyncio.wait({self_test}, timeout=SELF_TEST_UNLOAD_TIMEOUT_SECONDS)
    return True
