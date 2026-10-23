"""Tests for the polling coordinator: backoff, reachability, scheduling and notifications."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from datetime import timedelta
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from govee_ble_local import (
    Device,
    GoveeBleAuthError,
    GoveeBleConnectionError,
    GoveeBleHandshakeError,
    GoveeBleNotSupported,
    GoveeBleTimeout,
    const as lib_const,
    create_device,
)
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.light import DATA_COMPONENT as LIGHT_DATA
from homeassistant.const import STATE_OFF, STATE_UNAVAILABLE
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.govee_ble_local.const import (
    CONF_SKU,
    DOMAIN,
    LAST_SEEN_RESOLUTION_SECONDS,
    MAX_CONCURRENT_POLLS,
    MAX_POLL_INTERVAL_SECONDS,
    POLL_CONNECT_ATTEMPTS,
    POLL_CONNECT_TIMEOUT_SECONDS,
    POLL_HOLDOFF_AFTER_COMMAND_SECONDS,
    POLL_HOLDOFF_MAX_SECONDS,
    POLL_INTERVAL_SECONDS,
    SLOW_POLL_MAX_STEPS,
    SLOW_POLL_SECONDS,
    UNAVAILABLE_AFTER_FAILURES,
    UNAVAILABLE_AFTER_UNHEARD_SECONDS,
    WEAK_SIGNAL_RSSI,
)
from custom_components.govee_ble_local.coordinator import (
    GoveeBleLocalCoordinator,
    PollPolicy,
    _poll_semaphore,
)

from .conftest import (
    FakeConnection,
    advance,
    advertisement_callback,
    allow_adverts,
    connection,
    make_device,
    make_service_info,
    unavailable_callback,
)
from .const import ADDRESS, TITLE

MAIN_LIGHT = "light.test_ceiling"
JITTER_TARGET = "custom_components.govee_ble_local.coordinator.random.uniform"
BASE = POLL_INTERVAL_SECONDS
BACKOFF_SEQUENCE = [BASE, BASE * 2, BASE * 4, BASE * 8, 1800, 1800]

# The conftest ``presence`` fixture replaces these on the class for every test.
REAL_ADDRESS_PRESENT = GoveeBleLocalCoordinator.async_address_present
REAL_LAST_SERVICE_INFO = GoveeBleLocalCoordinator.async_last_service_info

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

    async def _update(**_kwargs: Any) -> Any:
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


async def test_a_poll_connects_with_its_own_tighter_budget(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """A poll passes its connect budget, so a device that doesn't answer frees the lock sooner."""
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    await coordinator.async_refresh()
    mock_device.update.assert_awaited_once_with(  # type: ignore[attr-defined]
        connect_attempts=POLL_CONNECT_ATTEMPTS, connect_timeout=POLL_CONNECT_TIMEOUT_SECONDS
    )


async def test_the_library_takes_the_poll_connect_budget() -> None:
    """The installed library's real update() accepts the budget the coordinator passes.

    The coordinator tests mock update(), and a poll that raises is only logged, so without
    this a library lacking the keywords would fail every poll unnoticed.
    """
    device = create_device(
        BLEDevice(address=ADDRESS, name="GVH60A6ABCD", details={}), "H60A6", None
    )
    device._conn = FakeConnection()  # type: ignore[assignment]
    await device.update(
        connect_attempts=POLL_CONNECT_ATTEMPTS, connect_timeout=POLL_CONNECT_TIMEOUT_SECONDS
    )


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
    await hass.async_block_till_done(wait_background_tasks=True)

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
    error = coordinator.last_exception
    assert isinstance(error, UpdateFailed)
    assert error.translation_key == "poll_failed"
    assert error.translation_placeholders == {"address": ADDRESS, "error": "ValueError('boom')"}
    assert "boom" in str(error)


@pytest.mark.parametrize(
    ("error", "message"),
    [(GoveeBleConnectionError("no slot"), "no slot"), (TimeoutError(), "TimeoutError")],
    ids=["library", "bare_timeout"],
)
async def test_failed_poll_error_is_translated(
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    error: BaseException,
    message: str,
) -> None:
    """A failed poll raises a translated UpdateFailed naming the device and the error."""
    fail(mock_device, error)
    await coordinator.async_refresh()

    last = coordinator.last_exception
    assert isinstance(last, UpdateFailed)
    assert (last.translation_domain, last.translation_key) == (DOMAIN, "poll_failed")
    assert last.translation_placeholders == {"address": ADDRESS, "error": message}
    assert str(last) == f"Could not poll {ADDRESS}: {message}"


async def test_skipped_poll_error_is_translated(
    coordinator: GoveeBleLocalCoordinator, presence: SimpleNamespace
) -> None:
    """A poll skipped for absence raises a translated UpdateFailed."""
    presence.connectable = False
    await coordinator.async_refresh()

    last = coordinator.last_exception
    assert isinstance(last, UpdateFailed)
    assert (last.translation_domain, last.translation_key) == (DOMAIN, "poll_skipped_absent")
    assert last.translation_placeholders == {"address": ADDRESS}
    assert str(last) == f"{ADDRESS} is not advertising; poll skipped"


async def test_auth_error_starts_reauth_and_stops_polling(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    setup_integration: MockConfigEntry,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """A rejected secret starts reauth, isn't a poll failure and cancels further polling.

    Home Assistant's own ERROR for the failed refresh names the device (the coordinator is named
    after its address), so it reaches the diagnostics' recent warnings, scrubbed.
    """
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
    error = coordinator.last_exception
    assert isinstance(error, ConfigEntryAuthFailed)
    assert (error.translation_domain, error.translation_key) == (DOMAIN, "secret_rejected")
    assert error.translation_placeholders == {"device_name": TITLE}
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, setup_integration)
    (line,) = diagnostics["recent_warnings"]
    assert line.startswith(
        "ERROR Authentication failed while fetching govee_ble_local_**REDACTED**"
    )

    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    await advance(hass, 3 * 3600)
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]


