"""Light platform for Govee BLE lights (capability-driven)."""

from __future__ import annotations

from abc import abstractmethod
import logging
from typing import TYPE_CHECKING, Any

from govee_ble_local import Capability, Device
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    EFFECT_OFF,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_MAX_KELVIN, DEFAULT_MIN_KELVIN, DOMAIN
from .entity import GoveeBleLocalEntity
from .helpers import brightness_to_pct, pct_to_brightness

if TYPE_CHECKING:
    from . import GoveeBleLocalConfigEntry

_LOGGER = logging.getLogger(__name__)

# Commands for one device are serialized per platform here (and across
# platforms by the library's per-device connection lock).
PARALLEL_UPDATES = 1

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
_COLOUR_KWARGS = {ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ATTR_COLOR_TEMP_KELVIN, ATTR_EFFECT}


def _kelvin_range(device: Device) -> tuple[int, int]:
    return (device.min_kelvin or DEFAULT_MIN_KELVIN, device.max_kelvin or DEFAULT_MAX_KELVIN)


def _clamp_kelvin(device: Device, kelvin: int) -> int:
    """Clamp a requested colour temperature into the device's supported range."""
    lo, hi = _kelvin_range(device)
    return max(lo, min(hi, kelvin))


def _sub_light_modes(device: Device) -> set[ColorMode]:
    """Return the colour modes of a zone/segment sub-light.

    Always RGB, plus COLOR_TEMP when the fixture supports colour temperature (the
    library exposes masked, independently-addressable per-zone/segment kelvin).
    """
    modes = {ColorMode.RGB}
    if Capability.COLOR_TEMP in device.capabilities:
        modes.add(ColorMode.COLOR_TEMP)
    return modes


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeBleLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the light entities for a device.

    - the whole-fixture light (power/brightness/colour/scene),
    - one light per colour-controllable zone (a zone that has segments; on/off-only
      zones are switches instead - see switch.py),
    - one light per addressable segment (disabled by default).

    A device with no light-relevant capability (e.g. a smart plug) gets none.
    """
    device = entry.runtime_data.device
    caps = device.capabilities

    entities: list[LightEntity] = []
    if caps & {Capability.BRIGHTNESS, Capability.RGB, Capability.COLOR_TEMP}:
        entities.append(GoveeBleLocalLight(entry))
    entities.extend(
        GoveeBleLocalZoneLight(entry, zone.name) for zone in device.zones if zone.segments
    )
    if Capability.SEGMENTS in caps:
        entities.extend(
            GoveeBleLocalSegmentLight(entry, index) for index in range(device.profile.segments)
        )
    async_add_entities(entities)


class _SubLight(GoveeBleLocalEntity, LightEntity):
    """Shared colour handling for zone and segment lights.

    Colour/brightness are read back best-effort from the device's segments and
    tracked optimistically on each command.
    """

    def __init__(self, entry: GoveeBleLocalConfigEntry, key: str) -> None:
        super().__init__(entry, key)
        self._attr_supported_color_modes = _sub_light_modes(self._device)
        if ColorMode.COLOR_TEMP in self._attr_supported_color_modes:
            self._attr_min_color_temp_kelvin, self._attr_max_color_temp_kelvin = _kelvin_range(
                self._device
            )
        self._attr_color_mode = ColorMode.RGB  # updated on command
        self._attr_rgb_color: tuple[int, int, int] | None = None
        self._attr_color_temp_kelvin: int | None = None
        self._attr_brightness: int | None = None

    @abstractmethod
    def _sync_from_readback(self) -> None:
        """Adopt the device's read-back colour/brightness for this sub-light."""

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync_from_readback()
        super()._handle_coordinator_update()


