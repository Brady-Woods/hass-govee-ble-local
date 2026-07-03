"""Diagnostics support for the Govee H60A6 integration."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import GoveeH60A6ConfigEntry

# BLE/Wi-Fi MACs and the serial number identify a specific physical unit, so
# keep them out of shared diagnostics dumps.
TO_REDACT = {"address", "ble_mac", "wifi_mac", "serial_number"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GoveeH60A6ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data
    coordinator = data.coordinator
    status = coordinator.data

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
            "serial_number": data.serial_number,
            "scene_library_count": len(data.scene_library),
            "status": asdict(status) if status is not None else None,
        },
        TO_REDACT,
    )
