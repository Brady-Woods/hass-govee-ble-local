"""Tests for the Govee BLE Local connectivity binary sensor."""
from __future__ import annotations

from unittest.mock import patch

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.const import DOMAIN

from .const import ADDRESS

UID = f"{ADDRESS}_connectivity"
_BS = "custom_components.govee_ble_local.binary_sensor"


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


async def test_connectivity_reflects_advertisement_presence(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """On when the device is still advertising (present), off when it isn't -
    independent of whether the connect-poll succeeds; stays available either way."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, UID)
    assert entity_id is not None
    coordinator = setup_integration.runtime_data.coordinator

    with patch(f"{_BS}.bluetooth.async_address_present", return_value=True):
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == "on"

    with patch(f"{_BS}.bluetooth.async_address_present", return_value=False):
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        # Reports the down state, not "unavailable".
        assert hass.states.get(entity_id).state == "off"
