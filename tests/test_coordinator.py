"""Tests for the polling coordinator: backoff, reachability, scheduling and notifications."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from datetime import timedelta
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from bleak.exc import BleakError
from govee_ble_local import (
    Device,
    GoveeBleAuthError,
    GoveeBleConnectionError,
    GoveeBleHandshakeError,
    GoveeBleNotSupported,
    GoveeBleTimeout,
)
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.light import DATA_COMPONENT as LIGHT_DATA
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.govee_ble_local.const import (
    DOMAIN,
    LAST_SEEN_RESOLUTION_SECONDS,
    MAX_CONCURRENT_POLLS,
    POLL_INTERVAL_SECONDS,
    UNAVAILABLE_AFTER_FAILURES,
)
from custom_components.govee_ble_local.coordinator import GoveeBleLocalCoordinator

from .conftest import (
    advertisement_callback,
    allow_adverts,
    make_device,
    make_service_info,
)
from .const import ADDRESS

MAIN_LIGHT = "light.test_ceiling"
JITTER_TARGET = "custom_components.govee_ble_local.coordinator.random.uniform"
BASE = POLL_INTERVAL_SECONDS
BACKOFF_SEQUENCE = [BASE, BASE * 2, BASE * 4, BASE * 8, 1800, 1800]

# The conftest ``presence`` fixture replaces these on the class for every test.
REAL_ADDRESS_PRESENT = GoveeBleLocalCoordinator.address_present
REAL_LAST_SERVICE_INFO = GoveeBleLocalCoordinator.last_service_info

ERRORS: list[Callable[[], BaseException]] = [
    lambda: GoveeBleConnectionError("no slot"),
    lambda: GoveeBleTimeout("timed out"),
    lambda: GoveeBleHandshakeError("bad handshake"),
    lambda: GoveeBleNotSupported("nope"),
    lambda: BleakError("link dropped"),
    TimeoutError,
]
ERROR_IDS = ["connection", "timeout", "handshake", "not_supported", "bleak", "builtin_timeout"]


@pytest.fixture
def jitter() -> Generator[MagicMock]:
    """Make the poll jitter deterministic (0 seconds unless a test changes it)."""
    with patch(JITTER_TARGET, return_value=0) as uniform:
        yield uniform


@pytest.fixture
async def coordinator(
    jitter: MagicMock, setup_integration: MockConfigEntry
) -> GoveeBleLocalCoordinator:
    """The loaded entry's coordinator (first poll done, jitter patched to 0)."""
    return setup_integration.runtime_data.coordinator


def succeed(device: Device, *, optimistic: bool = False) -> None:
    """Make the device's next polls succeed, optionally returning optimistic state."""

    async def _update() -> Any:
        device.state.optimistic = optimistic
        return device.state

    device.update.side_effect = _update  # type: ignore[attr-defined]


def fail(device: Device, error: BaseException) -> None:
    """Make the device's polls raise ``error``."""
    device.update.side_effect = error  # type: ignore[attr-defined]


def interval(coordinator: GoveeBleLocalCoordinator) -> float:
    """Return the coordinator's current poll interval in seconds."""
    assert coordinator.update_interval is not None
    return coordinator.update_interval.total_seconds()


