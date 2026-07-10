"""Switch platform for Govee BLE per-zone control and whole-device power
(capability-driven)."""
from __future__ import annotations

import logging
from typing import Any

from govee_ble_local import Capability, Device
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GoveeBleLocalConfigEntry
from .const import ZONE_TRANSLATION_KEYS
from .coordinator import GoveeBleLocalCoordinator
from .entity import GoveeBleLocalEntity

_LOGGER = logging.getLogger(__name__)

# See light.py: all BLE work serializes through one connection; never run
# entity commands for this integration concurrently.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeBleLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up an on/off switch per on/off-only zone (a zone with no segments;
    colour-controllable zones are lights instead - see light.py), or - for a
    device with no light capability and no zones (e.g. a smart plug) - a single
    whole-device power switch."""
    device = entry.runtime_data.device
    coordinator = entry.runtime_data.coordinator
    address: str = entry.data["address"]
    caps = device.capabilities

    entities: list[GoveeBleLocalZoneSwitch | GoveeBleLocalPowerSwitch] = [
        GoveeBleLocalZoneSwitch(coordinator, device, address, entry.title, zone.name)
        for zone in device.zones
        if not zone.segments
    ]

    is_light = bool(caps & {Capability.BRIGHTNESS, Capability.RGB, Capability.COLOR_TEMP})
    if not device.zones and not is_light and Capability.POWER in caps:
        entities.append(GoveeBleLocalPowerSwitch(coordinator, device, address, entry.title))

    async_add_entities(entities)


class GoveeBleLocalZoneSwitch(GoveeBleLocalEntity, SwitchEntity):
    """On/off control for a single named zone (e.g. H60A6 ring or panel)."""

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
        self._attr_unique_id = f"{address}_zone_{zone_name}"
        self._attr_translation_key = ZONE_TRANSLATION_KEYS.get(zone_name, zone_name)

    @property
    def is_on(self) -> bool | None:
        # Read the zone's actual power state back from the device (polled);
        # set_zone_power updates it optimistically for instant feedback.
        return self._device.zone_is_on(self._zone_name)

    async def async_turn_on(self, **kwargs: Any) -> None:
        _LOGGER.debug("Turning zone %s on for %s", self._zone_name, self._address)
        await self._run_client_command(self._device.set_zone_power(self._zone_name, True))
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.debug("Turning zone %s off for %s", self._zone_name, self._address)
        await self._run_client_command(self._device.set_zone_power(self._zone_name, False))
        self.async_write_ha_state()


class GoveeBleLocalPowerSwitch(GoveeBleLocalEntity, SwitchEntity):
    """Whole-device on/off for a device that is fundamentally just a switch
    (e.g. Govee's smart-plug family). State is read back over BLE (the plug
    profile polls its relay state) and updated optimistically on each command."""

    _attr_name = None  # the only entity for this device - use the device's own name
    _attr_device_class = SwitchDeviceClass.OUTLET

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: Device,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, address, device_name, device.sku)
        self._device = device
        self._attr_unique_id = f"{address}_power"

    @property
    def is_on(self) -> bool | None:
        # The plug polls its relay state back into DeviceState (coordinator.data);
        # set_power also updates it optimistically for instant feedback.
        data = self.coordinator.data
        return data.is_on if data else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        _LOGGER.debug("Turning power on for %s", self._address)
        await self._run_client_command(self._device.set_power(True))
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.debug("Turning power off for %s", self._address)
        await self._run_client_command(self._device.set_power(False))
        self.async_write_ha_state()
