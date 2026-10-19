"""Connection coordinator for a Govee BLE device, shared by all entities.

``Device.update()`` connects and reads back real state for devices that support
it (profile ``readback`` of ``status``/``polled``/``plug``) and otherwise returns
the device's best-known (optimistic) :class:`DeviceState`. This coordinator runs
that poll in the background on a jittered cadence with capped exponential
backoff on failure; on/off is also tracked passively from advertisements (no
connection).

How often a device is polled in the background is decided per device by one
policy (``_async_policy_step``). While Home Assistant keeps hearing a device's
advertised on/off byte, a light that is off and a relay plug get no background
polls, a light turned on outside Home Assistant gets one poll shortly after,
and a light that is on is polled with a backoff while polls find nothing new.
A device whose byte isn't heard, one no connectable scanner sees, an excluded
model, or an install whose library predates the app-matching advertisement
parser keeps the fixed cadence. Because HA calls back only when an
advertisement's content changes, the presence tick re-offers HA's newest
advertisement to the library (``_async_reoffer_advertisement``), so an on/off
change the library ignored right after HA's own I/O isn't lost while polls are
off.

Availability is driven by *reachability* (is any connectable scanner currently
seeing the device's advertisements?), not by whether the last poll succeeded: a
poll can fail for transient reasons (a busy proxy, a dropped link) while the
device is perfectly controllable, and marking it unavailable then blocks the
very commands that would succeed.

Two notification channels keep state writes proportional to what changed:
``async_update_listeners`` reaches every entity (device state or availability
changed), ``async_update_diagnostics`` only the diagnostic entities (last seen,
RSSI, failure count, poll interval). Last seen / RSSI are refreshed from HA's
advertisement history on a timer (``async_refresh_presence``): the
advertisement callback only fires when an advertisement's content changes.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta
from enum import StrEnum
import logging
import math
import random
from typing import TYPE_CHECKING, Any

from govee_ble_local import Device, DeviceState, GoveeBleAuthError, const as lib_const
from govee_ble_local.identify import parse_broadcast_onoff
from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import (
    ADVERT_FLIP_POLL_DELAY_SECONDS,
    ADVERT_POWER_FRESH_SECONDS,
    DOMAIN,
    EXCLUDED_FROM_ADVERT_POLICY,
    MAX_CONCURRENT_POLLS,
    MAX_POLL_INTERVAL_SECONDS,
    ON_POLL_MAX_INTERVAL_SECONDS,
    POLL_CONNECT_ATTEMPTS,
    POLL_CONNECT_TIMEOUT_SECONDS,
    POLL_HOLDOFF_AFTER_COMMAND_SECONDS,
    POLL_HOLDOFF_MAX_SECONDS,
    POLL_INTERVAL_SECONDS,
    POLL_JITTER_SECONDS,
    SLOW_POLL_MAX_STEPS,
    SLOW_POLL_SECONDS,
    UNAVAILABLE_AFTER_FAILURES,
    WEAK_SIGNAL_RSSI,
)
from .helpers import BLE_ERRORS, kelvin_range

if TYPE_CHECKING:
    from . import GoveeBleLocalConfigEntry

_LOGGER = logging.getLogger(__name__)

_POLL_SEMAPHORE: HassKey[asyncio.Semaphore] = HassKey(f"{DOMAIN}_poll_semaphore")

_BASE_INTERVAL = timedelta(seconds=POLL_INTERVAL_SECONDS)
_MAX_INTERVAL = timedelta(seconds=MAX_POLL_INTERVAL_SECONDS)
_ON_MAX_INTERVAL = timedelta(seconds=ON_POLL_MAX_INTERVAL_SECONDS)
# Each backoff doubles per step until it reaches its cap; more doublings than
# that only risk overflowing timedelta.
_MAX_BACKOFF_STEPS = math.ceil(math.log2(_MAX_INTERVAL / _BASE_INTERVAL))
_MAX_ON_BACKOFF_STEPS = math.ceil(math.log2(_ON_MAX_INTERVAL / _BASE_INTERVAL))
# How long the library ignores advertisements after a command or a read that
# returned power. govee_ble_local.const has it on the libraries the
# advertisement policy runs with; the 1.2.1 floor keeps the same 15 s elsewhere.
_ADVERT_SUPPRESS_SECONDS = float(getattr(lib_const, "ADVERT_SUPPRESS_SECONDS", 15.0))


class PollPolicy(StrEnum):
    """How a device is polled in the background (see ``_async_policy_step``)."""

    # POLL_INTERVAL_SECONDS plus jitter: the cadence of a device the
    # advertisement policy doesn't cover.
    FIXED = "fixed"
    # A light that is on, followed by its advertisements: backs off from
    # POLL_INTERVAL_SECONDS while polls find nothing new.
    ON = "on"
    # A light that is off, followed by its advertisements: no background polls.
    OFF = "off"
    # A relay plug followed by its advertisements: no background polls.
    RELAY = "relay"


# The steps with no background polls.
_UNPOLLED = frozenset({PollPolicy.OFF, PollPolicy.RELAY})


def _poll_semaphore(hass: HomeAssistant) -> asyncio.Semaphore:
    """Return the domain-wide cap on simultaneous polls.

    The HA Bluetooth manager already routes each connect to a scanner with a
    free slot (and bleak-retry-connector backs off on out-of-slots); this only
    keeps the integration's own background polling from launching a connect
    storm.
    """
    if (semaphore := hass.data.get(_POLL_SEMAPHORE)) is None:
        semaphore = hass.data[_POLL_SEMAPHORE] = asyncio.Semaphore(MAX_CONCURRENT_POLLS)
    return semaphore


def has_app_advert_parser(device: Device) -> bool:
    """Return whether the library reads the advertised on/off byte the way the Govee app does.

    The libraries with ``DeviceProfile.advert_on_mask`` trust only a broadcast
    whose header says it carries a switch state, as the app does, and decode a
    plug's byte with the plug's mask (added after 1.3.0). Older ones (the 1.2.1
    floor, 1.3.0) read the byte after the pact fields of any Govee broadcast,
    which may not be a switch state, so the advertisement policy stays off there
    and every device keeps the fixed cadence.
    """
    return hasattr(device.profile, "advert_on_mask")


def _polled_state(state: DeviceState) -> dict[str, Any]:
    """Return a copy of what a poll read, to tell whether the next one found anything new."""
    polled = asdict(state)
    polled.pop("optimistic", None)  # bookkeeping, not device state
    return polled


@callback
def _async_subscribe(
    listeners: list[CALLBACK_TYPE], update_callback: CALLBACK_TYPE
) -> CALLBACK_TYPE:
    """Add ``update_callback`` to ``listeners``; return the callback that removes it."""
    listeners.append(update_callback)

    @callback
    def _remove() -> None:
        listeners.remove(update_callback)

    return _remove


class GoveeBleLocalCoordinator(DataUpdateCoordinator[DeviceState]):
    """Polls one device and tracks its reachability."""

    config_entry: GoveeBleLocalConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GoveeBleLocalConfigEntry,
        device: Device,
    ) -> None:
        """Initialize the coordinator for one config entry's device."""
        address: str = entry.data[CONF_ADDRESS]
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{address}",
            update_interval=_BASE_INTERVAL,
        )
        self._device = device
        self._address = address
        self.consecutive_failures = 0
        # Read-only diagnostics surfaced by the sensor/binary_sensor platforms.
        self.rssi: int | None = None
        self.total_failures = 0
        # The poll interval before jitter (base or backed off; None while the
        # advertisement policy has background polls off): what the diagnostics
        # show, so they only change when the cadence does.
        self.nominal_interval: timedelta | None = _BASE_INTERVAL
        # "Last seen" = most recent advertisement any scanner heard (passive, no
        # connection); "last connected" = most recent poll that read real state
        # back. They answer different questions: reachability vs. successful control.
        self.last_seen: datetime | None = None
        self.last_connected: datetime | None = None
        self._diagnostic_listeners: list[CALLBACK_TYPE] = []
        self._state_listeners: list[CALLBACK_TYPE] = []
        # Receive time (monotonic) of the advertisement last_seen/RSSI came from.
        self._presence_time: float | None = None
        # Availability as last pushed to the entities (None = never pushed).
        self._published_available: bool | None = None
        # A poll was skipped because the device wasn't advertising; when it
        # reappears, poll right away instead of waiting out the backoff.
        self._skipped_absent = False
        # The last poll was rejected for the secret: HA schedules no more polls.
        self._auth_failed = False
        # A command already dropped the backoff during this failure streak.
        self._command_reset_used = False
        # Whether the advertisement policy covers this device at all: never an
        # excluded model, nor with a library whose parser doesn't match the app's.
        self._advert_policy = (
            has_app_advert_parser(device) and device.sku not in EXCLUDED_FROM_ADVERT_POLICY
        )
        # The policy's current step (in the diagnostics).
        self.poll_policy = PollPolicy.FIXED
        # Doublings of a light's on-backoff (polls in a row that found nothing new).
        self._on_backoff_steps = 0
        # What the last successful poll read (None before the first).
        self._last_polled: dict[str, Any] | None = None
        # The pending poll after an advertised turn-on.
        self._unsub_flip_poll: CALLBACK_TYPE | None = None
        # The device dropped out of reach while it had no background polls (so
        # its absence alone made it unavailable) and they have resumed since:
        # it stays unavailable until it is heard again, instead of getting the
        # grace of UNAVAILABLE_AFTER_FAILURES failed polls.
        self._lost_while_unpolled = False
        # Bluetooth monotonic time HA last talked to the device (a poll, a
        # command or a self-test); None before the first time.
        self._io_at: float | None = None
        # The manufacturer data a re-offered advertisement last changed on/off
        # from (see _async_reoffer_advertisement).
        self._reoffered: dict[int, bytes] | None = None
        # Entity commands running now, and the timer that ends the hold-off
        # POLL_HOLDOFF_AFTER_COMMAND_SECONDS after the last one: background polls
        # wait for both (``commands_holding_off``). Whether a background poll is
        # waiting for the hold-off, and the timer that runs it anyway once it
        # has waited POLL_HOLDOFF_MAX_SECONDS. Set once the entry is unloading.
        self._commands_running = 0
        self._unsub_holdoff_end: CALLBACK_TYPE | None = None
        self._poll_waiting = False
        self._unsub_holdoff_cap: CALLBACK_TYPE | None = None
        self._shutting_down = False
        # Bluetooth monotonic time each zone's power (by power index) was last
        # confirmed: a poll read it back, or the device acked a write that set
        # it. The library assigns state.zone_power a new dict whenever it applies
        # one (a read, or an acked write), and never on a read that timed out or
        # was discarded, so a new dict across a poll or a command is the signal.
        self._zones_confirmed_at: dict[int, float] = {}
        # Doublings of the poll interval for a slow-connecting or weak-signal
        # device, and how long the last successful poll took (diagnostics).
        self.slow_poll_steps = 0
        self.last_poll_seconds: float | None = None

    # -- reachability -------------------------------------------------------

    @callback
    def async_address_present(self, *, connectable: bool = True) -> bool:
        """Return True if a scanner (connectable by default) currently sees the device."""
        return bluetooth.async_address_present(self.hass, self._address, connectable=connectable)

    @callback
    def async_last_service_info(
        self, *, connectable: bool = True
    ) -> bluetooth.BluetoothServiceInfoBleak | None:
        """Return the advertisement HA keeps for a (connectable by default) scanner, if any."""
        return bluetooth.async_last_service_info(self.hass, self._address, connectable=connectable)

    @property
    def _polls_enabled(self) -> bool:
        """Return False while polling is disabled in system options or halted by the secret."""
        return not (self.config_entry.pref_disable_polling or self._auth_failed)

    @property
    def scheduled_interval(self) -> timedelta | None:
        """Return the nominal interval of HA's background polls; None while HA schedules none.

        HA schedules none while polling is disabled in the entry's system
        options, after a rejected secret until a poll runs again (a reload with
        a new secret, or a manual refresh), or while the advertisement policy
        has background polls off (``PollPolicy.OFF`` / ``RELAY``).
        """
        return self.nominal_interval if self._polls_enabled else None

    @property
    def polls_scheduled(self) -> bool:
        """Return whether HA schedules background polls (see ``scheduled_interval``)."""
        return self.scheduled_interval is not None

    @property
    def device_available(self) -> bool:
        """Entity availability: reachable over BLE, or still polling fine.

        A device that is advertising stays available through transient poll
        failures (commands can still reach it); one that has vanished from every
        connectable scanner goes unavailable once polls have also failed (or
        been skipped) a few times - or at once when no background polls are
        scheduled to count those failures (see ``polls_scheduled``), and it
        stays so when they resume before it is heard again.
        """
        if self.async_address_present():
            return True
        if not self.polls_scheduled or self._lost_while_unpolled:
            return False
        return self.consecutive_failures < UNAVAILABLE_AFTER_FAILURES

    # -- notifications ------------------------------------------------------

    @callback
    def async_update_listeners(self) -> None:
        """Push to every entity and state listener, remembering the availability pushed.

        The entities come first, and a state listener that raises is logged
        rather than propagated, the way HA isolates coordinator listeners: a
        failing hook must not cost the entities their update.
        """
        self._published_available = self.device_available
        super().async_update_listeners()
        for update_callback in list(self._state_listeners):
            try:
                update_callback()
            except Exception:
                self.logger.exception(
                    "Unexpected error updating state listener %s for %s",
                    id(update_callback),
                    self.name,
                )

    @callback
    def async_add_state_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Follow every push to all entities; returns the unsubscribe callback.

        Unlike ``async_add_listener`` this doesn't keep HA's poll timer running
        (HA polls while the coordinator has listeners), so a hook that isn't an
        entity can't keep polling a device whose entities are all disabled.
        """
        return _async_subscribe(self._state_listeners, update_callback)

    @callback
    def async_add_diagnostic_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Listen for diagnostic-only changes; returns the unsubscribe callback."""
        return _async_subscribe(self._diagnostic_listeners, update_callback)

    @callback
    def async_update_diagnostics(self) -> None:
        """Push a diagnostic-only change (no device state changed)."""
        for update_callback in list(self._diagnostic_listeners):
            update_callback()

    @callback
    def async_publish_device_state(self) -> None:
        """Publish the device's (in-place mutated) state to every entity.

        Unlike ``async_set_updated_data`` this doesn't mark a failing poll as
        successful, and it leaves the poll timer alone unless the new state
        moves the advertisement policy to another step (a light turned off
        stops its background polls): passive advertisement changes must not
        postpone the next poll or re-arm HA's log-once gate.
        """
        self.data = self._device.state
        self._async_apply_policy()
        self.async_update_listeners()

    @callback
    def _async_publish_status(self) -> None:
        """Push a reachability change to everyone, anything else to diagnostics."""
        if self.device_available != self._published_available:
            self.async_update_listeners()
        else:
            self.async_update_diagnostics()

    # -- passive (advertisement) path ---------------------------------------

    @callback
    def async_handle_advertisement(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        """Take on/off (and from a connectable scanner, the BLEDevice) from an advertisement.

        HA calls this from any scanner, but only when the advertisement's content
        changed, not for each repeat: last seen / RSSI therefore come from the
        history on a timer (``async_refresh_presence``), except when the device
        is heard again after no scanner did. Only pushes to the entities when
        something they show actually changed: on/off, or a pact the device learns
        from it (and the Kelvin range that pact picks). Each call re-checks the
        poll policy too.
        """
        if service_info.connectable:
            # Keep the freshest BLEDevice (best scanner/proxy path) for the next
            # connection; a passive scanner's can't be connected through.
            self._device.update_ble_device(service_info.device)
        # New content: HA offers it once itself.
        self._reoffered = None

        return_poll = False
        if self._skipped_absent and self.async_address_present():
            # Back after polls were skipped for its absence: drop the backoff and
            # reconcile the rich state now instead of up to MAX_POLL_INTERVAL later.
            self._skipped_absent = False
            if self.poll_policy not in _UNPOLLED:
                self._set_poll_interval(_BASE_INTERVAL)
            self.config_entry.async_create_background_task(
                self.hass, self.async_request_refresh(), name=f"{self.name}_return_poll"
            )
            return_poll = True

        # Heard again after no scanner did: show it now, not at the next refresh.
        heard_again = False
        if self.rssi is None:
            heard_again = self._take_presence(service_info)
        was_on = self._device.state.is_on
        learned_before = self._advert_learned()
        changed = self._device.ingest_advertisement(service_info.advertisement)
        if changed and self._device.state.is_on != was_on:
            # The return poll already reads what a turn-on poll would.
            self._async_handle_power_flip(was_on, schedule_poll=not return_poll)
            self.async_publish_device_state()
        elif changed or self._advert_learned() != learned_before:
            # The advert gave the device its pact, which can pick another Kelvin range
            # (kelvin_by_pact): show it now, not at the next poll (up to 15 minutes away).
            # Not an on/off flip, whether or not the library reports it as a change.
            self.async_publish_device_state()
        elif self._async_apply_policy() or self.device_available != self._published_available:
            self._async_publish_status()
        elif heard_again:
            self.async_update_diagnostics()

    def _advert_learned(self) -> tuple[tuple[int, int] | None, tuple[int, int]]:
        """Return what an advert can change besides on/off: the pact and the Kelvin range."""
        return self._device.pact, kelvin_range(self._device)

    @callback
    def _async_handle_power_flip(self, was_on: bool | None, *, schedule_poll: bool = True) -> None:
        """React to an advertised on/off change the library took.

        The change came from outside HA: the library ignores advertisements for
        ADVERT_SUPPRESS_SECONDS after HA's own commands and reads. It starts a
        light's on-backoff over, and a light the policy follows that was just
        turned on is polled once ADVERT_FLIP_POLL_DELAY_SECONDS later (a newer
        change replaces a pending poll), unless ``schedule_poll`` is False (a
        return poll is already running). A first on/off from unknown (the
        history HA replays at setup) isn't a turn-on: the first poll is due anyway.
        """
        self._async_cancel_flip_poll()
        self._async_apply_policy()
        self._async_restart_on_backoff()
        if was_on is False and self.poll_policy is PollPolicy.ON:
            # The poll after a turn-on reads the light as it came on: a change,
            # even if it matches the poll from before it went off (so the next
            # poll comes at the base cadence, not a doubled one).
            self._last_polled = None
            if schedule_poll:
                self._unsub_flip_poll = async_call_later(
                    self.hass, ADVERT_FLIP_POLL_DELAY_SECONDS, self._async_flip_poll_due
                )

    @callback
    def _async_flip_poll_due(self, _now: datetime) -> None:
        """Run the poll scheduled after an advertised turn-on.

        Checked when it fires, not when it was scheduled: HA's stop doesn't
        cancel an async_call_later timer and a refresh it didn't schedule isn't
        refused while stopping, so this would poll a link the stop listener is
        closing. Nor does it poll after a rejected secret or with polling
        disabled since (halted polls stay halted), or while no entity listens
        (like HA's own timer).
        """
        self._unsub_flip_poll = None
        if not (self.hass.is_running and self._polls_enabled and self._listeners):
            return
        self.config_entry.async_create_background_task(
            self.hass, self.async_request_refresh(), name=f"{self.name}_turn_on_poll"
        )

    @callback
    def _async_cancel_flip_poll(self) -> None:
        """Drop a pending poll after an advertised turn-on, if any."""
        if self._unsub_flip_poll is not None:
            self._unsub_flip_poll()
            self._unsub_flip_poll = None

    async def async_shutdown(self) -> None:
        """Also drop a pending turn-on poll and hold-off (HA calls this on unload)."""
        self._shutting_down = True
        self._async_cancel_flip_poll()
        self._async_cancel_holdoff_end()
        await super().async_shutdown()

    @callback
    def async_refresh_presence(self, _now: datetime | None = None) -> None:
        """Publish last seen / RSSI from the newest advertisement any scanner heard.

        Runs every LAST_SEEN_RESOLUTION_SECONDS (see __init__): HA's history
        records every advertisement, while the advertisement callback fires once
        for a device that keeps repeating the same one. It re-offers that
        advertisement to the library (``_async_reoffer_advertisement``) and
        re-checks the poll policy too, so a device whose on/off byte goes stale
        is back on the fixed cadence within one tick (its first poll follows one
        POLL_INTERVAL_SECONDS, plus jitter, later).
        """
        info = self.async_last_service_info(connectable=False)
        took = info is not None and self._take_presence(info)
        if info is not None and self._async_reoffer_advertisement(info):
            return  # published to every entity
        if self._async_apply_policy():
            self._async_publish_status()
        elif took:
            self.async_update_diagnostics()

    @callback
    def _async_reoffer_advertisement(self, info: bluetooth.BluetoothServiceInfoBleak) -> bool:
        """Offer HA's newest advertisement to the library again; return True if on/off flipped.

        HA calls ``async_handle_advertisement`` only when an advertisement's
        content changes, so two on/off changes would otherwise never reach the
        library while the device keeps repeating them: one it ignored because
        it came within ADVERT_SUPPRESS_SECONDS of HA's own command or read, and
        one whose advertisement matches what HA heard before a command (the
        device doesn't advertise while HA holds the link). With background
        polls off nothing else would correct them.

        So the presence tick offers the newest advertisement again if HA
        received it more than that window after it last talked to the device:
        one the library would have taken had HA called back for it. One from
        before that (still the newest while the link stays up after a command)
        is never offered again, so the stale flip-back the window prevents
        can't come back this way. The library still folds and deduplicates it:
        on/off still comes from its state.

        Only for devices the policy covers, with a fresh byte, and once HA has
        talked to the device (before that, every content change reached the
        library through the callback). Like the callback, it takes a change
        from each content once: a poll that then reads the opposite (a byte
        that doesn't follow the device, or an older one an H6006 leaves in HA's
        merged entry when a scanner misses its scan response) isn't overridden
        every tick, only after the content changes or HA's next command.
        """
        if not (
            self._advert_policy
            and self._io_at is not None
            and info.time > self._io_at + _ADVERT_SUPPRESS_SECONDS
            and info.manufacturer_data != self._reoffered
            and self._async_advert_power_fresh()
        ):
            return False
        was_on = self._device.state.is_on
        if not self._device.ingest_advertisement(info.advertisement):
            return False
        self._reoffered = dict(info.manufacturer_data)
        _LOGGER.debug(
            "%s: on/off %s -> %s from the repeated advertisement",
            self.name,
            was_on,
            self._device.state.is_on,
        )
        self._async_handle_power_flip(was_on)
        self.async_publish_device_state()
        return True

    def _take_presence(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> bool:
        """Take last seen / RSSI from an advertisement; False if it was already taken."""
        if service_info.time == self._presence_time:
            return False
        self._presence_time = service_info.time
        self.rssi = service_info.rssi
        # Stamped with the monotonic time it was received: a history entry can
        # be minutes old (e.g. one from the BlueZ cache at startup).
        age = max(0.0, bluetooth.MONOTONIC_TIME() - service_info.time)
        self.last_seen = dt_util.utcnow() - timedelta(seconds=age)
        return True

    @callback
    def async_handle_unavailable(self, *, connectable: bool) -> None:
        """React to the device dropping out of the (connectable) scanner history."""
        if not connectable:
            # No scanner at all sees it any more: the last RSSI is meaningless.
            self.rssi = None
        self._async_apply_policy()
        # HA drops both histories in one sweep, and usually only the diagnostics
        # change (connectivity, RSSI, connection source): push to everyone only
        # when availability flipped.
        self._async_publish_status()

    @callback
    def async_note_device_io(self) -> None:
        """Note that HA just talked to the device: a command (landed or not) or a self-test.

        Polls note themselves. The library ignores advertisements for a while
        after that, and HA's history may still hold one from before it; see
        ``_async_reoffer_advertisement``. A command may have changed the
        device's state, so a repeated advertisement may be offered again.
        """
        self._io_at = bluetooth.MONOTONIC_TIME()
        self._reoffered = None

    @property
    def commands_holding_off(self) -> bool:
        """Whether background polls wait: a command is running, or one ended just now.

        "Just now" is POLL_HOLDOFF_AFTER_COMMAND_SECONDS after the last one ended.
        """
        return bool(self._commands_running) or self._unsub_holdoff_end is not None

    @callback
    def async_note_command_started(self) -> dict[int, bool]:
        """Note an entity command starting: background polls wait until it is done.

        Returns the zone power it starts from, for ``async_note_command_finished``.
        """
        self._commands_running += 1
        self._async_cancel_holdoff_end()  # the hold-off restarts when the last one ends
        return self._device.state.zone_power

    @callback
    def async_note_command_finished(self, zones_before: dict[int, bool] | None = None) -> None:
        """Note an entity command ending (landed or not): polls wait a little longer still.

        ``zones_before`` (from ``async_note_command_started``): zone power the
        command set, the device having acked it, counts as confirmed.
        """
        if zones_before is not None:
            self._note_zone_power(zones_before)
        self._commands_running = max(0, self._commands_running - 1)
        if self._commands_running or self._shutting_down:
            # Another command still runs (the hold-off starts when it ends), or the
            # entry is unloading: a timer armed now would outlive it.
            return
        self._async_cancel_holdoff_end()
        self._unsub_holdoff_end = async_call_later(
            self.hass, POLL_HOLDOFF_AFTER_COMMAND_SECONDS, self._async_end_holdoff
        )

    @callback
    def _async_end_holdoff(self, _now: datetime) -> None:
        """End the hold-off, and run the background poll it held back, if any."""
        self._unsub_holdoff_end = None
        if self._poll_waiting:
            self._async_release_poll()

    @callback
    def _async_cancel_holdoff_end(self) -> None:
        """Drop the timer that would end the hold-off, if any."""
        if self._unsub_holdoff_end is not None:
            self._unsub_holdoff_end()
            self._unsub_holdoff_end = None

    @callback
    def _async_hold_poll(self) -> None:
        """Keep a background poll that came due until the hold-off ends, or for at most the cap.

        Only the first poll to come due arms the cap: later ones join it, and one
        poll runs when the hold-off is released, however many came due.
        """
        if self._poll_waiting:
            return
        self._poll_waiting = True
        self._unsub_holdoff_cap = async_call_later(
            self.hass, POLL_HOLDOFF_MAX_SECONDS, self._async_holdoff_cap_reached
        )

    @callback
    def _async_holdoff_cap_reached(self, _now: datetime) -> None:
        """Run the held poll anyway: a stream of commands mustn't postpone it for ever."""
        self._unsub_holdoff_cap = None
        if self._poll_waiting:
            self._async_release_poll()

    @callback
    def _async_release_poll(self) -> None:
        """Run the held background poll, as HA's timer would have."""
        self._async_drop_held_poll()
        self.config_entry.async_create_background_task(
            self.hass,
            super()._handle_refresh_interval(),
            name=f"{self.name}_held_off_poll",
            eager_start=True,
        )

    @callback
    def _async_drop_held_poll(self) -> None:
        """Forget the held background poll and its cap timer."""
        self._poll_waiting = False
        if self._unsub_holdoff_cap is not None:
            self._unsub_holdoff_cap()
            self._unsub_holdoff_cap = None

    async def async_background_poll(self) -> None:
        """Run a background poll now, or hold it while commands hold polls off.

        The first poll after setup comes here; HA's scheduled polls come through
        ``_handle_refresh_interval``. Both hold the same way, so one hold-off
        releases one poll.
        """
        if self.commands_holding_off:
            self._async_hold_poll()
            return
        await self.async_refresh()

    async def _handle_refresh_interval(self, _now: datetime | None = None) -> None:
        """Run a scheduled poll, unless an entity command is running or just ran.

        A poll then would take the device for its reads while the user is likely
        to follow up (a second tap, a slider drag), and HA already shows what the
        command did. It is held until the hold-off ends (``_async_end_holdoff``),
        or for at most POLL_HOLDOFF_MAX_SECONDS (``_async_holdoff_cap_reached``).
        Overrides DataUpdateCoordinator's scheduled-refresh entry point (present
        since before 2025.10; from 2026.10 it also takes the debouncer's lock).
        """
        if self.commands_holding_off:
            self._unsub_refresh = None  # the timer that just fired
            self._async_hold_poll()
            return
        await super()._handle_refresh_interval(_now)

    def _async_unsub_refresh(self) -> None:
        """Cancel the scheduled poll, a held one included.

        HA calls this before arming a new timer (``_schedule_refresh``) and on
        shutdown: the new timer replaces a held poll, so two never run.
        """
        super()._async_unsub_refresh()
        self._async_drop_held_poll()

    @callback
    def async_handle_command_success(self) -> None:
        """Drop the poll backoff: an entity command just reached the device.

        A command is a change, so a light that is on starts its on-backoff
        over. After failed polls the link demonstrably works again, so the next
        poll comes at the base cadence instead of up to MAX_POLL_INTERVAL_SECONDS
        later. Only a backed-off interval is reset, so a stream of commands
        can't keep postponing the poll; the failure count is left for that poll
        to reset, so availability and the diagnostics stay driven by polls. The
        failure-backoff reset happens once per failure streak: when polls keep
        failing although commands land (a read-back that keeps failing),
        repeated commands must not hold the device at the base cadence. Nothing
        is re-armed while no polls are scheduled (``polls_scheduled``), and the
        policy is re-checked first (a command that turns a light off stops its
        background polls).
        """
        rescheduled = self._async_apply_policy()
        rescheduled = self._async_restart_on_backoff() or rescheduled
        interval = self.scheduled_interval
        if (
            self.consecutive_failures
            and not self._command_reset_used
            and interval is not None
            and interval > self._floored(_BASE_INTERVAL)
        ):
            self._command_reset_used = True
            self._set_poll_interval(_BASE_INTERVAL)
            # HA armed the poll timer with the backed-off interval: re-arm it.
            self._schedule_refresh()
            rescheduled = True
        if rescheduled:
            self.async_update_diagnostics()

    # -- poll policy ----------------------------------------------------------

    @callback
    def _async_advert_power_fresh(self) -> bool:
        """Return whether HA has recently heard this device's advertised on/off byte.

        The newest advertisement any scanner heard must carry the byte (read
        with the library's parser; only whether it is there matters, its value
        comes from the library's folded state) and be younger than
        ADVERT_POWER_FRESH_SECONDS. An H6006 sends the byte only in its scan
        response, which a passive scanner never asks for: heard that way it
        never counts as fresh and keeps the fixed cadence.

        HA merges each scanner's advertisement data with what that scanner
        heard before, so a byte from an earlier packet can stay in the entry
        after newer packets without manufacturer data (an advertisement
        without its scan response). The entry's receive time bounds that: the
        byte is only trusted while that scanner still hears the device.
        """
        info = self.async_last_service_info(connectable=False)
        return (
            info is not None
            and bluetooth.MONOTONIC_TIME() - info.time < ADVERT_POWER_FRESH_SECONDS
            and parse_broadcast_onoff(info.manufacturer_data) is not None
        )

    @callback
    def _async_policy_step(self) -> PollPolicy:
        """Decide how this device is polled in the background: the policy's one decision.

        Background polls stop only for a device HA otherwise follows: one the
        policy covers (a library with the app's advertisement parser, a model
        not excluded), that HA polls at all (not disabled in system options,
        no rejected secret), that a connectable scanner currently sees (an
        absent device keeps the skip, count and return-poll handling of the
        fixed cadence), and whose on/off byte is fresh. Then a relay plug
        (whose relay is its only state) and a light the library's state says is
        off get none, and a light that is on backs off. On/off comes from the
        library's state, which folds the advertised byte in (and ignores it for
        a while after HA's own commands and reads), never from the raw byte.
        Anything else keeps the fixed cadence.
        """
        if not (
            self._advert_policy
            and self._polls_enabled
            and self.async_address_present()
            and self._async_advert_power_fresh()
        ):
            return PollPolicy.FIXED
        if self._device.profile.relay:
            return PollPolicy.RELAY
        is_on = self._device.state.is_on
        if is_on is None:
            return PollPolicy.FIXED
        return PollPolicy.ON if is_on else PollPolicy.OFF

    @callback
    def _async_set_policy(self, step: PollPolicy) -> bool:
        """Move to ``step``; return True if it is a different step."""
        if step is self.poll_policy:
            return False
        _LOGGER.debug("%s: background poll policy %s -> %s", self.name, self.poll_policy, step)
        self.poll_policy = step
        self._on_backoff_steps = 0
        if step in _UNPOLLED:
            self._async_cancel_flip_poll()
        return True

    @callback
    def _async_apply_policy(self) -> bool:
        """Re-check the policy between polls; return True if it moved to another step.

        HA's poll timer is re-armed only when the interval changes, so checking
        on every advertisement and presence tick never postpones a poll. While
        polls fail, a change between the polled steps keeps the failure backoff.
        """
        present = self.async_address_present()
        if present:
            self._lost_while_unpolled = False
        previous = self.nominal_interval
        if not self._async_set_policy(self._async_policy_step()):
            return False
        if self.poll_policy in _UNPOLLED:
            self._async_clear_poll_interval()
            return True
        if previous is None:  # background polls resume
            self._async_note_polls_resume()
            nominal = self._failure_interval() if self.consecutive_failures else _BASE_INTERVAL
        elif self.consecutive_failures:
            return True
        else:
            nominal = _BASE_INTERVAL  # the on-backoff starts over
        nominal = self._floored(nominal)
        if nominal != previous:
            self._set_poll_interval(nominal)
            # Never arm a timer HA wouldn't keep: none while polling is
            # disabled or halted, nor while no entity listens.
            if self._polls_enabled and self._listeners:
                self._schedule_refresh()
        return True

    @callback
    def _async_restart_on_backoff(self) -> bool:
        """Start a light's on-backoff over after a change; return True if the timer was re-armed.

        Only a backed-off interval is cut back, so repeated changes can't keep
        postponing the poll.
        """
        self._on_backoff_steps = 0
        interval = self.scheduled_interval
        if (
            self.poll_policy is not PollPolicy.ON
            or self.consecutive_failures
            or interval is None
            or interval <= self._floored(_BASE_INTERVAL)
        ):
            return False
        self._set_poll_interval(_BASE_INTERVAL)
        self._schedule_refresh()
        return True

    def _healthy_interval(self) -> timedelta:
        """Return the interval after a successful poll: the on-backoff, or the base cadence."""
        if self.poll_policy is PollPolicy.ON:
            return min(_BASE_INTERVAL * (1 << self._on_backoff_steps), _ON_MAX_INTERVAL)
        return _BASE_INTERVAL

    def _floored(self, interval: timedelta) -> timedelta:
        """Return ``interval``, but no shorter than a slow device's floor (BASE x 2^steps).

        Every interval goes through it (``_set_poll_interval``), so a failure, a
        change or a policy step doesn't drop a slow device back to the base
        cadence; the comparisons that decide whether to re-arm use it too.
        """
        if not self.slow_poll_steps:
            return interval
        floor = min(_BASE_INTERVAL * (1 << self.slow_poll_steps), _MAX_INTERVAL)
        return max(interval, floor)

    def _failure_interval(self) -> timedelta:
        """Return the failure backoff for the current streak of failed (or skipped) polls.

        The first failure keeps the base cadence (grace for a transient blip),
        then it doubles up to the cap.
        """
        steps = min(max(0, self.consecutive_failures - 1), _MAX_BACKOFF_STEPS)
        return min(_BASE_INTERVAL * (1 << steps), _MAX_INTERVAL)

    # -- polling ------------------------------------------------------------

    async def _async_update_data(self) -> DeviceState:
        # Any poll that runs (a manual refresh after an auth failure included)
        # means polling is scheduled again.
        self._auth_failed = False
        if not self.async_address_present():
            # Nothing can reach it: a connect would just burn ~1 min of
            # bleak-retry-connector attempts (and a poll slot) to fail.
            self._skipped_absent = True
            self._async_note_failure()
            raise self._update_failed("poll_skipped_absent")

        async with _poll_semaphore(self.hass):
            started = bluetooth.MONOTONIC_TIME()
            zones_before = self._device.state.zone_power
            try:
                # A tighter connect budget than a command's: a device that doesn't
                # answer frees the device (and a command waiting for it) sooner.
                data = await self._device.update(
                    connect_attempts=POLL_CONNECT_ATTEMPTS,
                    connect_timeout=POLL_CONNECT_TIMEOUT_SECONDS,
                )
            except GoveeBleAuthError as err:
                # Starts HA's reauth flow (config_flow.async_step_reauth); HA stops
                # scheduling polls until the entry is reloaded with a new secret.
                self._auth_failed = True
                # HA doesn't notify listeners when this follows a failed poll;
                # the poll interval (and availability) change regardless.
                self._async_publish_status()
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN,
                    translation_key="secret_rejected",
                    translation_placeholders={"device_name": self.config_entry.title},
                ) from err
            except BLE_ERRORS as err:
                self.total_failures += 1
                self._async_note_failure()
                # A bare TimeoutError has no message.
                raise self._update_failed(
                    "poll_failed", error=str(err) or type(err).__name__
                ) from err
            except Exception as err:
                # A library bug must still back off like any failed poll instead
                # of hammering the device at the base cadence.
                _LOGGER.exception("Unexpected error polling %s", self._address)
                self.total_failures += 1
                self._async_note_failure()
                raise self._update_failed("poll_failed", error=repr(err)) from err
            finally:
                # Even a failed poll may have read power before it failed.
                self._io_at = bluetooth.MONOTONIC_TIME()
                self._note_zone_power(zones_before)
        if not data.optimistic:
            self.last_connected = dt_util.utcnow()
        self.consecutive_failures = 0
        self._skipped_absent = False
        self._command_reset_used = False
        self._lost_while_unpolled = False
        self._note_poll_speed(bluetooth.MONOTONIC_TIME() - started)
        self._async_note_success(data)
        return data

    def _note_zone_power(self, before: dict[int, bool]) -> None:
        """Stamp the zones whose power the library applied since ``before`` as confirmed."""
        zone_power = self._device.state.zone_power
        if zone_power is before:
            return
        now = bluetooth.MONOTONIC_TIME()
        for index in zone_power:
            self._zones_confirmed_at[index] = now

    def zone_power_age(self, index: int) -> float | None:
        """Return the seconds since zone ``index``'s power was confirmed (None: never)."""
        confirmed = self._zones_confirmed_at.get(index)
        return None if confirmed is None else bluetooth.MONOTONIC_TIME() - confirmed

    def _note_poll_speed(self, seconds: float) -> None:
        """Count a slow successful poll (or a weak signal) toward its slow backoff, or reset it."""
        self.last_poll_seconds = round(seconds, 1)
        info = self.async_last_service_info(connectable=True)
        weak = info is not None and info.rssi <= WEAK_SIGNAL_RSSI
        if seconds >= SLOW_POLL_SECONDS or weak:
            self.slow_poll_steps = min(self.slow_poll_steps + 1, SLOW_POLL_MAX_STEPS)
        else:
            self.slow_poll_steps = 0

    @callback
    def _async_note_success(self, state: DeviceState) -> None:
        """Pick the interval after a successful poll (HA re-arms its timer with it).

        A light that is on doubles its interval when the poll read the same as
        the one before, and starts over when it read something else.
        """
        polled = _polled_state(state)
        changed = polled != self._last_polled
        self._last_polled = polled
        if self._async_set_policy(self._async_policy_step()) or changed:
            self._on_backoff_steps = 0
        elif self.poll_policy is PollPolicy.ON:
            self._on_backoff_steps = min(self._on_backoff_steps + 1, _MAX_ON_BACKOFF_STEPS)
        if self.poll_policy in _UNPOLLED:
            self._async_clear_poll_interval()
        else:
            self._async_note_polls_resume()
            self._set_poll_interval(self._healthy_interval())

    @callback
    def _async_note_failure(self) -> None:
        """Count a failed (or skipped) poll, back off, and publish the new status."""
        self.consecutive_failures += 1
        self._async_set_policy(self._async_policy_step())
        if self.poll_policy in _UNPOLLED:
            # A failed one-off poll (a manual refresh) of a device the policy
            # has no background polls for doesn't start them.
            self._async_clear_poll_interval()
        else:
            self._async_note_polls_resume()
            self._apply_backoff()
        # DataUpdateCoordinator only notifies listeners when a failure *changes*
        # last_update_success; push explicitly so the diagnostic entities
        # (failure count, poll interval) and availability track an ongoing outage.
        self._async_publish_status()

    def _update_failed(self, translation_key: str, **placeholders: str) -> UpdateFailed:
        """Build the translated error for a failed (or skipped) poll."""
        return UpdateFailed(
            translation_domain=DOMAIN,
            translation_key=translation_key,
            translation_placeholders={"address": self._address, **placeholders},
        )

    @staticmethod
    def _jittered(interval: timedelta) -> timedelta:
        """Add random jitter to a poll interval.

        Devices sharing a nominal interval would otherwise drift into lockstep
        and contend for the same connection slots.
        """
        return interval + timedelta(seconds=random.uniform(*POLL_JITTER_SECONDS))  # noqa: S311

    def _set_poll_interval(self, nominal: timedelta) -> timedelta:
        """Poll every ``nominal`` plus jitter; return the jittered interval HA schedules.

        Only HA's schedule is jittered: the diagnostics show ``nominal_interval``,
        which would otherwise take a new random value (a new state) every poll.
        """
        nominal = self._floored(nominal)
        self.nominal_interval = nominal
        interval = self._jittered(nominal)
        self.update_interval = interval
        return interval

    @callback
    def _async_note_polls_resume(self) -> None:
        """Note background polls resuming, before their interval is set.

        With none scheduled, a device no connectable scanner sees was
        unavailable for its absence alone: it stays so until it is heard again
        (``_lost_while_unpolled``) instead of getting the grace of
        UNAVAILABLE_AFTER_FAILURES failed polls.
        """
        if self.nominal_interval is None and not self.async_address_present():
            self._lost_while_unpolled = True

    @callback
    def _async_clear_poll_interval(self) -> None:
        """Schedule no background polls, and cancel a timer already armed.

        HA arms no timer while the interval is None, but at the end of a poll
        it returns before cancelling one: a timer armed while the poll ran (the
        policy moving to a polled step meanwhile) would otherwise still fire.
        """
        self.nominal_interval = None
        self.update_interval = None
        self._async_unsub_refresh()

    def _apply_backoff(self) -> None:
        """Lengthen the poll interval after consecutive failures.

        See ``_failure_interval``. Jitter is added after capping so devices
        sitting at the cap still don't poll in lockstep. Reset on success.
        """
        interval = self._set_poll_interval(self._failure_interval())
        _LOGGER.debug(
            "%s: %d consecutive failures, next poll in %ds",
            self.name,
            self.consecutive_failures,
            interval.total_seconds(),
        )
