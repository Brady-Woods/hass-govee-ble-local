"""Diagnostic sensors for a Govee BLE device.

These are read-only diagnostics (no BLE I/O of their own) that read values the
coordinator already tracks: advertisement RSSI, the cumulative connection-failure
count, the current (adaptive-backoff) poll interval, last seen / last connected,
and which Bluetooth source last heard the device. They attach to the same device
as the light/switch entities. All except RSSI stay available even when the
device can't be reached (that's the interesting case); RSSI is unavailable while
no scanner hears the device, so stale values don't pollute statistics.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import GoveeBleLocalCoordinator
from .entity import GoveeBleLocalEntity

if TYPE_CHECKING:
    from . import GoveeBleLocalConfigEntry

# Read-only; no BLE work, so no need to serialize with the command platforms.
PARALLEL_UPDATES = 0


def _connection_source(coordinator: GoveeBleLocalCoordinator) -> str | None:
    info = coordinator.last_service_info()
    return info.source if info is not None else None


def _poll_interval(coordinator: GoveeBleLocalCoordinator) -> int | None:
    interval = coordinator.update_interval
    return int(interval.total_seconds()) if interval is not None else None


@dataclass(frozen=True, kw_only=True)
class GoveeBleLocalSensorEntityDescription(SensorEntityDescription):
    """Describes a coordinator-backed diagnostic sensor."""

    value_fn: Callable[[GoveeBleLocalCoordinator], int | str | datetime | None]
    available_fn: Callable[[GoveeBleLocalCoordinator], bool] = lambda _coordinator: True


SENSORS: tuple[GoveeBleLocalSensorEntityDescription, ...] = (
    GoveeBleLocalSensorEntityDescription(
        key="rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        value_fn=lambda coordinator: coordinator.rssi,
        available_fn=lambda coordinator: coordinator.rssi is not None,
    ),
    GoveeBleLocalSensorEntityDescription(
        key="connection_failures",
        translation_key="connection_failures",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda coordinator: coordinator.total_failures,
    ),
    GoveeBleLocalSensorEntityDescription(
        key="poll_interval",
        translation_key="poll_interval",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        value_fn=_poll_interval,
    ),
    GoveeBleLocalSensorEntityDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda coordinator: coordinator.last_seen,
    ),
    GoveeBleLocalSensorEntityDescription(
        key="last_connected",
        translation_key="last_connected",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda coordinator: coordinator.last_connected,
    ),
    GoveeBleLocalSensorEntityDescription(
        key="connection_source",
        translation_key="connection_source",
        value_fn=_connection_source,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeBleLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the per-device diagnostic sensors."""
    async_add_entities(GoveeBleLocalDiagnosticSensor(entry, description) for description in SENSORS)


class GoveeBleLocalDiagnosticSensor(GoveeBleLocalEntity, SensorEntity):
    """A diagnostic sensor reading a value the coordinator tracks."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description: GoveeBleLocalSensorEntityDescription

    def __init__(
        self,
        entry: GoveeBleLocalConfigEntry,
        description: GoveeBleLocalSensorEntityDescription,
    ) -> None:
        """Initialize the sensor from its description."""
        super().__init__(entry, description.key)
        self.entity_description = description

    async def async_added_to_hass(self) -> None:
        """Also follow the coordinator's diagnostic-only updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_diagnostic_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Report even when the device is unreachable (that's the interesting case)."""
        return self.entity_description.available_fn(self.coordinator)

    @property
    def native_value(self) -> int | str | datetime | None:
        """Return the tracked value."""
        return self.entity_description.value_fn(self.coordinator)
