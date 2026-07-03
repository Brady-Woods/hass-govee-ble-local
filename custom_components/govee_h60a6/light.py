"""Light platform for the Govee H60A6."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import SEGMENT_COUNT, GoveeH60A6Client
from .const import (
    BROKEN_SCENE_NAMES,
    DOMAIN,
    MAX_COLOR_TEMP_KELVIN,
    MIN_COLOR_TEMP_KELVIN,
    SCENES,
    ZONE_LOWER,
    ZONE_UPPER,
)
from .coordinator import GoveeH60A6Coordinator
from .entity import GoveeH60A6Entity
from .scene_library import SceneData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    address = entry.data["address"]
    serial_number = data.get("serial_number")
    entities: list[LightEntity] = [
        GoveeH60A6Light(
            data["coordinator"],
            data["client"],
            address,
            entry.title,
            data.get("scene_library") or {},
            serial_number,
        )
    ]
    entities.extend(
        GoveeH60A6SegmentLight(
            data["coordinator"],
            data["client"],
            address,
            entry.title,
            index,
            serial_number,
        )
        for index in range(SEGMENT_COUNT)
    )
    async_add_entities(entities)


def _scene_id(scene_code: int) -> tuple[int, int]:
    return (scene_code & 0xFF, (scene_code >> 8) & 0xFF)


def _sorted_selectable_scenes(names) -> list[str]:
    """Alphabetized effect list, with scenes confirmed broken over BLE
    (see const.BROKEN_SCENE_NAMES and PROTOCOL.md 6.3.1/10) left out so
    selecting one doesn't produce a silent no-op or a scene that visibly
    fails to render. They're only hidden from the picker - if the device
    ever reports one of these as its current scene, it's still displayed
    correctly by name (see _scene_id_to_name, built separately and not
    filtered), not shown as "unknown"."""
    return sorted(
        (name for name in names if name.lower() not in BROKEN_SCENE_NAMES),
        key=str.casefold,
    )


class GoveeH60A6Light(GoveeH60A6Entity, LightEntity):
    """Overall power/brightness/color/scene control for the fixture.

    The device has no way to read back the current RGB color or color
    temperature over BLE (only zones/brightness/scene are queryable), so
    those two are tracked optimistically from the last command we sent,
    same as many write-only BLE light integrations.
    """

    _attr_name = None
    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
    _attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN

    def __init__(
        self,
        coordinator: GoveeH60A6Coordinator,
        client: GoveeH60A6Client,
        address: str,
        device_name: str,
        scene_library: dict[str, SceneData],
        serial_number: str | None = None,
    ) -> None:
        super().__init__(coordinator, address, device_name, serial_number)
        self._client = client
        self._attr_unique_id = f"{address}_light"
        self._scene_library = scene_library
        # HA requires color_mode to always be a real value from
        # supported_color_modes once that's set - it can't be None, or the
        # entity fails to even register. We have no way to read back the
        # device's actual current color/temp over BLE, so this is a
        # placeholder default until the user explicitly sets one.
        self._optimistic_color_mode: ColorMode = ColorMode.COLOR_TEMP
        self._optimistic_rgb_color: tuple[int, int, int] | None = None
        self._optimistic_color_temp_kelvin: int | None = 4000

        if scene_library:
            self._attr_effect_list = _sorted_selectable_scenes(scene_library.keys())
            self._scene_id_to_name = {
                _scene_id(data.scene_code): name for name, data in scene_library.items()
            }
        else:
            self._attr_effect_list = _sorted_selectable_scenes(SCENES.keys())
            self._scene_id_to_name = {v: k for k, v in SCENES.items()}

    @property
    def is_on(self) -> bool | None:
        status = self.coordinator.data
        if status is None or status.zone_upper_on is None or status.zone_lower_on is None:
            return None
        return status.zone_upper_on or status.zone_lower_on

    @property
    def brightness(self) -> int | None:
        status = self.coordinator.data
        if status is None or status.brightness_pct is None:
            return None
        return round(status.brightness_pct / 100 * 255)

    @property
    def effect(self) -> str | None:
        status = self.coordinator.data
        if status is None or status.scene_id is None:
            return None
        return self._scene_id_to_name.get(status.scene_id)

    @property
    def color_mode(self) -> ColorMode:
        return self._optimistic_color_mode

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        return self._optimistic_rgb_color

    @property
    def color_temp_kelvin(self) -> int | None:
        return self._optimistic_color_temp_kelvin

    async def _activate_scene(self, effect: str) -> None:
        # Full upload (set_scene_full) is the default when we have real
        # scenceParam data for this effect. Bare activation (no upload) was
        # previously the default on the theory that the device "effectively
        # already has every scene cached" from prior use - that assumption
        # was wrong in practice (confirmed by real-world reports of scene
        # switching failing/no-oping in HA) and bare mode has no way to
        # recover when the assumption doesn't hold: it silently does
        # nothing if the device hasn't actually seen that exact scene
        # before. Full upload is guaranteed correct regardless of cache
        # state, and live device testing (test_scene_switching.py, see
        # PROTOCOL.md 6.2.1) found it far more reliable after fixing the
        # ack-handling bugs there - 100% success across repeated switching
        # for every tested scene up to 13 chunks. One still-open exception:
        # very large scenes (~20 chunks, e.g. "Ocean") are not yet reliable
        # even with full upload - see PROTOCOL.md 6.3/10.
        if effect.lower() in BROKEN_SCENE_NAMES:
            # Hidden from effect_list, but guard here too in case something
            # calls light.turn_on with this effect directly (e.g. a service
            # call or automation bypassing the dropdown).
            raise HomeAssistantError(
                f"'{effect}' is known to fail to render correctly on this device "
                "(see PROTOCOL.md 6.3.1/10) and is disabled until that's resolved."
            )

        scene_data = self._scene_library.get(effect)
        if scene_data is not None:
            _LOGGER.debug("Activating scene %s via full upload (code %d)", effect, scene_data.scene_code)
            await self._run_client_command(
                self._client.set_scene_full(scene_data.scene_code, scene_data.scenceParam)
            )
            return

        scene_id = SCENES.get(effect)
        if scene_id is not None:
            # Static fallback table only has scene codes, not scenceParam
            # data - bare activation is the only option here, and only
            # works if the device already has this scene cached.
            _LOGGER.debug("Activating scene %s with bare id %s (from static fallback table)", effect, scene_id)
            await self._run_client_command(self._client.set_scene(scene_id))
            return

        _LOGGER.warning("Unknown effect requested: %s", effect)

    async def async_turn_on(self, **kwargs: Any) -> None:
        was_off = self.is_on is not True

        if ATTR_BRIGHTNESS in kwargs:
            pct = round(kwargs[ATTR_BRIGHTNESS] / 255 * 100)
            _LOGGER.debug("Setting brightness to %d%%", pct)
            await self._run_client_command(self._client.set_brightness_pct(pct))

        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            _LOGGER.debug("Setting RGB color to (%d, %d, %d)", r, g, b)
            await self._run_client_command(self._client.set_rgb_color(r, g, b))
            self._optimistic_color_mode = ColorMode.RGB
            self._optimistic_rgb_color = (r, g, b)

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            _LOGGER.debug("Setting color temperature to %dK", kelvin)
            await self._run_client_command(self._client.set_color_temp_kelvin(kelvin))
            self._optimistic_color_mode = ColorMode.COLOR_TEMP
            self._optimistic_color_temp_kelvin = kelvin

        if ATTR_EFFECT in kwargs:
            await self._activate_scene(kwargs[ATTR_EFFECT])

        # Only touch zone power when actually turning the fixture on from off.
        # Otherwise a brightness/effect-only call while already on would
        # needlessly re-toggle both zones and can interrupt what was just set.
        if was_off:
            await self._run_client_command(self._client.set_zone(ZONE_UPPER, True))
            await self._run_client_command(self._client.set_zone(ZONE_LOWER, True))

        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._run_client_command(self._client.set_zone(ZONE_UPPER, False))
        await self._run_client_command(self._client.set_zone(ZONE_LOWER, False))
        await self.coordinator.async_request_refresh()


class GoveeH60A6SegmentLight(GoveeH60A6Entity, LightEntity):
    """One individually-addressable segment (PROTOCOL.md 4.2/5.3).

    Unlike the main light entity's RGB/color-temp, segment color and
    brightness genuinely can be read back from the device (confirmed live
    - PROTOCOL.md 5.3), so this entity reflects real polled state instead
    of tracking commands optimistically.

    Named "Segment 0" through "Segment 11" by raw bitmask/record index,
    not a physical position (e.g. "front-left") - which bit corresponds
    to which physical LED position on the fixture has not been confirmed
    by direct observation, only the command format and the fact that the
    command bit and status record index are the same number (PROTOCOL.md
    5.3). This matches the naming convention used by
    wez/govee2mqtt for the same reason, for other Govee segmented models.
    """

    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB

    def __init__(
        self,
        coordinator: GoveeH60A6Coordinator,
        client: GoveeH60A6Client,
        address: str,
        device_name: str,
        index: int,
        serial_number: str | None = None,
    ) -> None:
        super().__init__(coordinator, address, device_name, serial_number)
        self._client = client
        self._index = index
        self._mask = 1 << index
        self._attr_unique_id = f"{address}_segment_{index}"
        self._attr_name = f"Segment {index}"

    def _segment(self):
        status = self.coordinator.data
        if status is None or status.segments is None:
            return None
        return status.segments[self._index]

    @property
    def is_on(self) -> bool | None:
        segment = self._segment()
        if segment is None:
            return None
        return segment.brightness_pct > 0

    @property
    def brightness(self) -> int | None:
        segment = self._segment()
        if segment is None:
            return None
        return round(segment.brightness_pct / 100 * 255)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        segment = self._segment()
        if segment is None:
            return None
        return (segment.r, segment.g, segment.b)

    @property
    def available(self) -> bool:
        # In addition to the usual coordinator-availability check, a
        # segment entity specifically needs segment data to have parsed
        # successfully at least once - which isn't guaranteed the same
        # way the rest of status is (PROTOCOL.md 5.3: chunks 0x05-0x08
        # are captured opportunistically, not required for the status
        # query to otherwise succeed).
        return super().available and self._segment() is not None

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            _LOGGER.debug("Setting segment %d color to (%d, %d, %d)", self._index, r, g, b)
            await self._run_client_command(self._client.set_segment_color(self._mask, r, g, b))

        if ATTR_BRIGHTNESS in kwargs:
            pct = round(kwargs[ATTR_BRIGHTNESS] / 255 * 100)
            _LOGGER.debug("Setting segment %d brightness to %d%%", self._index, pct)
            await self._run_client_command(self._client.set_segment_brightness(self._mask, pct))
        elif not self.is_on:
            # Plain "turn on" with no explicit brightness, and the segment
            # was previously off (brightness 0, our only concept of "off"
            # for a segment - PROTOCOL.md 5.3 found no separate on/off bit)
            # - restore to full brightness rather than silently staying at 0.
            _LOGGER.debug("Turning on segment %d at full brightness", self._index)
            await self._run_client_command(self._client.set_segment_brightness(self._mask, 100))

        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.debug("Turning off segment %d (brightness -> 0)", self._index)
        await self._run_client_command(self._client.set_segment_brightness(self._mask, 0))
        await self.coordinator.async_request_refresh()
