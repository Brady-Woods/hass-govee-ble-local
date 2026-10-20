"""Connectivity diagnostic for a Govee BLE device.

Reports whether the device is currently *reachable* — i.e. whether any Bluetooth
scanner (connectable or not) still sees its advertisements. That is the honest
signal for a device we mostly track passively: the periodic connect-poll is
throttled and expected to fail sometimes, so it would flap here, while
advertisements keep flowing. It stays available itself so it can report the
"disconnected" state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import GoveeBleLocalDiagnosticEntity

if TYPE_CHECKING:
    from . import GoveeBleLocalConfigEntry

# Read-only; no BLE work.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeBleLocalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the connectivity binary sensor."""
    async_add_entities([GoveeBleLocalConnectivityBinarySensor(entry)])


class GoveeBleLocalConnectivityBinarySensor(GoveeBleLocalDiagnosticEntity, BinarySensorEntity):
    """On when the device is currently advertising (reachable over BLE)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, entry: GoveeBleLocalConfigEntry) -> None:
        """Initialize the connectivity sensor."""
        super().__init__(entry, "connectivity")

    @property
    def is_on(self) -> bool:
        """Return presence from the Bluetooth stack, NOT the connect-poll."""
        return self.coordinator.async_address_present(connectable=False)
