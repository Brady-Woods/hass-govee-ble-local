"""Tests for Govee BLE Local setup, unload, and background wiring."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.const import DOMAIN

from .const import ADDRESS, WIFI_MAC, make_status


async def test_setup_and_unload(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """A full setup loads the entry, populates the device, then unloads cleanly."""
    entry = setup_integration
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.serial_number == "SN0001"
    assert entry.runtime_data.profile.sku == "H60A6"

    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, ADDRESS)})
    assert device is not None
    assert (dr.CONNECTION_BLUETOOTH, ADDRESS) in device.connections
    assert (dr.CONNECTION_NETWORK_MAC, WIFI_MAC) in device.connections

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    mock_client.disconnect.assert_awaited()


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


async def test_setup_profile_via_name_match(
    hass: HomeAssistant, mock_client: AsyncMock, mock_bluetooth: object
) -> None:
    """With no stored SKU, the profile is resolved from the advertised name."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="X", unique_id=ADDRESS, data={"address": ADDRESS}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.profile.sku == "H60A6"


async def test_setup_profile_default_fallback(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """An unrecognized advertised name falls back to the default SKU."""
    device = BLEDevice(address=ADDRESS, name="Unknown Device", details={})
    with (
        patch(
            "custom_components.govee_ble_local.bluetooth.async_ble_device_from_address",
            return_value=device,
        ),
        patch(
            "custom_components.govee_ble_local.bluetooth.async_register_callback",
            return_value=MagicMock(),
        ),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, title="X", unique_id=ADDRESS, data={"address": ADDRESS}
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.runtime_data.profile.sku == "H60A6"


async def test_coordinator_update_failed(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """A BleakError during a poll is turned into a failed (not crashed) update."""
    entry = setup_integration
    mock_client.get_status.side_effect = BleakError("boom")
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False


async def test_ble_advertisement_updates_client(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    mock_bluetooth: object,
) -> None:
    """The registered BLE callback refreshes the client's BLEDevice handle."""
    register = mock_bluetooth.register  # type: ignore[attr-defined]
    assert register.call_count == 1
    # async_register_callback(hass, callback, matcher, mode)
    callback = register.call_args.args[1]
    service_info = MagicMock()
    callback(service_info, MagicMock())
    mock_client.update_ble_device.assert_called_once_with(service_info.device)


async def test_device_registry_reconciles_connections(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """A changed Wi-Fi MAC on a later poll replaces the stored connections."""
    entry = setup_integration
    registry = dr.async_get(hass)

    new_mac = "99:99:99:99:99:99"
    mock_client.get_status.return_value = make_status(wifi_mac=new_mac)
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    device = registry.async_get_device(identifiers={(DOMAIN, ADDRESS)})
    assert device is not None
    assert (dr.CONNECTION_NETWORK_MAC, new_mac) in device.connections


async def test_setup_no_profile_raises(
    hass: HomeAssistant, mock_client: AsyncMock, mock_bluetooth: object
) -> None:
    """Setup retries when no device profile can be resolved at all."""
    with (
        patch(
            "custom_components.govee_ble_local.govee_profile.load_by_sku",
            return_value=None,
        ),
        patch(
            "custom_components.govee_ble_local.govee_profile.match_local_name",
            return_value=None,
        ),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, title="X", unique_id=ADDRESS,
            data={"address": ADDRESS, "sku": "H60A6"},
        )
        entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_tolerates_serial_number_failure(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_bluetooth: object,
) -> None:
    """A failed serial-number read doesn't block setup."""
    mock_client.get_serial_number.side_effect = BleakError("no serial")
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.runtime_data.serial_number is None
