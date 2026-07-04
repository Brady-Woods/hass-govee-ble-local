"""Tests for the Govee BLE Local light platform."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from bleak.exc import BleakError
from govee_ble_local import profile as govee_profile
from govee_ble_local.profile import Capabilities, DeviceProfile
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ColorMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.const import DOMAIN
from custom_components.govee_ble_local.coordinator import GoveeBleLocalCoordinator
from custom_components.govee_ble_local.light import GoveeBleLocalLight

from .const import ADDRESS, TITLE, make_status

H60A6 = govee_profile.load_by_sku("H60A6")

_CREATED_COORDINATORS: list[GoveeBleLocalCoordinator] = []


@pytest.fixture(autouse=True)
async def _shutdown_created_coordinators() -> None:
    """Shut down coordinators built by _make_light so no debouncer lingers."""
    yield
    while _CREATED_COORDINATORS:
        await _CREATED_COORDINATORS.pop().async_shutdown()


def _make_light(
    hass: HomeAssistant,
    client: AsyncMock,
    status,
    profile: DeviceProfile = H60A6,
) -> GoveeBleLocalLight:
    """Construct a light entity backed by a coordinator with fixed data."""
    coordinator = GoveeBleLocalCoordinator(hass, client, ADDRESS)
    _CREATED_COORDINATORS.append(coordinator)
    coordinator.data = status
    coordinator.last_update_success = True
    return GoveeBleLocalLight(coordinator, client, ADDRESS, TITLE, profile, "SN0001")


async def test_light_state_via_setup(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The set-up light reflects polled state and optimistic color defaults."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("light", DOMAIN, f"{ADDRESS}_light")
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "on"  # upper zone on
    assert state.attributes[ATTR_BRIGHTNESS] == 102  # 40% of 255
    assert state.attributes[ATTR_COLOR_TEMP_KELVIN] == 4000
    assert ColorMode.COLOR_TEMP in state.attributes["supported_color_modes"]
    assert ColorMode.RGB in state.attributes["supported_color_modes"]
    assert state.attributes["effect_list"]  # non-empty scene list


def test_light_is_on_variants(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    """is_on is True if any zone is on, False if all off, None if unknown."""
    light = _make_light(hass, mock_client, make_status(zone_upper_on=True, zone_lower_on=False))
    assert light.is_on is True

    light = _make_light(hass, mock_client, make_status(zone_upper_on=False, zone_lower_on=False))
    assert light.is_on is False

    light = _make_light(hass, mock_client, make_status(zone_upper_on=None, zone_lower_on=False))
    assert light.is_on is None


def test_light_brightness_and_effect_properties(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """Brightness and effect are derived from polled status."""
    light = _make_light(hass, mock_client, make_status(brightness_pct=None))
    assert light.brightness is None

    scene = H60A6.selectable_scenes()[0]
    light = _make_light(hass, mock_client, make_status(scene_id=scene.scene_id))
    assert light.effect == scene.name

    light = _make_light(hass, mock_client, make_status(scene_id=None))
    assert light.effect is None

    # optimistic color defaults
    assert light.color_mode is ColorMode.COLOR_TEMP
    assert light.rgb_color is None
    assert light.color_temp_kelvin == 4000


def test_light_minimal_profile_brightness_only(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A brightness-only profile yields a BRIGHTNESS light with no zones."""
    profile = DeviceProfile(
        sku="MIN", name="Min", local_name_prefixes=("MIN",),
        capabilities=Capabilities(brightness=True),
    )
    light = _make_light(hass, mock_client, make_status(brightness_pct=40), profile=profile)
    assert light.supported_color_modes == {ColorMode.BRIGHTNESS}
    assert light.color_mode is ColorMode.BRIGHTNESS
    assert light.effect_list is None
    assert light.is_on is True  # brightness > 0, no zones


def test_light_minimal_profile_onoff(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A capability-less profile yields an ONOFF light."""
    profile = DeviceProfile(
        sku="OO", name="OnOff", local_name_prefixes=("OO",),
        capabilities=Capabilities(brightness=False),
    )
    light = _make_light(hass, mock_client, make_status(brightness_pct=None), profile=profile)
    assert light.supported_color_modes == {ColorMode.ONOFF}
    assert light.is_on is None  # brightness unknown


async def test_light_turn_on_all_attributes(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """turn_on applies brightness/rgb/color-temp and powers zones from off."""
    light = _make_light(
        hass, mock_client,
        make_status(zone_upper_on=False, zone_lower_on=False, brightness_pct=0),
    )
    await light.async_turn_on(
        **{ATTR_BRIGHTNESS: 255, ATTR_RGB_COLOR: (10, 20, 30), ATTR_COLOR_TEMP_KELVIN: 3000}
    )
    mock_client.set_brightness_pct.assert_awaited_once_with(100)
    mock_client.set_rgb_color.assert_awaited_once_with(10, 20, 30)
    mock_client.set_color_temp_kelvin.assert_awaited_once_with(3000)
    assert mock_client.set_zone.await_count == 2  # both zones powered on
    assert light.color_mode is ColorMode.COLOR_TEMP
    assert light.rgb_color == (10, 20, 30)
    assert light.color_temp_kelvin == 3000


async def test_light_turn_on_effect_full_upload(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A scene with an upload blob is activated via set_scene_full."""
    scene = next(s for s in H60A6.scenes if s.working and s.param)
    light = _make_light(hass, mock_client, make_status())
    await light.async_turn_on(effect=scene.name)
    mock_client.set_scene_full.assert_awaited_once_with(scene.code, scene.param)
    mock_client.set_scene.assert_not_awaited()


async def test_light_turn_on_effect_bare(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A scene without an upload blob is activated by bare id."""
    scene = next(s for s in H60A6.scenes if s.working and not s.param)
    light = _make_light(hass, mock_client, make_status())
    await light.async_turn_on(effect=scene.name)
    mock_client.set_scene.assert_awaited_once_with(scene.scene_id)
    mock_client.set_scene_full.assert_not_awaited()


async def test_light_turn_on_effect_broken_raises(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """Activating a known-broken scene raises a clean error."""
    scene = next(s for s in H60A6.scenes if not s.working)
    light = _make_light(hass, mock_client, make_status())
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on(effect=scene.name)


async def test_light_turn_on_effect_unknown_is_ignored(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """An unknown effect name is logged and ignored, not sent to the device."""
    light = _make_light(hass, mock_client, make_status())
    await light.async_turn_on(effect="Definitely Not A Scene")
    mock_client.set_scene.assert_not_awaited()
    mock_client.set_scene_full.assert_not_awaited()


async def test_light_turn_off(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    """turn_off powers down every zone."""
    light = _make_light(hass, mock_client, make_status())
    await light.async_turn_off()
    assert mock_client.set_zone.await_count == 2
    for call in mock_client.set_zone.await_args_list:
        assert call.args[1] is False


async def test_light_command_error_becomes_ha_error(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A BLE failure during a command surfaces as a HomeAssistantError."""
    mock_client.set_brightness_pct.side_effect = BleakError("nope")
    light = _make_light(hass, mock_client, make_status())
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on(**{ATTR_BRIGHTNESS: 255})


def test_light_rgb_only_profile(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    """An RGB-only profile yields an RGB light (no color-temp)."""
    profile = DeviceProfile(
        sku="RGB", name="Rgb", local_name_prefixes=("RGB",),
        capabilities=Capabilities(rgb=True),
    )
    light = _make_light(hass, mock_client, make_status(), profile=profile)
    assert light.supported_color_modes == {ColorMode.RGB}
    assert light.color_mode is ColorMode.RGB
