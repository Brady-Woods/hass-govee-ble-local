"""Tests for holding every device's background polls during a burst of commands.

A burst is entity commands to two or more devices at once (a light group, an
area, "turn off all the lights"): every device's background polls wait until
POLL_HOLDOFF_AFTER_COMMAND_SECONDS after the last command anywhere ends.

The devices are three H60A6 ceilings, a model the advertisement policy leaves
on the fixed cadence, so every device's poll comes due every POLL_INTERVAL_SECONDS
(jitter 0). Time moves on a moving clock: wall, loop and Bluetooth time
advance together, and what falls due runs as it would.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from govee_ble_local import Device
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util, slugify
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.govee_ble_local.const import (
    CONF_SKU,
    DOMAIN,
    MAX_CONCURRENT_POLLS,
    POLL_HOLDOFF_AFTER_COMMAND_SECONDS,
    POLL_HOLDOFF_MAX_SECONDS,
    POLL_INTERVAL_SECONDS,
)
from custom_components.govee_ble_local.coordinator import (
    CommandBurst,
    GoveeBleLocalCoordinator,
    _poll_semaphore,
    command_burst,
)

from .conftest import make_device, make_service_info
from .const import ADDRESS, TITLE

BASE = POLL_INTERVAL_SECONDS
HOLDOFF = POLL_HOLDOFF_AFTER_COMMAND_SECONDS
CAP = POLL_HOLDOFF_MAX_SECONDS
FLEET = ((ADDRESS, TITLE), ("AA:BB:CC:DD:EE:02", "Second Ceiling"), ("AA:BB:CC:DD:EE:03", "Hall"))
# The clock's step.
STEP = 0.5


def near(seconds: float) -> Any:
    """A poll time, to within a second: the test clock runs timers up to a step early."""
    return pytest.approx(seconds, abs=1)


@dataclass
class Member:
    """One configured device: its entry, its device and when its polls ran (loop time)."""

    entry: MockConfigEntry
    device: Device
    polls: list[float] = field(default_factory=list)

    @property
    def coordinator(self) -> GoveeBleLocalCoordinator:
        coordinator: GoveeBleLocalCoordinator = self.entry.runtime_data.coordinator
        return coordinator

    @property
    def light(self) -> str:
        """The main light's entity id."""
        return f"light.{slugify(self.entry.title)}"

    @property
    def address(self) -> str:
        """The device's address (still there once the entry is unloaded)."""
        return str(self.entry.data["address"])

    def start(self) -> None:
        """An entity command to this device starts (and keeps running)."""
        self.coordinator.async_note_command_started()

    def finish(self) -> None:
        """A command to this device ends."""
        self.coordinator.async_note_command_finished()


class Clock:
    """Wall, loop and Bluetooth time, moved together; what falls due runs as it would.

    Each step runs what falls due within the next step (``async_fire_time_changed`` fires
    timers up to half a second early), so a poll runs up to a step before it is due.
    """

    def __init__(self, hass: HomeAssistant, freezer: Any, monotonic: SimpleNamespace) -> None:
        self.hass = hass
        self.freezer = freezer
        self.monotonic = monotonic
        self.start = hass.loop.time()

    @property
    def now(self) -> float:
        """Seconds since the clock was made."""
        return self.hass.loop.time() - self.start

    async def run(self, seconds: float, *, settle: bool = True) -> None:
        """Let ``seconds`` pass, step by step.

        Unless ``settle`` is False, each step then waits for the work it started;
        otherwise it only yields, so a service call a test holds open doesn't block it.
        """
        for _ in range(round(seconds / STEP)):
            self.freezer.tick(timedelta(seconds=STEP))
            self.monotonic.now += STEP
            async_fire_time_changed(self.hass)
            if settle:
                await self.hass.async_block_till_done()
            else:
                for _ in range(5):
                    await asyncio.sleep(0)

    async def run_to(self, at: float, *, settle: bool = True) -> None:
        """Let time pass until ``at`` seconds since the clock was made."""
        await self.run(at - self.now, settle=settle)

    def since(self, member: Member) -> list[float]:
        """When the member's polls ran, in seconds since the clock was made."""
        return [round(at - self.start, 1) for at in member.polls]


@pytest.fixture(autouse=True)
def jitter() -> Generator[MagicMock]:
    """Deterministic poll intervals (no jitter)."""
    with patch(
        "custom_components.govee_ble_local.coordinator.random.uniform", return_value=0
    ) as uniform:
        yield uniform


async def add_member(hass: HomeAssistant, address: str, title: str, *, stagger: int = 0) -> Member:
    """Set an H60A6 entry up; its first poll waits ``stagger`` seconds (done now if 0)."""
    member = Member(
        MockConfigEntry(
            domain=DOMAIN,
            title=title,
            unique_id=address,
            data={"address": address, CONF_SKU: "H60A6"},
        ),
        make_device(address=address),
    )

    async def _update(**_kwargs: Any) -> Any:
        member.polls.append(hass.loop.time())
        member.device.state.optimistic = False
        return member.device.state

    member.device.update.side_effect = _update  # type: ignore[attr-defined]
    member.entry.add_to_hass(hass)
    with (
        patch("custom_components.govee_ble_local.create_device", return_value=member.device),
        patch("custom_components.govee_ble_local._first_poll_stagger", return_value=stagger),
    ):
        assert await hass.config_entries.async_setup(member.entry.entry_id)
        # A staggered first poll sleeps on the moving clock: don't wait for it here.
        await hass.async_block_till_done(wait_background_tasks=not stagger)
    return member