async def test_vanishing_after_auth_failure_makes_the_device_unavailable(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """With polls halted by a rejected secret, leaving range is enough to go unavailable."""
    fail(mock_device, GoveeBleAuthError("secret rejected"))
    await coordinator.async_refresh()
    assert hass.states.get(MAIN_LIGHT).state != STATE_UNAVAILABLE  # still advertising

    presence.connectable = presence.any = False
    unavailable_callback(mock_bluetooth, connectable=True)(make_service_info())
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE

    # A poll that does run again (here a manual refresh) re-arms polling, and with
    # it the usual grace of a few failed polls.
    await coordinator.async_refresh()
    assert coordinator.consecutive_failures == 1
    assert coordinator.device_available


async def test_vanishing_with_polling_disabled_makes_the_device_unavailable(
    hass: HomeAssistant,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """With polling turned off nothing counts failures: absence alone decides availability."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=ADDRESS,
        data={"address": ADDRESS, CONF_SKU: "H60A6"},
        pref_disable_polling=True,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(MAIN_LIGHT).state != STATE_UNAVAILABLE

    presence.connectable = presence.any = False
    unavailable_callback(mock_bluetooth, connectable=True)(make_service_info())
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE

    presence.connectable = presence.any = True
    advertisement_callback(mock_bluetooth)(make_service_info(), BluetoothChange.ADVERTISEMENT)
    assert hass.states.get(MAIN_LIGHT).state != STATE_UNAVAILABLE


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
    """Within its unheard grace, a vanished device goes unavailable only after
    UNAVAILABLE_AFTER_FAILURES skips."""
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
    """Within its unheard grace, availability needs absence AND failures: either one alone
    keeps the device available."""
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


async def _turn_on(hass: HomeAssistant) -> None:
    await hass.services.async_call("light", "turn_on", {"entity_id": MAIN_LIGHT}, blocking=True)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_successful_command_drops_the_poll_backoff(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """A command that lands brings a backed-off poll forward to the base cadence."""
    interval_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{ADDRESS}_poll_interval"
    )
    fail(mock_device, GoveeBleConnectionError("no slot"))
    for _ in range(5):
        await coordinator.async_refresh()
    assert interval(coordinator) == 1800
    succeed(mock_device)
    mock_device.update.reset_mock()  # type: ignore[attr-defined]

    await _turn_on(hass)
    assert interval(coordinator) == BASE
    assert hass.states.get(interval_id).state == str(BASE)
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]  # no extra poll
    assert coordinator.consecutive_failures == 5  # left for the poll to reset

    await advance(hass, BASE + 10)
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    assert coordinator.consecutive_failures == 0


async def test_failed_or_repeated_commands_leave_the_poll_schedule_alone(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    jitter: MagicMock,
) -> None:
    """A failed command keeps the backoff; once reset, further commands don't re-arm the poll."""
    jitter.return_value = 30
    fail(mock_device, GoveeBleConnectionError("no slot"))
    for _ in range(5):
        await coordinator.async_refresh()
    connection(mock_device).error = GoveeBleConnectionError("link down")
    with pytest.raises(HomeAssistantError):
        await _turn_on(hass)
    assert interval(coordinator) == 1800 + 30

    connection(mock_device).error = None
    await _turn_on(hass)
    # The jitter on top of the base cadence isn't mistaken for a backoff.
    assert interval(coordinator) == BASE + 30
    with patch.object(coordinator, "_schedule_refresh") as schedule:
        await _turn_on(hass)  # would otherwise keep postponing the poll
    schedule.assert_not_called()


async def test_commands_drop_the_backoff_once_per_failure_streak(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """Polls that keep failing while commands land aren't held at the base cadence."""
    fail(mock_device, GoveeBleConnectionError("read-back failed"))
    for _ in range(5):
        await coordinator.async_refresh()
    await _turn_on(hass)
    assert interval(coordinator) == BASE
    await advance(hass, BASE + 10)  # the poll brought forward fails again
    assert interval(coordinator) == 1800

    with patch.object(coordinator, "_schedule_refresh") as schedule:
        await _turn_on(hass)
    schedule.assert_not_called()
    assert interval(coordinator) == 1800

    # A successful poll ends the streak, so the next one can be cut short again.
    succeed(mock_device)
    await coordinator.async_refresh()
    fail(mock_device, GoveeBleConnectionError("read-back failed"))
    for _ in range(5):
        await coordinator.async_refresh()
    assert interval(coordinator) == 1800
    await _turn_on(hass)
    assert interval(coordinator) == BASE


async def test_command_after_a_rejected_secret_does_not_restart_polling(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """HA stops polling on a rejected secret; a command that lands doesn't re-arm the timer."""
    fail(mock_device, GoveeBleConnectionError("no slot"))
    for _ in range(3):
        await coordinator.async_refresh()
    assert interval(coordinator) == BASE * 4
    fail(mock_device, GoveeBleAuthError("secret rejected"))
    await coordinator.async_refresh()
    assert not coordinator.polls_scheduled

    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    await _turn_on(hass)
    await advance(hass, 3 * 3600)
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

        async def _update(**_kwargs: Any) -> Any:
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


# -- polls give way to commands ---------------------------------------------------

HOLDOFF = POLL_HOLDOFF_AFTER_COMMAND_SECONDS


async def test_scheduled_poll_waits_for_a_running_command_and_the_hold_off(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """A poll that comes due mid-command runs only once the hold-off after it has passed."""
    gate = asyncio.Event()

    async def _slow_turn_on(*_args: Any, **_kwargs: Any) -> None:
        await gate.wait()

    mock_device.turn_on.side_effect = _slow_turn_on  # type: ignore[attr-defined]
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    # Not a Home Assistant task: async_block_till_done would wait for the held command.
    command = asyncio.create_task(_turn_on(hass))
    await _until(lambda: coordinator.commands_holding_off)

    # HA's timer fires while the command still runs (within its 60 s deadline).
    coordinator._set_poll_interval(timedelta(seconds=20))
    coordinator._schedule_refresh()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=25))
    await _until(lambda: coordinator._poll_waiting)
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]

    gate.set()
    await command
    await advance(hass, HOLDOFF - 5)  # inside the hold-off after it
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]
    assert coordinator.commands_holding_off

    await advance(hass, HOLDOFF + 1)
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    assert not coordinator.commands_holding_off
    assert coordinator.last_update_success


def test_hold_off_ends_while_the_command_link_is_still_up() -> None:
    """The held poll runs a few seconds before the library drops a command's link (its 30 s upper
    bound; without the library's keepalive heartbeat, many models end it sooner)."""
    assert lib_const.ACTIVE_IDLE_DELAY - 10 <= HOLDOFF < lib_const.ACTIVE_IDLE_DELAY


def _live_timers(hass: HomeAssistant, name: str) -> list[asyncio.TimerHandle]:
    """The event loop's live timers whose callback, or HA job target, is called ``name``."""
    found = []
    for handle in hass.loop._scheduled:  # type: ignore[attr-defined]
        if handle.cancelled():
            continue
        names = {getattr(handle._callback, "__name__", "")}
        names |= {getattr(getattr(arg, "target", None), "__name__", "") for arg in handle._args}
        if any(name in n for n in names):
            found.append(handle)
    return found


def _due_in(hass: HomeAssistant, handle: asyncio.TimerHandle) -> float:
    return handle.when() - hass.loop.time()


async def _run_clock(hass: HomeAssistant, freezer: Any, seconds: float, step: float = 0.5) -> None:
    """Let ``seconds`` pass on a moving clock (wall and loop time), running what falls due."""
    for _ in range(round(seconds / step)):
        freezer.tick(timedelta(seconds=step))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def _set_up_with_stagger(
    hass: HomeAssistant, entry: MockConfigEntry, device: Device, stagger: int
) -> list[float]:
    """Set the entry up with a real first-poll stagger; return the loop times polls ran at."""
    polls: list[float] = []

    async def _update(**_kwargs: Any) -> Any:
        polls.append(hass.loop.time())
        device.state.optimistic = False
        return device.state

    device.update.side_effect = _update  # type: ignore[attr-defined]
    with patch("custom_components.govee_ble_local._first_poll_stagger", return_value=stagger):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=False)
    return polls


