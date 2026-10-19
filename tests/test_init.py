"""Tests for setup, unload, passive Bluetooth handling and the device registry."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import DEFAULT, AsyncMock, MagicMock, patch
import zlib

from bleak.backends.device import BLEDevice
from govee_ble_local import (
    Device,
    GoveeBleConnectionError,
    create_device,
    profile_for,
)
from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.light import DATA_COMPONENT as LIGHT_DATA
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local import capture
from custom_components.govee_ble_local.const import (
    CONF_SECRET,
    CONF_SKU,
    DOMAIN,
    LAST_SEEN_RESOLUTION_SECONDS,
    MAX_POLL_INTERVAL_SECONDS,
    POLL_IDLE_DISCONNECT_SECONDS,
)
from custom_components.govee_ble_local.coordinator import GoveeBleLocalCoordinator

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
from .const import ADDRESS, LOCAL_NAME, SECRET_HEX, TITLE, pact_mfg

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

    async def _hung_update() -> Any:
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

    async def _slow_update() -> object:
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
    }
    # A finished poll's link is dropped soon: 0 or less would never drop it.
    assert POLL_IDLE_DISCONNECT_SECONDS > 0


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
    assert (
        entry.reason == "The Govee model H9999 is not supported by this version of the integration"
    )


@pytest.mark.parametrize(
    ("pact", "state"),
    [((1, 1), ConfigEntryState.LOADED), ((2, 3), ConfigEntryState.SETUP_RETRY)],
    ids=["accepted_pact", "refused_pact"],
)
async def test_setup_with_the_real_library_routes_by_the_advertised_pact(
    hass: HomeAssistant,
    presence: SimpleNamespace,
    mock_bluetooth: SimpleNamespace,
    pact: tuple[int, int],
    state: ConfigEntryState,
) -> None:
    """The real create_device builds the H6009 at its advertised pact, or setup retries.

    Only the BLE transport is faked, so the keyword arguments setup passes and the
    library's pact routing both run for real. The H6009 is served only at its row's
    pacts: at another one the model is supported but refused, which can change with a
    later advert, so setup retries instead of failing for good.
    """
    info = make_service_info(
        name="GVH6009ABCD", manufacturer_data=pact_mfg(*pact), advertisement=True
    )
    presence.service_info = mock_bluetooth.last_info.return_value = info
    entry = MockConfigEntry(
        domain=DOMAIN, title=TITLE, unique_id=ADDRESS, data={"address": ADDRESS, CONF_SKU: "H6009"}
    )
    entry.add_to_hass(hass)
    with patch(
        "govee_ble_local.devices.device.GoveeConnection",
        side_effect=lambda *_a, **_k: FakeConnection(),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.state is state
        if state is ConfigEntryState.LOADED:
            assert entry.runtime_data.device.profile == profile_for("H6009", pact=pact)
            assert isinstance(entry.runtime_data.device._conn, FakeConnection)
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

    async def _update() -> Any:
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
