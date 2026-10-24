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
from collections.abc import Coroutine
from dataclasses import asdict
from datetime import datetime, timedelta
from enum import StrEnum
import logging
import math
import random
from typing import TYPE_CHECKING, Any, NamedTuple

from govee_ble_local import (
    Device,
    DeviceState,
    GoveeBleAuthError,
    GoveeBleTimeout,
    const as lib_const,
    profile_for,
)
from govee_ble_local.identify import parse_broadcast_onoff
from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .connect_gate import Outcome, async_connect_slot, async_note_releasing
from .const import (
    ADVERT_FLIP_POLL_DELAY_SECONDS,
    ADVERT_POWER_FRESH_SECONDS,
    COMMAND_RELEASE_DELAY_SECONDS,
    CONF_ALLOW_MODELLED,
    CONF_BROADCAST,
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
    UNAVAILABLE_AFTER_UNHEARD_SECONDS,
    WEAK_SIGNAL_RSSI,
)
from .helpers import (
    BLE_ERRORS,
    Broadcast,
    advertised_broadcast,
    async_store_broadcast,
    kelvin_range,
)

if TYPE_CHECKING:
    from . import GoveeBleLocalConfigEntry

_LOGGER = logging.getLogger(__name__)

_POLL_SEMAPHORE: HassKey[asyncio.Semaphore] = HassKey(f"{DOMAIN}_poll_semaphore")
_COMMAND_BURST: HassKey[CommandBurst] = HassKey(f"{DOMAIN}_command_burst")

_BASE_INTERVAL = timedelta(seconds=POLL_INTERVAL_SECONDS)
_MAX_INTERVAL = timedelta(seconds=MAX_POLL_INTERVAL_SECONDS)
_ON_MAX_INTERVAL = timedelta(seconds=ON_POLL_MAX_INTERVAL_SECONDS)
# Each backoff doubles per step until it reaches its cap; more doublings than
# that only risk overflowing timedelta.
_MAX_BACKOFF_STEPS = math.ceil(math.log2(_MAX_INTERVAL / _BASE_INTERVAL))
_MAX_ON_BACKOFF_STEPS = math.ceil(math.log2(_ON_MAX_INTERVAL / _BASE_INTERVAL))