async def advance(hass: HomeAssistant, seconds: float) -> None:
    """Fire the timers due ``seconds`` from now and let the resulting work finish."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def light_entity(hass: HomeAssistant, entity_id: str = MAIN_LIGHT) -> Any:
    """Return the live light entity object."""
    entity = hass.data[LIGHT_DATA].get_entity(entity_id)
    assert entity is not None
    return entity


# -- polling outcome ------------------------------------------------------------


async def test_successful_poll_resets_state_and_schedules_base_interval(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    jitter: MagicMock,
    freezer: Any,
) -> None:
    """A read-back poll clears failures, stamps last_connected and re-arms base + jitter."""
    fail(mock_device, GoveeBleConnectionError("no slot"))
    for _ in range(3):
        await coordinator.async_refresh()
    assert coordinator.consecutive_failures == 3
    assert interval(coordinator) == BASE * 4

    jitter.return_value = 17
    freezer.tick(timedelta(seconds=500))
    succeed(mock_device)
    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.consecutive_failures == 0
    assert coordinator.total_failures == 3  # cumulative: never reset
    assert interval(coordinator) == BASE + 17
    assert coordinator.last_connected == dt_util.utcnow()


async def test_optimistic_state_does_not_count_as_connected(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    jitter: MagicMock,
) -> None:
    """A poll that only echoes optimistic state succeeds but never sets last_connected."""
    succeed(mock_device, optimistic=True)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data.coordinator
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    assert coordinator.last_update_success
    assert coordinator.consecutive_failures == 0
    assert coordinator.last_connected is None

    succeed(mock_device)
    await coordinator.async_refresh()
    assert coordinator.last_connected is not None


@pytest.mark.parametrize("make_error", ERRORS, ids=ERROR_IDS)
async def test_poll_failures_are_counted_and_back_off(
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    make_error: Callable[[], BaseException],
) -> None:
    """Every BLE-class error counts as a failed poll and doubles the interval."""
    fail(mock_device, make_error())
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert (coordinator.consecutive_failures, coordinator.total_failures) == (1, 1)
    assert interval(coordinator) == BASE  # first failure keeps the base cadence

    await coordinator.async_refresh()
    assert (coordinator.consecutive_failures, coordinator.total_failures) == (2, 2)
    assert interval(coordinator) == BASE * 2


async def test_backoff_sequence_is_capped(
    coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """Backoff doubles from the base cadence to the 1800 s cap, then holds; success resets."""
    fail(mock_device, GoveeBleConnectionError("no slot"))
    seen = []
    for _ in BACKOFF_SEQUENCE:
        await coordinator.async_refresh()
        seen.append(interval(coordinator))
    assert seen == BACKOFF_SEQUENCE

    for _ in range(40):  # far beyond the doubling range: no overflow, still capped
        await coordinator.async_refresh()
    assert interval(coordinator) == 1800

    succeed(mock_device)
    await coordinator.async_refresh()
    assert interval(coordinator) == BASE
    assert coordinator.consecutive_failures == 0


async def test_jitter_is_added_after_the_cap(
    coordinator: GoveeBleLocalCoordinator, mock_device: Device, jitter: MagicMock
) -> None:
    """Capped devices still spread out: jitter rides on top of the cap, not inside it."""
    jitter.return_value = 30
    fail(mock_device, GoveeBleConnectionError("no slot"))
    for _ in range(6):
        await coordinator.async_refresh()
    assert interval(coordinator) == 1800 + 30

    succeed(mock_device)
    await coordinator.async_refresh()
    assert interval(coordinator) == BASE + 30


async def test_unexpected_error_still_backs_off_and_logs_traceback(
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A library bug is logged with its traceback but handled like any failed poll."""
    fail(mock_device, ValueError("boom"))
    with caplog.at_level(logging.ERROR, logger="custom_components.govee_ble_local.coordinator"):
        await coordinator.async_refresh()
        await coordinator.async_refresh()

    records = [r for r in caplog.records if "Unexpected error polling" in r.getMessage()]
    assert len(records) == 2
    assert records[0].exc_info is not None
    assert isinstance(records[0].exc_info[1], ValueError)
    assert (coordinator.consecutive_failures, coordinator.total_failures) == (2, 2)
    assert interval(coordinator) == BASE * 2
    assert "boom" in str(coordinator.last_exception)


