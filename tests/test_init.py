"""Tests for setup, unload, passive Bluetooth handling and the device registry."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import dataclasses
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import DEFAULT, AsyncMock, MagicMock, patch
import zlib

from bleak.backends.device import BLEDevice
import govee_ble_local
from govee_ble_local import (
    Capability,
    Device,
    DeviceProfile,
    Encryption,
    GoveeBleConnectionError,
    GoveeBleError,
    create_device,
    profile_for,
)
import govee_ble_local.registry
from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.homeassistant import exposed_entities
from homeassistant.components.light import DATA_COMPONENT as LIGHT_DATA
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    label_registry as lr,
)
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local import (
    _async_report_scene_store,
    _first_poll_stagger,
    capture,
)
from custom_components.govee_ble_local.const import (
    CONF_ALLOW_MODELLED,
    CONF_BROADCAST,
    CONF_SECRET,
    CONF_SKU,
    DOMAIN,
    LAST_SEEN_RESOLUTION_SECONDS,
    MAX_POLL_INTERVAL_SECONDS,
    POLL_HOLDOFF_AFTER_COMMAND_SECONDS,
    POLL_IDLE_DISCONNECT_SECONDS,
)
from custom_components.govee_ble_local.coordinator import GoveeBleLocalCoordinator

from .conftest import (
    ModelledCase,
    advance,
    advertisement_callback,
    allow_adverts,
    connection,
    fake_transport,
    make_device,
    make_power_only_light,
    make_service_info,
    unavailable_callback,
)
from .const import ADDRESS, LOCAL_NAME, SECRET_HEX, TITLE, broadcast_data, pact_mfg

MAIN_LIGHT = "light.test_ceiling"
SELF_TEST_BUTTON = "button.test_ceiling_run_self_test"
FIRST_POLL_TASK = f"{DOMAIN}_first_poll_{ADDRESS}"


def _first_poll_task() -> asyncio.Task[Any]:
    """Return the entry's background first-poll task."""
    [task] = [t for t in asyncio.all_tasks() if t.get_name() == FIRST_POLL_TASK]
    return task


async def test_setup_and_unload(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """Setup loads every platform; unload stops polling and closes the link."""
    entry = setup_integration
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(MAIN_LIGHT) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    mock_device.stop.assert_awaited_once()
    assert connection(mock_device).closed

    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    await advance(hass, 2 * MAX_POLL_INTERVAL_SECONDS)
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]