async def test_a_first_poll_held_then_released_leaves_one_poll_timer(
    hass: HomeAssistant,
    freezer: Any,
    jitter: MagicMock,
    *,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """A command during the stagger holds the first poll; released before HA's first timer, it
    must cancel that timer, or two timers would poll for good (one cadence, one live timer)."""
    polls = await _set_up_with_stagger(hass, mock_config_entry, mock_device, 30)
    await _run_clock(hass, freezer, 20)
    await _turn_on(hass)  # holds the first poll (due at 30 s) until 45 s
    await _run_clock(hass, freezer, 15 * BASE)
    assert len(_live_timers(hass, "wrap_handle_refresh_interval")) == 1
    assert len(polls) <= 15 + 2  # one poll per base interval, not two


async def test_a_rearm_keeps_a_held_first_poll(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """A re-arm (or a policy step that stops polls) drops a held scheduled poll, but the first
    poll after setup still runs once when the hold-off ends."""
    await _turn_on(hass)
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    await coordinator.async_background_poll()  # the first poll, held
    coordinator._set_poll_interval(timedelta(seconds=BASE))
    coordinator._schedule_refresh()
    assert coordinator._first_poll_waiting
    assert len(_live_timers(hass, "_async_holdoff_cap_reached")) == 1
    await advance(hass, HOLDOFF + 1)
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]


async def test_unload_drops_a_held_first_poll(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    setup_integration: MockConfigEntry,
) -> None:
    """Unloading drops a held first poll and its cap: it doesn't run after the entry is gone."""
    await _turn_on(hass)
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    await coordinator.async_background_poll()
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    assert not coordinator._first_poll_waiting
    assert _live_timers(hass, "_async_holdoff_cap_reached") == []
    await advance(hass, POLL_HOLDOFF_MAX_SECONDS + 1)
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]


async def test_a_held_poll_doesnt_run_while_home_assistant_stops(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """Released, a held poll runs as HA's timer would: not once HA is stopping."""
    await _turn_on(hass)
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    await coordinator.async_background_poll()
    hass.set_state(CoreState.stopping)
    try:
        await advance(hass, HOLDOFF + 1)
        mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]
    finally:
        hass.set_state(CoreState.running)


@pytest.mark.parametrize("rejected", [False, True], ids=["read", "secret_rejected"])
async def test_a_poll_that_runs_meanwhile_is_the_held_first_poll(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    *,
    rejected: bool,
) -> None:
    """A refresh while the first poll is held (update_entity, the turn-on poll) is that poll:
    the hold-off end doesn't connect again, nor after that refresh halted polls (the secret)."""
    await _turn_on(hass)
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    await coordinator.async_background_poll()  # the first poll, held
    if rejected:
        mock_device.update.side_effect = GoveeBleAuthError("rejected")  # type: ignore[attr-defined]
    await coordinator.async_refresh()
    assert not coordinator._first_poll_waiting
    assert _live_timers(hass, "_async_holdoff_cap_reached") == []
    await advance(hass, HOLDOFF + 1)
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "outcome",
    [None, GoveeBleConnectionError("link lost"), GoveeBleAuthError("rejected")],
    ids=["read", "failed", "secret_rejected"],
)
@pytest.mark.parametrize("released_by", [None, "holdoff_end", "cap"])
async def test_a_poll_running_when_the_hold_off_ends_is_the_held_first_poll(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    *,
    outcome: Exception | None,
    released_by: str | None,
) -> None:
    """A refresh (update_entity, the turn-on poll) that starts while the first poll is held
    reads the device as that poll would, however it ends: the hold-off ending, or its cap,
    while it still runs doesn't release the held poll alongside it, nor does its end."""
    gate = asyncio.Event()
    read = mock_device.update.side_effect  # type: ignore[attr-defined]

    async def _held(**kwargs: Any) -> Any:
        await gate.wait()
        if outcome is not None:
            raise outcome
        return await read(**kwargs)

    if released_by == "cap":
        coordinator.async_note_command_started()  # still running: only the cap releases
    else:
        await _turn_on(hass)
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    await coordinator.async_background_poll()  # the first poll, held
    mock_device.update.side_effect = _held  # type: ignore[attr-defined]
    refresh = hass.async_create_task(coordinator.async_refresh())
    for _ in range(5):
        await asyncio.sleep(0)
    if released_by is not None:  # while the refresh still reads (no waiting for it to end)
        due = POLL_HOLDOFF_MAX_SECONDS if released_by == "cap" else HOLDOFF
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=due + 1))
        for _ in range(5):
            await asyncio.sleep(0)
        assert coordinator._first_poll_waiting  # not released while the refresh runs
    gate.set()
    await refresh
    assert not coordinator._first_poll_waiting
    assert _live_timers(hass, "_async_holdoff_cap_reached") == []
    await advance(hass, HOLDOFF + 1)  # before HA's next scheduled poll
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    if released_by == "cap":
        coordinator.async_note_command_finished()

    # Polling carries on, however that poll ended: a later held poll is released and runs.
    mock_device.update.side_effect = read  # type: ignore[attr-defined]
    await _turn_on(hass)
    await _poll_comes_due(hass, coordinator)
    assert coordinator._poll_waiting
    await advance(hass, HOLDOFF + 1)
    assert mock_device.update.await_count == 2  # type: ignore[attr-defined]