@pytest.fixture
def clock(hass: HomeAssistant, freezer: Any, monotonic: SimpleNamespace) -> Clock:
    """The moving clock, started on a whole second before any entry is set up.

    Like the Bluetooth clock (conftest ``monotonic``): HA arms a poll timer on a whole second
    of the loop clock plus a fraction of its own, so from a fractional start the poll due at
    120 s would come a second early for some start times and not others.
    """
    freezer.move_to(dt_util.utcnow().replace(microsecond=0) + timedelta(seconds=1))
    return Clock(hass, freezer, monotonic)


@pytest.fixture
async def fleet(hass: HomeAssistant, clock: Clock, mock_bluetooth: SimpleNamespace) -> list[Member]:
    """Three loaded entries, each polled once at setup (at 0 s): the next polls are due at 120 s."""
    members = [await add_member(hass, address, title) for address, title in FLEET]
    for member in members:
        assert clock.since(member) == [0.0]
    return members


@pytest.fixture
def burst(hass: HomeAssistant) -> CommandBurst:
    """The domain-wide burst tracker."""
    return command_burst(hass)


def timers(
    hass: HomeAssistant, name: str, owner: object | None = None
) -> list[asyncio.TimerHandle]:
    """The loop's live timers whose callback (or HA job target) is named ``name``, of ``owner``."""
    found = []
    for handle in hass.loop._scheduled:  # type: ignore[attr-defined]
        if handle.cancelled():
            continue
        for target in (handle._callback, *(getattr(arg, "target", None) for arg in handle._args)):
            if getattr(target, "__name__", "").endswith(name) and (
                owner is None or getattr(target, "__self__", None) is owner
            ):
                found.append(handle)
                break
    return found


def poll_timers(hass: HomeAssistant, member: Member) -> list[asyncio.TimerHandle]:
    """The member's live HA poll timers: one while polls are scheduled, never two."""
    return timers(hass, "wrap_handle_refresh_interval", member.coordinator)


def holding_timers(hass: HomeAssistant, member: Member) -> list[asyncio.TimerHandle]:
    """The member's live hold-off and cap timers."""
    coordinator = member.coordinator
    return [
        *timers(hass, "_async_end_holdoff", coordinator),
        *timers(hass, "_async_holdoff_cap_reached", coordinator),
    ]


def due(clock: Clock, handle: asyncio.TimerHandle) -> float:
    """When a timer is due, in seconds since the clock was made.

    A poll "due at 120 s" is due a fraction of a second after it (HA's per-coordinator
    offset), and a cap armed as it is held follows it.
    """
    return handle.when() - clock.start


async def fire(hass: HomeAssistant, handle: asyncio.TimerHandle) -> None:
    """Run one due timer, as the loop does when it is the earliest."""
    handle._run()  # type: ignore[attr-defined]
    handle.cancel()
    await hass.async_block_till_done()


async def until(condition: Callable[[], bool]) -> None:
    """Yield to the loop until ``condition`` holds (without waiting for HA's tasks)."""
    for _ in range(50):
        if condition():
            return
        await asyncio.sleep(0)
    pytest.fail("condition never held")


def gate_commands(*members: Member) -> asyncio.Event:
    """Make the members' turn-on commands run until the returned event is set."""
    gate = asyncio.Event()

    async def _held(*_args: Any, **_kwargs: Any) -> None:
        await gate.wait()

    for member in members:
        member.device.turn_on.side_effect = _held  # type: ignore[attr-defined]
    return gate


# -- a burst holds every device's polls -----------------------------------------------------


async def test_a_group_command_holds_another_devices_poll_until_after_the_last_command(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], burst: CommandBurst
) -> None:
    """One service call turning two lights on is a burst: the third device's poll, due while
    it runs, waits until 25 s after both commands end, then runs once, on one timer."""
    first, second, hall = fleet
    await clock.run_to(100)
    gate = gate_commands(first, second)
    # Not a Home Assistant task: async_block_till_done would wait for the held commands.
    group = asyncio.create_task(
        hass.services.async_call(
            "light", "turn_on", {"entity_id": [first.light, second.light]}, blocking=True
        )
    )
    await until(lambda: burst.active)

    # Every device's poll came due at about 120 s: all held.
    await clock.run_to(130, settle=False)
    assert hall.coordinator._poll_waiting
    assert [clock.since(m) for m in fleet] == [[0.0]] * 3
    gate.set()
    await group  # both commands end at 130 s
    await hass.async_block_till_done()

    await clock.run_to(130 + HOLDOFF - 1)
    assert burst.active
    assert clock.since(hall) == [0.0]
    await clock.run(2)
    assert not burst.active
    assert burst._unsub_end is None
    assert clock.since(hall) == [0.0, near(130 + HOLDOFF)]
    for member in fleet:
        assert len(member.polls) == 2  # one poll per device, released once
        assert len(poll_timers(hass, member)) == 1
        assert holding_timers(hass, member) == []
    assert timers(hass, "_async_end_burst") == []

    # Then the fixed cadence again, from that poll: one poll per interval, one timer.
    await clock.run(5 * BASE + BASE / 2)
    assert len(hall.polls) == 2 + 5
    assert len(poll_timers(hass, hall)) == 1


