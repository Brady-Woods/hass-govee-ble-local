"""Tests for the Govee BLE Local config flow."""
from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_h60a6.const import DOMAIN

from .const import ADDRESS, LOCAL_NAME

MANUFACTURER_ID = 34883

# Starting any flow for this integration pulls in its ``bluetooth_adapters``
# dependency, so the bluetooth component must be set up (with mocked adapters).
# That component creates a discovery debouncer HA only cancels on
# HOMEASSISTANT_STOP, so it lingers past each test. It's HA-owned (not ours),
# so we allow the lingering timer for this module only; the other test modules
# still enforce strict timer cleanup on the integration's own code.
pytestmark = [
    pytest.mark.usefixtures("enable_bluetooth"),
    pytest.mark.parametrize("expected_lingering_timers", [True]),
]


@pytest.fixture(autouse=True)
def _mock_setup_entry() -> Generator[None]:
    """Skip real entry setup so creating an entry makes no BLE connection."""
    with patch("custom_components.govee_h60a6.async_setup_entry", return_value=True):
        yield


def _service_info(
    name: str = LOCAL_NAME, address: str = ADDRESS
) -> BluetoothServiceInfoBleak:
    device = BLEDevice(address=address, name=name, details={})
    adv = AdvertisementData(
        local_name=name,
        manufacturer_data={MANUFACTURER_ID: b"\x01"},
        service_data={},
        service_uuids=[],
        tx_power=-127,
        rssi=-60,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={MANUFACTURER_ID: b"\x01"},
        service_data={},
        service_uuids=[],
        source="local",
        device=device,
        advertisement=adv,
        connectable=True,
        time=0.0,
        tx_power=-127,
    )


async def test_bluetooth_discovery_supported(hass: HomeAssistant) -> None:
    """A supported Govee device discovered over BLE can be set up."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_service_info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == LOCAL_NAME
    assert result["data"] == {"address": ADDRESS, "sku": "H60A6"}


async def test_bluetooth_discovery_not_supported(hass: HomeAssistant) -> None:
    """A Govee device with no matching profile is rejected."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=_service_info(name="GVH9999XXXX"),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


async def test_bluetooth_discovery_already_configured(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Re-discovering an already-configured device aborts."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_service_info()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """The user flow lists supported discovered devices and creates an entry."""
    infos = [
        SimpleNamespace(address=ADDRESS, name=LOCAL_NAME),
        SimpleNamespace(address="00:00:00:00:00:99", name="GVH9999ZZZZ"),
    ]
    with patch(
        "custom_components.govee_h60a6.config_flow.async_discovered_service_info",
        return_value=infos,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"address": ADDRESS}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == LOCAL_NAME
    assert result["data"] == {"address": ADDRESS, "sku": "H60A6"}


async def test_user_flow_no_devices(hass: HomeAssistant) -> None:
    """The user flow aborts when nothing supported is around."""
    with patch(
        "custom_components.govee_h60a6.config_flow.async_discovered_service_info",
        return_value=[SimpleNamespace(address="00:00:00:00:00:99", name="GVH9999ZZZZ")],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_reconfigure_success(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Reconfigure confirms the device is reachable and reloads."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with patch(
        "custom_components.govee_h60a6.config_flow.async_ble_device_from_address",
        return_value=BLEDevice(address=ADDRESS, name=LOCAL_NAME, details={}),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


async def test_reconfigure_not_found(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Reconfigure aborts if the device isn't currently reachable."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    with patch(
        "custom_components.govee_h60a6.config_flow.async_ble_device_from_address",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_found"