async def test_the_first_poll_due_while_a_poll_runs_is_that_poll(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """The first poll coming due, with no command holding polls off, while a refresh already
    reads the device doesn't queue a second one behind it."""
    gate = asyncio.Event()
    read = mock_device.update.side_effect  # type: ignore[attr-defined]

    async def _held(**kwargs: Any) -> Any:
        await gate.wait()
        return await read(**kwargs)

    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    mock_device.update.side_effect = _held  # type: ignore[attr-defined]
    refresh = hass.async_create_task(coordinator.async_refresh())
    for _ in range(5):
        await asyncio.sleep(0)
    first = hass.async_create_task(coordinator.async_background_poll())
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    await refresh
    await first
    await hass.async_block_till_done()
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]


async def test_a_poll_timer_firing_while_a_poll_runs_adds_no_poll(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """After failures (backed off to 480 s) a command lands during a long poll and re-arms
    the timer at the base cadence; when it fires the poll still runs, past the hold-off. It
    queues no second poll behind it: the running poll re-arms the timer as it ends."""
    fail(mock_device, GoveeBleConnectionError("no slot"))
    for _ in range(3):
        await coordinator.async_refresh()
    assert interval(coordinator) == BASE * 4
    gate = asyncio.Event()

    async def _long(**_kwargs: Any) -> Any:
        await gate.wait()
        mock_device.state.optimistic = False
        return mock_device.state

    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    mock_device.update.side_effect = _long  # type: ignore[attr-defined]
    refresh = hass.async_create_task(coordinator.async_refresh())
    for _ in range(5):
        await asyncio.sleep(0)
    await _turn_on(hass)  # lands: the backoff is dropped, the timer re-armed at BASE
    assert len(_live_timers(hass, "wrap_handle_refresh_interval")) == 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=BASE + 1))
    for _ in range(5):
        await asyncio.sleep(0)
    assert _live_timers(hass, "wrap_handle_refresh_interval") == []  # it fired
    gate.set()
    await refresh
    await hass.async_block_till_done()
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    assert len(_live_timers(hass, "wrap_handle_refresh_interval")) == 1  # re-armed once


async def test_the_cap_runs_a_held_first_poll_while_a_command_runs(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator, mock_device: Device
) -> None:
    """With background polls stopped (a light turned off) and only the first poll held, the
    cap still runs it while a command keeps the hold-off going."""
    coordinator._async_clear_poll_interval()
    coordinator.async_note_command_started()
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    await coordinator.async_background_poll()  # the first poll, held
    assert not coordinator._poll_waiting
    await advance(hass, POLL_HOLDOFF_MAX_SECONDS + 1)
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    coordinator.async_note_command_finished()


def _arm_poll_soon(coordinator: GoveeBleLocalCoordinator) -> None:
    """Re-arm HA's own poll timer 5 s out (jitter is 0 in these tests)."""
    coordinator._set_poll_interval(timedelta(seconds=5))
    coordinator._schedule_refresh()


async def _poll_comes_due(hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator) -> None:
    """Make HA's real poll timer fire now, as it would at the end of an interval."""
    _arm_poll_soon(coordinator)
    await advance(hass, 6)


async def test_a_new_command_restarts_the_hold_off(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator
) -> None:
    """Each command restarts the hold-off from its own end: the earlier timer is cancelled."""
    await _turn_on(hass)
    [first] = _live_timers(hass, "_async_end_holdoff")
    assert _due_in(hass, first) == pytest.approx(HOLDOFF, abs=1)

    coordinator.async_note_command_started()
    assert first.cancelled()
    assert _live_timers(hass, "_async_end_holdoff") == []  # held while it runs
    assert coordinator.commands_holding_off
    coordinator.async_note_command_finished()
    [second] = _live_timers(hass, "_async_end_holdoff")
    assert second is not first


async def test_overlapping_commands_hold_off_until_the_last_one_ends(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator
) -> None:
    """Two commands to the device at once (a zone and the main light): the hold-off starts last."""
    coordinator.async_note_command_started()
    coordinator.async_note_command_started()
    coordinator.async_note_command_finished()
    assert coordinator.commands_holding_off
    assert _live_timers(hass, "_async_end_holdoff") == []
    coordinator.async_note_command_finished()
    assert len(_live_timers(hass, "_async_end_holdoff")) == 1


async def test_a_stream_of_commands_holds_a_poll_off_at_most_the_cap(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """Commands that keep coming can't postpone a due poll past POLL_HOLDOFF_MAX_SECONDS."""
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    coordinator.async_note_command_started()
    await _poll_comes_due(hass, coordinator)  # held
    [cap] = _live_timers(hass, "_async_holdoff_cap_reached")
    # advance() fires due timers without moving the loop's clock.
    assert _due_in(hass, cap) == pytest.approx(POLL_HOLDOFF_MAX_SECONDS, abs=1)
    for _ in range(4):  # one command after another
        coordinator.async_note_command_finished()
        coordinator.async_note_command_started()
    await coordinator.async_background_poll()  # another poll due: it joins the held one
    assert _live_timers(hass, "_async_holdoff_cap_reached") == [cap]

    await advance(hass, POLL_HOLDOFF_MAX_SECONDS + 1)
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    assert coordinator.commands_holding_off  # it ran although commands still hold polls off
    coordinator.async_note_command_finished()

    # A cap timer that fires with nothing held (a late callback) starts no poll.
    coordinator._async_holdoff_cap_reached(dt_util.utcnow())
    await hass.async_block_till_done()
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]


