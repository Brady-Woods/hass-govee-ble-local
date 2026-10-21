"""Tests for releasing a command's BLE link unless a person alone used it.

The library keeps a command's link up for its 30 s warm window, for a person
adjusting again. COMMAND_RELEASE_DELAY_SECONDS after a device's last command
ends (its command window closes), the coordinator releases the link
(``Device.release``) when no command in that window came from a person, or
when another device's window was open during it (a burst). The devices are
H60A6 ceilings: a main light and two zone lights each. ``Device.release`` is
the library's own, on the fake transport (each release counts a disconnect).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from datetime import timedelta
import logging
from typing import Any
from unittest.mock import patch

from bleak.exc import BleakError
from govee_ble_local import Device, GoveeBleConnectionError
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockUser,
    async_fire_time_changed_exact,
)

from custom_components.govee_ble_local import connect_gate
from custom_components.govee_ble_local.const import CONF_SKU, DOMAIN
from custom_components.govee_ble_local.coordinator import (
    CommandBurst,
    GoveeBleLocalCoordinator,
    command_burst,
)

from .conftest import connection, make_device
from .const import ADDRESS

MAIN = "light.test_ceiling"
HALL_ADDRESS = "AA:BB:CC:DD:EE:02"
HALL = "light.hall"
# Short of the 0.5 s settle (COMMAND_RELEASE_DELAY_SECONDS), and past it: the test clock's
# float steps can't land exactly on it.
SHORT = 0.45
PAST = 0.55

pytestmark = pytest.mark.usefixtures("mock_bluetooth")


# -- helpers --------------------------------------------------------------------------------


async def run(hass: HomeAssistant, freezer: Any, seconds: float) -> None:
    """Let ``seconds`` pass on the wall and loop clocks, run what fell due, and settle."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed_exact(hass)
    await hass.async_block_till_done()


async def turn_on(hass: HomeAssistant, entity_id: str | list[str], context: Context) -> None:
    """Turn lights on with a service call made in ``context``."""
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id}, blocking=True, context=context
    )


def zone(hass: HomeAssistant, name: str) -> str:
    """The entity id of the test ceiling's zone light ``name``."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "light", DOMAIN, f"{ADDRESS}_zone_{name}_light"
    )
    assert entity_id is not None
    return entity_id


def releases(device: Device) -> int:
    """How often the integration released the device's link."""
    count: int = device.release.await_count  # type: ignore[attr-defined]
    return count


def coordinator_of(entry: MockConfigEntry) -> GoveeBleLocalCoordinator:
    """The entry's coordinator."""
    coordinator: GoveeBleLocalCoordinator = entry.runtime_data.coordinator
    return coordinator


def window_timers(hass: HomeAssistant, owner: object) -> list[asyncio.TimerHandle]:
    """The live timers that close ``owner``'s command window."""
    found = []
    for handle in hass.loop._scheduled:  # type: ignore[attr-defined]
        if handle.cancelled():
            continue
        for target in (handle._callback, *(getattr(arg, "target", None) for arg in handle._args)):
            if getattr(target, "__name__", "") == "_async_end_window" and (
                getattr(target, "__self__", None) is owner
            ):
                found.append(handle)
                break
    return found


def hold_turn_on(device: Device) -> asyncio.Event:
    """Make the device's turn_on wait for the returned event, then run as the library's."""
    gate = asyncio.Event()

    async def _held(*args: Any, **kwargs: Any) -> None:
        await gate.wait()
        await Device.turn_on(device, *args, **kwargs)

    device.turn_on.side_effect = _held  # type: ignore[attr-defined]
    return gate


async def until(condition: Callable[[], bool]) -> None:
    """Yield to the loop until ``condition`` holds (without waiting for HA's tasks)."""
    for _ in range(50):
        if condition():
            return
        await asyncio.sleep(0)
    pytest.fail("condition never held")


