"""Tests for the light platform: main light, zone lights and segment lights."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import PropertyMock, patch

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from govee_ble_local import (
    Capability,
    Device,
    GoveeBleAuthError,
    GoveeBleConnectionError,
    GoveeBleError,
    GoveeBleHandshakeError,
    GoveeBleNotSupported,
    GoveeBleTimeout,
    Segment,
    create_device,
    profile_for,
    supported_skus,
)
from govee_ble_local.wire import build
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.light import DATA_COMPONENT
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.const import (
    CONF_ALLOW_MODELLED,
    CONF_SKU,
    DEFAULT_MAX_KELVIN,
    DEFAULT_MIN_KELVIN,
    DOMAIN,
    UNAVAILABLE_AFTER_FAILURES,
    ZONE_STATE_FRESH_SECONDS,
)

from .conftest import (
    FakeConnection,
    advertisement_callback,
    connection,
    make_device,
    make_service_info,
    rejected_write,
    runtime_segment_count,
)
from .const import ADDRESS, TITLE, pact_mfg

MAIN = "light.test_ceiling"
AURORA = 19074  # H60A6 scene code of "Aurora"
BLACK = (0, 0, 0)
RED = (255, 0, 0)
GREEN = (0, 255, 0)
WHITE = (255, 255, 255)

# Colour-temperature ranges to run the range-dependent tests over: one unlike
# DEFAULT_MIN/MAX_KELVIN (so the device's own range is told apart from the
# fallback), and a profile that gives none (so the fallback is what applies).
KELVIN_RANGES = pytest.mark.parametrize(
    ("kelvin_range", "lo", "hi"),
    [((2200, 7000), 2200, 7000), ((None, None), DEFAULT_MIN_KELVIN, DEFAULT_MAX_KELVIN)],
    ids=["device_range", "fallback_range"],
)


# -- helpers --------------------------------------------------------------------------


@pytest.fixture
def kelvin_range() -> tuple[int | None, int | None] | None:
    """The (min, max) kelvin to give the device's profile; None keeps the library's."""
    return None


@pytest.fixture
def drop_capabilities() -> frozenset[Capability]:
    """Capabilities to remove from the device's profile (e.g. to model an RGB-only fixture)."""
    return frozenset()


@pytest.fixture
def mock_device(
    mock_device: Device,
    kelvin_range: tuple[int | None, int | None] | None,
    drop_capabilities: frozenset[Capability],
) -> Device:
    """The shared test device, its profile's kelvin range or capabilities replaced on request."""
    if kelvin_range is not None:
        lo, hi = kelvin_range
        mock_device.profile = dataclasses.replace(mock_device.profile, min_kelvin=lo, max_kelvin=hi)
    if drop_capabilities:
        mock_device.profile = dataclasses.replace(
            mock_device.profile, capabilities=mock_device.profile.capabilities - drop_capabilities
        )
    return mock_device


@pytest.fixture
async def enabled_entry(
    entity_registry_enabled_by_default: None, entry: MockConfigEntry
) -> MockConfigEntry:
    """Like ``entry`` but with the disabled-by-default segment lights enabled."""
    return entry


