"""Tests for Govee BLE Local setup, unload, and background wiring."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bleak.exc import BleakError
from govee_ble_local import Capability, DeviceState, GoveeBleNotSupported
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.const import DOMAIN

from .conftest import make_device
from .const import ADDRESS, TITLE


async def test_setup_and_unload(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: AsyncMock
) -> None:
    """A full setup loads the entry and registers the device, then unloads."""
    entry = setup_integration
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.device is mock_device

    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, ADDRESS)})
    assert device is not None
    assert (dr.CONNECTION_BLUETOOTH, ADDRESS) in device.connections
    assert device.model == "H60A6"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    mock_device.stop.assert_awaited()


async def test_setup_ble_device_not_found(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Setup retries when the device isn't visible over Bluetooth."""
    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.govee_ble_local.bluetooth.async_ble_device_from_address",
        return_value=None,
    ):
        assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_sku_from_advertisement(
    hass: HomeAssistant, mock_device: AsyncMock, mock_bluetooth: SimpleNamespace
) -> None:
    """With no stored SKU, the SKU is identified from the advertisement."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="X", unique_id=ADDRESS, data={"address": ADDRESS}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_setup_unsupported_sku_retries(
    hass: HomeAssistant, mock_bluetooth: SimpleNamespace
) -> None:
    """An unsupported SKU (create_device raises) becomes a setup retry."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="X", unique_id=ADDRESS,
        data={"address": ADDRESS, "sku": "H9999"},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.govee_ble_local.create_device",
        side_effect=GoveeBleNotSupported("nope"),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_coordinator_update_failed(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: AsyncMock
) -> None:
    """A BleakError during a poll is turned into a failed (not crashed) update."""
    entry = setup_integration
    mock_device.update.side_effect = BleakError("boom")
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False


async def test_ble_advertisement_updates_device(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: AsyncMock,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """The registered BLE callback refreshes the device's BLEDevice handle."""
    register = mock_bluetooth.register
    assert register.call_count == 1
    callback = register.call_args.args[1]  # (hass, callback, matcher, mode)
    service_info = MagicMock()
    callback(service_info, MagicMock())
    mock_device.update_ble_device.assert_called_once_with(service_info.device)
    mock_device.ingest_advertisement.assert_called_once_with(service_info)


async def test_passive_advert_pushes_state(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: AsyncMock,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """A passive advertisement that changes on/off is pushed to the coordinator
    (no connection) so entities update between polls."""
    coordinator = setup_integration.runtime_data.coordinator
    callback = mock_bluetooth.register.call_args.args[1]
    mock_device.ingest_advertisement.return_value = True
    with patch.object(coordinator, "async_set_updated_data") as push:
        callback(MagicMock(), MagicMock())
    push.assert_called_once()


async def test_ble_mac_added_to_device_registry(
    hass: HomeAssistant, mock_bluetooth: SimpleNamespace
) -> None:
    """A device that reads back a distinct BLE MAC gets it as a second
    Bluetooth connection on the registry entry."""
    device = make_device()
    device.state = DeviceState(optimistic=False, ble_mac="A4:C1:38:AA:BB:CC")
    device.update.return_value = device.state
    entry = MockConfigEntry(
        domain=DOMAIN, title=TITLE, unique_id=ADDRESS,
        data={"address": ADDRESS, "sku": "H60A6"},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.govee_ble_local.create_device", return_value=device):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = dr.async_get(hass)
    dev = registry.async_get_device(identifiers={(DOMAIN, ADDRESS)})
    assert dev is not None
    assert (dr.CONNECTION_BLUETOOTH, dr.format_mac("A4:C1:38:AA:BB:CC")) in dev.connections
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_capture_session_service_returns_report(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: AsyncMock
) -> None:
    """The capture_session service runs the self-test for the targeted device and
    returns the captured report as response data."""
    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_device(identifiers={(DOMAIN, ADDRESS)})
    assert device_entry is not None

    response = await hass.services.async_call(
        DOMAIN,
        "capture_session",
        {"device_id": device_entry.id},
        blocking=True,
        return_response=True,
    )

    assert response is not None
    assert response["results"], response
    assert response["results"][0]["sku"] == "H60A6"
    # Stored for the diagnostics dump too.
    assert setup_integration.runtime_data.coordinator.last_self_test is not None


async def test_setup_removes_legacy_and_orphaned_zone_switches(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Setup drops both pre-v0.11 integer-indexed zone switches and switches for
    zones that are now colour lights, while keeping on/off-only zone switches."""
    registry = er.async_get(hass)
    # A pre-v0.11 integer-indexed switch, and a switch for `main` - which has
    # segments in the test fixture and is now a light, so its switch is orphaned.
    registry.async_get_or_create(
        "switch", DOMAIN, f"{ADDRESS}_zone_1",
        config_entry=setup_integration, suggested_object_id="legacy_ring",
    )
    registry.async_get_or_create(
        "switch", DOMAIN, f"{ADDRESS}_zone_main",
        config_entry=setup_integration, suggested_object_id="legacy_main",
    )
    assert registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_zone_1") is not None
    assert registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_zone_main") is not None

    await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_zone_1") is None
    assert registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_zone_main") is None
    # The on/off-only zone (background, no segments) keeps its switch.
    assert (
        registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_zone_background")
        is not None
    )


async def test_setup_passes_secret_to_library(
    hass: HomeAssistant, mock_bluetooth: SimpleNamespace
) -> None:
    """A stored secret is decoded from hex and handed to create_device."""
    device = make_device(
        capabilities=frozenset({Capability.POWER}), zones=(), scene_names=[], sku="H5083"
    )
    entry = MockConfigEntry(
        domain=DOMAIN, title="Plug", unique_id=ADDRESS,
        data={"address": ADDRESS, "sku": "H5083", "secret": "a1b2c3d4e5f60718"},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.govee_ble_local.create_device", return_value=device
    ) as create:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert create.call_args.kwargs["secret"] == bytes.fromhex("a1b2c3d4e5f60718")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_plug_gets_switch_not_light(
    hass: HomeAssistant, mock_bluetooth: SimpleNamespace
) -> None:
    """A plug (POWER only, no zones) sets up with a switch entity, not a light."""
    device = make_device(
        capabilities=frozenset({Capability.POWER}), zones=(), scene_names=[], sku="H5083"
    )
    entry = MockConfigEntry(
        domain=DOMAIN, title="Test Plug", unique_id=ADDRESS,
        data={"address": ADDRESS, "sku": "H5083"},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.govee_ble_local.create_device", return_value=device):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_power") is not None
    assert registry.async_get_entity_id("light", DOMAIN, f"{ADDRESS}_light") is None

    assert await hass.config_entries.async_unload(entry.entry_id)