class _BackgroundPoll(NamedTuple):
    """A background poll that commands hold off (``_async_background_refresh``)."""

    # Held again apart (``_async_hold_poll``'s ``first``: the first poll, a
    # return poll), should commands hold polls off once it has its place.
    apart: bool
    # When it was first held (loop time), if it was: its cap counts from then.
    held_since: float | None


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

    The connect gate (connect_gate.py) keeps each connect off a radio without
    room for it; this only keeps the integration's own background polling from
    launching a connect storm.
    """
    if (semaphore := hass.data.get(_POLL_SEMAPHORE)) is None:
        semaphore = hass.data[_POLL_SEMAPHORE] = asyncio.Semaphore(MAX_CONCURRENT_POLLS)
    return semaphore


class CommandBurst:
    """Entity commands to several devices at once: every device's background polls wait.

    A light group, an area or "turn off all the lights" sends one command per
    device, all at the same time, and each needs one of the few connection
    slots of the adapter or proxy that reaches its device (an ESPHome proxy
    has three). A background poll of any other device takes a slot for its
    connect and reads, and a command then waits behind it. So once commands
    to two or more devices run at the same time, a burst starts: every
    device's polls wait (``GoveeBleLocalCoordinator.commands_holding_off``)
    until POLL_HOLDOFF_AFTER_COMMAND_SECONDS after the last command anywhere
    ends. A command starting meanwhile, to any device, extends it: the tail
    starts again when that one ends. Outside a burst, a command to one
    device holds only that device's polls.

    As the burst ends, each loaded entry's held poll runs, unless its
    device's own hold-off still runs (which releases it as it ends). Each
    held poll still runs after POLL_HOLDOFF_MAX_SECONDS at the latest, so an
    entry that wasn't loaded as the burst ended (still setting up, or one
    that failed to unload) waits that long at most. The released polls run
    as any do, at most MAX_CONCURRENT_POLLS at a time. Any background poll
    (these, or one that came due with nothing holding it) that is still
    waiting for its place, or at the connect gate, as the next burst starts
    is held again (``GoveeBleLocalCoordinator._async_background_refresh``):
    it runs POLL_HOLDOFF_AFTER_COMMAND_SECONDS after that burst, or at the
    cap counted from when it came due. A manual refresh, and the poll the
    cap released, aren't held again.

    A turn-off waiting in the deferral (``deferral.OffDeferral``: its device
    already said it is off, so it lets the other devices go first) counts
    toward starting and ending the burst, though it isn't a command yet: one
    light on and the rest already off is still a command to several devices.
    It isn't among ``commanding_devices``, so such turn-offs don't wait for each
    other.

    Entities can also ask which devices a command runs to now
    (``commanding_devices``), or whether one runs to another device
    (``other_devices_commanding``), and be told whenever one starts or ends
    (``async_add_command_listener``). It is reached as ``command_burst(hass)``,
    or from a coordinator as ``GoveeBleLocalCoordinator.command_burst``.

    It also keeps each device's command window, which decides whether the
    device's link is released after it (``GoveeBleLocalCoordinator``'s
    ``_async_end_window``): open from a command's start until
    COMMAND_RELEASE_DELAY_SECONDS after the device's last command ends. A
    window during which another device's was open at some point is a burst,
    though the commands themselves never overlapped (a script's steps), and
    this one doesn't last the tail of POLL_HOLDOFF_AFTER_COMMAND_SECONDS.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        # Entity commands running now, per device address: a command still
        # running on an entry that is reloading counts for the same device as
        # the reloaded entry's.
        self._running: dict[str, int] = {}
        # Turn-offs waiting in the deferral, per device address.
        self._deferred: dict[str, int] = {}
        self.active = False
        # The timer that ends the burst once no command runs.
        self._unsub_end: CALLBACK_TYPE | None = None
        # Called after each command starts or ends (``async_add_command_listener``).
        self._listeners: list[CALLBACK_TYPE] = []
        # The devices whose command window is open, per address: whether
        # another device's window was open during it.
        self._windows: dict[str, bool] = {}

    @property
    def commanding_devices(self) -> set[str]:
        """Return the addresses of the devices an entity command is running to now."""
        return set(self._running)

    @callback
    def other_devices_commanding(self, address: str) -> bool:
        """Return whether an entity command to a device other than the one at ``address`` runs."""
        return any(other != address for other in self._running)

    @callback
    def async_add_command_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call ``update_callback`` as any device's command starts or ends; return its unsubscribe.

        It is called synchronously, once the counts (``other_devices_commanding``)
        and the burst are up to date: it may wake a waiter or schedule work, but
        must not start or end a command itself. One that raises is logged, and
        the others are still called.
        """
        return _async_subscribe(self._listeners, update_callback)

    @callback
    def async_note_window_open(self, address: str) -> None:
        """Open the command window of the device at ``address``, or keep it open.

        With another device's window open too, every open window is a burst.
        """
        self._windows.setdefault(address, False)
        if len(self._windows) > 1:
            self._windows = dict.fromkeys(self._windows, True)

    @callback
    def async_close_window(self, address: str) -> bool:
        """Close the device's command window; return whether it was a burst."""
        return self._windows.pop(address, False)

    @callback
    def async_note_command_started(self, address: str) -> None:
        """Count a command to the device at ``address``; a second device's starts a burst."""
        self._running[address] = self._running.get(address, 0) + 1
        self._async_cancel_end()  # the tail starts again when the last command ends
        self._async_maybe_start()
        self._async_notify()

    @callback
    def async_note_command_finished(self, address: str) -> None:
        """Count a command to the device at ``address`` ending; the last one starts the tail."""
        if (running := self._running.pop(address, 0) - 1) > 0:
            self._running[address] = running
        if self.active and not self._running and not self._deferred:
            self._async_start_tail()
        self._async_notify()

    @callback
    def async_note_deferred_started(self, address: str) -> None:
        """Count a turn-off of the device at ``address`` waiting in the deferral.

        It waits only while a command to another device runs, so a burst starts
        with it: every device's polls wait.
        """
        self._deferred[address] = self._deferred.get(address, 0) + 1
        self._async_cancel_end()
        self._async_maybe_start()

    @callback
    def async_note_deferred_finished(self, address: str) -> None:
        """Count such a turn-off no longer waiting (sent next, or dropped)."""
        if (deferred := self._deferred.pop(address, 0) - 1) > 0:
            self._deferred[address] = deferred
        if self.active and not self._running and not self._deferred:
            self._async_start_tail()

    @callback
    def _async_maybe_start(self) -> None:
        """Start a burst once commands or waiting turn-offs involve two or more devices."""
        devices = self._running.keys() | self._deferred.keys()
        if not self.active and len(devices) > 1:
            _LOGGER.debug("Commands to %d devices at once: every poll waits", len(devices))
            self.active = True

    @callback
    def _async_notify(self) -> None:
        """Call every command listener, logging one that raises."""
        for update_callback in list(self._listeners):
            try:
                update_callback()
            except Exception:
                _LOGGER.exception("Unexpected error in command listener %s", update_callback)

    @callback
    def _async_start_tail(self) -> None:
        """Start the tail that ends the burst, as the last command anywhere ends."""
        self._async_cancel_end()  # one tail, even after an end it never saw start
        if not self._hass.config_entries.async_loaded_entries(DOMAIN):
            # The last entry unloaded while this command ran: no poll is left to
            # hold, and a timer would outlive the integration.
            self.active = False
            return
        self._unsub_end = async_call_later(
            self._hass, POLL_HOLDOFF_AFTER_COMMAND_SECONDS, self._async_end_burst
        )

    @callback
    def async_note_unload(self) -> None:
        """End the burst once no entry is loaded: there is nothing left to hold.

        Unless commands still run (HA doesn't cancel them on unload): an entry
        set up again before they end (a reload) holds its polls until after
        them, and the last one to end ends the burst if none is.
        """
        if not self._hass.config_entries.async_loaded_entries(DOMAIN):
            self._async_cancel_end()
            if not self._running and not self._deferred:
                self.active = False

    @callback
    def _async_end_burst(self, _now: datetime) -> None:
        """End the burst, and run the polls it held back."""
        self._unsub_end = None
        self.active = False
        _LOGGER.debug("Commands to several devices done: held polls run")
        for entry in self._hass.config_entries.async_loaded_entries(DOMAIN):
            coordinator: GoveeBleLocalCoordinator = entry.runtime_data.coordinator
            coordinator._async_release_held_poll()

    @callback
    def _async_cancel_end(self) -> None:
        """Drop the timer that would end the burst, if any."""
        if self._unsub_end is not None:
            self._unsub_end()
            self._unsub_end = None


def command_burst(hass: HomeAssistant) -> CommandBurst:
    """Return the domain-wide tracker of commands to several devices at once."""
    if (burst := hass.data.get(_COMMAND_BURST)) is None:
        burst = hass.data[_COMMAND_BURST] = CommandBurst(hass)
    return burst


def _polled_state(state: DeviceState) -> dict[str, Any]:
    """Return a copy of what a poll read, to tell whether the next one found anything new."""
    polled = asdict(state)
    # Bookkeeping, not device state: a read that only confirms the zones again
    # stamps them anew (zone_power_confirmed), which is nothing new.
    del polled["optimistic"], polled["zone_power_confirmed"]
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
        *,
        set_up_without_pact: bool = False,
    ) -> None:
        """Initialize the coordinator for one config entry's device.

        ``set_up_without_pact``: setup knew no pact for it (its record had no Govee block and
        none was stored), so the first one heard may route its model elsewhere.
        """
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
        self._set_up_without_pact = set_up_without_pact
        self.consecutive_failures = 0
        # The failures of the current streak that drive its backoff: all but the
        # slow ones (a timeout after SLOW_POLL_SECONDS, ``_async_note_failure``).
        self._backoff_failures = 0
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
        # Polls that read the device without failing, whether or not it answered each read in
        # them: the segment count is taken from two in a row (``helpers.segment_count_read``).
        # Not a failed one: the library keeps the last count through it.
        self.device_reads = 0
        # last_connected on the Bluetooth monotonic clock (``power_confirmed_off``).
        self._read_back_at: float | None = None
        self._diagnostic_listeners: list[CALLBACK_TYPE] = []
        self._state_listeners: list[CALLBACK_TYPE] = []
        # Receive time (monotonic) of the advertisement last_seen/RSSI came from.
        self._presence_time: float | None = None
        # When the device was last known reachable (monotonic): the last
        # advertisement a connectable scanner heard before HA dropped it from its
        # connectable history (``async_handle_unavailable``), or HA reaching it
        # since (a command that landed, or a poll that didn't fail once the device
        # has been read back at least once: it doesn't advertise while HA holds
        # the link), and no earlier than setup.
        # Availability once it is gone counts from it (``device_available``).
        self._connectable_heard_at = bluetooth.MONOTONIC_TIME()
        # When an entity command last landed (monotonic): its link may still be
        # open (``_async_link_held``). When the last one that says something
        # about the device's power landed (all but a zone's turn-off, which
        # can't switch a fixture on), and whether it was a whole-device
        # turn-off, whose ack says the device is off (``power_confirmed_off``).
        self._command_landed_at: float | None = None
        self._power_landed_at: float | None = None
        self._landed_off = False
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
        # excluded model.
        self._advert_policy = device.sku not in EXCLUDED_FROM_ADVERT_POLICY
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
        # wait for both, and for a burst of commands to several devices
        # (``commands_holding_off``, ``CommandBurst``). Whether a scheduled poll, and
        # the first poll after setup (or a return poll), are waiting for the hold-off
        # (kept apart: a re-arm or a policy step that stops polls drops a held
        # scheduled poll, never the first one), and the timer that runs the held poll
        # anyway once it has waited POLL_HOLDOFF_MAX_SECONDS. Set once the entry
        # is unloading.
        self._commands_running = 0
        self._burst = command_burst(hass)
        self._unsub_holdoff_end: CALLBACK_TYPE | None = None
        self._poll_waiting = False
        self._first_poll_waiting = False
        self._unsub_holdoff_cap: CALLBACK_TYPE | None = None
        # When the held poll was first held (loop time): the cap counts from it.
        self._holdoff_since: float | None = None
        self._shutting_down = False
        # The device's command window (``_async_end_window``): whether a command
        # in it came from a person, and the timer that closes it.
        self._window_person = False
        self._unsub_window_end: CALLBACK_TYPE | None = None
        # A poll is reading the device (``_async_poll``): a window closing then
        # releases the link only once the poll ends (``_release_after_poll``),
        # so the poll's remaining reads don't have to connect again.
        self._poll_reading = False
        self._release_after_poll = False
        # A poll is running (``_async_update_data``): it uses up the polls held
        # meanwhile, so the hold-off ending doesn't release one alongside it.
        self._poll_running = False
        # The background polls that commands hold off, by the task that runs
        # each one's refresh (``_async_background_refresh``): taken as its read
        # starts (``_async_update_data``). A manual refresh, or the poll the
        # hold-off cap released, has none.
        self._background_polls: dict[asyncio.Task[Any], _BackgroundPoll] = {}
        # The poll that just ended found commands holding polls off once it
        # had its place, so read nothing: held again as its refresh ends
        # (``_async_refresh_finished``), apart or not, with the cap counting
        # from when it was first held (loop time).
        self._poll_held_again: tuple[bool, float] | None = None
        # Home Assistant fired its stop event: its close stage refuses nothing,
        # so no held poll is released any more (``_async_release_poll``).
        self._ha_stopping = False
        # Zone turn-offs of this device waiting in the deferral: any call to the
        # device ends them (``GoveeBleLocalEntity.async_request_call``).
        self._zone_off_hurries: set[CALLBACK_TYPE] = set()
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
    def commands_running(self) -> bool:
        """Return whether an entity command to the device is running now.

        Unlike ``commands_holding_off``, not the hold-off after one ended.
        """
        return bool(self._commands_running)

    @property
    def device_available(self) -> bool:
        """Entity availability: reachable over BLE, or still polling fine.

        A device that is advertising stays available through transient poll
        failures (commands can still reach it); one that has vanished from every
        connectable scanner goes unavailable once polls have also failed (or
        been skipped) a few times, or it hasn't been heard or reached for
        UNAVAILABLE_AFTER_UNHEARD_SECONDS (a slow device's polls are far apart;
        the presence tick re-checks it) - or at once when no background polls
        are scheduled to count those failures (see ``polls_scheduled``), and it
        stays so when they resume before it is heard again.
        """
        if self.async_address_present():
            return True
        if not self.polls_scheduled or self._lost_while_unpolled:
            return False
        unheard = bluetooth.MONOTONIC_TIME() - self._connectable_heard_at
        return (
            self.consecutive_failures < UNAVAILABLE_AFTER_FAILURES
            and unheard < UNAVAILABLE_AFTER_UNHEARD_SECONDS
        )

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
            self._async_request_poll("return_poll", keep=True)
            return_poll = True

        # Heard again after no scanner did: show it now, not at the next refresh.
        heard_again = False
        if self.rssi is None:
            heard_again = self._take_presence(service_info)
        was_on = self._device.state.is_on
        learned_before = self._advert_learned()
        changed = self._device.ingest_advertisement(service_info.advertisement)
        self._async_keep_broadcast(service_info)
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

    @callback
    def _async_keep_broadcast(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        """Keep the entry's broadcast header current, for a later setup without a record.

        The header of the record setup routes by (the connectable one) replaces a stored one
        whose pact it no longer shows (a firmware update's, say); with none stored, the first
        header any scanner hears is stored. Nothing else is written: this callback hears every
        scanner, whose records can lead with different headers, so taking each would flip the
        stored one as another scanner's advert arrives. A device set up without a pact (none
        stored, none in the record at setup) has its model's pact-less profile. When the first
        pact it advertises routes the model to another profile, the entry reloads, so setup
        builds the device at that pact. Only then: a device set up at a pact keeps its profile
        through a later one, as the library does.
        """
        stored = Broadcast.from_data(self.config_entry.data.get(CONF_BROADCAST))
        routed_by = advertised_broadcast(self.async_last_service_info(connectable=True))
        if stored is None:
            store = routed_by or advertised_broadcast(service_info)
        elif routed_by is not None and routed_by.pact != stored.pact:
            store = routed_by
        else:
            store = None
        if store is not None:
            async_store_broadcast(self.hass, self.config_entry, store)
        if not self._set_up_without_pact or self._device.pact is None:
            return
        self._set_up_without_pact = False  # the first pact heard decides
        routed = profile_for(
            self._device.sku,
            pact=self._device.pact,
            allow_modelled=bool(self.config_entry.data.get(CONF_ALLOW_MODELLED, False)),
        )
        if routed is not None and routed != self._device.profile:
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

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
        (like HA's own timer). It runs as a scheduled poll would, and waits
        while commands hold polls off (``_async_request_poll``).
        """
        self._unsub_flip_poll = None
        if not (self.hass.is_running and self._polls_enabled and self._listeners):
            return
        self._async_request_poll("turn_on_poll")

    @callback
    def _async_request_poll(self, name: str, *, keep: bool = False) -> None:
        """Run a one-off background poll (the turn-on or return poll) as if HA's timer fired.

        Like a scheduled poll (``_handle_refresh_interval``), it waits while
        commands hold polls off (during a burst of commands to several devices
        it would take a connection slot they need, ``CommandBurst``), a poll
        running now uses it up, and otherwise it runs at once in place of HA's
        next scheduled one. Not through HA's request debouncer: a request that
        waits out its cooldown, or a running poll, runs later unchecked. Held,
        it joins any poll already held; a re-arm or a policy step that stops
        polls replaces it, unless ``keep`` holds it apart like the first poll
        after setup, to run once whatever the schedule (the return poll: the
        advertisement that brought the device back may stop its polls next).
        """
        if self.commands_holding_off or self._poll_running:
            self._async_hold_poll(first=keep)
            return
        self._async_release_poll(name, apart=keep)

    @callback
    def _async_cancel_flip_poll(self) -> None:
        """Drop a pending poll after an advertised turn-on, if any."""
        if self._unsub_flip_poll is not None:
            self._unsub_flip_poll()
            self._unsub_flip_poll = None

    async def async_shutdown(self) -> None:
        """Also drop a pending turn-on poll, hold-off and link release (HA calls this on unload).

        The last entry to unload ends a burst too, once no command runs
        (``CommandBurst``). The device's command window closes: ``device.stop()``
        closes the link next.
        """
        self._shutting_down = True
        self._release_after_poll = False  # device.stop() closes the link next
        self._async_cancel_flip_poll()
        self._async_cancel_holdoff_end()
        self._async_cancel_window_end()
        self._burst.async_close_window(self._address)
        self._async_drop_held_polls()
        self._burst.async_note_unload()
        await super().async_shutdown()

    @callback
    def async_refresh_presence(self, _now: datetime | None = None) -> None:
        """Publish last seen / RSSI from the newest advertisement any scanner heard.

        Runs every LAST_SEEN_RESOLUTION_SECONDS (see __init__): HA's history
        records every advertisement, while the advertisement callback fires once
        for a device that keeps repeating the same one. It also publishes
        availability that lapsed with time since the last push
        (``device_available``). It re-offers that
        advertisement to the library (``_async_reoffer_advertisement``) and
        re-checks the poll policy too, so a device whose on/off byte goes stale
        is back on the fixed cadence within one tick (its first poll follows one
        POLL_INTERVAL_SECONDS, plus jitter, later).
        """
        info = self.async_last_service_info(connectable=False)
        took = info is not None and self._take_presence(info)
        if info is not None and self._async_reoffer_advertisement(info):
            return  # published to every entity
        # Availability can lapse with time alone (``device_available``).
        if self._async_apply_policy() or self.device_available != self._published_available:
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
            and info.time > self._io_at + lib_const.ADVERT_SUPPRESS_SECONDS
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
    def async_handle_unavailable(
        self, *, connectable: bool, last: bluetooth.BluetoothServiceInfoBleak | None = None
    ) -> None:
        """React to the device dropping out of the (connectable) scanner history.

        ``last`` is the advertisement HA last had for it there: when it was last
        heard, which availability counts from (``device_available``).
        """
        if connectable and last is not None:
            self._connectable_heard_at = max(self._connectable_heard_at, last.time)
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
    def command_burst(self) -> CommandBurst:
        """The domain-wide tracker of entity commands to every device (``CommandBurst``)."""
        return self._burst

    @property
    def commands_holding_off(self) -> bool:
        """Whether background polls wait: a command is running, or one ended just now.

        "Just now" is POLL_HOLDOFF_AFTER_COMMAND_SECONDS after the last one
        ended: the last one to this device, or during a burst of commands to
        several devices at once, the last one to any device (``CommandBurst``).
        """
        return (
            bool(self._commands_running)
            or self._unsub_holdoff_end is not None
            or self._burst.active
        )

    @callback
    def async_note_command_started(self, *, person: bool = False) -> None:
        """Note an entity command starting: background polls wait until it is done.

        It opens the device's command window, or keeps it open
        (``_async_end_window``); ``person``: the command came from a person
        (``helpers.async_context_is_person``).
        """
        self._commands_running += 1
        self._burst.async_note_command_started(self._address)
        self._async_cancel_holdoff_end()  # the hold-off restarts when the last one ends
        self._async_cancel_window_end()  # and so does the window's settle
        self._window_person = self._window_person or person
        # A release a poll put off belongs to the window before: this one decides
        # again when it closes (a person's command alone keeps the link).
        if self._release_after_poll:
            _LOGGER.debug(
                "%s: not releasing the link after the poll: a new command's window decides",
                self.name,
            )
            self._release_after_poll = False
        if not self._shutting_down:
            # Once unloading, no settle would close it.
            self._burst.async_note_window_open(self._address)

    @callback
    def async_note_command_finished(self) -> None:
        """Note an entity command ending (landed or not): polls wait a little longer still.

        The last one running starts the settle that closes the command window.
        """
        self._commands_running = max(0, self._commands_running - 1)
        # Even once the entry is unloading: a burst ends only once every command has.
        self._burst.async_note_command_finished(self._address)
        if self._commands_running or self._shutting_down:
            # Another command still runs (the hold-off starts when it ends), or the
            # entry is unloading: a timer armed now would outlive it.
            return
        self._async_cancel_holdoff_end()
        self._unsub_holdoff_end = async_call_later(
            self.hass, POLL_HOLDOFF_AFTER_COMMAND_SECONDS, self._async_end_holdoff
        )
        self._async_cancel_window_end()  # one settle, even after an end it never saw start
        self._unsub_window_end = async_call_later(
            self.hass, COMMAND_RELEASE_DELAY_SECONDS, self._async_end_window
        )

    @callback
    def _async_end_window(self, _now: datetime) -> None:
        """Close the command window: release the link unless a person alone used it.

        The library keeps a command's link up for its warm window (30 s), for a
        person adjusting again: a second tap, a slider drag. A window in which
        no command came from a person (a voice satellite, an automation; see
        ``helpers.async_context_is_person``), or another device's window was
        open (a burst, ``CommandBurst``), expects no such follow-up, so its
        link is released now (``Device.release``), and the connection slot it
        holds with it. A command after that connects again. The release waits
        for any operation queued before it, and isn't cancelled by a command
        starting meanwhile: that one queues behind it and reconnects, unless it
        reaches the link first, when the library keeps the link
        (``Device.release`` returns False and drops nothing). While a
        poll is reading the device, the release is put off until the poll ends
        (``_async_update_data``): released between its reads, the rest of the
        poll would connect again. A command starting before then drops the
        put-off release (``async_note_command_started``): its own window
        decides when it closes.
        """
        self._unsub_window_end = None
        burst = self._burst.async_close_window(self._address)
        person, self._window_person = self._window_person, False
        if person and not burst:
            return
        if self._poll_reading:
            _LOGGER.debug(
                "%s: releasing the link once the running poll ends (%s)",
                self.name,
                "commands to other devices too" if burst else "no person's command",
            )
            self._release_after_poll = True
            return
        _LOGGER.debug(
            "%s: releasing the link (%s)",
            self.name,
            "commands to other devices too" if burst else "no person's command",
        )
        self._async_start_release()

    @callback
    def _async_start_release(self) -> None:
        """Start releasing the device's link (``_async_release_link``).

        The connect gate counts the link as down from now until the release
        ends: a radio keeps listing it while it's torn down.
        """
        end = async_note_releasing(self.hass, self._address)
        self.config_entry.async_create_background_task(
            self.hass, self._async_release_link(end), name=f"{self.name}_release", eager_start=True
        )

    async def _async_release_link(self, end: CALLBACK_TYPE) -> None:
        """Release the device's link; the library logs it (a guard only: it never raises).

        Once it dropped the link, a poll no longer counts on it
        (``_async_link_held``): a device that has stopped advertising is then
        skipped as absent, as after an idle disconnect, instead of spending its
        connect budget on it. A link the library kept (a newer write went out
        on it) still counts.
        """
        try:
            try:
                dropped = await self._device.release()
            except BLE_ERRORS as err:
                _LOGGER.debug("%s: releasing the link failed: %s", self.name, err)
                return
            if dropped:
                self._command_landed_at = None
        finally:
            end()

    @callback
    def _async_cancel_window_end(self) -> None:
        """Drop the timer that would close the command window, if any."""
        if self._unsub_window_end is not None:
            self._unsub_window_end()
            self._unsub_window_end = None

    @property
    def _poll_held(self) -> bool:
        """Whether a background poll (scheduled, or the first) waits for the hold-off."""
        return self._poll_waiting or self._first_poll_waiting

    @callback
    def _async_end_holdoff(self, _now: datetime) -> None:
        """End the hold-off, and run the background poll it held back, if any."""
        self._unsub_holdoff_end = None
        self._async_release_held_poll()

    @callback
    def _async_release_held_poll(self) -> None:
        """Run the held background poll, if any, once no command holds polls off any more.

        Called as this device's hold-off ends and as a burst of commands to
        several devices ends (``CommandBurst``): whichever ends last releases
        it. Not while a poll runs: that one uses up the held polls.
        """
        if self._poll_held and not self._poll_running and not self.commands_holding_off:
            self._async_release_poll(apart=self._first_poll_waiting)

    @callback
    def _async_cancel_holdoff_end(self) -> None:
        """Drop the timer that would end the hold-off, if any."""
        if self._unsub_holdoff_end is not None:
            self._unsub_holdoff_end()
            self._unsub_holdoff_end = None

    @callback
    def _async_hold_poll(self, *, first: bool = False, since: float | None = None) -> None:
        """Keep a background poll that came due until the hold-off ends, or for at most the cap.

        ``first``: held apart, so a re-arm or a policy step that stops polls
        doesn't drop it: the first poll after setup, or a return poll
        (``_async_request_poll``). The first poll held arms the cap: later ones
        join it, and one poll runs when the hold-off is released, however many
        came due. The cap counts from ``since`` (loop time), when a poll held
        again (``_async_refresh_finished``) was first held, else from now: held
        again, a poll still runs POLL_HOLDOFF_MAX_SECONDS after it came due.
        """
        if first:
            self._first_poll_waiting = True
        else:
            self._poll_waiting = True
        if self._unsub_holdoff_cap is None:
            now = self.hass.loop.time()
            self._holdoff_since = now if since is None else min(since, now)
            self._unsub_holdoff_cap = async_call_later(
                self.hass,
                self._holdoff_since + POLL_HOLDOFF_MAX_SECONDS - now,
                self._async_holdoff_cap_reached,
            )

    @callback
    def _async_holdoff_cap_reached(self, _now: datetime) -> None:
        """Run the held poll anyway: a stream of commands mustn't postpone it for ever.

        It can then run while one long command is still under way (a zone
        light's several calls on a failing link): it waits for the device lock
        like any other operation. Nor is it held again once it has its place.
        """
        self._unsub_holdoff_cap = None
        self._holdoff_since = None
        if self._poll_held and not self._poll_running:
            self._async_release_poll(apart=None)

    @callback
    def _async_release_poll(self, name: str = "held_off_poll", *, apart: bool | None) -> None:
        """Run the held background poll in place of HA's armed timer, as that timer would have.

        HA's _handle_refresh_interval forgets its timer without cancelling it (it
        expects to be called by that timer firing). A held first poll ran with
        HA's first timer still armed, so it is cancelled here: two timers would
        otherwise poll for good. The poll then re-arms one timer as it ends.
        ``name`` names its task (a one-off poll that runs at once, too).
        ``apart``: how it is held again should commands hold polls off once it
        has its place (``_async_background_refresh``), with its cap still
        counting from when it was first held; None for the poll the cap
        released, which runs whatever holds polls off.

        Not once HA is stopping (``async_note_ha_stopping``): in its close stage
        HA no longer refuses a scheduled refresh, and the stop listener has
        closed the link (a burst's tail, the hold-off end and the cap can all
        fire then). Bootstrap isn't refused, though HA isn't "running" yet then.
        """
        since = self._holdoff_since
        self._async_drop_held_polls()
        if self._ha_stopping or self.hass.is_stopping:
            return
        super()._async_unsub_refresh()
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_background_refresh(
                super()._handle_refresh_interval(),
                None if apart is None else _BackgroundPoll(apart, since),
            ),
            name=f"{self.name}_{name}",
            eager_start=True,
        )

    async def _async_background_refresh(
        self, refresh: Coroutine[Any, Any, None], background: _BackgroundPoll | None
    ) -> None:
        """Run ``refresh`` as a background poll that commands hold off (``background``).

        Such a poll is held while commands hold polls off as it comes due, but
        it can then wait a while for its place (MAX_CONCURRENT_POLLS, and the
        connect gate), while commands start: a new burst's, or one to its own
        device. So it looks again once it has its place (``_async_poll``): held
        again, apart or not (``_BackgroundPoll``), it reads nothing now, and
        runs once those commands are done, or at the cap. ``background`` None:
        it runs whatever holds polls off (the poll the hold-off cap released).
        A manual refresh doesn't wait for commands either.

        ``background`` is kept for the task that runs ``refresh``, which reads
        it as its read starts (``_async_update_data``): another task's refresh
        that takes HA's refresh lock first (a manual refresh the debouncer runs,
        say) doesn't take it.
        """
        if background is None or (task := asyncio.current_task()) is None:
            await refresh
            return
        self._background_polls[task] = background
        try:
            await refresh
        finally:
            # Taken by now, unless the refresh read nothing at all (HA stopping).
            self._background_polls.pop(task, None)

    @callback
    def async_note_ha_stopping(self) -> None:
        """Note Home Assistant's stop event: no held poll is released from now on."""
        self._ha_stopping = True

    @callback
    def async_track_zone_off_wait(self, hurry: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Track a zone turn-off waiting in the deferral; return the callback that stops it."""
        self._zone_off_hurries.add(hurry)

        @callback
        def _async_untrack() -> None:
            self._zone_off_hurries.discard(hurry)

        return _async_untrack

    @callback
    def async_hurry_zone_offs(self) -> None:
        """Send this device's waiting zone turn-offs now: a call to the device is queued."""
        for hurry in list(self._zone_off_hurries):
            hurry()

    @callback
    def _async_drop_held_polls(self) -> None:
        """Forget every held background poll and the cap timer."""
        self._poll_waiting = False
        self._first_poll_waiting = False
        self._async_cancel_holdoff_cap()

    @callback
    def _async_cancel_holdoff_cap(self) -> None:
        """Drop the cap timer, if any."""
        if self._unsub_holdoff_cap is not None:
            self._unsub_holdoff_cap()
            self._unsub_holdoff_cap = None
        self._holdoff_since = None

    async def async_background_poll(self) -> None:
        """Run the first background poll after setup now, or hold it while commands do.

        HA's scheduled polls come through ``_handle_refresh_interval``. Both hold
        the same way, so one hold-off releases one poll; but the held first poll
        always runs once, even if polls stop meanwhile (a command turned the
        light off): it is what reads the device's info. Any poll that runs
        first (a manual refresh), or is running now, is that poll
        (``_async_update_data``).
        """
        if self._poll_running:
            return
        if self.commands_holding_off:
            self._async_hold_poll(first=True)
            return
        await self._async_background_refresh(self.async_refresh(), _BackgroundPoll(True, None))

    async def _handle_refresh_interval(self, _now: datetime | None = None) -> None:
        """Run a scheduled poll, unless an entity command is running or just ran.

        A poll then would take the device for its reads while the user is likely
        to follow up (a second tap, a slider drag), and HA already shows what the
        command did; during a burst of commands to several devices, any device's
        poll would take a connection slot they need (``CommandBurst``). It is
        held until the hold-off and any burst end (``_async_release_held_poll``),
        or for at most POLL_HOLDOFF_MAX_SECONDS (``_async_holdoff_cap_reached``);
        and held again if they start while it waits for its place
        (``_async_background_refresh``), the cap still counting from when it
        came due. Nor while a poll already runs: that
        poll re-arms the timer as it ends. Overrides DataUpdateCoordinator's
        scheduled-refresh entry point, which takes the debouncer's lock.
        """
        if self._poll_running:
            # A refresh started meanwhile reads the device now, and HA re-arms
            # the timer as it ends: running this one too would queue a second
            # poll behind it.
            self._unsub_refresh = None  # the timer that just fired
            return
        if self.commands_holding_off:
            self._unsub_refresh = None  # the timer that just fired
            self._async_hold_poll()
            return
        await self._async_background_refresh(
            super()._handle_refresh_interval(_now), _BackgroundPoll(False, None)
        )

    def _async_unsub_refresh(self) -> None:
        """Cancel the scheduled poll, a held scheduled one included.

        HA calls this before arming a new timer (``_schedule_refresh``), and so
        does a policy step that stops polls: the new timer, or no polls, replace
        a held scheduled poll, so two never run. A held first poll stays (it
        runs once whatever the schedule, unless another poll runs first);
        shutdown drops it.
        """
        super()._async_unsub_refresh()
        self._poll_waiting = False
        if not self._first_poll_waiting:
            self._async_cancel_holdoff_cap()

    @callback
    def async_handle_command_success(
        self, *, power_off: bool = False, keeps_power: bool = False
    ) -> None:
        """Drop the poll backoff: an entity command just reached the device.

        ``power_off``: it was a whole-device turn-off (``power_confirmed_off``).
        ``keeps_power``: it says nothing about the device's power (a zone's
        turn-off: it can't switch the fixture on), so the last power word stands.

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
        background polls). Nor once the entry is unloading: a command that lands
        after the coordinator shut down must not arm a timer on it.
        """
        if self._shutting_down:
            return
        # It answered: reachable, though it doesn't advertise while HA holds the link.
        now = bluetooth.MONOTONIC_TIME()
        self._connectable_heard_at = self._command_landed_at = now
        if not keeps_power:
            self._power_landed_at = now
            self._landed_off = power_off
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

    @property
    def power_confirmed_off(self) -> bool:
        """Return whether the device itself said, recently, that it is off.

        A turn-off of a device already off lets the other devices' commands of
        a burst go first (``deferral``), so only what the device said counts,
        never what HA assumed. Its newest word decides, and must be younger than
        ADVERT_POWER_FRESH_SECONDS: its advertised on/off byte (decoded as the
        library decodes it, with the profile's mask); a poll, once the library's
        state isn't optimistic (the on/off it left); or a command that
        landed: a whole-device turn-off's ack says off, any other command (a
        segment write can wake a fixture the library still holds off) leaves the
        power unknown, except a zone's turn-off, which says nothing about it. On
        a tie, HA's own I/O is the newer: the library ignores an advertisement
        for a while after it. The library's on/off must say off as well, so an
        unknown on/off never counts.

        A poll counts once a poll's state read has answered since setup:
        govee-ble-local 1.6.0 clears ``optimistic`` on the first answered state
        read and never sets it again, and doesn't say whether a later poll's
        reads answered, so a poll whose reads all went unanswered (the library
        returns the state it had) counts too. Before the first answered state
        read, no poll counts.

        Not words from the device, so they don't count: a command that failed
        (one that may have switched the device on leaves the off standing: the
        library applies nothing), and an old byte HA keeps in a newer merged
        advertisement (``_async_advert_power_fresh``). A wrong word only delays
        that device's turn-off (DEFER_OFF_MAX_SECONDS at most), never drops it.
        """
        state = self._device.state
        if state.is_on is not False:
            return False
        # (when, rank on a tie, whether it says off)
        words: list[tuple[float, int, bool]] = []
        info = self.async_last_service_info(connectable=False)
        if (
            info is not None
            and (
                on := parse_broadcast_onoff(
                    info.manufacturer_data, on_mask=self._device.profile.advert_on_mask
                )
            )
            is not None
        ):
            words.append((info.time, 0, not on))
        if self._read_back_at is not None and not state.optimistic:
            words.append((self._read_back_at, 1, True))
        if self._power_landed_at is not None:
            words.append((self._power_landed_at, 2, self._landed_off))
        if not words:
            return False
        at, _rank, off = max(words)
        return off and bluetooth.MONOTONIC_TIME() - at < ADVERT_POWER_FRESH_SECONDS

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
        then it doubles up to the cap. Slow failures don't count: they add slow
        steps instead (``_async_note_failure``), which floor every interval.
        """
        steps = min(max(0, self._backoff_failures - 1), _MAX_BACKOFF_STEPS)
        return min(_BASE_INTERVAL * (1 << steps), _MAX_INTERVAL)

    # -- polling ------------------------------------------------------------

    async def _async_update_data(self) -> DeviceState:
        # The poll that runs (a manual refresh, say) uses up every background
        # poll held for the hold-off, the first one after setup included: it
        # reads the device as they would. Held ones wait while it runs (the
        # hold-off end and the cap don't release them, ``_poll_running``), and
        # are dropped as it ends, however it ends. Unless it read nothing, held
        # again for commands (``_async_poll``): it is held once more, as the
        # refresh ends, joined by one held meanwhile (a return poll), apart if
        # either is, and its cap counting from the earlier hold.
        held: tuple[bool, float] | None = None
        if (task := asyncio.current_task()) is not None and (
            background := self._background_polls.pop(task, None)
        ) is not None:
            # One that came due unheld would have its cap count from now.
            since = background.held_since
            held = (background.apart, self.hass.loop.time() if since is None else since)
        self._poll_running = True
        try:
            return await self._async_poll(held)
        finally:
            self._poll_running = False
            if (again := self._poll_held_again) is not None:
                apart, since = again
                if self._holdoff_since is not None:
                    since = min(since, self._holdoff_since)
                self._poll_held_again = (apart or self._first_poll_waiting, since)
            self._async_drop_held_polls()
            self._async_release_after_poll()

    @callback
    def _async_refresh_finished(self) -> None:
        """Hold the background poll again that commands held off once it had its place.

        Only now: HA re-arms its poll timer as the refresh ends, which drops a
        held scheduled poll (``_async_unsub_refresh``). Releasing it cancels that
        timer again, as for any held poll. Its cap counts from when it was first
        held, so it still runs POLL_HOLDOFF_MAX_SECONDS after it came due.
        """
        super()._async_refresh_finished()
        if (again := self._poll_held_again) is not None:
            self._poll_held_again = None
            apart, since = again
            self._async_hold_poll(first=apart, since=since)

    @callback
    def _async_release_after_poll(self) -> None:
        """Release the link a command window closed on while this poll read the device.

        Not once a command runs again or its window is open (that window
        decides when it closes), nor once the entry is unloading.
        """
        if not self._release_after_poll:
            return
        self._release_after_poll = False
        if self._commands_running or self._unsub_window_end is not None or self._shutting_down:
            return
        _LOGGER.debug("%s: the poll ended: releasing the link", self.name)
        self._async_start_release()

    async def _async_poll(self, held: tuple[bool, float] | None) -> DeviceState:
        """Read the device (``_async_update_data``), counting the outcome for the backoff.

        ``held``: for a background poll that commands hold off, how it is held
        again if they hold polls off once it has its place: apart or not, and
        when it was first held (``_async_background_refresh``). None for any
        other poll.
        """
        self._poll_held_again = None
        # Any poll that runs (a manual refresh after an auth failure included)
        # means polling is scheduled again.
        self._auth_failed = False
        if self._async_unreachable():
            raise self._async_skip_absent()

        async with (
            _poll_semaphore(self.hass),
            # Behind every waiting command: see connect_gate.py.
            async_connect_slot(self.hass, self._address, poll=True) as admission,
        ):
            if admission.outcome is Outcome.RELEASED:
                # Its entry is unloading (or HA stopping) and the link closes next:
                # no new connect. HA cancels a background poll right after anyway.
                nothing = "its entry is unloading"
            elif held is not None and self.commands_holding_off:
                # Again once it has its place: commands may hold polls off by now
                # (they started while it waited for its place or at the gate).
                # Held again, it reads nothing now.
                _LOGGER.debug("%s: commands hold polls off again: the poll waits", self.name)
                self._poll_held_again = held
                nothing = "commands hold polls off"
            else:
                return await self._async_read()
        # After the gate's block: it counts a call that raises as a failed connect.
        return self._async_read_nothing(nothing)

    async def _async_read(self) -> DeviceState:
        """Read the device, once the poll has its place and the gate admitted it."""
        # Again once it has a slot: the wait may have outlasted a held link.
        if self._async_unreachable():
            raise self._async_skip_absent()
        present = self.async_address_present()
        started = bluetooth.MONOTONIC_TIME()
        try:
            # A tighter connect budget than a command's: a device that doesn't
            # answer frees the device (and a command waiting for it) sooner.
            self._poll_reading = True
            try:
                data = await self._device.update(
                    connect_attempts=POLL_CONNECT_ATTEMPTS,
                    connect_timeout=POLL_CONNECT_TIMEOUT_SECONDS,
                )
            finally:
                self._poll_reading = False
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
            elapsed = bluetooth.MONOTONIC_TIME() - started
            if not present:
                # It ran only for a link a command left open, which was gone by
                # then: absent, as a skip counts it (no slow step; polled as
                # soon as it is heard again).
                self._skipped_absent = True
                self._async_note_failure()
            elif isinstance(err, GoveeBleTimeout) and elapsed >= SLOW_POLL_SECONDS:
                # Typically the connect ran out of POLL_CONNECT_TIMEOUT_SECONDS.
                self._async_note_failure(slow_seconds=elapsed)
            else:
                self._async_note_failure()
            # A bare TimeoutError has no message.
            raise self._update_failed("poll_failed", error=str(err) or type(err).__name__) from err
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
        self.device_reads += 1
        if not data.optimistic:
            self.last_connected = dt_util.utcnow()
            self._connectable_heard_at = self._read_back_at = bluetooth.MONOTONIC_TIME()
        self.consecutive_failures = 0
        self._backoff_failures = 0
        self._skipped_absent = False
        self._command_reset_used = False
        self._lost_while_unpolled = False
        self._note_poll_speed(bluetooth.MONOTONIC_TIME() - started)
        self._async_note_success(data)
        return data

    @callback
    def _async_read_nothing(self, why: str) -> DeviceState:
        """Return what a poll that read nothing (``why``) gives HA: the state, unless failed.

        After a failed poll, returning would read as a recovery ("data
        recovered") with nothing read: it stays failed, which HA doesn't log
        again. Nothing counts toward the backoff.
        """
        if not self.last_update_success:
            raise self._update_failed("poll_failed", error=why)
        return self._device.state

    @callback
    def _async_unreachable(self) -> bool:
        """Return whether nothing can reach the device: no connectable scanner, no held link."""
        return not self.async_address_present() and not self._async_link_held()

    @callback
    def _async_skip_absent(self) -> UpdateFailed:
        """Count a poll skipped because nothing can reach the device; return its error.

        A connect would just burn ~1 min of bleak-retry-connector attempts (and a
        poll slot) to fail.
        """
        self._skipped_absent = True
        self._async_note_failure()
        return self._update_failed("poll_skipped_absent")

    @callback
    def _async_link_held(self) -> bool:
        """Return whether a command landed within the library's warm window: its link may be up.

        The library keeps a command's link open up to ACTIVE_IDLE_DELAY (30 s)
        after each write starts (transport/connection.py ``_run``, and
        ``_schedule_idle_timer`` waits it out): the upper bound. Unless a person
        alone used it, the integration releases it COMMAND_RELEASE_DELAY_SECONDS
        after the last command (``_async_end_window``). The device doesn't
        advertise while connected, so HA may drop it from its history, yet a
        poll still reaches it: update() reuses the open link
        (``_ensure_connected_locked`` returns while ``is_connected``), with no
        BLEDevice lookup. So such a poll runs instead of being skipped as
        absent, and its read resets the failures. Once the link is gone (the
        device ended it, the integration released it, or the window outlasts
        it by the last write's time: the stamp is taken as the command
        returns), a poll in the window connects afresh within its connect
        budget, and one that fails
        counts as the skipped poll would have been. The window is longer than
        the hold-off (POLL_HOLDOFF_AFTER_COMMAND_SECONDS), so the poll the
        hold-off releases isn't skipped as absent: it runs over the link while
        it is up, and connects afresh once the device has ended it (it
        advertises again then). A burst of commands to several devices can hold
        it past the window (``CommandBurst``); by then the link is gone and the
        device advertises again. The poll checks it again once it has a poll
        slot (the wait can outlast the window), and one that fails because the
        link was gone counts as a skip.
        """
        landed = self._command_landed_at
        return (
            landed is not None and bluetooth.MONOTONIC_TIME() - landed < lib_const.ACTIVE_IDLE_DELAY
        )

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
    def _async_note_failure(self, *, slow_seconds: float | None = None) -> None:
        """Count a failed (or skipped) poll, back off, and publish the new status.

        ``slow_seconds``: the poll timed out after SLOW_POLL_SECONDS or more
        (typically its connect ran out of POLL_CONNECT_TIMEOUT_SECONDS). That is
        a slow device as much as a failed poll: it counts toward availability
        and the diagnostics like any failure, but adds a slow step (up to
        SLOW_POLL_MAX_STEPS doublings) instead of a failure-backoff step, which
        would take a weak device to MAX_POLL_INTERVAL_SECONDS.
        """
        self.consecutive_failures += 1
        if slow_seconds is None:
            self._backoff_failures += 1
        else:
            self.last_poll_seconds = round(slow_seconds, 1)
            self.slow_poll_steps = min(self.slow_poll_steps + 1, SLOW_POLL_MAX_STEPS)
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
