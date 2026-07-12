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
from bleak_retry_connector import BleakOutOfConnectionSlotsError
from govee_ble_local import DeviceState, GoveeBleConnectionError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.govee_ble_local.const import (
    MAX_POLL_INTERVAL_SECONDS,
    OUT_OF_SLOTS_BACKOFF_STEPS,
    POLL_INTERVAL_SECONDS,
    POLL_JITTER_SECONDS,
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
    """The first successful poll restores the (dynamic) base interval. Jitter
    neutralized; device_count=1 <= the plenty-of-slots fixture keeps
    dynamic_interval at exactly BASE."""
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


async def test_out_of_connection_slots_skips_grace_and_backs_off_harder(
    hass: HomeAssistant,
) -> None:
    """BleakOutOfConnectionSlotsError - the whole pool is out of slots, not just
    this device - skips the usual 1-failure grace and jumps straight several
    exponential-backoff steps ahead, unlike a generic BleakError."""
    device = make_device()
    device.update.side_effect = BleakOutOfConnectionSlotsError("no slots")
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    with _NO_JITTER:
        await _fail(coordinator)
        # A generic BleakError's 1st failure would keep BASE (grace); this
        # jumps straight to steps = OUT_OF_SLOTS_BACKOFF_STEPS ahead of that.
        expected = min(BASE * 2**OUT_OF_SLOTS_BACKOFF_STEPS, CAP)
        assert coordinator.update_interval == expected
        assert coordinator.total_failures == 1
    await coordinator.async_shutdown()


async def test_slots_exhausted_check_skips_poll_without_attempting(
    hass: HomeAssistant,
) -> None:
    """When the live slot-availability check reports no room at all, the
    coordinator skips the connect attempt entirely (device.update() never
    called) and records it the same as an out-of-slots failure."""
    device = make_device()
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    with (
        _NO_JITTER,
        patch(
            "custom_components.govee_ble_local.coordinator.async_slots_exhausted",
            return_value=True,
        ),
    ):
        await _fail(coordinator)
    device.update.assert_not_awaited()
    assert coordinator.total_failures == 1
    assert coordinator.update_interval == min(
        BASE * 2**OUT_OF_SLOTS_BACKOFF_STEPS, CAP
    )
    await coordinator.async_shutdown()


async def test_dynamic_interval_lengthens_with_more_devices_than_slots(
    hass: HomeAssistant,
) -> None:
    """A successful poll retargets the interval using device_count vs usable
    slots - more devices than slots lengthens the cadence beyond BASE even on
    a clean success (not just via failure backoff)."""
    device = make_device()
    device.update.return_value = DeviceState(is_on=True, optimistic=False)
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    with (
        _NO_JITTER,
        patch(
            "custom_components.govee_ble_local.coordinator.async_loaded_entry_count",
            return_value=6,
        ),
        patch(
            "custom_components.govee_ble_local.coordinator.async_usable_slots",
            return_value=2,
        ),
    ):
        await coordinator._async_update_data()
    # ceil(6 / 2) == 3 base-intervals apart.
    assert coordinator.update_interval == BASE * 3
    await coordinator.async_shutdown()


async def test_connect_semaphore_caps_concurrent_updates(hass: HomeAssistant) -> None:
    """The domain-wide semaphore actually serializes device.update() calls when
    sized to fewer slots than there are concurrent pollers."""
    import asyncio

    device_a = make_device()
    device_b = make_device()
    concurrent = 0
    max_concurrent = 0

    async def _slow_update(*_a: object, **_k: object) -> DeviceState:
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.01)
        concurrent -= 1
        return DeviceState(is_on=True, optimistic=False)

    device_a.update.side_effect = _slow_update
    device_b.update.side_effect = _slow_update
    coordinator_a = GoveeBleLocalCoordinator(hass, device_a, ADDRESS)
    coordinator_b = GoveeBleLocalCoordinator(hass, device_b, "AA:BB:CC:DD:EE:06")

    with patch(
        "custom_components.govee_ble_local.coordinator.async_connect_semaphore",
        return_value=asyncio.Semaphore(1),
    ):
        await asyncio.gather(
            coordinator_a._async_update_data(), coordinator_b._async_update_data()
        )
    assert max_concurrent == 1
    await coordinator_a.async_shutdown()
    await coordinator_b.async_shutdown()


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
