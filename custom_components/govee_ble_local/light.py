"""Light platform for Govee BLE lights (profile-driven)."""
from __future__ import annotations

import logging
from typing import Any

from govee_ble_local import GoveeBleClient, ZONE_UPPER
from govee_ble_local.profile import DeviceProfile
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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GoveeBleLocalConfigEntry
from .const import DOMAIN, ZONE_META
from .coordinator import GoveeBleLocalCoordinator
from .entity import GoveeBleLocalEntity

_LOGGER = logging.getLogger(__name__)

# All BLE work funnels through the client's single connection/lock; never let
# HA issue entity commands for this integration concurrently.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeBleLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the main light entity."""
    data = entry.runtime_data
    async_add_entities(
        [
            GoveeBleLocalLight(
                data.coordinator,
                data.client,
                entry.data["address"],
                entry.title,
                data.profile,
                data.serial_number,
            )
        ]
    )


class GoveeBleLocalLight(GoveeBleLocalEntity, LightEntity):
    """Overall power/brightness/color/scene control for the fixture.

    Capabilities (color modes, temp range, zones, scenes) are taken from the
    device profile. RGB color and color temperature have no reliable BLE
    read-back on the short status query used here, so they're tracked
    optimistically from the last command sent.
    """

    _attr_name = None

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        client: GoveeBleClient,
        address: str,
        device_name: str,
        profile: DeviceProfile,
        serial_number: str | None = None,
    ) -> None:
        super().__init__(coordinator, address, device_name, profile.name, serial_number)
        self._client = client
        self._profile = profile
        self._attr_unique_id = f"{address}_light"

        cap = profile.capabilities
        modes: set[ColorMode] = set()
        if cap.rgb:
            modes.add(ColorMode.RGB)
        if cap.color_temp:
            modes.add(ColorMode.COLOR_TEMP)
        if not modes:
            modes = {ColorMode.BRIGHTNESS if cap.brightness else ColorMode.ONOFF}
        self._attr_supported_color_modes = modes
        if cap.color_temp:
            self._attr_min_color_temp_kelvin, self._attr_max_color_temp_kelvin = cap.color_temp
        if cap.scenes:
            self._attr_supported_features = LightEntityFeature.EFFECT
            self._attr_effect_list = [s.name for s in profile.selectable_scenes()]
        self._scene_id_to_name = {s.scene_id: s.name for s in profile.scenes}

        # BLE zone indices making up this fixture (for whole-light on/off).
        self._zone_indices = [
            ZONE_META[z][0] for z in cap.zones if z in ZONE_META
        ]

        # Optimistic color state (no BLE read-back on the short query).
        if ColorMode.COLOR_TEMP in modes:
            self._optimistic_color_mode = ColorMode.COLOR_TEMP
        elif ColorMode.RGB in modes:
            self._optimistic_color_mode = ColorMode.RGB
        else:
            self._optimistic_color_mode = next(iter(modes))
        self._optimistic_rgb_color: tuple[int, int, int] | None = None
        self._optimistic_color_temp_kelvin: int | None = (
            4000 if cap.color_temp else None
        )

    def _zone_is_on(self, zone_index: int) -> bool | None:
        status = self.coordinator.data
        return status.zone_upper_on if zone_index == ZONE_UPPER else status.zone_lower_on

    @property
    def is_on(self) -> bool | None:
        if self._zone_indices:
            vals = [self._zone_is_on(z) for z in self._zone_indices]
            if any(v is None for v in vals):
                return None
            return any(vals)
        status = self.coordinator.data
        if status.brightness_pct is None:
            return None
        return status.brightness_pct > 0

    @property
    def brightness(self) -> int | None:
        status = self.coordinator.data
        if status.brightness_pct is None:
            return None
        return round(status.brightness_pct / 100 * 255)

    @property
    def effect(self) -> str | None:
        status = self.coordinator.data
        if status.scene_id is None:
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
        scene = self._profile.scene_by_name(effect)
        if scene is None:
            _LOGGER.warning("Unknown effect requested: %s", effect)
            return
        if not scene.working:
            # Hidden from effect_list, but guard here too in case something
            # calls light.turn_on with this effect directly.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="scene_broken",
                translation_placeholders={"effect": effect},
            )
        if scene.param:
            _LOGGER.debug("Activating scene %s via full upload (code %d)", effect, scene.code)
            await self._run_client_command(self._client.set_scene_full(scene.code, scene.param))
        else:
            _LOGGER.debug("Activating scene %s via bare id %s", effect, scene.scene_id)
            await self._run_client_command(self._client.set_scene(scene.scene_id))

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

        # Only touch zone power when turning the fixture on from off, so a
        # brightness/effect-only call while already on doesn't re-toggle zones.
        if was_off and self._zone_indices:
            for zone in self._zone_indices:
                await self._run_client_command(self._client.set_zone(zone, True))

        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        for zone in self._zone_indices:
            await self._run_client_command(self._client.set_zone(zone, False))
        await self.coordinator.async_request_refresh()
