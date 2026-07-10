"""Tests for Govee BLE Local diagnostics."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .const import ADDRESS

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


async def test_diagnostics_includes_capture_with_address_scrubbed(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Diagnostics surface recent warnings + the last self-test, with the device MAC
    scrubbed out of the free-text frame/log lines."""
    coordinator = setup_integration.runtime_data.coordinator
    coordinator.last_self_test = {
        "ok": True,
        "sku": "H60A6",
        "encryption": "aes_rc4_psk",
        "capabilities": ["power"],
        "steps": [{"step": "power_on", "ok": True, "acked": True}],
        "frames": [f"{ADDRESS} tx switch plain=3301 wire=3301 enc=aes_rc4_psk"],
        "log": [f"INFO {ADDRESS}: session ready"],
    }

    diagnostics = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert "recent_warnings" in diagnostics
    dump = diagnostics["last_self_test"]
    assert dump["ok"] is True
    assert ADDRESS not in dump["frames"][0]
    assert REDACTED in dump["frames"][0]
    assert ADDRESS not in dump["log"][0]