async def test_a_first_poll_due_during_a_burst_waits_and_runs_once(
    hass: HomeAssistant, clock: Clock, mock_bluetooth: SimpleNamespace, burst: CommandBurst
) -> None:
    """An entry set up during a burst holds its first poll apart and runs it once when the
    burst ends, cancelling HA's first timer: two timers would otherwise poll for good."""
    first = await add_member(hass, *FLEET[0])
    second = await add_member(hass, *FLEET[1])
    first.start()
    second.start()
    assert burst.active
    hall = await add_member(hass, *FLEET[2], stagger=10)
    await clock.run_to(15)  # its first poll came due at 10 s
    assert hall.coordinator._first_poll_waiting
    assert hall.polls == []

    await clock.run_to(20)
    first.finish()
    second.finish()
    await clock.run_to(20 + HOLDOFF - 1)
    assert hall.polls == []
    await clock.run(2)
    assert clock.since(hall) == [near(20 + HOLDOFF)]
    assert len(poll_timers(hass, hall)) == 1

    await clock.run(15 * BASE + BASE / 2)
    assert len(hall.polls) == 1 + 15  # one poll per base interval, not two
    assert len(poll_timers(hass, hall)) == 1


async def test_a_first_poll_held_by_a_burst_still_runs_when_polls_stop(
    hass: HomeAssistant, clock: Clock, mock_bluetooth: SimpleNamespace
) -> None:
    """The first poll reads the device's info: a policy step that stops background polls
    during the burst (a light turned off; the H60A6 never gets one, so it is called here
    directly) drops a held scheduled poll, never the first one."""
    first = await add_member(hass, *FLEET[0])
    second = await add_member(hass, *FLEET[1])
    first.start()
    second.start()
    hall = await add_member(hass, *FLEET[2], stagger=10)
    await clock.run_to(15)
    hall.coordinator._async_clear_poll_interval()  # as the policy does for a light turned off
    assert hall.coordinator._first_poll_waiting
    first.finish()
    second.finish()
    await clock.run_to(15 + HOLDOFF + 1)
    assert clock.since(hall) == [near(15 + HOLDOFF)]
    assert len(poll_timers(hass, hall)) == 1  # the poll set the fixed cadence again: one timer
    assert holding_timers(hass, hall) == []


# -- what is and isn't a burst ---------------------------------------------------------------


async def test_commands_to_one_device_at_a_time_hold_no_other_devices_poll(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], burst: CommandBurst
) -> None:
    """Commands to one device, even two at once (a zone and the main light), hold only its own
    polls; so do commands to two devices one after the other."""
    first, second, hall = fleet
    [hall_timer] = poll_timers(hass, hall)
    hall_due = due(clock, hall_timer)
    await clock.run_to(100)
    first.start()
    first.start()  # a zone and the main light: one device
    await clock.run_to(105)
    first.finish()
    first.finish()
    second.start()  # the next device, once the first one's commands have ended
    await clock.run_to(110)
    second.finish()
    assert not burst.active
    assert timers(hass, "_async_end_burst") == []

    await clock.run_to(122)
    assert clock.since(hall) == [0.0, near(hall_due)]  # on time
    assert len(first.polls) == len(second.polls) == 1  # each waits for its own hold-off
    await clock.run_to(137)
    assert clock.since(first) == [0.0, near(105 + HOLDOFF)]
    assert clock.since(second) == [0.0, near(110 + HOLDOFF)]


async def test_overlapping_commands_to_one_device_in_a_burst_all_count(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], burst: CommandBurst
) -> None:
    """Two commands to one device during a burst: the burst's tail waits for both."""
    first, second, hall = fleet
    await clock.run_to(100)
    first.start()
    first.start()
    second.start()
    await clock.run_to(125)
    first.finish()
    second.finish()  # one command to the first device still runs
    await clock.run_to(170)
    assert burst.active
    assert clock.since(hall) == [0.0]
    first.finish()
    await clock.run_to(170 + HOLDOFF + 1)
    assert clock.since(hall) == [0.0, near(170 + HOLDOFF)]


async def test_an_unmatched_command_end_changes_nothing(
    hass: HomeAssistant, fleet: list[Member], burst: CommandBurst
) -> None:
    """A command end the burst never saw start doesn't count below zero: the next commands to
    two devices are still a burst, and it ends only after the last of them."""
    first, second, _hall = fleet
    first.finish()
    assert burst._running == {}
    first.start()
    second.start()
    first.finish()
    assert burst.active
    assert timers(hass, "_async_end_burst") == []  # the second device's command still runs
    second.finish()
    assert len(timers(hass, "_async_end_burst")) == 1
    first.finish()  # another unmatched end, in the tail: it starts again, one timer
    assert len(timers(hass, "_async_end_burst")) == 1


async def test_a_burst_is_logged_once(
    hass: HomeAssistant, fleet: list[Member], caplog: pytest.LogCaptureFixture
) -> None:
    """A third device joining a running burst doesn't start another."""
    caplog.set_level(logging.DEBUG, logger="custom_components.govee_ble_local.coordinator")
    for member in fleet:
        member.start()
    assert caplog.text.count("every poll waits") == 1
    for member in fleet:
        member.finish()


# -- a turn-off waiting in the deferral ------------------------------------------------------
#
# A turn-off of a device already off waits while commands to other devices run (deferral.py),
# and the deferral counts it here while it waits: a burst counts it as it does a command.


async def test_a_turn_off_waiting_for_another_devices_command_starts_a_burst(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], burst: CommandBurst
) -> None:
    """A command to one device and a turn-off of another waiting for it are two devices at
    once: every device's poll waits until 25 s after the command has ended and the turn-off no
    longer waits."""
    first, second, hall = fleet
    await clock.run_to(100)
    first.start()
    burst.async_note_deferred_started(second.address)
    assert burst.active

    await clock.run_to(130)  # every device's poll came due at about 120 s: all held
    assert hall.coordinator._poll_waiting
    assert second.coordinator._poll_waiting
    assert [clock.since(m) for m in fleet] == [[0.0]] * 3
    first.finish()
    burst.async_note_deferred_finished(second.address)  # it goes out (or is dropped)
    await clock.run_to(130 + HOLDOFF - 1)
    assert [clock.since(m) for m in fleet] == [[0.0]] * 3
    await clock.run(2)
    assert not burst.active
    assert clock.since(hall) == clock.since(second) == [0.0, near(130 + HOLDOFF)]


