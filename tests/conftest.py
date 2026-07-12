"""Shared fixtures for the Govee BLE Local integration tests."""
from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.backends.device import BLEDevice
from govee_ble_local import Capability, DeviceState, Zone
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.const import DOMAIN

from .const import ADDRESS, H60A6_CAPS, H60A6_ZONES, LOCAL_NAME, SCENE_NAMES, TITLE

pytest_plugins = ["pytest_homeassistant_custom_component"]


def make_device(
    *,
    capabilities: frozenset[Capability] = H60A6_CAPS,
    zones: tuple[Zone, ...] = H60A6_ZONES,
    scene_names: list[str] | None = None,
    sku: str = "H60A6",
    min_kelvin: int = 2700,
    max_kelvin: int = 6500,
    segments: int = 13,
) -> AsyncMock:
    """Build a mock v3 Device with the given capability surface."""
    device = AsyncMock()
    device.address = ADDRESS
    device.sku = sku
    device.capabilities = capabilities
    device.zones = zones
    # Segment count comes off the device's DeviceProfile (light.py reads
    # device.profile.segments to build one entity per segment).
    device.profile.segments = segments
    # Explicit, sane default - an AsyncMock's auto-created attributes are
    # truthy, so leaving this unset would make every mock device look
    # "gradual-capable" (only H61A8 really is) to capture.py's self-test.
    device.profile.gradual = False
    device.scene_names = SCENE_NAMES if scene_names is None else scene_names
    device.active_scene = None
    device.min_kelvin = min_kelvin
    device.max_kelvin = max_kelvin
    # Device-info + on/off live on the DeviceState in the v3 library; the plugin
    # reads them through device.state (not standalone Device properties).
    device.update.return_value = DeviceState(optimistic=True)
    device.update_ble_device = MagicMock()
    device.ingest_advertisement = MagicMock(return_value=False)
    device.state = DeviceState(optimistic=True)
    # Zone power: mock zone_is_on off a dict that set_zone_power updates, so the
    # switch entities behave like the real device.
    zones_on: dict[str, bool] = {}
    device.zone_is_on = lambda name: zones_on.get(name)

    async def _set_zone_power(name: str, on: bool) -> None:
        zones_on[name] = on

    device.set_zone_power = AsyncMock(side_effect=_set_zone_power)
    return device


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Enable loading of the custom integration in every test."""
    yield


@pytest.fixture(autouse=True)
def mock_plenty_of_ble_slots() -> Generator[MagicMock]:
    """By default, report an abundance of BLE connection slots so tests that
    don't care about slot-awareness (the vast majority) aren't affected by it -
    the isolated test environment has no real scanners registered, which would
    otherwise read as "0 slots available" and make every poll skip itself as
    exhausted. Tests specifically exercising slot-exhaustion / dynamic-interval
    behavior override this patch locally with their own values."""
    with patch(
        "custom_components.govee_ble_local.scheduling.bluetooth.async_scanner_count",
        return_value=10,
    ) as scanner_count:
        yield scanner_count


@pytest.fixture
def ble_device() -> BLEDevice:
    """A fake BLEDevice standing in for the light."""
    return BLEDevice(address=ADDRESS, name=LOCAL_NAME, details={})


@pytest.fixture
def mock_device() -> Generator[AsyncMock]:
    """Patch create_device with a mock Device (H60A6 capability surface)."""
    device = make_device()
    with patch(
        "custom_components.govee_ble_local.create_device", return_value=device
    ):
        yield device


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
    service_info = MagicMock()
    service_info.device = ble_device
    service_info.name = LOCAL_NAME
    service_info.manufacturer_data = {34883: bytes([0xEC, 0, 0, 0, 0])}
    service_info.advertisement = MagicMock()
    service_info.rssi = -60
    with (
        patch(
            "custom_components.govee_ble_local.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ) as ble_lookup,
        patch(
            "custom_components.govee_ble_local.bluetooth.async_last_service_info",
            return_value=service_info,
        ) as last_info,
        patch(
            "custom_components.govee_ble_local.bluetooth.async_register_callback",
            return_value=MagicMock(),
        ) as register,
        patch(
            "custom_components.govee_ble_local.bluetooth.async_track_unavailable",
            return_value=MagicMock(),
        ) as track_unavailable,
    ):
        yield SimpleNamespace(
            ble_lookup=ble_lookup, register=register, last_info=last_info,
            service_info=service_info, track_unavailable=track_unavailable,
        )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: AsyncMock,
    mock_bluetooth: SimpleNamespace,
) -> MockConfigEntry:
    """Set the integration up and return the loaded config entry."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
