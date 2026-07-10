"""Tests for the Govee BLE Local light platform."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from bleak.exc import BleakError
from govee_ble_local import Capability, DeviceState, Segment
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    EFFECT_OFF,
    ColorMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local import GoveeBleLocalRuntimeData
from custom_components.govee_ble_local.capture import LogCapture
from custom_components.govee_ble_local.const import DOMAIN
from custom_components.govee_ble_local.coordinator import GoveeBleLocalCoordinator
from custom_components.govee_ble_local.light import (
    GoveeBleLocalLight,
    GoveeBleLocalSegmentLight,
    GoveeBleLocalZoneLight,
    async_setup_entry,
)

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


def test_device_info_surfaces_wifi_hw_serial(hass: HomeAssistant) -> None:
    """device_info exposes wifi MAC (connection), hardware version, and serial
    from the polled DeviceState."""
    from homeassistant.helpers.device_registry import (
        CONNECTION_NETWORK_MAC,
        DeviceInfo,
    )

    device = make_device()
    light = _make_light(
        hass,
        device,
        DeviceState(
            wifi_mac="11:22:33:44:55:66",
            hardware_version="1.02.30",
            firmware_version="1.04.03",
            serial_number="SN12345",
        ),
    )
    info: DeviceInfo = light.device_info
    assert info["hw_version"] == "1.02.30"
    assert info["sw_version"] == "1.04.03"
    assert info["serial_number"] == "SN12345"
    assert (CONNECTION_NETWORK_MAC, "11:22:33:44:55:66") in info["connections"]


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
    assert light.effect == "Aurora"  # optimistic (device reports no active scene)

    # once the device reports an active scene back, that wins
    device.active_scene = "Forest"
    assert light.effect == "Forest"


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
    entry.runtime_data = GoveeBleLocalRuntimeData(
        device=device, coordinator=coordinator, log_capture=LogCapture(ADDRESS)
    )
    added: list[object] = []
    await async_setup_entry(hass, entry, added.extend)
    assert added == []


def _make_segment(
    hass: HomeAssistant, device: AsyncMock, index: int, data: DeviceState | None = None
) -> GoveeBleLocalSegmentLight:
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    coordinator.last_update_success = True
    coordinator.data = data if data is not None else DeviceState()
    _CREATED.append(coordinator)
    seg = GoveeBleLocalSegmentLight(coordinator, device, ADDRESS, TITLE, index)
    seg.hass = hass
    seg.entity_id = f"light.segment_{index}"
    return seg


async def test_segment_entities_created(hass: HomeAssistant) -> None:
    """A SEGMENTS-capable device gets one light per segment, plus the main light."""
    device = make_device(segments=3)
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    coordinator.data = DeviceState()
    _CREATED.append(coordinator)
    entry = MockConfigEntry(domain=DOMAIN, title=TITLE, data={"address": ADDRESS})
    entry.runtime_data = GoveeBleLocalRuntimeData(
        device=device, coordinator=coordinator, log_capture=LogCapture(ADDRESS)
    )
    added: list[object] = []
    await async_setup_entry(hass, entry, added.extend)

    segments = [e for e in added if isinstance(e, GoveeBleLocalSegmentLight)]
    assert len(segments) == 3
    assert {e.unique_id for e in segments} == {f"{ADDRESS}_segment_{i}" for i in range(3)}
    assert any(isinstance(e, GoveeBleLocalLight) for e in added)  # main light too


async def test_segment_turn_on_off_via_service(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: AsyncMock
) -> None:
    """A segment maps plain-on/RGB/brightness to set_segment_* and off to black."""
    registry = er.async_get(hass)
    eid = registry.async_get_entity_id("light", DOMAIN, f"{ADDRESS}_segment_1")
    assert eid is not None
    # Segments are disabled by default; enable this one and reload so it's live.
    registry.async_update_entity(eid, disabled_by=None)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()

    # Plain on with no prior colour -> white so the segment lights.
    await hass.services.async_call("light", "turn_on", {"entity_id": eid}, blocking=True)
    mock_device.set_segment_rgb.assert_awaited_with([1], (255, 255, 255))
    assert hass.states.get(eid).state == "on"

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": eid, "rgb_color": [10, 20, 30]}, blocking=True
    )
    mock_device.set_segment_rgb.assert_awaited_with([1], (10, 20, 30))

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": eid, "brightness": 128}, blocking=True
    )
    mock_device.set_segment_brightness.assert_awaited_with([1], 50)

    await hass.services.async_call("light", "turn_off", {"entity_id": eid}, blocking=True)
    mock_device.set_segment_rgb.assert_awaited_with([1], (0, 0, 0))
    assert hass.states.get(eid).state == "off"


async def test_segment_reads_back_state(hass: HomeAssistant) -> None:
    """Segment colour/brightness come from the device's read-back state.segments."""
    data = DeviceState(
        segments=[
            Segment(index=0, rgb=(9, 9, 9), brightness=None),  # on via colour only
            Segment(index=1, rgb=(1, 2, 3), brightness=40),
            Segment(index=2, rgb=(0, 0, 0), brightness=0),
        ]
    )
    device = make_device(segments=3)

    rgb_only = _make_segment(hass, device, 0, data)
    assert rgb_only.is_on is True
    assert rgb_only.brightness is None

    lit = _make_segment(hass, device, 1, data)
    assert lit.rgb_color == (1, 2, 3)
    assert lit.brightness == round(40 / 100 * 255)
    assert lit.is_on is True

    dark = _make_segment(hass, device, 2, data)
    assert dark.is_on is False