def _eid(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    """Resolve an entity id from its unique id."""
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, f"no {domain} entity with unique id {unique_id}"
    return entity_id


def _segment(hass: HomeAssistant, index: int) -> str:
    return _eid(hass, "light", f"{ADDRESS}_segment_{index}")


def _zone(hass: HomeAssistant, name: str) -> str:
    return _eid(hass, "light", f"{ADDRESS}_zone_{name}_light")


def _seed(entry: MockConfigEntry, **fields: object) -> None:
    """Mutate the live device state, then publish it like a poll/advert would."""
    state = entry.runtime_data.device.state
    for key, value in fields.items():
        setattr(state, key, value)
    entry.runtime_data.coordinator.async_publish_device_state()


async def _call(hass: HomeAssistant, service: str, entity_id: str, **data: object) -> None:
    await hass.services.async_call(
        "light", service, {"entity_id": entity_id, **data}, blocking=True
    )


def _fail(device: Device, error: BaseException, *, after: int | None = None) -> None:
    """Make the fake transport raise ``error`` (after letting ``after`` frames through)."""
    conn = connection(device)
    conn.error = error
    conn.fail_after = after


def _govee_flows(hass: HomeAssistant) -> list[dict[str, Any]]:
    """In-progress config flows of this integration (ignores the Bluetooth stack's own)."""
    return [f for f in hass.config_entries.flow.async_progress() if f["handler"] == DOMAIN]


def _lights(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, er.RegistryEntry]:
    """The entry's registered light entities keyed by unique id."""
    return {
        e.unique_id: e
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.domain == "light"
    }


def _kelvin_ranges(hass: HomeAssistant, *entity_ids: str) -> dict[str, tuple[int, int]]:
    """The (min, max) kelvin each light publishes, in its state and in the entity registry.

    Keyed "<entity id> state" / "<entity id> registry", so a failed comparison names the
    light, and the place, still showing a stale range.
    """
    registry = er.async_get(hass)
    ranges: dict[str, tuple[int, int]] = {}
    for entity_id in entity_ids:
        attrs = hass.states.get(entity_id).attributes
        caps = registry.async_get(entity_id).capabilities or {}
        ranges[f"{entity_id} state"] = (
            attrs["min_color_temp_kelvin"],
            attrs["max_color_temp_kelvin"],
        )
        ranges[f"{entity_id} registry"] = (
            caps["min_color_temp_kelvin"],
            caps["max_color_temp_kelvin"],
        )
    return ranges


async def _setup_custom(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    device: Device,
    mock_bluetooth: SimpleNamespace,
) -> MockConfigEntry:
    """Set an entry up around a hand-built (profile-tweaked) real device."""
    entry.add_to_hass(hass)
    with patch("custom_components.govee_ble_local.create_device", return_value=device):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    return entry


# -- 1. entity creation ---------------------------------------------------------------


async def test_h60a6_entities(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """H60A6 gets a main light, two zone lights and 13 segment lights (disabled)."""
    lights = _lights(hass, entry)
    expected = {
        f"{ADDRESS}_light",
        f"{ADDRESS}_zone_main_light",
        f"{ADDRESS}_zone_background_light",
        *(f"{ADDRESS}_segment_{i}" for i in range(13)),
    }
    assert set(lights) == expected
    for unique_id, reg in lights.items():
        if "_segment_" in unique_id:
            assert reg.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        else:
            assert reg.disabled_by is None
    assert sorted(s.entity_id for s in hass.states.async_all("light")) == sorted(
        lights[u].entity_id for u in expected if "_segment_" not in u
    )


async def test_h60a6_zone_and_segment_names(
    hass: HomeAssistant, enabled_entry: MockConfigEntry
) -> None:
    """Zone lights use the translated zone names; segments are numbered from 1."""
    assert hass.states.get(_zone(hass, "main")).attributes["friendly_name"] == (
        "Test Ceiling Main light"
    )
    assert hass.states.get(_zone(hass, "background")).attributes["friendly_name"] == (
        "Test Ceiling Background light"
    )
    assert hass.states.get(_segment(hass, 0)).attributes["friendly_name"] == (
        "Test Ceiling Segment 1"
    )
    assert hass.states.get(_segment(hass, 12)).attributes["friendly_name"] == (
        "Test Ceiling Segment 13"
    )
    assert len(hass.states.async_all("light")) == 16


@pytest.mark.parametrize("device_sku", ["H6006"])
async def test_h6006_has_only_the_main_light(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A zoneless, segmentless bulb gets only the whole-fixture light."""
    assert set(_lights(hass, entry)) == {f"{ADDRESS}_light"}
    assert hass.states.get(MAIN) is not None


@pytest.mark.parametrize("device_sku", ["H5083"])
async def test_plug_has_no_lights(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A smart plug (power only) gets no light entities."""
    assert _lights(hass, entry) == {}
    assert hass.states.async_all("light") == []


@pytest.mark.parametrize("device_sku", ["H6047"])
async def test_h6047_zone_light_names(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """The bar light's zone lights are named Left bar / Right bar."""
    assert hass.states.get(_zone(hass, "left")).attributes["friendly_name"] == (
        "Test Ceiling Left bar"
    )
    assert hass.states.get(_zone(hass, "right")).attributes["friendly_name"] == (
        "Test Ceiling Right bar"
    )


@pytest.mark.parametrize("device_sku", ["H6047"])
async def test_h6047_has_ten_segment_lights(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """H6047 registers its 10 segments (disabled) next to the main and zone lights."""
    lights = _lights(hass, entry)
    assert sum("_segment_" in u for u in lights) == 10
    assert len(lights) == 13


async def test_segment_lights_follow_a_count_known_at_setup(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_bluetooth: SimpleNamespace
) -> None:
    """A runtime segment count already known at platform setup sets the segment lights.

    Today the library only learns a device's own count on a poll, and the first poll
    runs after platform setup, so this covers only a count given before setup.
    """
    device = make_device("H60A6")
    assert device.profile.segments == 13
    with runtime_segment_count(10):
        await _setup_custom(hass, mock_config_entry, device, mock_bluetooth)

    lights = _lights(hass, mock_config_entry)
    assert {u for u in lights if "_segment_" in u} == {f"{ADDRESS}_segment_{i}" for i in range(10)}


async def test_segment_lights_use_the_librarys_set_segment_count(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_bluetooth: SimpleNamespace
) -> None:
    """The library's runtime count, set through its real API (not a patch), sets the lights."""
    device = make_device("H60A6")
    device.set_segment_count(10)
    assert device.segment_count == 10

    await _setup_custom(hass, mock_config_entry, device, mock_bluetooth)

    lights = _lights(hass, mock_config_entry)
    assert {u for u in lights if "_segment_" in u} == {f"{ADDRESS}_segment_{i}" for i in range(10)}


@KELVIN_RANGES
async def test_main_light_supported_features(
    hass: HomeAssistant, entry: MockConfigEntry, lo: int, hi: int
) -> None:
    """The main light advertises RGB + colour temp, the device's kelvin range and effects."""
    _seed(entry, is_on=True)
    attrs = hass.states.get(MAIN).attributes
    assert set(attrs["supported_color_modes"]) == {"rgb", "color_temp"}
    assert (attrs["min_color_temp_kelvin"], attrs["max_color_temp_kelvin"]) == (lo, hi)
    assert attrs["effect_list"][0] == "off"
    assert "Aurora" in attrs["effect_list"]
    # The zone lights advertise the same range.
    zone = hass.states.get(_zone(hass, "main")).attributes
    assert (zone["min_color_temp_kelvin"], zone["max_color_temp_kelvin"]) == (lo, hi)


# -- 2. main light power logic --------------------------------------------------------


async def test_plain_turn_on_sends_power_even_when_already_on(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A plain turn_on always sends the power frame, even if the light is already on."""
    _seed(entry, is_on=True, optimistic=False)
    await _call(hass, "turn_on", MAIN)
    assert mock_device.turn_on.await_args.kwargs["power"] is True
    assert connection(mock_device).sent == [build.switch(True)]
    assert hass.states.get(MAIN).state == STATE_ON


@pytest.mark.parametrize(
    ("is_on", "optimistic", "expect_power"),
    [
        (True, False, False),  # lit zoned fixture: don't re-light zones the user turned off
        (True, True, False),  # ...even when optimistic: zones exist
        (False, False, True),  # off -> must power on
        (None, False, True),  # unknown -> must power on
    ],
)
async def test_zoned_colour_change_power_logic(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    mock_device: Device,
    is_on: bool | None,
    optimistic: bool,
    expect_power: bool,
) -> None:
    """On a zoned fixture a brightness change only sends power when the light isn't on."""
    _seed(entry, is_on=is_on, optimistic=optimistic)
    await _call(hass, "turn_on", MAIN, brightness=128)
    assert mock_device.turn_on.await_args.kwargs["power"] is expect_power
    frames = connection(mock_device).sent
    assert (build.switch(True) in frames) is expect_power
    assert build.brightness(50) in frames
    assert hass.states.get(MAIN).state == STATE_ON


@pytest.mark.parametrize("device_sku", ["H6006"])
@pytest.mark.parametrize(
    ("optimistic", "expect_power"),
    [
        (True, True),  # fresh zoneless bulb: on/off is only a guess -> send power
        (False, False),  # polled: it really is on -> no power frame
    ],
)
async def test_zoneless_power_logic(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_device: Device,
    optimistic: bool,
    expect_power: bool,
) -> None:
    """A zoneless bulb with only-optimistic state gets power with a colour change."""
    _seed(entry, is_on=True, optimistic=optimistic)
    await _call(hass, "turn_on", MAIN, rgb_color=RED)
    assert mock_device.turn_on.await_args.kwargs["power"] is expect_power
    assert (build.switch(True) in connection(mock_device).sent) is expect_power


async def test_turn_off_powers_everything_off(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """Turning the main light off sends power-off and shows the zone lights off too."""
    _seed(enabled_entry, is_on=True, zone_power={0: True, 1: True})
    assert hass.states.get(_zone(hass, "main")).state == STATE_ON
    await _call(hass, "turn_off", MAIN)
    mock_device.turn_off.assert_awaited_once()
    assert connection(mock_device).sent == [build.switch(False)]
    assert hass.states.get(MAIN).state == STATE_OFF
    assert hass.states.get(_zone(hass, "main")).state == STATE_OFF
    assert hass.states.get(_zone(hass, "background")).state == STATE_OFF


# -- 3. brightness / colour conversion ------------------------------------------------


@pytest.mark.parametrize(
    ("ha_brightness", "device_pct", "ha_back"),
    [(1, 1, 3), (128, 50, 128), (255, 100, 255)],
)
async def test_brightness_conversion(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    mock_device: Device,
    ha_brightness: int,
    device_pct: int,
    ha_back: int,
) -> None:
    """HA 1..255 maps onto the device's 1..100 and back (never rounding down to 0)."""
    _seed(entry, is_on=True)
    await _call(hass, "turn_on", MAIN, brightness=ha_brightness)
    assert mock_device.turn_on.await_args.kwargs["brightness"] == device_pct
    assert mock_device.state.brightness == device_pct
    assert hass.states.get(MAIN).attributes["brightness"] == ha_back


async def test_rgb_colour_state(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Setting an RGB colour reports rgb mode with that colour and no colour temp."""
    _seed(entry, is_on=True, color_temp_kelvin=3000, rgb_color=None)
    await _call(hass, "turn_on", MAIN, rgb_color=(10, 20, 30))
    attrs = hass.states.get(MAIN).attributes
    assert attrs["color_mode"] == "rgb"
    assert attrs["rgb_color"] == (10, 20, 30)
    assert attrs["color_temp_kelvin"] is None


async def test_colour_temp_state(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Setting a colour temperature reports color_temp mode and drops the RGB colour."""
    _seed(entry, is_on=True, rgb_color=RED, color_temp_kelvin=None)
    await _call(hass, "turn_on", MAIN, color_temp_kelvin=4000)
    attrs = hass.states.get(MAIN).attributes
    assert attrs["color_mode"] == "color_temp"
    assert attrs["color_temp_kelvin"] == 4000
    assert entry.runtime_data.device.state.rgb_color is None


@KELVIN_RANGES
@pytest.mark.parametrize("requested", [9000, 1000, 3500])
async def test_kelvin_is_clamped_to_device_range(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_device: Device,
    *,
    lo: int,
    hi: int,
    requested: int,
) -> None:
    """Out-of-range kelvin is clamped to the device's range before it is sent."""
    expected = {9000: hi, 1000: lo, 3500: 3500}[requested]
    _seed(entry, is_on=True)
    await _call(hass, "turn_on", MAIN, color_temp_kelvin=requested)
    assert mock_device.turn_on.await_args.kwargs["kelvin"] == expected
    assert mock_device.state.color_temp_kelvin == expected
    assert hass.states.get(MAIN).attributes["color_temp_kelvin"] == expected


# -- 4. effects -----------------------------------------------------------------------


async def test_effect_activates_scene(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A valid effect name activates that scene and is reported as the effect."""
    _seed(entry, is_on=True)
    await _call(hass, "turn_on", MAIN, effect="Aurora")
    mock_device.set_scene_by_name.assert_awaited_once_with("Aurora")
    assert mock_device.state.scene_code == AURORA
    assert connection(mock_device).sent[-1] == build.scene_activate(AURORA)
    # Already-lit zoned fixture: the effect alone sends no power frame of its own.
    mock_device.turn_on.assert_not_awaited()
    assert hass.states.get(MAIN).attributes["effect"] == "Aurora"


async def test_effect_with_brightness(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """Brightness and an effect together: brightness goes out first, then the scene."""
    _seed(entry, is_on=True)
    await _call(hass, "turn_on", MAIN, effect="Aurora", brightness=100)
    assert mock_device.turn_on.await_args.kwargs["brightness"] == 39
    mock_device.set_scene_by_name.assert_awaited_once_with("Aurora")
    frames = connection(mock_device).sent
    assert frames[0] == build.brightness(39)
    assert frames[-1] == build.scene_activate(AURORA)
    attrs = hass.states.get(MAIN).attributes
    assert attrs["effect"] == "Aurora"
    assert attrs["brightness"] == 99


async def test_effect_when_off_also_powers_on(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """An effect on an off light sends power first, then the scene."""
    _seed(entry, is_on=False)
    await _call(hass, "turn_on", MAIN, effect="Aurora")
    assert mock_device.turn_on.await_args.kwargs["power"] is True
    assert connection(mock_device).sent[0] == build.switch(True)
    assert hass.states.get(MAIN).state == STATE_ON


async def test_unknown_effect_is_rejected(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """An unknown effect raises ServiceValidationError and sends nothing."""
    _seed(entry, is_on=False)
    with pytest.raises(ServiceValidationError) as exc:
        await _call(hass, "turn_on", MAIN, effect="No Such Scene", brightness=200)
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "unknown_effect"
    assert exc.value.translation_placeholders["effect"] == "No Such Scene"
    assert connection(mock_device).sent == []
    mock_device.turn_on.assert_not_awaited()
    mock_device.set_scene_by_name.assert_not_awaited()
    assert hass.states.get(MAIN).state == STATE_OFF


async def test_effect_list_follows_the_device_on_each_update(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """Scenes the device stops offering (its scene set, read after setup) leave the effect list."""
    full = list(mock_device.scene_names)
    assert hass.states.get(MAIN).attributes["effect_list"] == ["off", *full]
    dropped = full[-1]
    light = hass.data[DATA_COMPONENT].get_entity(MAIN)
    light._optimistic_effect = dropped  # as if it had just been activated

    offered = full[: len(full) // 2]
    with patch.object(Device, "scene_names", new_callable=PropertyMock, return_value=offered):
        entry.runtime_data.coordinator.async_publish_device_state()
        await hass.async_block_till_done()

        assert hass.states.get(MAIN).attributes["effect_list"] == ["off", *offered]
        capabilities = er.async_get(hass).async_get(MAIN).capabilities
        assert capabilities["effect_list"] == ["off", *offered]
        assert light._optimistic_effect is None  # no longer one the device offers
        with pytest.raises(ServiceValidationError):
            await _call(hass, "turn_on", MAIN, effect=dropped)
    mock_device.set_scene_by_name.assert_not_awaited()


async def test_a_scene_dropped_since_the_last_update_is_refused_before_any_write(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """The library stops offering a scene before the light has heard (a poll read the
    device's versions, its update not yet published): turning the light on with it is
    refused with nothing sent, not after the power-on went out."""
    _seed(entry, is_on=False)
    full = list(mock_device.scene_names)
    dropped = full[-1]
    assert dropped in hass.states.get(MAIN).attributes["effect_list"]
    with (
        patch.object(Device, "scene_names", new_callable=PropertyMock, return_value=full[:-1]),
        pytest.raises(ServiceValidationError) as exc,
    ):
        await _call(hass, "turn_on", MAIN, effect=dropped)
    assert exc.value.translation_key == "unknown_effect"
    assert connection(mock_device).sent == []
    mock_device.turn_on.assert_not_awaited()
    assert hass.states.get(MAIN).state == STATE_OFF
    # ...and the picker no longer offers it.
    assert dropped not in hass.states.get(MAIN).attributes["effect_list"]
    assert dropped not in er.async_get(hass).async_get(MAIN).capabilities["effect_list"]


async def test_effect_off_in_rgb_mode_restores_the_colour(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """EFFECT_OFF in RGB mode re-applies the current colour (no scene command)."""
    _seed(entry, is_on=True, rgb_color=(10, 20, 30), color_temp_kelvin=None)
    await _call(hass, "turn_on", MAIN, effect="off")
    assert mock_device.turn_on.await_args.kwargs["rgb"] == (10, 20, 30)
    assert mock_device.turn_on.await_args.kwargs["kelvin"] is None
    mock_device.set_scene_by_name.assert_not_awaited()
    attrs = hass.states.get(MAIN).attributes
    assert attrs["effect"] == "off"
    assert attrs["rgb_color"] == (10, 20, 30)


async def test_effect_off_without_a_known_colour_uses_white(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """EFFECT_OFF with no known colour falls back to white."""
    _seed(entry, is_on=True, rgb_color=None, color_temp_kelvin=None)
    await _call(hass, "turn_on", MAIN, effect="off")
    assert mock_device.turn_on.await_args.kwargs["rgb"] == WHITE
    assert hass.states.get(MAIN).attributes["rgb_color"] == WHITE


async def test_effect_off_in_cct_mode_keeps_the_kelvin(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """EFFECT_OFF in colour-temperature mode re-applies the current kelvin."""
    _seed(entry, is_on=True, rgb_color=None, color_temp_kelvin=3000)
    await _call(hass, "turn_on", MAIN, effect="off")
    kwargs = mock_device.turn_on.await_args.kwargs
    assert (kwargs["rgb"], kwargs["kelvin"]) == (None, 3000)
    attrs = hass.states.get(MAIN).attributes
    assert attrs["color_mode"] == "color_temp"
    assert attrs["color_temp_kelvin"] == 3000


async def test_effect_off_with_explicit_colour_uses_it(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """EFFECT_OFF together with a colour applies the requested colour instead."""
    _seed(entry, is_on=True, rgb_color=(10, 20, 30))
    await _call(hass, "turn_on", MAIN, effect="off", rgb_color=GREEN)
    assert mock_device.turn_on.await_args.kwargs["rgb"] == GREEN
    mock_device.set_scene_by_name.assert_not_awaited()
    assert hass.states.get(MAIN).attributes["rgb_color"] == GREEN


async def test_leaving_a_scene_with_effect_off(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Start a scene then EFFECT_OFF: the effect goes back to 'off' with a solid colour."""
    _seed(entry, is_on=True)
    await _call(hass, "turn_on", MAIN, effect="Aurora")
    assert hass.states.get(MAIN).attributes["effect"] == "Aurora"
    await _call(hass, "turn_on", MAIN, effect="off")
    attrs = hass.states.get(MAIN).attributes
    assert attrs["effect"] == "off"
    assert attrs["rgb_color"] == WHITE
    assert entry.runtime_data.device.state.scene_code is None


async def test_effect_follows_the_devices_active_scene(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A read-back scene code is reported as the effect even if we never set one."""
    _seed(entry, is_on=True, scene_code=AURORA)
    assert hass.states.get(MAIN).attributes["effect"] == "Aurora"
    _seed(entry, scene_code=1)  # a code with no catalog name
    assert hass.states.get(MAIN).attributes["effect"] == "off"


async def test_solid_colour_readback_clears_optimistic_effect(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The optimistic effect survives a poll with no info, but not one showing a solid colour."""
    _seed(entry, is_on=True)
    await _call(hass, "turn_on", MAIN, effect="Aurora")
    # A poll that no longer reports the scene code but shows no colour either.
    _seed(entry, scene_code=None)
    assert hass.states.get(MAIN).attributes["effect"] == "Aurora"
    # A poll showing a solid colour means the fixture left the scene.
    _seed(entry, rgb_color=(1, 2, 3))
    assert hass.states.get(MAIN).attributes["effect"] == "off"
    # ...and it stays cleared when the colour read-back disappears again.
    _seed(entry, rgb_color=None)
    assert hass.states.get(MAIN).attributes["effect"] == "off"


async def test_solid_kelvin_readback_clears_optimistic_effect(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A poll showing a colour temperature also clears the optimistic effect."""
    _seed(entry, is_on=True)
    await _call(hass, "turn_on", MAIN, effect="Aurora")
    _seed(entry, scene_code=None, color_temp_kelvin=4000)
    assert hass.states.get(MAIN).attributes["effect"] == "off"


async def test_colour_command_clears_the_effect(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Commanding a colour while a scene runs ends the effect."""
    _seed(entry, is_on=True)
    await _call(hass, "turn_on", MAIN, effect="Aurora")
    await _call(hass, "turn_on", MAIN, rgb_color=RED)
    assert hass.states.get(MAIN).attributes["effect"] == "off"


# -- 5. partial failure ---------------------------------------------------------------


async def test_partial_failure_still_publishes_state(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """Power + brightness land, the scene fails: the error is raised AND the state shows on."""
    _seed(entry, is_on=False)
    assert hass.states.get(MAIN).state == STATE_OFF
    _fail(mock_device, GoveeBleConnectionError("link dropped"), after=2)

    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, "turn_on", MAIN, effect="Aurora", brightness=128)

    assert exc.value.translation_key == "ble_command_failed"
    assert connection(mock_device).sent == [build.switch(True), build.brightness(50)]
    state = hass.states.get(MAIN)
    assert state.state == STATE_ON
    assert state.attributes["brightness"] == 128
    # The scene never landed, so it must not be shown as running.
    assert state.attributes["effect"] == "off"


async def test_failure_before_anything_lands_changes_nothing(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """If the very first frame fails the light stays off."""
    _seed(entry, is_on=False)
    _fail(mock_device, GoveeBleConnectionError("no slot"))
    with pytest.raises(HomeAssistantError):
        await _call(hass, "turn_on", MAIN, brightness=128)
    assert hass.states.get(MAIN).state == STATE_OFF


async def test_turn_off_failure_keeps_light_on(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A failed turn_off raises and leaves the published state on."""
    _seed(entry, is_on=True)
    _fail(mock_device, GoveeBleTimeout("no ack"))
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, "turn_off", MAIN)
    assert exc.value.translation_key == "ble_command_failed"
    assert hass.states.get(MAIN).state == STATE_ON


# -- 6. error translation -------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        GoveeBleConnectionError("no connection"),
        GoveeBleTimeout("no ack"),
        GoveeBleHandshakeError("bad handshake"),
        BleakError("bleak says no"),
        TimeoutError(),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_ble_errors_become_ble_command_failed(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device, error: BaseException
) -> None:
    """Transport failures surface as one translated HomeAssistantError, without reauth."""
    _fail(mock_device, error)
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, "turn_on", MAIN)
    assert not isinstance(exc.value, ServiceValidationError)
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "ble_command_failed"
    placeholders = exc.value.translation_placeholders
    assert placeholders["device_name"] == "Test Ceiling"
    assert placeholders["error"]  # never empty, even for a bare TimeoutError
    assert exc.value.__cause__ is error
    await hass.async_block_till_done()
    assert _govee_flows(hass) == []


async def test_bare_timeout_error_is_named(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A message-less TimeoutError is reported by its type name."""
    _fail(mock_device, TimeoutError())
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, "turn_on", MAIN)
    assert exc.value.translation_placeholders["error"] == "TimeoutError"


async def test_not_supported_is_a_validation_error(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """GoveeBleNotSupported is the caller's mistake: ServiceValidationError, no reauth."""
    _fail(mock_device, GoveeBleNotSupported("nope"))
    with pytest.raises(ServiceValidationError) as exc:
        await _call(hass, "turn_on", MAIN)
    assert exc.value.translation_key == "not_supported"
    assert exc.value.translation_placeholders["error"] == "nope"
    assert exc.value.translation_placeholders["device_name"] == "Test Ceiling"
    assert _govee_flows(hass) == []


async def test_auth_error_starts_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A rejected secret raises secret_rejected and starts a reauth flow."""
    _fail(mock_device, GoveeBleAuthError("secret rejected"))
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, "turn_on", MAIN)
    assert exc.value.translation_key == "secret_rejected"
    assert not isinstance(exc.value, ServiceValidationError)
    await hass.async_block_till_done()
    flows = _govee_flows(hass)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"
    assert flows[0]["context"]["entry_id"] == entry.entry_id


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The integration's own WARNING+ lines (the ones its diagnostics capture keeps)."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("custom_components.govee_ble_local") and r.levelno >= logging.WARNING
    ]


async def test_rejected_command_is_logged_for_diagnostics(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_device: Device,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A write the device refuses fails the call, no reauth, and is logged at WARNING.

    The library logs the rejection only at DEBUG, so the integration's WARNING is
    what reaches the device's diagnostics capture.
    """
    error = rejected_write()
    _fail(mock_device, error)
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, "turn_on", MAIN)
    assert not isinstance(exc.value, ServiceValidationError)
    assert exc.value.translation_key == "ble_command_failed"
    assert exc.value.translation_placeholders["error"] == str(error)
    assert exc.value.__cause__ is error
    await hass.async_block_till_done()
    assert _govee_flows(hass) == []

    line = f"{ADDRESS} rejected a command: {error}"
    assert _warnings(caplog) == [line]
    assert entry.runtime_data.log_capture.records() == [f"WARNING {line}"]


async def test_rejected_secret_is_logged_for_diagnostics(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_device: Device,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected secret is logged at WARNING too (it is not a GoveeBleCommandRejected)."""
    _fail(mock_device, GoveeBleAuthError("secret rejected"))
    with pytest.raises(HomeAssistantError):
        await _call(hass, "turn_on", MAIN)
    line = f"{ADDRESS} rejected its secret key: secret rejected"
    assert _warnings(caplog) == [line]
    assert entry.runtime_data.log_capture.records() == [f"WARNING {line}"]


async def test_transport_failure_is_not_a_warning(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_device: Device,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lost link stays at DEBUG: only refusals are worth a WARNING."""
    _fail(mock_device, GoveeBleConnectionError("no connection"))
    with pytest.raises(HomeAssistantError):
        await _call(hass, "turn_on", MAIN)
    assert _warnings(caplog) == []
    assert entry.runtime_data.log_capture.records() == []


async def test_hung_command_hits_the_deadline(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A command that never completes is cut off by the command deadline."""
    never = asyncio.Event()

    async def _hang(**_kwargs: object) -> None:
        await never.wait()

    mock_device.turn_on.side_effect = _hang
    with (
        patch("custom_components.govee_ble_local.entity.COMMAND_TIMEOUT_SECONDS", 0.01),
        pytest.raises(HomeAssistantError) as exc,
    ):
        await _call(hass, "turn_on", MAIN)
    assert exc.value.translation_key == "ble_command_failed"
    assert exc.value.translation_placeholders["error"] == "TimeoutError"


async def test_unavailable_when_device_vanishes(
    hass: HomeAssistant, entry: MockConfigEntry, presence: SimpleNamespace
) -> None:
    """Entities follow reachability: gone from every scanner for a few polls -> unavailable."""
    assert hass.states.get(MAIN).state != STATE_UNAVAILABLE
    presence.connectable = False
    entry.runtime_data.coordinator.consecutive_failures = UNAVAILABLE_AFTER_FAILURES
    entry.runtime_data.coordinator.async_update_listeners()
    assert hass.states.get(MAIN).state == STATE_UNAVAILABLE
    presence.connectable = True
    entry.runtime_data.coordinator.async_update_listeners()
    assert hass.states.get(MAIN).state != STATE_UNAVAILABLE


# -- 7. colour mode is always a supported one -----------------------------------------


@pytest.mark.parametrize("device_sku", ["H61A8"])
@pytest.mark.parametrize("drop_capabilities", [frozenset({Capability.COLOR_TEMP})])
async def test_color_mode_never_reports_unsupported_color_temp(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """An RGB-only fixture never reports color_temp, even if a kelvin is in its state.

    No shipped profile is RGB-only any more (the library gives the H61A8 colour
    temperature), so the H61A8 profile is modelled without it.
    """
    _seed(entry, is_on=True, color_temp_kelvin=4000, rgb_color=None)
    attrs = hass.states.get(MAIN).attributes
    assert attrs["color_mode"] == "rgb"
    assert attrs["supported_color_modes"] == ["rgb"]
    assert "min_color_temp_kelvin" not in attrs or attrs["min_color_temp_kelvin"] is None


async def test_kelvin_range_follows_the_device_on_each_update(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """A range the library changes after setup reaches every light's state and registry entry."""
    zone = next(z.name for z in mock_device.zones if z.segments)
    ranges = _kelvin_ranges(hass, MAIN, _zone(hass, zone), _segment(hass, 0))
    assert ranges == dict.fromkeys(ranges, (mock_device.min_kelvin, mock_device.max_kelvin))

    # As if the library had just read a hardware version that widens the range.
    mock_device.profile = dataclasses.replace(mock_device.profile, min_kelvin=2000, max_kelvin=9000)
    enabled_entry.runtime_data.coordinator.async_publish_device_state()
    await hass.async_block_till_done()
    assert _kelvin_ranges(hass, MAIN, _zone(hass, zone), _segment(hass, 0)) == dict.fromkeys(
        ranges, (2000, 9000)
    )

    # A request in the widened range reaches the device unclamped (_clamp_kelvin reads it live).
    await _call(hass, "turn_on", MAIN, color_temp_kelvin=8500)
    assert mock_device.turn_on.await_args.kwargs["kelvin"] == 8500


@pytest.mark.parametrize("device_sku", ["H61A8"])
async def test_h61a8_range_is_refreshed_after_the_hardware_read(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """An older H61A8 (hardware below 3.08.01) offers its wider range once that is read."""
    ranges = _kelvin_ranges(hass, MAIN, _segment(hass, 0))
    assert ranges == dict.fromkeys(ranges, (2700, 6500))  # valid on every revision until read
    _seed(enabled_entry, hardware_version="3.00.00")
    await hass.async_block_till_done()
    widened = (mock_device.min_kelvin, mock_device.max_kelvin)
    assert widened == (2000, 9000)
    assert _kelvin_ranges(hass, MAIN, _segment(hass, 0)) == dict.fromkeys(ranges, widened)

    # A request in the widened range reaches the device unclamped.
    await _call(hass, "turn_on", MAIN, color_temp_kelvin=2200)
    assert mock_device.turn_on.await_args.kwargs["kelvin"] == 2200


@pytest.fixture
def kelvin_by_pact_case() -> tuple[str, bool, tuple[int, int], tuple[int, int]]:
    """A model whose Kelvin range the advertised pact picks: (sku, opt-in, pact, its range).

    Taken from the library (default models first, then the opt-in ones), never a fixed SKU:
    one whose range at that pact differs from the range it has before any pact is known.
    """
    for allow in (False, True):
        for sku in supported_skus(allow_modelled=allow):
            profile = profile_for(sku, allow_modelled=allow)
            if profile is None or Capability.COLOR_TEMP not in profile.capabilities:
                continue
            for pact, lo, hi in profile.kelvin_by_pact:
                if (lo, hi) != (profile.min_kelvin, profile.max_kelvin):
                    return sku, allow, pact, (lo, hi)
    pytest.fail("the installed library has no model whose Kelvin range depends on the pact")


async def test_kelvin_range_follows_a_pact_learned_from_an_advert(
    hass: HomeAssistant,
    mock_bluetooth: SimpleNamespace,
    kelvin_by_pact_case: tuple[str, bool, tuple[int, int], tuple[int, int]],
) -> None:
    """An advert that first carries the device's pact shows the range it picks, without a poll."""
    sku, allow, pact, picked = kelvin_by_pact_case
    device = make_device(sku, name=f"GV{sku}ABCD", allow_modelled=allow)
    data: dict[str, Any] = {"address": ADDRESS, CONF_SKU: sku}
    if allow:
        data[CONF_ALLOW_MODELLED] = True
    entry = await _setup_custom(
        hass,
        MockConfigEntry(domain=DOMAIN, title=TITLE, unique_id=ADDRESS, data=data),
        device,
        mock_bluetooth,
    )
    assert device.pact is None
    ranges = _kelvin_ranges(hass, MAIN)
    assert ranges == dict.fromkeys(ranges, (device.profile.min_kelvin, device.profile.max_kelvin))
    _seed(entry, is_on=True)  # the advert repeats the on/off already known: only its pact is new
    await hass.async_block_till_done()
    device.update.reset_mock()

    info = make_service_info(
        name=f"GV{sku}ABCD", manufacturer_data=pact_mfg(*pact), advertisement=True
    )
    advertisement_callback(mock_bluetooth)(info, BluetoothChange.ADVERTISEMENT)
    await hass.async_block_till_done()
    assert device.pact == pact
    assert _kelvin_ranges(hass, MAIN) == dict.fromkeys(ranges, picked)
    device.update.assert_not_awaited()  # published from the advert, not by a poll


@pytest.mark.parametrize("reported", [False, True], ids=["unreported", "reported"])
async def test_a_learned_pact_is_not_an_on_off_flip(
    hass: HomeAssistant,
    mock_bluetooth: SimpleNamespace,
    kelvin_by_pact_case: tuple[str, bool, tuple[int, int], tuple[int, int]],
    reported: bool,
) -> None:
    """A pact-only advert publishes the new range but never takes the on/off-flip path.

    ``reported`` is whether the library returns True for it: govee-ble-local may signal a
    learned pact as a change, and the flip path would restart a lit light's poll backoff.
    """
    sku, allow, pact, picked = kelvin_by_pact_case
    device = make_device(sku, name=f"GV{sku}ABCD", allow_modelled=allow)
    data: dict[str, Any] = {"address": ADDRESS, CONF_SKU: sku}
    if allow:
        data[CONF_ALLOW_MODELLED] = True
    entry = await _setup_custom(
        hass,
        MockConfigEntry(domain=DOMAIN, title=TITLE, unique_id=ADDRESS, data=data),
        device,
        mock_bluetooth,
    )
    _seed(entry, is_on=True)
    await hass.async_block_till_done()
    ingest = device.ingest_advertisement

    def _ingest(advertisement: Any) -> bool:
        learned = device.pact
        took = ingest(advertisement)
        return took or (reported and device.pact != learned)

    coordinator = entry.runtime_data.coordinator
    info = make_service_info(
        name=f"GV{sku}ABCD", manufacturer_data=pact_mfg(*pact), advertisement=True
    )
    with (
        patch.object(device, "ingest_advertisement", side_effect=_ingest),
        patch.object(coordinator, "_async_handle_power_flip") as flip,
    ):
        advertisement_callback(mock_bluetooth)(info, BluetoothChange.ADVERTISEMENT)
        await hass.async_block_till_done()
    flip.assert_not_called()
    ranges = _kelvin_ranges(hass, MAIN)
    assert ranges == dict.fromkeys(ranges, picked)


@pytest.mark.parametrize("device_sku", ["H61A8"])
async def test_h61a8_offers_color_temp(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """The H61A8's main and segment lights gain colour temp with the library's range."""
    lo, hi = mock_device.min_kelvin, mock_device.max_kelvin
    assert (lo, hi) == (2700, 6500)
    for entity_id in (MAIN, _segment(hass, 0)):
        attrs = hass.states.get(entity_id).attributes
        assert set(attrs["supported_color_modes"]) == {"rgb", "color_temp"}
        assert (attrs["min_color_temp_kelvin"], attrs["max_color_temp_kelvin"]) == (lo, hi)

    await _call(hass, "turn_on", MAIN, color_temp_kelvin=3000)
    assert mock_device.turn_on.await_args.kwargs["kelvin"] == 3000
    await _call(hass, "turn_on", _segment(hass, 0), color_temp_kelvin=4000, brightness=128)
    mock_device.set_segment_color_temp.assert_awaited_once_with([0], 4000)
    mock_device.set_segment_brightness.assert_awaited_once()
    assert hass.states.get(_segment(hass, 0)).attributes["color_mode"] == "color_temp"


@pytest.mark.parametrize("device_sku", ["H6006"])
@pytest.mark.parametrize(
    ("fields", "mode"),
    [
        ({"rgb_color": (1, 2, 3), "color_temp_kelvin": None}, "rgb"),
        ({"rgb_color": None, "color_temp_kelvin": 3000}, "color_temp"),
        ({"rgb_color": None, "color_temp_kelvin": None}, "color_temp"),  # default mode
    ],
)
async def test_color_mode_follows_state(
    hass: HomeAssistant, entry: MockConfigEntry, fields: dict[str, object], mode: str
) -> None:
    """The reported colour mode follows whichever of rgb / kelvin the device state carries."""
    _seed(entry, is_on=True, **fields)
    assert hass.states.get(MAIN).attributes["color_mode"] == mode


@pytest.mark.parametrize("device_sku", ["H6006"])
async def test_brightness_only_fixture(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_bluetooth: SimpleNamespace
) -> None:
    """A dimmable-only fixture exposes the brightness mode and no effects."""
    device = make_device("H6006")
    device.profile = dataclasses.replace(
        device.profile, capabilities=frozenset({Capability.POWER, Capability.BRIGHTNESS})
    )
    entry = await _setup_custom(hass, mock_config_entry, device, mock_bluetooth)
    _seed(entry, is_on=True)
    attrs = hass.states.get(MAIN).attributes
    assert attrs["supported_color_modes"] == ["brightness"]
    assert attrs["color_mode"] == "brightness"
    assert "effect" not in attrs
    assert "effect_list" not in attrs
    # HA never asks, but the entity itself must not invent an effect either.
    assert hass.data[DATA_COMPONENT].get_entity(MAIN).effect is None

    await _call(hass, "turn_on", MAIN, brightness=51)
    assert hass.states.get(MAIN).attributes["brightness"] == 51


@pytest.mark.parametrize("device_sku", ["H6052"])
async def test_h6052_colour_temp_reads_back_as_rgb(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_device: Device,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An H6052 set to a Kelvin switches to rgb mode once a poll reads the colour back.

    The library reads the lamp's colour as the Govee app does: RGB only (the Kelvin's
    tint) with no colour temperature, so the light flips from color_temp to rgb.
    """
    coordinator = entry.runtime_data.coordinator
    _seed(entry, is_on=True, optimistic=False)
    await _call(hass, "turn_on", MAIN, color_temp_kelvin=2700)
    attrs = hass.states.get(MAIN).attributes
    assert attrs["color_mode"] == "color_temp"
    assert attrs["color_temp_kelvin"] == 2700

    async def _poll(**_kwargs: object) -> Any:
        # What the lamp reports after a 2700 K write: aa 05 0d ff ae 54 ...
        mock_device.state.rgb_color = (255, 174, 84)
        mock_device.state.color_temp_kelvin = None
        mock_device.state.optimistic = False
        return mock_device.state

    mock_device.update.side_effect = _poll  # type: ignore[attr-defined]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(MAIN)
    assert state.state == STATE_ON
    assert state.attributes["color_mode"] == "rgb"
    assert state.attributes["rgb_color"] == (255, 174, 84)
    assert state.attributes["color_temp_kelvin"] is None
    assert set(state.attributes["supported_color_modes"]) == {"color_temp", "rgb"}

    # A brightness change keeps the read-back colour: nothing is re-sent for it.
    await _call(hass, "turn_on", MAIN, brightness=128)
    kwargs = mock_device.turn_on.await_args.kwargs
    assert (kwargs["rgb"], kwargs["kelvin"]) == (None, None)
    assert hass.states.get(MAIN).attributes["color_mode"] == "rgb"

    # Leaving a scene restores the read-back RGB (there is no Kelvin to go back to).
    await _call(hass, "turn_on", MAIN, effect="off")
    assert mock_device.turn_on.await_args.kwargs["rgb"] == (255, 174, 84)

    # Setting a Kelvin again goes back to color_temp.
    await _call(hass, "turn_on", MAIN, color_temp_kelvin=4000)
    attrs = hass.states.get(MAIN).attributes
    assert attrs["color_mode"] == "color_temp"
    assert attrs["color_temp_kelvin"] == 4000
    # Home Assistant found nothing invalid in the reported colour state.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("device_sku", ["H6006"])
async def test_effect_off_on_colour_temp_only_fixture(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_bluetooth: SimpleNamespace
) -> None:
    """Without RGB, EFFECT_OFF falls back to the middle of the kelvin range."""
    device = make_device("H6006")
    device.profile = dataclasses.replace(
        device.profile,
        capabilities=frozenset(
            {Capability.POWER, Capability.BRIGHTNESS, Capability.COLOR_TEMP, Capability.SCENES}
        ),
        min_kelvin=2200,
        max_kelvin=7000,
    )
    entry = await _setup_custom(hass, mock_config_entry, device, mock_bluetooth)
    _seed(entry, is_on=True, optimistic=False)
    assert hass.states.get(MAIN).attributes["supported_color_modes"] == ["color_temp"]
    await _call(hass, "turn_on", MAIN, effect="off")
    assert device.turn_on.await_args.kwargs["kelvin"] == (2200 + 7000) // 2
    assert hass.states.get(MAIN).attributes["color_temp_kelvin"] == 4600


@pytest.mark.parametrize("device_sku", ["H6006"])
async def test_plain_turn_on_without_power_capability(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_bluetooth: SimpleNamespace
) -> None:
    """A light that lacks a power capability never sends power frames."""
    device = make_device("H6006")
    device.profile = dataclasses.replace(
        device.profile, capabilities=frozenset({Capability.BRIGHTNESS, Capability.RGB})
    )
    entry = await _setup_custom(hass, mock_config_entry, device, mock_bluetooth)
    _seed(entry, is_on=False)
    await _call(hass, "turn_on", MAIN, brightness=255)
    assert device.turn_on.await_args.kwargs["power"] is False
    assert connection(device).sent == [build.brightness(100)]
    # A plain turn_on has nothing to send at all.
    connection(device).sent.clear()
    device.turn_on.reset_mock()
    await _call(hass, "turn_on", MAIN)
    device.turn_on.assert_not_awaited()
    assert connection(device).sent == []


# -- 8. segment lights (BUG-001 regressions) ------------------------------------------

SEG = 3
SEG_MASK = 1 << SEG


def _seg_rgb_frame(rgb: tuple[int, int, int], index: int = SEG) -> bytes:
    return build.segment_rgb(1 << index, *rgb, "h60a6")


def _seg_bri_frame(pct: int, index: int = SEG) -> bytes:
    return build.segment_brightness(1 << index, pct, "h60a6")


async def test_segment_off_stays_off_after_publish_and_poll(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """BUG-001: a segment set to a brightness then turned off stays off across publish/poll."""
    seg = _segment(hass, SEG)
    coordinator = enabled_entry.runtime_data.coordinator
    await _call(hass, "turn_on", seg, rgb_color=RED, brightness=128)
    assert hass.states.get(seg).state == STATE_ON

    await _call(hass, "turn_off", seg)
    assert hass.states.get(seg).state == STATE_OFF
    # The device's own bookkeeping now holds the segment as black with its old brightness.
    assert mock_device.state.segments == [Segment(index=SEG, rgb=BLACK, brightness=50)]

    coordinator.async_publish_device_state()
    assert hass.states.get(seg).state == STATE_OFF
    await coordinator.async_refresh()
    assert hass.states.get(seg).state == STATE_OFF
    # A different segment's activity must not wake it up either.
    await _call(hass, "turn_on", _segment(hass, 5), rgb_color=GREEN)
    assert hass.states.get(seg).state == STATE_OFF


async def test_segment_turn_on_restores_last_real_colour(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """BUG-001: after 'off', a plain turn_on re-sends the last real colour, not black."""
    seg = _segment(hass, SEG)
    await _call(hass, "turn_on", seg, rgb_color=RED)
    await _call(hass, "turn_off", seg)
    connection(mock_device).sent.clear()
    mock_device.set_segment_rgb.reset_mock()

    await _call(hass, "turn_on", seg)

    mock_device.set_segment_rgb.assert_awaited_once_with([SEG], RED)
    assert connection(mock_device).sent == [_seg_rgb_frame(RED)]
    state = hass.states.get(seg)
    assert state.state == STATE_ON
    assert state.attributes["rgb_color"] == RED
    assert state.attributes["color_mode"] == "rgb"


async def test_segment_brightness_only_turn_on_relights_blacked_out_segment(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """BUG-001: brightness-only turn_on on a black segment re-sends the colour, then brightness."""
    seg = _segment(hass, SEG)
    await _call(hass, "turn_on", seg, rgb_color=GREEN)
    await _call(hass, "turn_off", seg)
    connection(mock_device).sent.clear()

    await _call(hass, "turn_on", seg, brightness=200)

    assert connection(mock_device).sent == [_seg_rgb_frame(GREEN), _seg_bri_frame(78)]
    state = hass.states.get(seg)
    assert state.state == STATE_ON
    assert state.attributes["rgb_color"] == GREEN
    assert state.attributes["brightness"] == 199  # 78 % of 255


async def test_segment_brightness_only_on_lit_segment_keeps_colour(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """Brightness-only turn_on on a lit segment sends only the brightness frame."""
    seg = _segment(hass, SEG)
    await _call(hass, "turn_on", seg, rgb_color=GREEN)
    connection(mock_device).sent.clear()

    await _call(hass, "turn_on", seg, brightness=51)

    assert connection(mock_device).sent == [_seg_bri_frame(20)]
    assert hass.states.get(seg).attributes["rgb_color"] == GREEN


async def test_never_coloured_segment_turns_on_white(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """A segment that never had a colour comes on white."""
    seg = _segment(hass, SEG)
    assert hass.states.get(seg).state == STATE_UNKNOWN
    await _call(hass, "turn_on", seg)
    mock_device.set_segment_rgb.assert_awaited_once_with([SEG], WHITE)
    state = hass.states.get(seg)
    assert state.state == STATE_ON
    assert state.attributes["rgb_color"] == WHITE


async def test_segment_explicit_black_is_not_remembered(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """Commanding black explicitly doesn't overwrite the remembered colour."""
    seg = _segment(hass, SEG)
    await _call(hass, "turn_on", seg, rgb_color=RED)
    await _call(hass, "turn_on", seg, rgb_color=BLACK)
    mock_device.set_segment_rgb.reset_mock()
    await _call(hass, "turn_off", seg)
    await _call(hass, "turn_on", seg)
    mock_device.set_segment_rgb.assert_awaited_with([SEG], RED)


async def test_segment_colour_temp_stays_on_after_poll(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """A segment colour temperature stays color_temp and on even if read-back rgb is black."""
    seg = _segment(hass, SEG)
    coordinator = enabled_entry.runtime_data.coordinator
    await _call(hass, "turn_on", seg, color_temp_kelvin=4000)
    mock_device.set_segment_color_temp.assert_awaited_once_with([SEG], 4000)
    attrs = hass.states.get(seg).attributes
    assert hass.states.get(seg).state == STATE_ON
    assert attrs["color_mode"] == "color_temp"
    assert attrs["color_temp_kelvin"] == 4000

    # The device reports a CCT segment as black in its colour read-back.
    mock_device.state.segments = [Segment(index=SEG, rgb=BLACK, brightness=80)]
    coordinator.async_publish_device_state()
    await coordinator.async_refresh()

    state = hass.states.get(seg)
    assert state.state == STATE_ON
    assert state.attributes["color_mode"] == "color_temp"
    assert state.attributes["color_temp_kelvin"] == 4000
    assert state.attributes["brightness"] == 204  # brightness is still adopted


@KELVIN_RANGES
async def test_segment_colour_temp_is_clamped(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device, lo: int, hi: int
) -> None:
    """Segment colour temperature is clamped into the device range."""
    seg = _segment(hass, SEG)
    attrs = hass.states.get(seg).attributes
    assert (attrs["min_color_temp_kelvin"], attrs["max_color_temp_kelvin"]) == (lo, hi)
    await _call(hass, "turn_on", seg, color_temp_kelvin=9000)
    mock_device.set_segment_color_temp.assert_awaited_once_with([SEG], hi)
    assert hass.states.get(seg).attributes["color_temp_kelvin"] == hi


async def test_segment_colour_after_colour_temp_returns_to_rgb(
    hass: HomeAssistant, enabled_entry: MockConfigEntry
) -> None:
    """Setting an RGB colour after a colour temperature switches the segment back to rgb."""
    seg = _segment(hass, SEG)
    await _call(hass, "turn_on", seg, color_temp_kelvin=4000)
    await _call(hass, "turn_on", seg, rgb_color=RED)
    attrs = hass.states.get(seg).attributes
    assert attrs["color_mode"] == "rgb"
    assert attrs["rgb_color"] == RED


async def test_segment_readback_black_is_off(
    hass: HomeAssistant, enabled_entry: MockConfigEntry
) -> None:
    """Read-back: rgb (0,0,0) with a non-zero brightness means the segment is off."""
    seg = _segment(hass, SEG)
    _seed(enabled_entry, segments=[Segment(index=SEG, rgb=BLACK, brightness=80)])
    assert hass.states.get(seg).state == STATE_OFF


async def test_segment_readback_colour_is_on_and_zero_brightness_not_adopted(
    hass: HomeAssistant, enabled_entry: MockConfigEntry
) -> None:
    """Read-back: a coloured segment is on; brightness 0 is 'unreported', never a level."""
    seg = _segment(hass, SEG)
    _seed(enabled_entry, segments=[Segment(index=SEG, rgb=RED, brightness=80)])
    state = hass.states.get(seg)
    assert state.state == STATE_ON
    assert state.attributes["rgb_color"] == RED
    assert state.attributes["brightness"] == 204

    _seed(enabled_entry, segments=[Segment(index=SEG, rgb=RED, brightness=0)])
    state = hass.states.get(seg)
    assert state.state == STATE_ON
    assert state.attributes["brightness"] == 204  # 0 not adopted


async def test_segment_readback_colour_without_brightness_stays_on(
    hass: HomeAssistant, enabled_entry: MockConfigEntry
) -> None:
    """Read-back: a coloured segment with brightness 0 from the start is on, level unknown."""
    seg = _segment(hass, SEG)
    _seed(enabled_entry, segments=[Segment(index=SEG, rgb=RED, brightness=0)])
    state = hass.states.get(seg)
    assert state.state == STATE_ON
    assert state.attributes["brightness"] is None


async def test_segment_readback_ignores_other_segments_and_missing_rgb(
    hass: HomeAssistant, enabled_entry: MockConfigEntry
) -> None:
    """Read-back for another segment, or without a colour, leaves this segment untouched."""
    seg = _segment(hass, SEG)
    _seed(enabled_entry, segments=[Segment(index=7, rgb=RED, brightness=50)])
    assert hass.states.get(seg).state == STATE_UNKNOWN
    _seed(enabled_entry, segments=[Segment(index=SEG, rgb=None, brightness=50)])
    state = hass.states.get(seg)
    assert state.state == STATE_UNKNOWN


async def test_segment_readback_remembers_colour_for_later_turn_on(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """A colour learned from read-back is what a later 'off' then 'on' restores."""
    seg = _segment(hass, SEG)
    _seed(enabled_entry, segments=[Segment(index=SEG, rgb=GREEN, brightness=60)])
    await _call(hass, "turn_off", seg)
    mock_device.set_segment_rgb.reset_mock()
    await _call(hass, "turn_on", seg)
    mock_device.set_segment_rgb.assert_awaited_once_with([SEG], GREEN)


async def test_segment_partial_failure_still_publishes(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """The colour lands, the brightness frame fails: error raised, landed colour published."""
    seg = _segment(hass, SEG)
    _fail(mock_device, GoveeBleConnectionError("dropped"), after=1)

    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, "turn_on", seg, rgb_color=RED, brightness=128)

    assert exc.value.translation_key == "ble_command_failed"
    assert connection(mock_device).sent == [_seg_rgb_frame(RED)]
    state = hass.states.get(seg)
    assert state.state == STATE_ON
    assert state.attributes["rgb_color"] == RED


async def test_segment_turn_off_failure_leaves_state(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """A failed segment turn_off raises and the segment keeps showing on."""
    seg = _segment(hass, SEG)
    await _call(hass, "turn_on", seg, rgb_color=RED)
    _fail(mock_device, BleakError("gone"))
    with pytest.raises(HomeAssistantError):
        await _call(hass, "turn_off", seg)
    assert hass.states.get(seg).state == STATE_ON


async def test_segment_auth_error_starts_reauth(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """Sub-lights share the main light's error translation (reauth on a rejected secret)."""
    _fail(mock_device, GoveeBleAuthError("rejected"))
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, "turn_on", _segment(hass, SEG))
    assert exc.value.translation_key == "secret_rejected"
    await hass.async_block_till_done()
    assert [f["context"]["source"] for f in _govee_flows(hass)] == ["reauth"]


async def test_segment_lights_unavailable_with_the_device(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, presence: SimpleNamespace
) -> None:
    """Segment lights go unavailable together with the rest of the device."""
    seg = _segment(hass, SEG)
    presence.connectable = False
    enabled_entry.runtime_data.coordinator.consecutive_failures = UNAVAILABLE_AFTER_FAILURES
    enabled_entry.runtime_data.coordinator.async_update_listeners()
    assert hass.states.get(seg).state == STATE_UNAVAILABLE
    assert enabled_entry.state is ConfigEntryState.LOADED


# -- 9. zone lights -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("is_on", "zone_power", "expected"),
    [
        (False, {0: True, 1: True}, STATE_OFF),  # fixture off beats a stale zone power
        (True, {0: True, 1: False}, STATE_ON),
        (None, {0: False, 1: True}, STATE_OFF),
        (True, {}, STATE_UNKNOWN),  # zone power never read
    ],
)
async def test_zone_light_state(
    hass: HomeAssistant,
    enabled_entry: MockConfigEntry,
    is_on: bool | None,
    zone_power: dict[int, bool],
    expected: str,
) -> None:
    """A zone light is off whenever the fixture is off, else it shows the zone's own power."""
    _seed(enabled_entry, is_on=is_on, zone_power=zone_power)
    assert hass.states.get(_zone(hass, "main")).state == expected


async def test_zone_turn_on_with_rgb_and_brightness(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """An off zone is switched on first, then its colour and brightness go to its segments."""
    zone = _zone(hass, "main")
    _seed(enabled_entry, is_on=True, zone_power={0: False, 1: True})
    await _call(hass, "turn_on", zone, rgb_color=(5, 6, 7), brightness=200)

    mock_device.set_zone_rgb.assert_awaited_once_with("main", (5, 6, 7))
    mock_device.set_segment_brightness.assert_awaited_once_with([12], 78)
    mock_device.set_zone_power.assert_awaited_once_with("main", True)
    mock_device.set_zone_color_temp.assert_not_awaited()
    assert connection(mock_device).sent == [
        build.zone_power(0, True),
        _seg_rgb_frame((5, 6, 7), 12),
        _seg_bri_frame(78, 12),
    ]
    state = hass.states.get(zone)
    assert state.state == STATE_ON
    assert state.attributes["rgb_color"] == (5, 6, 7)
    assert state.attributes["brightness"] == 199  # 78 % of 255
    assert state.attributes["color_mode"] == "rgb"


@KELVIN_RANGES
async def test_zone_turn_on_with_colour_temp(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device, lo: int, hi: int
) -> None:
    """Zone colour temperature is clamped, sent for the zone, and reported as color_temp."""
    zone = _zone(hass, "background")
    _seed(enabled_entry, is_on=True, zone_power={0: True, 1: False})
    await _call(hass, "turn_on", zone, color_temp_kelvin=9000)

    mock_device.set_zone_color_temp.assert_awaited_once_with("background", hi)
    mock_device.set_zone_power.assert_awaited_once_with("background", True)
    mock_device.set_zone_rgb.assert_not_awaited()
    state = hass.states.get(zone)
    assert state.state == STATE_ON
    assert state.attributes["color_mode"] == "color_temp"
    assert state.attributes["color_temp_kelvin"] == hi


async def test_zone_brightness_targets_all_zone_segments(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """The background zone's brightness goes to all twelve ring segments."""
    await _call(hass, "turn_on", _zone(hass, "background"), brightness=128)
    mock_device.set_segment_brightness.assert_awaited_once_with(list(range(12)), 50)


async def test_zone_plain_turn_on_only_powers_the_zone(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """A plain zone turn_on sends just the zone power frame."""
    zone = _zone(hass, "main")
    _seed(enabled_entry, is_on=True, zone_power={0: False, 1: True})
    await _call(hass, "turn_on", zone)
    assert connection(mock_device).sent == [build.zone_power(0, True)]
    assert hass.states.get(zone).state == STATE_ON


async def _poll_back(entry: MockConfigEntry, zone_power: dict[int, bool]) -> None:
    """Run a poll that read the fixture on and the zones' power back, as the library applies it.

    A read that carries a zone's power stamps it confirmed (``zone_power_confirmed``, on
    ``time.monotonic()``); one whose reads go unanswered stamps nothing.
    """
    device = entry.runtime_data.device

    async def _update(**_kwargs: Any) -> Any:
        device.state.is_on = True
        device.state.zone_power = dict(zone_power)
        device.state.zone_power_confirmed = dict.fromkeys(zone_power, time.monotonic())
        device.state.optimistic = False
        return device.state

    device.update.side_effect = _update  # type: ignore[attr-defined]
    await entry.runtime_data.coordinator.async_refresh()


def _age_confirmations(entry: MockConfigEntry, seconds: float) -> None:
    """Move every zone's confirmation ``seconds`` into the past."""
    state = entry.runtime_data.device.state
    state.zone_power_confirmed = {i: at - seconds for i, at in state.zone_power_confirmed.items()}


LIT_CHANGES = pytest.mark.parametrize(
    "change",
    [
        {"rgb_color": (5, 6, 7), "brightness": 200},
        {"color_temp_kelvin": 4000},
        {"brightness": 200},
        {"color_temp_kelvin": 4000, "brightness": 200},
    ],
    ids=["rgb_brightness", "color_temp", "brightness", "color_temp_brightness"],
)


@LIT_CHANGES
async def test_lit_zone_change_sends_no_zone_power(
    hass: HomeAssistant,
    enabled_entry: MockConfigEntry,
    mock_device: Device,
    change: dict[str, Any],
) -> None:
    """A change to a zone a poll just read on sends only that change, whatever its shape."""
    await _poll_back(enabled_entry, {0: True, 1: True})
    connection(mock_device).sent.clear()
    await _call(hass, "turn_on", _zone(hass, "main"), **change)
    mock_device.set_zone_power.assert_not_awaited()
    assert build.zone_power(0, True) not in connection(mock_device).sent
    assert connection(mock_device).sent  # the change itself went out


@pytest.mark.parametrize(
    "zone_power", [{0: False, 1: True}, {1: True}], ids=["zone_off", "zone_unknown"]
)
@pytest.mark.parametrize(
    "change",
    [{"color_temp_kelvin": 4000}, {"brightness": 128}, {"rgb_color": RED}],
    ids=["color_temp", "brightness", "rgb"],
)
async def test_off_or_unknown_zone_is_powered_first(
    hass: HomeAssistant,
    enabled_entry: MockConfigEntry,
    mock_device: Device,
    zone_power: dict[int, bool],
    change: dict[str, Any],
) -> None:
    """A zone not known to be on is switched on before the change, whatever its shape."""
    await _poll_back(enabled_entry, zone_power)
    connection(mock_device).sent.clear()
    await _call(hass, "turn_on", _zone(hass, "main"), **change)
    sent = connection(mock_device).sent
    assert sent[0] == build.zone_power(0, True)
    assert len(sent) == 2  # then the change's frame


async def test_plain_turn_on_powers_even_a_lit_zone(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """A plain turn_on is the zone's on switch: it is sent whatever HA thinks the zone is."""
    await _poll_back(enabled_entry, {0: True, 1: True})
    connection(mock_device).sent.clear()
    await _call(hass, "turn_on", _zone(hass, "main"))
    assert connection(mock_device).sent == [build.zone_power(0, True)]


async def test_the_library_confirms_an_acked_zone_write_and_only_that_zone() -> None:
    """The contract the zone light rests on: an acked zone write stamps that zone alone."""
    device = make_device("H60A6")
    device.state.zone_power = {0: False, 1: True}
    device.state.zone_power_confirmed = {}
    before = time.monotonic()
    await device.set_zone_power("main", True)  # acked on the fake link
    assert device.state.zone_power_confirmed.get(0, before - 1) >= before
    assert 1 not in device.state.zone_power_confirmed


@pytest.mark.parametrize("sku", ["H60A6", "H6047"])
async def test_an_unanswered_poll_confirms_nothing(sku: str) -> None:
    """A real update() whose reads all go unanswered leaves zone power and its confirmations
    exactly as they were."""
    device = create_device(BLEDevice(address=ADDRESS, name=f"GV{sku}ABCD", details={}), sku, None)
    device._conn = FakeConnection()  # type: ignore[assignment]
    device.state.zone_power = {0: True, 1: False}
    device.state.zone_power_confirmed = {0: time.monotonic() - 200}
    confirmed = dict(device.state.zone_power_confirmed)
    await device.update()
    assert device.state.zone_power == {0: True, 1: False}
    assert device.state.zone_power_confirmed == confirmed


def _zone_ops(device: Device) -> list[tuple[str, Any]]:
    """Every library call HA makes that can change a zone's power, by name."""
    ops: list[tuple[str, Any]] = [
        ("fixture_on", lambda: device.set_power(True)),
        ("fixture_off", lambda: device.set_power(False)),
    ]
    ops += [
        (f"{z.name}_{on}", lambda n=z.name, o=on: device.set_zone_power(n, o))
        for z in device.zones
        for on in (True, False)
    ]
    if Capability.SCENES in device.capabilities:
        ops.append(("scene", lambda: device.set_scene(1)))
    return ops


@pytest.mark.parametrize("sku", ["H60A6", "H6047"])
@pytest.mark.parametrize(
    "zone_power",
    [{0: True, 1: True}, {0: False, 1: True}, {0: False, 1: False}],
    ids=["both_on", "one_on", "both_off"],
)
async def test_a_confirmation_never_vouches_for_a_change_it_didnt_see(
    sku: str, zone_power: dict[int, bool]
) -> None:
    """Whatever HA sends, a zone whose power it changed keeps no confirmation from before:
    the device confirmed the new value during the call, or the zone has none."""
    device = make_device(sku)
    for name, op in _zone_ops(device):
        device.state.is_on = True
        device.state.zone_power = dict(zone_power)
        device.state.zone_power_confirmed = dict.fromkeys(zone_power, time.monotonic() - 60)
        started = time.monotonic()
        await op()
        after = device.state.zone_power
        for index in zone_power.keys() | after.keys():
            if after.get(index) == zone_power.get(index):
                continue
            confirmed = device.state.zone_power_confirmed.get(index)
            assert confirmed is None or confirmed >= started, (name, index)


async def test_zone_of_unknown_state_isnt_lit_by_a_fresh_stamp(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """A zone whose state is unknown is powered first, even with a fresh confirmation."""
    await _poll_back(enabled_entry, {0: True, 1: True})
    _seed(enabled_entry, zone_power={1: True})  # the main panel's state forgotten
    connection(mock_device).sent.clear()
    await _call(hass, "turn_on", _zone(hass, "main"), rgb_color=RED)
    assert connection(mock_device).sent[0] == build.zone_power(0, True)


@pytest.mark.parametrize(
    ("zone_power", "age"),
    [({0: True, 1: True}, ZONE_STATE_FRESH_SECONDS + 1), ({0: False, 1: True}, 0)],
    ids=["read_long_ago", "read_off_just_now"],
)
async def test_a_scene_confirms_no_zone(
    hass: HomeAssistant,
    enabled_entry: MockConfigEntry,
    mock_device: Device,
    *,
    zone_power: dict[int, bool],
    age: float,
) -> None:
    """Activating a scene can set zone power, but never confirms a zone: one last read long
    ago, or read off just before the scene switched it on, still gets its power first."""
    await _poll_back(enabled_entry, zone_power)
    _age_confirmations(enabled_entry, age)
    await _call(hass, "turn_on", MAIN, effect="Aurora")
    connection(mock_device).sent.clear()
    await _call(hass, "turn_on", _zone(hass, "main"), rgb_color=RED)
    assert connection(mock_device).sent[0] == build.zone_power(0, True)


async def test_zone_lit_by_a_seed_alone_is_not_confirmed(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """State nothing read back or acked (here, seeded) doesn't count: power goes first."""
    _seed(enabled_entry, is_on=True, zone_power={0: True, 1: True})
    await _call(hass, "turn_on", _zone(hass, "main"), rgb_color=RED)
    assert connection(mock_device).sent[0] == build.zone_power(0, True)


async def test_a_poll_whose_reads_go_unanswered_confirms_nothing(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """A poll that "succeeds" without reading the zones back leaves them unconfirmed."""
    _seed(enabled_entry, is_on=True, zone_power={0: True, 1: True})
    await enabled_entry.runtime_data.coordinator.async_refresh()  # reads nothing back
    assert enabled_entry.runtime_data.coordinator.last_update_success
    connection(mock_device).sent.clear()
    await _call(hass, "turn_on", _zone(hass, "main"), rgb_color=RED)
    assert connection(mock_device).sent[0] == build.zone_power(0, True)


async def test_ha_switching_a_zone_on_confirms_it(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """A zone HA just switched on (the device acked it) takes the next change without power."""
    zone = _zone(hass, "main")
    _seed(enabled_entry, is_on=True, zone_power={0: False, 1: True})
    await _call(hass, "turn_on", zone)
    connection(mock_device).sent.clear()
    await _call(hass, "turn_on", zone, rgb_color=RED)
    assert connection(mock_device).sent == [_seg_rgb_frame(RED, 12)]


@pytest.mark.parametrize(
    ("age", "powered"),
    [(ZONE_STATE_FRESH_SECONDS - 1, False), (ZONE_STATE_FRESH_SECONDS, True)],
    ids=["fresh", "stale"],
)
async def test_lit_zone_confirmed_too_long_ago_is_powered_first(
    hass: HomeAssistant,
    enabled_entry: MockConfigEntry,
    mock_device: Device,
    *,
    age: int,
    powered: bool,
) -> None:
    """A zone read on ZONE_STATE_FRESH_SECONDS ago or more may be off by now: power first."""
    await _poll_back(enabled_entry, {0: True, 1: True})
    _age_confirmations(enabled_entry, age)
    connection(mock_device).sent.clear()
    await _call(hass, "turn_on", _zone(hass, "main"), rgb_color=RED)
    sent = connection(mock_device).sent
    assert (sent[0] == build.zone_power(0, True)) is powered
    assert sent[-1] == _seg_rgb_frame(RED, 12)


async def test_failed_power_on_an_off_zone_stops_the_change(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """Switching an off zone on fails: the colour isn't sent to a zone that stays dark."""
    await _poll_back(enabled_entry, {0: False, 1: True})
    connection(mock_device).sent.clear()
    _fail(mock_device, GoveeBleTimeout("no ack"))
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, "turn_on", _zone(hass, "main"), rgb_color=RED)
    assert exc.value.translation_key == "ble_command_failed"
    mock_device.set_zone_rgb.assert_not_awaited()


async def test_a_write_to_one_zone_doesnt_confirm_the_other(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """Switching the main panel on carries the ring's cached state along, but confirms only
    the main panel: a ring read long ago (it may be off now) still gets its power first."""
    await _poll_back(enabled_entry, {0: True, 1: True})
    _age_confirmations(enabled_entry, ZONE_STATE_FRESH_SECONDS - 20)
    await _call(hass, "turn_on", _zone(hass, "main"))  # writes {main: on, ring: cached}
    _age_confirmations(enabled_entry, 30)  # the ring's read is now stale; main's write 30 s
    connection(mock_device).sent.clear()
    await _call(hass, "turn_on", _zone(hass, "background"), rgb_color=RED)
    assert connection(mock_device).sent[0] == build.zone_power(1, True)


@pytest.mark.parametrize(
    "error",
    [GoveeBleError("refused"), GoveeBleConnectionError("link lost"), GoveeBleTimeout("no ack")],
    ids=["refused", "link_lost", "no_ack"],
)
async def test_failed_power_on_a_stale_lit_zone_stops_the_change(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device, error: Exception
) -> None:
    """A zone shown on but not confirmed lately is switched on first; if that fails, for any
    reason, the change isn't sent: the zone may be dark (and on a dead link, a second long
    operation would fail too)."""
    await _poll_back(enabled_entry, {0: True, 1: True})
    _age_confirmations(enabled_entry, ZONE_STATE_FRESH_SECONDS + 1)
    _fail(mock_device, error)
    with pytest.raises(HomeAssistantError):
        await _call(hass, "turn_on", _zone(hass, "main"), rgb_color=RED)
    mock_device.set_zone_power.assert_awaited_once_with("main", True)
    mock_device.set_zone_rgb.assert_not_awaited()


async def test_zone_of_a_fixture_known_off_is_powered_first(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """A zone read on, in a fixture since reported off (an advert), is switched on first."""
    await _poll_back(enabled_entry, {0: True, 1: True})
    _seed(enabled_entry, is_on=False)  # zone power stays cached on
    connection(mock_device).sent.clear()
    await _call(hass, "turn_on", _zone(hass, "main"), rgb_color=RED)
    assert connection(mock_device).sent[0] == build.zone_power(0, True)


async def test_zone_turn_off(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """Zone turn_off sends the zone power-off and leaves the sibling zone alone."""
    main, background = _zone(hass, "main"), _zone(hass, "background")
    _seed(enabled_entry, is_on=True, zone_power={0: True, 1: True})
    await _call(hass, "turn_off", main)
    mock_device.set_zone_power.assert_awaited_once_with("main", False)
    assert connection(mock_device).sent == [build.zone_power(0, False)]
    assert hass.states.get(main).state == STATE_OFF
    assert hass.states.get(background).state == STATE_ON
    assert hass.states.get(MAIN).state == STATE_ON


async def test_last_zone_off_turns_the_fixture_off(
    hass: HomeAssistant, enabled_entry: MockConfigEntry
) -> None:
    """Turning the last lit zone off is published to the main light too."""
    _seed(enabled_entry, is_on=True, zone_power={0: False, 1: True})
    await _call(hass, "turn_off", _zone(hass, "background"))
    assert hass.states.get(MAIN).state == STATE_OFF


async def test_zone_readback_adopts_colour_and_brightness(
    hass: HomeAssistant, enabled_entry: MockConfigEntry
) -> None:
    """A zone light adopts its segments' read-back colour and brightness."""
    main, background = _zone(hass, "main"), _zone(hass, "background")
    _seed(
        enabled_entry,
        is_on=True,
        zone_power={0: True, 1: True},
        segments=[
            Segment(index=0, rgb=(1, 2, 3), brightness=40),
            Segment(index=12, rgb=(9, 8, 7), brightness=100),
        ],
    )
    assert hass.states.get(main).attributes["rgb_color"] == (9, 8, 7)
    assert hass.states.get(main).attributes["brightness"] == 255
    assert hass.states.get(background).attributes["rgb_color"] == (1, 2, 3)
    assert hass.states.get(background).attributes["brightness"] == 102


async def test_out_of_range_readback_brightness_is_clamped(
    hass: HomeAssistant, enabled_entry: MockConfigEntry
) -> None:
    """A read-back brightness above 100 % shows as full brightness, never above 255."""
    _seed(
        enabled_entry,
        is_on=True,
        brightness=150,
        zone_power={0: True, 1: True},
        segments=[
            Segment(index=SEG, rgb=RED, brightness=200),
            Segment(index=12, rgb=RED, brightness=200),
        ],
    )
    assert hass.states.get(MAIN).attributes["brightness"] == 255
    assert hass.states.get(_segment(hass, SEG)).attributes["brightness"] == 255
    assert hass.states.get(_zone(hass, "main")).attributes["brightness"] == 255


async def test_sub_light_debug_logs_name_the_device(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """Segment/zone command logs carry the device address and the segment's shown number."""
    caplog.set_level(logging.DEBUG, logger="custom_components.govee_ble_local.light")
    await _call(hass, "turn_on", _segment(hass, SEG), rgb_color=RED)
    await _call(hass, "turn_on", _zone(hass, "main"), rgb_color=RED)
    assert f"Setting segment {SEG + 1} of {ADDRESS} RGB to (255, 0, 0)" in caplog.text
    assert f"Setting zone main of {ADDRESS} RGB to (255, 0, 0)" in caplog.text


async def test_zone_readback_without_matching_segments_or_values(
    hass: HomeAssistant, enabled_entry: MockConfigEntry
) -> None:
    """Segments of other zones, or reports without brightness, don't disturb a zone light."""
    main = _zone(hass, "main")
    _seed(
        enabled_entry,
        is_on=True,
        zone_power={0: True, 1: True},
        segments=[Segment(index=2, rgb=RED, brightness=50)],
    )
    attrs = hass.states.get(main).attributes
    assert attrs["rgb_color"] is None
    assert attrs["brightness"] is None
    _seed(enabled_entry, segments=[Segment(index=12, rgb=GREEN, brightness=0)])
    attrs = hass.states.get(main).attributes
    assert attrs["rgb_color"] == GREEN
    assert attrs["brightness"] is None


async def test_zone_readback_does_not_override_colour_temp_mode(
    hass: HomeAssistant, enabled_entry: MockConfigEntry
) -> None:
    """After a zone colour temperature, read-back rgb doesn't pull the zone back to rgb."""
    zone = _zone(hass, "main")
    await _call(hass, "turn_on", zone, color_temp_kelvin=4000)
    _seed(enabled_entry, segments=[Segment(index=12, rgb=BLACK, brightness=70)])
    attrs = hass.states.get(zone).attributes
    assert attrs["color_mode"] == "color_temp"
    assert attrs["color_temp_kelvin"] == 4000


async def test_zone_partial_failure_still_publishes(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """Power and colour land but the brightness frame fails: what landed is published."""
    zone = _zone(hass, "main")
    _seed(enabled_entry, is_on=True, zone_power={0: False, 1: True}, segments=[])
    _fail(mock_device, GoveeBleTimeout("no ack"), after=2)

    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, "turn_on", zone, rgb_color=RED, brightness=128)

    assert exc.value.translation_key == "ble_command_failed"
    state = hass.states.get(zone)
    assert state.state == STATE_ON
    assert state.attributes["rgb_color"] == RED
    assert state.attributes["brightness"] is None  # the frame that failed


async def test_zone_turn_off_failure(
    hass: HomeAssistant, enabled_entry: MockConfigEntry, mock_device: Device
) -> None:
    """A failed zone turn_off raises a translated error and the zone stays on."""
    zone = _zone(hass, "main")
    _seed(enabled_entry, is_on=True, zone_power={0: True, 1: True})
    _fail(mock_device, GoveeBleConnectionError("nope"))
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, "turn_off", zone)
    assert exc.value.translation_key == "ble_command_failed"
    assert hass.states.get(zone).state == STATE_ON


@pytest.mark.parametrize("device_sku", ["H6047"])
async def test_h6047_zone_turn_on_keeps_other_bar(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """Bar zones go through the combined bar frame without disturbing the other bar."""
    left, right = _zone(hass, "left"), _zone(hass, "right")
    _seed(entry, is_on=True, zone_power={0: False, 1: False})
    await _call(hass, "turn_on", left, rgb_color=RED)
    mock_device.set_zone_rgb.assert_awaited_once_with("left", RED)
    mock_device.set_zone_power.assert_awaited_once_with("left", True)
    assert connection(mock_device).sent[0] == build.bar_switch(True, False)  # power first
    assert hass.states.get(left).state == STATE_ON
    assert hass.states.get(right).state == STATE_OFF


@pytest.mark.parametrize("device_sku", ["H6047"])
async def test_h6047_lit_bar_change_reads_and_switches_nothing(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A lit bar's colour change is one frame: no bar switch, so no aa 36 read before it."""
    await _poll_back(entry, {0: True})  # the other bar's state unknown
    connection(mock_device).sent.clear()
    connection(mock_device).queries.clear()
    await _call(hass, "turn_on", _zone(hass, "left"), rgb_color=RED)
    mock_device.set_zone_power.assert_not_awaited()
    assert len(connection(mock_device).sent) == 1
    assert build.bar_switch_query() not in connection(mock_device).queries


@pytest.mark.parametrize("device_sku", ["H6047"])
async def test_h6047_whole_fixture_on_unconfirms_the_bars(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A whole-fixture turn-on leaves which bars lit unknown: a bar confirmed just before
    gets its bar switch first again (here its aa 36 read goes unanswered, so it fails)."""
    await _poll_back(entry, {0: True, 1: False})
    await _call(hass, "turn_on", MAIN)
    with pytest.raises(HomeAssistantError):
        await _call(hass, "turn_on", _zone(hass, "left"), rgb_color=RED)
    mock_device.set_zone_power.assert_awaited_once_with("left", True)


@pytest.mark.parametrize("device_sku", ["H6047"])
async def test_h6047_bar_of_unknown_state_gets_no_colour_when_its_switch_is_refused(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """Both bars unknown (after a whole-fixture on), the library refuses the bar switch (its
    aa 36 read goes unanswered): the bar may be dark, so its colour isn't sent."""
    _seed(entry, is_on=True, zone_power={})
    with pytest.raises(HomeAssistantError):
        await _call(hass, "turn_on", _zone(hass, "left"), rgb_color=RED)
    assert build.bar_switch_query() in connection(mock_device).queries
    mock_device.set_zone_rgb.assert_not_awaited()
