"""Diagnostic sensors for a Govee BLE device.

These are read-only diagnostics (no BLE I/O of their own) that read values the
coordinator already tracks: advertisement RSSI, the cumulative connection-failure
count, and the current (adaptive-backoff) poll interval. They attach to the same
device as the light/switch entities and are graphable / kept as long-term
statistics. All stay available even when the device can't connect, so you can
graph exactly when a device is struggling.
"""
from __future__ import annotations

from govee_ble_local import GoveeDevice
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GoveeBleLocalConfigEntry
from .coordinator import GoveeBleLocalCoordinator
from .entity import GoveeBleLocalEntity

# Read-only; no BLE work, so no need to serialize with the command platforms.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeBleLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the per-device diagnostic sensors."""
    coordinator = entry.runtime_data.coordinator
    device = entry.runtime_data.device
    address: str = entry.data["address"]
    async_add_entities(
        [
            GoveeBleLocalRssiSensor(coordinator, device, address, entry.title),
            GoveeBleLocalConnectionFailuresSensor(coordinator, device, address, entry.title),
            GoveeBleLocalPollIntervalSensor(coordinator, device, address, entry.title),
        ]
    )


class _DiagnosticSensor(GoveeBleLocalEntity, SensorEntity):
    """Base for the always-available diagnostic sensors."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: GoveeDevice,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, address, device_name, device.model)

    @property
    def available(self) -> bool:
        # Diagnostics describe the connection itself, so they must report even
        # when the device is unreachable (that's the interesting case).
        return True


class GoveeBleLocalRssiSensor(_DiagnosticSensor):
    """BLE advertisement signal strength (dBm)."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: GoveeDevice,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, device, address, device_name)
        self._attr_unique_id = f"{address}_rssi"

    @property
    def available(self) -> bool:
        return self.coordinator.rssi is not None

    @property
    def native_value(self) -> int | None:
        return self.coordinator.rssi


class GoveeBleLocalConnectionFailuresSensor(_DiagnosticSensor):
    """Cumulative count of failed polls (connect/read failures) since load."""

    _attr_translation_key = "connection_failures"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: GoveeDevice,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, device, address, device_name)
        self._attr_unique_id = f"{address}_connection_failures"

    @property
    def native_value(self) -> int:
        return self.coordinator.total_failures


class GoveeBleLocalPollIntervalSensor(_DiagnosticSensor):
    """Current poll interval in seconds (grows with the adaptive backoff)."""

    _attr_translation_key = "poll_interval"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: GoveeDevice,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, device, address, device_name)
        self._attr_unique_id = f"{address}_poll_interval"

    @property
    def native_value(self) -> int | None:
        interval = self.coordinator.update_interval
        return int(interval.total_seconds()) if interval else None