async def test_auth_error_starts_reauth_and_stops_polling(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """A rejected secret starts reauth, isn't a poll failure and cancels further polling."""
    fail(mock_device, GoveeBleAuthError("secret rejected"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert any(
        flow["context"]["source"] == "reauth"
        and flow["context"]["entry_id"] == setup_integration.entry_id
        for flow in flows
    )
    assert (coordinator.consecutive_failures, coordinator.total_failures) == (0, 0)
    assert not coordinator.last_update_success

    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    await advance(hass, 3 * 3600)
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]


# -- reachability ---------------------------------------------------------------


async def test_poll_skipped_while_not_advertising(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    presence: SimpleNamespace,
) -> None:
    """With no connectable scanner hearing it the poll is skipped, not counted as a BLE error."""
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    presence.connectable = False
    for expected in (1, 2, 3, 4):
        await coordinator.async_refresh()
        assert coordinator.consecutive_failures == expected
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]
    assert coordinator.total_failures == 0
    assert not coordinator.last_update_success
    assert interval(coordinator) == BASE * 8  # skips back off like failures


async def test_device_unavailable_after_enough_skipped_polls(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    presence: SimpleNamespace,
) -> None:
    """A vanished device goes unavailable only after UNAVAILABLE_AFTER_FAILURES skips."""
    presence.connectable = presence.any = False
    for _ in range(UNAVAILABLE_AFTER_FAILURES - 1):
        await coordinator.async_refresh()
        assert coordinator.device_available
        assert hass.states.get(MAIN_LIGHT).state != STATE_UNAVAILABLE

    await coordinator.async_refresh()
    assert not coordinator.device_available
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE


async def test_present_device_stays_available_through_failed_polls(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """Failing polls never make an advertising device unavailable."""
    fail(mock_device, GoveeBleConnectionError("no slot"))
    for _ in range(UNAVAILABLE_AFTER_FAILURES + 7):
        await coordinator.async_refresh()
    assert coordinator.consecutive_failures == UNAVAILABLE_AFTER_FAILURES + 7
    assert coordinator.device_available
    assert hass.states.get(MAIN_LIGHT).state != STATE_UNAVAILABLE


async def test_failure_count_alone_does_not_decide_availability(
    coordinator: GoveeBleLocalCoordinator, presence: SimpleNamespace
) -> None:
    """Availability needs absence AND failures: either one alone keeps the device available."""
    coordinator.consecutive_failures = UNAVAILABLE_AFTER_FAILURES
    assert coordinator.device_available  # still present
    presence.connectable = False
    assert not coordinator.device_available
    coordinator.consecutive_failures = UNAVAILABLE_AFTER_FAILURES - 1
    assert coordinator.device_available  # absent, but not for long enough yet


async def test_return_seen_only_by_non_connectable_scanner_does_not_poll(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """Only a connectable sighting triggers the immediate return poll."""
    presence.connectable = presence.any = False
    for _ in range(4):
        await coordinator.async_refresh()
    backed_off = interval(coordinator)
    assert backed_off == BASE * 8
    mock_device.update.reset_mock()  # type: ignore[attr-defined]

    # A passive-only scanner hears it again: can't connect, so no poll, no reset.
    presence.any = True
    advertisement_callback(mock_bluetooth)(
        make_service_info(connectable=False), BluetoothChange.ADVERTISEMENT
    )
    await hass.async_block_till_done()
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]
    assert interval(coordinator) == backed_off
    assert coordinator.consecutive_failures == 4

    # Now a connectable scanner hears it: poll immediately and drop the backoff.
    presence.connectable = True
    advertisement_callback(mock_bluetooth)(make_service_info(), BluetoothChange.ADVERTISEMENT)
    await hass.async_block_till_done()
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    assert coordinator.consecutive_failures == 0
    assert interval(coordinator) == BASE

    # The return poll is one-shot: later adverts don't poll again.
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    advertisement_callback(mock_bluetooth)(make_service_info(), BluetoothChange.ADVERTISEMENT)
    await hass.async_block_till_done()
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]


# -- concurrency ----------------------------------------------------------------


async def test_poll_concurrency_is_capped_across_devices(hass: HomeAssistant) -> None:
    """Simultaneous polls from different coordinators share MAX_CONCURRENT_POLLS slots."""
    release = asyncio.Event()
    at_cap = asyncio.Event()
    running = 0
    max_running = 0
    completed: list[str] = []

    def _make(index: int) -> GoveeBleLocalCoordinator:
        address = f"AA:BB:CC:DD:EE:{index:02X}"
        device = make_device(address=address)

        async def _update() -> Any:
            nonlocal running, max_running
            running += 1
            max_running = max(max_running, running)
            if running >= MAX_CONCURRENT_POLLS:
                at_cap.set()
            try:
                await release.wait()
            finally:
                running -= 1
            completed.append(address)
            device.state.optimistic = False
            return device.state

        device.update.side_effect = _update  # type: ignore[attr-defined]
        entry = MockConfigEntry(domain=DOMAIN, unique_id=address, data={"address": address})
        entry.add_to_hass(hass)
        return GoveeBleLocalCoordinator(hass, entry, device)

    coordinators = [_make(0x10 + i) for i in range(MAX_CONCURRENT_POLLS + 1)]
    refreshes = [asyncio.ensure_future(c.async_refresh()) for c in coordinators]

    await asyncio.wait_for(at_cap.wait(), timeout=5)
    for _ in range(10):  # give the over-cap poll every chance to (wrongly) start
        await asyncio.sleep(0)
    assert running == MAX_CONCURRENT_POLLS
    assert max_running == MAX_CONCURRENT_POLLS
    assert completed == []

    release.set()
    await asyncio.gather(*refreshes)
    assert max_running == MAX_CONCURRENT_POLLS
    assert len(completed) == len(coordinators)
    assert all(c.last_update_success and c.consecutive_failures == 0 for c in coordinators)


# -- scheduling -----------------------------------------------------------------


async def test_scheduler_polls_at_the_base_interval(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """After setup the next poll fires when the base interval elapses, not before."""
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    await advance(hass, BASE - 10)
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]

    await advance(hass, BASE + 10)
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    assert coordinator.last_update_success


async def test_scheduler_fires_at_the_backed_off_interval(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """After failures the next poll waits for the longer interval."""
    fail(mock_device, GoveeBleConnectionError("no slot"))
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert interval(coordinator) == BASE * 2
    mock_device.update.reset_mock()  # type: ignore[attr-defined]

    await advance(hass, BASE * 2 - 10)  # well past the base interval, short of the backoff
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]

    await advance(hass, BASE * 2 + 10)
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    assert coordinator.consecutive_failures == 3
    assert interval(coordinator) == BASE * 4


# -- notifications --------------------------------------------------------------


async def test_publish_device_state_updates_entities_without_touching_the_poll_timer(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """Passive publishes write entity state but neither reschedule nor mask a failing poll."""
    fail(mock_device, GoveeBleConnectionError("no slot"))
    await coordinator.async_refresh()
    assert not coordinator.last_update_success

    mock_device.state.is_on = True
    with patch.object(coordinator, "_schedule_refresh") as schedule:
        coordinator.async_publish_device_state()
    schedule.assert_not_called()
    assert hass.states.get(MAIN_LIGHT).state == "on"
    assert coordinator.data is mock_device.state
    assert not coordinator.last_update_success

    mock_device.state.is_on = False
    coordinator.async_publish_device_state()
    assert hass.states.get(MAIN_LIGHT).state == "off"


async def test_unchanged_advertisement_does_not_rewrite_main_light(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    freezer: Any,
) -> None:
    """An unchanged on/off never rewrites the light; diagnostics follow a 60 s cadence."""
    allow_adverts(mock_device)
    callback = advertisement_callback(mock_bluetooth)
    callback(make_service_info(on=True, rssi=-70), BluetoothChange.ADVERTISEMENT)
    assert hass.states.get(MAIN_LIGHT).state == "on"
    registry = er.async_get(hass)
    rssi_id = registry.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_rssi")
    assert hass.states.get(rssi_id).state == "-70"

    with patch.object(light_entity(hass), "async_write_ha_state") as write:
        freezer.tick(timedelta(seconds=5))
        callback(make_service_info(on=True, rssi=-80), BluetoothChange.ADVERTISEMENT)
        write.assert_not_called()
        # Inside the resolution window the diagnostics aren't republished either.
        assert hass.states.get(rssi_id).state == "-70"
        assert coordinator.rssi == -80  # but the coordinator tracks it

        freezer.tick(timedelta(seconds=LAST_SEEN_RESOLUTION_SECONDS + 1))
        callback(make_service_info(on=True, rssi=-85), BluetoothChange.ADVERTISEMENT)
        write.assert_not_called()  # the light is still not rewritten...
    assert hass.states.get(rssi_id).state == "-85"  # ...but the diagnostics are
    last_seen_id = registry.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_last_seen")
    assert dt_util.parse_datetime(hass.states.get(last_seen_id).state) == dt_util.utcnow().replace(
        microsecond=0
    )


async def test_diagnostics_keep_publishing_during_a_steady_advertisement_stream(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    freezer: Any,
) -> None:
    """A device advertising every 20 s still refreshes last seen / RSSI roughly once a minute."""
    allow_adverts(mock_device)
    callback = advertisement_callback(mock_bluetooth)
    registry = er.async_get(hass)
    rssi_id = registry.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_rssi")
    callback(make_service_info(rssi=-70), BluetoothChange.ADVERTISEMENT)
    assert hass.states.get(rssi_id).state == "-70"
    for _ in range(4):  # 80 s of adverts, each well inside the 60 s resolution
        freezer.tick(timedelta(seconds=20))
        callback(make_service_info(rssi=-90), BluetoothChange.ADVERTISEMENT)
    assert hass.states.get(rssi_id).state == "-90"


async def test_on_off_change_from_advertisement_rewrites_main_light(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """A real on/off change is pushed to the light immediately."""
    allow_adverts(mock_device)
    callback = advertisement_callback(mock_bluetooth)
    callback(make_service_info(on=True), BluetoothChange.ADVERTISEMENT)
    with patch.object(light_entity(hass), "async_write_ha_state") as write:
        callback(make_service_info(on=False), BluetoothChange.ADVERTISEMENT)
    write.assert_called_once()


async def test_availability_flip_pushes_to_every_entity(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """Going unavailable, and coming back, reaches every entity, not just the diagnostics."""
    registry = er.async_get(hass)
    entities = [
        entry.entity_id
        for entry in er.async_entries_for_config_entry(registry, coordinator.config_entry.entry_id)
        if entry.disabled_by is None and entry.entity_category is None
    ]
    assert len(entities) == 3  # the three lights

    mock_device.state.is_on = True  # so the returning advertisement carries no on/off change
    coordinator.async_publish_device_state()
    presence.connectable = presence.any = False
    for _ in range(UNAVAILABLE_AFTER_FAILURES):
        await coordinator.async_refresh()
    assert all(hass.states.get(e).state == STATE_UNAVAILABLE for e in entities)

    # The device returns; hang the immediate return poll so only the advertisement's
    # own availability push can bring the entities back.
    gate = asyncio.Event()

    async def _slow_update() -> Any:
        await gate.wait()
        mock_device.state.optimistic = False
        return mock_device.state

    mock_device.update.side_effect = _slow_update  # type: ignore[attr-defined]
    presence.connectable = presence.any = True
    allow_adverts(mock_device)
    advertisement_callback(mock_bluetooth)(
        make_service_info(on=True), BluetoothChange.ADVERTISEMENT
    )
    assert all(hass.states.get(e).state != STATE_UNAVAILABLE for e in entities)
    assert coordinator.consecutive_failures == UNAVAILABLE_AFTER_FAILURES  # poll still in flight

    gate.set()
    await hass.async_block_till_done()
    assert coordinator.consecutive_failures == 0


async def test_failed_poll_with_unchanged_availability_notifies_diagnostics_only(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """Repeated failures on a reachable device refresh the diagnostics, not the other entities."""
    registry = er.async_get(hass)
    failures_id = registry.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_connection_failures")
    interval_id = registry.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_poll_interval")
    fail(mock_device, GoveeBleConnectionError("no slot"))
    # HA itself notifies everyone when the first failure flips last_update_success.
    await coordinator.async_refresh()

    with patch.object(light_entity(hass), "async_write_ha_state") as write:
        await coordinator.async_refresh()
        await coordinator.async_refresh()
    write.assert_not_called()
    assert hass.states.get(failures_id).state == "3"
    assert hass.states.get(interval_id).state == str(BASE * 4)


async def test_diagnostic_listener_unsubscribe(coordinator: GoveeBleLocalCoordinator) -> None:
    """The unsubscribe callback stops further diagnostic notifications."""
    seen: list[str] = []
    other: list[str] = []
    unsubscribe = coordinator.async_add_diagnostic_listener(lambda: seen.append("a"))
    coordinator.async_add_diagnostic_listener(lambda: other.append("b"))

    coordinator.async_update_diagnostics()
    assert (seen, other) == (["a"], ["b"])

    unsubscribe()
    coordinator.async_update_diagnostics()
    assert (seen, other) == (["a"], ["b", "b"])


async def test_unavailable_callback_clears_rssi_only_when_nothing_hears_it(
    coordinator: GoveeBleLocalCoordinator,
) -> None:
    """Losing the connectable history keeps the RSSI; losing every scanner clears it."""
    coordinator.rssi = -64
    coordinator.async_handle_unavailable(connectable=True)
    assert coordinator.rssi == -64
    coordinator.async_handle_unavailable(connectable=False)
    assert coordinator.rssi is None


async def test_real_bluetooth_helpers_defer_to_the_stack(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_bluetooth: SimpleNamespace
) -> None:
    """Without the presence fixture the coordinator asks the Bluetooth stack directly."""
    coordinator = setup_integration.runtime_data.coordinator
    with (
        patch.object(GoveeBleLocalCoordinator, "address_present", REAL_ADDRESS_PRESENT),
        patch.object(GoveeBleLocalCoordinator, "last_service_info", REAL_LAST_SERVICE_INFO),
    ):
        assert coordinator.address_present() is False  # no scanner has seen it
        assert coordinator.address_present(connectable=False) is False
        assert coordinator.last_service_info() is mock_bluetooth.last_info.return_value
    assert mock_bluetooth.last_info.call_args.kwargs == {"connectable": True}
