"""A command waiting at the connect gate as its entry unloads or Home Assistant stops.

The device's waiting calls go ahead then (``async_release_waiters``), and the unload and
the stop listener each give them a turn of the event loop before anything closes the
link: the command reaches the device as it would have without the gate. The device's
transport here refuses I/O once closed, as the library's does, so a command that resumed
only after the close fails with "Could not communicate".

The radio is a real habluetooth proxy registered with Home Assistant's Bluetooth manager
(tests/test_connect_gate.py's), reporting all three of its slots taken by other devices,
so the gate holds the command until it is released.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
import logging
from typing import Any
from unittest.mock import patch

from govee_ble_local import Device, GoveeBleConnectionError
from govee_ble_local.wire import build
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import api as bluetooth_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local import connect_gate
from custom_components.govee_ble_local.connect_gate import _GATE

from .conftest import FakeConnection, connection
from .const import ADDRESS
from .test_connect_gate import ATHOM, MAIN, Radio


class ClosingConnection(FakeConnection):
    """A fake transport that, like the library's, refuses every write and read once closed."""

    def _write(self, frames: list[bytes]) -> None:
        if self.closed:
            raise GoveeBleConnectionError("connection closed")
        super()._write(frames)

    async def query(self, *args: Any, **kwargs: Any) -> list[bytes]:
        if self.closed:
            raise GoveeBleConnectionError("connection closed")
        return await super().query(*args, **kwargs)


@pytest.fixture
def mock_device(mock_device: Device) -> Device:
    """The device under test, on a transport that refuses I/O after its close."""
    mock_device._conn = ClosingConnection()  # type: ignore[assignment]
    return mock_device


@pytest.fixture
async def proxy(hass: HomeAssistant, enable_bluetooth: None) -> AsyncGenerator[Radio]:
    """A 3-slot ESPHome proxy, the only radio; it hears the device once the test fills it."""
    radio = Radio(ATHOM, "athom", 3)
    cancels = [radio.async_setup(), bluetooth.async_register_scanner(hass, radio)]
    # The real lookup, not conftest's stand-in.
    with patch(
        "homeassistant.components.bluetooth.async_scanner_devices_by_address",
        bluetooth_api.async_scanner_devices_by_address,
    ):
        yield radio
    for cancel in cancels:
        cancel()


async def _hold_a_turn_off(hass: HomeAssistant, proxy: Radio, device: Device) -> asyncio.Task[Any]:
    """Start a light turn-off the gate holds: the proxy hears the device, all its slots taken.

    Filled only now: the setup's first poll went through the gate unseen.
    """
    for slot in range(3):
        proxy.take(f"11:22:33:44:55:{slot:02X}")
    proxy.hear(ADDRESS, -70)
    device.state.is_on = True  # a plain turn-off, not deferred: the device is on
    connection(device).sent.clear()
    command = hass.async_create_task(
        hass.services.async_call("light", "turn_off", {"entity_id": MAIN}, blocking=True)
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert [waiter.address for waiter in hass.data[_GATE]._waiters] == [ADDRESS]
    device.turn_off.assert_not_awaited()  # type: ignore[attr-defined]
    assert connection(device).sent == []
    return command


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def _stop(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    # As Home Assistant stops: the state first, then the event; it doesn't unload entries.
    hass.set_state(CoreState.stopping)
    try:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        async with asyncio.timeout(5):
            await hass.async_block_till_done()
    finally:
        hass.set_state(CoreState.running)
    assert entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize("close", [_unload, _stop], ids=["unload", "home_assistant_stop"])
async def test_a_command_waiting_at_the_gate_reaches_the_device_before_its_link_closes(
    hass: HomeAssistant,
    proxy: Radio,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    close: Callable[[HomeAssistant, MockConfigEntry], Awaitable[None]],
) -> None:
    """Released as the entry goes away, it lands before the close, not into a closed link.

    The turn-off resumes in the turn of the loop it is given, so it is on the link (its
    frame written) by the time the close runs, as a command already using the link is;
    run after the close, it would fail with "Could not communicate".
    """
    device = entry.runtime_data.device  # mock_device, on its closing transport
    with caplog.at_level(logging.DEBUG, logger=connect_gate.__name__):
        command = await _hold_a_turn_off(hass, proxy, device)
        await close(hass, entry)
        async with asyncio.timeout(5):  # still held at the gate without the release
            await command  # raises "Could not communicate" had it run after the close
    assert connection(device).sent == [build.switch(False)]  # written before the close
    assert connection(device).closed
    device.stop.assert_awaited()  # type: ignore[attr-defined]
    assert f"{ADDRESS}: command released by the connect gate" in caplog.text
    assert not hass.data[_GATE]._waiters
