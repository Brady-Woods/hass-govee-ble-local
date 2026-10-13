"""Tests for the Govee BLE Local diagnostic sensors."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from govee_ble_local import Device, GoveeBleConnectionError
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.const import (
    DOMAIN,
    LAST_SEEN_RESOLUTION_SECONDS,
    POLL_INTERVAL_SECONDS,
    UNAVAILABLE_AFTER_FAILURES,
)
from custom_components.govee_ble_local.coordinator import GoveeBleLocalCoordinator

from .conftest import (
    advertisement_callback,
    allow_adverts,
    make_service_info,
    unavailable_callback,
)
from .const import ADDRESS

JITTER_TARGET = "custom_components.govee_ble_local.coordinator.random.uniform"
MAIN_LIGHT = "light.test_ceiling"
SENSOR_KEYS = (
    "rssi",
    "connection_failures",
    "poll_interval",
    "last_seen",
    "last_connected",
    "connection_source",
)
# Sensors that must keep reporting while the device is unreachable.
ALWAYS_AVAILABLE_KEYS = tuple(key for key in SENSOR_KEYS if key != "rssi")


@pytest.fixture(autouse=True)
def no_jitter() -> Any:
    """Deterministic poll intervals."""
    with patch(JITTER_TARGET, return_value=0):
        yield


@pytest.fixture
async def coordinator(setup_integration: MockConfigEntry) -> GoveeBleLocalCoordinator:
    """The loaded entry's coordinator."""
    return setup_integration.runtime_data.coordinator


def sensor_id(hass: HomeAssistant, key: str) -> str:
    """Return the entity id of the diagnostic sensor with this key."""
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_{key}")
    assert entity_id is not None, key
    return entity_id


def sensor_state(hass: HomeAssistant, key: str) -> str:
    """Return the current state string of the diagnostic sensor with this key."""
    state = hass.states.get(sensor_id(hass, key))
    assert state is not None
    return state.state


# -- registration ---------------------------------------------------------------


