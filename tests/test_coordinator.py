"""Tests for the connection coordinator.

The v2 library has no aggregate status read-back: the coordinator just calls
GoveeDevice.update() (connect + optimistic state) and maps BLE failures to
UpdateFailed so HA logs them as expected, not as a crash.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from bleak.exc import BleakError
from govee_ble_local import DeviceState, GoveeBleConnectionError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.govee_ble_local.const import (
    MAX_POLL_INTERVAL_SECONDS,
    POLL_INTERVAL_SECONDS,
)
from custom_components.govee_ble_local.coordinator import GoveeBleLocalCoordinator

from .conftest import make_device
from .const import ADDRESS

BASE = timedelta(seconds=POLL_INTERVAL_SECONDS)
CAP = timedelta(seconds=MAX_POLL_INTERVAL_SECONDS)


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
    cap, instead of retrying every base interval forever."""
    device = make_device()
    device.update.side_effect = BleakError("no slot")
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)

    # 1st failure keeps the base cadence (grace for a transient blip).
    await _fail(coordinator)
    assert coordinator.update_interval == BASE

    # Then double: 240, 480, 960, then clamp at the 1800s cap.
    for want in (BASE * 2, BASE * 4, BASE * 8, CAP, CAP):
        await _fail(coordinator)
        assert coordinator.update_interval == min(want, CAP)
    await coordinator.async_shutdown()


async def test_backoff_resets_on_success(hass: HomeAssistant) -> None:
    """The first successful poll restores the base interval."""
    device = make_device()
    device.update.side_effect = BleakError("no slot")
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    for _ in range(4):
        await _fail(coordinator)
    assert coordinator.update_interval > BASE  # backed off

    device.update.side_effect = None
    device.update.return_value = DeviceState(is_on=True, optimistic=False)
    result = await coordinator._async_update_data()
    assert result.is_on is True
    assert coordinator.update_interval == BASE
    await coordinator.async_shutdown()