async def test_a_waiting_turn_off_isnt_a_command_to_its_device(
    hass: HomeAssistant, fleet: list[Member], burst: CommandBurst
) -> None:
    """The waiting turn-offs ask which devices a command runs to: one that only waits isn't
    among them (nor among the other devices commanding), so they don't wait for each other."""
    first, second, hall = fleet
    first.start()
    burst.async_note_deferred_started(second.address)
    burst.async_note_deferred_started(hall.address)
    assert burst.commanding_devices == {first.address}
    assert not burst.other_devices_commanding(first.address)
    assert burst.other_devices_commanding(second.address)  # the first device's command
    first.finish()
    assert burst.commanding_devices == set()
    assert not burst.other_devices_commanding(second.address)
    assert not burst.other_devices_commanding(hall.address)
    burst.async_note_deferred_finished(second.address)
    burst.async_note_deferred_finished(hall.address)


@pytest.mark.parametrize("last", ["the_turn_off", "the_command"])
async def test_the_tail_starts_once_the_command_and_the_waiting_turn_off_are_both_done(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], burst: CommandBurst, *, last: str
) -> None:
    """The burst's tail waits for the turn-off as it does for a command, whichever is done
    last: the turn-off still waiting, or the command still running after the turn-off went
    out at its cap."""
    first, second, hall = fleet
    await clock.run_to(100)
    first.start()
    burst.async_note_deferred_started(second.address)
    await clock.run_to(125)
    if last == "the_turn_off":
        first.finish()
    else:
        burst.async_note_deferred_finished(second.address)
    assert burst.active
    assert timers(hass, "_async_end_burst") == []

    await clock.run_to(150)
    assert clock.since(hall) == [0.0]
    if last == "the_turn_off":
        burst.async_note_deferred_finished(second.address)
    else:
        first.finish()
    assert len(timers(hass, "_async_end_burst")) == 1
    await clock.run_to(150 + HOLDOFF - 1)
    assert clock.since(hall) == [0.0]
    await clock.run(2)
    assert not burst.active
    assert clock.since(hall) == [0.0, near(150 + HOLDOFF)]


async def test_two_turn_offs_waiting_for_one_device_both_count(
    hass: HomeAssistant, fleet: list[Member], burst: CommandBurst
) -> None:
    """Two turn-offs of one device waiting at once (a fixture's two zone lights): the burst's
    tail starts only once neither waits."""
    first, second, _hall = fleet
    first.start()
    burst.async_note_deferred_started(second.address)
    burst.async_note_deferred_started(second.address)
    first.finish()
    burst.async_note_deferred_finished(second.address)
    assert burst.active
    assert timers(hass, "_async_end_burst") == []  # the other one still waits
    burst.async_note_deferred_finished(second.address)
    assert len(timers(hass, "_async_end_burst")) == 1


# -- the tail, the device's own hold-off and the cap ------------------------------------------


async def test_a_command_in_the_tail_restarts_it(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], burst: CommandBurst
) -> None:
    """A command to any one device during the tail extends the burst: the tail starts again
    when it ends, and the other devices' own hold-offs ending meanwhile release nothing."""
    first, second, hall = fleet
    await clock.run_to(100)
    first.start()
    second.start()
    await clock.run_to(130)  # every poll held
    first.finish()
    second.finish()
    [tail] = timers(hass, "_async_end_burst")

    await clock.run_to(150)
    hall.start()  # one device alone, inside the tail
    assert tail.cancelled()
    assert burst._unsub_end is None
    assert burst.active
    await clock.run_to(152)
    hall.finish()
    await clock.run_to(152 + HOLDOFF - 1)  # past the first two's own hold-offs (155 s)
    assert [clock.since(m) for m in fleet] == [[0.0]] * 3
    assert first.coordinator._poll_waiting
    await clock.run(2)
    for member in fleet:
        assert clock.since(member) == [0.0, near(152 + HOLDOFF)]
        assert len(poll_timers(hass, member)) == 1


async def test_the_devices_own_hold_off_still_running_as_the_burst_ends(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], burst: CommandBurst
) -> None:
    """The device whose command ended last starts the burst's tail and its own hold-off
    together, the tail first: as the burst ends, that device's poll waits for its own
    hold-off, and the others' run."""
    first, second, hall = fleet
    await clock.run_to(100)
    first.start()
    second.start()
    await clock.run_to(128)
    second.finish()
    await clock.run_to(130)
    first.finish()  # the last command anywhere: the tail, then its own hold-off
    [tail] = timers(hass, "_async_end_burst")
    [own] = timers(hass, "_async_end_holdoff", first.coordinator)
    assert tail.when() <= own.when()

    await clock.run_to(130 + HOLDOFF - 1)  # the second device's own hold-off has ended
    assert [clock.since(m) for m in fleet] == [[0.0]] * 3
    await fire(hass, tail)  # the loop takes the earlier one first
    assert not burst.active
    assert clock.since(second) == clock.since(hall) == [0.0, near(130 + HOLDOFF)]
    assert clock.since(first) == [0.0]
    assert first.coordinator._poll_waiting  # its own hold-off still runs

    await clock.run(2)
    assert clock.since(first) == [0.0, near(130 + HOLDOFF)]
    for member in fleet:
        assert len(member.polls) == 2
        assert len(poll_timers(hass, member)) == 1


