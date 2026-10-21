"""A domain-wide gate that keeps our connects from overbooking a Bluetooth radio.

A mass command ("turn off all the lights") makes Home Assistant start every
device's BLE connect within milliseconds. habluetooth sends each one down the
best-scoring path whose radio says it can take a connect, and for an ESPHome
proxy that is the free-slot count the proxy last reported: before its next
report it handed a 3-slot proxy five connects. The proxy refused the extra ones
("No free connections available"); bleak-retry-connector saw a plain disconnect
and moved on to the next path, even a hopeless one, where the device waited out
a 20 s connect timeout.

Every entity command's library call (entity.py) and every background poll
(coordinator.py) first takes a ticket here (``async_connect_slot``):

* A device with a link up (it is in a radio's allocated list), or whose ticket
  another call holds (that call is connecting), goes at once: it needs no new
  connect. So does one no connectable scanner sees: there is nothing to judge.
* Any other device goes when the radio of the path habluetooth would pick now
  (its own scoring, ``BluetoothScannerDevice.score_connection_path``, over the
  paths whose radio reports a free slot) has room: an effective free slot (the
  free count it reported, less our tickets on it that aren't in its allocated
  list yet) and, for a proxy, fewer than CONNECT_GATE_IN_FLIGHT_PER_PROXY
  connects in progress through it, ours or anyone's (an ESPHome proxy jams on
  concurrent connect requests while a slot is still connecting). A local
  adapter takes as many as it has slots: BlueZ runs pending connects side by
  side (in a measured burst a 10 s connect didn't hold up two others on the
  same adapter, done in 0.25 and 0.6 s).
* That path must also be usable for the device: its advertisement there at or
  above CONNECT_GATE_RSSI_FLOOR, fewer than CONNECT_GATE_PATH_FAILURES connects
  through it failed since one worked, and its radio not avoided (below). If it
  isn't, the device waits while a busy usable path (a connect in progress
  through it, or no room) is heard better than the predicted one scores: once
  that one is free, habluetooth's score may put it first. Otherwise waiting
  can't change habluetooth's pick, and the device goes. A device with no usable
  path at all is judged by all its paths.

A radio that has heard no advertisement for CONNECT_GATE_DEAF_SECONDS while it
isn't connecting normally (no connect in progress, or one with no slot listed)
is avoided for CONNECT_GATE_AVOID_SECONDS. A proxy whose connects hang goes deaf
while they do, and may still report every slot free; an ESPHome proxy pauses
scanning while it connects, but lists the slot it reserved for that. Avoided, it
doesn't draw the devices whose better path is only busy. One of our connects
seen coming up through it ends that early. For CONNECT_GATE_RELEASE_GRACE_SECONDS
after one of our calls through a radio ends, no pass judges that radio stuck (a
proxy that paused scanning for the connect hasn't heard anything yet). A call
that failed has its radio checked once that grace is over; one that succeeded
queues no such check (its connect worked), though a later pass still avoids the
radio if it reads deaf and idle then.

A ticket sits on the radio it was predicted for (or the one the device turns up
in the allocated list of). It stops counting as under way once the device is in
that list with no connect in progress there, stops counting at all after
CONNECT_GATE_MAX_WAIT_SECONDS (a proxy still counts a connect habluetooth has in
progress through it), and is given back when the library call returns.

Commands are served before polls, and a poll needing a new connect waits while
any command does, a turn-off waiting in the deferral (deferral.py) included.
Among the commands, those turn-offs of devices already off (``last``) go after
the others; among each, a device with a single usable path goes first (it
has nowhere else to go), then in arrival order. So an H6006 bulb, which fails a
third of its connects through the proxy and almost none through the local
adapter, takes the adapter while the proxy is busy, and a nightstand heard only
through the proxy waits for it, first in line. While anything waits, the
scanners are re-read every CONNECT_GATE_TICK_SECONDS (Home Assistant's API has
no callback for a slot freeing). A call that has waited
CONNECT_GATE_MAX_WAIT_SECONDS goes ahead as it would without the gate, one per
radio per tick and not to a radio another call was admitted to in the same
pass, so calls queued together don't burst out together. If reading the
scanners fails, the waiting calls go ahead unjudged and the error is logged.
When an entry unloads or Home Assistant stops, its device's waiting calls go
ahead at once (``async_release_waiters``), so they reach the device before its
link closes, as they would have without the gate.

Limits: the gate predicts habluetooth's choice but can't force it (habluetooth
chooses again inside the connect, with whatever changed meanwhile), and it can't
see bleak-retry-connector's retries inside one ``establish_connection``. A
proxy's allocated list can lag it: a link just dropped may still read as up. A
radio with no allocated list never shows a connect as up, so our ticket there
counts until its call ends; a proxy reporting no slot count is taken to have a
free one, though its connector may refuse connects. The self-test (capture.py)
isn't gated.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
import logging
from typing import TYPE_CHECKING

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BaseHaRemoteScanner
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey

from .const import (
    CONNECT_GATE_AVOID_SECONDS,
    CONNECT_GATE_DEAF_SECONDS,
    CONNECT_GATE_IN_FLIGHT_PER_PROXY,
    CONNECT_GATE_MAX_WAIT_SECONDS,
    CONNECT_GATE_PATH_FAILURES,
    CONNECT_GATE_RELEASE_GRACE_SECONDS,
    CONNECT_GATE_RSSI_FLOOR,
    CONNECT_GATE_TICK_SECONDS,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BaseHaScanner, BluetoothScannerDevice

_LOGGER = logging.getLogger(__name__)

_GATE: HassKey[ConnectGate] = HassKey(f"{DOMAIN}_connect_gate")


class Outcome(StrEnum):
    """How a call got through the gate (its debug line says)."""

    # Its link is up: no new connect.
    LINKED = "linked"
    # Another call to the device holds its ticket: that call's connect serves both.
    JOINED = "joined"
    # No connectable scanner sees it: nothing to judge.
    UNSEEN = "unseen"
    # Its predicted radio had room.
    ADMITTED = "admitted"
    # It waited CONNECT_GATE_MAX_WAIT_SECONDS and went ahead regardless.
    CAPPED = "capped"
    # Reading the scanners failed: it went ahead regardless.
    UNJUDGED = "unjudged"
    # Its entry is unloading or Home Assistant is stopping: it went ahead before
    # the link closes.
    RELEASED = "released"


@dataclass(slots=True)
class _Ticket:
    """One of our connects: the device, the radio it's on (predicted, then seen)."""

    address: str
    scanner: BaseHaScanner
    admitted_at: float
    holders: int = 1
    # The device is in that radio's allocated list with no connect in progress there.
    connected: bool = False


