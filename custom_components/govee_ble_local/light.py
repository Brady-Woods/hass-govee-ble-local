"""Light platform for Govee BLE lights (capability-driven)."""
from __future__ import annotations

import logging
from typing import Any

from govee_ble_local import Capability, GoveeDevice
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GoveeBleLocalConfigEntry
from .coordinator import GoveeBleLocalCoordinator
from .entity import GoveeBleLocalEntity

_LOGGER = logging.getLogger(__name__)

# All BLE work funnels through the device's single connection/lock; never let
# HA issue entity commands for this integration concurrently.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeBleLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the main light entity - skipped for a device with no
    light-relevant capability (e.g. a smart plug; see switch.py)."""
    device = entry.runtime_data.device
    caps = device.capabilities
    if not (caps & {Capability.BRIGHTNESS, Capability.RGB, Capability.COLOR_TEMP}):
        return
    async_add_entities(
        [GoveeBleLocalLight(entry.runtime_data.coordinator, device, entry.data["address"], entry.title)]
    )


class GoveeBleLocalLight(GoveeBleLocalEntity, LightEntity):
    """Overall power/brightness/color/scene control for the fixture.

    The v2 library has no reliable BLE state read-back, so all state is tracked
    optimistically from the last command sent (the same pattern the library
    itself uses internally).
    """

    _attr_name = None

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: GoveeDevice,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, address, device_name, device.model)
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
            self._attr_effect_list = device.scene_names

        # Whole-fixture power: use the named zones when present (H60A6), else
        # the global power command.
        self._zone_names = [z.name for z in device.zones]

        # Optimistic state (no BLE read-back).
        if ColorMode.COLOR_TEMP in modes:
            self._attr_color_mode = ColorMode.COLOR_TEMP
        elif ColorMode.RGB in modes:
            self._attr_color_mode = ColorMode.RGB
        else:
            self._attr_color_mode = next(iter(modes))
        self._attr_is_on = None
        self._attr_brightness = None
        self._attr_rgb_color = None
        self._attr_color_temp_kelvin = 4000 if ColorMode.COLOR_TEMP in modes else None
        self._attr_effect = None

    async def async_turn_on(self, **kwargs: Any) -> None:
        was_off = self._attr_is_on is not True

        if ATTR_BRIGHTNESS in kwargs:
            pct = round(kwargs[ATTR_BRIGHTNESS] / 255 * 100)
            _LOGGER.debug("Setting brightness to %d%%", pct)
            await self._run_client_command(self._device.set_brightness(pct))
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]

        if ATTR_RGB_COLOR in kwargs:
            rgb = kwargs[ATTR_RGB_COLOR]
            _LOGGER.debug("Setting RGB color to %s", rgb)
            await self._run_client_command(self._device.set_rgb(rgb))
            self._attr_color_mode = ColorMode.RGB
            self._attr_rgb_color = rgb
            self._attr_effect = None

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            _LOGGER.debug("Setting color temperature to %dK", kelvin)
            await self._run_client_command(self._device.set_color_temp(kelvin))
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_color_temp_kelvin = kelvin
            self._attr_effect = None

        if ATTR_EFFECT in kwargs:
            effect = kwargs[ATTR_EFFECT]
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
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._zone_names:
            for name in self._zone_names:
                await self._run_client_command(self._device.set_zone_power(name, False))
        else:
            await self._run_client_command(self._device.set_power(False))
        self._attr_is_on = False
        self.async_write_ha_state()