async def test_the_cap_runs_a_poll_a_long_burst_holds(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], burst: CommandBurst
) -> None:
    """A burst kept going by command after command holds a poll for at most the cap: then
    it runs, and the next one due waits again until the burst ends."""
    first, second, hall = fleet
    await clock.run_to(100)
    first.start()
    second.start()
    await clock.run_to(122)
    assert hall.coordinator._poll_waiting  # due at about 120 s
    [cap] = timers(hass, "_async_holdoff_cap_reached", hall.coordinator)
    cap_due = due(clock, cap)
    assert cap_due == pytest.approx(120 + CAP, abs=2)
    while clock.now < 400:  # one command after another to the first device
        await clock.run(20)
        first.finish()
        first.start()
        assert burst.active
    # The cap ran it; that poll re-armed HA's timer, due again at about 360 s, and held.
    assert clock.since(hall) == [0.0, near(cap_due)]
    assert hall.coordinator._poll_waiting
    first.finish()
    second.finish()
    end = clock.now
    await clock.run(HOLDOFF + 1)
    assert clock.since(hall) == [0.0, near(cap_due), near(end + HOLDOFF)]
    assert len(poll_timers(hass, hall)) == 1
    assert holding_timers(hass, hall) == []


async def take_every_poll_place(hass: HomeAssistant) -> Callable[[], None]:
    """Take every MAX_CONCURRENT_POLLS place (other devices' polls); return what gives them back."""
    semaphore = _poll_semaphore(hass)
    for _ in range(MAX_CONCURRENT_POLLS):
        await semaphore.acquire()

    def _give_back() -> None:
        for _ in range(MAX_CONCURRENT_POLLS):
            semaphore.release()

    return _give_back


@pytest.mark.parametrize("return_poll", [False, True], ids=["scheduled", "with_a_return_poll"])
async def test_a_released_poll_still_waiting_for_its_place_waits_for_the_next_burst(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], burst: CommandBurst, return_poll: bool
) -> None:
    """The polls a burst's end released wait for a poll place; a burst starting meanwhile holds
    them again once they have one: they read nothing until 25 s after it, then run once.

    A return poll held meanwhile (it joins the running poll) keeps the poll held apart.
    """
    first, second, hall = fleet
    await clock.run_to(100)
    first.start()
    second.start()
    await clock.run_to(122)
    assert hall.coordinator._poll_waiting  # due at about 120 s
    give_back = await take_every_poll_place(hass)
    first.finish()
    second.finish()
    await clock.run(HOLDOFF + 1)
    assert not burst.active
    assert hall.coordinator._poll_running  # released, waiting for a place
    if return_poll:
        hall.coordinator._async_request_poll("return_poll", keep=True)

    first.start()  # the next burst
    second.start()
    give_back()
    await hass.async_block_till_done()
    assert [clock.since(m) for m in fleet] == [[0.0]] * 3
    assert hall.coordinator._first_poll_waiting is return_poll
    assert hall.coordinator._poll_waiting is not return_poll
    assert len(poll_timers(hass, hall)) == 1

    first.finish()
    second.finish()
    end = clock.now
    await clock.run(HOLDOFF + 1)
    for member in fleet:
        assert clock.since(member) == [0.0, near(end + HOLDOFF)]
        assert len(poll_timers(hass, member)) == 1
        assert holding_timers(hass, member) == []


async def test_a_first_poll_still_waiting_for_its_place_as_a_burst_starts_is_held_apart(
    hass: HomeAssistant, clock: Clock, mock_bluetooth: SimpleNamespace, burst: CommandBurst
) -> None:
    """A first poll that came due with no command running but waits for a poll place is held
    apart once it has one, if a burst started meanwhile: a policy step that stops polls
    doesn't drop it, and it runs once as the burst ends."""
    first = await add_member(hass, *FLEET[0])
    second = await add_member(hass, *FLEET[1])
    give_back = await take_every_poll_place(hass)
    hall = await add_member(hass, *FLEET[2], stagger=10)
    await clock.run_to(15)  # its first poll came due at 10 s
    assert hall.coordinator._poll_running
    first.start()
    second.start()
    give_back()
    await hass.async_block_till_done()
    assert hall.polls == []
    assert hall.coordinator._first_poll_waiting
    hall.coordinator._async_clear_poll_interval()  # as the policy does for a light turned off
    assert hall.coordinator._first_poll_waiting

    first.finish()
    second.finish()
    await clock.run_to(15 + HOLDOFF + 1)
    assert clock.since(hall) == [near(15 + HOLDOFF)]
    assert len(poll_timers(hass, hall)) == 1  # the poll set the fixed cadence again
    assert holding_timers(hass, hall) == []


# -- a poll that runs meanwhile, the one-off polls ---------------------------------------------


