"""A turn-off of a device already off waits for the other commands of a burst.

"Turn off all the lights" sends one command per device at the same time, and
each takes one of the few connection slots of the adapter or proxy that
reaches its device (an ESPHome proxy has three). A light that is already off
takes its slot like one that changes, and in real nightly bursts about half of
them were: the last light to go out waited behind one.

So a turn-off of a device that already said it is off
(``GoveeBleLocalCoordinator.power_confirmed_off``) waits while commands to
other devices run, for at most DEFER_OFF_MAX_SECONDS, then goes out. It is
still sent: a light wrongly believed off still goes off, only later. A
command to one device alone never waits, and such turn-offs don't wait for
each other: while one is sent, its device doesn't count as commanding.

The wait runs in the entity's service call, before its command starts, so
COMMAND_TIMEOUT_SECONDS doesn't count it. Home Assistant has already taken the
entity platform's PARALLEL_UPDATES slot for the device by then, so the
device's other entities of that platform (a fixture's zone and segment lights)
wait behind it, in the order HA started them: a later turn-on of the same light
must still come after it. So another call to the same entity queued while one
waits (``OffDeferral.async_hurry``: a motion automation's turn-on, say) ends
the wait: the turn-off goes out at once, and the newer call follows it, as
without the deferral. A call to another entity of the device doesn't (a
fixture's zone lights queued with it in the same "all off" must not send it
early), except for a waiting zone's turn-off, which any call to the device ends
(sent early, it costs one frame to a fixture already dark). A turn-off of a
device whose link is up doesn't wait at all: it saves no connection slot. A
turn-off still waiting when HA stops or the entity is removed is dropped: the
link is closing, and the device said it is off.

While it waits, it counts toward the burst of commands to several devices
(``coordinator.CommandBurst``: every device's polls wait) and, at the connect
gate, as a command to come (polls needing a connect wait behind it); once it is
sent, the gate serves it after the other commands.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
import logging

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util.hass_dict import HassKey

from .connect_gate import async_link_up, async_note_deferred_off
from .const import DEFER_OFF_MAX_SECONDS, DOMAIN
from .coordinator import command_burst

_LOGGER = logging.getLogger(__name__)

# The devices whose turn-off is being sent although they already said they are
# off: they don't count as commanding. One at a time each: only one entity of a
# device turns it off whole, and HA runs an entity's calls one at a time
# (PARALLEL_UPDATES).
_CONFIRMED_OFFS: HassKey[set[str]] = HassKey(f"{DOMAIN}_confirmed_offs")

# Loop steps a turn-off lets pass before it looks for other commands, so that
# the commands started with it have started too. They take no time. A call for
# several entities runs each in a task of its own, a step later, and so does
# each layer above it: homeassistant.turn_off's call per domain, or a light
# group's call for its members. Three steps cover an "all lights" group of room
# groups next to a light called on its own (Assist calls each entity on its
# own); a light that looks too early just goes out at once, as before.
_START_STEPS = 3


# -- command tracker adapter ----------------------------------------------------
#
# Every look at which devices have entity commands running goes through these
# two functions, which read the integration-wide command tracker
# (``coordinator.CommandBurst``): the tests can stand a tracker of their own
# behind them.


@callback
def commanding_devices(hass: HomeAssistant) -> set[str]:
    """Return the addresses of the devices an entity command is running to."""
    return command_burst(hass).commanding_devices


@callback
def async_track_command_changes(hass: HomeAssistant, action: CALLBACK_TYPE) -> CALLBACK_TYPE:
    """Call ``action`` whenever an entity command to any device starts or ends.

    Returns the callback that stops the calls.
    """
    return command_burst(hass).async_add_command_listener(action)


@callback
def async_note_waiting(hass: HomeAssistant, address: str) -> CALLBACK_TYPE:
    """Count a turn-off of ``address`` waiting: toward the burst, and at the connect gate.

    Returns the callback that ends both counts.
    """
    burst = command_burst(hass)
    burst.async_note_deferred_started(address)
    end_gate = async_note_deferred_off(hass)

    @callback
    def _async_end() -> None:
        burst.async_note_deferred_finished(address)
        end_gate()

    return _async_end


# -- deferral -------------------------------------------------------------------


def _confirmed_offs(hass: HomeAssistant) -> set[str]:
    """Return the devices whose turn-off is being sent although they said they are off."""
    if (offs := hass.data.get(_CONFIRMED_OFFS)) is None:
        offs = hass.data[_CONFIRMED_OFFS] = set()
    return offs


@callback
def _others_commanding(hass: HomeAssistant, address: str) -> bool:
    """Return whether a command to another device runs, other than such a turn-off."""
    return bool(commanding_devices(hass) - {address} - _confirmed_offs(hass))


class OffDeferral:
    """One entity's turn-offs of its device while that device already said it is off."""

    def __init__(self, address: str) -> None:
        """Start with none waiting, for the device at ``address``."""
        self._address = address
        self._waits: set[asyncio.Future[bool]] = set()
        # How often the entity was removed: a turn-off that started before the
        # last removal is dropped, even if it wasn't waiting yet.
        self._removals = 0

    @asynccontextmanager
    async def async_turn_off(self, hass: HomeAssistant) -> AsyncIterator[bool]:
        """Wrap such a turn-off: yield whether to send it, after waiting (``_async_wait``).

        While it is sent, its device doesn't count as commanding for the other
        waiting turn-offs.
        """
        if not await self._async_wait(hass):
            yield False
            return
        offs = _confirmed_offs(hass)
        offs.add(self._address)
        try:
            yield True
        finally:
            offs.discard(self._address)

    async def _async_wait(self, hass: HomeAssistant) -> bool:
        """Wait while commands to other devices run; return whether to send the turn-off.

        True once no other device's command runs (at once if none does, or if
        the device's link is already up: waiting would save no connection slot),
        or after DEFER_OFF_MAX_SECONDS; False if HA stops or the entity is
        removed meanwhile (``async_drop``).
        """
        removals = self._removals
        for _ in range(_START_STEPS):
            await asyncio.sleep(0)
        if self._removals != removals:
            return False
        if (
            hass.is_stopping
            or not _others_commanding(hass, self._address)
            # Its link is up (a command to it just landed): waiting saves no slot.
            or async_link_up(hass, self._address)
        ):
            return True
        outcome: asyncio.Future[bool] = hass.loop.create_future()

        @callback
        def _async_commands_changed() -> None:
            if not _others_commanding(hass, self._address):
                self._async_end(outcome, True, "the other devices' commands are done")

        @callback
        def _async_cap_reached(_now: datetime) -> None:
            self._async_end(outcome, True, f"waited {DEFER_OFF_MAX_SECONDS} s")

        @callback
        def _async_stopping(_event: Event) -> None:
            self._async_end(outcome, False, "Home Assistant is stopping")

        _LOGGER.debug("%s: already off: turn-off waits for other devices", self._address)
        unsubs = (
            async_track_command_changes(hass, _async_commands_changed),
            async_call_later(hass, DEFER_OFF_MAX_SECONDS, _async_cap_reached),
            hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, _async_stopping),
            async_note_waiting(hass, self._address),
        )
        self._waits.add(outcome)
        try:
            return await outcome
        finally:
            self._waits.discard(outcome)
            for unsub in unsubs:
                unsub()

    @callback
    def async_hurry(self) -> None:
        """Send every turn-off of this entity still waiting now: a newer call to it is queued."""
        for outcome in list(self._waits):
            self._async_end(outcome, True, "a newer call is queued behind it")

    @callback
    def async_drop(self) -> None:
        """Drop every turn-off waiting or about to: the entity is being removed."""
        self._removals += 1
        for outcome in self._waits:
            self._async_end(outcome, False, "the entity is being removed")

    @callback
    def _async_end(self, outcome: asyncio.Future[bool], send: bool, why: str) -> None:
        """End a wait, unless another ending came first (in the same loop iteration)."""
        if outcome.done():
            return
        _LOGGER.debug("%s: turn-off %s: %s", self._address, "goes out" if send else "dropped", why)
        outcome.set_result(send)
