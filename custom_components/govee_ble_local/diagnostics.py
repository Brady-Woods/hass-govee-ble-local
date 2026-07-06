"""Diagnostics support for the Govee BLE Local integration."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import GoveeBleLocalConfigEntry

# The BLE address identifies a specific physical unit, so keep it out of shared
# diagnostics dumps.
TO_REDACT = {"address", "ble_mac", "wifi_mac", "serial_number"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GoveeBleLocalConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data
    coordinator = data.coordinator
    device = data.device
    state = coordinator.data

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
                "model": device.model,
                "capabilities": sorted(c.value for c in device.capabilities),
                "zones": [z.name for z in device.zones],
                "scene_count": len(device.scene_names)
                if hasattr(device, "scene_names")
                else 0,
            },
            "state": asdict(state) if state is not None else None,
        },
        TO_REDACT,
    )