@dataclass(slots=True)
class Admission:
    """What the gate decided for one call."""

    outcome: Outcome
    waited: float
    # The radio its connect is expected on (or its link is up on); None if none.
    radio_name: str | None
    admitted_at: float
    ticket: _Ticket | None


@dataclass(slots=True)
class _Waiter:
    """A call waiting at the gate."""

    address: str
    poll: bool
    queued_at: float
    future: asyncio.Future[Admission]
    # A turn-off of a device already off: served after the other commands.
    last: bool = False


@dataclass(slots=True)
class _Radio:
    """A scanner as one pass of the gate reads it."""

    scanner: BaseHaScanner
    # The free slots it reported; None when it reports no slot count.
    free: int | None
    # The addresses holding a slot on it, upper-cased like Home Assistant's: a
    # proxy lists one from when it reserves the slot, before the link is up.
    allocated: frozenset[str]
    # habluetooth's connects in progress through it, ours or not.
    in_progress: int
    # It has heard no advertisement for CONNECT_GATE_DEAF_SECONDS.
    deaf: bool
    # A remote scanner (a proxy), not a local adapter.
    proxy: bool

    @property
    def stuck(self) -> bool:
        """Whether it is deaf without connecting normally (a connect with its slot listed)."""
        return self.deaf and not (self.in_progress and self.allocated)

    def lists(self, address: str) -> bool:
        """Whether ``address`` holds (or has reserved) one of its slots."""
        return address in self.allocated

    @property
    def has_free_slot(self) -> bool:
        """Whether it reports a free slot (or reports no slot count at all)."""
        return self.free is None or self.free > 0


