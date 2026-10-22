"""Tests for the deferred turn-off of a device already off (deferral.py).

The integration-wide command tracker is reached only through deferral's
adapter; here :class:`CommandTracker` stands behind it, fed by every
coordinator's command start and end as the real one is. Each device is a real
entry with its own address. A command keeps running while its device's writes
are held (``hold``). Time moves on the wall, event-loop and Bluetooth clocks
together (``Clock``).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import timedelta
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from govee_ble_local import Device, GoveeBleConnectionError
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant
from homeassistant.helpers import entity_platform, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util, slugify
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.govee_ble_local import connect_gate, deferral
from custom_components.govee_ble_local.connect_gate import _GATE
from custom_components.govee_ble_local.const import (
    ADVERT_POWER_FRESH_SECONDS,
    CONF_SKU,
    DEFER_OFF_MAX_SECONDS,
    DOMAIN,
    POLL_HOLDOFF_AFTER_COMMAND_SECONDS,
)
from custom_components.govee_ble_local.coordinator import (
    GoveeBleLocalCoordinator,
    command_burst,
)
from custom_components.govee_ble_local.entity import GoveeBleLocalEntity
from custom_components.govee_ble_local.helpers import zone_confirmed_off

from .conftest import connection, make_device, make_service_info
from .const import GOVEE_COMPANY_ID, govee_mfg

PACKAGE = "custom_components.govee_ble_local"
DEFERRAL = f"{PACKAGE}.deferral"
FRESH = ADVERT_POWER_FRESH_SECONDS
CAP = DEFER_OFF_MAX_SECONDS
HOLDOFF = POLL_HOLDOFF_AFTER_COMMAND_SECONDS
# A device with a command running that no entry here sets up.
ELSEWHERE = "AA:BB:CC:DD:EE:99"


class CommandTracker:
    """Stands in for the integration-wide command tracker behind deferral's adapter."""

    def __init__(self) -> None:
        self.running: Counter[str] = Counter()
        self.listeners: list[CALLBACK_TYPE] = []

    def commanding_devices(self, _hass: HomeAssistant) -> set[str]:
        """The devices a command is running to (the adapter's first half)."""
        return set(+self.running)

    def track(self, _hass: HomeAssistant, action: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call ``action`` on every command start and end (the adapter's second half)."""
        self.listeners.append(action)
        return lambda: self.listeners.remove(action)

    def start(self, address: str) -> None:
        """A command to the device at ``address`` starts."""
        self.running[address] += 1
        self._notify()

    def finish(self, address: str) -> None:
        """A command to the device at ``address`` ends."""
        self.running[address] -= 1
        self._notify()

    def _notify(self) -> None:
        for action in list(self.listeners):
            action()


@pytest.fixture
def tracker() -> Generator[CommandTracker]:
    """Put a CommandTracker behind deferral's adapter, fed by every coordinator."""
    fake = CommandTracker()
    started = GoveeBleLocalCoordinator.async_note_command_started
    finished = GoveeBleLocalCoordinator.async_note_command_finished

    def _started(self: GoveeBleLocalCoordinator, *, person: bool = False) -> None:
        started(self, person=person)
        fake.start(self._address)

    def _finished(self: GoveeBleLocalCoordinator) -> None:
        finished(self)
        fake.finish(self._address)

    with (
        patch.object(GoveeBleLocalCoordinator, "async_note_command_started", _started),
        patch.object(GoveeBleLocalCoordinator, "async_note_command_finished", _finished),
        patch(f"{DEFERRAL}.commanding_devices", fake.commanding_devices),
        patch(f"{DEFERRAL}.async_track_command_changes", fake.track),
    ):
        yield fake
    assert not fake.listeners, "a wait left its tracker listener behind"


@dataclass
class Clock:
    """The wall, event-loop and Bluetooth clocks, moved together."""

    hass: HomeAssistant
    freezer: Any
    bluetooth: SimpleNamespace

    async def wait(self, seconds: float) -> None:
        """Let ``seconds`` pass, run the timers due, and let the commands get as far as they can."""
        self.freezer.tick(timedelta(seconds=seconds))
        self.bluetooth.now += seconds
        async_fire_time_changed(self.hass, dt_util.utcnow())
        await settle()


@pytest.fixture
def clock(hass: HomeAssistant, freezer: Any, monotonic: SimpleNamespace) -> Clock:
    """Move the clocks; an advertisement HA heard keeps its receive time."""
    return Clock(hass, freezer, monotonic)


@dataclass
class Rig:
    """One set-up device: its entry, its library Device and the entity a turn-off goes to."""

    entry: MockConfigEntry
    device: Device
    entity_id: str

    @property
    def coordinator(self) -> GoveeBleLocalCoordinator:
        """The entry's coordinator."""
        return self.entry.runtime_data.coordinator

    @property
    def sent(self) -> list[bytes]:
        """Every frame written to the device since setup."""
        return connection(self.device).sent


@pytest.fixture
def add_device(
    hass: HomeAssistant, mock_bluetooth: SimpleNamespace, clock: Clock
) -> Callable[..., Any]:
    """Set up a device (first poll done: read back, so not optimistic) with the given on/off."""
    count = 0

    async def _add(*, is_on: bool | None, sku: str = "H6008", read_back: bool = True) -> Rig:
        nonlocal count
        count += 1
        address = f"AA:BB:CC:DD:EE:{count:02X}"
        title = f"Device {count}"
        device = make_device(sku, address=address)
        if not read_back:
            device.update.side_effect = GoveeBleConnectionError("no slot")  # type: ignore[attr-defined]
        entry = MockConfigEntry(
            domain=DOMAIN,
            title=title,
            unique_id=address,
            data={"address": address, CONF_SKU: sku},
        )
        entry.add_to_hass(hass)
        with patch(f"{PACKAGE}.create_device", return_value=device):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done(wait_background_tasks=True)
        device.state.is_on = is_on
        platform = "switch" if device.profile.relay else "light"
        return Rig(entry, device, f"{platform}.{slugify(title)}")

    return _add


async def settle() -> None:
    """Let every running command get as far as it can (they aren't Home Assistant tasks)."""
    for _ in range(20):
        await asyncio.sleep(0)


def hold(rig: Rig) -> asyncio.Event:
    """Hold the device's writes until the returned event is set: its command keeps running."""
    gate = asyncio.Event()
    conn = connection(rig.device)
    send, send_many = conn.send, conn.send_many

    async def _send(frame: bytes, **kwargs: Any) -> bytes | None:
        await gate.wait()
        return await send(frame, **kwargs)

    async def _send_many(frames: list[bytes]) -> None:
        await gate.wait()
        await send_many(frames)

    conn.send = _send  # type: ignore[method-assign]
    conn.send_many = _send_many  # type: ignore[method-assign]
    return gate


def turn_off(hass: HomeAssistant, rig: Rig) -> asyncio.Task[Any]:
    """Start a turn-off service call for the rig's entity, as Assist does, one per entity.

    A plain task, not a Home Assistant one: async_block_till_done would wait for
    a call held open.
    """
    domain = rig.entity_id.partition(".")[0]
    return asyncio.create_task(
        hass.services.async_call(domain, "turn_off", {"entity_id": rig.entity_id}, blocking=True)
    )


def entity_of(hass: HomeAssistant, rig: Rig) -> GoveeBleLocalEntity:
    """Return the entity object a rig's turn-off goes to."""
    for platform in entity_platform.async_get_platforms(hass, DOMAIN):
        if (entity := platform.entities.get(rig.entity_id)) is not None:
            assert isinstance(entity, GoveeBleLocalEntity)
            return entity
    raise AssertionError(f"no entity {rig.entity_id}")


def parallel_updates(hass: HomeAssistant, rig: Rig) -> asyncio.Semaphore:
    """Return the PARALLEL_UPDATES semaphore HA takes for each call to the rig's entity."""
    slot = entity_of(hass, rig).parallel_updates
    assert slot is not None
    return slot


def waits(hass: HomeAssistant, rig: Rig) -> int:
    """Return how many turn-offs the rig's entity has waiting."""
    return len(entity_of(hass, rig)._off_deferral._waits)


def stop_listeners(hass: HomeAssistant) -> int:
    """Return how many listeners wait for Home Assistant's stop event."""
    return hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STOP, 0)


# -- the deferral -----------------------------------------------------------------


async def test_an_off_light_goes_out_after_the_other_devices(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """Two lights turning off and one already off: the off one goes out once both are done.

    Its call starts first, as the first of a burst can: it must still see the
    commands started with it. One of the others finishing isn't enough.
    """
    off = await add_device(is_on=False)
    first, second = await add_device(is_on=True), await add_device(is_on=True)
    first_gate, second_gate = hold(first), hold(second)
    stop_before = stop_listeners(hass)

    calls = [turn_off(hass, off), turn_off(hass, first), turn_off(hass, second)]
    await settle()
    assert off.sent == []
    assert stop_listeners(hass) == stop_before + 1
    assert waits(hass, off) == 1

    first_gate.set()
    await settle()
    assert first.sent
    assert off.sent == []  # the second still runs

    second_gate.set()
    await settle()
    assert second.sent
    assert off.sent  # sent, never skipped
    await asyncio.gather(*calls)
    assert off.device.state.is_on is False
    assert stop_listeners(hass) == stop_before
    assert waits(hass, off) == 0


async def test_a_failed_scanner_read_doesnt_fail_the_deferred_off(
    hass: HomeAssistant,
    add_device: Callable[..., Any],
    tracker: CommandTracker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the Bluetooth scanners can't be read, the deferral still waits and then sends.

    Its "is the link up" check reads them; a failure counts as no link (logged),
    as the gate itself lets its waiting calls through unjudged, instead of
    failing the service call (an automation's "all off" step would abort).
    """
    off, lit = await add_device(is_on=False), await add_device(is_on=True)
    lit_gate = hold(lit)
    with patch(
        "homeassistant.components.bluetooth.async_scanner_devices_by_address",
        side_effect=RuntimeError("no paths today"),
    ):
        calls = [turn_off(hass, off), turn_off(hass, lit)]
        await settle()
        assert off.sent == []  # waits for the lit light, as with readable scanners
        assert waits(hass, off) == 1

        lit_gate.set()
        await settle()
        await asyncio.gather(*calls)  # neither call raises
    assert lit.sent
    assert off.sent
    assert "couldn't read the Bluetooth scanners" in caplog.text


@pytest.mark.parametrize("target", ["device", "zone"])
async def test_a_deferred_off_goes_to_the_gate_last(
    hass: HomeAssistant,
    add_device: Callable[..., Any],
    tracker: CommandTracker,
    clock: Clock,
    target: str,
) -> None:
    """Once its wait ends, a deferred whole-device or zone turn-off is served last at the
    connect gate (after the other commands, still before polls), not as a plain command."""
    rig = await add_device(is_on=False, sku="H60A6")
    entity_id = rig.entity_id
    if target == "zone":
        entity_id = next(
            entry.entity_id
            for entry in er.async_entries_for_config_entry(er.async_get(hass), rig.entry.entry_id)
            if entry.unique_id.startswith(f"{rig.device.address}_zone_") and entry.domain == "light"
        )
    seen: list[tuple[str, bool]] = []
    real = connect_gate.async_connect_slot

    def _spy(hass: HomeAssistant, address: str, *, last: bool = False, **kwargs: Any) -> Any:
        seen.append((address, last))
        return real(hass, address, last=last, **kwargs)

    tracker.start(ELSEWHERE)  # another device's command keeps running: the wait runs to its cap
    with patch(f"{PACKAGE}.entity.async_connect_slot", _spy):
        call = asyncio.create_task(
            hass.services.async_call("light", "turn_off", {"entity_id": entity_id}, blocking=True)
        )
        await settle()
        assert rig.sent == []  # waiting
        await clock.wait(CAP)
        await call
    tracker.finish(ELSEWHERE)
    assert rig.sent
    assert seen == [(rig.device.address, True)]


def commanding(tracker: CommandTracker, *rigs: Rig) -> bool:
    """Return whether every rig's command has started and is still running."""
    return all(tracker.running[rig.device.address] for rig in rigs)


async def test_off_lights_dont_wait_for_each_other(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """A turn-off of a device already off doesn't count for the others: two off lights go
    out together once the on one is done, each while the other's send is still in flight."""
    off_a, off_b = await add_device(is_on=False), await add_device(is_on=False)
    on = await add_device(is_on=True)
    gate, gate_a, gate_b = hold(on), hold(off_a), hold(off_b)

    calls = [turn_off(hass, off_a), turn_off(hass, off_b), turn_off(hass, on)]
    await settle()
    assert commanding(tracker, on)
    assert not tracker.running[off_a.device.address]
    assert not tracker.running[off_b.device.address]
    gate.set()
    await settle()
    assert commanding(tracker, off_a, off_b)  # both under way, neither waiting
    gate_a.set()
    gate_b.set()
    await asyncio.gather(*calls)
    assert off_a.sent
    assert off_b.sent


async def test_a_device_counts_again_once_its_off_is_sent(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """A device stops counting as commanding only while its confirmed-off turn-off is sent:
    turned on since, its next turn-off holds the other off lights back like any light's."""
    was_off = await add_device(is_on=False)
    await turn_off(hass, was_off)
    assert was_off.sent
    was_off.device.state.is_on = True
    gate = hold(was_off)
    off = await add_device(is_on=False)
    calls = [turn_off(hass, off), turn_off(hass, was_off)]
    await settle()
    assert off.sent == []
    gate.set()
    await asyncio.gather(*calls)
    assert off.sent


async def test_with_every_light_already_off_all_go_out_at_once(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """Nothing to let through first: every turn-off starts at once, its sends still in flight."""
    rigs = [await add_device(is_on=False) for _ in range(3)]
    gates = [hold(rig) for rig in rigs]
    calls = [turn_off(hass, rig) for rig in rigs]
    await settle()
    assert commanding(tracker, *rigs)
    for gate in gates:
        gate.set()
    await asyncio.gather(*calls)
    assert all(rig.sent for rig in rigs)


async def test_one_call_for_several_lights(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """One light.turn_off for a list of lights (an area, a group's members) defers the off one."""
    off = await add_device(is_on=False)
    first, second = await add_device(is_on=True), await add_device(is_on=True)
    gates = [hold(first), hold(second)]
    call = asyncio.create_task(
        hass.services.async_call(
            "light",
            "turn_off",
            {"entity_id": [off.entity_id, first.entity_id, second.entity_id]},
            blocking=True,
        )
    )
    await settle()
    assert commanding(tracker, first, second)
    assert off.sent == []
    for gate in gates:
        gate.set()
    await call
    assert off.sent


async def test_homeassistant_turn_off_across_domains(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """homeassistant.turn_off calls each domain in a task of its own, and the plugs' call runs
    each plug in one more: the off light, called alone, still sees them start."""
    assert await async_setup_component(hass, "homeassistant", {})
    off = await add_device(is_on=False)
    plugs = [await add_device(is_on=True, sku="H5083") for _ in range(2)]
    gates = [hold(plug) for plug in plugs]
    call = asyncio.create_task(
        hass.services.async_call(
            "homeassistant",
            "turn_off",
            {"entity_id": [off.entity_id, *(plug.entity_id for plug in plugs)]},
            blocking=True,
        )
    )
    await settle()
    assert commanding(tracker, *plugs)
    assert off.sent == []
    for gate in gates:
        gate.set()
    await call
    assert off.sent


async def test_a_light_that_is_on_goes_out_at_once(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """Only a device already off waits: one that is on goes out while the others still run."""
    other = await add_device(is_on=True)
    gate = hold(other)
    on = await add_device(is_on=True)

    calls = [turn_off(hass, other), turn_off(hass, on)]
    await settle()
    assert on.sent
    assert other.sent == []
    gate.set()
    await asyncio.gather(*calls)


async def test_the_off_goes_out_even_if_the_light_came_on_meanwhile(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """The user asked for off: a light seen turning on during the wait is still turned off."""
    off = await add_device(is_on=False)
    on = await add_device(is_on=True)
    gate = hold(on)
    calls = [turn_off(hass, off), turn_off(hass, on)]
    await settle()
    assert off.sent == []
    off.device.state.is_on = True  # an advertisement said on, say
    gate.set()
    await settle()
    await asyncio.gather(*calls)
    assert off.sent
    assert off.device.state.is_on is False


async def test_a_fixtures_zone_offs_queue_behind_its_deferred_off(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """The zone lights share the fixture's PARALLEL_UPDATES slot: called after its deferred off,
    they wait for it and run after it (and may then find the zones confirmed off)."""
    ceiling = await add_device(is_on=False, sku="H60A6")
    zones = [
        entry.entity_id
        for entry in er.async_entries_for_config_entry(er.async_get(hass), ceiling.entry.entry_id)
        if entry.unique_id.startswith(f"{ceiling.device.address}_zone_") and entry.domain == "light"
    ]
    assert len(zones) == 2
    order: list[str] = []
    device = ceiling.device
    fixture_off, zone_power = device.turn_off, device.set_zone_power

    async def _fixture_off() -> None:
        order.append("fixture")
        await fixture_off()

    async def _zone_power(zone: str, on: bool) -> None:
        order.append(f"zone {zone}")
        await zone_power(zone, on)

    device.turn_off = _fixture_off  # type: ignore[method-assign]
    device.set_zone_power = _zone_power  # type: ignore[method-assign]
    other = await add_device(is_on=True)
    gate = hold(other)

    calls = [turn_off(hass, ceiling), turn_off(hass, other)]
    calls += [
        asyncio.create_task(
            hass.services.async_call("light", "turn_off", {"entity_id": zone}, blocking=True)
        )
        for zone in zones
    ]
    await settle()
    assert order == []  # the fixture off waits; the zone offs wait for its slot
    assert not any(call.done() for call in calls[2:])
    gate.set()
    await settle()
    await asyncio.gather(*calls)
    assert order[0] == "fixture"
    assert all(step.startswith("zone ") for step in order[1:])


async def test_the_wait_ends_at_the_cap_outside_the_command_timeout(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker, clock: Clock
) -> None:
    """A command elsewhere that doesn't end holds the off for DEFER_OFF_MAX_SECONDS, no longer.

    The command timeout doesn't count the wait: it bounds the device call after it.
    """
    off = await add_device(is_on=False)
    tracker.start(ELSEWHERE)
    with patch(f"{PACKAGE}.entity.COMMAND_TIMEOUT_SECONDS", 1):
        call = turn_off(hass, off)
        await settle()
        # async_fire_time_changed runs what is due up to 0.5 s later.
        await clock.wait(CAP - 1)
        assert off.sent == []
        assert not call.done()
        await clock.wait(1)
        assert off.sent
        await call  # no timeout raised
    tracker.finish(ELSEWHERE)


async def test_a_command_to_one_device_is_never_deferred(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """Alone, or with another command to the same device running: it goes out at once."""
    off = await add_device(is_on=False)
    await turn_off(hass, off)
    assert off.sent

    connection(off.device).sent.clear()
    off.device.state.is_on = False
    tracker.start(off.device.address)
    await turn_off(hass, off)
    assert off.sent
    tracker.finish(off.device.address)


async def test_a_plug_already_off_waits_too(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """The plug's power switch defers its turn-off like the fixture light."""
    plug = await add_device(is_on=False, sku="H5083")
    assert plug.entity_id.startswith("switch.")
    tracker.start(ELSEWHERE)
    call = turn_off(hass, plug)
    await settle()
    assert plug.sent == []
    tracker.finish(ELSEWHERE)
    await settle()
    assert plug.sent
    await call


@pytest.mark.parametrize("sku", ["H6008", "H5083"])
async def test_unload_during_the_wait_drops_the_off(
    hass: HomeAssistant,
    add_device: Callable[..., Any],
    tracker: CommandTracker,
    clock: Clock,
    sku: str,
) -> None:
    """The entity going away ends the wait at once; nothing is sent on the closing link."""
    off = await add_device(is_on=False, sku=sku)
    slot = parallel_updates(hass, off)
    tracker.start(ELSEWHERE)
    stop_before = stop_listeners(hass)
    call = turn_off(hass, off)
    await settle()
    assert stop_listeners(hass) == stop_before + 1
    assert slot.locked()  # the wait keeps the call's PARALLEL_UPDATES slot

    assert await hass.config_entries.async_unload(off.entry.entry_id)
    await settle()
    assert call.done()
    await call
    assert not slot.locked()  # given back as the call ends
    await clock.wait(CAP)
    assert off.sent == []
    # The wait's listener is gone, and so is the entry's own (_async_close_on_stop).
    assert stop_listeners(hass) == stop_before - 1
    tracker.finish(ELSEWHERE)


async def test_a_light_group_with_a_light_called_alone(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """homeassistant.turn_off for an off light and a light group of two on lights: the group's
    members start two layers of tasks later, and the off light still sees them."""
    assert await async_setup_component(hass, "homeassistant", {})
    # Before the devices: the light platform's config is read once, as it sets up.
    group = {"platform": "group", "name": "Group", "entities": ["light.device_2", "light.device_3"]}
    assert await async_setup_component(hass, "light", {"light": [group]})
    off = await add_device(is_on=False)
    members = [await add_device(is_on=True) for _ in range(2)]
    assert [member.entity_id for member in members] == group["entities"]
    await hass.async_block_till_done()
    assert hass.states.get("light.group") is not None
    gates = [hold(member) for member in members]
    call = asyncio.create_task(
        hass.services.async_call(
            "homeassistant",
            "turn_off",
            {"entity_id": [off.entity_id, "light.group"]},
            blocking=True,
        )
    )
    await settle()
    assert commanding(tracker, *members)
    assert off.sent == []
    for gate in gates:
        gate.set()
    await call
    assert off.sent


async def test_a_group_of_groups_with_a_light_called_alone(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """Assist's call per entity: an off light, and an "all lights" group of two room groups of
    on lights. Those lights start three layers of tasks deep; the off light still sees them."""
    rooms = [
        {"platform": "group", "name": "Room A", "entities": ["light.device_2", "light.device_3"]},
        {"platform": "group", "name": "Room B", "entities": ["light.device_4", "light.device_5"]},
    ]
    every = {"platform": "group", "name": "All", "entities": ["light.room_a", "light.room_b"]}
    assert await async_setup_component(hass, "light", {"light": [*rooms, every]})
    off = await add_device(is_on=False)
    lights = [await add_device(is_on=True) for _ in range(4)]
    await hass.async_block_till_done()
    assert hass.states.get("light.all") is not None
    gates = [hold(light) for light in lights]
    calls = [
        turn_off(hass, off),
        asyncio.create_task(
            hass.services.async_call("light", "turn_off", {"entity_id": "light.all"}, blocking=True)
        ),
    ]
    await settle()
    assert commanding(tracker, *lights)
    assert off.sent == []
    for gate in gates:
        gate.set()
    await asyncio.gather(*calls)
    assert off.sent


async def test_a_renamed_light_still_drops_its_off_on_unload(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """HA re-adds a renamed entity without building it again: its next removal still drops."""
    off = await add_device(is_on=False)
    er.async_get(hass).async_update_entity(off.entity_id, new_entity_id="light.renamed")
    await hass.async_block_till_done()
    off.entity_id = "light.renamed"
    tracker.start(ELSEWHERE)
    call = turn_off(hass, off)
    await settle()
    assert not call.done()
    assert await hass.config_entries.async_unload(off.entry.entry_id)
    await settle()
    assert call.done()
    await call
    assert off.sent == []
    tracker.finish(ELSEWHERE)


async def test_removal_before_the_wait_starts_drops_the_off(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """The entity removed while its turn-off still lets the others start: dropped all the same."""
    off = await add_device(is_on=False)
    deferral_ = entity_of(hass, off)._off_deferral
    call = turn_off(hass, off)
    await asyncio.sleep(0)  # the call has started, the wait hasn't
    deferral_.async_drop()
    await settle()
    await call
    assert off.sent == []


async def test_home_assistant_stopping_drops_the_waiting_off(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker, clock: Clock
) -> None:
    """HA's stop ends the wait at once (it would delay the stop otherwise), sending nothing."""
    off = await add_device(is_on=False)
    slot = parallel_updates(hass, off)
    tracker.start(ELSEWHERE)
    call = turn_off(hass, off)
    await settle()
    assert slot.locked()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await settle()
    assert call.done()
    await call
    assert not slot.locked()
    await clock.wait(CAP)
    assert off.sent == []
    tracker.finish(ELSEWHERE)


async def test_the_first_ending_wins(
    hass: HomeAssistant,
    add_device: Callable[..., Any],
    tracker: CommandTracker,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other devices finish just as the cap runs out, in one loop iteration: the cap
    ending second changes nothing (and raises nothing); the off goes out once."""
    off = await add_device(is_on=False)
    tracker.start(ELSEWHERE)
    call = turn_off(hass, off)
    await settle()
    tracker.finish(ELSEWHERE)
    clock.freezer.tick(timedelta(seconds=CAP))
    async_fire_time_changed(hass, dt_util.utcnow())
    await settle()
    await call
    assert len(off.sent) == 1
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


async def test_no_wait_once_home_assistant_is_stopping(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """A turn-off that starts while HA stops goes out as before: no stop event is left to end it."""
    off = await add_device(is_on=False)
    tracker.start(ELSEWHERE)
    hass.set_state(CoreState.stopping)
    try:
        await turn_off(hass, off)
        assert off.sent
    finally:
        hass.set_state(CoreState.running)
        tracker.finish(ELSEWHERE)


def _unknown(rig: Rig, clock: Clock) -> None:
    rig.device.state.is_on = None


def _optimistic(rig: Rig, clock: Clock) -> None:
    # As if no state read had answered since setup (this rig's first poll did): the library
    # keeps the state optimistic until one does, and govee-ble-local 1.6.0 never sets it back.
    rig.device.state.optimistic = True


def _stale(rig: Rig, clock: Clock) -> None:
    # Read back, and the advertisement heard, FRESH seconds ago.
    clock.bluetooth.now += FRESH


@pytest.mark.parametrize("make_unsure", [_unknown, _optimistic, _stale])
async def test_an_off_the_device_didnt_confirm_is_never_deferred(
    hass: HomeAssistant,
    add_device: Callable[..., Any],
    tracker: CommandTracker,
    clock: Clock,
    make_unsure: Callable[[Rig, Clock], None],
) -> None:
    """Unknown, never answered or stale state: the turn-off goes out at once, burst or not."""
    off = await add_device(is_on=False)
    make_unsure(off, clock)
    tracker.start(ELSEWHERE)
    await turn_off(hass, off)
    assert off.sent
    tracker.finish(ELSEWHERE)


async def test_the_adapter_reads_the_integration_wide_tracker(
    hass: HomeAssistant, add_device: Callable[..., Any]
) -> None:
    """With no stand-in behind the adapter, the real tracker drives the deferral.

    A command to one device makes it commanding and calls the listener, on its
    start and on its end; the off light waits for it, then goes out.
    """
    off = await add_device(is_on=False)
    on = await add_device(is_on=True)
    gate = hold(on)
    calls: list[None] = []
    stop = deferral.async_track_command_changes(hass, lambda: calls.append(None))

    call_on, call_off = turn_off(hass, on), turn_off(hass, off)
    await settle()
    assert deferral.commanding_devices(hass) == {on.device.address}
    assert calls == [None]  # the on light's command started
    assert off.sent == []
    assert waits(hass, off) == 1

    gate.set()
    await asyncio.gather(call_on, call_off)
    assert on.sent
    assert off.sent  # went out once the other device's command ended
    assert deferral.commanding_devices(hass) == set()
    assert len(calls) == 4  # each command's start and end
    stop()
    assert waits(hass, off) == 0


async def scheduled_poll(rig: Rig) -> None:
    """Run the rig's scheduled background poll now, as HA's poll timer does as it fires."""
    coordinator = rig.coordinator
    if coordinator._unsub_refresh is not None:
        coordinator._unsub_refresh()  # the timer firing now: it doesn't fire again
    await coordinator._handle_refresh_interval()


async def test_a_waiting_off_makes_a_burst_with_the_real_tracker(
    hass: HomeAssistant, add_device: Callable[..., Any], clock: Clock
) -> None:
    """One light turning off and one already off is a burst: while the off one waits, every
    device's polls wait (a bystander's scheduled poll reads nothing) and the connect gate
    counts it as a command to come. Once both are done, the burst's tail runs as usual."""
    off = await add_device(is_on=False)
    on = await add_device(is_on=True)
    bystander = await add_device(is_on=True)
    gate = hold(on)
    burst = command_burst(hass)
    reads = bystander.device.update.await_count  # type: ignore[attr-defined]

    calls = [turn_off(hass, on), turn_off(hass, off)]
    await settle()
    assert deferral.commanding_devices(hass) == {on.device.address}
    assert waits(hass, off) == 1
    assert burst.active
    assert hass.data[_GATE]._deferred_offs == 1
    assert bystander.coordinator.commands_holding_off
    await scheduled_poll(bystander)
    assert bystander.device.update.await_count == reads  # type: ignore[attr-defined]

    gate.set()
    await asyncio.gather(*calls)
    assert off.sent
    assert hass.data[_GATE]._deferred_offs == 0
    await clock.wait(HOLDOFF - 1)
    assert burst.active
    assert bystander.device.update.await_count == reads  # type: ignore[attr-defined]
    await clock.wait(1)
    assert not burst.active
    assert bystander.device.update.await_count == reads + 1  # type: ignore[attr-defined]


def turn_on(hass: HomeAssistant, rig: Rig) -> asyncio.Task[Any]:
    """Start a turn-on service call for the rig's light (a plain task, as ``turn_off``)."""
    return asyncio.create_task(
        hass.services.async_call("light", "turn_on", {"entity_id": rig.entity_id}, blocking=True)
    )


async def test_a_turn_on_of_the_same_light_sends_its_waiting_off_at_once(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """A turn-on queued behind a light's waiting turn-off (a motion automation's, say) ends
    the wait: the off goes out at once, then the turn-on, neither waiting for the other device."""
    off = await add_device(is_on=False)
    other = await add_device(is_on=True)
    gate = hold(other)
    calls = [turn_off(hass, off), turn_off(hass, other)]
    await settle()
    assert waits(hass, off) == 1
    assert off.sent == []

    call = turn_on(hass, off)
    await settle()
    assert call.done()
    await call
    assert commanding(tracker, other)  # the other device's command still runs
    off.device.turn_off.assert_awaited_once()  # type: ignore[attr-defined]
    off.device.turn_on.assert_awaited_once()  # type: ignore[attr-defined]
    assert off.device.state.is_on is True  # the turn-on came after the off
    assert waits(hass, off) == 0
    gate.set()
    await asyncio.gather(*calls)


async def test_a_call_to_another_device_leaves_the_off_waiting(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """Only a call to the same device ends the wait: a turn-on of a third light doesn't."""
    off = await add_device(is_on=False)
    other = await add_device(is_on=True)
    third = await add_device(is_on=False)
    gate = hold(other)
    calls = [turn_off(hass, off), turn_off(hass, other)]
    await settle()
    assert waits(hass, off) == 1

    await turn_on(hass, third)
    await settle()
    assert third.sent
    assert off.sent == []
    assert waits(hass, off) == 1
    gate.set()
    await asyncio.gather(*calls)
    assert off.sent


async def test_only_the_commands_running_as_the_off_starts_hold_it_back(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """The off waits for its burst: the commands running as it starts. One that starts later
    doesn't hold it back, to a device it doesn't wait for (another automation) or to one it
    did once that device's commands had ended (a slider's next step)."""
    off = await add_device(is_on=False)
    first, second, later = ELSEWHERE, "AA:BB:CC:DD:EE:98", "AA:BB:CC:DD:EE:97"
    tracker.start(first)
    tracker.start(second)
    call = turn_off(hass, off)
    await settle()
    assert waits(hass, off) == 1

    tracker.start(later)
    tracker.finish(first)
    tracker.start(first)
    await settle()
    assert off.sent == []  # the second device's command still runs
    tracker.finish(second)
    await settle()
    assert off.sent  # though the later commands still run
    await call
    tracker.finish(first)
    tracker.finish(later)


def zone_light(hass: HomeAssistant, rig: Rig, zone: str = "main") -> Rig:
    """Return the fixture's rig with its turn-off going to the light of ``zone`` instead."""
    unique_id = f"{rig.device.address}_zone_{zone}_light"
    entity_id = er.async_get(hass).async_get_entity_id("light", DOMAIN, unique_id)
    assert entity_id is not None
    return Rig(rig.entry, rig.device, entity_id)


async def test_a_zone_off_of_a_fixture_already_off_waits_for_the_other_devices(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """A fixture that said it is off is dark whatever its zones say: a zone light's turn-off
    lets the other device go first, then is sent (nothing confirmed the zone off meanwhile)."""
    ceiling = await add_device(is_on=False, sku="H60A6")
    zone = zone_light(hass, ceiling)
    other = await add_device(is_on=True)
    gate = hold(other)
    assert ceiling.coordinator.power_confirmed_off

    calls = [turn_off(hass, zone), turn_off(hass, other)]
    await settle()
    assert commanding(tracker, other)
    assert waits(hass, zone) == 1
    assert ceiling.sent == []
    gate.set()
    await settle()
    await asyncio.gather(*calls)
    ceiling.device.set_zone_power.assert_awaited_once_with("main", False)  # type: ignore[attr-defined]
    assert ceiling.sent


async def test_a_waiting_zone_off_is_skipped_once_the_fixture_went_off(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """The fixture's own turn-off lands while its zone light's turn-off waits, its ack
    confirming every zone off: once the other device is done, the zone-off isn't sent."""
    ceiling = await add_device(is_on=False, sku="H60A6")
    zone = zone_light(hass, ceiling)
    other = await add_device(is_on=True)
    gate = hold(other)

    calls = [turn_off(hass, zone), turn_off(hass, other)]
    await settle()
    assert waits(hass, zone) == 1
    assert ceiling.sent == []
    # Straight through the library: the zone's wait holds the light platform's slot.
    await ceiling.device.turn_off()
    fixture_off = list(ceiling.sent)
    assert zone_confirmed_off(ceiling.device, "main")
    gate.set()
    await settle()
    await asyncio.gather(*calls)
    ceiling.device.set_zone_power.assert_not_awaited()  # type: ignore[attr-defined]
    assert ceiling.sent == fixture_off


@pytest.mark.parametrize("make_unsure", [_unknown, _optimistic, _stale])
async def test_a_zone_off_of_a_fixture_not_confirmed_off_goes_out_at_once(
    hass: HomeAssistant,
    add_device: Callable[..., Any],
    tracker: CommandTracker,
    clock: Clock,
    make_unsure: Callable[[Rig, Clock], None],
) -> None:
    """Unknown, optimistic or stale fixture state: the zone-off goes out at once, burst or not."""
    ceiling = await add_device(is_on=False, sku="H60A6")
    make_unsure(ceiling, clock)
    zone = zone_light(hass, ceiling)
    tracker.start(ELSEWHERE)
    call = turn_off(hass, zone)
    await settle()
    assert zone.sent
    await call
    ceiling.device.set_zone_power.assert_awaited_once_with("main", False)  # type: ignore[attr-defined]
    tracker.finish(ELSEWHERE)


async def test_a_zone_lights_call_doesnt_send_the_fixtures_waiting_off_early(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """Only a call to the fixture light ends its whole-device turn-off's wait: the fixture's
    zone light, queued in the same "all off" after that turn-off started waiting, leaves it
    waiting."""
    ceiling = await add_device(is_on=False, sku="H60A6")
    zone = zone_light(hass, ceiling)
    other = await add_device(is_on=True)
    gate = hold(other)

    calls = [turn_off(hass, ceiling), turn_off(hass, other)]
    await settle()
    assert waits(hass, ceiling) == 1
    calls.append(turn_off(hass, zone))
    await settle()
    assert ceiling.sent == []  # still waiting for the other device
    assert waits(hass, ceiling) == 1
    gate.set()
    await settle()
    await asyncio.gather(*calls)
    assert ceiling.sent


async def test_a_fixture_turn_on_sends_its_zones_waiting_off_at_once(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """A zone light's waiting turn-off holds the light platform's slot the fixture light
    shares: a turn-on of the fixture queued meanwhile (a motion automation's, say) ends that
    wait, as any call to the device does (sent early, it costs one frame to a fixture
    already dark). The zone-off goes out at once, then the turn-on, neither waiting for the
    other device."""
    ceiling = await add_device(is_on=False, sku="H60A6")
    zone = zone_light(hass, ceiling)
    assert parallel_updates(hass, zone) is parallel_updates(hass, ceiling)
    other = await add_device(is_on=True)
    gate = hold(other)
    calls = [turn_off(hass, zone), turn_off(hass, other)]
    await settle()
    assert waits(hass, zone) == 1
    assert ceiling.sent == []

    call = turn_on(hass, ceiling)
    await settle()
    assert call.done()
    await call
    assert commanding(tracker, other)  # the other device's command still runs
    ceiling.device.set_zone_power.assert_awaited_once_with("main", False)  # type: ignore[attr-defined]
    ceiling.device.turn_on.assert_awaited_once()  # type: ignore[attr-defined]
    assert ceiling.device.zone_is_on("main") is True  # the turn-on came after the zone-off
    assert waits(hass, zone) == 0
    gate.set()
    await asyncio.gather(*calls)


async def test_a_fixture_off_queued_behind_its_zones_waiting_off_waits_one_cap(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker, clock: Clock
) -> None:
    """The fixture's turn-off queued behind its zone light's waiting one sends that one at
    once; then it waits for the other device itself (the zone's ack leaves the fixture
    confirmed off), up to DEFER_OFF_MAX_SECONDS from when it was queued: one cap, not the
    zone's and then its own."""
    ceiling = await add_device(is_on=False, sku="H60A6")
    zone = zone_light(hass, ceiling)
    tracker.start(ELSEWHERE)
    calls = [turn_off(hass, zone)]
    await settle()
    assert waits(hass, zone) == 1

    calls.append(turn_off(hass, ceiling))
    await settle()
    ceiling.device.set_zone_power.assert_awaited_once_with("main", False)  # type: ignore[attr-defined]
    assert ceiling.coordinator.power_confirmed_off
    assert waits(hass, ceiling) == 1
    await clock.wait(CAP - 1)
    ceiling.device.turn_off.assert_not_awaited()  # type: ignore[attr-defined]
    await clock.wait(1)
    ceiling.device.turn_off.assert_awaited_once()  # type: ignore[attr-defined]
    await asyncio.gather(*calls)
    tracker.finish(ELSEWHERE)


async def test_a_zone_off_leaves_the_fixtures_confirmed_off_standing(
    hass: HomeAssistant, add_device: Callable[..., Any], tracker: CommandTracker
) -> None:
    """A zone's turn-off can't switch the fixture on: its ack leaves the device's last power
    word as it was, so a fixture that said it is off still counts as off after it."""
    ceiling = await add_device(is_on=False, sku="H60A6")
    zone = zone_light(hass, ceiling)
    assert ceiling.coordinator.power_confirmed_off
    await turn_off(hass, zone)
    ceiling.device.set_zone_power.assert_awaited_once_with("main", False)  # type: ignore[attr-defined]
    assert ceiling.coordinator.power_confirmed_off


# -- a device whose link is up --------------------------------------------------------


class LinkRadio:
    """A local adapter that lists the devices whose link is up (``linked``) as holding a slot.

    It is the connect gate's only radio, and only for those devices: the others still have
    no path, as in the other tests. A listed device's call goes through the gate as linked,
    and its turn-off sees the link up.
    """

    source = name = "hci0"
    slots = 3

    def __init__(self) -> None:
        self.linked: set[str] = set()

    def get_allocations(self) -> SimpleNamespace:
        """Its slot count, free slots and the devices holding one, as Home Assistant reports."""
        return SimpleNamespace(
            slots=self.slots, free=self.slots - len(self.linked), allocated=sorted(self.linked)
        )

    def connections_in_progress(self) -> int:
        """No connect is under way: the listed links are up."""
        return 0

    def time_since_last_detection(self) -> float:
        """It hears advertisements."""
        return 0.0

    def connection_failures(self, _address: str) -> int:
        """No connect through it failed."""
        return 0

    def paths(self, _hass: HomeAssistant, address: str, connectable: bool = True) -> list[Any]:
        """The device's connectable paths, as ``bluetooth.async_scanner_devices_by_address``."""
        assert connectable
        if address not in self.linked:
            return []
        return [
            SimpleNamespace(
                scanner=self,
                advertisement=SimpleNamespace(rssi=-60),
                ble_device=SimpleNamespace(address=address),
            )
        ]

    def keep_up(self, rig: Rig) -> None:
        """List the rig's device once a frame to it lands: its link stays up after a command."""
        conn = connection(rig.device)
        send, send_many = conn.send, conn.send_many

        async def _send(frame: bytes, **kwargs: Any) -> bytes | None:
            result = await send(frame, **kwargs)
            self.linked.add(rig.device.address)
            return result

        async def _send_many(frames: list[bytes]) -> None:
            await send_many(frames)
            self.linked.add(rig.device.address)

        conn.send = _send  # type: ignore[method-assign]
        conn.send_many = _send_many  # type: ignore[method-assign]


@pytest.fixture
def link_radio(presence: SimpleNamespace) -> Generator[LinkRadio]:
    """Put a LinkRadio behind the connect gate's view of the Bluetooth radios."""
    radio = LinkRadio()
    with patch("homeassistant.components.bluetooth.async_scanner_devices_by_address", radio.paths):
        yield radio


async def test_an_off_whose_link_is_up_goes_out_at_once(
    hass: HomeAssistant,
    add_device: Callable[..., Any],
    tracker: CommandTracker,
    link_radio: LinkRadio,
) -> None:
    """A radio lists the device (a command to it just landed, and its link is still up): its
    turn-off doesn't wait while another device's command runs, as waiting would save no
    connection slot. Once the link is down, the next one waits again."""
    off = await add_device(is_on=False)
    link_radio.keep_up(off)
    await turn_off(hass, off)  # alone: at once, and its link stays up
    assert link_radio.linked == {off.device.address}
    assert off.coordinator.power_confirmed_off
    tracker.start(ELSEWHERE)

    call = turn_off(hass, off)
    await settle()
    assert call.done()
    await call
    assert off.device.turn_off.await_count == 2  # type: ignore[attr-defined]
    assert hass.data[_GATE]._deferred_offs == 0

    link_radio.linked.clear()  # the link dropped
    call = turn_off(hass, off)
    await settle()
    assert waits(hass, off) == 1
    tracker.finish(ELSEWHERE)
    await settle()
    await call
    assert off.device.turn_off.await_count == 3  # type: ignore[attr-defined]


async def test_a_fixture_off_behind_its_zones_landed_off_goes_out_at_once(
    hass: HomeAssistant,
    add_device: Callable[..., Any],
    tracker: CommandTracker,
    link_radio: LinkRadio,
) -> None:
    """The fixture's turn-off queued behind its zone light's waiting one sends that one at
    once; it lands and the link stays up, so the fixture's own goes out right after it while
    another device's command runs, rather than wait with the link sitting idle."""
    ceiling = await add_device(is_on=False, sku="H60A6")
    zone = zone_light(hass, ceiling)
    link_radio.keep_up(ceiling)
    tracker.start(ELSEWHERE)
    calls = [turn_off(hass, zone)]
    await settle()
    assert waits(hass, zone) == 1

    calls.append(turn_off(hass, ceiling))
    await settle()
    assert all(call.done() for call in calls)
    await asyncio.gather(*calls)
    ceiling.device.set_zone_power.assert_awaited_once_with("main", False)  # type: ignore[attr-defined]
    ceiling.device.turn_off.assert_awaited_once()  # type: ignore[attr-defined]
    tracker.finish(ELSEWHERE)


async def test_a_light_groups_repeated_off_doesnt_hold_up_a_turn_on(
    hass: HomeAssistant,
    add_device: Callable[..., Any],
    tracker: CommandTracker,
    link_radio: LinkRadio,
) -> None:
    """An "all off" calls a light already off directly and again through each light group
    over it (a group calls turn_off for its members): the repeats queue behind the first
    turn-off, which waits for the other device. A turn-on of the light then sends that first
    one at once; it lands and the link stays up, so each repeat goes out at once too, and the
    turn-on follows them, all while the other device's command still runs."""
    # Before the devices: the light platform's config is read once, as it sets up.
    hallway = {"platform": "group", "name": "Hallway", "entities": ["light.device_1"]}
    downstairs = {"platform": "group", "name": "Downstairs", "entities": ["light.hallway"]}
    assert await async_setup_component(hass, "light", {"light": [hallway, downstairs]})
    off = await add_device(is_on=False)
    other = await add_device(is_on=True)
    assert off.entity_id == hallway["entities"][0]
    await hass.async_block_till_done()
    assert hass.states.get("light.downstairs") is not None
    link_radio.keep_up(off)
    gate = hold(other)

    every = [off.entity_id, other.entity_id, "light.hallway", "light.downstairs"]
    call = asyncio.create_task(
        hass.services.async_call("light", "turn_off", {"entity_id": every}, blocking=True)
    )
    await settle()
    assert commanding(tracker, other)
    assert waits(hass, off) == 1
    assert off.sent == []

    turned_on = turn_on(hass, off)
    await settle()
    assert turned_on.done()
    await turned_on
    assert commanding(tracker, other)  # the other device's command still runs
    assert off.device.turn_off.await_count == 3  # type: ignore[attr-defined]  # once per layer
    off.device.turn_on.assert_awaited_once()  # type: ignore[attr-defined]
    assert off.device.state.is_on is True  # the turn-on came after every off
    gate.set()
    await call


# -- what counts as confirmed off -----------------------------------------------------


def advertise(presence: SimpleNamespace, at: float, mfg: dict[int, bytes]) -> None:
    """HA's newest advertisement (from any scanner): ``mfg``, received at ``at``."""
    presence.service_info = make_service_info(manufacturer_data=mfg, seen_at=at)


async def test_confirmed_off_needs_the_library_to_say_off(
    add_device: Callable[..., Any],
) -> None:
    """An unknown or an "on" on/off never counts, read back just now or not."""
    rig = await add_device(is_on=None)
    assert not rig.coordinator.power_confirmed_off
    rig.device.state.is_on = True
    assert not rig.coordinator.power_confirmed_off
    rig.device.state.is_on = False
    assert rig.coordinator.power_confirmed_off


async def test_a_read_back_confirms_off_for_a_while(
    add_device: Callable[..., Any], clock: Clock, presence: SimpleNamespace
) -> None:
    """A read-back counts for ADVERT_POWER_FRESH_SECONDS, once the state isn't optimistic."""
    rig = await add_device(is_on=False)
    presence.service_info = None  # no advertisement to go by
    clock.bluetooth.now += FRESH - 1
    assert rig.coordinator.power_confirmed_off
    clock.bluetooth.now += 1
    assert not rig.coordinator.power_confirmed_off

    await rig.coordinator.async_refresh()  # read back again: still off
    assert rig.coordinator.power_confirmed_off
    # The guard on its own (the library never sets optimistic again once a read answered):
    # an optimistic state's polls don't count.
    rig.device.state.optimistic = True
    assert not rig.coordinator.power_confirmed_off


async def test_a_landed_command_confirms_off(
    hass: HomeAssistant, add_device: Callable[..., Any], clock: Clock
) -> None:
    """An acked turn-off counts like a read-back: long after the last read, it confirms."""
    rig = await add_device(is_on=True)
    clock.bluetooth.now += FRESH  # the read-back at setup and the advertisement are stale
    await hass.services.async_call("light", "turn_off", {"entity_id": rig.entity_id}, blocking=True)
    assert rig.device.state.is_on is False
    assert rig.coordinator.power_confirmed_off


async def test_a_landed_command_other_than_the_off_leaves_power_unknown(
    add_device: Callable[..., Any], presence: SimpleNamespace, clock: Clock
) -> None:
    """A segment write can wake a fixture the library still holds off: once one lands after the
    read-back, only a newer word (the off's ack, say) confirms the off again."""
    rig = await add_device(is_on=False)
    presence.service_info = None
    assert rig.coordinator.power_confirmed_off
    rig.coordinator.async_handle_command_success()  # as the read-back, on a tie: newer
    assert not rig.coordinator.power_confirmed_off
    clock.bluetooth.now += 1
    rig.coordinator.async_handle_command_success(power_off=True)
    assert rig.coordinator.power_confirmed_off
    clock.bluetooth.now += 1
    await rig.coordinator.async_refresh()  # a newer read-back: the state again
    assert rig.coordinator.power_confirmed_off


async def test_a_segment_lights_turn_off_doesnt_confirm_the_fixture_off(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    add_device: Callable[..., Any],
    presence: SimpleNamespace,
    clock: Clock,
) -> None:
    """A segment light's turn-off is a write to black, not a turn-off of the fixture: it
    lands, and a fixture the library holds off is still not confirmed off by it."""
    rope = await add_device(is_on=False, sku="H61A8")
    presence.service_info = None  # no advertisement to go by
    clock.bluetooth.now += FRESH  # and the read-back at setup is stale
    assert not rope.coordinator.power_confirmed_off
    segment = er.async_get(hass).async_get_entity_id(
        "light", DOMAIN, f"{rope.device.address}_segment_0"
    )
    assert segment is not None

    await hass.services.async_call("light", "turn_off", {"entity_id": segment}, blocking=True)
    assert rope.sent
    assert rope.device.state.is_on is False
    assert not rope.coordinator.power_confirmed_off


async def test_the_newest_advertisement_decides(
    add_device: Callable[..., Any], clock: Clock, presence: SimpleNamespace
) -> None:
    """A fresh advertisement heard after the read-back decides, even over optimistic state;
    one heard before it, a stale one, or one without the on/off byte does not."""
    rig = await add_device(is_on=False)
    read_at = clock.bluetooth.now
    clock.bluetooth.now += 5

    advertise(presence, clock.bluetooth.now, govee_mfg(on=True))
    assert not rig.coordinator.power_confirmed_off  # it turned on since (not taken yet)
    advertise(presence, read_at - 1, govee_mfg(on=True))
    assert rig.coordinator.power_confirmed_off  # the read-back is newer

    # The guard on its own (the library never sets optimistic again once a read answered):
    # with the poll not counting, the advertisement still does.
    rig.device.state.optimistic = True
    advertise(presence, clock.bluetooth.now, govee_mfg(on=False))
    assert rig.coordinator.power_confirmed_off  # the device's own word
    clock.bluetooth.now += FRESH
    assert not rig.coordinator.power_confirmed_off  # it went stale

    rig.device.state.optimistic = False
    advertise(presence, clock.bluetooth.now, govee_mfg(on=None))
    assert not rig.coordinator.power_confirmed_off  # no byte, and the read-back is stale


async def test_an_advertisement_confirms_off_before_the_first_read(
    add_device: Callable[..., Any], clock: Clock, presence: SimpleNamespace
) -> None:
    """Never read back nor commanded yet (just after startup): a fresh advertised off counts."""
    rig = await add_device(is_on=False, read_back=False)
    assert rig.device.state.optimistic
    advertise(presence, clock.bluetooth.now, govee_mfg(on=False))
    assert rig.coordinator.power_confirmed_off
    advertise(presence, clock.bluetooth.now, govee_mfg(on=True))
    assert not rig.coordinator.power_confirmed_off


async def test_the_advertised_byte_is_decoded_with_the_profiles_mask(
    add_device: Callable[..., Any], clock: Clock, presence: SimpleNamespace
) -> None:
    """A plug reads its byte as a bitmask, as the library does: 3 is on, though not 1."""
    plug = await add_device(is_on=False, sku="H5083")
    clock.bluetooth.now += 1
    advertise(presence, clock.bluetooth.now, {GOVEE_COMPANY_ID: bytes([0xEC, 0, 1, 1, 3, 0])})
    assert not plug.coordinator.power_confirmed_off
    advertise(presence, clock.bluetooth.now, {GOVEE_COMPANY_ID: bytes([0xEC, 0, 1, 1, 2, 0])})
    assert plug.coordinator.power_confirmed_off
