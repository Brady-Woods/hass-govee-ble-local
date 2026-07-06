"""Tests for the Govee BLE Local config flow."""
from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.config_flow import GoveeBleLocalConfigFlow
from custom_components.govee_ble_local.const import DOMAIN

from .const import ADDRESS, LOCAL_NAME

_CF = "custom_components.govee_ble_local.config_flow"

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
    with patch("custom_components.govee_ble_local.async_setup_entry", return_value=True):
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


async def _plug_to_secret_menu(hass: HomeAssistant) -> str:
    """Drive an H5083 discovery to the secret menu (auto-read patched to fail);
    return the flow_id."""
    with patch(
        f"{_CF}.GoveeBleLocalConfigFlow._read_secret_from_device", return_value=None
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=_service_info(name="ihoment_H5083_A2D1"),
        )
        assert result["step_id"] == "bluetooth_confirm"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "secret"
    return result["flow_id"]


async def test_bluetooth_discovery_plug_secret_manual(hass: HomeAssistant) -> None:
    """The manual menu branch validates + stores the hex secret."""
    flow_id = await _plug_to_secret_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, user_input={"next_step_id": "secret_manual"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "secret_manual"

    result = await hass.config_entries.flow.async_configure(
        flow_id, user_input={"secret": "nothex!!"}
    )
    assert result["errors"] == {"secret": "invalid_secret"}

    result = await hass.config_entries.flow.async_configure(
        flow_id, user_input={"secret": "a1:b2:c3:d4:e5:f6:07:18"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "address": ADDRESS,
        "sku": "H5083",
        "secret": "a1b2c3d4e5f60718",
    }


async def test_secret_cloud_fetch_success(hass: HomeAssistant) -> None:
    """The cloud branch logs in, fetches the secret, and stores it."""
    flow_id = await _plug_to_secret_menu(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, user_input={"next_step_id": "secret_cloud"}
    )
    assert result["step_id"] == "secret_cloud"
    with patch(
        f"{_CF}.GoveeBleLocalConfigFlow._fetch_cloud_secret",
        return_value=("a1b2c3d4e5f60718", None),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, user_input={"email": "u@example.com", "password": "pw"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["secret"] == "a1b2c3d4e5f60718"


async def test_secret_cloud_fetch_error(hass: HomeAssistant) -> None:
    """A cloud login failure shows the error and stays on the form."""
    flow_id = await _plug_to_secret_menu(hass)
    await hass.config_entries.flow.async_configure(
        flow_id, user_input={"next_step_id": "secret_cloud"}
    )
    with patch(
        f"{_CF}.GoveeBleLocalConfigFlow._fetch_cloud_secret",
        return_value=(None, "cannot_connect"),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, user_input={"email": "u@example.com", "password": "bad"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_bluetooth_discovery_plug_secret_auto_read(hass: HomeAssistant) -> None:
    """When the device is unbound, read_secret() succeeds and the entry is
    created automatically with no manual secret step."""
    with patch(
        "custom_components.govee_ble_local.config_flow."
        "GoveeBleLocalConfigFlow._read_secret_from_device",
        return_value="a1b2c3d4e5f60718",
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=_service_info(name="ihoment_H5083_A2D1"),
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "address": ADDRESS,
        "sku": "H5083",
        "secret": "a1b2c3d4e5f60718",
    }


async def test_bluetooth_discovery_plug_blank_secret(hass: HomeAssistant) -> None:
    """A blank manual secret adds the device without one (settable later)."""
    flow_id = await _plug_to_secret_menu(hass)
    await hass.config_entries.flow.async_configure(
        flow_id, user_input={"next_step_id": "secret_manual"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, user_input={"secret": ""}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"address": ADDRESS, "sku": "H5083"}


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
    mfg = {MANUFACTURER_ID: b"\x01"}
    infos = [
        SimpleNamespace(address=ADDRESS, name=LOCAL_NAME, manufacturer_data=mfg),
        SimpleNamespace(address="00:00:00:00:00:99", name="GVH9999ZZZZ", manufacturer_data=mfg),
    ]
    with patch(
        "custom_components.govee_ble_local.config_flow.async_discovered_service_info",
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
        "custom_components.govee_ble_local.config_flow.async_discovered_service_info",
        return_value=[
            SimpleNamespace(
                address="00:00:00:00:00:99",
                name="GVH9999ZZZZ",
                manufacturer_data={MANUFACTURER_ID: b"\x01"},
            )
        ],
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
        "custom_components.govee_ble_local.config_flow.async_ble_device_from_address",
        return_value=BLEDevice(address=ADDRESS, name=LOCAL_NAME, details={}),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


async def test_read_secret_from_device_success(hass: HomeAssistant) -> None:
    """_read_secret_from_device returns the hex secret when read_secret works."""
    flow = GoveeBleLocalConfigFlow()
    flow.hass = hass
    device = AsyncMock()
    device.read_secret.return_value = bytes.fromhex("a1b2c3d4e5f60718")
    with (
        patch(f"{_CF}.async_ble_device_from_address",
              return_value=BLEDevice(address=ADDRESS, name="p", details={})),
        patch(f"{_CF}.async_last_service_info", return_value=None),
        patch(f"{_CF}.create_device", return_value=device),
    ):
        result = await flow._read_secret_from_device(ADDRESS, "H5083")
    assert result == "a1b2c3d4e5f60718"
    device.stop.assert_awaited()


async def test_read_secret_from_device_no_ble_device(hass: HomeAssistant) -> None:
    """No reachable BLE device -> None (falls back to manual entry)."""
    flow = GoveeBleLocalConfigFlow()
    flow.hass = hass
    with patch(f"{_CF}.async_ble_device_from_address", return_value=None):
        assert await flow._read_secret_from_device(ADDRESS, "H5083") is None


async def test_read_secret_from_device_bound_returns_none(hass: HomeAssistant) -> None:
    """A bound device declines read_secret (returns None / errors) -> None."""
    flow = GoveeBleLocalConfigFlow()
    flow.hass = hass
    device = AsyncMock()
    device.read_secret.side_effect = BleakError("bound / no response")
    with (
        patch(f"{_CF}.async_ble_device_from_address",
              return_value=BLEDevice(address=ADDRESS, name="p", details={})),
        patch(f"{_CF}.async_last_service_info", return_value=None),
        patch(f"{_CF}.create_device", return_value=device),
    ):
        assert await flow._read_secret_from_device(ADDRESS, "H5083") is None
    device.stop.assert_awaited()


def _cloud_flow(hass: HomeAssistant) -> GoveeBleLocalConfigFlow:
    flow = GoveeBleLocalConfigFlow()
    flow.hass = hass
    flow._pending = {"address": ADDRESS, "sku": "H5083", "title": "Plug"}
    return flow


async def test_fetch_cloud_secret_matches_device(hass: HomeAssistant) -> None:
    """_fetch_cloud_secret returns the hex secret of the account device whose
    BLE MAC matches the config entry's address."""
    from govee_ble_local.cloud import CloudDevice

    dev = CloudDevice(
        sku="H5083", device=f"AB:CD:{ADDRESS}", name="Plug",
        secret=bytes.fromhex("a1b2c3d4e5f60718"),
        pact_type=None, pact_code=None, goods_type=None,
    )
    account = AsyncMock()
    account.get_devices.return_value = [dev]
    with patch("govee_ble_local.cloud.GoveeCloudAccount", return_value=account):
        secret, error = await _cloud_flow(hass)._fetch_cloud_secret("u@e.com", "pw")
    assert secret == "a1b2c3d4e5f60718"
    assert error is None
    account.close.assert_awaited()


async def test_fetch_cloud_secret_not_found(hass: HomeAssistant) -> None:
    """No matching device on the account -> secret_not_found."""
    account = AsyncMock()
    account.get_devices.return_value = []
    with patch("govee_ble_local.cloud.GoveeCloudAccount", return_value=account):
        secret, error = await _cloud_flow(hass)._fetch_cloud_secret("u@e.com", "pw")
    assert secret is None
    assert error == "secret_not_found"


async def test_fetch_cloud_secret_login_error(hass: HomeAssistant) -> None:
    """A GoveeCloudError (bad creds / network) -> cannot_connect."""
    from govee_ble_local.cloud import GoveeCloudError

    account = AsyncMock()
    account.get_devices.side_effect = GoveeCloudError("bad login")
    with patch("govee_ble_local.cloud.GoveeCloudAccount", return_value=account):
        secret, error = await _cloud_flow(hass)._fetch_cloud_secret("u@e.com", "pw")
    assert secret is None
    assert error == "cannot_connect"
    account.close.assert_awaited()


async def test_reconfigure_plug_updates_secret(hass: HomeAssistant) -> None:
    """Reconfiguring a secret-gated device offers a secret field and stores it."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="Plug", unique_id=ADDRESS,
        data={"address": ADDRESS, "sku": "H5083"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    with patch(
        "custom_components.govee_ble_local.config_flow.async_ble_device_from_address",
        return_value=BLEDevice(address=ADDRESS, name="ihoment_H5083_A2D1", details={}),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"secret": "a1b2c3d4e5f60718"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["secret"] == "a1b2c3d4e5f60718"


async def test_reconfigure_not_found(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Reconfigure aborts if the device isn't currently reachable."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    with patch(
        "custom_components.govee_ble_local.config_flow.async_ble_device_from_address",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_found"
