"""Diagnostics support for the Govee BLE Local integration."""
from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import GoveeBleLocalConfigEntry

# The BLE address identifies a specific physical unit, so keep it out of shared
# diagnostics dumps.
TO_REDACT = {"address", "ble_mac", "wifi_mac", "serial_number"}
_REDACTED = "**REDACTED**"


def _scrub(text: str, address: str) -> str:
    """Strip the device MAC out of a captured log/frame line (it is embedded in the
    message text, which key-based redaction can't reach)."""
    return text.replace(address, _REDACTED).replace(address.replace(":", ""), _REDACTED)


def _scrub_self_test(report: dict[str, Any], address: str) -> dict[str, Any]:
    scrubbed = copy.deepcopy(report)
    scrubbed["frames"] = [_scrub(line, address) for line in scrubbed.get("frames", [])]
    scrubbed["log"] = [_scrub(line, address) for line in scrubbed.get("log", [])]
    return scrubbed


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GoveeBleLocalConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data
    coordinator = data.coordinator
    device = data.device
    state = coordinator.data
    address: str = entry.data["address"]

    last_self_test = coordinator.last_self_test
    return async_redact_data(
        {
            "entry": {
                "title": entry.title,
                "data": dict(entry.data),
            },
            "coordinator": {
                "last_update_success": coordinator.last_update_success,
                "update_interval": str(coordinator.update_interval),
            },
            "device": {
                "sku": device.sku,
                "model": device.sku,
                "capabilities": sorted(c.value for c in device.capabilities),
                "zones": [z.name for z in device.zones],
                "scene_count": len(device.scene_names)
                if hasattr(device, "scene_names")
                else 0,
            },
            "state": asdict(state) if state is not None else None,
            # Capture surfaces (addresses scrubbed from the free-text lines).
            "recent_warnings": [_scrub(line, address) for line in data.log_capture.records()],
            "last_self_test": _scrub_self_test(last_self_test, address)
            if last_self_test is not None
            else None,
        },
        TO_REDACT,
    )
