"""Tests for the Govee BLE Local diagnostic sensors."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from govee_ble_local import Device, GoveeBleAuthError, GoveeBleConnectionError
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
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.const import (
    CONF_SKU,
    DOMAIN,
    LAST_SEEN_RESOLUTION_SECONDS,
    POLL_INTERVAL_SECONDS,
    UNAVAILABLE_AFTER_FAILURES,
)
from custom_components.govee_ble_local.coordinator import GoveeBleLocalCoordinator

from .conftest import (
    advance,
    advertisement_callback,
    allow_adverts,
    make_service_info,
    unavailable_callback,
)
from .const import ADDRESS, TITLE

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
# Sensors most users never need, which would otherwise churn the recorder.
DISABLED_BY_DEFAULT_KEYS = ("rssi", "poll_interval", "last_seen", "last_connected")


@pytest.fixture(autouse=True)
def no_jitter() -> Any:
    """Deterministic poll intervals."""
    with patch(JITTER_TARGET, return_value=0):
        yield


@pytest.fixture
async def coordinator(
    entity_registry_enabled_by_default: None, setup_integration: MockConfigEntry
) -> GoveeBleLocalCoordinator:
    """The loaded entry's coordinator, with every sensor enabled."""
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
    """All six sensors are DIAGNOSTIC entities of the device; the noisy ones start disabled."""
    registry = er.async_get(hass)
    sensors = {
        entry.unique_id.removeprefix(f"{ADDRESS}_"): entry
        for entry in er.async_entries_for_config_entry(registry, setup_integration.entry_id)
        if entry.domain == "sensor"
    }
    assert sorted(sensors) == sorted(SENSOR_KEYS)
    assert all(entry.entity_category is EntityCategory.DIAGNOSTIC for entry in sensors.values())
    assert {key: entry.disabled_by for key, entry in sensors.items()} == {
        key: RegistryEntryDisabler.INTEGRATION if key in DISABLED_BY_DEFAULT_KEYS else None
        for key in SENSOR_KEYS
    }
    assert len({entry.device_id for entry in sensors.values()}) == 1


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


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_rssi_is_unavailable_while_nothing_has_been_heard(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """With no advertisement in HA's history there is no RSSI (or last seen) to report."""
    presence.service_info = None
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_config_entry.runtime_data.coordinator.rssi is None
    assert sensor_state(hass, "rssi") == STATE_UNAVAILABLE
    assert sensor_state(hass, "last_seen") == STATE_UNKNOWN


async def test_rssi_follows_the_history_at_the_refresh_cadence(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """RSSI comes from HA's advertisement history, republished once per resolution period."""
    assert sensor_state(hass, "rssi") == "-60"  # the history entry at setup

    presence.service_info = make_service_info(rssi=-72)
    # An advertisement callback doesn't carry RSSI (it fires only on content changes).
    allow_adverts(mock_device)
    advertisement_callback(mock_bluetooth)(presence.service_info, BluetoothChange.ADVERTISEMENT)
    assert sensor_state(hass, "rssi") == "-60"

    await advance(hass, LAST_SEEN_RESOLUTION_SECONDS)
    assert sensor_state(hass, "rssi") == "-72"

    presence.service_info = make_service_info(rssi=-48)
    await advance(hass, LAST_SEEN_RESOLUTION_SECONDS)
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
    assert sensor_state(hass, "rssi") == "-60"

    presence.connectable = presence.any = False
    unavailable_callback(mock_bluetooth, connectable=False)(make_service_info())
    assert coordinator.rssi is None
    assert sensor_state(hass, "rssi") == STATE_UNAVAILABLE
    # A refresh finding nothing new doesn't bring a stale value back.
    await advance(hass, LAST_SEEN_RESOLUTION_SECONDS)
    assert sensor_state(hass, "rssi") == STATE_UNAVAILABLE

    # It recovers with the first advertisement after the silence, not a minute later.
    freezer.tick(timedelta(minutes=10))
    presence.connectable = presence.any = True
    allow_adverts(mock_device)
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

    async def _ok(**_kwargs: Any) -> Any:
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


async def test_poll_interval_ignores_the_jitter(
    hass: HomeAssistant, coordinator: GoveeBleLocalCoordinator
) -> None:
    """Jitter spreads the polls out but doesn't write a new sensor state on every poll."""
    before = hass.states.get(sensor_id(hass, "poll_interval"))
    scheduled = []
    with patch(JITTER_TARGET, side_effect=[5.0, 17.0, 29.0]):
        for _ in range(3):
            await coordinator.async_refresh()
            assert coordinator.update_interval is not None
            scheduled.append(coordinator.update_interval.total_seconds())
            assert sensor_state(hass, "poll_interval") == str(POLL_INTERVAL_SECONDS)
    assert scheduled == [POLL_INTERVAL_SECONDS + j for j in (5, 17, 29)]
    assert hass.states.get(sensor_id(hass, "poll_interval")).last_changed == before.last_changed


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "mock_device", "mock_bluetooth")
async def test_poll_interval_is_unknown_with_polling_disabled(hass: HomeAssistant) -> None:
    """Polling turned off in the entry's system options reads as unknown, not 120 s."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=ADDRESS,
        data={"address": ADDRESS, CONF_SKU: "H60A6"},
        pref_disable_polling=True,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert sensor_state(hass, "poll_interval") == STATE_UNKNOWN


async def test_poll_interval_is_unknown_while_a_rejected_secret_halts_polling(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_device: Device,
) -> None:
    """Also right after failed polls (HA pushes nothing then); a poll that runs restores it."""
    mock_device.update.side_effect = GoveeBleConnectionError("no slot")  # type: ignore[attr-defined]
    for _ in range(2):
        await coordinator.async_refresh()
    assert sensor_state(hass, "poll_interval") == str(2 * POLL_INTERVAL_SECONDS)

    mock_device.update.side_effect = GoveeBleAuthError("secret rejected")  # type: ignore[attr-defined]
    await coordinator.async_refresh()
    assert sensor_state(hass, "poll_interval") == STATE_UNKNOWN

    async def _ok(**_kwargs: Any) -> Any:
        return mock_device.state

    mock_device.update.side_effect = _ok  # type: ignore[attr-defined]
    await coordinator.async_refresh()
    assert sensor_state(hass, "poll_interval") == str(POLL_INTERVAL_SECONDS)


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "mock_device", "mock_bluetooth")
async def test_last_seen_and_last_connected_timestamps(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    mock_config_entry: MockConfigEntry,
    presence: SimpleNamespace,
    freezer: Any,
) -> None:
    """Last seen follows the advertisement history (by its age), last connected the polls."""
    # At startup HA's history already holds an advertisement from five minutes ago.
    presence.service_info = make_service_info(seen_at=monotonic.now - 300)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = mock_config_entry.runtime_data.coordinator

    connected = dt_util.utcnow().replace(microsecond=0)
    seen = connected - timedelta(minutes=5)
    assert dt_util.parse_datetime(sensor_state(hass, "last_seen")) == seen
    assert dt_util.parse_datetime(sensor_state(hass, "last_connected")) == connected

    # Half a minute on, the next refresh (and no poll yet) picks up a newer one.
    freezer.tick(timedelta(seconds=30))
    monotonic.now += 30
    presence.service_info = make_service_info(seen_at=monotonic.now - 2)
    await advance(hass, LAST_SEEN_RESOLUTION_SECONDS)
    seen = dt_util.utcnow().replace(microsecond=0) - timedelta(seconds=2)
    assert dt_util.parse_datetime(sensor_state(hass, "last_seen")) == seen
    assert dt_util.parse_datetime(sensor_state(hass, "last_connected")) == connected

    freezer.tick(timedelta(minutes=5))
    await coordinator.async_refresh()
    assert dt_util.parse_datetime(sensor_state(hass, "last_connected")) == dt_util.utcnow().replace(
        microsecond=0
    )
    assert dt_util.parse_datetime(sensor_state(hass, "last_seen")) == seen


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_last_connected_stays_unknown_for_optimistic_devices(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """A device that never reads back real state never reports a last-connected time."""

    async def _optimistic(**_kwargs: Any) -> Any:
        mock_device.state.optimistic = True
        return mock_device.state

    mock_device.update.side_effect = _optimistic  # type: ignore[attr-defined]
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert sensor_state(hass, "last_connected") == STATE_UNKNOWN


async def test_connection_source_follows_the_preferred_scanner(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    presence: SimpleNamespace,
) -> None:
    """The source shows the connectable adapter/proxy HA prefers; unknown if there is none."""
    assert sensor_state(hass, "connection_source") == "local"

    presence.service_info = make_service_info(source="esphome-proxy-1")
    await advance(hass, LAST_SEEN_RESOLUTION_SECONDS)
    assert sensor_state(hass, "connection_source") == "esphome-proxy-1"

    presence.service_info = None
    coordinator.async_update_diagnostics()
    assert sensor_state(hass, "connection_source") == STATE_UNKNOWN


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_diagnostic_listeners_are_released_with_their_entities(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Removing an entity, or unloading the entry, drops its diagnostic subscription."""
    coordinator = setup_integration.runtime_data.coordinator
    assert len(coordinator._diagnostic_listeners) == len(SENSOR_KEYS) + 1  # + connectivity
    assert coordinator._state_listeners  # the device-registry sync, the segment count's

    # An entity removed while the entry stays loaded lets go of its listener.
    er.async_get(hass).async_remove(sensor_id(hass, "rssi"))
    await hass.async_block_till_done()
    assert len(coordinator._diagnostic_listeners) == len(SENSOR_KEYS)

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    assert coordinator._diagnostic_listeners == []
    assert coordinator._state_listeners == []


# -- availability ---------------------------------------------------------------


async def test_diagnostics_stay_available_while_the_device_is_unavailable(
    hass: HomeAssistant,
    coordinator: GoveeBleLocalCoordinator,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """When the device drops out the diagnostics keep reporting; only the RSSI goes away."""
    assert sensor_state(hass, "rssi") == "-60"

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
