"""Switch platform for Govee BLE per-zone control (profile-driven)."""
from __future__ import annotations

import logging
from typing import Any

from govee_ble_local import GoveeBleClient, ZONE_UPPER
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GoveeH60A6ConfigEntry
from .const import ZONE_META
from .coordinator import GoveeH60A6Coordinator
from .entity import GoveeH60A6Entity

_LOGGER = logging.getLogger(__name__)

# See light.py: all BLE work serializes through one connection; never run
# entity commands for this integration concurrently.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeH60A6ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one switch per physical zone the profile declares."""
    data = entry.runtime_data
    address: str = entry.data["address"]
    entities = []
    for zone_name in data.profile.capabilities.zones:
        meta = ZONE_META.get(zone_name)
        if meta is None:
            _LOGGER.warning("No mapping for zone %r; skipping", zone_name)
            continue
        zone_index, translation_key = meta
        entities.append(
            GoveeH60A6ZoneSwitch(
                data.coordinator,
                data.client,
                address,
                entry.title,
                data.profile.name,
                zone_index,
                translation_key,
                data.serial_number,
            )
        )
    async_add_entities(entities)


class GoveeH60A6ZoneSwitch(GoveeH60A6Entity, SwitchEntity):
    """On/off control for a single zone (e.g. upper ring or lower panel)."""

    def __init__(
        self,
        coordinator: GoveeH60A6Coordinator,
        client: GoveeBleClient,
        address: str,
        device_name: str,
        model: str,
        zone: int,
        translation_key: str,
        serial_number: str | None = None,
    ) -> None:
        super().__init__(coordinator, address, device_name, model, serial_number)
        self._client = client
        self._zone = zone
        self._attr_unique_id = f"{address}_zone_{zone}"
        self._attr_translation_key = translation_key

    @property
    def is_on(self) -> bool | None:
        status = self.coordinator.data
        return status.zone_upper_on if self._zone == ZONE_UPPER else status.zone_lower_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        _LOGGER.debug("Turning zone %d on for %s", self._zone, self._address)
        await self._run_client_command(self._client.set_zone(self._zone, True))
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.debug("Turning zone %d off for %s", self._zone, self._address)
        await self._run_client_command(self._client.set_zone(self._zone, False))
        await self.coordinator.async_request_refresh()
