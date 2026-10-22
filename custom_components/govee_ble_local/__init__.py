"""The Govee BLE Local integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from pathlib import Path
from typing import NoReturn
import zlib

from bleak.backends.scanner import AdvertisementData
import govee_ble_local
from govee_ble_local import (
    Capability,
    Device,
    GoveeAdvertisement,
    GoveeBleError,
    GoveeBleNotSupported,
    create_device,
    supported_skus,
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
from homeassistant.util.hass_dict import HassKey

from .capture import LogCapture, SelfTestReport
from .connect_gate import async_release_waiters
from .const import (
    CONF_ALLOW_MODELLED,
    CONF_BROADCAST,
    CONF_SECRET,
    CONF_SKU,
    DOMAIN,
    FIRST_POLL_STAGGER_MAX_SECONDS,
    FIRST_POLL_STAGGER_PER_DEVICE_SECONDS,
    FIRST_POLL_STAGGER_SECONDS,
    LAST_SEEN_RESOLUTION_SECONDS,
    MANUFACTURER,
    POLL_IDLE_DISCONNECT_SECONDS,
    SELF_TEST_STOP_TIMEOUT_SECONDS,
    SELF_TEST_UNLOAD_TIMEOUT_SECONDS,
    STOP_CLOSE_TIMEOUT_SECONDS,
)
from .coordinator import GoveeBleLocalCoordinator
from .helpers import (
    Broadcast,
    advertised_broadcast,
    clean_mac,
    clean_text,
    is_colour_zone,
    parse_secret,
)
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)
# Whether a broken scene store was already reported this run (once, not per device).
_SCENE_STORE_REPORTED: HassKey[bool] = HassKey(f"{DOMAIN}_scene_store_reported")
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
    # Why the library's scene store couldn't be read at setup (None: it could).
    # Then nothing reads the device's scenes: each read would hit the file again.
    scene_store_error: str | None = None


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


@callback
def _async_report_scene_store(hass: HomeAssistant, err: GoveeBleError) -> None:
    """Log, once per run, that the installed library's scene store can't be read."""
    if hass.data.get(_SCENE_STORE_REPORTED):
        _LOGGER.debug("Scene store still unreadable: %s", err)
        return
    hass.data[_SCENE_STORE_REPORTED] = True
    # Both in memory: no package-metadata read on the event loop.
    _LOGGER.error(
        "The installed govee-ble-local (%s, %s) can't read its bundled scene store: %s. "
        "Govee lights keep working without effects. To fix it: on Home Assistant OS the "
        "next Home Assistant update reinstalls the library; in a Docker container, recreate "
        "the container (a restart keeps the broken copy); in a Python virtual environment, "
        "run 'pip install --force-reinstall govee-ble-local' and restart. If it persists, "
        "report it with this device's diagnostics",
        govee_ble_local.__version__,
        Path(govee_ble_local.__file__).parent,
        err,
    )


def _first_poll_stagger(hass: HomeAssistant, address: str) -> int:
    """Return the seconds this device's first background poll waits after setup.

    Derived from the address, so it is the same on every restart, over a window
    that grows with the number of configured devices (see FIRST_POLL_STAGGER_*).
    """
    devices = len(
        hass.config_entries.async_entries(DOMAIN, include_ignore=False, include_disabled=False)
    )
    window = min(
        max(FIRST_POLL_STAGGER_SECONDS, FIRST_POLL_STAGGER_PER_DEVICE_SECONDS * devices),
        FIRST_POLL_STAGGER_MAX_SECONDS,
    )
    return zlib.crc32(address.encode()) % window


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


@callback
def _async_broadcast(
    hass: HomeAssistant, entry: GoveeBleLocalConfigEntry, advertised: Broadcast | None
) -> Broadcast | None:
    """Return the device's Govee broadcast header: the ``advertised`` one, else the stored one.

    An advertised header that differs from the stored one replaces it, so the entry keeps
    the latest the device has shown: one that the library refuses included, since a later
    restart without a record should wait for the device rather than route it by an older
    pact. An entry set up before the header was stored gets one this way, from the first
    setup that hears it.
    """
    if advertised is None:
        return Broadcast.from_data(entry.data.get(CONF_BROADCAST))
    if entry.data.get(CONF_BROADCAST) != advertised.as_data():
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_BROADCAST: advertised.as_data()}
        )
    return advertised


def _raise_not_supported(
    sku: str, broadcast: Broadcast | None, *, allow_modelled: bool, err: GoveeBleNotSupported
) -> NoReturn:
    """Raise the setup error for a model ``create_device`` refused.

    The library lists every model it serves at some pact (``supported_skus``), and some
    only at their rows' pacts, so a refusal is final only for a model it doesn't list.
    """
    if sku.upper() not in supported_skus(allow_modelled=allow_modelled):
        # Permanent: retrying can't make an unsupported model supported.
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="model_not_supported",
            translation_placeholders={"sku": sku},
        ) from err
    if broadcast is None:
        # The library serves this model only at the pact it advertises, and none has been
        # heard yet: the next advertisement carries it.
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="protocol_unknown",
            translation_placeholders={"sku": sku},
        ) from err
    # The library serves the model, only not at the pact the device advertises now.
    # A later advert can carry another, so retry instead of giving up for good.
    raise ConfigEntryNotReady(
        translation_domain=DOMAIN,
        translation_key="protocol_not_supported",
        translation_placeholders={"sku": sku, "protocol": "{},{}".format(*broadcast.pact)},
    ) from err


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
    # `encrypt` flag) and routes the model by its pact, so pass it through when we have it.
    service_info = bluetooth.async_last_service_info(hass, address, connectable=True)
    advertisement: AdvertisementData | GoveeAdvertisement | None = (
        service_info.advertisement if service_info else None
    )
    sku = _resolve_sku(entry, service_info)
    advertised = advertised_broadcast(service_info)
    broadcast = _async_broadcast(hass, entry, advertised)
    if advertised is None and broadcast is not None:
        # Home Assistant's record of the device has no Govee block (none heard since a
        # restart, or one merged with a record without it): build it from the last one heard.
        advertisement = broadcast.advertisement(sku, service_info.name if service_info else "")
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
        _raise_not_supported(sku, broadcast, allow_modelled=allow_modelled, err=err)
    # Close the link whenever the entry goes away (a failed setup included).
    # HA runs on-unload jobs last in, first out, so registering this before the
    # coordinator exists means polling is shut down first. HA cancels only entry
    # background tasks - the scheduled, first and return polls - which frees the
    # connection for the close. An entity command, a manual update_entity
    # refresh or an overrunning self-test is not cancelled, and the close waits
    # for it: HA stops waiting after 10 s, logs that the task "did not complete
    # in time" and finishes the unload while the close completes in the
    # background. A command still waiting at the connect gate is let through as
    # the unload starts (async_unload_entry), so it reaches the link first.
    entry.async_on_unload(device.stop)

    # Load the (blocking, cached) scene catalog off the event loop, so the scene
    # reads on the loop afterwards (the effect list, diagnostics) are cache hits.
    scene_store_error: str | None = None
    if Capability.SCENES in device.capabilities:
        try:
            await hass.async_add_executor_job(lambda: device.scene_names)
        except GoveeBleError as err:
            # A missing or corrupt bundled scene store raises for every model.
            # The light works without effects; only a reinstall of the library
            # brings them back.
            scene_store_error = str(err)
            _async_report_scene_store(hass, err)

    _async_remove_legacy_zone_entities(hass, address, device)

    # Always-on WARNING+ capture so unrecognised frames / rejections surface in the
    # downloadable diagnostics without the user having to enable debug logging.
    log_capture = LogCapture(address)
    entry.async_on_unload(log_capture.detach)

    coordinator = GoveeBleLocalCoordinator(hass, entry, device)
    entry.runtime_data = GoveeBleLocalRuntimeData(
        device=device,
        coordinator=coordinator,
        log_capture=log_capture,
        scene_store_error=scene_store_error,
    )

    # The device is advertising (async_ble_device_from_address above found it),
    # so mark the entry ready from passive state instead of blocking setup on a
    # full connect+handshake for every device on every restart - that startup
    # storm is what exhausts connection slots. The first real poll runs in the
    # background, staggered per device; a failure there is handled by the
    # coordinator's backoff, not ConfigEntryNotReady. HA cancels it (like every
    # entry background task) on unload.
    coordinator.async_set_updated_data(device.state)
    stagger = _first_poll_stagger(hass, address)

    async def _staggered_first_poll() -> None:
        _LOGGER.debug("Waiting %ds stagger before first poll of %s", stagger, address)
        await asyncio.sleep(stagger)
        # The same dispatch as HA's timer: a command sent meanwhile holds it off,
        # and one hold-off releases one poll however many came due during it.
        await coordinator.async_background_poll()

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
        # No held poll is released from now on (HA's close stage refuses none).
        data.coordinator.async_note_ha_stopping()
        # A command still waiting at the connect gate goes ahead now, and gets a
        # turn of the loop to reach the link before the close below takes it.
        async_release_waiters(hass, entry.data[CONF_ADDRESS])
        await asyncio.sleep(0)
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
    def _async_connectable_lost(info: bluetooth.BluetoothServiceInfoBleak) -> None:
        coordinator.async_handle_unavailable(connectable=True, last=info)

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
    # A command still waiting at the connect gate goes ahead now, and gets a
    # turn of the loop to reach the link, so the close (an on-unload job) waits
    # for it, as for one already using the link.
    async_release_waiters(hass, entry.data[CONF_ADDRESS])
    await asyncio.sleep(0)
    # The capture_session service is registered in async_setup (integration-wide),
    # so it is intentionally left in place across entry unloads.
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    if (self_test := entry.runtime_data.self_test) is not None:
        # Closing the link mid-test would leave the device in a test state.
        await asyncio.wait({self_test}, timeout=SELF_TEST_UNLOAD_TIMEOUT_SECONDS)
    return True