async def test_rearming_the_schedule_replaces_a_held_poll(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """A schedule re-armed during a hold-off (a policy step, say) replaces the held poll."""
    await _turn_on(hass)
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    await _poll_comes_due(hass, coordinator)
    assert coordinator._poll_waiting
    assert _live_timers(hass, "wrap_handle_refresh_interval") == []  # the one that fired

    coordinator._set_poll_interval(timedelta(seconds=BASE))
    coordinator._schedule_refresh()  # the next poll a base interval out, as a policy step sets it
    assert not coordinator._poll_waiting
    assert _live_timers(hass, "_async_holdoff_cap_reached") == []
    assert len(_live_timers(hass, "wrap_handle_refresh_interval")) == 1
    await advance(hass, HOLDOFF + 1)  # the hold-off ends with nothing held
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]


async def test_first_and_scheduled_poll_held_together_run_once(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """The first poll and HA's timer, both due in one hold-off, end it with a single poll."""
    await _turn_on(hass)
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    _arm_poll_soon(coordinator)
    await coordinator.async_background_poll()  # the first poll, at the end of its stagger
    await advance(hass, 6)  # HA's timer, in the same hold-off
    assert coordinator._poll_waiting
    await advance(hass, HOLDOFF + 1)
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]


async def test_a_command_ending_after_unload_arms_no_hold_off(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    setup_integration: MockConfigEntry,
) -> None:
    """A command that outlives the unload doesn't leave a hold-off timer behind."""
    coordinator.async_note_command_started()
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    coordinator.async_note_command_finished()
    assert _live_timers(hass, "_async_end_holdoff") == []


async def test_a_command_landing_after_unload_arms_no_poll(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    setup_integration: MockConfigEntry,
) -> None:
    """A command that lands after the unload (HA doesn't cancel service calls) re-arms no poll
    on the shut-down coordinator, even with a failure backoff it would otherwise reset."""
    coordinator.consecutive_failures = 2
    coordinator._set_poll_interval(timedelta(seconds=BASE * 2))
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    coordinator.async_handle_command_success()
    assert _live_timers(hass, "wrap_handle_refresh_interval") == []


async def test_unload_drops_a_held_off_poll(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    setup_integration: MockConfigEntry,
) -> None:
    """Unloading during a hold-off cancels it and the poll it held back."""
    await _turn_on(hass)
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    await _poll_comes_due(hass, coordinator)
    assert coordinator._poll_waiting
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    assert not coordinator.commands_holding_off  # its timer is gone, not just yet to fire
    await advance(hass, HOLDOFF + 1)
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]


async def _until(condition: Callable[[], bool]) -> None:
    """Yield to the event loop until ``condition`` holds (without waiting for HA's tasks)."""
    for _ in range(50):
        if condition():
            return
        await asyncio.sleep(0)
    pytest.fail("condition never held")


def _timed_polls(device: Device, clock: SimpleNamespace, *seconds: float) -> None:
    """Make the next polls succeed, each taking the given seconds on the Bluetooth clock."""
    durations = iter(seconds)

    async def _update(**_kwargs: Any) -> Any:
        clock.now += next(durations)
        device.state.optimistic = False
        return device.state

    device.update.side_effect = _update  # type: ignore[attr-defined]


async def test_slow_polls_back_off_and_a_fast_one_resets(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    monotonic: SimpleNamespace,
) -> None:
    """Each slow poll doubles the interval, up to the cap of steps; a fast poll undoes it."""
    slow = SLOW_POLL_SECONDS + 2
    _timed_polls(mock_device, monotonic, *([slow] * (SLOW_POLL_MAX_STEPS + 1)), 2.0)
    for step in range(1, SLOW_POLL_MAX_STEPS + 2):
        await coordinator.async_refresh()
        assert coordinator.slow_poll_steps == min(step, SLOW_POLL_MAX_STEPS)
        expected = BASE * (1 << min(step, SLOW_POLL_MAX_STEPS))
        assert interval(coordinator) == min(expected, MAX_POLL_INTERVAL_SECONDS)
    assert coordinator.last_poll_seconds == slow

    await coordinator.async_refresh()
    assert coordinator.slow_poll_steps == 0
    assert interval(coordinator) == BASE
    assert coordinator.last_poll_seconds == 2.0


@pytest.mark.parametrize(
    ("seconds", "steps"),
    [(SLOW_POLL_SECONDS, 1), (SLOW_POLL_SECONDS - 0.5, 0)],
    ids=["at", "below"],
)
async def test_slow_poll_threshold(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    monotonic: SimpleNamespace,
    *,
    seconds: float,
    steps: int,
) -> None:
    """A poll of SLOW_POLL_SECONDS counts as slow; one just under it doesn't."""
    _timed_polls(mock_device, monotonic, seconds)
    await coordinator.async_refresh()
    assert coordinator.slow_poll_steps == steps


async def test_slow_floor_holds_through_a_failure_and_stays_within_the_cap(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    monotonic: SimpleNamespace,
) -> None:
    """A failed poll doesn't drop a slow device to the base cadence; the cap still holds."""
    _timed_polls(mock_device, monotonic, SLOW_POLL_SECONDS + 2, SLOW_POLL_SECONDS + 2)
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert interval(coordinator) == BASE * 4

    fail(mock_device, GoveeBleConnectionError("no slot"))
    await coordinator.async_refresh()  # the first failure keeps the base cadence...
    assert interval(coordinator) == BASE * 4  # ...but not below the slow floor
    for _ in range(6):
        await coordinator.async_refresh()
    assert interval(coordinator) == MAX_POLL_INTERVAL_SECONDS


async def test_slow_floor_holds_through_a_restart_and_a_policy_step(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator
) -> None:
    """The on-backoff starting over and a policy step keep a slow device at its floor."""
    coordinator.slow_poll_steps = 2
    coordinator.poll_policy = PollPolicy.ON
    coordinator._set_poll_interval(timedelta(seconds=BASE * 8))
    assert coordinator._async_restart_on_backoff()  # a change: the on-backoff starts over
    assert coordinator.nominal_interval == timedelta(seconds=BASE * 4)

    coordinator._set_poll_interval(timedelta(seconds=BASE * 8))
    with patch.object(coordinator, "_async_policy_step", return_value=PollPolicy.FIXED):
        assert coordinator._async_apply_policy()  # ON -> FIXED: the base cadence
    assert coordinator.nominal_interval == timedelta(seconds=BASE * 4)


