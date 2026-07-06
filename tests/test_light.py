"""Tests for the Govee BLE Local light platform."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from bleak.exc import BleakError
from govee_ble_local import Capability, DeviceState
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local import GoveeBleLocalRuntimeData
from custom_components.govee_ble_local.const import DOMAIN
from custom_components.govee_ble_local.coordinator import GoveeBleLocalCoordinator
from custom_components.govee_ble_local.light import GoveeBleLocalLight, async_setup_entry

from .conftest import make_device
from .const import ADDRESS, TITLE

_CREATED: list[GoveeBleLocalCoordinator] = []


@pytest.fixture(autouse=True)
async def _shutdown_created() -> None:
    yield
    while _CREATED:
        await _CREATED.pop().async_shutdown()


def _make_light(
    hass: HomeAssistant, device: AsyncMock, data: DeviceState | None = None
) -> GoveeBleLocalLight:
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    coordinator.last_update_success = True
    coordinator.data = data if data is not None else DeviceState()
    _CREATED.append(coordinator)
    light = GoveeBleLocalLight(coordinator, device, ADDRESS, TITLE)
    light.hass = hass
    light.entity_id = "light.test"
    return light


def test_light_reflects_polled_state(hass: HomeAssistant) -> None:
    """State properties read from the coordinator's DeviceState (polled truth)."""
    device = make_device()
    light = _make_light(
        hass, device, DeviceState(is_on=True, brightness=50, rgb_color=(10, 20, 30))
    )
    assert light.is_on is True
    assert light.brightness == round(50 / 100 * 255)   # 128
    assert light.rgb_color == (10, 20, 30)
    assert light.color_mode is ColorMode.RGB


async def test_light_state_via_setup(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The set-up light exposes the expected color modes + effect list."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("light", DOMAIN, f"{ADDRESS}_light")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert ColorMode.COLOR_TEMP in state.attributes["supported_color_modes"]
    assert ColorMode.RGB in state.attributes["supported_color_modes"]
    assert state.attributes["effect_list"]  # non-empty scene list
    assert state.attributes["min_color_temp_kelvin"] == 2700
    assert state.attributes["max_color_temp_kelvin"] == 6500


async def test_light_turn_on_all_attributes(hass: HomeAssistant) -> None:
    """turn_on applies brightness/rgb/color-temp and powers zones from off."""
    device = make_device()
    light = _make_light(hass, device)
    await light.async_turn_on(
        **{ATTR_BRIGHTNESS: 255, ATTR_RGB_COLOR: (10, 20, 30), ATTR_COLOR_TEMP_KELVIN: 3000}
    )
    device.set_brightness.assert_awaited_once_with(100)
    device.set_rgb.assert_awaited_once_with((10, 20, 30))
    device.set_color_temp.assert_awaited_once_with(3000)
    assert device.set_zone_power.await_count == 2  # both zones powered on
    assert light.is_on is True  # zone power nudged onto the DeviceState


async def test_light_turn_on_effect(hass: HomeAssistant) -> None:
    """An effect is activated by name via set_scene_by_name."""
    device = make_device()
    light = _make_light(hass, device)
    await light.async_turn_on(**{ATTR_EFFECT: "Aurora"})
    device.set_scene_by_name.assert_awaited_once_with("Aurora")
    assert light.effect == "Aurora"


async def test_light_turn_off_powers_zones(hass: HomeAssistant) -> None:
    """turn_off powers down every zone."""
    device = make_device()
    light = _make_light(hass, device)
    await light.async_turn_off()
    assert device.set_zone_power.await_count == 2
    for call in device.set_zone_power.await_args_list:
        assert call.args[1] is False
    assert light.is_on is False


async def test_light_no_zones_uses_global_power(hass: HomeAssistant) -> None:
    """A light with no zones toggles global power instead."""
    device = make_device(
        capabilities=frozenset({Capability.BRIGHTNESS, Capability.RGB}), zones=()
    )
    light = _make_light(hass, device)
    await light.async_turn_on()
    device.set_power.assert_awaited_once_with(True)
    await light.async_turn_off()
    device.set_power.assert_awaited_with(False)


async def test_light_command_error_becomes_ha_error(hass: HomeAssistant) -> None:
    """A BLE failure during a command surfaces as a HomeAssistantError."""
    device = make_device()
    device.set_brightness.side_effect = BleakError("nope")
    light = _make_light(hass, device)
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on(**{ATTR_BRIGHTNESS: 255})


async def test_light_command_timeout_becomes_ha_error(hass: HomeAssistant) -> None:
    """A stalled-handshake TimeoutError also surfaces as a clean HA error."""
    device = make_device()
    device.set_brightness.side_effect = TimeoutError("handshake stalled")
    light = _make_light(hass, device)
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on(**{ATTR_BRIGHTNESS: 255})


def test_light_brightness_only_mode(hass: HomeAssistant) -> None:
    """A brightness-only device yields a BRIGHTNESS light."""
    device = make_device(capabilities=frozenset({Capability.BRIGHTNESS}), zones=())
    light = _make_light(hass, device)
    assert light.supported_color_modes == {ColorMode.BRIGHTNESS}
    assert light.color_mode is ColorMode.BRIGHTNESS


def test_light_rgb_only_mode(hass: HomeAssistant) -> None:
    """An RGB-only device yields an RGB light (no color-temp)."""
    device = make_device(capabilities=frozenset({Capability.RGB}), zones=())
    light = _make_light(hass, device)
    assert light.supported_color_modes == {ColorMode.RGB}
    assert light.color_mode is ColorMode.RGB


async def test_no_light_entity_for_plug(hass: HomeAssistant) -> None:
    """A POWER-only device (plug) gets no light entity."""
    device = make_device(
        capabilities=frozenset({Capability.POWER}), zones=(), scene_names=[]
    )
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    _CREATED.append(coordinator)
    entry = MockConfigEntry(domain=DOMAIN, title=TITLE, data={"address": ADDRESS})
    entry.runtime_data = GoveeBleLocalRuntimeData(device=device, coordinator=coordinator)
    added: list[object] = []
    await async_setup_entry(hass, entry, added.extend)
    assert added == []
