"""Tests for the connection coordinator.

The coordinator calls Device.update() (connect + read-back where supported,
else optimistic state) and maps BLE failures to UpdateFailed so HA logs them as
expected, not as a crash.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from bleak.exc import BleakError
from govee_ble_local import DeviceState, GoveeBleAuthError, GoveeBleConnectionError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.govee_ble_local.const import (
    MAX_CONCURRENT_POLLS,
    MAX_POLL_INTERVAL_SECONDS,
    POLL_INTERVAL_SECONDS,
    POLL_JITTER_SECONDS,
    UNAVAILABLE_AFTER_FAILURES,
)
from custom_components.govee_ble_local.coordinator import GoveeBleLocalCoordinator

from .conftest import make_device
from .const import ADDRESS

BASE = timedelta(seconds=POLL_INTERVAL_SECONDS)
CAP = timedelta(seconds=MAX_POLL_INTERVAL_SECONDS)

# Neutralizes the random jitter (const.POLL_JITTER_SECONDS) so backoff/reset math
# can be asserted exactly; jitter itself is covered by test_jitter_spreads_interval.
_NO_JITTER = patch("custom_components.govee_ble_local.coordinator.random.uniform", return_value=0)


async def test_update_calls_device_update(hass: HomeAssistant) -> None:
    """A poll delegates to device.update() and returns its DeviceState."""
    device = make_device()
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    result = await coordinator._async_update_data()
    device.update.assert_awaited_once()
    assert result is device.update.return_value
    await coordinator.async_shutdown()


@pytest.mark.parametrize(
    "exc",
    [
        BleakError("boom"),
        GoveeBleConnectionError("no slot"),
        TimeoutError("stalled"),
    ],
)
async def test_update_errors_become_update_failed(
    hass: HomeAssistant, exc: Exception
) -> None:
    """BleakError, the library's own GoveeBleError (e.g. connection failure —
    NOT a BleakError subclass), and a bare TimeoutError (stalled handshake) all
    surface as UpdateFailed rather than propagating raw and being logged as an
    "unexpected error" by HA's coordinator."""
    device = make_device()
    device.update.side_effect = exc
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    await coordinator.async_shutdown()


async def _fail(coordinator: GoveeBleLocalCoordinator) -> None:
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_backoff_grows_then_caps(hass: HomeAssistant) -> None:
    """A chronically-unreachable device stops hammering the shared adapter: the
    poll interval doubles after each failure (past a one-poll grace) up to the
    cap, instead of retrying every base interval forever. Jitter neutralized so
    the doubling math itself can be asserted exactly."""
    device = make_device()
    device.update.side_effect = BleakError("no slot")
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)

    with _NO_JITTER:
        # 1st failure keeps the base cadence (grace for a transient blip).
        await _fail(coordinator)
        assert coordinator.update_interval == BASE

        # Then double: 240, 480, 960, then clamp at the 1800s cap.
        for want in (BASE * 2, BASE * 4, BASE * 8, CAP, CAP):
            await _fail(coordinator)
            assert coordinator.update_interval == min(want, CAP)
    await coordinator.async_shutdown()


async def test_backoff_resets_on_success(hass: HomeAssistant) -> None:
    """The first successful poll restores the base interval (jitter neutralized)."""
    device = make_device()
    device.update.side_effect = BleakError("no slot")
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    with _NO_JITTER:
        for _ in range(4):
            await _fail(coordinator)
        assert coordinator.update_interval > BASE  # backed off

        device.update.side_effect = None
        device.update.return_value = DeviceState(is_on=True, optimistic=False)
        result = await coordinator._async_update_data()
        assert result.is_on is True
        assert coordinator.update_interval == BASE
    await coordinator.async_shutdown()


async def test_jitter_spreads_interval(hass: HomeAssistant) -> None:
    """Without neutralizing jitter, a successful poll's interval lands within
    the configured jitter window above the base - not exactly on it - so
    devices sharing the same nominal cadence don't stay in lockstep."""
    device = make_device()
    device.update.return_value = DeviceState(is_on=True, optimistic=False)
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    await coordinator._async_update_data()
    lo, hi = POLL_JITTER_SECONDS
    assert BASE + timedelta(seconds=lo) <= coordinator.update_interval <= BASE + timedelta(
        seconds=hi
    )
    await coordinator.async_shutdown()


