"""Tests for the advertisement-driven background poll policy.

Time moves through :class:`Air`, which ticks the wall, event-loop and Bluetooth
clocks together and keeps HA's advertisement history as fresh as a device that
keeps advertising. Like a real device, a test's device advertises its new on/off
after HA commands it: the presence tick re-offers HA's newest advertisement to
the library, and one left showing the old state would read as an outside change.

The tests drive the integration through Home Assistant (light services,
``homeassistant.update_entity``, the Bluetooth callbacks HA registered, time) and
check what HA shows (entity states, the poll_interval sensor, the diagnostics) and
how often the library is asked to poll (``Device.update``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
import dataclasses
from datetime import timedelta
import itertools
import math
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from govee_ble_local import (
    Device,
    GoveeBleAuthError,
    GoveeBleConnectionError,
    GoveeBleTimeout,
    Zone,
)
import govee_ble_local.const as lib_const
from homeassistant.components.bluetooth import BluetoothChange, BluetoothServiceInfoBleak
from homeassistant.components.bluetooth.const import (
    CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockUser,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.govee_ble_local.const import (
    ADVERT_FLIP_POLL_DELAY_SECONDS,
    ADVERT_POWER_FRESH_SECONDS,
    CONF_SKU,
    DOMAIN,
    EXCLUDED_FROM_ADVERT_POLICY,
    LAST_SEEN_RESOLUTION_SECONDS,
    MAX_POLL_INTERVAL_SECONDS,
    ON_POLL_MAX_INTERVAL_SECONDS,
    POLL_CONNECT_TIMEOUT_SECONDS,
    POLL_HOLDOFF_AFTER_COMMAND_SECONDS,
    POLL_INTERVAL_SECONDS,
    SLOW_POLL_MAX_STEPS,
    SLOW_POLL_SECONDS,
    UNAVAILABLE_AFTER_FAILURES,
    UNAVAILABLE_AFTER_UNHEARD_SECONDS,
)
from custom_components.govee_ble_local.coordinator import (
    GoveeBleLocalCoordinator,
    PollPolicy,
    command_burst,
)

from .conftest import (
    SAME_AS_ANY_SCANNER,
    advance,
    advertisement_callback,
    connection,
    make_service_info,
    poll_reads_power,
    unavailable_callback,
)
from .const import ADDRESS, TITLE, govee_mfg, pact_mfg

COORDINATOR = "custom_components.govee_ble_local.coordinator"
BASE = POLL_INTERVAL_SECONDS
DELAY = ADVERT_FLIP_POLL_DELAY_SECONDS
HOLDOFF = POLL_HOLDOFF_AFTER_COMMAND_SECONDS
FRESH = ADVERT_POWER_FRESH_SECONDS
# The library ignores an advertised on/off for this long after HA's own command or read.
WINDOW = lib_const.ADVERT_SUPPRESS_SECONDS
MAIN_LIGHT = "light.test_ceiling"
PLUG = "switch.test_ceiling"
# HA arms a poll timer to the whole second plus a per-coordinator microsecond
# offset, so a poll can come up to a second either side of its interval.
SLACK = 2
UNPOLLED = {PollPolicy.OFF, PollPolicy.RELAY}
ON_BACKOFF = [BASE, BASE * 2, BASE * 4, ON_POLL_MAX_INTERVAL_SECONDS]
FAILURE_BACKOFF = [BASE, BASE * 2, BASE * 4, BASE * 8, MAX_POLL_INTERVAL_SECONDS]
# A light with bars: two on/off power zones (built here, not taken from a model's row).
BARS = (Zone("left", power_index=0), Zone("right", power_index=1))


@pytest.fixture
def device_sku() -> str:
    """A light the policy covers (it advertises its on/off byte in the advertisement)."""
    return "H6008"


@pytest.fixture
def device_zones() -> tuple[Zone, ...] | None:
    """Power zones to give the device's profile in place of its model's (None: its own)."""
    return None


@pytest.fixture
def device_readback() -> str | None:
    """The read-back to give the device's profile in place of its model's (None: its own)."""
    return None


@pytest.fixture
def mock_device(
    mock_device: Device, device_zones: tuple[Zone, ...] | None, device_readback: str | None
) -> Device:
    """The shared test device, its profile's zones or read-back replaced before setup."""
    if device_zones is not None:
        mock_device.profile = dataclasses.replace(mock_device.profile, zones=device_zones)
    if device_readback is not None:
        mock_device.profile = dataclasses.replace(mock_device.profile, readback=device_readback)
    return mock_device


@pytest.fixture(autouse=True)
def no_jitter() -> Generator[None]:
    """Deterministic poll intervals."""
    with patch(f"{COORDINATOR}.random.uniform", return_value=0):
        yield


class Air:
    """What Home Assistant hears from the device, on clocks the test moves together."""

    def __init__(
        self,
        hass: HomeAssistant,
        bluetooth_mocks: SimpleNamespace,
        presence: SimpleNamespace,
        clock: SimpleNamespace,
        freezer: Any,
    ) -> None:
        self.hass = hass
        self.bluetooth_mocks = bluetooth_mocks
        self.presence = presence
        self.clock = clock
        self.freezer = freezer
        self.manufacturer_data: dict[int, bytes] = presence.service_info.manufacturer_data

    def record(self) -> BluetoothServiceInfoBleak:
        """HA's history takes the device's current advertisement, received now."""
        info = make_service_info(manufacturer_data=self.manufacturer_data, seen_at=self.clock.now)
        self.presence.service_info = info
        return info

    def advertise(
        self, *, on: bool | None = None, manufacturer_data: dict[int, bytes] | None = None
    ) -> None:
        """The advertisement changes: HA records it and calls the integration back."""
        self.manufacturer_data = (
            govee_mfg(on=on) if manufacturer_data is None else manufacturer_data
        )
        advertisement_callback(self.bluetooth_mocks)(self.record(), BluetoothChange.ADVERTISEMENT)

    async def wait(self, seconds: float, *, heard: bool = True, settle: bool = True) -> None:
        """Let time pass and run what is due; a ``heard`` device keeps repeating its advertisement.

        HA records each repeat in its history but calls nothing back for it.
        Unless ``settle`` is False, it then waits for the work that started
        (polls included); otherwise it only starts what is due, so a poll a test
        holds open doesn't block it.
        """
        self.freezer.tick(timedelta(seconds=seconds))
        self.clock.now += seconds
        if heard:
            self.record()
        if not settle:
            async_fire_time_changed(self.hass, dt_util.utcnow())
            await asyncio.sleep(0)
            return
        await advance(self.hass, 0)
        await self.hass.async_block_till_done(wait_background_tasks=True)


@pytest.fixture
def air(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
    freezer: Any,
) -> Air:
    """Drive the device's advertisements and the clocks."""
    return Air(hass, mock_bluetooth, presence, monotonic, freezer)


@pytest.fixture
def entry(air: Air, setup_integration: MockConfigEntry) -> MockConfigEntry:
    """The loaded entry (first poll done), set up on the clocks the test moves."""
    return setup_integration


@pytest.fixture
def coordinator(entry: MockConfigEntry) -> GoveeBleLocalCoordinator:
    """The loaded entry's coordinator (first poll done); the test moves the clocks."""
    return entry.runtime_data.coordinator