async def test_effect_is_effect_off_when_idle(hass: HomeAssistant) -> None:
    """A scene-capable light reports EFFECT_OFF (not None) when idle, and lists it."""
    device = make_device()  # H60A6 caps include SCENES
    device.active_scene = None
    light = _make_light(hass, device, DeviceState())
    assert EFFECT_OFF in (light.effect_list or [])
    assert light.effect == EFFECT_OFF


async def test_segment_lights_disabled_by_default(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Segment lights are registered but disabled by default (opt-in)."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("light", DOMAIN, f"{ADDRESS}_segment_0")
    assert entity_id is not None
    entry = registry.async_get(entity_id)
    assert entry is not None
    assert entry.disabled_by is not None


async def test_zone_light_controls(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: AsyncMock
) -> None:
    """A colour zone (fixture `main`, which has segments) is a light: on/off via
    set_zone_power, colour via set_zone_rgb, brightness via the segment mask."""
    registry = er.async_get(hass)
    eid = registry.async_get_entity_id("light", DOMAIN, f"{ADDRESS}_zone_main_light")
    assert eid is not None

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": eid, "rgb_color": [5, 6, 7]}, blocking=True
    )
    mock_device.set_zone_rgb.assert_awaited_with("main", (5, 6, 7))
    mock_device.set_zone_power.assert_awaited_with("main", True)
    assert hass.states.get(eid).state == "on"

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": eid, "brightness": 128}, blocking=True
    )
    mock_device.set_segment_brightness.assert_awaited_with(list(range(13)), 50)

    await hass.services.async_call("light", "turn_off", {"entity_id": eid}, blocking=True)
    mock_device.set_zone_power.assert_awaited_with("main", False)
    assert hass.states.get(eid).state == "off"


async def test_zone_light_is_on_tracks_zone_power(hass: HomeAssistant) -> None:
    """The zone light's on/off reads the device's per-zone power state."""
    device = make_device()
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    coordinator.data = DeviceState()
    _CREATED.append(coordinator)
    zone = GoveeBleLocalZoneLight(coordinator, device, ADDRESS, TITLE, "main")
    assert zone.is_on is None  # unknown until a zone-power command / poll
    await device.set_zone_power("main", True)
    assert zone.is_on is True
    await device.set_zone_power("main", False)
    assert zone.is_on is False
