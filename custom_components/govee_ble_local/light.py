"""Light platform for Govee BLE lights (capability-driven)."""
from __future__ import annotations

import logging
from typing import Any

from govee_ble_local import Capability, Device, DeviceState
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
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GoveeBleLocalConfigEntry
from .const import ZONE_TRANSLATION_KEYS
from .coordinator import GoveeBleLocalCoordinator
from .entity import GoveeBleLocalEntity

_LOGGER = logging.getLogger(__name__)

# All BLE work funnels through the device's single connection/lock; never let
# HA issue entity commands for this integration concurrently.
PARALLEL_UPDATES = 1


def _sub_light_modes(device: Device) -> set[ColorMode]:
    """Colour modes for a zone/segment sub-light: always RGB, plus COLOR_TEMP
    when the fixture supports colour temperature (the library exposes masked,
    independently-addressable per-zone/segment kelvin)."""
    modes = {ColorMode.RGB}
    if Capability.COLOR_TEMP in device.capabilities:
        modes.add(ColorMode.COLOR_TEMP)
    return modes


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeBleLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the light entities for a device:

    - the whole-fixture light (power/brightness/colour/scene),
    - one light per colour-controllable zone (a zone that has segments; on/off-only
      zones are switches instead - see switch.py),
    - one light per addressable segment (disabled by default).

    A device with no light-relevant capability (e.g. a smart plug) gets none."""
    device = entry.runtime_data.device
    coordinator = entry.runtime_data.coordinator
    address: str = entry.data["address"]
    caps = device.capabilities

    entities: list[LightEntity] = []
    if caps & {Capability.BRIGHTNESS, Capability.RGB, Capability.COLOR_TEMP}:
        entities.append(GoveeBleLocalLight(coordinator, device, address, entry.title))
    # Colour-controllable zones (have segments) become lights; on/off-only zones
    # stay switches (switch.py).
    entities.extend(
        GoveeBleLocalZoneLight(coordinator, device, address, entry.title, zone.name)
        for zone in device.zones
        if zone.segments
    )
    if Capability.SEGMENTS in caps:
        entities.extend(
            GoveeBleLocalSegmentLight(coordinator, device, address, entry.title, index)
            for index in range(device.profile.segments)
        )
    async_add_entities(entities)


class GoveeBleLocalLight(GoveeBleLocalEntity, LightEntity):
    """Overall power/brightness/color/scene control for the fixture.

    State comes from the coordinator's DeviceState, which is the single source
    of truth: the library polls it back over BLE for devices that support
    read-back (H60A6), and for the rest it reflects the last command sent
    (optimistic). Commands mutate that same object, so the UI updates
    immediately and later polls reconcile with the real device.
    """

    _attr_name = None

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: Device,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, address, device_name, device.sku)
        self._device = device
        self._attr_unique_id = f"{address}_light"

        caps = device.capabilities
        modes: set[ColorMode] = set()
        if Capability.RGB in caps:
            modes.add(ColorMode.RGB)
        if Capability.COLOR_TEMP in caps:
            modes.add(ColorMode.COLOR_TEMP)
        if not modes:
            modes = {ColorMode.BRIGHTNESS if Capability.BRIGHTNESS in caps else ColorMode.ONOFF}
        self._attr_supported_color_modes = modes

        if Capability.COLOR_TEMP in caps:
            self._attr_min_color_temp_kelvin = getattr(device, "min_kelvin", 2700)
            self._attr_max_color_temp_kelvin = getattr(device, "max_kelvin", 6500)
        if Capability.SCENES in caps:
            self._attr_supported_features = LightEntityFeature.EFFECT
            # EFFECT_OFF lets the user clear a running scene from the UI.
            self._attr_effect_list = [EFFECT_OFF, *device.scene_names]

        # Whole-fixture power: use the named zones when present (H60A6), else
        # the global power command.
        self._zone_names = [z.name for z in device.zones]

        # Default color mode when the device hasn't reported/been set to one.
        if ColorMode.COLOR_TEMP in modes:
            self._default_color_mode = ColorMode.COLOR_TEMP
        elif ColorMode.RGB in modes:
            self._default_color_mode = ColorMode.RGB
        else:
            self._default_color_mode = next(iter(modes))
        # Effect isn't part of DeviceState, so it's tracked optimistically here.
        self._attr_effect: str | None = None

    @property
    def _data(self) -> DeviceState | None:
        return self.coordinator.data

    @property
    def is_on(self) -> bool | None:
        data = self._data
        return data.is_on if data else None

    @property
    def brightness(self) -> int | None:
        data = self._data
        if data is None or data.brightness is None:
            return None
        return round(data.brightness / 100 * 255)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        data = self._data
        return data.rgb_color if data else None

    @property
    def color_temp_kelvin(self) -> int | None:
        data = self._data
        return data.color_temp_kelvin if data else None

    @property
    def effect(self) -> str | None:
        # Prefer the scene the device reports as active (read back over BLE);
        # fall back to the optimistic value before the first poll / when the
        # device doesn't report its mode. HA expects EFFECT_OFF (not None) when
        # effects are supported but none is running.
        if not (self._attr_supported_features & LightEntityFeature.EFFECT):
            return None
        return self._device.active_scene or self._attr_effect or EFFECT_OFF

    @property
    def color_mode(self) -> ColorMode:
        data = self._data
        if data is not None:
            if data.color_temp_kelvin is not None:
                return ColorMode.COLOR_TEMP
            if data.rgb_color is not None:
                return ColorMode.RGB
        return self._default_color_mode

    @callback
    def _handle_coordinator_update(self) -> None:
        # A scene the device is running isn't reported back in DeviceState; if a
        # poll shows a solid colour/temp, the fixture is no longer on our effect.
        data = self._data
        if data is not None and (data.rgb_color is not None or data.color_temp_kelvin is not None):
            self._attr_effect = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        was_off = self.is_on is not True

        if ATTR_BRIGHTNESS in kwargs:
            pct = round(kwargs[ATTR_BRIGHTNESS] / 255 * 100)
            _LOGGER.debug("Setting brightness to %d%%", pct)
            await self._run_client_command(self._device.set_brightness(pct))

        if ATTR_RGB_COLOR in kwargs:
            rgb = kwargs[ATTR_RGB_COLOR]
            _LOGGER.debug("Setting RGB color to %s", rgb)
            await self._run_client_command(self._device.set_rgb(rgb))
            self._attr_effect = None

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            _LOGGER.debug("Setting color temperature to %dK", kelvin)
            await self._run_client_command(self._device.set_color_temp(kelvin))
            self._attr_effect = None

        if ATTR_EFFECT in kwargs:
            effect = kwargs[ATTR_EFFECT]
            if effect == EFFECT_OFF:
                # Exit scene mode by re-applying a solid colour.
                data = self._data
                rgb = (data.rgb_color if data else None) or (255, 255, 255)
                _LOGGER.debug("Clearing effect via solid RGB %s", rgb)
                await self._run_client_command(self._device.set_rgb(rgb))
                self._attr_effect = None
            else:
                _LOGGER.debug("Activating scene %s", effect)
                await self._run_client_command(self._device.set_scene_by_name(effect))
                self._attr_effect = effect

        # Only touch power when turning the fixture on from off, so a
        # brightness/effect-only call while already on doesn't re-toggle.
        if was_off:
            if self._zone_names:
                for name in self._zone_names:
                    await self._run_client_command(self._device.set_zone_power(name, True))
            else:
                await self._run_client_command(self._device.set_power(True))
            # Zone power isn't reflected in DeviceState; nudge it optimistically
            # (the next poll reconciles). coordinator.data IS the device state.
            if self._data is not None:
                self._data.is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._zone_names:
            for name in self._zone_names:
                await self._run_client_command(self._device.set_zone_power(name, False))
        else:
            await self._run_client_command(self._device.set_power(False))
        if self._data is not None:
            self._data.is_on = False
        self.async_write_ha_state()


class GoveeBleLocalSegmentLight(GoveeBleLocalEntity, LightEntity):
    """One addressable segment of a multi-segment fixture (colour + brightness).

    Segments have no independent power line on Govee's protocol, so "off" is
    modelled as setting the segment to black. Colour/brightness are read back
    from the device where the library supports it (``state.segments``) and
    tracked optimistically on each command for instant feedback.
    """

    _attr_translation_key = "segment"
    # A fixture has 10-16 segments; register them but leave the fine-grained
    # control opt-in rather than flooding the UI with per-segment lights.
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: Device,
        address: str,
        device_name: str,
        index: int,
    ) -> None:
        super().__init__(coordinator, address, device_name, device.sku)
        self._device = device
        self._index = index
        self._attr_unique_id = f"{address}_segment_{index}"
        self._attr_translation_placeholders = {"number": str(index + 1)}
        self._attr_supported_color_modes = _sub_light_modes(device)
        if ColorMode.COLOR_TEMP in self._attr_supported_color_modes:
            self._attr_min_color_temp_kelvin = device.min_kelvin or 2700
            self._attr_max_color_temp_kelvin = device.max_kelvin or 6500
        self._attr_color_mode = ColorMode.RGB  # updated on command
        self._attr_is_on: bool | None = None
        self._attr_rgb_color: tuple[int, int, int] | None = None
        self._attr_color_temp_kelvin: int | None = None
        self._attr_brightness: int | None = None
        self._sync_from_readback()

    def _sync_from_readback(self) -> None:
        """Adopt the device's read-back colour/brightness for this segment."""
        # coordinator.data is typed non-optional by the generic, but is None
        # before the first refresh; widen so the runtime guard type-checks.
        data: DeviceState | None = self.coordinator.data
        if data is None:
            return
        seg = next((s for s in data.segments if s.index == self._index), None)
        if seg is None:
            return
        if seg.rgb is not None:
            self._attr_rgb_color = seg.rgb
        if seg.brightness is not None:
            self._attr_brightness = round(seg.brightness / 100 * 255)
        if seg.brightness is not None:
            self._attr_is_on = seg.brightness > 0
        elif seg.rgb is not None:
            self._attr_is_on = seg.rgb != (0, 0, 0)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync_from_readback()
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            _LOGGER.debug("Setting segment %d colour temp to %dK", self._index, kelvin)
            await self._run_client_command(
                self._device.set_segment_color_temp([self._index], kelvin)
            )
            self._attr_color_temp_kelvin = kelvin
            self._attr_color_mode = ColorMode.COLOR_TEMP
        if ATTR_RGB_COLOR in kwargs:
            rgb = kwargs[ATTR_RGB_COLOR]
            _LOGGER.debug("Setting segment %d RGB to %s", self._index, rgb)
            await self._run_client_command(self._device.set_segment_rgb([self._index], rgb))
            self._attr_rgb_color = rgb
            self._attr_color_mode = ColorMode.RGB
        if ATTR_BRIGHTNESS in kwargs:
            pct = round(kwargs[ATTR_BRIGHTNESS] / 255 * 100)
            _LOGGER.debug("Setting segment %d brightness to %d%%", self._index, pct)
            await self._run_client_command(
                self._device.set_segment_brightness([self._index], pct)
            )
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
        if not (kwargs.keys() & {ATTR_RGB_COLOR, ATTR_COLOR_TEMP_KELVIN, ATTR_BRIGHTNESS}):
            # Plain on with no prior colour: default to white so the segment lights.
            rgb = self._attr_rgb_color or (255, 255, 255)
            await self._run_client_command(self._device.set_segment_rgb([self._index], rgb))
            self._attr_rgb_color = rgb
            self._attr_color_mode = ColorMode.RGB
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        # No per-segment power command; black it out.
        await self._run_client_command(self._device.set_segment_rgb([self._index], (0, 0, 0)))
        self._attr_is_on = False
        self.async_write_ha_state()


