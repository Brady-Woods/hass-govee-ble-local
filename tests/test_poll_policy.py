"""Tests for the advertisement-driven background poll policy (coordinator._async_policy_step).

Time moves through :class:`Air`, which ticks the wall, event-loop and Bluetooth
clocks together and keeps HA's advertisement history as fresh as a device that
keeps advertising. Like a real device, a test's device advertises its new on/off
after HA commands it: the presence tick re-offers HA's newest advertisement to
the library, and one left showing the old state would read as an outside change.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import timedelta
import itertools
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from govee_ble_local import Device, GoveeBleAuthError, GoveeBleConnectionError, GoveeBleTimeout
import govee_ble_local.const as lib_const
from homeassistant.components.bluetooth import BluetoothChange, BluetoothServiceInfoBleak
from homeassistant.components.bluetooth.const import (
    CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    get_scheduled_timer_handles,
)

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
    POLL_INTERVAL_SECONDS,
    SLOW_POLL_MAX_STEPS,
    SLOW_POLL_SECONDS,
    UNAVAILABLE_AFTER_FAILURES,
    UNAVAILABLE_AFTER_UNHEARD_SECONDS,
)
from custom_components.govee_ble_local.coordinator import GoveeBleLocalCoordinator, PollPolicy
from custom_components.govee_ble_local.diagnostics import async_get_config_entry_diagnostics

from .conftest import (
    SAME_AS_ANY_SCANNER,
    advance,
    advertisement_callback,
    connection,
    make_service_info,
    unavailable_callback,
)
from .const import ADDRESS, TITLE, govee_mfg

COORDINATOR = "custom_components.govee_ble_local.coordinator"
BASE = POLL_INTERVAL_SECONDS
DELAY = ADVERT_FLIP_POLL_DELAY_SECONDS
FRESH = ADVERT_POWER_FRESH_SECONDS
MAIN_LIGHT = "light.test_ceiling"
PLUG = "switch.test_ceiling"
# HA arms a poll timer to the whole second plus a per-coordinator microsecond
# offset, so a poll can come up to a second either side of its interval.
SLACK = 2
UNPOLLED = {PollPolicy.OFF, PollPolicy.RELAY}
ON_BACKOFF = [BASE, BASE * 2, BASE * 4, ON_POLL_MAX_INTERVAL_SECONDS]
FAILURE_BACKOFF = [BASE, BASE * 2, BASE * 4, BASE * 8, MAX_POLL_INTERVAL_SECONDS]


@pytest.fixture
def device_sku() -> str:
    """A light the policy covers (it advertises its on/off byte in the advertisement)."""
    return "H6008"


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
async def coordinator(air: Air, setup_integration: MockConfigEntry) -> GoveeBleLocalCoordinator:
    """The loaded entry's coordinator (first poll done); the test moves the clocks."""
    return setup_integration.runtime_data.coordinator


def polls(device: Device) -> int:
    """Return how many polls have run."""
    return int(device.update.await_count)  # type: ignore[attr-defined]


def nominal(coordinator: GoveeBleLocalCoordinator) -> float | None:
    """Return the nominal poll interval in seconds (None while there are no background polls)."""
    interval = coordinator.nominal_interval
    return None if interval is None else interval.total_seconds()


def turn_on_poll_pending(hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator) -> bool:
    """Return whether the poll after an advertised turn-on is waiting on a timer."""
    return any(
        getattr(arg, "target", None) == coordinator._async_flip_poll_due
        for handle in get_scheduled_timer_handles(hass.loop)
        if not handle.cancelled()
        for arg in handle._args
    )


def sensor_id(hass: HomeAssistant, key: str) -> str:
    """Return the entity id of the diagnostic sensor with this key."""
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_{key}")
    assert entity_id is not None
    return entity_id


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
    assert polls(mock_device) >= 1


async def turned_on_outside_ha(air: Air) -> None:
    """The light, off, is turned on outside HA; its turn-on poll runs."""
    air.advertise(on=False)
    air.advertise(on=True)
    await air.wait(DELAY + SLACK)


async def light_service(hass: HomeAssistant, service: str, **data: Any) -> None:
    """Call a light service on the main light."""
    await hass.services.async_call(
        "light", service, {"entity_id": MAIN_LIGHT, **data}, blocking=True
    )


async def power_service(hass: HomeAssistant, sku: str, service: str) -> None:
    """Turn the light or the plug on or off from HA."""
    if sku == "H5083":
        await hass.services.async_call("switch", service, {"entity_id": PLUG}, blocking=True)
    else:
        await light_service(hass, service)