async def add_hall(hass: HomeAssistant) -> tuple[MockConfigEntry, Device]:
    """Set a second H60A6 ("Hall") up; return its entry and device."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hall",
        unique_id=HALL_ADDRESS,
        data={"address": HALL_ADDRESS, CONF_SKU: "H60A6"},
    )
    device = make_device(address=HALL_ADDRESS)
    entry.add_to_hass(hass)
    with patch("custom_components.govee_ble_local.create_device", return_value=device):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    return entry, device


@pytest.fixture
def device(entry: MockConfigEntry) -> Device:
    """The test ceiling's device."""
    device: Device = entry.runtime_data.device
    return device


@pytest.fixture
def gui(hass_admin_user: MockUser) -> Context:
    """The context of a command from the frontend: the logged-in user's."""
    return Context(user_id=hass_admin_user.id)


@pytest.fixture
def burst(hass: HomeAssistant) -> CommandBurst:
    """The domain-wide command tracker."""
    return command_burst(hass)


# -- who sent the command -------------------------------------------------------------------


async def test_a_voice_satellites_command_releases_the_link_after_the_settle(
    freezer: Any,
    hass: HomeAssistant,
    device: Device,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ESPHome or Wyoming satellite passes no user (``Context()``): the link is released
    COMMAND_RELEASE_DELAY_SECONDS after the command, not held for the library's 30 s."""
    caplog.set_level(logging.DEBUG, logger="custom_components.govee_ble_local.coordinator")
    await turn_on(hass, MAIN, Context())
    await run(hass, freezer, SHORT)
    assert releases(device) == 0
    await run(hass, freezer, PAST - SHORT)
    assert releases(device) == 1
    assert connection(device).disconnects == 1
    assert "releasing the link (no person's command)" in caplog.text
    await run(hass, freezer, 31)
    assert releases(device) == 1


async def test_a_command_from_the_frontend_keeps_the_link(
    freezer: Any, hass: HomeAssistant, device: Device, gui: Context
) -> None:
    """A logged-in user's command (a dashboard, a more-info dialog) leaves the link to the
    library's warm window: a second tap or a slider drag finds it up."""
    await turn_on(hass, MAIN, gui)
    await run(hass, freezer, PAST)
    await run(hass, freezer, 31)
    assert releases(device) == 0
    assert connection(device).disconnects == 0


async def test_a_cloud_voice_assistants_command_releases_the_link(
    freezer: Any, hass: HomeAssistant, device: Device
) -> None:
    """Alexa or Google Assistant through Home Assistant Cloud acts as a system user."""
    cloud = await hass.auth.async_create_system_user(
        "Home Assistant Cloud", group_ids=[GROUP_ID_ADMIN]
    )
    await turn_on(hass, MAIN, Context(user_id=cloud.id))
    await run(hass, freezer, PAST)
    assert releases(device) == 1


async def test_an_automation_a_person_triggered_releases_the_link(
    freezer: Any, hass: HomeAssistant, device: Device, gui: Context
) -> None:
    """An automation runs in a context of its own, with no user, even when a person's
    action triggered it: no follow-up adjustment is coming."""
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "triggers": [{"trigger": "event", "event_type": "test_lights_on"}],
                "actions": [{"action": "light.turn_on", "target": {"entity_id": MAIN}}],
            }
        },
    )
    hass.bus.async_fire("test_lights_on", context=gui)
    await hass.async_block_till_done()
    assert device.turn_on.await_count == 1  # type: ignore[attr-defined]
    await run(hass, freezer, PAST)
    assert releases(device) == 1


# -- one device's command window ------------------------------------------------------------


async def test_a_devices_lights_in_a_row_share_one_link(
    freezer: Any, hass: HomeAssistant, device: Device
) -> None:
    """The main light, then a zone light 0.3 s after it: the settle restarts with the second
    command, so the link is released once, after both, never between them."""
    await turn_on(hass, MAIN, Context())
    await run(hass, freezer, 0.3)
    await turn_on(hass, zone(hass, "main"), Context())
    await run(hass, freezer, SHORT)  # the first command's settle is over
    assert releases(device) == 0
    await run(hass, freezer, PAST - SHORT)
    assert releases(device) == 1


