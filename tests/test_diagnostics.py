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
    """Diagnostics expose profile/status but redact identifying fields."""
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert diagnostics["entry"]["data"]["address"] == REDACTED
    assert diagnostics["serial_number"] == REDACTED
    assert diagnostics["status"]["wifi_mac"] == REDACTED

    assert diagnostics["profile"]["sku"] == "H60A6"
    assert diagnostics["profile"]["capabilities"]["rgb"] is True
    assert diagnostics["coordinator"]["last_update_success"] is True
    assert diagnostics["status"]["brightness_pct"] == 40