def reads_power(device: Device) -> None:
    """Make each poll read power back, as a real one does: the library then ignores adverts."""
    read = device.update.side_effect  # type: ignore[attr-defined]

    async def _update(**_kwargs: Any) -> Any:
        state = await read()
        device._mark_authoritative()
        return state

    device.update.side_effect = _update  # type: ignore[attr-defined]


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


async def test_h60a1_is_excluded_by_its_sku(
    hass: HomeAssistant,
    air: Air,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
) -> None:
    """The exclusion goes by the device's SKU (no library profile names H60A1 yet)."""
    mock_device._sku = "H60A1"
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = mock_config_entry.runtime_data.coordinator

    air.advertise(on=False)
    assert coordinator.poll_policy is PollPolicy.FIXED
    assert nominal(coordinator) == BASE


# -- off, and turned on outside HA ------------------------------------------------


async def test_light_off_gets_no_background_polls(
    air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """While its advertisements say off, a light isn't connected to at all."""
    air.advertise(on=False)
    before = polls(mock_device)
    for _ in range(6):
        await air.wait(MAX_POLL_INTERVAL_SECONDS)
    assert polls(mock_device) == before
    assert coordinator.poll_policy is PollPolicy.OFF


async def test_turn_on_outside_ha_polls_once_after_the_delay(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """One poll ADVERT_FLIP_POLL_DELAY_SECONDS after the advertisement shows it on."""
    air.advertise(on=False)
    before = polls(mock_device)
    air.advertise(on=True)
    assert coordinator.poll_policy is PollPolicy.ON
    assert turn_on_poll_pending(hass, coordinator)

    await air.wait(DELAY - 1)
    assert polls(mock_device) == before
    await air.wait(1)
    assert polls(mock_device) == before + 1
    assert not turn_on_poll_pending(hass, coordinator)

    await air.wait(BASE - SLACK)  # one poll, not a stream; the next comes at the base cadence
    assert polls(mock_device) == before + 1
    await air.wait(2 * SLACK)
    assert polls(mock_device) == before + 2


async def test_a_newer_flip_replaces_the_pending_turn_on_poll(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """Off again cancels it; on again waits the full delay from the newest flip."""
    air.advertise(on=False)
    before = polls(mock_device)
    air.advertise(on=True)
    await air.wait(DELAY - 2)
    air.advertise(on=False)
    assert not turn_on_poll_pending(hass, coordinator)
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


async def test_first_on_off_from_unknown_is_not_a_turn_on(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """The history HA replays at setup sets on/off from unknown: no extra poll for it."""
    assert mock_device.state.is_on is None
    before = polls(mock_device)
    air.advertise(on=True)
    assert coordinator.poll_policy is PollPolicy.ON
    assert not turn_on_poll_pending(hass, coordinator)
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before


async def test_lagging_advert_after_a_command_is_not_a_turn_on(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """A stale report right after HA's own command is ignored, inside the window and after it."""
    air.advertise(on=True)
    await air.wait(LAST_SEEN_RESOLUTION_SECONDS)  # well after the last poll
    await light_service(hass, "turn_off")
    assert mock_device.state.is_on is False
    assert coordinator.poll_policy is PollPolicy.OFF
    before = polls(mock_device)

    await air.wait(2, heard=False)
    air.advertise(on=False)
    air.advertise(on=True)  # a late report of the state from before the command
    assert mock_device.state.is_on is False
    assert not turn_on_poll_pending(hass, coordinator)
    # The link stays up after a command, so the device itself is silent; a
    # presence tick past the window finds that report still the newest.
    await air.wait(18, heard=False)
    coordinator.async_refresh_presence()
    assert mock_device.state.is_on is False
    assert not turn_on_poll_pending(hass, coordinator)

    await air.wait(10, heard=False)
    air.advertise(on=False)  # the link closes: it advertises what it is
    for _ in range(3):
        await air.wait(LAST_SEEN_RESOLUTION_SECONDS)
    assert mock_device.state.is_on is False
    assert polls(mock_device) == before

    air.advertise(on=True)  # long after the command: a real turn-on
    assert turn_on_poll_pending(hass, coordinator)


async def test_history_from_before_a_command_is_not_offered_again(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """While HA holds the link after a command, the history still shows the old state: kept out."""
    air.advertise(on=True)
    await air.wait(LAST_SEEN_RESOLUTION_SECONDS)  # heard well after the last poll
    await light_service(hass, "turn_off")
    await air.wait(20, heard=False)  # past the library's window, the link still up
    coordinator.async_refresh_presence()
    assert mock_device.state.is_on is False
    assert coordinator.poll_policy is PollPolicy.OFF
    assert not turn_on_poll_pending(hass, coordinator)


async def test_repeats_are_offered_again_only_after_the_librarys_window(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """A repeat is offered again only once HA received it more than the library's own
    ADVERT_SUPPRESS_SECONDS after it last talked to the device: one inside the window isn't."""
    window = lib_const.ADVERT_SUPPRESS_SECONDS
    assert window == int(window)  # whole-second steps land on either side of it
    air.advertise(on=True)
    await light_service(hass, "turn_off")
    assert coordinator._io_at == air.clock.now
    await air.wait(window - 1, heard=False)
    air.advertise(on=True)  # turned back on outside HA, received just inside the window
    assert mock_device.state.is_on is False  # the library ignored the callback's copy

    with patch.object(
        mock_device, "ingest_advertisement", wraps=mock_device.ingest_advertisement
    ) as ingest:
        coordinator.async_refresh_presence()
        ingest.assert_not_called()  # received at io_at + window - 1
        await air.wait(2)  # the device repeats it, received at io_at + window + 1
        coordinator.async_refresh_presence()
    ingest.assert_called_once()
    assert mock_device.state.is_on is True


@pytest.mark.parametrize(
    ("device_sku", "step"), [("H6008", PollPolicy.ON), ("H5083", PollPolicy.RELAY)]
)
async def test_turned_on_inside_the_window_is_taken_from_the_repeats(
    hass: HomeAssistant,
    air: Air,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    *,
    device_sku: str,
    step: PollPolicy,
) -> None:
    """Turned back on outside HA seconds after HA turned it off: the repeats correct it.

    HA calls back once for the advertisement, which the library ignores (it came
    within its window after the command); the device then keeps repeating it,
    which calls nothing back, and with no background polls nothing else would.
    """
    air.advertise(on=True)
    await power_service(hass, device_sku, "turn_off")
    air.advertise(on=False)
    before = polls(mock_device)
    await air.wait(10, heard=False)
    air.advertise(on=True)  # turned back on, inside the library's window
    assert mock_device.state.is_on is False

    await air.wait(LAST_SEEN_RESOLUTION_SECONDS + 15)
    assert mock_device.state.is_on is True
    assert coordinator.poll_policy is step
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before + (step is PollPolicy.ON)


async def test_turned_on_while_ha_held_the_link_is_taken_from_the_repeats(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """Back on before the link closed: its advertisement matches the one before the command.

    So HA never calls back for it at all; the repeats are offered again instead.
    """
    air.advertise(on=True)
    await light_service(hass, "turn_off")
    before = polls(mock_device)
    await air.wait(30, heard=False)  # the link stays up after a command: no advertisements
    await air.wait(LAST_SEEN_RESOLUTION_SECONDS)  # then "on" again, as before the command
    assert mock_device.state.is_on is True
    assert coordinator.poll_policy is PollPolicy.ON
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before + 1


async def test_turned_on_right_after_a_poll_is_taken_from_the_repeats(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """A poll that read power, then a turn-on the library ignores: corrected within a tick.

    A poll that keeps reading the opposite doesn't make it flip (and poll) every tick.
    """
    air.advertise(on=False)
    reads_power(mock_device)
    await coordinator.async_refresh()  # a one-off poll (e.g. a manual refresh)
    before = polls(mock_device)
    await air.wait(9, heard=False)
    air.advertise(on=True)
    assert mock_device.state.is_on is False

    await air.wait(LAST_SEEN_RESOLUTION_SECONDS)
    assert mock_device.state.is_on is True
    assert turn_on_poll_pending(hass, coordinator)

    read = mock_device.update.side_effect  # type: ignore[attr-defined]

    async def _reads_off(**_kwargs: Any) -> Any:
        mock_device.state.is_on = False
        return await read()

    mock_device.update.side_effect = _reads_off  # type: ignore[attr-defined]
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before + 1
    assert mock_device.state.is_on is False
    for _ in range(5):
        await air.wait(LAST_SEEN_RESOLUTION_SECONDS)
    assert mock_device.state.is_on is False
    assert polls(mock_device) == before + 1


async def test_turned_off_right_after_a_backed_off_poll_is_taken_from_the_repeats(
    air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """A light at the 15 min cap turned off just after a poll: off within a tick, not 15 min."""
    await turned_on_outside_ha(air)
    reads_power(mock_device)
    for _ in range(3):
        await coordinator.async_refresh()
    assert nominal(coordinator) == ON_POLL_MAX_INTERVAL_SECONDS
    await air.wait(3, heard=False)
    air.advertise(on=False)
    assert mock_device.state.is_on is True

    await air.wait(LAST_SEEN_RESOLUTION_SECONDS + 15)
    assert mock_device.state.is_on is False
    assert coordinator.poll_policy is PollPolicy.OFF
    assert coordinator.update_interval is None


@pytest.mark.parametrize("device_sku", ["H60A6"])  # excluded: the fixed cadence
async def test_repeats_are_not_offered_again_outside_the_policy(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """For an excluded model, only the fixed cadence's polls correct it."""
    air.advertise(on=True)
    await light_service(hass, "turn_off")
    air.advertise(on=False)
    await air.wait(10, heard=False)
    air.advertise(on=True)
    await air.wait(LAST_SEEN_RESOLUTION_SECONDS + 15)
    assert mock_device.state.is_on is False
    assert coordinator.poll_policy is PollPolicy.FIXED


async def test_unload_cancels_the_pending_turn_on_poll(
    hass: HomeAssistant,
    air: Air,
    setup_integration: MockConfigEntry,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """Unloading while the turn-on poll waits drops it: no timer outlives the entry."""
    air.advertise(on=False)
    air.advertise(on=True)
    assert turn_on_poll_pending(hass, coordinator)

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    assert not turn_on_poll_pending(hass, coordinator)
    before = polls(mock_device)
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before


async def test_a_turn_on_during_a_poll_that_reads_off_leaves_no_timer_armed(
    air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """The policy moved to on while a poll ran and back to off when it ended: no stray poll.

    HA arms its timer when the policy moves to on; at the end of the poll it
    doesn't cancel it while the interval is None, so the coordinator does.
    """
    air.advertise(on=False)
    before = polls(mock_device)
    read = mock_device.update.side_effect  # type: ignore[attr-defined]

    async def _read(**_kwargs: Any) -> Any:
        air.advertise(on=True)  # mid-read: the policy moves to on and arms HA's timer
        mock_device.state.is_on = False  # but the read, taken before, says off
        return await read()

    mock_device.update.side_effect = _read  # type: ignore[attr-defined]
    await coordinator.async_refresh()
    mock_device.update.side_effect = read  # type: ignore[attr-defined]
    assert coordinator.poll_policy is PollPolicy.OFF
    assert coordinator._unsub_refresh is None
    air.advertise(on=False)  # what it really is
    await air.wait(BASE + SLACK)
    assert polls(mock_device) == before + 1


@pytest.mark.parametrize("held", [False, True], ids=["read_at_once", "read_held"])
async def test_return_poll_stands_in_for_the_turn_on_poll(
    hass: HomeAssistant,
    air: Air,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    *,
    presence: SimpleNamespace,
    held: bool,
) -> None:
    """Back in reach and turned on in one advertisement: one poll, not a return and a turn-on poll.

    ``read_held`` keeps the return poll running past the turn-on poll's time.
    """
    air.advertise(on=False)
    presence.connectable = False
    await coordinator.async_refresh()  # skipped: no connectable scanner sees it
    before = polls(mock_device)
    gate = asyncio.Event()
    if held:
        read = mock_device.update.side_effect  # type: ignore[attr-defined]

        async def _held() -> Any:
            await gate.wait()
            return await read()

        mock_device.update.side_effect = _held  # type: ignore[attr-defined]

    presence.connectable = True
    air.advertise(on=True)
    assert not turn_on_poll_pending(hass, coordinator)
    await air.wait(DELAY + SLACK, settle=False)
    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    await air.wait(30)  # past HA's request debouncer cooldown
    assert polls(mock_device) == before + 1


async def test_return_poll_of_an_unpolled_light_starts_no_background_polls(
    hass: HomeAssistant,
    air: Air,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    presence: SimpleNamespace,
) -> None:
    """Back in reach, and a presence tick already found it off: its return poll starts none."""
    air.advertise(on=False)
    presence.connectable = False
    await coordinator.async_refresh()  # skipped: no connectable scanner sees it
    presence.connectable = True
    coordinator.async_refresh_presence()
    assert coordinator.poll_policy is PollPolicy.OFF
    before = polls(mock_device)
    gate = asyncio.Event()
    read = mock_device.update.side_effect  # type: ignore[attr-defined]

    async def _held() -> Any:
        await gate.wait()
        return await read()

    mock_device.update.side_effect = _held  # type: ignore[attr-defined]
    air.advertise(on=False)
    await asyncio.sleep(0)
    assert coordinator.nominal_interval is None  # while the return poll runs
    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert polls(mock_device) == before + 1
    assert coordinator.update_interval is None


async def test_turn_on_poll_is_dropped_after_a_rejected_secret(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """A poll rejects the secret while the turn-on poll waits: it isn't tried again."""
    air.advertise(on=False)
    air.advertise(on=True)
    assert turn_on_poll_pending(hass, coordinator)
    mock_device.update.side_effect = GoveeBleAuthError("secret rejected")  # type: ignore[attr-defined]
    before = polls(mock_device)
    await coordinator.async_refresh()
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before + 1


async def test_turn_on_poll_is_dropped_when_home_assistant_stops(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """HA's stop neither cancels the timer nor refuses the refresh: no poll of a closing link."""
    air.advertise(on=False)
    air.advertise(on=True)
    before = polls(mock_device)
    hass._cancel_cancellable_timers()  # as HA's stop does; this timer isn't one of them
    assert turn_on_poll_pending(hass, coordinator)
    hass.set_state(CoreState.stopping)
    try:
        await air.wait(DELAY + SLACK)
    finally:
        hass.set_state(CoreState.running)
    assert polls(mock_device) == before


async def test_turn_on_poll_is_dropped_while_no_entity_listens(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """Every entity disabled: like HA's own timer, the turn-on poll doesn't connect."""
    air.advertise(on=False)
    air.advertise(on=True)
    before = polls(mock_device)
    with patch.dict(coordinator._listeners, clear=True):
        await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before


async def test_a_slow_connect_while_unpolled_arms_no_poll(
    air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """A refresh of a light that's off whose connect runs out of its budget counts as a slow,
    failed poll, and arms no background poll for it."""
    air.advertise(on=False)
    assert coordinator.poll_policy is PollPolicy.OFF

    async def _update(**_kwargs: Any) -> Any:
        air.clock.now += POLL_CONNECT_TIMEOUT_SECONDS + 1
        raise GoveeBleTimeout(f"{ADDRESS}: connect exceeded {POLL_CONNECT_TIMEOUT_SECONDS}s")

    mock_device.update.side_effect = _update  # type: ignore[attr-defined]
    await coordinator.async_refresh()
    assert coordinator.slow_poll_steps == 1
    assert coordinator.consecutive_failures == 1
    assert coordinator.poll_policy is PollPolicy.OFF
    assert coordinator.update_interval is None


async def test_off_flip_stops_background_polls(
    air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """A light turned off outside HA is no longer polled."""
    await turned_on_outside_ha(air)
    assert coordinator.polls_scheduled
    air.advertise(on=False)
    assert coordinator.poll_policy is PollPolicy.OFF
    assert coordinator.update_interval is None

    before = polls(mock_device)
    for _ in range(6):
        await air.wait(MAX_POLL_INTERVAL_SECONDS)
    assert polls(mock_device) == before


async def test_commands_from_ha_start_and_stop_background_polls(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """Turned on from HA it is polled at the base cadence; turned off from HA, not at all."""
    air.advertise(on=False)
    before = polls(mock_device)
    await light_service(hass, "turn_on")
    air.advertise(on=True)
    assert coordinator.poll_policy is PollPolicy.ON
    assert nominal(coordinator) == BASE
    await air.wait(BASE + SLACK)
    assert polls(mock_device) == before + 1

    await light_service(hass, "turn_off")
    air.advertise(on=False)
    assert coordinator.poll_policy is PollPolicy.OFF
    for _ in range(6):
        await air.wait(MAX_POLL_INTERVAL_SECONDS)
    assert polls(mock_device) == before + 1


# -- on: the backoff --------------------------------------------------------------


async def test_on_backoff_doubles_to_the_cap(
    air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """Each poll that finds nothing new doubles the interval, up to 15 minutes."""
    await turned_on_outside_ha(air)
    seen = []
    for _ in range(len(ON_BACKOFF) + 1):
        interval = nominal(coordinator)
        assert interval is not None
        seen.append(interval)
        before = polls(mock_device)
        await air.wait(interval - SLACK)
        assert polls(mock_device) == before  # not before the interval
        await air.wait(2 * SLACK)
        assert polls(mock_device) == before + 1
    assert seen == [*ON_BACKOFF, ON_POLL_MAX_INTERVAL_SECONDS]
    assert nominal(coordinator) == ON_POLL_MAX_INTERVAL_SECONDS


async def test_on_backoff_restarts_when_a_poll_reads_something_new(
    coordinator: GoveeBleLocalCoordinator, air: Air, mock_device: Device
) -> None:
    """A colour, brightness or scene changed outside HA (seen by a poll) starts it over."""
    await turned_on_outside_ha(air)
    for expected in ON_BACKOFF[1:3]:
        await coordinator.async_refresh()
        assert nominal(coordinator) == expected

    mock_device.state.brightness = 42  # what the next poll reads back
    await coordinator.async_refresh()
    assert nominal(coordinator) == BASE
    await coordinator.async_refresh()
    assert nominal(coordinator) == BASE * 2


@pytest.mark.parametrize("device_sku", ["H6047"])
async def test_on_backoff_ignores_a_poll_that_only_reconfirms_the_bars(
    coordinator: GoveeBleLocalCoordinator, air: Air, mock_device: Device
) -> None:
    """A poll that reads the same bars again stamps them confirmed again: that is bookkeeping
    (``zone_power_confirmed``), not something new, so the backoff keeps doubling."""
    await turned_on_outside_ha(air)
    stamps = itertools.count(1)

    async def _update(**_kwargs: Any) -> Any:
        mock_device.state.zone_power = {0: True, 1: True}
        mock_device.state.zone_power_confirmed = dict.fromkeys((0, 1), float(next(stamps)))
        mock_device.state.optimistic = False
        return mock_device.state

    mock_device.update.side_effect = _update  # type: ignore[attr-defined]
    await coordinator.async_refresh()  # the bars read for the first time: something new
    assert nominal(coordinator) == BASE
    for expected in ON_BACKOFF[1:3]:
        await coordinator.async_refresh()
        assert nominal(coordinator) == expected


async def test_on_backoff_restarts_on_an_advertised_flip(
    air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """Off and on again outside HA: after the turn-on poll, polls at the base cadence again.

    Even when the light comes back as it was before (the turn-on poll reads the
    same as the last poll before it went off).
    """
    await turned_on_outside_ha(air)
    for _ in range(3):
        await coordinator.async_refresh()
    assert nominal(coordinator) == ON_POLL_MAX_INTERVAL_SECONDS

    air.advertise(on=False)
    air.advertise(on=True)
    assert nominal(coordinator) == BASE
    before = polls(mock_device)
    await air.wait(DELAY + SLACK)  # the turn-on poll
    assert polls(mock_device) == before + 1
    assert nominal(coordinator) == BASE
    await air.wait(BASE - SLACK)
    assert polls(mock_device) == before + 1
    await air.wait(2 * SLACK)
    assert polls(mock_device) == before + 2
    assert nominal(coordinator) == BASE * 2


async def test_on_backoff_restarts_on_a_command(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """An HA command brings the next poll forward to the base cadence (once per backoff)."""
    await turned_on_outside_ha(air)
    for _ in range(3):
        await coordinator.async_refresh()
    assert nominal(coordinator) == ON_POLL_MAX_INTERVAL_SECONDS
    before = polls(mock_device)

    await light_service(hass, "turn_on", brightness=128)
    assert nominal(coordinator) == BASE
    with patch.object(coordinator, "_schedule_refresh") as schedule:
        await light_service(hass, "turn_on", brightness=64)  # can't keep postponing it
    schedule.assert_not_called()
    await air.wait(BASE + SLACK)
    assert polls(mock_device) == before + 1


# -- failures ---------------------------------------------------------------------


@pytest.mark.parametrize("absent", [False, True], ids=["failed", "skipped"])
async def test_failure_backoff_is_unchanged_while_on(
    air: Air,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    presence: SimpleNamespace,
    absent: bool,
) -> None:
    """Failed or skipped polls of a light that is on back off as before, to 30 minutes.

    Skipped ones are skipped because no connectable scanner sees it, which puts
    it back on the fixed cadence, so they are counted as before.
    """
    await turned_on_outside_ha(air)
    mock_device.update.side_effect = GoveeBleConnectionError("no slot")  # type: ignore[attr-defined]
    presence.connectable = not absent
    seen = []
    for _ in range(len(FAILURE_BACKOFF) + 1):
        await coordinator.async_refresh()
        seen.append(nominal(coordinator))
    assert seen == [*FAILURE_BACKOFF, MAX_POLL_INTERVAL_SECONDS]
    assert coordinator.poll_policy is (PollPolicy.FIXED if absent else PollPolicy.ON)

    # Going stale meanwhile keeps the failure backoff.
    await air.wait(FRESH + LAST_SEEN_RESOLUTION_SECONDS, heard=False)
    assert coordinator.poll_policy is PollPolicy.FIXED
    assert nominal(coordinator) == MAX_POLL_INTERVAL_SECONDS


async def test_commands_drop_the_failure_backoff_once_per_streak_while_on(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """The once-per-failure-streak rule still holds for a light that is on."""
    await turned_on_outside_ha(air)
    mock_device.update.side_effect = GoveeBleConnectionError("read-back failed")  # type: ignore[attr-defined]
    for _ in range(5):
        await coordinator.async_refresh()
    await light_service(hass, "turn_on", brightness=200)
    assert nominal(coordinator) == BASE
    await coordinator.async_refresh()
    assert nominal(coordinator) == MAX_POLL_INTERVAL_SECONDS
    await light_service(hass, "turn_on", brightness=100)
    assert nominal(coordinator) == MAX_POLL_INTERVAL_SECONDS


async def test_failed_one_off_polls_of_an_off_light_start_no_background_polls(
    air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """A failing manual refresh of an off light keeps it unpolled; turned on, it backs off."""
    air.advertise(on=False)
    mock_device.update.side_effect = GoveeBleConnectionError("no slot")  # type: ignore[attr-defined]
    for _ in range(2):
        await coordinator.async_refresh()
    assert coordinator.update_interval is None
    assert coordinator.consecutive_failures == 2

    air.advertise(on=True)
    assert coordinator.poll_policy is PollPolicy.ON
    assert nominal(coordinator) == BASE * 2  # the failure backoff of the streak so far


# -- freshness --------------------------------------------------------------------


async def test_losing_the_byte_restores_the_fixed_cadence_within_one_presence_tick(
    air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """No on/off byte heard for ADVERT_POWER_FRESH_SECONDS: back to the fixed cadence.

    The tick re-arms it; the first poll follows one base interval later.
    """
    air.advertise(on=False)
    before = polls(mock_device)
    await air.wait(FRESH - 1, heard=False)
    assert coordinator.poll_policy is PollPolicy.OFF  # still fresh at that tick

    await air.wait(LAST_SEEN_RESOLUTION_SECONDS, heard=False)
    assert coordinator.poll_policy is PollPolicy.FIXED
    assert nominal(coordinator) == BASE
    await air.wait(BASE + SLACK, heard=False)
    assert polls(mock_device) == before + 1

    # Heard again (the same advertisement): no background polls again.
    air.advertise(on=False)
    assert coordinator.poll_policy is PollPolicy.OFF


async def test_backed_off_light_losing_the_byte_resumes_the_fixed_cadence(
    air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """A light backed off to 15 minutes that goes stale polls at the base cadence again."""
    await turned_on_outside_ha(air)
    for _ in range(3):
        await coordinator.async_refresh()
    assert nominal(coordinator) == ON_POLL_MAX_INTERVAL_SECONDS
    assert coordinator.consecutive_failures == 0
    before = polls(mock_device)

    await air.wait(FRESH + LAST_SEEN_RESOLUTION_SECONDS, heard=False)
    assert coordinator.poll_policy is PollPolicy.FIXED
    assert nominal(coordinator) == BASE
    await air.wait(BASE + SLACK, heard=False)
    assert polls(mock_device) == before + 1


async def test_freshness_reads_the_any_scanner_history(
    air: Air, coordinator: GoveeBleLocalCoordinator, presence: SimpleNamespace
) -> None:
    """The byte counts as heard when any scanner heard it, not only a connectable one."""
    air.advertise(on=False)
    assert coordinator.poll_policy is PollPolicy.OFF
    # The connectable scanner's entry has no manufacturer data (no scan response, say).
    presence.connectable_service_info = make_service_info(
        manufacturer_data={}, seen_at=air.clock.now
    )
    coordinator.async_refresh_presence()
    assert coordinator.poll_policy is PollPolicy.OFF


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
    assert not turn_on_poll_pending(hass, coordinator)
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
    assert not turn_on_poll_pending(hass, coordinator)
    await air.wait(DELAY + SLACK)
    assert polls(mock_device) == before + 1
    for count in (2, 3):
        await air.wait(BASE)
        assert polls(mock_device) == before + count
        assert nominal(coordinator) == BASE


# -- what polling can't follow keeps today's semantics ----------------------------


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_polling_disabled_keeps_its_semantics(
    hass: HomeAssistant, air: Air, mock_device: Device
) -> None:
    """Disabled in system options: no polls at all, and no turn-on poll either."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=ADDRESS,
        data={"address": ADDRESS, CONF_SKU: "H6008"},
        pref_disable_polling=True,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data.coordinator

    air.advertise(on=False)
    air.advertise(on=True)
    assert coordinator.poll_policy is PollPolicy.FIXED
    assert not turn_on_poll_pending(hass, coordinator)
    before = polls(mock_device)
    await air.wait(MAX_POLL_INTERVAL_SECONDS)
    assert polls(mock_device) == before
    assert hass.states.get(sensor_id(hass, "poll_interval")).state == STATE_UNKNOWN


async def test_rejected_secret_keeps_polling_halted(
    air: Air, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """A rejected secret halts polls; the policy moving on doesn't re-arm them."""
    air.advertise(on=False)
    mock_device.update.side_effect = GoveeBleAuthError("secret rejected")  # type: ignore[attr-defined]
    await coordinator.async_refresh()
    await air.wait(LAST_SEEN_RESOLUTION_SECONDS)
    assert coordinator.poll_policy is PollPolicy.FIXED
    assert not coordinator.polls_scheduled

    before = polls(mock_device)
    for _ in range(3):
        await air.wait(MAX_POLL_INTERVAL_SECONDS)
    assert polls(mock_device) == before


# -- availability -----------------------------------------------------------------


async def test_availability_follows_presence_while_unpolled(
    hass: HomeAssistant,
    air: Air,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    *,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """No polls counted failures, so leaving every connectable scanner is unavailable at once.

    Out of their reach it is back on the fixed cadence (its polls skipped and
    counted, as before), with no grace: unavailable until it is heard again.
    """
    air.advertise(on=False)
    assert hass.states.get(MAIN_LIGHT).state == "off"
    assert coordinator.poll_policy is PollPolicy.OFF

    presence.connectable = False
    presence.connectable_service_info = None  # a passive scanner still hears it (fresh byte)
    unavailable_callback(mock_bluetooth, connectable=True)(make_service_info())
    assert coordinator.poll_policy is PollPolicy.FIXED
    assert nominal(coordinator) == BASE
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE
    assert coordinator.consecutive_failures == 0

    # Its polls are skipped while it is away...
    before = polls(mock_device)
    await air.wait(BASE + SLACK)
    assert polls(mock_device) == before
    assert coordinator.consecutive_failures == 1
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE

    # ...and back in reach it is available at once, with its return poll, and unpolled again.
    presence.connectable = True
    presence.connectable_service_info = SAME_AS_ANY_SCANNER
    air.advertise(on=False)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(MAIN_LIGHT).state == "off"
    assert polls(mock_device) == before + 1
    assert coordinator.poll_policy is PollPolicy.OFF
    assert coordinator.update_interval is None


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
    assert coordinator.poll_policy is PollPolicy.FIXED
    assert hass.states.get(MAIN_LIGHT).state == "on"

    await coordinator.async_refresh()  # skipped: no connectable scanner sees it
    air.advertise(on=False)  # heard by a passive scanner
    assert mock_device.state.is_on is False
    assert coordinator.poll_policy is PollPolicy.FIXED
    assert coordinator.polls_scheduled
    assert hass.states.get(MAIN_LIGHT).state == "off"

    for _ in range(UNAVAILABLE_AFTER_FAILURES - 1):
        await coordinator.async_refresh()
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE


async def test_polls_resuming_for_an_absent_device_keep_it_unavailable(
    hass: HomeAssistant,
    air: Air,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    *,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """Gone from every scanner in one sweep: unavailable, and not available again meanwhile."""
    air.advertise(on=False)
    presence.connectable = False
    presence.connectable_service_info = None
    unavailable_callback(mock_bluetooth, connectable=True)(make_service_info())
    assert coordinator.poll_policy is PollPolicy.FIXED
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE

    # The same sweep then drops the any-scanner history: the byte is gone too.
    presence.any = False
    presence.service_info = None
    unavailable_callback(mock_bluetooth, connectable=False)(make_service_info())
    assert coordinator.poll_policy is PollPolicy.FIXED
    assert coordinator.polls_scheduled
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE

    before = polls(mock_device)
    await air.wait(BASE + SLACK, heard=False)  # skipped: it can't be reached
    assert coordinator.consecutive_failures == 1
    assert polls(mock_device) == before
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE

    presence.connectable = presence.any = True
    presence.connectable_service_info = SAME_AS_ANY_SCANNER
    air.advertise(on=False)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(MAIN_LIGHT).state == "off"
    assert polls(mock_device) == before + 1  # the return poll
    assert coordinator.poll_policy is PollPolicy.OFF


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
) -> None:
    """A device doesn't advertise while HA holds its link, so commands under 30 s apart
    outlast HA's history of it. Each command that lands shows it reachable, and its polls run
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
        await light_service(hass, "turn_on", brightness=10 + step % 80)
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


async def test_diagnostics_name_the_policy_step(
    hass: HomeAssistant, air: Air, coordinator: GoveeBleLocalCoordinator
) -> None:
    """The downloadable diagnostics say which step a device is on, and why it isn't polled."""
    air.advertise(on=False)
    diagnostics = await async_get_config_entry_diagnostics(hass, coordinator.config_entry)
    assert diagnostics["coordinator"]["poll_policy"] == "off"
    assert diagnostics["coordinator"]["update_interval"] == "None"
    assert [step.value for step in PollPolicy] == ["fixed", "on", "off", "relay"]