async def test_a_restart_at_the_slow_floor_doesnt_re_arm(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator
) -> None:
    """An on-backoff already at the slow floor has nothing to cut back: no re-arm."""
    coordinator.slow_poll_steps = 2
    coordinator.poll_policy = PollPolicy.ON
    coordinator._set_poll_interval(timedelta(seconds=BASE * 4))  # == the floor
    with patch.object(coordinator, "_schedule_refresh") as schedule:
        assert not coordinator._async_restart_on_backoff()
    schedule.assert_not_called()
    assert coordinator.nominal_interval == timedelta(seconds=BASE * 4)


async def test_a_command_doesnt_reset_a_backoff_at_the_slow_floor(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator
) -> None:
    """After failures, a command cuts a backoff only down to the slow floor, never re-arming
    one already there (it would just postpone the poll)."""
    coordinator.slow_poll_steps = 2
    coordinator.consecutive_failures = 2
    coordinator._set_poll_interval(timedelta(seconds=BASE * 4))  # == the floor
    with patch.object(coordinator, "_schedule_refresh") as schedule:
        coordinator.async_handle_command_success()
    schedule.assert_not_called()
    assert not coordinator._command_reset_used


def _timed_failure(
    device: Device, clock: SimpleNamespace, seconds: float, error: Exception
) -> None:
    """Make the next polls fail with ``error`` after ``seconds`` on the Bluetooth clock."""

    async def _update(**_kwargs: Any) -> Any:
        clock.now += seconds
        raise error

    device.update.side_effect = _update  # type: ignore[attr-defined]


async def test_a_connect_out_of_budget_backs_off_as_slow(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    monotonic: SimpleNamespace,
) -> None:
    """A weak device whose connect keeps running out of its 15 s slows down to the slow cap
    (8x), instead of the failure backoff taking it to the 30-minute cap; each such poll still
    counts as a failed one."""
    error = GoveeBleTimeout(f"{ADDRESS}: connect exceeded {POLL_CONNECT_TIMEOUT_SECONDS}s")
    _timed_failure(mock_device, monotonic, POLL_CONNECT_TIMEOUT_SECONDS + 1, error)
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert coordinator.slow_poll_steps == 1
    assert coordinator.last_poll_seconds == POLL_CONNECT_TIMEOUT_SECONDS + 1
    assert coordinator.consecutive_failures == coordinator.total_failures == 1
    assert interval(coordinator) == BASE * 2
    for _ in range(6):
        await coordinator.async_refresh()
    assert coordinator.slow_poll_steps == SLOW_POLL_MAX_STEPS
    assert coordinator.consecutive_failures == 7
    assert interval(coordinator) == BASE * (1 << SLOW_POLL_MAX_STEPS)


@pytest.mark.parametrize(
    ("seconds", "error", "slow"),
    [
        (SLOW_POLL_SECONDS, GoveeBleTimeout("connect exceeded 15s"), True),
        (SLOW_POLL_SECONDS - 0.5, GoveeBleTimeout("no reply"), False),
        (POLL_CONNECT_TIMEOUT_SECONDS + 1, GoveeBleConnectionError("link lost"), False),
    ],
    ids=["timeout_at_the_slow_threshold", "quick_timeout", "slow_connection_error"],
)
async def test_only_a_slow_timeout_backs_off_as_slow(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    monotonic: SimpleNamespace,
    *,
    seconds: float,
    error: Exception,
    slow: bool,
) -> None:
    """A timeout after SLOW_POLL_SECONDS adds slow steps; a quicker one, or another error
    however slow, backs off as an ordinary failure. Both count as failed polls."""
    _timed_failure(mock_device, monotonic, seconds, error)
    for _ in range(3):
        await coordinator.async_refresh()
    assert coordinator.consecutive_failures == 3
    assert coordinator.slow_poll_steps == (3 if slow else 0)
    assert interval(coordinator) == BASE * (8 if slow else 4)


async def test_a_slow_timeout_amid_failures_keeps_their_backoff(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    monotonic: SimpleNamespace,
) -> None:
    """A connect that runs out of its budget in a streak of quick failures keeps the streak's
    backoff (floored by its slow step), instead of dropping back to the slow floor."""
    _timed_failure(mock_device, monotonic, 1, GoveeBleConnectionError("no slot"))
    for _ in range(3):
        await coordinator.async_refresh()
    assert interval(coordinator) == BASE * 4
    error = GoveeBleTimeout(f"{ADDRESS}: connect exceeded {POLL_CONNECT_TIMEOUT_SECONDS}s")
    _timed_failure(mock_device, monotonic, POLL_CONNECT_TIMEOUT_SECONDS + 1, error)
    await coordinator.async_refresh()
    assert coordinator.consecutive_failures == 4
    assert coordinator.slow_poll_steps == 1
    assert interval(coordinator) == BASE * 4


