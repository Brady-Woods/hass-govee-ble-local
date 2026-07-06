"""Tests for the Govee BLE Local diagnostic sensors."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from bleak.exc import BleakError
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.const import DOMAIN, POLL_INTERVAL_SECONDS

from .const import ADDRESS

FAILURES = f"{ADDRESS}_connection_failures"
POLL = f"{ADDRESS}_poll_interval"


async def test_diagnostic_sensors_created(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """All three diagnostic sensors are registered as DIAGNOSTIC entities."""
    registry = er.async_get(hass)
    for suffix in ("rssi", "connection_failures", "poll_interval"):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_{suffix}")
        assert entity_id is not None, suffix
        entry = registry.async_get(entity_id)
        assert entry is not None
        assert entry.entity_category is EntityCategory.DIAGNOSTIC


async def test_rssi_reports_last_advertisement(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """RSSI reflects the value the coordinator sampled from the advertisement."""
    coordinator = setup_integration.runtime_data.coordinator
    registry = er.async_get(hass)
    rssi_id = registry.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_rssi")
    assert rssi_id is not None

    # conftest's mock_bluetooth advertises rssi=-60; the first refresh sampled it.
    assert coordinator.rssi == -60
    state = hass.states.get(rssi_id)
    assert state is not None
    assert state.state == "-60"
    assert state.attributes["state_class"] == SensorStateClass.MEASUREMENT
    assert state.attributes["device_class"] == "signal_strength"
    assert state.attributes["unit_of_measurement"] == "dBm"


async def test_failures_and_poll_interval_track_coordinator(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: AsyncMock
) -> None:
    """Failed polls increment the failures sensor and grow the poll-interval
    sensor via the backoff; both stay available while the device is unreachable."""
    coordinator = setup_integration.runtime_data.coordinator
    registry = er.async_get(hass)
    fail_id = registry.async_get_entity_id("sensor", DOMAIN, FAILURES)
    poll_id = registry.async_get_entity_id("sensor", DOMAIN, POLL)
    assert fail_id is not None and poll_id is not None

    assert hass.states.get(fail_id).state == "0"
    assert hass.states.get(poll_id).state == str(POLL_INTERVAL_SECONDS)

    mock_device.update.side_effect = BleakError("no slot")
    # A few failed polls: failures climb; interval backs off past the grace poll.
    for _ in range(3):
        await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.total_failures == 3
    assert hass.states.get(fail_id).state == "3"
    # Still available (diagnostics report during outages) and interval grew.
    assert hass.states.get(fail_id).state != "unavailable"
    assert int(hass.states.get(poll_id).state) > POLL_INTERVAL_SECONDS
    assert coordinator.update_interval > timedelta(seconds=POLL_INTERVAL_SECONDS)
