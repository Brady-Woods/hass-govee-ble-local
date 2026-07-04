"""Shared fixtures for the Govee BLE Local integration tests."""
from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_h60a6.const import DOMAIN

from .const import ADDRESS, LOCAL_NAME, TITLE, make_status

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Enable loading of the custom integration in every test."""
    yield


@pytest.fixture
def ble_device() -> BLEDevice:
    """A fake BLEDevice standing in for the light."""
    return BLEDevice(address=ADDRESS, name=LOCAL_NAME, details={})


@pytest.fixture
def mock_client() -> Generator[AsyncMock]:
    """Patch GoveeBleClient with an AsyncMock returning canned status."""
    client = AsyncMock()
    client.address = ADDRESS
    client.get_status.return_value = make_status()
    client.get_serial_number.return_value = "SN0001"
    client.update_ble_device = MagicMock()
    with patch("custom_components.govee_h60a6.GoveeBleClient", return_value=client):
        yield client


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A config entry for the H60A6."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=ADDRESS,
        data={"address": ADDRESS, "sku": "H60A6"},
    )


@pytest.fixture
def mock_bluetooth(ble_device: BLEDevice) -> Generator[SimpleNamespace]:
    """Patch the bluetooth lookups __init__ uses during setup."""
    with (
        patch(
            "custom_components.govee_h60a6.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ) as ble_lookup,
        patch(
            "custom_components.govee_h60a6.bluetooth.async_register_callback",
            return_value=MagicMock(),
        ) as register,
    ):
        yield SimpleNamespace(ble_lookup=ble_lookup, register=register)


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    mock_bluetooth: SimpleNamespace,
) -> MockConfigEntry:
    """Set the integration up and return the loaded config entry."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
