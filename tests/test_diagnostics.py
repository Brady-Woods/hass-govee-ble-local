"""Tests for Govee BLE Local diagnostics."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.diagnostics import (
    async_get_config_entry_diagnostics,
)

REDACTED = "**REDACTED**"


async def test_diagnostics_redacts_identifiers(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Diagnostics expose device capabilities but redact identifying fields."""
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert diagnostics["entry"]["data"]["address"] == REDACTED

    assert diagnostics["device"]["sku"] == "H60A6"
    assert diagnostics["device"]["model"] == "H60A6"
    assert "rgb" in diagnostics["device"]["capabilities"]
    assert diagnostics["device"]["zones"] == ["main", "background"]
    assert diagnostics["coordinator"]["last_update_success"] is True