async def test_unload_closes_link_after_polling_stops(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """The coordinator is shut down BEFORE the device link is closed."""
    order: list[str] = []
    real_shutdown = GoveeBleLocalCoordinator.async_shutdown

    async def _shutdown(self: GoveeBleLocalCoordinator) -> None:
        order.append("shutdown")
        await real_shutdown(self)

    mock_device.stop.side_effect = lambda: order.append("stop")  # type: ignore[attr-defined]
    # Patched before setup: the coordinator registers its shutdown when it is built.
    with patch.object(GoveeBleLocalCoordinator, "async_shutdown", _shutdown):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert order == ["shutdown", "stop"]


async def test_unload_does_not_wait_for_an_inflight_poll(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """A poll still in flight is cancelled on unload instead of stalling the link close.

    The library's close waits for the operation holding the connection; ``stop``
    models that here by waiting for the poll to end.
    """
    poll_ended = asyncio.Event()

    async def _hung_update(**_kwargs: Any) -> Any:
        try:
            await asyncio.Event().wait()
        finally:
            poll_ended.set()

    async def _stop() -> None:
        await poll_ended.wait()

    mock_device.update.side_effect = _hung_update  # type: ignore[attr-defined]
    mock_device.stop.side_effect = _stop  # type: ignore[attr-defined]
    coordinator = setup_integration.runtime_data.coordinator
    # The way the coordinator runs its interval polls: as an entry background task.
    poll = setup_integration.async_create_background_task(
        hass, coordinator.async_refresh(), "interval_poll"
    )
    await asyncio.sleep(0)
    assert not poll.done()

    async with asyncio.timeout(5):
        assert await hass.config_entries.async_unload(setup_integration.entry_id)
    assert poll.cancelled()
    mock_device.stop.assert_awaited_once()  # type: ignore[attr-defined]


async def test_unload_waits_for_a_running_self_test(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """Unloading mid self-test lets it finish its restore before the link is closed."""
    mock_device.state.is_on = True
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_rgb(*_args: Any) -> Any:
        started.set()
        await release.wait()
        return DEFAULT

    mock_device.set_rgb.side_effect = _slow_rgb  # type: ignore[attr-defined]
    data = setup_integration.runtime_data
    stopped_with: list[bool] = []
    mock_device.stop.side_effect = lambda: stopped_with.append(  # type: ignore[attr-defined]
        data.self_test is not None and data.self_test.done()
    )
    press = hass.async_create_task(
        hass.services.async_call("button", "press", {"entity_id": SELF_TEST_BUTTON}, blocking=True)
    )
    await started.wait()
    unload = hass.async_create_task(hass.config_entries.async_unload(setup_integration.entry_id))
    for _ in range(10):
        await asyncio.sleep(0)
    assert not unload.done()
    mock_device.stop.assert_not_awaited()  # type: ignore[attr-defined]

    release.set()
    assert await unload
    await press
    assert stopped_with == [True]
    report = data.last_self_test
    assert report is not None
    assert (report["ok"], report["restored"]) == (True, True)


async def test_unload_wait_for_a_self_test_is_bounded(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """A self-test that doesn't finish in time no longer holds the unload up."""
    mock_device.state.is_on = True
    started = asyncio.Event()
    link_closed = asyncio.Event()

    async def _stuck_rgb(*_args: Any) -> Any:
        started.set()
        await link_closed.wait()
        return DEFAULT

    mock_device.set_rgb.side_effect = _stuck_rgb  # type: ignore[attr-defined]
    mock_device.stop.side_effect = link_closed.set  # type: ignore[attr-defined]
    press = hass.async_create_task(
        hass.services.async_call("button", "press", {"entity_id": SELF_TEST_BUTTON}, blocking=True)
    )
    await started.wait()
    with patch("custom_components.govee_ble_local.SELF_TEST_UNLOAD_TIMEOUT_SECONDS", 0.01):
        async with asyncio.timeout(5):
            assert await hass.config_entries.async_unload(setup_integration.entry_id)
    mock_device.stop.assert_awaited_once()  # type: ignore[attr-defined]
    await press


# The failed unload leaves the entry (and its poll timer) loaded on purpose.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_unload_platform_failure_keeps_link(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """If the platforms fail to unload, the device link is left alone."""
    with patch.object(hass.config_entries, "async_unload_platforms", return_value=False):
        assert not await hass.config_entries.async_unload(setup_integration.entry_id)
    mock_device.stop.assert_not_awaited()


async def test_home_assistant_stop_closes_link(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """Home Assistant stopping closes the link: it doesn't unload config entries."""
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    mock_device.stop.assert_awaited_once()  # type: ignore[attr-defined]
    assert connection(mock_device).closed
    assert setup_integration.state is ConfigEntryState.LOADED


async def test_unload_after_home_assistant_stop(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: Device,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unload after the stop event closes the link a second time and logs no error."""
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert setup_integration.state is ConfigEntryState.NOT_LOADED
    assert mock_device.stop.await_count == 2  # type: ignore[attr-defined]
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


async def test_unload_removes_the_stop_listener(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """A stop event after an unload doesn't close the (already closed) link again."""
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    mock_device.stop.assert_awaited_once()  # type: ignore[attr-defined]


async def test_home_assistant_stop_waits_for_a_running_self_test(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """Stopping mid self-test lets it finish its restore before the link is closed."""
    mock_device.state.is_on = True
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_rgb(*_args: Any) -> Any:
        started.set()
        await release.wait()
        return DEFAULT

    data = setup_integration.runtime_data
    stopped_with: list[bool] = []

    def _stop() -> None:
        stopped_with.append(data.self_test is not None and data.self_test.done())
        # Like the library's close: any later write (a restore too) is refused.
        connection(mock_device).error = GoveeBleConnectionError("connection closed")

    mock_device.set_rgb.side_effect = _slow_rgb  # type: ignore[attr-defined]
    mock_device.stop.side_effect = _stop  # type: ignore[attr-defined]
    press = hass.async_create_task(
        hass.services.async_call("button", "press", {"entity_id": SELF_TEST_BUTTON}, blocking=True)
    )
    await started.wait()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    for _ in range(10):
        await asyncio.sleep(0)
    mock_device.stop.assert_not_awaited()  # type: ignore[attr-defined]

    release.set()
    await press
    await hass.async_block_till_done()
    assert stopped_with == [True]
    report = data.last_self_test
    assert report is not None
    assert (report["ok"], report["restored"]) == (True, True)


async def test_home_assistant_stop_wait_for_a_self_test_is_bounded(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: Device,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A self-test still running after the bound is cut off: its restore fails, logged once."""
    mock_device.state.is_on = True
    started = asyncio.Event()
    link_closed = asyncio.Event()

    async def _stuck_rgb(*_args: Any) -> Any:
        started.set()
        await link_closed.wait()
        return DEFAULT  # then the real write, which the closed link refuses

    def _stop() -> None:
        connection(mock_device).error = GoveeBleConnectionError("connection closed")
        link_closed.set()

    mock_device.set_rgb.side_effect = _stuck_rgb  # type: ignore[attr-defined]
    mock_device.stop.side_effect = _stop  # type: ignore[attr-defined]
    press = hass.async_create_task(
        hass.services.async_call("button", "press", {"entity_id": SELF_TEST_BUTTON}, blocking=True)
    )
    await started.wait()
    with patch("custom_components.govee_ble_local.SELF_TEST_STOP_TIMEOUT_SECONDS", 0.01):
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        async with asyncio.timeout(5):
            await hass.async_block_till_done()
    await press
    mock_device.stop.assert_awaited_once()  # type: ignore[attr-defined]
    report = setup_integration.runtime_data.last_self_test
    assert report is not None
    assert report["restored"] is False
    assert all("connection closed" in err for err in report["restore_errors"])
    warnings = [r for r in caplog.records if "could not fully restore" in r.getMessage()]
    assert len(warnings) == 1


async def test_home_assistant_stop_does_not_wait_long_for_a_busy_link(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: Device,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A close stuck behind an operation still using the link stops waiting after the bound."""
    caplog.set_level(logging.DEBUG, logger="custom_components.govee_ble_local")
    close_cancelled = asyncio.Event()

    async def _busy_close() -> None:
        # The library's close waits for the operation holding the link.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            close_cancelled.set()
            raise

    mock_device.stop.side_effect = _busy_close  # type: ignore[attr-defined]
    with patch("custom_components.govee_ble_local.STOP_CLOSE_TIMEOUT_SECONDS", 0.01):
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        async with asyncio.timeout(5):
            await hass.async_block_till_done()
    assert close_cancelled.is_set()
    assert "link still busy at shutdown" in caplog.text
    # Let the fixture's unload close promptly (HA would wait 10 s for a hung stop()).
    mock_device.stop.side_effect = None  # type: ignore[attr-defined]


async def test_library_stop_twice_is_harmless() -> None:
    """The library's ``Device.stop()`` may run twice: the stop event, then an unload.

    The first call drops the open link and refuses further I/O; the second
    finds no link and returns. The library has no test of its own for this.
    """
    device = create_device(BLEDevice(address=ADDRESS, name=LOCAL_NAME, details={}), "H60A6", None)
    client = MagicMock()
    client.disconnect = AsyncMock()
    # An open link. Relies on the private GoveeConnection._client
    # (govee_ble_local/transport/connection.py); a library rename breaks this line.
    device._conn._client = client
    await device.stop()
    await device.stop()
    client.disconnect.assert_awaited_once()
    with pytest.raises(GoveeBleConnectionError):
        await device.set_power(True)


async def test_setup_publishes_passive_state_before_first_poll(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """Entities are available from passive state while the first poll is still running."""
    release = asyncio.Event()

    async def _slow_update(**_kwargs: Any) -> object:
        await release.wait()
        mock_device.state.optimistic = False
        return mock_device.state

    mock_device.update.side_effect = _slow_update  # type: ignore[attr-defined]
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=False)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(MAIN_LIGHT).state != STATE_UNAVAILABLE
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    coordinator = mock_config_entry.runtime_data.coordinator
    assert coordinator.last_connected is None

    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    assert coordinator.last_update_success
    assert coordinator.last_connected is not None


async def test_first_poll_failure_is_backoff_not_setup_failure(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """A failing first poll still loads the entry; the coordinator backs off."""
    mock_device.update.side_effect = GoveeBleConnectionError("no slot")  # type: ignore[attr-defined]
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    coordinator = mock_config_entry.runtime_data.coordinator
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    assert coordinator.consecutive_failures == 1
    assert coordinator.total_failures == 1


async def test_first_poll_is_staggered(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """The first poll waits a per-device stagger derived from the address."""
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    # The patch replaces asyncio.sleep process-wide: record only the first poll's.
    async def _sleep(delay: float) -> None:
        task = asyncio.current_task()
        if task is not None and task.get_name() == FIRST_POLL_TASK:
            sleeps.append(delay)
        await real_sleep(0)

    with (
        patch("custom_components.govee_ble_local.FIRST_POLL_STAGGER_SECONDS", 30),
        patch("custom_components.govee_ble_local.asyncio.sleep", _sleep),
    ):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert sleeps == [zlib.crc32(ADDRESS.encode()) % 30]
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]


async def test_first_poll_stagger_counts_only_active_entries(hass: HomeAssistant) -> None:
    """Ignored and disabled entries don't widen the first-poll window."""
    MockConfigEntry(domain=DOMAIN, unique_id="AA:BB:CC:DD:00:01").add_to_hass(hass)
    for n in range(20):
        MockConfigEntry(
            domain=DOMAIN, unique_id=f"AA:BB:CC:DD:01:{n:02X}", source="ignore"
        ).add_to_hass(hass)
        MockConfigEntry(
            domain=DOMAIN,
            unique_id=f"AA:BB:CC:DD:02:{n:02X}",
            disabled_by=ConfigEntryDisabler.USER,
        ).add_to_hass(hass)
    with (
        patch("custom_components.govee_ble_local.FIRST_POLL_STAGGER_SECONDS", 30),
        patch("custom_components.govee_ble_local.FIRST_POLL_STAGGER_PER_DEVICE_SECONDS", 5),
    ):
        assert _first_poll_stagger(hass, ADDRESS) == zlib.crc32(ADDRESS.encode()) % 30


@pytest.mark.parametrize(("devices", "window"), [(1, 30), (10, 50), (40, 100)])
async def test_first_poll_stagger_window_grows_with_the_device_count(
    hass: HomeAssistant, devices: int, window: int
) -> None:
    """The first polls spread over 5 s per active device, from 30 s up to 100 s."""
    for n in range(devices):
        MockConfigEntry(
            domain=DOMAIN, unique_id=f"AA:BB:CC:DD:{n // 256:02X}:{n % 256:02X}"
        ).add_to_hass(hass)
    with (
        patch("custom_components.govee_ble_local.FIRST_POLL_STAGGER_SECONDS", 30),
        patch("custom_components.govee_ble_local.FIRST_POLL_STAGGER_PER_DEVICE_SECONDS", 5),
    ):
        stagger = _first_poll_stagger(hass, ADDRESS)
    assert stagger == zlib.crc32(ADDRESS.encode()) % window


async def test_first_poll_waits_for_a_command_sent_during_its_stagger(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """A command sent during the first poll's stagger holds it off like any background poll."""
    real_sleep = asyncio.sleep
    stagger_over = asyncio.Event()

    async def _sleep(delay: float) -> None:
        task = asyncio.current_task()
        if task is not None and task.get_name() == FIRST_POLL_TASK:
            await stagger_over.wait()
        else:
            await real_sleep(0)

    with patch("custom_components.govee_ble_local.asyncio.sleep", _sleep):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=False)
        coordinator = mock_config_entry.runtime_data.coordinator
        coordinator.async_note_command_started()  # during the stagger
        stagger_over.set()
        for _ in range(20):
            await real_sleep(0)
    assert coordinator._first_poll_waiting
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]

    coordinator.async_note_command_finished()
    await advance(hass, POLL_HOLDOFF_AFTER_COMMAND_SECONDS - 5)
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]
    await advance(hass, POLL_HOLDOFF_AFTER_COMMAND_SECONDS + 1)
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]


async def test_unreadable_scene_store_sets_up_without_effects(
    hass: HomeAssistant,
    broken_scene_store: SimpleNamespace,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    *,
    mock_bluetooth: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken scene store costs the light its effects, not its setup; nothing reads it
    on the event loop."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data.scene_store_error == str(broken_scene_store.error)

    # The report is the only error: no entity update failed on a scene read either
    # (the coordinator would log that, not raise it).
    [record] = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "scene store" in record.getMessage()
    assert "govee-ble-local" in record.getMessage()
    assert str(broken_scene_store.error) in record.getMessage()
    assert "diagnostics" in record.getMessage()  # what to do if reinstalling doesn't help

    attrs = hass.states.get("light.test_ceiling").attributes
    assert "effect_list" not in attrs
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.test_ceiling", "brightness": 128}, blocking=True
    )
    mock_device.turn_on.assert_awaited()  # type: ignore[attr-defined]
    assert hass.states.get("light.test_ceiling").attributes["brightness"] == 128
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == [record]
    assert broken_scene_store.on_loop  # the store was read...
    assert not any(broken_scene_store.on_loop)  # ...but never on the event loop


async def test_unreadable_scene_store_is_reported_once_per_run(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Every device hits the same broken store: one error, then debug lines."""
    err = GoveeBleError("scene catalog could not be read")
    with (
        caplog.at_level(logging.DEBUG, logger="custom_components.govee_ble_local"),
        # It runs on the event loop: no package-metadata read (that's file I/O).
        patch("importlib.metadata.version", side_effect=AssertionError("metadata read")),
    ):
        _async_report_scene_store(hass, err)
        _async_report_scene_store(hass, err)
    records = [r for r in caplog.records if "scene store" in r.getMessage().lower()]
    assert [r.levelno for r in records] == [logging.ERROR, logging.DEBUG]
    assert govee_ble_local.__version__ in records[0].getMessage()


async def test_setup_not_ready_when_device_not_seen(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """No connectable BLEDevice yet: retry later."""
    mock_bluetooth.ble_lookup.return_value = None
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_sku_from_advertisement(
    hass: HomeAssistant, mock_device: Device, mock_bluetooth: SimpleNamespace
) -> None:
    """An entry without a stored SKU identifies it from the advertisement.

    Also pins the rest of the ``create_device`` call: no secret, and the short
    idle-link delay (POLL_IDLE_DISCONNECT_SECONDS instead of the library's 8 s).
    """
    entry = MockConfigEntry(
        domain=DOMAIN, title=TITLE, unique_id=ADDRESS, data={"address": ADDRESS}
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.govee_ble_local.create_device", return_value=mock_device
    ) as create:
        assert await hass.config_entries.async_setup(entry.entry_id)
    ble_device, sku, advertisement = create.call_args.args
    assert sku == "H60A6"
    assert ble_device is mock_bluetooth.ble_lookup.return_value
    assert advertisement is mock_bluetooth.last_info.return_value.advertisement
    assert create.call_args.kwargs == {
        "secret": None,
        "idle_disconnect": POLL_IDLE_DISCONNECT_SECONDS,
        "allow_modelled": False,  # an entry without the opt-in
    }
    # A finished poll's link is dropped soon: 0 or less would never drop it.
    assert POLL_IDLE_DISCONNECT_SECONDS > 0


async def test_setup_passes_the_modelled_opt_in(
    hass: HomeAssistant, mock_device: Device, mock_bluetooth: SimpleNamespace
) -> None:
    """An entry the user opted in for a "modelled" model sets the device up with it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=ADDRESS,
        data={"address": ADDRESS, CONF_SKU: "H60A6", CONF_ALLOW_MODELLED: True},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.govee_ble_local.create_device", return_value=mock_device
    ) as create:
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert create.call_args.kwargs["allow_modelled"] is True


@pytest.mark.parametrize(
    "service_info",
    [None, make_service_info(name="Other"), make_service_info(name="", manufacturer_data={})],
    ids=["no_advertisement", "foreign_name", "no_name"],
)
async def test_setup_retries_when_sku_unknown(
    hass: HomeAssistant,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    service_info: BluetoothServiceInfoBleak | None,
) -> None:
    """No stored SKU and no advertisement that identifies it: retry later."""
    mock_bluetooth.last_info.return_value = service_info
    entry = MockConfigEntry(
        domain=DOMAIN, title=TITLE, unique_id=ADDRESS, data={"address": ADDRESS}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unsupported_model_is_a_permanent_error(
    hass: HomeAssistant, mock_bluetooth: SimpleNamespace
) -> None:
    """An unsupported SKU fails setup for good instead of retrying forever."""
    entry = MockConfigEntry(
        domain=DOMAIN, title=TITLE, unique_id=ADDRESS, data={"address": ADDRESS, CONF_SKU: "H9999"}
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.govee_ble_local.create_device", wraps=create_device
    ) as create:  # the real library refuses it
        await hass.config_entries.async_setup(entry.entry_id)
    assert create.call_args.args[1] == "H9999"
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "model_not_supported"
    assert entry.error_reason_translation_placeholders == {"sku": "H9999"}
    assert entry.reason is not None
    assert entry.reason.startswith("The Govee model H9999 is not supported")


@pytest.mark.parametrize(
    ("pact", "name", "state"),
    [
        ((1, 1), "GVH6009ABCD", ConfigEntryState.LOADED),
        ((2, 3), "GVH6009ABCD", ConfigEntryState.SETUP_RETRY),
        ((1, 1), "5134P0098101970F", ConfigEntryState.LOADED),
        ((2, 3), "5134P0098101970F", ConfigEntryState.SETUP_RETRY),
    ],
    ids=["accepted_pact", "refused_pact", "accepted_pact_renamed", "refused_pact_renamed"],
)
async def test_setup_with_the_real_library_routes_by_the_advertised_pact(
    hass: HomeAssistant,
    presence: SimpleNamespace,
    mock_bluetooth: SimpleNamespace,
    *,
    pact: tuple[int, int],
    name: str,
    state: ConfigEntryState,
) -> None:
    """The real create_device builds the H6009 at its advertised pact, or setup retries.

    Only the BLE transport is faked, so the keyword arguments setup passes and the
    library's pact routing both run for real. The H6009 is served only at its row's
    pacts: at another one the model is supported but refused, which can change with a
    later advert, so setup retries instead of failing for good. That holds when Home
    Assistant's record shows another name (another device's advertisement merged in):
    the entry's model and its Govee manufacturer data decide, not the name.
    """
    info = make_service_info(name=name, manufacturer_data=pact_mfg(*pact), advertisement=True)
    presence.service_info = mock_bluetooth.last_info.return_value = info
    entry = MockConfigEntry(
        domain=DOMAIN, title=TITLE, unique_id=ADDRESS, data={"address": ADDRESS, CONF_SKU: "H6009"}
    )
    entry.add_to_hass(hass)
    with fake_transport() as transport:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.state is state
        if state is ConfigEntryState.LOADED:
            device = entry.runtime_data.device
            assert device.profile == profile_for("H6009", pact=pact)
            assert len(transport) == 1  # built on the fake: no radio
            # The advert's encrypted Govee block decides, whatever the name: library 1.6.1
            # (the floor) does so for a renamed record too; 1.6.0 fell back to the profile's
            # cipher. Read through the public Device.encryption (the fake is the real class).
            assert device.pact == pact
            assert device.encryption is Encryption.AES_RC4_PSK
            assert await hass.config_entries.async_unload(entry.entry_id)
        else:
            assert entry.error_reason_translation_domain == DOMAIN
            assert entry.error_reason_translation_key == "protocol_not_supported"
            assert entry.error_reason_translation_placeholders == {
                "sku": "H6009",
                "protocol": "2,3",
            }
            assert entry.reason is not None
            assert "advertises protocol version 2,3" in entry.reason


@pytest.mark.parametrize(
    ("opted_in", "other_pact", "state"),
    [
        (True, False, ConfigEntryState.LOADED),
        (False, False, ConfigEntryState.SETUP_ERROR),  # not supported without the opt-in
        (True, True, ConfigEntryState.SETUP_RETRY),  # served as modelled, not at this pact
    ],
    ids=["opted_in", "not_opted_in", "opted_in_other_pact"],
)
async def test_setup_with_the_real_library_takes_the_modelled_opt_in(
    hass: HomeAssistant,
    presence: SimpleNamespace,
    mock_bluetooth: SimpleNamespace,
    modelled: ModelledCase,
    *,
    opted_in: bool,
    other_pact: bool,
    state: ConfigEntryState,
) -> None:
    """The real create_device sets a modelled-only model up only for an entry that opted in.

    At a pact its rows don't list, the opted-in model is still one the library serves, so
    setup retries rather than failing for good.
    """
    info = modelled.info(
        manufacturer_data=pact_mfg(*modelled.unlisted_pact) if other_pact else None,
        advertisement=True,
    )
    presence.service_info = mock_bluetooth.last_info.return_value = info
    data: dict[str, Any] = {"address": ADDRESS, CONF_SKU: modelled.sku}
    if opted_in:
        data[CONF_ALLOW_MODELLED] = True
    entry = MockConfigEntry(domain=DOMAIN, title=TITLE, unique_id=ADDRESS, data=data)
    entry.add_to_hass(hass)
    with fake_transport():
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.state is state
        if state is ConfigEntryState.LOADED:
            assert entry.runtime_data.device.profile == profile_for(
                modelled.sku, pact=modelled.pact, allow_modelled=True
            )
            assert await hass.config_entries.async_unload(entry.entry_id)
        if other_pact:
            assert entry.error_reason_translation_key == "protocol_not_supported"
            assert entry.error_reason_translation_placeholders == {
                "sku": modelled.sku,
                "protocol": "{},{}".format(*modelled.unlisted_pact),
            }


# -- the stored broadcast header -------------------------------------------------
#
# The device-list library release serves some models only at the pact their rows list (none
# without one), and picks other models' profile by it. These tests build such routing for a
# made-up model: a profile of its own, served at the pacts each case names. The real
# create_device and Device run; only the library's route table is replaced.

SKU = "H0PA1"
NAME = f"Govee_{SKU}_ABCD"
LIGHT = DeviceProfile(
    skus=(SKU,),
    capabilities=frozenset({Capability.POWER, Capability.BRIGHTNESS}),
    encryption=Encryption.NONE,
)
# Another profile for the same model: a BK unit's pact-less one, say (another brightness byte).
PACTLESS = dataclasses.replace(LIGHT, brightness_range=(0, 254))
HEADER_2_1, HEADER_9_9 = broadcast_data(2, 1), broadcast_data(9, 9)
_LOADED, _RETRY, _ERROR = (
    ConfigEntryState.LOADED,
    ConfigEntryState.SETUP_RETRY,
    ConfigEntryState.SETUP_ERROR,
)


@contextmanager
def _served(routes: Mapping[tuple[int, int] | None, DeviceProfile] | None) -> Iterator[None]:
    """Make the library serve SKU with ``routes[pact]`` (None: no pact), listing it unless None."""
    real_profile_for = govee_ble_local.registry.profile_for
    real_supported = govee_ble_local.supported_skus

    def _profile_for(
        name: str, *, pact: tuple[int, int] | None = None, **kwargs: Any
    ) -> DeviceProfile | None:
        if name.upper() == SKU:
            return None if routes is None else routes.get(pact)
        return real_profile_for(name, pact=pact, **kwargs)

    def _supported(*, allow_modelled: bool = False) -> list[str]:
        listed = real_supported(allow_modelled=allow_modelled)
        return listed if routes is None else sorted({*listed, SKU})

    with (
        patch("govee_ble_local.registry.profile_for", _profile_for),
        patch("custom_components.govee_ble_local.coordinator.profile_for", _profile_for),
        patch("custom_components.govee_ble_local.supported_skus", _supported),
    ):
        yield


async def _set_up(
    hass: HomeAssistant,
    presence: SimpleNamespace,
    mock_bluetooth: SimpleNamespace,
    record: BluetoothServiceInfoBleak,
    stored: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Set an entry for SKU up while Home Assistant's record of the device is ``record``."""
    presence.service_info = mock_bluetooth.last_info.return_value = record
    data: dict[str, Any] = {"address": ADDRESS, CONF_SKU: SKU}
    if stored is not None:
        data[CONF_BROADCAST] = stored
    entry = MockConfigEntry(domain=DOMAIN, title=TITLE, unique_id=ADDRESS, data=data)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def _record(pact: tuple[int, int] | None) -> BluetoothServiceInfoBleak:
    """Home Assistant's record of the device: its advert at ``pact``, or one without the
    Govee block (None), as a record restored at startup or merged with another can be."""
    mfg = {} if pact is None else pact_mfg(*pact)
    return make_service_info(name=NAME, manufacturer_data=mfg, advertisement=True)


@pytest.mark.parametrize(
    ("routes", "stored", "advertised", "state", "error", "kept"),
    [
        # A model served only at (2,1).
        ({(2, 1): LIGHT}, None, None, _RETRY, "protocol_unknown", None),
        ({(2, 1): LIGHT}, HEADER_2_1, None, _LOADED, None, HEADER_2_1),
        ({(2, 1): LIGHT}, None, (2, 1), _LOADED, None, HEADER_2_1),
        ({(2, 1): LIGHT}, HEADER_2_1, (2, 1), _LOADED, None, HEADER_2_1),
        ({(2, 1): LIGHT}, HEADER_2_1, (9, 9), _RETRY, "protocol_not_supported", HEADER_9_9),
        ({(2, 1): LIGHT}, HEADER_9_9, None, _RETRY, "protocol_not_supported", HEADER_9_9),
        # A model whose pact picks its profile.
        ({None: PACTLESS, (2, 1): LIGHT}, HEADER_2_1, None, _LOADED, None, HEADER_2_1),
        # Served without a pact, as every model is today: set up as before, nothing stored.
        ({None: LIGHT}, None, None, _LOADED, None, None),
        # Not listed at any pact: no advert can serve it.
        (None, HEADER_2_1, None, _ERROR, "model_not_supported", HEADER_2_1),
    ],
    ids=[
        "nothing_known",
        "stored",
        "advertised",
        "advertised_as_stored",
        "advertised_refused",
        "stored_refused",
        "stored_picks_the_profile",
        "served_without_a_pact",
        "not_listed",
    ],
)
async def test_setup_routes_by_the_advertised_else_the_stored_header(
    hass: HomeAssistant,
    presence: SimpleNamespace,
    mock_bluetooth: SimpleNamespace,
    *,
    routes: Mapping[tuple[int, int] | None, DeviceProfile] | None,
    stored: dict[str, Any] | None,
    advertised: tuple[int, int] | None,
    state: ConfigEntryState,
    error: str | None,
    kept: dict[str, Any] | None,
) -> None:
    """The advertised header routes the model, else the stored one; the entry keeps the latest.

    A restart's record can carry no Govee block until the device advertises again. A model
    the library lists but can't serve at the known pact, or with none known, retries: a later
    advert can serve it. Only a model it doesn't list fails for good. The stored encrypt flag
    picks the cipher, as the advertised one does.
    """
    with _served(routes), fake_transport():
        entry = await _set_up(hass, presence, mock_bluetooth, _record(advertised), stored)
        assert entry.state is state
        assert entry.error_reason_translation_key == error
        assert entry.data.get(CONF_BROADCAST) == kept
        if state is _LOADED:
            assert routes is not None
            pact = advertised or (tuple(stored["pact"]) if stored else None)
            device = entry.runtime_data.device
            assert device.profile is routes[pact]
            assert device.pact == pact
            assert device.encryption is (
                Encryption.NONE if pact is None else Encryption.AES_RC4_PSK
            )
            assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("routes", "stored", "reloaded"),
    [
        ({None: PACTLESS, (10, 1): LIGHT}, None, True),
        ({None: LIGHT, (10, 1): LIGHT}, None, False),
        ({(2, 1): LIGHT, (10, 1): LIGHT}, HEADER_2_1, False),
    ],
    ids=["routes_elsewhere", "same_profile", "stale_stored"],
)
async def test_a_header_heard_after_setup(
    hass: HomeAssistant,
    presence: SimpleNamespace,
    mock_bluetooth: SimpleNamespace,
    *,
    routes: Mapping[tuple[int, int] | None, DeviceProfile],
    stored: dict[str, Any] | None,
    reloaded: bool,
) -> None:
    """The header of the record setup routes by is stored once heard, replacing a stored one
    of another pact (a firmware update's). A device set up without a pact reloads when that
    pact routes the model to another profile, so setup builds it there."""
    set_up_at = tuple(stored["pact"]) if stored else None
    with _served(routes), fake_transport():
        entry = await _set_up(hass, presence, mock_bluetooth, _record(None), stored)
        device = entry.runtime_data.device
        assert device.profile is routes[set_up_at]
        heard = _record((10, 1))
        presence.service_info = mock_bluetooth.last_info.return_value = heard
        advertisement_callback(mock_bluetooth)(heard, BluetoothChange.ADVERTISEMENT)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.data[CONF_BROADCAST] == broadcast_data(10, 1)
        assert entry.state is ConfigEntryState.LOADED
        assert (entry.runtime_data.device is not device) is reloaded
        assert entry.runtime_data.device.profile is (LIGHT if reloaded else routes[set_up_at])
        assert await hass.config_entries.async_unload(entry.entry_id)


# A power-only light: POWER alone on a profile that isn't a relay plug's.
ONOFF = dataclasses.replace(LIGHT, capabilities=frozenset({Capability.POWER}))


@pytest.mark.parametrize(
    # The model's profile without a pact: another power-only light (as PACTLESS is another
    # LIGHT), or a relay plug, which no model is today.
    "pactless",
    [dataclasses.replace(ONOFF, brightness_range=(0, 254)), dataclasses.replace(ONOFF, relay=True)],
    ids=["light_without_a_pact", "relay_plug_without_a_pact"],
)
async def test_a_power_switch_carried_over_stays_a_light_through_later_setups(
    hass: HomeAssistant,
    presence: SimpleNamespace,
    mock_bluetooth: SimpleNamespace,
    *,
    pactless: DeviceProfile,
) -> None:
    """An earlier version's power switch moves to the power-only light once, whichever setup
    first builds the light; every later setup keeps that light as it is.

    Set up without a pact, the device has its model's pact-less profile, and its first pact
    reloads the entry at the power-only light. A pact-less profile that is a light carries
    the switch over at the first setup; a relay plug's keeps the switch until the reload.
    Then the light has the switch's object id, settings and voice exposure, through that
    reload and a later one, and nothing is created twice.
    """
    assert await async_setup_component(hass, "homeassistant", {})
    registry = er.async_get(hass)
    area = ar.async_get(hass).async_create("Office")
    label = lr.async_get(hass).async_create("Desk")
    presence.service_info = mock_bluetooth.last_info.return_value = _record(None)
    entry = MockConfigEntry(
        domain=DOMAIN, title=TITLE, unique_id=ADDRESS, data={"address": ADDRESS, CONF_SKU: SKU}
    )
    entry.add_to_hass(hass)
    old = registry.async_get_or_create(
        "switch", DOMAIN, f"{ADDRESS}_power", config_entry=entry, suggested_object_id="desk"
    )
    old = registry.async_update_entity(
        old.entity_id,
        name="Desk",
        icon="mdi:desk",
        area_id=area.id,
        labels={label.label_id},
        aliases=["Desk light"],
    )
    exposed_entities.async_expose_entity(hass, "conversation", old.entity_id, True)

    async def _hear_the_pact() -> None:
        heard = _record((10, 1))
        presence.service_info = mock_bluetooth.last_info.return_value = heard
        advertisement_callback(mock_bluetooth)(heard, BluetoothChange.ADVERTISEMENT)
        await hass.async_block_till_done(wait_background_tasks=True)

    async def _reload() -> None:
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    with _served({None: pactless, (10, 1): ONOFF}), fake_transport():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        switch = registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_power")
        assert (switch is not None) is pactless.relay
        for later_setup in (_hear_the_pact, _reload):
            await later_setup()
            assert entry.state is ConfigEntryState.LOADED
            assert entry.runtime_data.device.profile is ONOFF
            entities = er.async_entries_for_config_entry(registry, entry.entry_id)
            assert [e.entity_id for e in entities if e.domain in ("light", "switch")] == [
                "light.desk"
            ]
            light = registry.async_get("light.desk")
            assert (light.unique_id, light.name, light.icon, light.area_id) == (
                f"{ADDRESS}_light",
                "Desk",
                "mdi:desk",
                area.id,
            )
            assert (light.labels, light.aliases) == ({label.label_id}, old.aliases)
            assert exposed_entities.async_should_expose(hass, "conversation", "light.desk")
            assert hass.states.get("light.desk").attributes["friendly_name"] == "Desk"
            assert hass.states.async_entity_ids("switch") == []
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_passes_secret_to_library(
    hass: HomeAssistant, mock_device: Device, mock_bluetooth: SimpleNamespace
) -> None:
    """A stored secret is handed to the library as bytes."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=ADDRESS,
        data={"address": ADDRESS, "sku": "H60A6", CONF_SECRET: SECRET_HEX},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.govee_ble_local.create_device", return_value=mock_device
    ) as create:
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert create.call_args.kwargs["secret"] == bytes.fromhex(SECRET_HEX)


@pytest.mark.parametrize("bad_secret", ["zz", "a1b2"])
async def test_malformed_stored_secret_starts_reauth(
    hass: HomeAssistant, mock_device: Device, mock_bluetooth: SimpleNamespace, bad_secret: str
) -> None:
    """A corrupt stored secret fails setup with an auth error (reauth), not a crash."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=ADDRESS,
        data={"address": ADDRESS, "sku": "H60A6", CONF_SECRET: bad_secret},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


async def test_bluetooth_callbacks_registered_and_released(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """Advertisements, presence and both unavailability histories are tracked, then released."""
    register = mock_bluetooth.register
    matcher, mode = register.call_args.args[2], register.call_args.args[3]
    # Any scanner, connectable or not: passive ones report on/off and presence too.
    assert matcher == {"address": ADDRESS, "connectable": False}
    assert mode is BluetoothScanningMode.PASSIVE
    assert mock_bluetooth.ble_lookup.call_args.kwargs == {"connectable": True}
    connectable_flags = sorted(
        call.kwargs["connectable"] for call in mock_bluetooth.track_unavailable.call_args_list
    )
    assert connectable_flags == [False, True]
    coordinator = setup_integration.runtime_data.coordinator
    presence.service_info = make_service_info(rssi=-81)
    await advance(hass, LAST_SEEN_RESOLUTION_SECONDS)
    assert coordinator.rssi == -81  # the presence refresh runs on a timer

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    register.return_value.assert_called_once()
    assert mock_bluetooth.track_unavailable.return_value.call_count == 2
    presence.service_info = make_service_info(rssi=-50)
    await advance(hass, LAST_SEEN_RESOLUTION_SECONDS)
    assert coordinator.rssi == -81  # ...which stops with the entry


async def test_log_capture_detached_on_unload(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The always-on warning capture is unregistered when the entry unloads."""
    log_capture = setup_integration.runtime_data.log_capture
    assert log_capture in capture._ROUTER.captures
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    assert log_capture not in capture._ROUTER.captures


async def test_passive_advertisement_updates_on_off(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """A real advertisement's on/off byte drives the light without a connection."""
    allow_adverts(mock_device)
    connection(mock_device).sent.clear()
    mock_device.update.reset_mock()  # type: ignore[attr-defined]
    callback = advertisement_callback(mock_bluetooth)
    callback(make_service_info(on=True), BluetoothChange.ADVERTISEMENT)
    assert hass.states.get(MAIN_LIGHT).state == STATE_ON
    callback(make_service_info(on=False), BluetoothChange.ADVERTISEMENT)
    assert hass.states.get(MAIN_LIGHT).state == STATE_OFF
    # Purely passive: no frames sent, and no poll started.
    assert not connection(mock_device).sent
    mock_device.update.assert_not_called()  # type: ignore[attr-defined]


async def test_passive_advertisement_does_not_reschedule_poll(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """On/off flips from advertisements neither postpone the poll nor mask failures."""
    coordinator = setup_integration.runtime_data.coordinator
    coordinator.last_update_success = False
    allow_adverts(mock_device)
    callback = advertisement_callback(mock_bluetooth)
    with patch.object(coordinator, "_schedule_refresh") as schedule:
        callback(make_service_info(on=True), BluetoothChange.ADVERTISEMENT)
        callback(make_service_info(on=False), BluetoothChange.ADVERTISEMENT)
    schedule.assert_not_called()
    assert coordinator.last_update_success is False


async def test_only_connectable_advertisements_refresh_the_ble_device(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """Any scanner's advertisement drives on/off; only a connectable one supplies the BLEDevice."""
    allow_adverts(mock_device)
    callback = advertisement_callback(mock_bluetooth)
    connectable = make_service_info(on=True)
    callback(connectable, BluetoothChange.ADVERTISEMENT)
    assert mock_device._ble_device is connectable.device
    assert hass.states.get(MAIN_LIGHT).state == STATE_ON

    passive = make_service_info(on=False, source="passive-proxy", connectable=False)
    callback(passive, BluetoothChange.ADVERTISEMENT)
    assert hass.states.get(MAIN_LIGHT).state == STATE_OFF  # its on/off counts...
    assert mock_device._ble_device is connectable.device  # ...but it can't be connected through


async def test_unavailable_tracking_flips_availability(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """Losing every scanner pushes the state change and clears the stale RSSI."""
    coordinator = setup_integration.runtime_data.coordinator
    coordinator.rssi = -60
    coordinator.consecutive_failures = 5  # polls already failing
    presence.connectable = presence.any = False
    unavailable_callback(mock_bluetooth, connectable=False)(make_service_info())
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE
    assert coordinator.rssi is None
    assert hass.states.get("binary_sensor.test_ceiling_connectivity").state == STATE_OFF

    # Losing only the connectable history keeps the RSSI (still heard).
    coordinator.rssi = -60
    unavailable_callback(mock_bluetooth, connectable=True)(make_service_info())
    assert coordinator.rssi == -60


async def test_device_returning_triggers_poll_and_resets_backoff(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """A device back from absence is polled right away, not after the backoff."""
    coordinator = setup_integration.runtime_data.coordinator
    presence.connectable = presence.any = False
    for _ in range(5):
        await coordinator.async_refresh()
    assert coordinator.update_interval.total_seconds() >= 1800
    mock_device.update.reset_mock()  # type: ignore[attr-defined]

    presence.connectable = presence.any = True
    advertisement_callback(mock_bluetooth)(make_service_info(), BluetoothChange.ADVERTISEMENT)
    await hass.async_block_till_done()
    mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
    assert coordinator.update_interval.total_seconds() < 200
    assert coordinator.consecutive_failures == 0
    assert hass.states.get(MAIN_LIGHT).state != STATE_UNAVAILABLE


async def test_device_registry_enriched_without_clearing(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """Read-back device info is added; unknown values never clear existing ones."""
    registry = dr.async_get(hass)
    entry = registry.async_get_device_by_identifier((DOMAIN, ADDRESS), setup_integration.entry_id)
    assert entry is not None
    assert entry.name == TITLE
    assert entry.model == "H60A6"

    state = mock_device.state
    state.firmware_version, state.hardware_version = "1.02.03", "3.01.01"
    state.serial_number, state.wifi_mac = "SN123456", "11:22:33:44:55:66"
    setup_integration.runtime_data.coordinator.async_publish_device_state()
    entry = registry.async_get_device_by_identifier((DOMAIN, ADDRESS), setup_integration.entry_id)
    assert (entry.sw_version, entry.hw_version, entry.serial_number) == (
        "1.02.03",
        "3.01.01",
        "SN123456",
    )
    assert (dr.CONNECTION_NETWORK_MAC, "11:22:33:44:55:66") in entry.connections

    # A restart starts from a new, empty device state; the registry keeps what it knew.
    with patch("custom_components.govee_ble_local.create_device", return_value=make_device()):
        await hass.config_entries.async_reload(setup_integration.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert setup_integration.runtime_data.device.state.firmware_version is None
    entry = registry.async_get_device_by_identifier((DOMAIN, ADDRESS), setup_integration.entry_id)
    assert (entry.sw_version, entry.hw_version, entry.serial_number) == (
        "1.02.03",
        "3.01.01",
        "SN123456",
    )


async def test_registry_sync_does_not_keep_the_device_polled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """With no entity listening (all disabled) polling stops; the registry is still enriched."""

    async def _update(**_kwargs: Any) -> Any:
        mock_device.state.firmware_version = "1.02.03"
        mock_device.state.optimistic = False
        return mock_device.state

    mock_device.update.side_effect = _update  # type: ignore[attr-defined]
    with patch("custom_components.govee_ble_local.PLATFORMS", []):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        device = dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, ADDRESS), mock_config_entry.entry_id
        )
        assert device is not None
        assert device.sw_version == "1.02.03"  # from the first poll
        mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]

        await advance(hass, MAX_POLL_INTERVAL_SECONDS)
        mock_device.update.assert_awaited_once()  # type: ignore[attr-defined]
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def test_entities_only_link_to_the_registry_device(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The device record has one writer (the registry sync); entities only link to it."""
    entity = hass.data[LIGHT_DATA].get_entity(MAIN_LIGHT)
    assert entity is not None
    assert entity.device_info == {"identifiers": {(DOMAIN, ADDRESS)}}
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ADDRESS), setup_integration.entry_id
    )
    assert device is not None
    assert (device.name, device.manufacturer, device.model) == (TITLE, "Govee", "H60A6")
    assert er.async_get(hass).async_get(MAIN_LIGHT).device_id == device.id


async def test_legacy_zone_switches_removed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """Old index-keyed and now-colour-capable zone switches are dropped."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for unique_id in (f"{ADDRESS}_zone_0", f"{ADDRESS}_zone_main", f"{ADDRESS}_zone_background"):
        registry.async_get_or_create("switch", DOMAIN, unique_id, config_entry=mock_config_entry)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    for unique_id in (f"{ADDRESS}_zone_0", f"{ADDRESS}_zone_main", f"{ADDRESS}_zone_background"):
        assert registry.async_get_entity_id("switch", DOMAIN, unique_id) is None


@pytest.mark.parametrize(
    ("device_sku", "case"),
    [("H6006", "power_only_light"), ("H6006", "light_registered"), ("H5083", "relay_plug")],
    ids=["power_only_light", "light_registered", "relay_plug"],
)
async def test_power_switch_moves_over_to_the_light(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth: SimpleNamespace,
    case: str,
) -> None:
    """An earlier version's power switch of a power-only light becomes its light; a plug keeps it.

    Earlier versions made every power-only device a switch. The light takes the switch's object
    id, the user's settings and its voice-assistant exposure, a hide the user chose too, but not
    a disable an integration set. A light entry already registered keeps its own settings. A
    relay plug's switch is its entity: the same registry entry, untouched.
    """
    assert await async_setup_component(hass, "homeassistant", {})
    device = make_device("H5083") if case == "relay_plug" else make_power_only_light()
    registry = er.async_get(hass)
    area = ar.async_get(hass).async_create("Office")
    label = lr.async_get(hass).async_create("Desk")
    mock_config_entry.add_to_hass(hass)
    if case == "light_registered":  # e.g. from a release with the light, then a downgrade
        registry.async_get_or_create(
            "light",
            DOMAIN,
            f"{ADDRESS}_light",
            config_entry=mock_config_entry,
            suggested_object_id="kept",
            disabled_by=er.RegistryEntryDisabler.USER,
        )
    old = registry.async_get_or_create(
        "switch",
        DOMAIN,
        f"{ADDRESS}_power",
        config_entry=mock_config_entry,
        suggested_object_id="desk",
        original_device_class=SwitchDeviceClass.OUTLET,  # as GoveeBleLocalPowerSwitch registers
    )
    old = registry.async_update_entity(
        old.entity_id,
        name="Desk",
        icon="mdi:desk",
        area_id=area.id,
        labels={label.label_id},
        aliases=["Desk light"],
        hidden_by=er.RegistryEntryHider.USER,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    exposed_entities.async_expose_entity(hass, "conversation", old.entity_id, True)
    with patch("custom_components.govee_ble_local.create_device", return_value=device):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    switch = registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_power")
    light = registry.async_get_entity_id("light", DOMAIN, f"{ADDRESS}_light")
    if case == "relay_plug":
        assert light is None
        assert switch == old.entity_id
        assert registry.async_get(switch).id == old.id
        return
    assert switch is None
    if case == "light_registered":
        kept = registry.async_get("light.kept")
        assert (kept.unique_id, kept.name, kept.disabled_by) == (
            f"{ADDRESS}_light",
            None,
            er.RegistryEntryDisabler.USER,
        )
        return
    moved = registry.async_get("light.desk")
    assert moved is not None
    assert moved.unique_id == f"{ADDRESS}_light"
    assert (moved.name, moved.icon, moved.area_id, moved.labels, moved.aliases) == (
        "Desk",
        "mdi:desk",
        area.id,
        {label.label_id},
        old.aliases,
    )
    assert (moved.hidden_by, moved.disabled_by) == (er.RegistryEntryHider.USER, None)
    assert exposed_entities.async_should_expose(hass, "conversation", "light.desk")
    assert hass.states.get("light.desk").attributes["friendly_name"] == "Desk"


@pytest.mark.parametrize("device_sku", ["H6047"])
async def test_real_zone_layout_creates_zone_lights_only(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """With the real H6047 profile both bars are lights and no zone switch exists."""
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, setup_integration.entry_id)
    unique_ids = {e.unique_id for e in entries}
    assert {f"{ADDRESS}_zone_left_light", f"{ADDRESS}_zone_right_light"} <= unique_ids
    zone_ids = {uid for uid in unique_ids if uid.startswith(f"{ADDRESS}_zone_")}
    assert all(uid.endswith("_light") for uid in zone_ids)


async def test_make_device_helper_matches_library(mock_device: Device) -> None:
    """Guard the fixture itself: a real profile, update() returns the live state."""
    assert [z.name for z in mock_device.zones] == ["main", "background"]
    assert await mock_device.update() is mock_device.state
    plug = make_device("H5083")
    assert plug.profile.requires_secret


async def test_reported_ble_mac_added_as_bluetooth_connection(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """A device-reported BLE MAC becomes an extra (upper-case) Bluetooth connection."""
    mock_device.state.ble_mac = "aa:bb:cc:dd:ee:99"
    setup_integration.runtime_data.coordinator.async_publish_device_state()
    entry = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ADDRESS), setup_integration.entry_id
    )
    assert (dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:99") in entry.connections
    assert (dr.CONNECTION_BLUETOOTH, ADDRESS) in entry.connections


async def test_unload_cancels_pending_first_poll(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """The staggered first poll is an entry task: unloading while it waits cancels it."""
    with patch("custom_components.govee_ble_local.FIRST_POLL_STAGGER_SECONDS", 10_000):
        # Any address hashes to a stagger in [0, 10000); this one is non-zero.
        assert zlib.crc32(ADDRESS.encode()) % 10_000
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=False)
        first_poll = _first_poll_task()
        assert not first_poll.done()
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert first_poll.cancelled()
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]