def polls(device: Device) -> int:
    """Return how many polls have run."""
    return int(device.update.await_count)  # type: ignore[attr-defined]


def nominal(coordinator: GoveeBleLocalCoordinator) -> float | None:
    """Return the nominal poll interval in seconds (None while there are no background polls)."""
    interval = coordinator.nominal_interval
    return None if interval is None else interval.total_seconds()


def sensor_id(hass: HomeAssistant, key: str) -> str:
    """Return the entity id of the diagnostic sensor with this key."""
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_{key}")
    assert entity_id is not None
    return entity_id


def poll_interval(hass: HomeAssistant) -> str:
    """Return the poll_interval sensor's state: seconds between background polls, or unknown.

    The sensor is disabled by default: the test needs ``entity_registry_enabled_by_default``.
    """
    state = hass.states.get(sensor_id(hass, "poll_interval"))
    assert state is not None
    return state.state


async def update_entity(hass: HomeAssistant) -> None:
    """Refresh the light now through HA (``homeassistant.update_entity``), as a user does."""
    assert await async_setup_component(hass, "homeassistant", {})
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": MAIN_LIGHT}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def coordinator_diagnostics(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, entry: MockConfigEntry
) -> dict[str, Any]:
    """Return the coordinator part of the entry's downloadable diagnostics."""
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    return diagnostics["coordinator"]


async def test_a_held_first_poll_still_runs_when_polls_stop(
    hass: HomeAssistant,
    air: Air,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
) -> None:
    """A command holds the first poll; a turn-off then stops background polls (the light is
    off): the first poll must still run once, or the device info would never be read."""
    with patch("custom_components.govee_ble_local._first_poll_stagger", return_value=30):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=False)
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    for _ in range(25):
        await air.wait(1, settle=False)
    await light_service(hass, "turn_on", brightness=50)
    for _ in range(10):  # the stagger ends inside the hold-off
        await air.wait(1, settle=False)
    await light_service(hass, "turn_off")
    air.advertise(on=False)
    for _ in range(60):
        await air.wait(30, settle=False)
    await hass.async_block_till_done()
    assert polls(mock_device) == 1


async def turned_on_outside_ha(air: Air) -> None:
    """The light, off, is turned on outside HA; its turn-on poll runs."""
    air.advertise(on=False)
    air.advertise(on=True)
    await air.wait(DELAY + SLACK)


async def light_service(
    hass: HomeAssistant, service: str, *, context: Context | None = None, **data: Any
) -> None:
    """Call a light service on the main light (with no user, as an automation does)."""
    await hass.services.async_call(
        "light", service, {"entity_id": MAIN_LIGHT, **data}, blocking=True, context=context
    )


async def power_service(hass: HomeAssistant, sku: str, service: str) -> None:
    """Turn the light or the plug on or off from HA."""
    if sku == "H5083":
        await hass.services.async_call("switch", service, {"entity_id": PLUG}, blocking=True)
    else:
        await light_service(hass, service)


# -- the decision ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("device_sku", "history", "age", "is_on", "expected"),
    [
        pytest.param("H6008", "byte", 0, False, PollPolicy.OFF, id="light_off"),
        pytest.param("H6008", "byte", 0, True, PollPolicy.ON, id="light_on"),
        pytest.param("H6008", "byte", 0, None, PollPolicy.FIXED, id="light_unknown"),
        pytest.param("H6008", "byte", FRESH - 1, False, PollPolicy.OFF, id="nearly_stale"),
        pytest.param("H6008", "byte", FRESH, False, PollPolicy.FIXED, id="stale_off"),
        pytest.param("H6008", "byte", FRESH, True, PollPolicy.FIXED, id="stale_on"),
        pytest.param("H6008", "short", 0, False, PollPolicy.FIXED, id="no_byte"),
        pytest.param("H6008", "no_mfg", 0, False, PollPolicy.FIXED, id="no_mfg"),
        pytest.param("H6008", "none", 0, False, PollPolicy.FIXED, id="no_history"),
        pytest.param("H5083", "byte", 0, False, PollPolicy.RELAY, id="relay_off"),
        pytest.param("H5083", "byte", 0, True, PollPolicy.RELAY, id="relay_on"),
        pytest.param("H5083", "byte", FRESH, True, PollPolicy.FIXED, id="relay_stale"),
        pytest.param("H60A6", "byte", 0, False, PollPolicy.FIXED, id="excluded_off"),
        pytest.param("H60A6", "byte", 0, True, PollPolicy.FIXED, id="excluded_on"),
    ],
)
async def test_policy_matrix(
    monotonic: SimpleNamespace,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    presence: SimpleNamespace,
    *,
    history: str,
    age: float,
    is_on: bool | None,
    expected: PollPolicy,
) -> None:
    """Background polls stop only for a covered device whose fresh on/off byte HA hears."""
    mock_device.state.is_on = is_on
    mfg = {"byte": govee_mfg(on=True), "short": govee_mfg(on=None), "no_mfg": {}}
    presence.service_info = (
        None
        if history == "none"
        else make_service_info(manufacturer_data=mfg[history], seen_at=monotonic.now - age)
    )
    coordinator.async_refresh_presence()

    assert coordinator.poll_policy is expected
    unpolled = expected in UNPOLLED
    assert coordinator.polls_scheduled is not unpolled
    assert (coordinator.update_interval is None) is unpolled
    assert (coordinator.nominal_interval is None) is unpolled


def test_policy_constants() -> None:
    """The chosen cadence: a 5 s turn-on poll, a 15 min on-backoff cap, a 180 s fresh window."""
    assert (DELAY, ON_POLL_MAX_INTERVAL_SECONDS, FRESH) == (5, 900, 180)
    assert ON_BACKOFF == [120, 240, 480, 900]


def test_fresh_window_is_inside_home_assistants_stale_advert_fallback() -> None:
    """A byte is trusted only while HA still treats the device's advertisements as current."""
    assert FRESH < CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS


def test_excluded_models() -> None:
    """H60A6 and H60A1 (music/group mode may end on a disconnect) keep the fixed cadence."""
    assert {"H60A1", "H60A6"} <= EXCLUDED_FROM_ADVERT_POLICY


# -- off, and turned on outside HA ------------------------------------------------


