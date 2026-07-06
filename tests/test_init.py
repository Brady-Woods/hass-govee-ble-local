"""Tests for Govee BLE Local setup, unload, and background wiring."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bleak.exc import BleakError
from govee_ble_local import Capability, GoveeBleNotSupported
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.const import DOMAIN

from .conftest import make_device
from .const import ADDRESS


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
    mock_device.update_ble_device.assert_called_once_with(
        service_info.device, service_info.advertisement
    )


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