class GoveeBleLocalLight(GoveeBleLocalEntity, LightEntity):
    """Overall power/brightness/color/scene control for the fixture.

    State comes from the coordinator's DeviceState, which is the single source
    of truth: the library polls it back over BLE for devices that support
    read-back, and for the rest it reflects the last command sent (optimistic).
    Commands mutate that same object, so the UI updates immediately and later
    polls reconcile with the real device.
    """

    _attr_name = None

    def __init__(self, entry: GoveeBleLocalConfigEntry) -> None:
        """Initialize the whole-fixture light."""
        super().__init__(entry, "light")
        caps = self._device.capabilities
        modes: set[ColorMode] = set()
        if Capability.RGB in caps:
            modes.add(ColorMode.RGB)
        if Capability.COLOR_TEMP in caps:
            modes.add(ColorMode.COLOR_TEMP)
            self._attr_min_color_temp_kelvin, self._attr_max_color_temp_kelvin = _kelvin_range(
                self._device
            )
        if not modes:
            modes = {ColorMode.BRIGHTNESS if Capability.BRIGHTNESS in caps else ColorMode.ONOFF}
        self._attr_supported_color_modes = modes
        if Capability.SCENES in caps:
            self._attr_supported_features = LightEntityFeature.EFFECT
            # EFFECT_OFF lets the user clear a running scene from the UI.
            self._attr_effect_list = [EFFECT_OFF, *self._device.scene_names]

        # Default color mode when the device hasn't reported/been set to one.
        if ColorMode.COLOR_TEMP in modes:
            self._default_color_mode = ColorMode.COLOR_TEMP
        elif ColorMode.RGB in modes:
            self._default_color_mode = ColorMode.RGB
        else:
            self._default_color_mode = next(iter(modes))
        # The scene we last activated, for devices that don't report the active one.
        self._attr_effect: str | None = None

    @property
    def is_on(self) -> bool | None:
        """Return whether the fixture is on (None while unknown)."""
        return self.coordinator.data.is_on

    @property
    def brightness(self) -> int | None:
        """Return the HA brightness (0..255)."""
        pct = self.coordinator.data.brightness
        return None if pct is None else pct_to_brightness(pct)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the solid colour, if in RGB mode."""
        return self.coordinator.data.rgb_color

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the colour temperature, if in colour-temperature mode."""
        return self.coordinator.data.color_temp_kelvin

    @property
    def effect(self) -> str | None:
        """Return the running scene, or EFFECT_OFF.

        Prefers the scene the device reports as active (read back over BLE),
        falling back to the optimistic value. HA expects EFFECT_OFF (not None)
        when effects are supported but none is running.
        """
        if not (self.supported_features & LightEntityFeature.EFFECT):
            return None
        return self._device.active_scene or self._attr_effect or EFFECT_OFF

    @property
    def color_mode(self) -> ColorMode:
        """Return the active colour mode (always one of the supported modes)."""
        data = self.coordinator.data
        supported = self._attr_supported_color_modes or set()
        if data.color_temp_kelvin is not None and ColorMode.COLOR_TEMP in supported:
            return ColorMode.COLOR_TEMP
        if data.rgb_color is not None and ColorMode.RGB in supported:
            return ColorMode.RGB
        return self._default_color_mode

    @callback
    def _handle_coordinator_update(self) -> None:
        # A poll showing a solid colour/temp means the fixture left our scene.
        data = self.coordinator.data
        if data.rgb_color is not None or data.color_temp_kelvin is not None:
            self._attr_effect = None
        super()._handle_coordinator_update()

    def _exit_scene_colour(self) -> tuple[tuple[int, int, int] | None, int | None]:
        """Return the (rgb, kelvin) to apply to leave scene mode (EFFECT_OFF)."""
        data = self.coordinator.data
        caps = self._device.capabilities
        if Capability.RGB not in caps or (
            data.color_temp_kelvin is not None and Capability.COLOR_TEMP in caps
        ):
            lo, hi = _kelvin_range(self._device)
            return None, data.color_temp_kelvin or (lo + hi) // 2
        return data.rgb_color or WHITE, None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on, optionally setting brightness, colour or a scene."""
        data = self.coordinator.data
        effect: str | None = kwargs.get(ATTR_EFFECT)
        if effect is not None and effect not in (self._attr_effect_list or []):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_effect",
                translation_placeholders={"effect": effect, "device_name": self._device_name},
            )
        brightness = (
            brightness_to_pct(kwargs[ATTR_BRIGHTNESS]) if ATTR_BRIGHTNESS in kwargs else None
        )
        rgb: tuple[int, int, int] | None = kwargs.get(ATTR_RGB_COLOR)
        kelvin = (
            _clamp_kelvin(self._device, kwargs[ATTR_COLOR_TEMP_KELVIN])
            if ATTR_COLOR_TEMP_KELVIN in kwargs
            else None
        )
        if effect == EFFECT_OFF:
            effect = None
            if rgb is None and kelvin is None:
                rgb, kelvin = self._exit_scene_colour()

        # A plain "turn on" always sends power (idempotent; on a zoned fixture it
        # also brings every zone back). For a brightness/colour/effect change on
        # a lit fixture power is skipped - it would re-light zones the user
        # turned off - unless the on/off state is only optimistic and there are
        # no zones to disturb.
        plain = not (kwargs.keys() & _COLOUR_KWARGS)
        power = Capability.POWER in self._device.capabilities and (
            plain or data.is_on is not True or (data.optimistic and not self._device.zones)
        )
        try:
            # Power + brightness + colour go out as ONE batched device operation
            # (one lock, one connection, each frame ack-verified, power first).
            if power or brightness is not None or rgb is not None or kelvin is not None:
                await self._run_client_command(
                    self._device.turn_on(power=power, brightness=brightness, rgb=rgb, kelvin=kelvin)
                )
                if rgb is not None or kelvin is not None:
                    self._attr_effect = None
            if effect is not None:
                await self._run_client_command(self._device.set_scene_by_name(effect))
                self._attr_effect = effect
        finally:
            # Also after a partial failure: the library already applied what landed.
            self._async_publish_command_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the whole fixture off."""
        try:
            await self._run_client_command(self._device.turn_off())
        finally:
            self._async_publish_command_state()