async def test_all_diagnostic_sensors_are_created(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Each of the six sensors is registered as a DIAGNOSTIC entity of the device."""
    registry = er.async_get(hass)
    sensors = [
        entry
        for entry in er.async_entries_for_config_entry(registry, setup_integration.entry_id)
        if entry.domain == "sensor"
    ]
    assert sorted(entry.unique_id for entry in sensors) == sorted(
        f"{ADDRESS}_{key}" for key in SENSOR_KEYS
    )
    assert all(entry.entity_category is EntityCategory.DIAGNOSTIC for entry in sensors)
    assert all(entry.disabled_by is None for entry in sensors)
    assert len({entry.device_id for entry in sensors}) == 1


@pytest.mark.parametrize(
    ("key", "device_class", "state_class", "unit"),
    [
        ("rssi", SensorDeviceClass.SIGNAL_STRENGTH, SensorStateClass.MEASUREMENT, "dBm"),
        ("connection_failures", None, SensorStateClass.TOTAL_INCREASING, None),
        (
            "poll_interval",
            SensorDeviceClass.DURATION,
            SensorStateClass.MEASUREMENT,
            UnitOfTime.SECONDS,
        ),
        ("last_seen", SensorDeviceClass.TIMESTAMP, None, None),
        ("last_connected", SensorDeviceClass.TIMESTAMP, None, None),
        ("connection_source", None, None, None),
    ],
)
async def test_sensor_classes_and_units(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    *,
    key: str,
    device_class: SensorDeviceClass | None,
    state_class: SensorStateClass | None,
    unit: str | None,
) -> None:
    """Device class, state class and unit match what each value means."""
    # RSSI is unavailable (no attributes) until something has been heard.
    coordinator.rssi = -60
    coordinator.async_update_listeners()
    attributes = hass.states.get(sensor_id(hass, key)).attributes
    assert attributes.get("device_class") == device_class
    assert attributes.get("state_class") == state_class
    assert attributes.get("unit_of_measurement") == unit


# -- values ---------------------------------------------------------------------


async def test_rssi_is_unavailable_until_an_advertisement_arrives(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator
) -> None:
    """With nothing heard yet there is no RSSI to report."""
    assert coordinator.rssi is None
    assert sensor_state(hass, "rssi") == STATE_UNAVAILABLE


async def test_rssi_reports_last_advertisement_at_diagnostics_cadence(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    freezer: Any,
) -> None:
    """The RSSI sensor follows advertisements but is only republished every minute."""
    allow_adverts(mock_device)
    callback = advertisement_callback(mock_bluetooth)
    callback(make_service_info(rssi=-72), BluetoothChange.ADVERTISEMENT)
    assert sensor_state(hass, "rssi") == "-72"

    callback(make_service_info(rssi=-55), BluetoothChange.ADVERTISEMENT)
    assert coordinator.rssi == -55
    assert sensor_state(hass, "rssi") == "-72"  # inside the resolution window

    freezer.tick(timedelta(seconds=LAST_SEEN_RESOLUTION_SECONDS + 1))
    callback(make_service_info(rssi=-48), BluetoothChange.ADVERTISEMENT)
    assert sensor_state(hass, "rssi") == "-48"


async def test_rssi_unavailable_once_no_scanner_hears_the_device(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    *,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
    freezer: Any,
) -> None:
    """The not-connectable unavailable callback clears RSSI so stale values aren't recorded."""
    allow_adverts(mock_device)
    advertisement_callback(mock_bluetooth)(
        make_service_info(rssi=-72), BluetoothChange.ADVERTISEMENT
    )
    assert sensor_state(hass, "rssi") == "-72"

    presence.connectable = presence.any = False
    unavailable_callback(mock_bluetooth, connectable=False)(make_service_info())
    assert coordinator.rssi is None
    assert sensor_state(hass, "rssi") == STATE_UNAVAILABLE

    # It recovers with the first advertisement after the silence.
    freezer.tick(timedelta(minutes=10))
    presence.connectable = presence.any = True
    advertisement_callback(mock_bluetooth)(
        make_service_info(rssi=-66), BluetoothChange.ADVERTISEMENT
    )
    assert sensor_state(hass, "rssi") == "-66"


async def test_connection_failures_count_failed_polls(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """The failure counter follows each failed poll and keeps its total after recovery."""
    assert sensor_state(hass, "connection_failures") == "0"
    mock_device.update.side_effect = GoveeBleConnectionError("no slot")  # type: ignore[attr-defined]
    for expected in ("1", "2", "3"):
        await coordinator.async_refresh()
        assert sensor_state(hass, "connection_failures") == expected

    async def _ok() -> Any:
        mock_device.state.optimistic = False
        return mock_device.state

    mock_device.update.side_effect = _ok  # type: ignore[attr-defined]
    await coordinator.async_refresh()
    assert sensor_state(hass, "connection_failures") == "3"


async def test_skipped_polls_are_not_connection_failures(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator, presence: SimpleNamespace
) -> None:
    """A poll skipped because the device isn't advertising doesn't bump the failure sensor."""
    presence.connectable = presence.any = False
    for _ in range(2):
        await coordinator.async_refresh()
    assert sensor_state(hass, "connection_failures") == "0"


async def test_poll_interval_reflects_backoff(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """The poll-interval sensor shows the current (backed-off) interval in seconds."""
    assert sensor_state(hass, "poll_interval") == str(POLL_INTERVAL_SECONDS)
    mock_device.update.side_effect = GoveeBleConnectionError("no slot")  # type: ignore[attr-defined]
    expected = [POLL_INTERVAL_SECONDS, 240, 480, 960, 1800, 1800]
    for seconds in expected:
        await coordinator.async_refresh()
        assert sensor_state(hass, "poll_interval") == str(seconds)


async def test_poll_interval_is_unknown_when_polling_is_off(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator
) -> None:
    """No update interval (polling disabled) reads as unknown rather than a bogus number."""
    coordinator.update_interval = None
    coordinator.async_update_diagnostics()
    assert sensor_state(hass, "poll_interval") == STATE_UNKNOWN


async def test_last_seen_and_last_connected_timestamps(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    freezer: Any,
) -> None:
    """Last seen follows advertisements, last connected follows read-back polls."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data.coordinator

    connected = dt_util.utcnow().replace(microsecond=0)
    assert sensor_state(hass, "last_seen") == STATE_UNKNOWN  # nothing advertised yet
    assert dt_util.parse_datetime(sensor_state(hass, "last_connected")) == connected

    freezer.tick(timedelta(minutes=5))
    allow_adverts(mock_device)
    advertisement_callback(mock_bluetooth)(make_service_info(), BluetoothChange.ADVERTISEMENT)
    seen = dt_util.utcnow().replace(microsecond=0)
    assert dt_util.parse_datetime(sensor_state(hass, "last_seen")) == seen
    assert dt_util.parse_datetime(sensor_state(hass, "last_connected")) == connected

    freezer.tick(timedelta(minutes=5))
    await coordinator.async_refresh()
    assert dt_util.parse_datetime(sensor_state(hass, "last_connected")) == dt_util.utcnow().replace(
        microsecond=0
    )
    assert dt_util.parse_datetime(sensor_state(hass, "last_seen")) == seen


async def test_last_connected_stays_unknown_for_optimistic_devices(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """A device that never reads back real state never reports a last-connected time."""

    async def _optimistic() -> Any:
        mock_device.state.optimistic = True
        return mock_device.state

    mock_device.update.side_effect = _optimistic  # type: ignore[attr-defined]
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert sensor_state(hass, "last_connected") == STATE_UNKNOWN


async def test_connection_source_follows_the_reporting_scanner(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """The source shows which adapter/proxy last heard the device; unknown if none did."""
    assert sensor_state(hass, "connection_source") == "local"

    presence.service_info = make_service_info(source="esphome-proxy-1")
    allow_adverts(mock_device)
    advertisement_callback(mock_bluetooth)(presence.service_info, BluetoothChange.ADVERTISEMENT)
    assert sensor_state(hass, "connection_source") == "esphome-proxy-1"

    presence.service_info = None
    coordinator.async_update_diagnostics()
    assert sensor_state(hass, "connection_source") == STATE_UNKNOWN


# -- availability ---------------------------------------------------------------


async def test_diagnostics_stay_available_while_the_device_is_unavailable(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """When the device drops out the diagnostics keep reporting; only the RSSI goes away."""
    allow_adverts(mock_device)
    advertisement_callback(mock_bluetooth)(
        make_service_info(rssi=-72), BluetoothChange.ADVERTISEMENT
    )

    presence.connectable = presence.any = False
    for _ in range(UNAVAILABLE_AFTER_FAILURES):
        await coordinator.async_refresh()

    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE
    for key in SENSOR_KEYS:
        assert sensor_state(hass, key) != STATE_UNAVAILABLE, key
    assert sensor_state(hass, "poll_interval") == str(POLL_INTERVAL_SECONDS * 4)

    # Once nothing at all hears it the RSSI goes unavailable, the rest still report.
    unavailable_callback(mock_bluetooth, connectable=False)(make_service_info())
    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE
    assert sensor_state(hass, "rssi") == STATE_UNAVAILABLE
    for key in ALWAYS_AVAILABLE_KEYS:
        assert sensor_state(hass, key) != STATE_UNAVAILABLE, key
