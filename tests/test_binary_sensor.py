"""Tests for the Govee BLE Local connectivity binary sensor."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from govee_ble_local import Device
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.const import (
    DOMAIN,
    LAST_SEEN_RESOLUTION_SECONDS,
    UNAVAILABLE_AFTER_FAILURES,
)

from .conftest import (
    advertisement_callback,
    allow_adverts,
    make_service_info,
    unavailable_callback,
)
from .const import ADDRESS

MAIN_LIGHT = "light.test_ceiling"


@pytest.fixture
def connectivity_id(hass: HomeAssistant, setup_integration: MockConfigEntry) -> str:
    """Entity id of the connectivity binary sensor."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{ADDRESS}_connectivity"
    )
    assert entity_id is not None
    return entity_id


async def test_connectivity_sensor_registered_as_diagnostic(
    hass: HomeAssistant, connectivity_id: str
) -> None:
    """The sensor is a DIAGNOSTIC connectivity entity and starts on for a heard device."""
    entry = er.async_get(hass).async_get(connectivity_id)
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    state = hass.states.get(connectivity_id)
    assert state.state == STATE_ON
    assert state.attributes["device_class"] == BinarySensorDeviceClass.CONNECTIVITY


async def test_connectivity_follows_any_scanner_history(
    hass: HomeAssistant,
    connectivity_id: str,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """Off when no scanner hears the device, regardless of connectable-only presence."""
    # Only the connectable history is lost: some scanner still hears it.
    presence.connectable = False
    unavailable_callback(mock_bluetooth, connectable=True)(make_service_info())
    assert hass.states.get(connectivity_id).state == STATE_ON

    presence.any = False
    unavailable_callback(mock_bluetooth, connectable=False)(make_service_info())
    assert hass.states.get(connectivity_id).state == STATE_OFF


async def test_connectivity_stays_available_when_off(
    hass: HomeAssistant,
    connectivity_id: str,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
) -> None:
    """The sensor reports "off" rather than going unavailable with the rest of the device."""
    presence.connectable = presence.any = False
    coordinator = hass.config_entries.async_entries(DOMAIN)[0].runtime_data.coordinator
    coordinator.consecutive_failures = UNAVAILABLE_AFTER_FAILURES
    unavailable_callback(mock_bluetooth, connectable=False)(make_service_info())

    assert hass.states.get(MAIN_LIGHT).state == STATE_UNAVAILABLE
    assert hass.states.get(connectivity_id).state == STATE_OFF


async def test_connectivity_returns_on_with_the_next_diagnostic_publish(
    hass: HomeAssistant,
    connectivity_id: str,
    *,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
    presence: SimpleNamespace,
    freezer: Any,
) -> None:
    """When the device is heard again the sensor flips back on from the advertisement tick."""
    allow_adverts(mock_device)
    callback = advertisement_callback(mock_bluetooth)
    callback(make_service_info(), BluetoothChange.ADVERTISEMENT)

    presence.connectable = presence.any = False
    unavailable_callback(mock_bluetooth, connectable=False)(make_service_info())
    assert hass.states.get(connectivity_id).state == STATE_OFF

    presence.connectable = presence.any = True
    freezer.tick(timedelta(seconds=LAST_SEEN_RESOLUTION_SECONDS + 1))
    callback(make_service_info(), BluetoothChange.ADVERTISEMENT)
    assert hass.states.get(connectivity_id).state == STATE_ON


async def test_connectivity_updates_on_skipped_poll(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    connectivity_id: str,
    presence: SimpleNamespace,
) -> None:
    """A skipped poll's diagnostic push also refreshes the connectivity state."""
    coordinator = setup_integration.runtime_data.coordinator
    presence.connectable = presence.any = False
    await coordinator.async_refresh()
    await coordinator.async_refresh()  # an ongoing outage only pushes diagnostics
    assert hass.states.get(connectivity_id).state == STATE_OFF
    assert hass.states.get(MAIN_LIGHT).state != STATE_UNAVAILABLE  # not unavailable yet