async def test_one_call_for_a_devices_lights_releases_once(
    freezer: Any, hass: HomeAssistant, device: Device
) -> None:
    """One call for the main and both zone lights runs them one after another (the light
    platform's PARALLEL_UPDATES): one link, one release."""
    await turn_on(hass, [MAIN, zone(hass, "main"), zone(hass, "background")], Context())
    await run(hass, freezer, PAST)
    assert releases(device) == 1
    await run(hass, freezer, 31)
    assert releases(device) == 1


async def test_a_command_in_the_settle_postpones_the_release(
    freezer: Any, hass: HomeAssistant, entry: MockConfigEntry, device: Device
) -> None:
    """A second command 0.3 s after the first ends: nothing is released at the first one's
    settle mark, and once 0.5 s after the second."""
    coordinator = coordinator_of(entry)
    await turn_on(hass, MAIN, Context())
    await run(hass, freezer, 0.3)
    gate = hold_turn_on(device)
    second = hass.async_create_task(turn_on(hass, MAIN, Context()))
    await until(lambda: coordinator.commands_running)
    assert coordinator._unsub_window_end is None  # the first command's settle is cancelled
    assert window_timers(hass, coordinator) == []
    gate.set()
    await second
    await run(hass, freezer, SHORT)
    assert releases(device) == 0
    await run(hass, freezer, PAST - SHORT)
    assert releases(device) == 1
    assert coordinator._unsub_window_end is None


@pytest.mark.parametrize("person_first", [True, False], ids=["person_first", "person_second"])
async def test_a_persons_command_anywhere_in_the_window_keeps_the_link(
    freezer: Any, hass: HomeAssistant, device: Device, gui: Context, person_first: bool
) -> None:
    """A voice command and a person's command to one device 0.3 s apart are one window,
    which a person used: the link stays up, whichever came first."""
    first, second = (gui, Context()) if person_first else (Context(), gui)
    await turn_on(hass, MAIN, first)
    await run(hass, freezer, 0.3)
    await turn_on(hass, MAIN, second)
    await run(hass, freezer, PAST)
    await run(hass, freezer, 31)
    assert releases(device) == 0


async def test_a_new_window_starts_without_the_last_ones_person(
    freezer: Any, hass: HomeAssistant, device: Device, gui: Context
) -> None:
    """A person's command, then once its window has closed (link kept), a voice command:
    that one's window had no person, so its link is released."""
    await turn_on(hass, MAIN, gui)
    await run(hass, freezer, PAST)
    assert releases(device) == 0
    await turn_on(hass, MAIN, Context())
    await run(hass, freezer, PAST)
    assert releases(device) == 1


async def test_a_failed_command_still_releases_the_link(
    freezer: Any, hass: HomeAssistant, device: Device
) -> None:
    """A command that fails part-way may have connected: the link is released all the same,
    and the command's error still reaches the caller."""
    connection(device).error = BleakError("link lost")
    with pytest.raises(HomeAssistantError):
        await turn_on(hass, MAIN, Context())
    await run(hass, freezer, PAST)
    assert releases(device) == 1


async def test_the_context_is_read_as_the_command_starts(
    freezer: Any, hass: HomeAssistant, entry: MockConfigEntry, device: Device, gui: Context
) -> None:
    """Home Assistant forgets an entity's context on a state write 5 s after the call: a
    person's command whose library call runs longer still counts as a person's."""
    gate = hold_turn_on(device)
    call = hass.async_create_task(turn_on(hass, MAIN, gui))
    await until(lambda: coordinator_of(entry).commands_running)
    freezer.tick(timedelta(seconds=6))
    entity = hass.data["light"].get_entity(MAIN)
    entity.async_write_ha_state()
    assert entity._context is None  # forgotten
    gate.set()
    await call
    await run(hass, freezer, PAST)
    await run(hass, freezer, 31)
    assert releases(device) == 0


async def test_an_unmatched_command_end_arms_one_settle(
    freezer: Any, hass: HomeAssistant, entry: MockConfigEntry, device: Device
) -> None:
    """A command end the coordinator never saw start, during a settle: still one timer, one
    release."""
    coordinator = coordinator_of(entry)
    coordinator.async_note_command_started()
    coordinator.async_note_command_finished()
    coordinator.async_note_command_finished()
    assert len(window_timers(hass, coordinator)) == 1
    await run(hass, freezer, PAST)
    assert releases(device) == 1