async def test_light_off_gets_no_background_polls(
    air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """While its advertisements say off, a light isn't connected to at all."""
    air.advertise(on=False)
    before = polls(mock_device)
    for _ in range(6):
        await air.wait(MAX_POLL_INTERVAL_SECONDS)
    assert polls(mock_device) == before


async def test_turn_on_outside_ha_polls_once_after_the_delay(
    air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """One poll ADVERT_FLIP_POLL_DELAY_SECONDS after the advertisement shows it on."""
    air.advertise(on=False)
    before = polls(mock_device)
    air.advertise(on=True)

    await air.wait(DELAY - 1)
    assert polls(mock_device) == before
    await air.wait(1)
    assert polls(mock_device) == before + 1

    await air.wait(BASE - SLACK)  # one poll, not a stream; the next comes at the base cadence
    assert polls(mock_device) == before + 1
    await air.wait(2 * SLACK)
    assert polls(mock_device) == before + 2


async def test_a_newer_flip_replaces_the_pending_turn_on_poll(
    air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """Off again cancels it; on again waits the full delay from the newest flip."""
    air.advertise(on=False)
    before = polls(mock_device)
    air.advertise(on=True)
    await air.wait(DELAY - 2)
    air.advertise(on=False)
    await air.wait(60)
    assert polls(mock_device) == before

    air.advertise(on=True)
    await air.wait(DELAY - 2)
    air.advertise(on=False)
    air.advertise(on=True)
    await air.wait(DELAY - 1)  # past the replaced poll's time
    assert polls(mock_device) == before
    await air.wait(1)
    assert polls(mock_device) == before + 1


async def turned_on_outside_ha_then_commanded(
    hass: HomeAssistant, air: Air, *, others: bool = False
) -> None:
    """The light, off, is turned on outside HA, then commanded from HA 2 s later: before its
    turn-on poll is due. With ``others``, two other lights are commanded instead, at once (a
    light group): a burst, which holds every device's polls as a command holds its own."""
    air.advertise(on=False)
    air.advertise(on=True)
    await air.wait(2)
    if not others:
        await light_service(hass, "turn_on", brightness=50)
        return
    burst = command_burst(hass)
    group = ("AA:BB:CC:DD:EE:0A", "AA:BB:CC:DD:EE:0B")
    for address in group:
        burst.async_note_command_started(address)
    for address in group:
        burst.async_note_command_finished(address)


@pytest.mark.parametrize("holder", ["its_own_command", "a_burst"])
async def test_turn_on_poll_waits_for_the_devices_own_hold_off(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device, *, holder: str
) -> None:
    """A light turned on outside HA, then commanded from HA (or two other lights at once, a
    burst) before its turn-on poll is due, is polled once that hold-off is over, like any
    background poll: whether the poll waits is decided when it comes due."""
    before = polls(mock_device)
    await turned_on_outside_ha_then_commanded(hass, air, others=holder == "a_burst")
    await air.wait(DELAY - 2)
    assert polls(mock_device) == before
    await air.wait(HOLDOFF - DELAY - SLACK)
    assert polls(mock_device) == before
    await air.wait(2 * SLACK)
    assert polls(mock_device) == before + 1


async def test_a_held_turn_on_poll_is_dropped_when_the_light_is_turned_off(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A turn-on poll waiting for a command's hold-off is dropped when the light is turned off
    outside HA meanwhile: no connection to a light that is off."""
    before = polls(mock_device)
    await turned_on_outside_ha_then_commanded(hass, air)
    await air.wait(WINDOW + 1)  # due meanwhile, and held; the library takes adverts again
    air.advertise(on=False)  # turned off outside HA
    assert hass.states.get(MAIN_LIGHT).state == "off"
    await air.wait(HOLDOFF + SLACK)
    await air.wait(3 * BASE)
    assert polls(mock_device) == before


async def test_a_return_poll_held_by_a_burst_runs_although_the_light_came_back_off(
    hass: HomeAssistant,
    air: Air,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    presence: SimpleNamespace,
) -> None:
    """The advertisement that brings a light back may show it off, which stops its background
    polls in the same callback: the held return poll still runs once as the burst ends, and
    starts no background polls."""
    burst = command_burst(hass)
    others = ("AA:BB:CC:DD:EE:0A", "AA:BB:CC:DD:EE:0B")
    air.advertise(on=True)
    presence.connectable = False
    await coordinator.async_refresh()  # skipped: no connectable scanner sees it
    for address in others:
        burst.async_note_command_started(address)
    presence.connectable = True
    before = polls(mock_device)
    air.advertise(on=False)  # back in reach, and off
    assert polls(mock_device) == before
    await air.wait(5)
    for address in others:
        burst.async_note_command_finished(address)
    await air.wait(HOLDOFF - SLACK)
    assert polls(mock_device) == before
    await air.wait(2 * SLACK)
    assert polls(mock_device) == before + 1
    await air.wait(3 * BASE)
    assert polls(mock_device) == before + 1


async def test_turn_on_poll_due_while_a_poll_runs_is_that_poll(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A poll running as the turn-on poll comes due (a refresh asked for through HA) reads the
    device as it would: no second poll queues behind it, as HA's own timer firing then
    wouldn't add one."""
    assert await async_setup_component(hass, "homeassistant", {})
    air.advertise(on=False)
    air.advertise(on=True)
    before = polls(mock_device)
    entered, release = asyncio.Event(), asyncio.Event()
    read = mock_device.update.side_effect  # type: ignore[attr-defined]

    async def _held(**kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await read(**kwargs)

    mock_device.update.side_effect = _held  # type: ignore[attr-defined]
    refresh = hass.async_create_task(
        hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": MAIN_LIGHT}, blocking=True
        )
    )
    await entered.wait()
    await air.wait(DELAY, settle=False)  # the turn-on poll comes due while it reads
    release.set()
    await refresh
    await air.wait(BASE - 2 * SLACK)
    assert polls(mock_device) == before + 1


async def test_first_on_off_from_unknown_is_not_a_turn_on(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """The history HA replays at setup sets on/off from unknown: no extra poll for it."""
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNKNOWN
    before = polls(mock_device)
    air.advertise(on=True)
    assert hass.states.get(MAIN_LIGHT).state == "on"
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before


@pytest.mark.parametrize("lag", [2, math.ceil(WINDOW) - 1], ids=["early", "just_inside_the_window"])
async def test_lagging_advert_after_a_command_is_not_a_turn_on(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device, *, lag: int
) -> None:
    """A stale report received any time inside the library's window after HA's own command is
    ignored: by the callback, and by the presence ticks after the window."""
    air.advertise(on=True)
    await air.wait(LAST_SEEN_RESOLUTION_SECONDS)  # well after the last poll
    await light_service(hass, "turn_off")
    assert hass.states.get(MAIN_LIGHT).state == "off"
    before = polls(mock_device)

    await air.wait(lag, heard=False)
    air.advertise(on=False)
    air.advertise(on=True)  # a late report of the state from before the command
    assert hass.states.get(MAIN_LIGHT).state == "off"
    # The link stays up after a command, so the device itself is silent; a presence tick
    # past the window finds that report still the newest.
    await air.wait(LAST_SEEN_RESOLUTION_SECONDS + math.ceil(WINDOW), heard=False)
    assert hass.states.get(MAIN_LIGHT).state == "off"

    air.advertise(on=False)  # the link closes: it advertises what it is
    for _ in range(3):
        await air.wait(LAST_SEEN_RESOLUTION_SECONDS)
    assert hass.states.get(MAIN_LIGHT).state == "off"
    assert polls(mock_device) == before

    air.advertise(on=True)  # long after the command: a real turn-on
    assert hass.states.get(MAIN_LIGHT).state == "on"
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before + 1


async def test_history_from_before_a_command_is_not_offered_again(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry
) -> None:
    """While HA holds the link after a command, the history still shows the old state: kept out."""
    air.advertise(on=True)
    await air.wait(LAST_SEEN_RESOLUTION_SECONDS)  # heard well after the setup poll
    await air.wait(5, heard=False)  # HA's newest advertisement is from before the command
    await light_service(hass, "turn_off")
    for _ in range(2):  # presence ticks while the link stays up: the device is silent
        await air.wait(LAST_SEEN_RESOLUTION_SECONDS, heard=False)
        assert hass.states.get(MAIN_LIGHT).state == "off"


@pytest.mark.parametrize(
    ("device_sku", "entity_id", "polled"),
    [
        pytest.param("H6008", MAIN_LIGHT, True, id="light"),
        pytest.param("H5083", PLUG, False, id="relay_plug"),
    ],
)
async def test_turned_on_inside_the_window_is_taken_from_the_repeats(
    hass: HomeAssistant,
    air: Air,
    entry: MockConfigEntry,
    mock_device: Device,
    *,
    device_sku: str,
    entity_id: str,
    polled: bool,
) -> None:
    """Turned back on outside HA seconds after HA turned it off: the repeats correct it.

    HA calls back once for the advertisement, which the library ignores (it came
    within its window after the command); the device then keeps repeating it,
    which calls nothing back, and with no background polls nothing else would.
    A light is then polled as after any turn-on; a relay plug isn't.
    """
    air.advertise(on=True)
    await power_service(hass, device_sku, "turn_off")
    air.advertise(on=False)
    before = polls(mock_device)
    await air.wait(WINDOW / 2, heard=False)
    air.advertise(on=True)  # turned back on, inside the library's window
    assert hass.states.get(entity_id).state == "off"

    # A repeat HA receives just past the window is the newest one at the next presence tick
    # (nothing newer is heard by then): that repeat, not a later one, is what is offered again.
    await air.wait(WINDOW / 2 + 1)
    await air.wait(LAST_SEEN_RESOLUTION_SECONDS, heard=False)
    assert hass.states.get(entity_id).state == "on"
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before + polled


async def test_turned_off_inside_the_window_is_taken_from_the_repeats(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """Turned off outside HA seconds after HA turned it on (the library ignores that advert
    inside its window): HA shows it off within a presence tick and stops polling it, instead of
    showing it on, and polling it, until the next poll."""
    air.advertise(on=False)
    await light_service(hass, "turn_on")
    air.advertise(on=True)  # the device advertises what HA set
    await air.wait(WINDOW / 2, heard=False)
    air.advertise(on=False)  # turned off outside HA, inside the library's window
    assert hass.states.get(MAIN_LIGHT).state == "on"
    before = polls(mock_device)

    await air.wait(LAST_SEEN_RESOLUTION_SECONDS + WINDOW)
    assert hass.states.get(MAIN_LIGHT).state == "off"
    for _ in range(3):
        await air.wait(BASE)
    assert polls(mock_device) == before


async def test_turned_on_while_ha_held_the_link_is_taken_from_the_repeats(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """Back on before the link closed: its advertisement matches the one before the command.

    So HA never calls back for it at all; the repeats are offered again instead.
    """
    air.advertise(on=True)
    await light_service(hass, "turn_off")
    before = polls(mock_device)
    await air.wait(30, heard=False)  # the link stays up after a command: no advertisements
    await air.wait(LAST_SEEN_RESOLUTION_SECONDS)  # then "on" again, as before the command
    assert hass.states.get(MAIN_LIGHT).state == "on"
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before + 1


async def test_turned_on_right_after_a_poll_is_taken_from_the_repeats(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A poll that read power, then a turn-on outside HA the library ignores (it came within its
    window after that read): corrected within a presence tick, and polled as a turn-on."""
    air.advertise(on=False)
    poll_reads_power(mock_device)
    await update_entity(hass)  # a one-off poll (a manual refresh)
    before = polls(mock_device)
    await air.wait(WINDOW / 2, heard=False)
    air.advertise(on=True)  # inside the library's window after that read

    await air.wait(LAST_SEEN_RESOLUTION_SECONDS)
    assert hass.states.get(MAIN_LIGHT).state == "on"
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before + 1


async def test_a_poll_reading_the_opposite_is_not_overridden_every_tick(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """Once the repeats corrected the light, a poll that reads it otherwise has the last word:
    the same repeats aren't offered again at each tick (flipping it, and polling it, each time).
    """
    air.advertise(on=True)
    await light_service(hass, "turn_off")
    air.advertise(on=False)
    await air.wait(WINDOW / 2, heard=False)
    air.advertise(on=True)  # turned on outside HA inside the library's window
    await air.wait(LAST_SEEN_RESOLUTION_SECONDS + WINDOW)
    assert hass.states.get(MAIN_LIGHT).state == "on"  # taken from the repeats
    before = polls(mock_device)
    read = mock_device.update.side_effect  # type: ignore[attr-defined]

    async def _reads_off(**kwargs: Any) -> Any:
        mock_device.state.is_on = False
        return await read(**kwargs)

    mock_device.update.side_effect = _reads_off  # type: ignore[attr-defined]
    await air.wait(DELAY + SLACK)  # its turn-on poll reads it off
    assert polls(mock_device) == before + 1
    assert hass.states.get(MAIN_LIGHT).state == "off"
    for _ in range(5):
        await air.wait(LAST_SEEN_RESOLUTION_SECONDS)
    assert hass.states.get(MAIN_LIGHT).state == "off"
    assert polls(mock_device) == before + 1


async def test_a_learned_pact_is_not_an_on_off_flip(
    air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """An advertisement that only brings a new pact (still on) is no on/off flip: the turn-on
    poll a turn-on outside HA just scheduled still runs, instead of being dropped as by a flip."""
    air.advertise(on=False)
    air.advertise(on=True)  # turned on outside HA: its poll is due DELAY later
    before = polls(mock_device)
    pact = (0x0002, 0x01)
    assert mock_device.pact != pact
    air.advertise(manufacturer_data=pact_mfg(*pact))
    assert mock_device.pact == pact  # the device learned it
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before + 1


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("device_sku", ["H60A6"])  # excluded: the fixed cadence
async def test_repeats_are_not_offered_again_outside_the_policy(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """For an excluded model, the repeats of a turn-on the library ignored aren't offered
    again: HA keeps showing it off, on the fixed cadence, whose next poll (and no other) is
    what corrects it."""
    air.advertise(on=True)
    await light_service(hass, "turn_off")
    air.advertise(on=False)
    await air.wait(WINDOW / 2, heard=False)
    air.advertise(on=True)  # turned back on outside HA, inside the library's window

    await air.wait(LAST_SEEN_RESOLUTION_SECONDS + WINDOW)  # a presence tick past the window
    assert hass.states.get(MAIN_LIGHT).state == "off"
    assert poll_interval(hass) == str(BASE)

    before = polls(mock_device)
    read = mock_device.update.side_effect  # type: ignore[attr-defined]

    async def _reads_on(**kwargs: Any) -> Any:
        mock_device.state.is_on = True
        return await read(**kwargs)

    mock_device.update.side_effect = _reads_on  # type: ignore[attr-defined]
    await air.wait(BASE)  # the fixed cadence's next poll reads it on
    assert polls(mock_device) == before + 1
    assert hass.states.get(MAIN_LIGHT).state == "on"


async def test_unload_cancels_the_pending_turn_on_poll(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry
) -> None:
    """Unloading while the turn-on poll waits drops it: no timer outlives the entry (HA's test
    harness fails a test that leaves one behind)."""
    air.advertise(on=False)
    air.advertise(on=True)  # its turn-on poll is now waiting
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_a_turn_on_during_a_poll_that_reads_off_leaves_no_timer_armed(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """The policy moved to on while a poll ran and back to off when it ended: no stray poll.

    HA arms its timer when the policy moves to on; at the end of the poll it
    doesn't cancel it while the interval is None, so the coordinator does.
    """
    air.advertise(on=False)
    before = polls(mock_device)
    read = mock_device.update.side_effect  # type: ignore[attr-defined]

    async def _read(**kwargs: Any) -> Any:
        air.advertise(on=True)  # mid-read: the policy moves to on and arms HA's timer
        mock_device.state.is_on = False  # but the read, taken before, says off
        return await read(**kwargs)

    mock_device.update.side_effect = _read  # type: ignore[attr-defined]
    await update_entity(hass)
    mock_device.update.side_effect = read  # type: ignore[attr-defined]
    air.advertise(on=False)  # what it really is
    await air.wait(BASE + SLACK)
    assert polls(mock_device) == before + 1


async def test_return_poll_stands_in_for_the_turn_on_poll(
    air: Air,
    entry: MockConfigEntry,
    mock_device: Device,
    *,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """Back in reach and turned on in one advertisement: one poll, not a return poll and a
    turn-on poll."""
    air.advertise(on=False)
    presence.connectable = False
    presence.connectable_service_info = None
    unavailable_callback(mock_bluetooth, connectable=True)(make_service_info())
    await air.wait(BASE + SLACK)  # its poll is skipped: no connectable scanner sees it
    before = polls(mock_device)

    presence.connectable = True
    presence.connectable_service_info = SAME_AS_ANY_SCANNER
    air.advertise(on=True)  # back in reach, and on
    await air.wait(DELAY + SLACK)
    await air.wait(30)  # past HA's request debouncer cooldown
    assert polls(mock_device) == before + 1


async def test_turn_on_poll_is_dropped_after_a_rejected_secret(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A poll rejects the secret while the turn-on poll waits: it isn't tried again (HA asks
    for the secret instead)."""
    air.advertise(on=False)
    air.advertise(on=True)
    mock_device.update.side_effect = GoveeBleAuthError("secret rejected")  # type: ignore[attr-defined]
    before = polls(mock_device)
    await update_entity(hass)
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before + 1
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_turn_on_poll_is_dropped_while_no_entity_listens(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """Every entity disabled: like HA's own timer, the turn-on poll doesn't connect."""
    registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        registry.async_update_entity(
            registered.entity_id, disabled_by=er.RegistryEntryDisabler.USER
        )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(MAIN_LIGHT) is None

    air.advertise(on=False)
    before = polls(mock_device)
    air.advertise(on=True)
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_off_flip_stops_background_polls(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A light turned off outside HA is no longer polled."""
    await turned_on_outside_ha(air)
    assert poll_interval(hass) == str(BASE)
    air.advertise(on=False)
    assert poll_interval(hass) == STATE_UNKNOWN

    before = polls(mock_device)
    for _ in range(6):
        await air.wait(MAX_POLL_INTERVAL_SECONDS)
    assert polls(mock_device) == before


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_commands_from_ha_start_and_stop_background_polls(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """Turned on from HA it is polled at the base cadence; turned off from HA, not at all."""
    air.advertise(on=False)
    before = polls(mock_device)
    await light_service(hass, "turn_on")
    air.advertise(on=True)
    assert poll_interval(hass) == str(BASE)
    await air.wait(BASE + SLACK)
    assert polls(mock_device) == before + 1

    await light_service(hass, "turn_off")
    air.advertise(on=False)
    assert poll_interval(hass) == STATE_UNKNOWN
    for _ in range(6):
        await air.wait(MAX_POLL_INTERVAL_SECONDS)
    assert polls(mock_device) == before + 1


# -- on: the backoff --------------------------------------------------------------


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_on_backoff_doubles_to_the_cap(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """Each poll that finds nothing new doubles the interval, up to 15 minutes."""
    await turned_on_outside_ha(air)
    seen = []
    for _ in range(len(ON_BACKOFF) + 1):
        interval = int(poll_interval(hass))
        seen.append(interval)
        before = polls(mock_device)
        await air.wait(interval - SLACK)
        assert polls(mock_device) == before  # not before the interval
        await air.wait(2 * SLACK)
        assert polls(mock_device) == before + 1
    assert seen == [*ON_BACKOFF, ON_POLL_MAX_INTERVAL_SECONDS]
    assert poll_interval(hass) == str(ON_POLL_MAX_INTERVAL_SECONDS)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_on_backoff_restarts_when_a_poll_reads_something_new(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A colour, brightness or scene changed outside HA (seen by a poll) starts it over."""
    await turned_on_outside_ha(air)
    for expected in ON_BACKOFF[1:3]:
        await air.wait(int(poll_interval(hass)) + SLACK)
        assert poll_interval(hass) == str(expected)

    mock_device.state.brightness = 42  # what the next poll reads back
    await air.wait(int(poll_interval(hass)) + SLACK)
    assert poll_interval(hass) == str(BASE)
    await air.wait(BASE + SLACK)
    assert poll_interval(hass) == str(BASE * 2)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("device_zones", [BARS], ids=["bars"])
async def test_on_backoff_ignores_a_poll_that_only_reconfirms_the_bars(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A poll that reads the same bars again stamps them confirmed again: that is bookkeeping
    (``zone_power_confirmed``), not something new, so the backoff keeps doubling."""
    bars = [zone.power_index for zone in mock_device.zones]
    await turned_on_outside_ha(air)
    stamps = itertools.count(1)

    async def _reads_the_bars(**_kwargs: Any) -> Any:
        mock_device.state.zone_power = dict.fromkeys(bars, True)
        mock_device.state.zone_power_confirmed = dict.fromkeys(bars, float(next(stamps)))
        mock_device.state.optimistic = False
        return mock_device.state

    mock_device.update.side_effect = _reads_the_bars  # type: ignore[attr-defined]
    await air.wait(BASE + SLACK)  # the bars read for the first time: something new
    assert poll_interval(hass) == str(BASE)
    for expected in ON_BACKOFF[1:3]:
        await air.wait(int(poll_interval(hass)) + SLACK)
        assert poll_interval(hass) == str(expected)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_on_backoff_restarts_on_an_advertised_flip(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """Off and on again outside HA: after the turn-on poll, polls at the base cadence again.

    Even when the light comes back as it was before (the turn-on poll reads the
    same as the last poll before it went off).
    """
    await turned_on_outside_ha(air)
    for _ in range(3):
        await coordinator.async_refresh()
    assert poll_interval(hass) == str(ON_POLL_MAX_INTERVAL_SECONDS)

    air.advertise(on=False)
    air.advertise(on=True)
    assert poll_interval(hass) == str(BASE)
    before = polls(mock_device)
    await air.wait(DELAY + SLACK)  # the turn-on poll
    assert polls(mock_device) == before + 1
    assert poll_interval(hass) == str(BASE)
    await air.wait(BASE - SLACK)
    assert polls(mock_device) == before + 1
    await air.wait(2 * SLACK)
    assert polls(mock_device) == before + 2
    assert poll_interval(hass) == str(BASE * 2)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_on_backoff_restarts_on_a_command(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """An HA command brings the next poll forward to the base cadence, once per backoff: a
    second command can't keep postponing it."""
    await turned_on_outside_ha(air)
    for _ in range(3):
        await air.wait(int(poll_interval(hass)) + SLACK)
    assert poll_interval(hass) == str(ON_POLL_MAX_INTERVAL_SECONDS)
    before = polls(mock_device)

    await light_service(hass, "turn_on", brightness=128)
    assert poll_interval(hass) == str(BASE)
    await air.wait(BASE / 2)  # past the first command's hold-off
    await light_service(hass, "turn_on", brightness=64)
    await air.wait(BASE / 2 - SLACK)
    assert polls(mock_device) == before
    await air.wait(2 * SLACK)
    assert polls(mock_device) == before + 1  # BASE after the first command, not the second


# -- failures ---------------------------------------------------------------------


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("absent", [False, True], ids=["failed", "skipped"])
async def test_failure_backoff_is_unchanged_while_on(
    hass: HomeAssistant,
    air: Air,
    entry: MockConfigEntry,
    mock_device: Device,
    *,
    presence: SimpleNamespace,
    absent: bool,
) -> None:
    """Failed or skipped polls of a light that is on back off as before, to 30 minutes.

    Skipped ones are skipped because no connectable scanner sees it, which puts
    it back on the fixed cadence, so they are counted as before. Going stale
    meanwhile keeps the failure backoff.
    """
    await turned_on_outside_ha(air)
    mock_device.update.side_effect = GoveeBleConnectionError("no slot")  # type: ignore[attr-defined]
    presence.connectable = not absent
    seen = []
    for _ in range(len(FAILURE_BACKOFF) + 1):
        await air.wait(int(poll_interval(hass)) + SLACK)
        seen.append(int(poll_interval(hass)))
    assert seen == [*FAILURE_BACKOFF, MAX_POLL_INTERVAL_SECONDS]

    await air.wait(FRESH + LAST_SEEN_RESOLUTION_SECONDS, heard=False)
    assert poll_interval(hass) == str(MAX_POLL_INTERVAL_SECONDS)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_commands_drop_the_failure_backoff_once_per_streak_while_on(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """The once-per-failure-streak rule still holds for a light that is on: a command brings the
    next poll forward to the base cadence once, not at every command."""
    await turned_on_outside_ha(air)
    mock_device.update.side_effect = GoveeBleConnectionError("read-back failed")  # type: ignore[attr-defined]
    for _ in range(len(FAILURE_BACKOFF) + 1):
        await air.wait(int(poll_interval(hass)) + SLACK)
    assert poll_interval(hass) == str(MAX_POLL_INTERVAL_SECONDS)

    await light_service(hass, "turn_on", brightness=200)
    assert poll_interval(hass) == str(BASE)
    before = polls(mock_device)
    await air.wait(BASE + SLACK)  # the poll brought forward fails too
    assert polls(mock_device) == before + 1
    assert poll_interval(hass) == str(MAX_POLL_INTERVAL_SECONDS)
    await light_service(hass, "turn_on", brightness=100)
    assert poll_interval(hass) == str(MAX_POLL_INTERVAL_SECONDS)
    await air.wait(BASE + SLACK)
    assert polls(mock_device) == before + 1  # not brought forward again


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("slow", "resumed_at"),
    [(False, BASE * 2), (True, BASE * 4)],
    ids=["quick_failures", "slow_timeouts"],
)
async def test_failed_one_off_polls_of_an_off_light_start_no_background_polls(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    air: Air,
    entry: MockConfigEntry,
    mock_device: Device,
    *,
    slow: bool,
    resumed_at: int,
) -> None:
    """Failing manual refreshes of an off light keep it unpolled; turned on, it backs off.

    A refresh whose connect runs out of its budget is a slow failure: counted like
    any other, it adds a slow step (a floor on every interval) instead of a backoff
    step. Turned on, the light is polled at that streak's interval, not the base one.
    """
    air.advertise(on=False)

    async def _fails(**_kwargs: Any) -> Any:
        if slow:
            air.clock.now += POLL_CONNECT_TIMEOUT_SECONDS + 1
            raise GoveeBleTimeout(f"{ADDRESS}: connect exceeded {POLL_CONNECT_TIMEOUT_SECONDS}s")
        raise GoveeBleConnectionError("no slot")

    mock_device.update.side_effect = _fails  # type: ignore[attr-defined]
    for _ in range(2):
        await update_entity(hass)
        await air.wait(15)  # past HA's request debouncer cooldown
    assert poll_interval(hass) == STATE_UNKNOWN
    diagnostics = await coordinator_diagnostics(hass, hass_client, entry)
    assert diagnostics["consecutive_failures"] == 2
    assert diagnostics["slow_poll_steps"] == (2 if slow else 0)
    assert diagnostics["poll_policy"] == "off"
    before = polls(mock_device)
    for _ in range(3):
        await air.wait(MAX_POLL_INTERVAL_SECONDS)
    assert polls(mock_device) == before

    air.advertise(on=True)
    assert poll_interval(hass) == str(resumed_at)


# -- freshness --------------------------------------------------------------------


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_losing_the_byte_restores_the_fixed_cadence_within_one_presence_tick(
    hass: HomeAssistant, air: Air, entry: MockConfigEntry, mock_device: Device
) -> None:
    """No on/off byte heard for ADVERT_POWER_FRESH_SECONDS: back to the fixed cadence.

    The tick re-arms it; the first poll follows one base interval later.
    """
    air.advertise(on=False)
    before = polls(mock_device)
    await air.wait(FRESH - 1, heard=False)
    assert poll_interval(hass) == STATE_UNKNOWN  # still fresh at that tick

    await air.wait(LAST_SEEN_RESOLUTION_SECONDS, heard=False)
    assert poll_interval(hass) == str(BASE)
    await air.wait(BASE + SLACK, heard=False)
    assert polls(mock_device) == before + 1

    # Heard again (the same advertisement): no background polls again.
    air.advertise(on=False)
    assert poll_interval(hass) == STATE_UNKNOWN


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_backed_off_light_losing_the_byte_resumes_the_fixed_cadence(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """A light backed off to 15 minutes that goes stale polls at the base cadence again."""
    await turned_on_outside_ha(air)
    for _ in range(3):
        await coordinator.async_refresh()
    assert poll_interval(hass) == str(ON_POLL_MAX_INTERVAL_SECONDS)
    before = polls(mock_device)

    await air.wait(FRESH + LAST_SEEN_RESOLUTION_SECONDS, heard=False)
    assert poll_interval(hass) == str(BASE)
    await air.wait(BASE + SLACK, heard=False)
    assert polls(mock_device) == before + 1


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_freshness_reads_the_any_scanner_history(
    hass: HomeAssistant,
    air: Air,
    entry: MockConfigEntry,
    mock_device: Device,
    presence: SimpleNamespace,
) -> None:
    """The byte counts as heard when any scanner heard it, not only a connectable one."""
    air.advertise(on=False)
    # The connectable scanner's entry has no manufacturer data (no scan response, say).
    presence.connectable_service_info = make_service_info(
        manufacturer_data={}, seen_at=air.clock.now
    )
    before = polls(mock_device)
    for _ in range(3):  # presence ticks meanwhile; any scanner keeps hearing the byte
        await air.wait(BASE)
    assert poll_interval(hass) == STATE_UNKNOWN
    assert polls(mock_device) == before


@pytest.mark.parametrize("device_sku", ["H6006"])
async def test_byte_never_heard_keeps_the_fixed_cadence(
    air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """An H6006 heard by a passive scanner (no scan response, so no byte) is polled as before."""
    air.advertise(manufacturer_data={})  # only flags and name
    mock_device.state.is_on = False
    for _ in range(3):
        before = polls(mock_device)
        await air.wait(BASE + SLACK)
        assert polls(mock_device) == before + 1
        assert coordinator.poll_policy is PollPolicy.FIXED
        assert nominal(coordinator) == BASE


# -- relay plugs and excluded models ----------------------------------------------


@pytest.mark.parametrize("device_sku", ["H5083"])
async def test_relay_plug_is_followed_by_its_advertisements_only(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """The relay is a plug's only state: no background polls and no turn-on poll while fresh."""
    air.advertise(on=False)
    assert coordinator.poll_policy is PollPolicy.RELAY
    before = polls(mock_device)
    air.advertise(on=True)
    assert mock_device.state.is_on is True
    for _ in range(6):
        await air.wait(MAX_POLL_INTERVAL_SECONDS)
    assert polls(mock_device) == before

    await air.wait(FRESH + LAST_SEEN_RESOLUTION_SECONDS, heard=False)
    assert coordinator.poll_policy is PollPolicy.FIXED
    await air.wait(BASE + SLACK, heard=False)
    assert polls(mock_device) == before + 1


@pytest.mark.parametrize("device_sku", ["H60A6"])
async def test_excluded_model_keeps_todays_cadence_whatever_it_advertises(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """H60A6: polled every POLL_INTERVAL_SECONDS off or on, and no turn-on poll."""
    air.advertise(on=False)
    before = polls(mock_device)
    await air.wait(BASE + SLACK)
    assert polls(mock_device) == before + 1

    air.advertise(on=True)
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before + 1
    for count in (2, 3):
        await air.wait(BASE)
        assert polls(mock_device) == before + count
        assert nominal(coordinator) == BASE


# -- what polling can't follow keeps today's semantics ----------------------------


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_polling_disabled_keeps_its_semantics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    air: Air,
    mock_device: Device,
    device_sku: str,
) -> None:
    """Disabled in system options: no polls at all, and no turn-on poll either."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=ADDRESS,
        data={"address": ADDRESS, CONF_SKU: device_sku},
        pref_disable_polling=True,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    air.advertise(on=False)
    air.advertise(on=True)
    before = polls(mock_device)
    await air.wait(MAX_POLL_INTERVAL_SECONDS)
    assert polls(mock_device) == before
    assert poll_interval(hass) == STATE_UNKNOWN
    # The advertisements don't move it off the fixed step it can't poll on.
    assert (await coordinator_diagnostics(hass, hass_client, entry))["poll_policy"] == "fixed"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_rejected_secret_keeps_polling_halted(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    air: Air,
    entry: MockConfigEntry,
    mock_device: Device,
) -> None:
    """A rejected secret halts polls; the policy moving on doesn't re-arm them."""
    air.advertise(on=False)
    mock_device.update.side_effect = GoveeBleAuthError("secret rejected")  # type: ignore[attr-defined]
    await update_entity(hass)
    await air.wait(LAST_SEEN_RESOLUTION_SECONDS)
    assert poll_interval(hass) == STATE_UNKNOWN
    assert (await coordinator_diagnostics(hass, hass_client, entry))["poll_policy"] == "fixed"

    before = polls(mock_device)
    for _ in range(3):
        await air.wait(MAX_POLL_INTERVAL_SECONDS)
    assert polls(mock_device) == before


# -- availability -----------------------------------------------------------------


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_out_of_reach_light_keeps_the_grace_of_failed_polls(
    hass: HomeAssistant,
    air: Air,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    *,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """A light that is on leaves every connectable scanner; a passive one hears it turn off.

    It stays on the fixed cadence (its polls skipped and counted), so it goes
    unavailable after UNAVAILABLE_AFTER_FAILURES of them, as before.
    """
    await turned_on_outside_ha(air)
    presence.connectable = False
    presence.connectable_service_info = None
    unavailable_callback(mock_bluetooth, connectable=True)(make_service_info())
    assert hass.states.get(MAIN_LIGHT).state == "on"

    await coordinator.async_refresh()  # skipped: no connectable scanner sees it
    air.advertise(on=False)  # heard by a passive scanner
    assert hass.states.get(MAIN_LIGHT).state == "off"
    assert poll_interval(hass) == str(BASE)  # still polled: off isn't followed out of reach

    for _ in range(UNAVAILABLE_AFTER_FAILURES - 1):
        await coordinator.async_refresh()
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE


@pytest.mark.parametrize("device_readback", ["none"])
async def test_a_device_with_no_read_back_is_never_polled_and_available_while_heard(
    hass: HomeAssistant,
    air: Air,
    entry: MockConfigEntry,
    mock_device: Device,
    hass_client: ClientSessionGenerator,
    *,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """A device the library reads nothing from (govee-ble-local 1.8.0's device-list card rows)
    gets no poll: none after setup, none scheduled, none after a turn-on outside Home
    Assistant, and a manual refresh reads nothing either. Its state is what its advertisement
    and Home Assistant's commands say. No poll answers, so none counts against it: it is
    available while a connectable scanner hears it, unavailable as soon as none does, and
    back once one does."""
    await light_service(hass, "turn_on", brightness=128)
    air.advertise(on=False)
    air.advertise(on=True)
    await update_entity(hass)
    await air.wait(MAX_POLL_INTERVAL_SECONDS + SLACK)
    assert polls(mock_device) == 0
    assert hass.states.get(MAIN_LIGHT).state == "on"
    assert (await coordinator_diagnostics(hass, hass_client, entry))["poll_policy"] == "unread"

    presence.connectable = False
    presence.connectable_service_info = None
    unavailable_callback(mock_bluetooth, connectable=True)(make_service_info())
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE
    presence.connectable = True
    presence.connectable_service_info = SAME_AS_ANY_SCANNER
    air.advertise(on=False)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(MAIN_LIGHT).state == "off"
    assert polls(mock_device) == 0


@pytest.mark.parametrize(
    "passive_still_hears", [True, False], ids=["passive_still_hears", "gone_from_all"]
)
async def test_polls_resuming_for_an_absent_device_keep_it_unavailable(
    hass: HomeAssistant,
    air: Air,
    entry: MockConfigEntry,
    mock_device: Device,
    *,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
    passive_still_hears: bool,
) -> None:
    """An off light (unpolled, so no failed polls gave it grace) that leaves every connectable
    scanner is unavailable at once, and stays so while its resumed polls are skipped, even if
    a passive scanner still hears it. Heard by a connectable one again, it is available at
    once, with its return poll, and unpolled again."""
    air.advertise(on=False)
    assert hass.states.get(MAIN_LIGHT).state == "off"
    presence.connectable = False
    presence.connectable_service_info = None
    unavailable_callback(mock_bluetooth, connectable=True)(make_service_info())
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE
    if not passive_still_hears:
        # The same sweep then drops the any-scanner history: the byte is gone too.
        presence.any = False
        presence.service_info = None
        unavailable_callback(mock_bluetooth, connectable=False)(make_service_info())
        assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE

    before = polls(mock_device)
    await air.wait(BASE + SLACK, heard=passive_still_hears)  # skipped: it can't be reached
    assert polls(mock_device) == before
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE

    presence.connectable = presence.any = True
    presence.connectable_service_info = SAME_AS_ANY_SCANNER
    air.advertise(on=False)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(MAIN_LIGHT).state == "off"
    assert polls(mock_device) == before + 1  # the return poll
    for _ in range(3):
        await air.wait(BASE)
    assert polls(mock_device) == before + 1  # unpolled again


async def _went_silent(air: Air) -> float:
    """The device is heard one last time; return when (on the Bluetooth clock)."""
    await air.wait(1)
    return float(air.clock.now)


def _dropped(mock_bluetooth: SimpleNamespace, presence: SimpleNamespace, last_heard: float) -> None:
    """HA drops the device from its connectable history (no connectable scanner has it),
    passing the advertisement it last had, heard at ``last_heard``."""
    presence.connectable = False
    presence.connectable_service_info = None
    unavailable_callback(mock_bluetooth, connectable=True)(make_service_info(seen_at=last_heard))


@pytest.mark.parametrize("device_sku", ["H60A6"])  # excluded: the fixed cadence
async def test_a_slow_device_gone_unheard_goes_unavailable_within_minutes(
    hass: HomeAssistant,
    air: Air,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    *,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """A weak device at the slow floor (polled every 16 minutes) that stops being heard goes
    unavailable within UNAVAILABLE_AFTER_UNHEARD_SECONDS of its last advertisement (plus a
    presence tick), not after three of its far-apart polls."""
    for _ in range(60):  # heard for half an hour first: counted from its last advert, not setup
        await air.wait(30)
    read = mock_device.update.side_effect  # type: ignore[attr-defined]

    async def _slow(**kwargs: Any) -> Any:
        air.clock.now += SLOW_POLL_SECONDS
        return await read(**kwargs)

    mock_device.update.side_effect = _slow  # type: ignore[attr-defined]
    for _ in range(SLOW_POLL_MAX_STEPS):
        await coordinator.async_refresh()
    assert nominal(coordinator) == BASE * (1 << SLOW_POLL_MAX_STEPS)

    last_heard = await _went_silent(air)
    await air.wait(200, heard=False)  # HA keeps it in its history meanwhile
    _dropped(mock_bluetooth, presence, last_heard)
    assert hass.states.get(MAIN_LIGHT).state != STATE_UNAVAILABLE  # not unheard long enough
    while hass.states.get(MAIN_LIGHT).state != STATE_UNAVAILABLE:
        await air.wait(10, heard=False)
        bound = UNAVAILABLE_AFTER_UNHEARD_SECONDS + LAST_SEEN_RESOLUTION_SECONDS
        assert air.clock.now - last_heard <= bound
    assert air.clock.now - last_heard >= UNAVAILABLE_AFTER_UNHEARD_SECONDS


@pytest.mark.parametrize("device_sku", ["H60A6"])  # excluded: the fixed cadence
async def test_a_device_gone_while_its_connects_time_out_is_unavailable_once_ha_drops_it(
    hass: HomeAssistant,
    air: Air,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    *,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """Switched off at the wall: HA keeps it in its history for up to 525 s, and the polls in
    that window run out their connect budget (slow failures, spaced further apart). When HA
    drops it, it has gone unheard for longer than UNAVAILABLE_AFTER_UNHEARD_SECONDS: it is
    unavailable at once, as when those polls counted as plain failures."""

    async def _timeout(**_kwargs: Any) -> Any:
        air.clock.now += POLL_CONNECT_TIMEOUT_SECONDS + 1
        raise GoveeBleTimeout(f"{ADDRESS}: connect exceeded {POLL_CONNECT_TIMEOUT_SECONDS}s")

    last_heard = await _went_silent(air)
    mock_device.update.side_effect = _timeout  # type: ignore[attr-defined]
    for _ in range(525 // 15):
        await air.wait(15, heard=False)
    assert coordinator.slow_poll_steps  # its polls timed out, slowly
    assert coordinator.consecutive_failures < UNAVAILABLE_AFTER_FAILURES
    assert hass.states.get(MAIN_LIGHT).state != STATE_UNAVAILABLE  # still in HA's history
    _dropped(mock_bluetooth, presence, last_heard)
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE


@pytest.mark.parametrize("device_sku", ["H60A6"])  # excluded: the fixed cadence
async def test_a_device_commanded_over_a_held_link_stays_available(
    hass: HomeAssistant,
    air: Air,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    *,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
    hass_admin_user: MockUser,
) -> None:
    """A device doesn't advertise while HA holds its link, so a person's commands under 30 s
    apart (a voice or automation command releases the link: test_link_release) outlast HA's
    history of it. Each command that lands shows it reachable, and its polls run
    over the held link instead of being skipped as absent: it stays available for 40 minutes
    and no command is skipped (HA skips an unavailable entity's commands). Once its power is
    cut, it goes unavailable within UNAVAILABLE_AFTER_UNHEARD_SECONDS (plus a presence tick)
    of the last command that landed.

    Source-modelled: the library reuses its open link for a poll (transport/connection.py
    ``_ensure_connected_locked``) and keeps it ACTIVE_IDLE_DELAY after each write; not yet
    checked on the bench, which needs commands (visible changes) and the user's OK.
    """
    last_heard = await _went_silent(air)
    polls_before_drop = 0
    dropped = False
    for step in range(120):  # a command every 20 s for 40 minutes
        sent = len(connection(mock_device).sent)
        await light_service(
            hass, "turn_on", context=Context(user_id=hass_admin_user.id), brightness=10 + step % 80
        )
        assert len(connection(mock_device).sent) > sent, step  # it went out
        assert hass.states.get(MAIN_LIGHT).state == "on", step
        await air.wait(20, heard=False)
        if not dropped and air.clock.now - last_heard >= 300:
            _dropped(mock_bluetooth, presence, last_heard)
            dropped = True
            polls_before_drop = polls(mock_device)
    assert dropped
    assert polls(mock_device) > polls_before_drop  # polls ran over the held link
    assert coordinator.consecutive_failures == 0

    last_landed = air.clock.now - 20
    mock_device.update.side_effect = GoveeBleConnectionError("link lost")  # power cut
    while hass.states.get(MAIN_LIGHT).state != STATE_UNAVAILABLE:
        await air.wait(10, heard=False)
        bound = UNAVAILABLE_AFTER_UNHEARD_SECONDS + LAST_SEEN_RESOLUTION_SECONDS + 10
        assert air.clock.now - last_landed <= bound
    assert air.clock.now - last_landed >= UNAVAILABLE_AFTER_UNHEARD_SECONDS


# -- the poll interval sensor -----------------------------------------------------


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_poll_interval_sensor_follows_the_policy_without_churn(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator
) -> None:
    """It shows the interval in effect (unknown with no background polls), changing only with it."""
    entity_id = sensor_id(hass, "poll_interval")
    assert hass.states.get(entity_id).state == str(BASE)

    air.advertise(on=False)
    assert hass.states.get(entity_id).state == STATE_UNKNOWN
    await turned_on_outside_ha(air)
    assert hass.states.get(entity_id).state == str(BASE)
    for expected in ON_BACKOFF[1:]:
        await coordinator.async_refresh()
        assert hass.states.get(entity_id).state == str(expected)

    changed = hass.states.get(entity_id).last_changed
    air.advertise(on=True)  # nothing new
    for _ in range(3):
        await air.wait(LAST_SEEN_RESOLUTION_SECONDS)
    assert hass.states.get(entity_id).state == str(ON_POLL_MAX_INTERVAL_SECONDS)
    assert hass.states.get(entity_id).last_changed == changed
