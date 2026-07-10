"""Tests for the Govee BLE Local self-test button."""
from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.const import DOMAIN

from .const import ADDRESS

UID = f"{ADDRESS}_self_test"


async def test_self_test_button_runs_and_stores_capture(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: AsyncMock
) -> None:
    """Pressing the button runs the self-test and stores the capture for diagnostics."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("button", DOMAIN, UID)
    assert entity_id is not None

    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True
    )

    report = setup_integration.runtime_data.coordinator.last_self_test
    assert report is not None
    assert report["sku"] == "H60A6"
    assert isinstance(report["steps"], list) and report["steps"]
    # The device was actually exercised.
    mock_device.set_rgb.assert_awaited()
    mock_device.set_scene_by_name.assert_awaited()