class GoveeBleLocalZoneLight(GoveeBleLocalEntity, LightEntity):
    """A colour-controllable physical zone (e.g. H60A6 background, H6047 bars).

    On/off is a real per-zone command (read back into ``state.zone_power``);
    colour targets the zone's segment mask (``set_zone_rgb``) and brightness the
    same mask (``set_segment_brightness``). Colour/brightness are read back
    best-effort from the zone's segments and tracked optimistically on command.
    Only created for zones that actually have segments; on/off-only zones are
    switches (switch.py).
    """

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: Device,
        address: str,
        device_name: str,
        zone_name: str,
    ) -> None:
        super().__init__(coordinator, address, device_name, device.sku)
        self._device = device
        self._zone_name = zone_name
        self._segments = next(
            (tuple(z.segments) for z in device.zones if z.name == zone_name), ()
        )
        self._attr_unique_id = f"{address}_zone_{zone_name}_light"
        self._attr_translation_key = ZONE_TRANSLATION_KEYS.get(zone_name, zone_name)
        self._attr_supported_color_modes = _sub_light_modes(device)
        if ColorMode.COLOR_TEMP in self._attr_supported_color_modes:
            self._attr_min_color_temp_kelvin = device.min_kelvin or 2700
            self._attr_max_color_temp_kelvin = device.max_kelvin or 6500
        self._attr_color_mode = ColorMode.RGB  # updated on command
        self._attr_rgb_color: tuple[int, int, int] | None = None
        self._attr_color_temp_kelvin: int | None = None
        self._attr_brightness: int | None = None
        self._sync_from_readback()

    def _sync_from_readback(self) -> None:
        """Adopt a representative segment's read-back colour/brightness."""
        data: DeviceState | None = self.coordinator.data
        if data is None:
            return
        seg = next((s for s in data.segments if s.index in self._segments), None)
        if seg is None:
            return
        if seg.rgb is not None:
            self._attr_rgb_color = seg.rgb
        if seg.brightness is not None:
            self._attr_brightness = round(seg.brightness / 100 * 255)

    @property
    def is_on(self) -> bool | None:
        # Real per-zone power, polled into state.zone_power; set_zone_power also
        # updates it optimistically.
        return self._device.zone_is_on(self._zone_name)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync_from_readback()
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
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
            pct = round(kwargs[ATTR_BRIGHTNESS] / 255 * 100)
            _LOGGER.debug("Setting zone %s brightness to %d%%", self._zone_name, pct)
            await self._run_client_command(
                self._device.set_segment_brightness(list(self._segments), pct)
            )
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
        await self._run_client_command(self._device.set_zone_power(self._zone_name, True))
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._run_client_command(self._device.set_zone_power(self._zone_name, False))
        self.async_write_ha_state()
