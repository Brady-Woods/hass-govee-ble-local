"""Tests for the Govee BLE Local connectivity binary sensor."""
from __future__ import annotations

from unittest.mock import AsyncMock

from bleak.exc import BleakError
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.const import DOMAIN

from .const import ADDRESS

UID = f"{ADDRESS}_connectivity"


async def test_connectivity_sensor_created(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The connectivity binary sensor is a DIAGNOSTIC entity."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, UID)
    assert entity_id is not None
    entry = registry.async_get(entity_id)
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC


async def test_connectivity_reflects_poll_outcome(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: AsyncMock
) -> None:
    """On when the last poll succeeded; off (but still available) when it fails."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, UID)
    assert entity_id is not None

    # Connectivity device_class: "on" == connected.
    assert hass.states.get(entity_id).state == "on"

    mock_device.update.side_effect = BleakError("no slot")
    await setup_integration.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state == "off"  # reports the down state, not "unavailable"
