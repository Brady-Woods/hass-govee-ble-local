"""Diagnostic sensors for a Govee BLE device.

These are read-only diagnostics (no BLE I/O of their own) that read values the
coordinator already tracks: advertisement RSSI, the cumulative connection-failure
count, and the current (adaptive-backoff) poll interval. They attach to the same
device as the light/switch entities and are graphable / kept as long-term
statistics. All stay available even when the device can't connect, so you can
graph exactly when a device is struggling.
"""
from __future__ import annotations

from datetime import datetime

from govee_ble_local import Device
from homeassistant.components import bluetooth
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
            GoveeBleLocalLastSeenSensor(coordinator, device, address, entry.title),
            GoveeBleLocalLastConnectedSensor(coordinator, device, address, entry.title),
            GoveeBleLocalConnectionSourceSensor(coordinator, device, address, entry.title),
        ]
    )


class _DiagnosticSensor(GoveeBleLocalEntity, SensorEntity):
    """Base for the always-available diagnostic sensors."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: Device,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, address, device_name, device.sku)

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
        device: Device,
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
        device: Device,
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
        device: Device,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, device, address, device_name)
        self._attr_unique_id = f"{address}_poll_interval"

    @property
    def native_value(self) -> int | None:
        interval = self.coordinator.update_interval
        return int(interval.total_seconds()) if interval else None


class GoveeBleLocalLastSeenSensor(_DiagnosticSensor):
    """When the device's advertisement was last seen (passive reachability)."""

    _attr_translation_key = "last_seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: Device,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, device, address, device_name)
        self._attr_unique_id = f"{address}_last_seen"

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_seen


class GoveeBleLocalLastConnectedSensor(_DiagnosticSensor):
    """When the device was last successfully connected to (control path)."""

    _attr_translation_key = "last_connected"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: Device,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, device, address, device_name)
        self._attr_unique_id = f"{address}_last_connected"

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_connected


class GoveeBleLocalConnectionSourceSensor(_DiagnosticSensor):
    """Which Bluetooth source (local adapter vs. a named ESPHome proxy) last saw
    this device's advertisement. Answers "is this actually reaching the proxy I
    expect, or silently falling back to the local adapter" without digging
    through raw logs - the local adapter being overloaded and proxies going
    offline are both things this integration otherwise has no visibility into."""

    _attr_translation_key = "connection_source"

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: Device,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, device, address, device_name)
        self._address = address
        self._attr_unique_id = f"{address}_connection_source"

    @property
    def native_value(self) -> str | None:
        try:
            info = bluetooth.async_last_service_info(
                self.hass, self._address, connectable=True
            )
        except RuntimeError:
            # Bluetooth manager not set up (isolated unit tests only).
            return None
        return info.source if info is not None else None