async def test_backoff_never_overflows(hass: HomeAssistant) -> None:
    """Regression: the uncapped 2**failures exponent overflowed timedelta after
    ~30 failures (live: 1347 OverflowError 'Unexpected error' tracebacks), which
    also froze the interval. Hundreds of failures must stay capped and clean."""
    device = make_device()
    device.update.side_effect = BleakError("gone")
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    with _NO_JITTER:
        for _ in range(300):
            await _fail(coordinator)
    assert coordinator.update_interval == CAP
    assert coordinator.consecutive_failures == 300
    await coordinator.async_shutdown()


async def test_not_advertising_skips_connect(hass: HomeAssistant) -> None:
    """A device no scanner can see is not connected to at all (a connect would
    burn ~1 min of retries and a poll slot to fail)."""
    device = make_device()
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    with patch(
        "custom_components.govee_ble_local.coordinator.bluetooth.async_address_present",
        return_value=False,
    ):
        await _fail(coordinator)
    device.update.assert_not_awaited()
    assert coordinator.total_failures == 1
    await coordinator.async_shutdown()


async def test_availability_follows_reachability(hass: HomeAssistant) -> None:
    """Advertising device: available through any number of poll failures.
    Vanished device: unavailable after UNAVAILABLE_AFTER_FAILURES."""
    device = make_device()
    device.update.side_effect = BleakError("busy proxy")
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    present = "custom_components.govee_ble_local.coordinator.bluetooth.async_address_present"
    with patch(present, return_value=True):
        for _ in range(10):
            await _fail(coordinator)
        assert coordinator.device_available
    with patch(present, return_value=False):
        assert not coordinator.device_available  # 10 >= threshold
        coordinator.consecutive_failures = UNAVAILABLE_AFTER_FAILURES - 1
        assert coordinator.device_available
    await coordinator.async_shutdown()


async def test_last_seen_publish_is_throttled(hass: HomeAssistant) -> None:
    """Advertisements arrive several times a second; last_seen only asks to be
    published about once a minute."""
    coordinator = GoveeBleLocalCoordinator(hass, make_device(), ADDRESS)
    assert coordinator.note_advertisement_seen() is True
    assert coordinator.note_advertisement_seen() is False
    await coordinator.async_shutdown()


async def test_poll_semaphore_caps_concurrent_updates(hass: HomeAssistant) -> None:
    """The domain-wide poll cap holds across coordinators."""
    import asyncio

    concurrent = 0
    max_concurrent = 0

    async def _slow_update(*_a: object, **_k: object) -> DeviceState:
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.01)
        concurrent -= 1
        return DeviceState(is_on=True, optimistic=False)

    coordinators = []
    for i in range(MAX_CONCURRENT_POLLS + 3):
        device = make_device()
        device.update.side_effect = _slow_update
        coordinators.append(GoveeBleLocalCoordinator(hass, device, f"AA:BB:CC:DD:EE:{i:02X}"))
    await asyncio.gather(*(c._async_update_data() for c in coordinators))
    assert max_concurrent == MAX_CONCURRENT_POLLS
    for c in coordinators:
        await c.async_shutdown()


async def test_rssi_sampling_survives_missing_bluetooth(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the bluetooth manager isn't set up, RSSI sampling is skipped and the
    poll still succeeds (rssi stays None)."""

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("BluetoothManager has not been set")

    monkeypatch.setattr(
        "custom_components.govee_ble_local.coordinator.bluetooth.async_last_service_info",
        _raise,
    )
    device = make_device()
    device.update.return_value = DeviceState(optimistic=False)
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    await coordinator._async_update_data()
    assert coordinator.rssi is None
    await coordinator.async_shutdown()


async def test_total_failures_is_cumulative(hass: HomeAssistant) -> None:
    """total_failures counts every failed poll and does NOT reset on success
    (unlike the consecutive counter that drives backoff)."""
    device = make_device()
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    assert coordinator.total_failures == 0

    device.update.side_effect = BleakError("boom")
    for _ in range(3):
        await _fail(coordinator)
    assert coordinator.total_failures == 3

    device.update.side_effect = None
    device.update.return_value = DeviceState(optimistic=False)
    await coordinator._async_update_data()
    assert coordinator.total_failures == 3  # unchanged by success
    await coordinator.async_shutdown()


async def test_auth_error_triggers_reauth(hass: HomeAssistant) -> None:
    """A rejected secret surfaces as ConfigEntryAuthFailed (HA starts reauth)."""
    device = make_device()
    device.update.side_effect = GoveeBleAuthError("rejected")
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    await coordinator.async_shutdown()
