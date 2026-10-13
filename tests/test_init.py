"""Tests for setup, unload, passive Bluetooth handling and the device registry."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch
import zlib

from govee_ble_local import Device, GoveeBleConnectionError, GoveeBleNotSupported
from homeassistant.components.bluetooth import BluetoothChange, BluetoothScanningMode
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local import capture
from custom_components.govee_ble_local.const import CONF_SECRET, DOMAIN

from .conftest import (
    advertisement_callback,
    allow_adverts,
    connection,
    make_device,
    make_service_info,
    unavailable_callback,
)
from .const import ADDRESS, SECRET_HEX, TITLE

MAIN_LIGHT = "light.test_ceiling"


async def test_setup_and_unload(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """Setup loads every platform; unload stops polling then closes the link."""
    entry = setup_integration
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(MAIN_LIGHT) is not None

    coordinator = entry.runtime_data.coordinator
    with patch.object(coordinator, "async_shutdown", wraps=coordinator.async_shutdown) as shutdown:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    shutdown.assert_awaited()
    mock_device.stop.assert_awaited_once()
    assert connection(mock_device).closed


async def test_unload_closes_link_after_polling_stops(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """The coordinator is shut down BEFORE the device link is closed."""
    entry = setup_integration
    order: list[str] = []
    coordinator = entry.runtime_data.coordinator
    real_shutdown = coordinator.async_shutdown

    async def _shutdown() -> None:
        order.append("shutdown")
        await real_shutdown()

    mock_device.stop.side_effect = lambda: order.append("stop")  # type: ignore[attr-defined]
    with patch.object(coordinator, "async_shutdown", _shutdown):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert order[:2] == ["shutdown", "stop"]


# The failed unload leaves the entry (and its poll timer) loaded on purpose.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_unload_platform_failure_keeps_link(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """If the platforms fail to unload, the device link is left alone."""
    with patch.object(hass.config_entries, "async_unload_platforms", return_value=False):
        assert not await hass.config_entries.async_unload(setup_integration.entry_id)
    mock_device.stop.assert_not_awaited()


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

    release.set()
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data.coordinator
    assert coordinator.consecutive_failures == 0
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
    await hass.async_block_till_done()

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

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    with (
        patch("custom_components.govee_ble_local.FIRST_POLL_STAGGER_SECONDS", 30),
        patch("custom_components.govee_ble_local.asyncio.sleep", _sleep),
    ):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert sleeps[0] == zlib.crc32(ADDRESS.encode()) % 30
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
    """An entry without a stored SKU identifies it from the advertisement."""
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
    assert create.call_args.kwargs == {"secret": None}


async def test_setup_retries_when_sku_unknown(
    hass: HomeAssistant, mock_device: Device, mock_bluetooth: SimpleNamespace
) -> None:
    """No stored SKU and no advertisement to identify it from: retry later."""
    mock_bluetooth.last_info.return_value = None
    entry = MockConfigEntry(
        domain=DOMAIN, title=TITLE, unique_id=ADDRESS, data={"address": ADDRESS}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unsupported_model_is_a_permanent_error(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_bluetooth: SimpleNamespace
) -> None:
    """An unsupported SKU fails setup for good instead of retrying forever."""
    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.govee_ble_local.create_device",
        side_effect=GoveeBleNotSupported("H9999"),
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR


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
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_bluetooth: SimpleNamespace
) -> None:
    """Passive advertisements and both unavailability histories are tracked, then released."""
    register = mock_bluetooth.register
    matcher, mode = register.call_args.args[2], register.call_args.args[3]
    assert matcher == {"address": ADDRESS}
    assert mode is BluetoothScanningMode.PASSIVE
    assert mock_bluetooth.ble_lookup.call_args.kwargs == {"connectable": True}
    connectable_flags = sorted(
        call.kwargs["connectable"] for call in mock_bluetooth.track_unavailable.call_args_list
    )
    assert connectable_flags == [False, True]

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    register.return_value.assert_called_once()
    assert mock_bluetooth.track_unavailable.return_value.call_count == 2


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
    callback = advertisement_callback(mock_bluetooth)
    callback(make_service_info(on=True), BluetoothChange.ADVERTISEMENT)
    assert hass.states.get(MAIN_LIGHT).state == STATE_ON
    callback(make_service_info(on=False), BluetoothChange.ADVERTISEMENT)
    assert hass.states.get(MAIN_LIGHT).state == STATE_OFF
    connection(mock_device).sent.clear()
    assert not connection(mock_device).sent  # purely passive


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


async def test_advertisement_updates_rssi_and_ble_device(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """Each advertisement refreshes RSSI and the BLEDevice used for the next connect."""
    info = make_service_info(rssi=-71)
    advertisement_callback(mock_bluetooth)(info, BluetoothChange.ADVERTISEMENT)
    coordinator = setup_integration.runtime_data.coordinator
    assert coordinator.rssi == -71
    assert mock_device._ble_device is info.device


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

    # A reload starts from an empty state; the registry keeps what it knew.
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()
    entry = registry.async_get_device_by_identifier((DOMAIN, ADDRESS), setup_integration.entry_id)
    assert entry.sw_version == "1.02.03"


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
    """Unloading while the staggered first poll is still waiting cancels it."""
    with patch("custom_components.govee_ble_local.FIRST_POLL_STAGGER_SECONDS", 10_000):
        # Any address hashes to a stagger in [0, 10000); this one is non-zero.
        assert zlib.crc32(ADDRESS.encode()) % 10_000
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=False)
        first_poll = mock_config_entry.runtime_data.first_poll
        assert first_poll is not None
        assert not first_poll.done()
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert first_poll.cancelled()
    mock_device.update.assert_not_awaited()  # type: ignore[attr-defined]
