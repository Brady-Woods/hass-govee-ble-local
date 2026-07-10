"""Connectivity diagnostic for a Govee BLE device.

Reports whether the device is currently *reachable* — i.e. whether the Bluetooth
stack is still seeing its advertisements. That is the honest signal for a device
we mostly track passively: the periodic connect-poll is throttled and expected
to fail, so it would flap here, while advertisements keep flowing. It stays
available itself so it can report the "disconnected" state.
"""
from __future__ import annotations

from govee_ble_local import Device
from homeassistant.components import bluetooth
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
    """On when the device is currently advertising (reachable over BLE)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: Device,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, address, device_name, device.sku)
        self._attr_unique_id = f"{address}_connectivity"

    @property
    def available(self) -> bool:
        # Must stay available to report the down state.
        return True

    @property
    def is_on(self) -> bool:
        # Presence from the Bluetooth stack (advertisement-based), NOT the
        # connect-poll: the device is "connected" while we can still see it.
        try:
            return bluetooth.async_address_present(
                self.hass, self._address, connectable=False
            )
        except RuntimeError:
            # Bluetooth manager not set up (isolated unit tests only).
            return False