class GoveeBleLocalSegmentLight(_SubLight):
    """One addressable segment of a multi-segment fixture (colour + brightness).

    Segments have no independent power line on Govee's protocol, so "off" is
    modelled as setting the segment to black and "on" re-applies the last real
    colour. The library doesn't track per-segment colour temperature, so while
    one is active the commanded state is kept instead of the read-back colour.
    """

    _attr_translation_key = "segment"
    # A fixture has 10-16 segments; register them but leave the fine-grained
    # control opt-in rather than flooding the UI with per-segment lights.
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: GoveeBleLocalConfigEntry, index: int) -> None:
        """Initialize the light for segment ``index`` (0-based)."""
        super().__init__(entry, f"segment_{index}")
        self._index = index
        self._attr_translation_placeholders = {"number": str(index + 1)}
        self._attr_is_on: bool | None = None
        # Last non-black colour: what "turn on" restores after a black-out "off".
        self._last_color: tuple[int, int, int] | None = None
        self._sync_from_readback()

    def _sync_from_readback(self) -> None:
        """Adopt the device's read-back colour/brightness for this segment."""
        seg = next((s for s in self.coordinator.data.segments if s.index == self._index), None)
        if seg is None:
            return
        if seg.brightness:
            # 0 is also what the library reports for an unreported group
            # brightness, so it is never adopted as a level.
            self._attr_brightness = pct_to_brightness(seg.brightness)
        if self._attr_color_mode == ColorMode.COLOR_TEMP or seg.rgb is None:
            return
        self._attr_rgb_color = seg.rgb
        if seg.rgb != BLACK:
            self._last_color = seg.rgb
        self._attr_is_on = seg.rgb != BLACK

    def _is_dark(self) -> bool:
        if self._attr_is_on is not True:
            return True
        return self._attr_color_mode == ColorMode.RGB and self._attr_rgb_color in (None, BLACK)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Light the segment, optionally with a colour/temperature/brightness."""
        try:
            if ATTR_COLOR_TEMP_KELVIN in kwargs:
                kelvin = _clamp_kelvin(self._device, kwargs[ATTR_COLOR_TEMP_KELVIN])
                _LOGGER.debug("Setting segment %d colour temp to %dK", self._index, kelvin)
                await self._run_client_command(
                    self._device.set_segment_color_temp([self._index], kelvin)
                )
                self._attr_color_temp_kelvin = kelvin
                self._attr_color_mode = ColorMode.COLOR_TEMP
            rgb: tuple[int, int, int] | None = kwargs.get(ATTR_RGB_COLOR)
            if rgb is None and ATTR_COLOR_TEMP_KELVIN not in kwargs and self._is_dark():
                # Plain on / brightness-only on a blacked-out segment: bring back
                # its last real colour (white if it never had one).
                rgb = self._last_color or WHITE
            if rgb is not None:
                _LOGGER.debug("Setting segment %d RGB to %s", self._index, rgb)
                await self._run_client_command(self._device.set_segment_rgb([self._index], rgb))
                self._attr_rgb_color = rgb
                self._attr_color_mode = ColorMode.RGB
                if rgb != BLACK:
                    self._last_color = rgb
            if ATTR_BRIGHTNESS in kwargs:
                pct = brightness_to_pct(kwargs[ATTR_BRIGHTNESS])
                _LOGGER.debug("Setting segment %d brightness to %d%%", self._index, pct)
                await self._run_client_command(
                    self._device.set_segment_brightness([self._index], pct)
                )
                self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
            self._attr_is_on = True
        finally:
            self._async_publish_command_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Black the segment out (there is no per-segment power command)."""
        try:
            await self._run_client_command(self._device.set_segment_rgb([self._index], BLACK))
            self._attr_rgb_color = BLACK
            self._attr_color_mode = ColorMode.RGB
            self._attr_is_on = False
        finally:
            self._async_publish_command_state()


