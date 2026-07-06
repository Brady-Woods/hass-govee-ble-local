"""Light platform for Govee BLE lights (capability-driven)."""
from __future__ import annotations

import logging
from typing import Any

from govee_ble_local import Capability, DeviceState, GoveeDevice
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
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
        # device doesn't report its mode.
        return self._device.active_scene or self._attr_effect

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