@dataclass(slots=True)
class _View:
    """A waiting device's paths, as one pass reads them."""

    paths: list[BluetoothScannerDevice]
    usable: list[BluetoothScannerDevice]
    # The path habluetooth would pick now, and its score there (its RSSI when it
    # is the only path); None if no radio reports a free slot.
    predicted: BluetoothScannerDevice | None
    predicted_score: float

    @property
    def predicted_usable(self) -> bool:
        """Whether the predicted path is one of the usable ones."""
        return any(path is self.predicted for path in self.usable)


def _rssi(path: BluetoothScannerDevice) -> int:
    """Return the device's RSSI as the path's radio last heard it."""
    return path.advertisement.rssi


class ConnectGate:
    """Admits our connects as the radios have room, commands first (see the module doc)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._waiters: list[_Waiter] = []
        self._tickets: dict[str, _Ticket] = {}
        # Avoided radios, by source: until when (Bluetooth monotonic time).
        self._avoided: dict[str, float] = {}
        self._tick: asyncio.TimerHandle | None = None
        # A pass has failed before: later failures are logged at debug only.
        self._failed = False
        # Turn-offs waiting in the deferral: commands to come, so polls wait.
        self._deferred_offs = 0
        # Failed calls' tickets whose radio is checked for being stuck once due
        # (Bluetooth monotonic time), at the next call's pass: no timer is armed
        # for it, and avoiding a radio only matters when a call is judged.
        self._release_checks: list[tuple[float, _Ticket]] = []
        # Radios one of our calls just ended on, by source: until when no pass
        # judges them stuck (a proxy that paused scanning for the connect hasn't
        # heard anything yet; the release check, if due, still runs after).
        self._released: dict[str, float] = {}

    @asynccontextmanager
    async def async_slot(
        self, address: str, *, poll: bool = False, last: bool = False
    ) -> AsyncIterator[Admission]:
        """Wait for the gate to admit a call to ``address``; give its ticket back as it ends.

        ``poll``: a background poll, served after every waiting command.
        ``last``: a turn-off of a device already off, served after the other
        commands (still before polls).
        """
        admission = await self._async_wait(address, poll, last)
        done = False
        try:
            yield admission
            done = True
        finally:
            # Where the device turned up, if not where it was expected.
            seen = admission.ticket.scanner.name if admission.ticket is not None else None
            # Only a failed call says anything about its radio being stuck.
            self._async_release(admission, check=not done)
            _LOGGER.debug(
                "%s: %s %s by the connect gate after %.2f s, radio %s%s; %s %.2f s later",
                address,
                "poll" if poll else "command",
                admission.outcome,
                admission.waited,
                admission.radio_name or "none",
                f" (seen on {seen})" if seen not in (None, admission.radio_name) else "",
                "done" if done else "failed",
                bluetooth.MONOTONIC_TIME() - admission.admitted_at,
            )

    async def _async_wait(self, address: str, poll: bool, last: bool) -> Admission:
        """Queue a call and return its admission; the common case is admitted at once."""
        waiter = _Waiter(
            address, poll, bluetooth.MONOTONIC_TIME(), self._hass.loop.create_future(), last=last
        )
        self._waiters.append(waiter)
        try:
            self._async_pump()
            return await waiter.future
        except asyncio.CancelledError:
            # Cancelled after a pass admitted it, before it resumed: give the ticket back.
            if waiter.future.done() and not waiter.future.cancelled():
                self._async_release(waiter.future.result())
            raise
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
                self._async_schedule()

    @callback
    def _async_release(self, admission: Admission, *, check: bool = False) -> None:
        """Give an admission's ticket back; a radio may have room now.

        ``check``: the call failed, so its radio is checked for being stuck once
        CONNECT_GATE_RELEASE_GRACE_SECONDS have passed (a proxy resumes scanning
        after a connect, and hears nothing until then).
        """
        if (ticket := admission.ticket) is None:
            return
        ticket.holders -= 1
        if ticket.holders:
            return
        del self._tickets[ticket.address]
        due = bluetooth.MONOTONIC_TIME() + CONNECT_GATE_RELEASE_GRACE_SECONDS
        self._released[ticket.scanner.source] = due
        if check:
            self._release_checks.append((due, ticket))
        self._async_pump()

    @callback
    def async_note_deferred_off(self) -> CALLBACK_TYPE:
        """Count a turn-off waiting in the deferral; return the callback that ends it.

        Until it ends, polls needing a new connect wait as they do for a waiting
        command: the turn-off is one, soon. Its end judges nothing at once: the
        turn-off it ends queues right after (and is served first), and the tick
        judges the polls held for it.
        """
        self._deferred_offs += 1
        ended = False

        @callback
        def _async_end() -> None:
            nonlocal ended
            if ended:
                return
            ended = True
            self._deferred_offs -= 1

        return _async_end

    @callback
    def async_link_up(self, address: str) -> bool:
        """Whether a radio lists ``address`` as holding a slot: its link is up (or coming up).

        False if the scanners can't be read, as the gate's own passes treat a
        failure (logged in full once): a broken gate must not hold up a call.
        """
        radios: dict[str, _Radio] = {}
        try:
            return any(
                self._radio(path.scanner, radios).lists(address) for path in self._paths(address)
            )
        except Exception:
            _LOGGER.log(
                logging.DEBUG if self._failed else logging.ERROR,
                "%s: the connect gate couldn't read the Bluetooth scanners;"
                " taking its link as down",
                address,
                exc_info=True,
            )
            self._failed = True
            return False

    @callback
    def async_release_waiters(self, address: str) -> None:
        """Let every call to ``address`` still waiting go ahead now (its entry is going away)."""
        now = bluetooth.MONOTONIC_TIME()
        for waiter in [w for w in self._waiters if w.address == address and not w.future.done()]:
            self._waiters.remove(waiter)
            waiter.future.set_result(
                Admission(Outcome.RELEASED, now - waiter.queued_at, None, now, None)
            )
        self._async_schedule()

    @callback
    def _async_schedule(self) -> None:
        """Keep the tick armed while any call waits, and only then."""
        if self._waiters and self._tick is None:
            self._tick = self._hass.loop.call_later(CONNECT_GATE_TICK_SECONDS, self._async_on_tick)
        elif not self._waiters and self._tick is not None:
            self._tick.cancel()
            self._tick = None

    @callback
    def _async_on_tick(self) -> None:
        """Re-read the scanners: a slot may have freed."""
        self._tick = None
        self._async_pump()

    @callback
    def _async_pump(self) -> None:
        """Admit every waiting call that can go now, in serving order.

        First, the radios of failed calls whose check is due are checked for
        being stuck (a lone call's radio is read by no pass).

        A failure reading the scanners lets every waiting call go unjudged: a
        broken gate must not hold up the commands.
        """
        waiting = [waiter for waiter in self._waiters if not waiter.future.done()]
        try:
            if waiting and self._release_checks:
                now = bluetooth.MONOTONIC_TIME()
                due = [ticket for at, ticket in self._release_checks if at <= now]
                self._release_checks = [(at, t) for at, t in self._release_checks if at > now]
                for ticket in due:
                    self._async_check_released(ticket)
            if waiting:
                self._async_pass(waiting)
        except Exception:
            # Logged in full once; a gate that keeps failing would otherwise log
            # for every call.
            _LOGGER.log(
                logging.DEBUG if self._failed else logging.ERROR,
                "The connect gate failed; letting the waiting calls go ahead unjudged",
                exc_info=True,
            )
            self._failed = True
            now = bluetooth.MONOTONIC_TIME()
            for waiter in waiting:
                if not waiter.future.done():
                    self._waiters.remove(waiter)
                    waiter.future.set_result(
                        Admission(Outcome.UNJUDGED, now - waiter.queued_at, None, now, None)
                    )
        finally:
            self._async_schedule()

    @callback
    def _async_pass(self, waiting: list[_Waiter]) -> None:
        """Read the radios once, then judge the waiting calls in serving order."""
        now = bluetooth.MONOTONIC_TIME()
        radios: dict[str, _Radio] = {}
        paths = {
            address: self._paths(address)
            for address in (*self._tickets, *(waiter.address for waiter in waiting))
        }
        for device_paths in paths.values():
            for path in device_paths:
                self._radio(path.scanner, radios)
        self._released = {source: until for source, until in self._released.items() if until > now}
        for radio in radios.values():
            if radio.stuck and radio.scanner.source not in self._released:
                self._async_avoid(radio.scanner, now)
        self._async_refresh_tickets(paths, radios)
        views = [(waiter, self._view(paths[waiter.address], radios, now)) for waiter in waiting]
        # Commands before polls, and turn-offs of devices already off after the
        # other commands; a device with one usable path before one with a
        # choice; then in arrival order (the waiters' order: the sort is stable).
        views.sort(key=lambda item: (item[0].poll, item[0].last, len(item[1].usable) > 1))
        command_waiting = self._deferred_offs > 0
        # The radios a capped call went to in this pass: one each.
        capped: set[str] = set()
        for waiter, view in views:
            admission = self._async_decide(
                waiter,
                view,
                radios,
                now,
                behind_command=waiter.poll and command_waiting,
                capped=capped,
            )
            if admission is None:
                command_waiting = command_waiting or not waiter.poll
                continue
            if admission.ticket is not None and admission.outcome in (
                Outcome.ADMITTED,
                Outcome.CAPPED,
            ):
                # A capped call doesn't pile onto a radio just given a connect.
                capped.add(admission.ticket.scanner.source)
            self._waiters.remove(waiter)
            waiter.future.set_result(admission)

    @callback
    def _async_check_released(self, ticket: _Ticket) -> None:
        """Avoid a failed call's radio if it's still stuck once the grace period is over.

        It runs at the first call's pass after that (a radio only matters when a
        call is judged), so possibly well after the call ended.

        Not if the device came up through it (seen connected, or listed there with
        no connect in progress), and only while that scanner still hears the
        device: a proxy that reconnected has a new scanner, which this one's
        silence says nothing about.
        """
        scanner = ticket.scanner
        radio = self._radio(scanner, {})
        if ticket.connected or (radio.lists(ticket.address) and not radio.in_progress):
            return
        if any(path.scanner is scanner for path in self._paths(ticket.address)) and radio.stuck:
            self._async_avoid(scanner, bluetooth.MONOTONIC_TIME())

    @callback
    def _async_refresh_tickets(
        self, paths: dict[str, list[BluetoothScannerDevice]], radios: dict[str, _Radio]
    ) -> None:
        """Find each ticket's device in a radio's allocated list, and whether it's connected.

        A device habluetooth sent elsewhere than predicted shows up in that
        radio's list: the ticket moves there. One seen connected ends its
        radio's avoidance: connects through it work.
        """
        for ticket in self._tickets.values():
            listing = (self._radio(path.scanner, radios) for path in paths[ticket.address])
            if (radio := next((r for r in listing if r.lists(ticket.address)), None)) is None:
                ticket.connected = False
                continue
            ticket.scanner = radio.scanner
            ticket.connected = not radio.in_progress
            if ticket.connected:
                self._avoided.pop(radio.scanner.source, None)

    @callback
    def _async_avoid(self, scanner: BaseHaScanner, now: float) -> None:
        """Avoid a stuck radio for CONNECT_GATE_AVOID_SECONDS from now."""
        if self._avoided.get(scanner.source, now) <= now:
            _LOGGER.debug(
                "%s has heard nothing for %.0f s: avoided for %d s",
                scanner.name,
                scanner.time_since_last_detection(),
                CONNECT_GATE_AVOID_SECONDS,
            )
        self._avoided[scanner.source] = now + CONNECT_GATE_AVOID_SECONDS

    def _view(
        self, paths: list[BluetoothScannerDevice], radios: dict[str, _Radio], now: float
    ) -> _View:
        """Judge a waiting device's paths: which are usable, and which habluetooth would pick."""
        usable = [path for path in paths if self._usable(path, now)] or paths
        return _View(paths, usable, *self._predict(paths, radios))

    def _paths(self, address: str) -> list[BluetoothScannerDevice]:
        """Return the device's connectable paths (one per scanner that hears it)."""
        return bluetooth.async_scanner_devices_by_address(self._hass, address, True)

    def _usable(self, path: BluetoothScannerDevice, now: float) -> bool:
        """Whether a path is fit: heard well, not failing again and again, radio not avoided."""
        return (
            _rssi(path) >= CONNECT_GATE_RSSI_FLOOR
            and path.scanner.connection_failures(path.ble_device.address)
            < CONNECT_GATE_PATH_FAILURES
            and self._avoided.get(path.scanner.source, now) <= now
        )

    def _predict(
        self, paths: list[BluetoothScannerDevice], radios: dict[str, _Radio]
    ) -> tuple[BluetoothScannerDevice | None, float]:
        """Return the path habluetooth would connect through now, as it picks one, and its score.

        It ranks the paths by RSSI, then (with more than one) by its score
        (which penalises a radio with connects in progress, earlier failures,
        a last free slot, and ranks a full one last), and takes the first whose
        radio has a free slot.
        """
        ranked = sorted(paths, key=_rssi, reverse=True)
        scores = {id(path): float(_rssi(path)) for path in ranked}
        if len(ranked) > 1:
            rssi_diff = _rssi(ranked[0]) - _rssi(ranked[1])
            scores = {id(path): path.score_connection_path(rssi_diff) for path in ranked}
            ranked.sort(key=lambda path: scores[id(path)], reverse=True)
        for path in ranked:
            if self._radio(path.scanner, radios).has_free_slot:
                return path, scores[id(path)]
        return None, 0.0

    @staticmethod
    def _radio(scanner: BaseHaScanner, radios: dict[str, _Radio]) -> _Radio:
        """Read a scanner once per pass."""
        if (radio := radios.get(scanner.source)) is None:
            free: int | None = None
            allocated: frozenset[str] = frozenset()
            if (allocations := scanner.get_allocations()) is not None:
                if allocations.slots > 0:  # 0: Home Assistant's "no slot count"
                    free = allocations.free
                allocated = frozenset(address.upper() for address in allocations.allocated)
            radio = radios[scanner.source] = _Radio(
                scanner,
                free,
                allocated,
                scanner.connections_in_progress(),
                scanner.time_since_last_detection() >= CONNECT_GATE_DEAF_SECONDS,
                isinstance(scanner, BaseHaRemoteScanner),
            )
        return radio

    @callback
    def _async_decide(
        self,
        waiter: _Waiter,
        view: _View,
        radios: dict[str, _Radio],
        now: float,
        *,
        behind_command: bool,
        capped: set[str],
    ) -> Admission | None:
        """Return a waiting call's admission, or None while it waits on.

        ``behind_command``: a poll, while a command waits. ``capped``: the radios
        a capped call went to in this pass.
        """
        waited = now - waiter.queued_at
        if (admission := self._async_without_connect(waiter, view, radios, now)) is not None:
            return admission
        if not behind_command and (predicted := self._goes_now(view, radios, now)) is not None:
            return self._async_admit(waiter, Outcome.ADMITTED, predicted.scanner, waited, now)
        if waited < CONNECT_GATE_MAX_WAIT_SECONDS:
            return None
        target = (view.predicted or max(view.usable, key=_rssi)).scanner
        if target.source in capped:
            return None  # the next tick
        capped.add(target.source)
        return self._async_admit(waiter, Outcome.CAPPED, target, waited, now)

    @callback
    def _async_without_connect(
        self, waiter: _Waiter, view: _View, radios: dict[str, _Radio], now: float
    ) -> Admission | None:
        """Admit a call that needs no new connect (joined, unseen or linked), if it's one."""
        waited = now - waiter.queued_at
        if (ticket := self._tickets.get(waiter.address)) is not None:
            ticket.holders += 1
            return Admission(Outcome.JOINED, waited, ticket.scanner.name, now, ticket)
        if not view.paths:
            return Admission(Outcome.UNSEEN, waited, None, now, None)
        for path in view.paths:
            if self._radio(path.scanner, radios).lists(waiter.address):
                return Admission(Outcome.LINKED, waited, path.scanner.name, now, None)
        return None

    def _goes_now(
        self, view: _View, radios: dict[str, _Radio], now: float
    ) -> BluetoothScannerDevice | None:
        """Return the predicted path if a connect through it may start now."""
        predicted = view.predicted
        if predicted is None or not self._has_room(self._radio(predicted.scanner, radios), now):
            return None
        if view.predicted_usable or not any(
            _rssi(path) > view.predicted_score and self._busy(path, radios, now)
            for path in view.usable
        ):
            return predicted
        return None

    def _busy(self, path: BluetoothScannerDevice, radios: dict[str, _Radio], now: float) -> bool:
        """Whether a path's radio can't take a connect now (once it can, the pick may move)."""
        radio = self._radio(path.scanner, radios)
        return bool(radio.in_progress) or not self._has_room(radio, now)

    def _has_room(self, radio: _Radio, now: float) -> bool:
        """Whether a radio can take one more of our connects.

        A ticket older than CONNECT_GATE_MAX_WAIT_SECONDS no longer counts. A
        proxy's connects in progress are habluetooth's (ours once they've
        started, and anyone else's) or our tickets not seen up yet, whichever
        are more.
        """
        ours = [
            ticket
            for ticket in self._tickets.values()
            if ticket.scanner.source == radio.scanner.source
            and now - ticket.admitted_at < CONNECT_GATE_MAX_WAIT_SECONDS
        ]
        connecting = max(radio.in_progress, sum(not ticket.connected for ticket in ours))
        if radio.proxy and connecting >= CONNECT_GATE_IN_FLIGHT_PER_PROXY:
            return False
        if radio.free is None:
            return True
        unlisted = sum(not radio.lists(ticket.address) for ticket in ours)
        return radio.free - unlisted > 0

    @callback
    def _async_admit(
        self,
        waiter: _Waiter,
        outcome: Outcome,
        scanner: BaseHaScanner,
        waited: float,
        now: float,
    ) -> Admission:
        """Issue a ticket for a waiting call's connect, on the radio expected to carry it."""
        ticket = self._tickets[waiter.address] = _Ticket(waiter.address, scanner, now)
        return Admission(outcome, waited, scanner.name, now, ticket)


def _gate(hass: HomeAssistant) -> ConnectGate:
    """Return the domain-wide connect gate."""
    if (gate := hass.data.get(_GATE)) is None:
        gate = hass.data[_GATE] = ConnectGate(hass)
    return gate


@callback
def async_connect_slot(
    hass: HomeAssistant, address: str, *, poll: bool = False, last: bool = False
) -> AbstractAsyncContextManager[Admission]:
    """Return the context a connect to ``address`` runs in, once the gate admits it."""
    return _gate(hass).async_slot(address, poll=poll, last=last)


@callback
def async_note_deferred_off(hass: HomeAssistant) -> CALLBACK_TYPE:
    """Count a turn-off waiting in the deferral (polls wait for it); return its end."""
    return _gate(hass).async_note_deferred_off()


@callback
def async_link_up(hass: HomeAssistant, address: str) -> bool:
    """Whether a Bluetooth radio lists the device at ``address`` as holding a slot."""
    return _gate(hass).async_link_up(address)


@callback
def async_release_waiters(hass: HomeAssistant, address: str) -> None:
    """Let the calls to ``address`` still waiting at the gate go ahead now."""
    _gate(hass).async_release_waiters(address)