async def test_a_failure_after_a_recovery_starts_the_backoff_over(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """A successful poll ends the failure streak: the next quick failure keeps the base
    cadence (the first failure's grace) instead of picking up the old streak's backoff."""
    fail(mock_device, GoveeBleConnectionError("no slot"))
    for _ in range(4):
        await coordinator.async_refresh()
    assert interval(coordinator) == BASE * 8
    succeed(mock_device)
    await coordinator.async_refresh()
    fail(mock_device, GoveeBleConnectionError("no slot"))
    await coordinator.async_refresh()
    assert coordinator.consecutive_failures == 1
    assert interval(coordinator) == BASE


async def test_an_absent_device_is_unavailable_once_unheard_long_enough(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    coordinator: GoveeBleLocalCoordinator,
    *,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """Dropped from the connectable history before its polls failed, a device stays available
    until UNAVAILABLE_AFTER_UNHEARD_SECONDS after HA last heard it (the advertisement HA hands
    over as it drops it); the presence tick then publishes it unavailable."""
    monotonic.now += 100
    last_heard = monotonic.now
    monotonic.now += UNAVAILABLE_AFTER_UNHEARD_SECONDS - 0.5
    presence.connectable = False
    unavailable_callback(mock_bluetooth, connectable=True)(make_service_info(seen_at=last_heard))
    assert coordinator.consecutive_failures == 0
    assert hass.states.get(MAIN_LIGHT).state != STATE_UNAVAILABLE
    monotonic.now += 0.5
    coordinator.async_refresh_presence()
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE


@pytest.mark.parametrize("optimistic", [False, True], ids=["connected", "never_read"])
async def test_a_poll_that_connected_and_didnt_fail_counts_as_hearing_it(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    *,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
    optimistic: bool,
) -> None:
    """A poll that connected and didn't fail shows the device reachable, though it didn't
    advertise around it: the unheard time counts from that poll, not its last advertisement.
    Not while the library's state is still only optimistic (nothing ever read back)."""
    monotonic.now += 100
    last_heard = monotonic.now
    monotonic.now += UNAVAILABLE_AFTER_UNHEARD_SECONDS - 60
    succeed(mock_device, optimistic=optimistic)
    await coordinator.async_refresh()  # while HA still lists it
    monotonic.now += 120  # 420 s since its last advertisement, 120 s since the poll
    presence.connectable = False
    unavailable_callback(mock_bluetooth, connectable=True)(make_service_info(seen_at=last_heard))
    available = hass.states.get(MAIN_LIGHT).state != STATE_UNAVAILABLE
    assert available is not optimistic


async def test_a_device_dropped_before_its_first_poll_gets_its_grace_from_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    *,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """After a restart HA's history may hold an advertisement from long before. A device HA
    drops with it before the (staggered) first poll has reached it still gets
    UNAVAILABLE_AFTER_UNHEARD_SECONDS from setup, as it would three skipped polls."""
    polls = await _set_up_with_stagger(hass, mock_config_entry, mock_device, 30)
    assert polls == []
    presence.connectable = False
    old = make_service_info()
    unavailable_callback(mock_bluetooth, connectable=True)(
        make_service_info(seen_at=old.time - 10_000)
    )
    assert hass.states.get(MAIN_LIGHT).state != STATE_UNAVAILABLE


async def test_a_poll_that_reads_nothing_still_ends_the_failure_streak(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """A poll that connects but reads nothing back (optimistic) is no failure: the next
    quick failure keeps the base cadence."""
    fail(mock_device, GoveeBleConnectionError("no slot"))
    for _ in range(3):
        await coordinator.async_refresh()
    succeed(mock_device, optimistic=True)
    await coordinator.async_refresh()
    fail(mock_device, GoveeBleConnectionError("no slot"))
    await coordinator.async_refresh()
    assert interval(coordinator) == BASE


async def test_a_poll_runs_over_a_held_link_only_within_the_librarys_warm_window(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    presence: SimpleNamespace,
) -> None:
    """Gone from HA's history, a device a command reached less than the library's
    ACTIVE_IDLE_DELAY ago is polled over the link that command left open; from then on the
    poll is skipped as absent again."""
    coordinator.async_handle_command_success()  # a command landed
    presence.connectable = False
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    monotonic.now += lib_const.ACTIVE_IDLE_DELAY - 0.5
    await coordinator.async_refresh()
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    assert coordinator.consecutive_failures == 0
    monotonic.now += 0.5
    await coordinator.async_refresh()
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    assert coordinator.consecutive_failures == 1  # skipped: nothing can reach it


async def test_a_held_link_poll_that_finds_the_link_gone_counts_as_a_skip(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    *,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """A poll run for a command's held link, which was gone by then (its connect runs out),
    counts as the skip it stood in for: no slow step, and the device is polled as soon as it
    is heard again."""
    coordinator.async_handle_command_success()
    presence.connectable = False

    async def _gone(**_kwargs: Any) -> Any:
        monotonic.now += POLL_CONNECT_TIMEOUT_SECONDS + 1
        raise GoveeBleTimeout(f"{ADDRESS}: connect exceeded {POLL_CONNECT_TIMEOUT_SECONDS}s")

    mock_device.update.side_effect = _gone  # type: ignore[attr-defined]
    await coordinator.async_refresh()
    assert coordinator.consecutive_failures == 1
    assert coordinator.slow_poll_steps == 0
    succeed(mock_device)
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    presence.connectable = True
    advertisement_callback(mock_bluetooth)(make_service_info(), BluetoothChange.ADVERTISEMENT)
    await hass.async_block_till_done()
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]  # the return poll


async def test_a_held_link_poll_that_waited_out_the_window_for_a_slot_is_skipped(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    presence: SimpleNamespace,
) -> None:
    """The warm window is checked again once the poll has a slot: a poll that waited past it
    behind other devices' polls is skipped as absent rather than connecting afresh."""
    coordinator.async_handle_command_success()
    presence.connectable = False
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    semaphore = _poll_semaphore(hass)
    for _ in range(MAX_CONCURRENT_POLLS):
        await semaphore.acquire()
    refresh = hass.async_create_task(coordinator.async_refresh())
    for _ in range(5):
        await asyncio.sleep(0)
    monotonic.now += lib_const.ACTIVE_IDLE_DELAY  # the other polls take that long
    for _ in range(MAX_CONCURRENT_POLLS):
        semaphore.release()
    await refresh
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]
    assert coordinator.consecutive_failures == 1


async def test_a_failed_command_opens_no_held_link_window(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    presence: SimpleNamespace,
) -> None:
    """Only a command that landed holds a link open: after one that failed, a poll of a
    device no connectable scanner sees is skipped as absent."""
    presence.connectable = False
    connection(mock_device).error = GoveeBleConnectionError("link lost")
    with pytest.raises(HomeAssistantError):
        await _turn_on(hass)
    connection(mock_device).error = None
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    await coordinator.async_refresh()
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]


async def test_a_weak_signal_backs_polls_off_too(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    presence: SimpleNamespace,
) -> None:
    """A device heard only at or below WEAK_SIGNAL_RSSI is polled less, however fast its poll."""
    presence.service_info = make_service_info(rssi=WEAK_SIGNAL_RSSI)
    await coordinator.async_refresh()
    assert coordinator.slow_poll_steps == 1
    assert interval(coordinator) == BASE * 2

    presence.service_info = make_service_info(rssi=WEAK_SIGNAL_RSSI + 1)
    await coordinator.async_refresh()
    assert coordinator.slow_poll_steps == 0
    assert interval(coordinator) == BASE


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


async def test_unchanged_advertisement_rewrites_nothing(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """An advertisement with unchanged on/off rewrites neither the light nor the diagnostics."""
    allow_adverts(mock_device)
    callback = advertisement_callback(mock_bluetooth)
    callback(make_service_info(on=True, rssi=-70), BluetoothChange.ADVERTISEMENT)
    assert hass.states.get(MAIN_LIGHT).state == "on"

    with (
        patch.object(light_entity(hass), "async_write_ha_state") as write,
        patch.object(coordinator, "async_update_diagnostics") as diagnostics,
    ):
        callback(make_service_info(on=True, rssi=-80), BluetoothChange.ADVERTISEMENT)
    write.assert_not_called()
    diagnostics.assert_not_called()
    # Last seen / RSSI come from the history refresh, not the callback.
    assert coordinator.rssi == -60


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_diagnostics_keep_publishing_during_a_steady_advertisement_stream(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    coordinator: GoveeBleLocalCoordinator,
    presence: SimpleNamespace,
    freezer: Any,
) -> None:
    """A device repeating one advertisement still refreshes last seen / RSSI every minute.

    HA calls the advertisement callback only when the content changes, so here
    it is never called: only the history (which records every repeat) moves on.
    """
    registry = er.async_get(hass)
    rssi_id = registry.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_rssi")
    last_seen_id = registry.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_last_seen")
    assert hass.states.get(rssi_id).state == "-60"
    for rssi in (-70, -90):
        freezer.tick(timedelta(seconds=LAST_SEEN_RESOLUTION_SECONDS))
        monotonic.now += LAST_SEEN_RESOLUTION_SECONDS
        presence.service_info = make_service_info(rssi=rssi, seen_at=monotonic.now - 3)
        await advance(hass, 0)
        assert hass.states.get(rssi_id).state == str(rssi)
        seen = dt_util.utcnow() - timedelta(seconds=3)
        assert dt_util.parse_datetime(hass.states.get(last_seen_id).state) == seen.replace(
            microsecond=0
        )


async def test_presence_refresh_skips_an_advertisement_already_taken(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator, presence: SimpleNamespace
) -> None:
    """Nothing new in the history (or nothing at all) publishes nothing."""
    with patch.object(coordinator, "async_update_diagnostics") as diagnostics:
        await advance(hass, LAST_SEEN_RESOLUTION_SECONDS)  # the entry taken at setup
        presence.service_info = None
        await advance(hass, LAST_SEEN_RESOLUTION_SECONDS)
    diagnostics.assert_not_called()


async def test_advertisement_after_silence_refreshes_presence_at_once(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """Heard again after no scanner did, the diagnostics update without waiting for the timer."""
    allow_adverts(mock_device)
    callback = advertisement_callback(mock_bluetooth)
    callback(make_service_info(on=True), BluetoothChange.ADVERTISEMENT)
    presence.connectable = presence.any = False
    unavailable_callback(mock_bluetooth, connectable=False)(make_service_info())
    assert coordinator.rssi is None

    presence.connectable = presence.any = True
    with (
        patch.object(light_entity(hass), "async_write_ha_state") as write,
        patch.object(
            coordinator, "async_update_diagnostics", wraps=coordinator.async_update_diagnostics
        ) as diagnostics,
    ):
        callback(make_service_info(on=True, rssi=-77), BluetoothChange.ADVERTISEMENT)
    write.assert_not_called()  # on/off and availability are unchanged
    diagnostics.assert_called_once()
    assert coordinator.rssi == -77
    assert coordinator.last_seen is not None


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

    async def _slow_update(**_kwargs: Any) -> Any:
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
    assert coordinator.nominal_interval == timedelta(seconds=BASE)  # the backoff is dropped

    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.consecutive_failures == 0


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
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


async def test_failing_state_listener_does_not_cost_the_entities_their_update(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A state listener that raises is logged; entities and the other listeners still update."""
    seen: list[str] = []
    mock_device.state.is_on = True
    coordinator.async_publish_device_state()
    assert hass.states.get(MAIN_LIGHT).state == "on"

    def _broken() -> None:
        raise RuntimeError("registry write failed")

    unsubscribes = [
        coordinator.async_add_state_listener(_broken),
        coordinator.async_add_state_listener(lambda: seen.append("next")),
    ]
    mock_device.state.is_on = False
    coordinator.async_publish_device_state()
    for unsubscribe in unsubscribes:
        unsubscribe()

    assert hass.states.get(MAIN_LIGHT).state == STATE_OFF
    assert seen == ["next"]
    assert "Unexpected error updating state listener" in caplog.text
    assert "registry write failed" in caplog.text


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


async def test_unavailable_with_unchanged_availability_pushes_diagnostics_only(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """Dropping out of both histories while still available rewrites only the diagnostics."""
    connectivity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{ADDRESS}_connectivity"
    )
    presence.connectable = presence.any = False
    with patch.object(light_entity(hass), "async_write_ha_state") as write:
        unavailable_callback(mock_bluetooth, connectable=True)(make_service_info())
        unavailable_callback(mock_bluetooth, connectable=False)(make_service_info())
    write.assert_not_called()  # no failed polls yet: still available
    assert hass.states.get(connectivity_id).state == STATE_OFF
    assert coordinator.rssi is None


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
        patch.object(GoveeBleLocalCoordinator, "async_address_present", REAL_ADDRESS_PRESENT),
        patch.object(GoveeBleLocalCoordinator, "async_last_service_info", REAL_LAST_SERVICE_INFO),
    ):
        assert coordinator.async_address_present() is False  # no scanner has seen it
        assert coordinator.async_address_present(connectable=False) is False
        assert coordinator.async_last_service_info() is mock_bluetooth.last_info.return_value
        assert mock_bluetooth.last_info.call_args.kwargs == {"connectable": True}
        coordinator.async_last_service_info(connectable=False)
        assert mock_bluetooth.last_info.call_args.kwargs == {"connectable": False}