async def test_the_release_queues_at_the_device_at_once(
    hass: HomeAssistant, entry: MockConfigEntry, device: Device
) -> None:
    """The release starts as the window closes, not a loop step later: it takes its place at
    the device's locks before a command started after it."""
    coordinator = coordinator_of(entry)
    await turn_on(hass, MAIN, Context())
    (handle,) = window_timers(hass, coordinator)
    handle._run()  # type: ignore[attr-defined]
    handle.cancel()
    assert connection(device).disconnects == 1
    assert releases(device) == 1
    await hass.async_block_till_done()


async def test_a_dropped_link_no_longer_counts_for_a_poll(
    freezer: Any,
    hass: HomeAssistant,
    entry: MockConfigEntry,
    device: Device,
    presence: Any,
) -> None:
    """Once the release dropped the link, a poll doesn't count on it: a device that has
    stopped advertising is skipped as absent instead of spending its connect budget."""
    coordinator = coordinator_of(entry)
    await turn_on(hass, MAIN, Context())
    assert coordinator._async_link_held()
    await run(hass, freezer, PAST)
    assert releases(device) == 1
    assert not coordinator._async_link_held()
    presence.connectable = False
    polls = device.update.await_count  # type: ignore[attr-defined]
    await coordinator.async_refresh()
    assert device.update.await_count == polls  # type: ignore[attr-defined]
    assert not coordinator.last_update_success


async def test_a_link_the_library_kept_still_counts_for_a_poll(
    freezer: Any, hass: HomeAssistant, entry: MockConfigEntry, device: Device
) -> None:
    """A release the library declined (a newer write went out on the link) drops nothing:
    the link still counts as held."""

    async def _kept() -> bool:
        return False

    device.release.side_effect = _kept  # type: ignore[attr-defined]
    coordinator = coordinator_of(entry)
    await turn_on(hass, MAIN, Context())
    await run(hass, freezer, PAST)
    assert releases(device) == 1
    assert coordinator._async_link_held()


async def test_the_gate_counts_the_link_down_while_it_is_released(
    freezer: Any, hass: HomeAssistant, device: Device
) -> None:
    """From the release's start to its end the connect gate counts the link as being released:
    a radio still lists the old link while it's torn down (connect_gate.async_note_releasing)."""
    gate = connect_gate._gate(hass)
    teardown = asyncio.Event()

    async def _slow_release() -> bool:
        assert gate._releasing[ADDRESS] == 1
        await teardown.wait()
        return True

    device.release.side_effect = _slow_release  # type: ignore[attr-defined]
    await turn_on(hass, MAIN, Context())
    await run(hass, freezer, PAST)
    assert releases(device) == 1
    assert gate._releasing[ADDRESS] == 1
    teardown.set()
    await hass.async_block_till_done()
    assert gate._releasing[ADDRESS] == 0


def hold_poll(
    device: Device, *, error: Exception | None = None
) -> tuple[asyncio.Event, asyncio.Event]:
    """Make the device's poll read until the second event is set; the first is set as it reads.

    ``error``: the poll then fails with it.
    """
    reading, finish = asyncio.Event(), asyncio.Event()

    async def _held(**_kwargs: Any) -> Any:
        reading.set()
        await finish.wait()
        if error is not None:
            raise error
        device.state.optimistic = False
        return device.state

    device.update.side_effect = _held  # type: ignore[attr-defined]
    return reading, finish