async def test_a_poll_running_as_the_burst_ends_is_the_held_first_poll(
    hass: HomeAssistant, clock: Clock, mock_bluetooth: SimpleNamespace, burst: CommandBurst
) -> None:
    """A refresh (update_entity) that starts while the burst holds the first poll reads the
    device as that poll would: the burst ending while it still runs doesn't release the first
    poll alongside it, nor does its end. (A held scheduled poll is gone as soon as a refresh
    starts: HA cancels its timer.)"""
    first = await add_member(hass, *FLEET[0])
    second = await add_member(hass, *FLEET[1])
    first.start()
    second.start()
    hall = await add_member(hass, *FLEET[2], stagger=10)
    await clock.run_to(15)
    assert hall.coordinator._first_poll_waiting
    read = hall.device.update.side_effect  # type: ignore[attr-defined]
    gate = asyncio.Event()

    async def _held(**kwargs: Any) -> Any:
        await gate.wait()
        return await read(**kwargs)

    hall.device.update.side_effect = _held  # type: ignore[attr-defined]
    # Not a Home Assistant task: async_block_till_done would wait for the held poll.
    refresh = asyncio.create_task(hall.coordinator.async_refresh())
    await until(lambda: hall.coordinator._poll_running)
    first.finish()
    second.finish()
    await fire(hass, timers(hass, "_async_end_burst")[0])
    assert not burst.active
    assert hall.coordinator._first_poll_waiting  # not released while the refresh runs
    gate.set()
    await refresh
    await hass.async_block_till_done()
    assert not hall.coordinator._first_poll_waiting
    assert holding_timers(hass, hall) == []
    await clock.run(BASE - 2 * HOLDOFF)  # before HA's next poll
    assert clock.since(hall) == [15.0]  # the refresh, and no second poll
    assert len(poll_timers(hass, hall)) == 1


async def test_a_return_poll_waits_for_the_burst(
    hass: HomeAssistant,
    clock: Clock,
    fleet: list[Member],
    burst: CommandBurst,
    presence: SimpleNamespace,
) -> None:
    """A device heard again after skipped polls is polled at once, but not during a burst: it
    waits, held apart like the first poll, and runs once as the burst ends."""
    first, second, hall = fleet
    presence.connectable = False
    await hall.coordinator.async_refresh()  # skipped: no scanner sees it
    assert len(hall.polls) == 1
    first.start()
    second.start()
    presence.connectable = True
    hall.coordinator.async_handle_advertisement(make_service_info(address=FLEET[2][0]))
    await hass.async_block_till_done()
    assert len(hall.polls) == 1
    assert hall.coordinator._first_poll_waiting
    await clock.run_to(10)
    first.finish()
    second.finish()
    await clock.run_to(10 + HOLDOFF + 1)
    assert clock.since(hall) == [0.0, near(10 + HOLDOFF)]
    assert len(poll_timers(hass, hall)) == 1


# -- unload and stop during a burst ------------------------------------------------------------


async def test_unloading_a_held_device_during_a_burst_drops_its_poll(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], burst: CommandBurst
) -> None:
    """An entry unloaded while the burst holds its poll leaves no timer and no poll behind;
    the burst still ends for the others."""
    first, second, hall = fleet
    await clock.run_to(100)
    first.start()
    second.start()
    await clock.run_to(121)
    coordinator = hall.coordinator
    assert coordinator._poll_waiting
    assert await hass.config_entries.async_unload(hall.entry.entry_id)
    assert not coordinator._poll_held
    assert timers(hass, "", coordinator) == []
    assert burst.active  # the other two still run their commands

    first.finish()
    second.finish()
    await clock.run(HOLDOFF + 1)
    assert not burst.active
    assert clock.since(hall) == [0.0]
    assert len(first.polls) == len(second.polls) == 2
    assert timers(hass, "", coordinator) == []


async def test_a_command_outliving_its_entry_still_ends_the_burst(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], burst: CommandBurst
) -> None:
    """HA doesn't cancel service calls on unload: a command still running on an unloaded entry
    counts until it ends, and then the burst's tail runs as usual (not out to the cap)."""
    first, second, hall = fleet
    await clock.run_to(100)
    first.start()
    second.start()
    await clock.run_to(121)
    coordinator = first.coordinator
    assert await hass.config_entries.async_unload(first.entry.entry_id)
    second.finish()
    assert burst.active  # the unloaded entry's command still runs
    await clock.run_to(130)
    coordinator.async_note_command_finished()
    assert burst._running == {}
    assert timers(hass, "", coordinator) == []  # no hold-off of its own once unloaded
    await clock.run(HOLDOFF + 1)
    assert not burst.active
    assert clock.since(hall) == [0.0, near(130 + HOLDOFF)]


@pytest.mark.parametrize("when", ["commands_running", "in_the_tail"])
async def test_unloading_every_entry_ends_the_burst(
    hass: HomeAssistant, fleet: list[Member], burst: CommandBurst, *, when: str
) -> None:
    """With no entry left there is nothing to hold: the burst ends as the last entry unloads,
    or, while commands still run (HA doesn't cancel them), as the last of them ends. No timer
    outlives the integration, even for a command that starts after the unload."""
    first, second, _hall = fleet
    first.start()
    second.start()
    if when == "in_the_tail":
        first.finish()
        second.finish()
        assert len(timers(hass, "_async_end_burst")) == 1
    coordinators = [member.coordinator for member in fleet]
    for member in fleet:
        assert await hass.config_entries.async_unload(member.entry.entry_id)
    assert timers(hass, "_async_end_burst") == []
    if when == "commands_running":
        assert burst.active  # an entry set up again before they end would wait for them
        # A command queued behind another to its device (PARALLEL_UPDATES) starts after the
        # unload, and the burst still ends with the last command, with no timer.
        coordinators[2].async_note_command_started()
        for coordinator in coordinators:
            coordinator.async_note_command_finished()
        assert burst._running == {}
    assert not burst.active
    assert timers(hass, "_async_end_burst") == []
    for coordinator in coordinators:
        assert timers(hass, "", coordinator) == []


