"""Switch platform for Govee BLE per-zone control and whole-device power."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import GoveeBleLocalEntity
from .helpers import has_power_switch, has_zone_lights

if TYPE_CHECKING:
    from . import GoveeBleLocalConfigEntry

_LOGGER = logging.getLogger(__name__)

# See light.py: commands for one device are serialized per platform here and
# across platforms by the library's per-device connection lock.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeBleLocalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switches.

    One on/off switch per zone of a device without a fixture light (a light
    fixture's zones are lights, on/off ones too - see light.py), or - for a
    relay plug with no zones (the H5083 family) - a single whole-device power
    switch. A power-only light is a light instead (see helpers.py).
    """
    device = entry.runtime_data.device

    entities: list[SwitchEntity] = []
    if not has_zone_lights(device):
        entities.extend(GoveeBleLocalZoneSwitch(entry, zone.name) for zone in device.zones)
    if has_power_switch(device):
        entities.append(GoveeBleLocalPowerSwitch(entry))
    async_add_entities(entities)


class GoveeBleLocalZoneSwitch(GoveeBleLocalEntity, SwitchEntity):
    """On/off control for one zone of a device without a fixture light (helpers.has_zone_lights)."""

    def __init__(self, entry: GoveeBleLocalConfigEntry, zone_name: str) -> None:
        """Initialize the switch for the named zone."""
        super().__init__(entry, f"zone_{zone_name}")
        self._zone_name = zone_name
        self._set_zone_name(zone_name)

    @property
    def is_on(self) -> bool | None:
        """Return the zone's polled power (off whenever the fixture is known off)."""
        if self.coordinator.data.is_on is False:
            return False
        return self._device.zone_is_on(self._zone_name)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Power the zone on."""
        _LOGGER.debug("Turning zone %s on for %s", self._zone_name, self._address)
        async with self._command():
            await self._run_client_command(self._device.set_zone_power(self._zone_name, True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Power the zone off, unless the device has just confirmed it off.

        See ``GoveeBleLocalEntity._async_turn_zone_off``: nothing is sent right
        after the fixture's acked power-off, and a zone-off of a fixture already
        off lets the other devices' commands go first.
        This platform doesn't wait for the light platform's commands, so a
        fixture turn-on still in flight is a real case here: the zone-off goes
        out after it.
        """
        await self._async_turn_zone_off(self._zone_name)


class GoveeBleLocalPowerSwitch(GoveeBleLocalEntity, SwitchEntity):
    """Whole-device on/off for a device that is fundamentally just a switch.

    E.g. Govee's smart-plug family. State is read back over BLE (the plug
    profile polls its relay state) and updated optimistically on each command.
    """

    _attr_name = None  # the only entity for this device - use the device's own name
    _attr_device_class = SwitchDeviceClass.OUTLET

    def __init__(self, entry: GoveeBleLocalConfigEntry) -> None:
        """Initialize the power switch."""
        super().__init__(entry, "power")

    @property
    def is_on(self) -> bool | None:
        """Return the relay state (None while unknown)."""
        return self.coordinator.data.is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch the relay on."""
        _LOGGER.debug("Turning power on for %s", self._address)
        async with self._command():
            await self._run_client_command(self._device.set_power(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch the relay off; one already off lets other devices go first."""
        _LOGGER.debug("Turning power off for %s", self._address)
        await self._async_turn_device_off(lambda: self._device.set_power(False))