async def test_a_release_waits_for_a_running_polls_reads(
    freezer: Any,
    hass: HomeAssistant,
    entry: MockConfigEntry,
    device: Device,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A window closing while a poll reads the device releases the link once the poll ends:
    released between its reads, the rest of the poll would have to connect again."""
    caplog.set_level(logging.DEBUG, logger="custom_components.govee_ble_local.coordinator")
    reading, finish = hold_poll(device)
    poll = asyncio.create_task(coordinator_of(entry).async_refresh())  # not an HA task: held
    await reading.wait()
    await turn_on(hass, MAIN, Context())  # an automation's command while the poll reads
    await run(hass, freezer, PAST)
    assert releases(device) == 0
    assert "releasing the link once the running poll ends (no person's command)" in caplog.text

    finish.set()
    await poll
    await hass.async_block_till_done()
    assert releases(device) == 1
    assert connection(device).disconnects == 1
    assert "the poll ended: releasing the link" in caplog.text


async def test_a_persons_command_during_the_poll_keeps_the_link(
    freezer: Any,
    hass: HomeAssistant,
    entry: MockConfigEntry,
    device: Device,
    *,
    gui: Context,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A release a poll put off belongs to the window before: a person's command in a new
    window decides again, and a person alone keeps the link."""
    caplog.set_level(logging.DEBUG, logger="custom_components.govee_ble_local.coordinator")
    reading, finish = hold_poll(device)
    poll = asyncio.create_task(coordinator_of(entry).async_refresh())
    await reading.wait()
    await turn_on(hass, MAIN, Context())
    await run(hass, freezer, PAST)  # put off: the poll reads
    await turn_on(hass, MAIN, gui)
    await run(hass, freezer, PAST)  # a person alone: kept

    finish.set()
    await poll
    await hass.async_block_till_done()
    assert releases(device) == 0
    assert "not releasing the link after the poll: a new command's window decides" in caplog.text
    assert "the poll ended: releasing the link" not in caplog.text


async def test_a_release_put_off_by_a_failing_poll_still_happens(
    freezer: Any, hass: HomeAssistant, entry: MockConfigEntry, device: Device
) -> None:
    """A poll that fails still ends its reading: the release it put off runs, and the next
    window isn't put off too."""
    coordinator = coordinator_of(entry)
    reading, finish = hold_poll(device, error=GoveeBleConnectionError("link lost"))
    poll = asyncio.create_task(coordinator.async_refresh())
    await reading.wait()
    await turn_on(hass, MAIN, Context())
    await run(hass, freezer, PAST)
    assert releases(device) == 0  # put off

    finish.set()
    await poll
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    assert not coordinator._poll_reading
    assert releases(device) == 1

    await turn_on(hass, MAIN, Context())  # no poll reads now: released after the settle
    await run(hass, freezer, PAST)
    assert releases(device) == 2


async def test_a_release_put_off_by_a_cancelled_poll_still_happens(
    freezer: Any, hass: HomeAssistant, entry: MockConfigEntry, device: Device
) -> None:
    """A poll cancelled mid-read (its refresh is cancelled) ends its reading the same way."""
    coordinator = coordinator_of(entry)
    reading, _finish = hold_poll(device)
    poll = asyncio.create_task(coordinator.async_refresh())
    await reading.wait()
    await turn_on(hass, MAIN, Context())
    await run(hass, freezer, PAST)
    assert releases(device) == 0

    poll.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await poll
    await hass.async_block_till_done()
    assert not coordinator._poll_reading
    assert releases(device) == 1


async def test_a_release_that_raises_is_logged_not_raised(
    freezer: Any, hass: HomeAssistant, device: Device, caplog: pytest.LogCaptureFixture
) -> None:
    """The library's release never raises; if it did, the error is a debug line, not an
    error from a background task."""
    caplog.set_level(logging.DEBUG, logger="custom_components.govee_ble_local.coordinator")
    device.release.side_effect = BleakError("proxy gone")  # type: ignore[attr-defined]
    await turn_on(hass, MAIN, Context())
    await run(hass, freezer, PAST)
    assert releases(device) == 1
    assert "releasing the link failed: proxy gone" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# -- several devices ------------------------------------------------------------------------


@pytest.mark.parametrize("service", ["turn_on", "turn_off"])
async def test_a_persons_command_to_several_devices_releases_every_link(
    freezer: Any,
    hass: HomeAssistant,
    device: Device,
    gui: Context,
    service: str,
) -> None:
    """One call from the frontend for two devices ("all lights off" on a dashboard) is a
    burst: each link holds one of the connection slots the other devices need."""
    _hall_entry, hall = await add_hall(hass)
    device.state.is_on = hall.state.is_on = True  # turned off as they are, not deferred
    await hass.services.async_call(
        "light", service, {"entity_id": [MAIN, HALL]}, blocking=True, context=gui
    )
    await run(hass, freezer, PAST)
    assert (releases(device), releases(hall)) == (1, 1)


async def test_commands_to_two_devices_in_a_row_are_a_burst(
    freezer: Any,
    hass: HomeAssistant,
    device: Device,
    gui: Context,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A script's steps from the frontend: the hall's command starts 0.2 s after the
    ceiling's ended, while the ceiling's window is still open. Both links are released."""
    caplog.set_level(logging.DEBUG, logger="custom_components.govee_ble_local.coordinator")
    _hall_entry, hall = await add_hall(hass)
    await turn_on(hass, MAIN, gui)
    await run(hass, freezer, 0.2)
    await turn_on(hass, HALL, gui)
    await run(hass, freezer, PAST)
    assert (releases(device), releases(hall)) == (1, 1)
    assert "releasing the link (commands to other devices too)" in caplog.text


async def test_a_command_after_another_devices_window_closed_is_no_burst(
    freezer: Any, hass: HomeAssistant, device: Device, gui: Context
) -> None:
    """The hall's command starts once the ceiling's window has closed: two separate person's
    commands, both links kept."""
    _hall_entry, hall = await add_hall(hass)
    await turn_on(hass, MAIN, gui)
    await run(hass, freezer, PAST)
    await turn_on(hass, HALL, gui)
    await run(hass, freezer, PAST)
    await run(hass, freezer, 31)
    assert (releases(device), releases(hall)) == (0, 0)


# -- unload ---------------------------------------------------------------------------------


async def test_unloading_during_the_settle_releases_nothing(
    freezer: Any, hass: HomeAssistant, entry: MockConfigEntry, device: Device
) -> None:
    """The entry unloads before the settle ends: the timer goes with it, and the link closes
    with the device (``device.stop``), not through a release."""
    coordinator = coordinator_of(entry)
    await turn_on(hass, MAIN, Context())
    await run(hass, freezer, 0.2)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert window_timers(hass, coordinator) == []
    await run(hass, freezer, 1)
    assert releases(device) == 0
    assert device.stop.await_count == 1  # type: ignore[attr-defined]


async def test_an_unloaded_devices_window_doesnt_make_a_burst(
    freezer: Any, hass: HomeAssistant, entry: MockConfigEntry, gui: Context, burst: CommandBurst
) -> None:
    """The ceiling unloads during its settle: its window closes, so the hall's person's
    command just after is no burst, and its link stays up."""
    _hall_entry, hall = await add_hall(hass)
    await turn_on(hass, MAIN, Context())
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert burst._windows == {}
    await turn_on(hass, HALL, gui)
    await run(hass, freezer, PAST)
    assert releases(hall) == 0


async def test_a_command_starting_after_shutdown_opens_no_window(
    hass: HomeAssistant, entry: MockConfigEntry, burst: CommandBurst
) -> None:
    """A command can still start once the coordinator has shut down (a call already queued
    for the entity): no settle would ever close its window, so it opens none."""
    coordinator = coordinator_of(entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator.async_note_command_started()
    assert burst._windows == {}
    coordinator.async_note_command_finished()
    assert window_timers(hass, coordinator) == []


# -- the tracker's windows ------------------------------------------------------------------


async def test_the_tracker_marks_every_open_window_once_two_are_open(burst: CommandBurst) -> None:
    """One device's window, opened again, is no burst; a second device's makes both one,
    and a window closed is gone (closing it again, or one never opened, says no burst)."""
    burst.async_note_window_open(ADDRESS)
    burst.async_note_window_open(ADDRESS)
    assert burst._windows == {ADDRESS: False}
    burst.async_note_window_open(HALL_ADDRESS)
    assert burst._windows == {ADDRESS: True, HALL_ADDRESS: True}
    assert burst.async_close_window(ADDRESS) is True
    assert burst.async_close_window(ADDRESS) is False
    burst.async_note_window_open(ADDRESS)  # the hall's is still open
    assert burst._windows == {HALL_ADDRESS: True, ADDRESS: True}
    assert burst.async_close_window(HALL_ADDRESS) is True
    assert burst.async_close_window(ADDRESS) is True
    assert burst._windows == {}