async def test_unloading_every_entry_keeps_the_burst_while_a_turn_off_still_waits(
    hass: HomeAssistant, fleet: list[Member], burst: CommandBurst
) -> None:
    """A turn-off still waiting as the last entry unloads counts as a command still running
    does: the burst goes on (an entry set up again would wait for it), and ends as the turn-off
    stops waiting, with no timer."""
    first, second, _hall = fleet
    first.start()
    burst.async_note_deferred_started(second.address)
    first.finish()  # the turn-off still waits: no tail yet
    coordinators = [member.coordinator for member in fleet]
    for member in fleet:
        assert await hass.config_entries.async_unload(member.entry.entry_id)
    assert burst.active
    assert timers(hass, "_async_end_burst") == []

    burst.async_note_deferred_finished(second.address)  # dropped: its entity is removed
    assert not burst.active
    assert timers(hass, "_async_end_burst") == []
    for coordinator in coordinators:
        assert timers(hass, "", coordinator) == []


async def test_an_entry_set_up_again_waits_for_commands_still_running(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], burst: CommandBurst
) -> None:
    """Every entry reloads while two commands still run: the entry set up again holds its
    first poll until 25 s after they end, as it would have before the reload."""
    first, second, hall = fleet
    first.start()
    second.start()
    unloaded = [first.coordinator, second.coordinator]
    for member in fleet:
        assert await hass.config_entries.async_unload(member.entry.entry_id)
    hall.polls.clear()
    with (
        patch("custom_components.govee_ble_local.create_device", return_value=hall.device),
        patch("custom_components.govee_ble_local._first_poll_stagger", return_value=10),
    ):
        assert await hass.config_entries.async_setup(hall.entry.entry_id)
        await hass.async_block_till_done()
    await clock.run_to(15)  # its first poll came due at 10 s
    assert hall.coordinator._first_poll_waiting
    for coordinator in unloaded:
        coordinator.async_note_command_finished()
    await clock.run_to(15 + HOLDOFF - 1)
    assert hall.polls == []
    await clock.run(2)
    assert clock.since(hall) == [near(15 + HOLDOFF)]
    assert len(poll_timers(hass, hall)) == 1


async def test_unloading_one_entry_keeps_the_burst_going(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], burst: CommandBurst
) -> None:
    """Only the last entry to unload ends the burst: the others' polls stay held."""
    first, second, hall = fleet
    await clock.run_to(100)
    first.start()
    second.start()
    await clock.run_to(121)
    first.finish()
    second.finish()
    assert await hass.config_entries.async_unload(first.entry.entry_id)
    assert burst.active
    assert len(timers(hass, "_async_end_burst")) == 1
    await clock.run_to(121 + HOLDOFF - 1)
    assert clock.since(hall) == [0.0]
    await clock.run(2)
    assert clock.since(hall) == [0.0, near(121 + HOLDOFF)]


async def test_held_polls_dont_run_as_the_burst_ends_while_home_assistant_stops(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], burst: CommandBurst
) -> None:
    """Released, a held poll runs as HA's timer would: not once HA is stopping."""
    first, second, hall = fleet
    await clock.run_to(100)
    first.start()
    second.start()
    await clock.run_to(121)
    first.finish()
    second.finish()
    hass.set_state(CoreState.stopping)
    try:
        await fire(hass, timers(hass, "_async_end_burst")[0])
        assert not burst.active
        assert clock.since(hall) == [0.0]
    finally:
        hass.set_state(CoreState.running)


# What releases a held poll: the burst's tail, the device's own hold-off ending, or the cap.
RELEASERS = ["the_burst_tail", "its_own_hold_off_end", "the_cap"]


async def hold_the_halls_poll(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], releaser: str
) -> asyncio.TimerHandle:
    """Hold the hall's poll due at about 120 s, and return the timer of ``releaser`` due next.

    The burst's tail: commands to the two other devices end at 121 s. The hall's own hold-off
    end: a command to it alone ends at 121 s. The cap: that command still runs.
    """
    first, second, hall = fleet
    await clock.run_to(100)
    if releaser == "the_burst_tail":
        first.start()
        second.start()
        await clock.run_to(121)
        first.finish()
        second.finish()
        [handle] = timers(hass, "_async_end_burst")
    else:
        hall.start()
        await clock.run_to(121)
        if releaser == "its_own_hold_off_end":
            hall.finish()
            [handle] = timers(hass, "_async_end_holdoff", hall.coordinator)
        else:
            [handle] = timers(hass, "_async_holdoff_cap_reached", hall.coordinator)
    assert hall.coordinator._poll_waiting
    assert poll_timers(hass, hall) == []  # HA's timer fired, and the poll was held
    return handle


@pytest.mark.parametrize("releaser", RELEASERS)
@pytest.mark.parametrize("state", [CoreState.stopping, CoreState.not_running])
async def test_a_held_poll_released_once_home_assistant_has_stopped_doesnt_run(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], *, releaser: str, state: CoreState
) -> None:
    """Each releaser can still fire as HA stops, and in its close stage HA no longer refuses a
    scheduled poll, though the link is closed: the held poll is dropped, nothing reads the
    device, and no timer is armed for a next poll. Stopping: before the stop listener has run.
    The close stage (not_running again) comes after the stop event, which the listener notes;
    bootstrap's not_running, before any stop event, still runs the poll (below)."""
    hall = fleet[2]
    handle = await hold_the_halls_poll(hass, clock, fleet, releaser)
    try:
        hass.set_state(CoreState.stopping)
        if state is CoreState.not_running:
            hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)  # every entry's stop listener runs
            await hass.async_block_till_done()
            hass.set_state(state)
        await fire(hass, handle)
        assert hall.device.update.await_count == 1  # type: ignore[attr-defined]  # the setup poll
        assert clock.since(hall) == [0.0]
        assert poll_timers(hass, hall) == []
        assert not hall.coordinator._poll_held
        assert holding_timers(hass, hall) == []
    finally:
        hass.set_state(CoreState.running)


