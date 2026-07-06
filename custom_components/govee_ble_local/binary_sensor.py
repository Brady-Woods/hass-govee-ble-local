"""Connectivity diagnostic for a Govee BLE device.

Reports whether the last poll reached the device, giving an availability/uptime
timeline that can be graphed and alerted on. It stays available itself so it can
report the "disconnected" state (unlike the control entities, which go
unavailable when the coordinator can't reach the device).
"""
from __future__ import annotations

from govee_ble_local import GoveeDevice
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GoveeBleLocalConfigEntry
from .coordinator import GoveeBleLocalCoordinator
from .entity import GoveeBleLocalEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeBleLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the connectivity binary sensor."""
    coordinator = entry.runtime_data.coordinator
    device = entry.runtime_data.device
    address: str = entry.data["address"]
    async_add_entities(
        [GoveeBleLocalConnectivityBinarySensor(coordinator, device, address, entry.title)]
    )


class GoveeBleLocalConnectivityBinarySensor(GoveeBleLocalEntity, BinarySensorEntity):
    """On when the last poll reached the device."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: GoveeDevice,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, address, device_name, device.model)
        self._attr_unique_id = f"{address}_connectivity"

    @property
    def available(self) -> bool:
        # Must stay available to report the down state.
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success