class GoveeBleLocalZoneLight(_SubLight):
    """A colour-controllable physical zone (e.g. H60A6 background, H6047 bars).

    On/off is a real per-zone command (read back into ``state.zone_power``);
    colour targets the zone's segment mask (``set_zone_rgb``) and brightness the
    same mask (``set_segment_brightness``). Only created for zones that actually
    have segments; on/off-only zones are switches (switch.py).
    """

    def __init__(self, entry: GoveeBleLocalConfigEntry, zone_name: str) -> None:
        """Initialize the light for the named zone."""
        super().__init__(entry, f"zone_{zone_name}_light")
        self._zone_name = zone_name
        self._segments = next(
            (tuple(z.segments) for z in self._device.zones if z.name == zone_name), ()
        )
        self._set_zone_name(zone_name)
        self._sync_from_readback()

    def _sync_from_readback(self) -> None:
        """Adopt a representative segment's read-back colour/brightness."""
        seg = next((s for s in self.coordinator.data.segments if s.index in self._segments), None)
        if seg is None:
            return
        if seg.brightness:
            self._attr_brightness = pct_to_brightness(seg.brightness)
        if seg.rgb is not None and self._attr_color_mode != ColorMode.COLOR_TEMP:
            self._attr_rgb_color = seg.rgb

    @property
    def is_on(self) -> bool | None:
        """Return the zone's power (off whenever the whole fixture is known off).

        An advertisement only carries whole-fixture on/off, so a fixture turned
        off elsewhere must not leave its zones showing "on" until the next poll.
        """
        if self.coordinator.data.is_on is False:
            return False
        return self._device.zone_is_on(self._zone_name)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Power the zone on, optionally setting colour/temperature/brightness."""
        try:
            if ATTR_COLOR_TEMP_KELVIN in kwargs:
                kelvin = _clamp_kelvin(self._device, kwargs[ATTR_COLOR_TEMP_KELVIN])
                _LOGGER.debug("Setting zone %s colour temp to %dK", self._zone_name, kelvin)
                await self._run_client_command(
                    self._device.set_zone_color_temp(self._zone_name, kelvin)
                )
                self._attr_color_temp_kelvin = kelvin
                self._attr_color_mode = ColorMode.COLOR_TEMP
            if ATTR_RGB_COLOR in kwargs:
                rgb = kwargs[ATTR_RGB_COLOR]
                _LOGGER.debug("Setting zone %s RGB to %s", self._zone_name, rgb)
                await self._run_client_command(self._device.set_zone_rgb(self._zone_name, rgb))
                self._attr_rgb_color = rgb
                self._attr_color_mode = ColorMode.RGB
            if ATTR_BRIGHTNESS in kwargs:
                pct = brightness_to_pct(kwargs[ATTR_BRIGHTNESS])
                _LOGGER.debug("Setting zone %s brightness to %d%%", self._zone_name, pct)
                await self._run_client_command(
                    self._device.set_segment_brightness(list(self._segments), pct)
                )
                self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
            await self._run_client_command(self._device.set_zone_power(self._zone_name, True))
        finally:
            self._async_publish_command_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Power the zone off."""
        try:
            await self._run_client_command(self._device.set_zone_power(self._zone_name, False))
        finally:
            self._async_publish_command_state()