@pytest.mark.parametrize("releaser", RELEASERS)
@pytest.mark.parametrize("state", [CoreState.starting, CoreState.running])
async def test_a_held_poll_released_while_home_assistant_runs_still_runs(
    hass: HomeAssistant, clock: Clock, fleet: list[Member], *, releaser: str, state: CoreState
) -> None:
    """While HA starts or runs, each releaser runs the held poll at once, and that poll arms
    HA's timer for the next one: one timer."""
    hall = fleet[2]
    handle = await hold_the_halls_poll(hass, clock, fleet, releaser)
    hass.set_state(state)
    try:
        await fire(hass, handle)
        assert clock.since(hall) == [0.0, 121.0]
        assert len(poll_timers(hass, hall)) == 1
        assert not hall.coordinator._poll_held
        assert holding_timers(hass, hall) == []
    finally:
        hass.set_state(CoreState.running)


# HA sets config entries up before it starts (bootstrap), in CoreState.not_running, as its close
# stage is: is_running is False in both. Only the stop event tells them apart.


@pytest.mark.parametrize("releaser", RELEASERS)
async def test_a_held_poll_released_while_home_assistant_sets_up_still_runs(
    hass: HomeAssistant,
    clock: Clock,
    mock_bluetooth: SimpleNamespace,
    *,
    releaser: str,
) -> None:
    """Entries set up and polls held and released before HA has started (a slow bootstrap):
    no stop event came, so each releaser runs the held poll at once, and that poll arms HA's
    timer for the next one: one timer."""
    hass.set_state(CoreState.not_running)
    try:
        fleet = [await add_member(hass, address, title) for address, title in FLEET]
        hall = fleet[2]
        handle = await hold_the_halls_poll(hass, clock, fleet, releaser)
        assert hass.state is CoreState.not_running
        await fire(hass, handle)
        assert clock.since(hall) == [0.0, 121.0]
        assert len(poll_timers(hass, hall)) == 1
        assert not hall.coordinator._poll_held
        assert holding_timers(hass, hall) == []
    finally:
        hass.set_state(CoreState.running)


async def test_a_return_poll_runs_while_home_assistant_sets_up(
    hass: HomeAssistant, clock: Clock, mock_bluetooth: SimpleNamespace, presence: SimpleNamespace
) -> None:
    """A device whose staggered first poll is skipped as absent before HA has started, and
    heard again still before it starts, is polled at once (it read nothing yet), and that poll
    arms HA's timer: the advert cleared the flag that asked for it, so nothing would retry it."""
    hass.set_state(CoreState.not_running)
    try:
        presence.connectable = False
        hall = await add_member(hass, *FLEET[2], stagger=10)
        await clock.run_to(15)  # its first poll came due at 10 s: no scanner sees it
        assert hall.polls == []
        assert hall.coordinator._skipped_absent
        presence.connectable = True
        hall.coordinator.async_handle_advertisement(make_service_info(address=FLEET[2][0]))
        await hass.async_block_till_done()
        assert hass.state is CoreState.not_running
        assert clock.since(hall) == [15.0]
        assert not hall.coordinator._skipped_absent
        assert len(poll_timers(hass, hall)) == 1
        assert holding_timers(hass, hall) == []
    finally:
        hass.set_state(CoreState.running)


# -- what other code can ask the tracker --------------------------------------------------


async def test_other_devices_commanding_counts_only_the_other_devices(
    hass: HomeAssistant, fleet: list[Member], burst: CommandBurst
) -> None:
    """Commands to the device itself don't count, however many; any other device's do."""
    first, second, _hall = fleet
    me = first.coordinator._address
    assert first.coordinator.command_burst is burst
    assert not burst.other_devices_commanding(me)
    first.start()
    first.start()  # a zone and the main light
    assert not burst.other_devices_commanding(me)
    second.start()
    assert burst.other_devices_commanding(me)
    assert burst.other_devices_commanding(second.coordinator._address)
    second.finish()
    assert not burst.other_devices_commanding(me)
    first.finish()
    first.finish()


async def test_command_listeners_hear_every_start_and_end(
    hass: HomeAssistant, fleet: list[Member], burst: CommandBurst, caplog: pytest.LogCaptureFixture
) -> None:
    """Each start and end calls every listener once the counts and the burst are up to date; one
    that raises or unsubscribes itself doesn't cost the others their call, and unsubscribing
    stops the calls."""
    first, second, _hall = fleet
    me = first.coordinator._address
    heard: list[tuple[bool, bool]] = []

    def _broken() -> None:
        raise RuntimeError("listener bug")

    def _once() -> None:
        unsubscribe_once()

    burst.async_add_command_listener(_broken)
    unsubscribe_once = burst.async_add_command_listener(_once)
    unsubscribe = burst.async_add_command_listener(
        lambda: heard.append((burst.other_devices_commanding(me), burst.active))
    )
    second.start()
    first.start()
    first.finish()
    second.finish()
    assert heard == [(True, False), (True, True), (True, True), (False, True)]
    assert len(timers(hass, "_async_end_burst")) == 1  # the end still started the tail
    assert caplog.text.count("Unexpected error in command listener") == 4
    unsubscribe()
    second.start()
    second.finish()
    assert len(heard) == 4
