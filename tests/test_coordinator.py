"""Tests for the connection coordinator.

The v2 library has no aggregate status read-back: the coordinator just calls
GoveeDevice.update() (connect + optimistic state) and maps BLE failures to
UpdateFailed so HA logs them as expected, not as a crash.
"""
from __future__ import annotations

import pytest
from bleak.exc import BleakError
from govee_ble_local import GoveeBleConnectionError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.govee_ble_local.coordinator import GoveeBleLocalCoordinator

from .conftest import make_device
from .const import ADDRESS


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
