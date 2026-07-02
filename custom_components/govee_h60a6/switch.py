"""Switch platform for the Govee H60A6 zone (upper ring / lower panel) control."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import GoveeH60A6Client
from .const import DOMAIN, ZONE_LOWER, ZONE_UPPER
from .coordinator import GoveeH60A6Coordinator
from .entity import GoveeH60A6Entity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    address = entry.data["address"]
    async_add_entities(
        [
            GoveeH60A6ZoneSwitch(
                data["coordinator"],
                data["client"],
                address,
                entry.title,
                ZONE_UPPER,
                "Upper Ring",
                "mdi:wall-sconce-flat-variant",
            ),
            GoveeH60A6ZoneSwitch(
                data["coordinator"],
                data["client"],
                address,
                entry.title,
                ZONE_LOWER,
                "Lower Panel",
                "mdi:wall-sconce-flat",
            ),
        ]
    )


class GoveeH60A6ZoneSwitch(GoveeH60A6Entity, SwitchEntity):
    """On/off control for a single zone (upper ring or lower panel)."""

    def __init__(
        self,
        coordinator: GoveeH60A6Coordinator,
        client: GoveeH60A6Client,
        address: str,
        device_name: str,
        zone: int,
        zone_name: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator, address, device_name)
        self._client = client
        self._zone = zone
        self._attr_unique_id = f"{address}_zone_{zone}"
        self._attr_name = zone_name
        self._attr_icon = icon
        # Zone status readback is known-unreliable whenever the two zones
        # differ (PROTOCOL.md 5.2) - confirmed via live testing to happen
        # during ordinary toggling, not just "scene mode" as originally
        # believed. Tracked optimistically from the last command sent
        # until the real byte encoding is solved; None until this session
        # has issued a command, so a fresh start still shows the
        # coordinator's best-effort polled guess rather than nothing.
        self._optimistic_is_on: bool | None = None

    @property
    def is_on(self) -> bool | None:
        if self._optimistic_is_on is not None:
            return self._optimistic_is_on
        status = self.coordinator.data
        if status is None:
            return None
        return status.zone_upper_on if self._zone == ZONE_UPPER else status.zone_lower_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        _LOGGER.debug("Turning zone %d on", self._zone)
        await self._run_client_command(self._client.set_zone(self._zone, True))
        self._optimistic_is_on = True
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.debug("Turning zone %d off", self._zone)
        await self._run_client_command(self._client.set_zone(self._zone, False))
        self._optimistic_is_on = False
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
