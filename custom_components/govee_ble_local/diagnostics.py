"""Diagnostics support for the Govee BLE Local integration."""

from __future__ import annotations

from dataclasses import asdict
import re
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from .const import CONF_SECRET

if TYPE_CHECKING:
    from . import GoveeBleLocalConfigEntry

# Identifiers of a specific physical unit (and the account-lock key) stay out of
# shared diagnostics dumps.
TO_REDACT = {CONF_ADDRESS, "ble_mac", "wifi_mac", "serial_number", CONF_SECRET}
_REDACTED = "**REDACTED**"
_SEPARATORS = re.compile(r"[:\-]")
# Shorter values (e.g. a serial like "1") would over-redact unrelated text.
_MIN_IDENTIFIER_LEN = 6
_MAC_HEX_LEN = 12


def _identifier_pattern(values: list[str]) -> re.Pattern[str] | None:
    """Match each identifier in free text, case-insensitively.

    A MAC-like value matches with ``:``/``-``/no separators and also as the
    contiguous hex of its bytes in either order (device-info replies embed the
    address bytes reversed, and frame hex is lower-case).
    """
    alternatives: set[str] = set()
    for value in values:
        compact = _SEPARATORS.sub("", value)
        if len(compact) < _MIN_IDENTIFIER_LEN:
            continue
        alternatives.add(re.escape(value))
        alternatives.add(re.escape(compact))
        if len(compact) == _MAC_HEX_LEN and re.fullmatch(r"[0-9A-Fa-f]+", compact):
            octets = [compact[i : i + 2] for i in range(0, _MAC_HEX_LEN, 2)]
            alternatives.add(":".join(octets))
            alternatives.add("-".join(octets))
            alternatives.add("".join(reversed(octets)))
    if not alternatives:
        return None
    # Longest first so a full form wins over a substring form.
    return re.compile("|".join(sorted(alternatives, key=len, reverse=True)), re.IGNORECASE)


def _scrub(value: Any, pattern: re.Pattern[str] | None) -> Any:
    """Return ``value`` with every string (recursively) scrubbed of identifiers."""
    if pattern is None:
        return value
    if isinstance(value, str):
        return pattern.sub(_REDACTED, value)
    if isinstance(value, dict):
        return {key: _scrub(item, pattern) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(item, pattern) for item in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GoveeBleLocalConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data
    coordinator = data.coordinator
    device = data.device
    state = coordinator.data

    # Key-based redaction can't reach identifiers embedded in log lines, frame
    # hex or error messages, so every string is also scrubbed by value.
    identifiers = [
        value
        for value in (
            entry.data[CONF_ADDRESS],
            entry.data.get(CONF_SECRET),
            state.ble_mac,
            state.wifi_mac,
            state.serial_number,
        )
        if value
    ]
    diagnostics = {
        "entry": {"title": entry.title, "data": dict(entry.data)},
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval),
            "consecutive_failures": coordinator.consecutive_failures,
            "total_failures": coordinator.total_failures,
            "device_available": coordinator.device_available,
        },
        "device": {
            "sku": device.sku,
            "capabilities": sorted(c.value for c in device.capabilities),
            "zones": [z.name for z in device.zones],
            "scene_count": len(device.scene_names),
        },
        "state": asdict(state),
        "recent_warnings": data.log_capture.records(),
        "last_self_test": coordinator.last_self_test,
    }
    scrubbed: dict[str, Any] = _scrub(
        async_redact_data(diagnostics, TO_REDACT), _identifier_pattern(identifiers)
    )
    return scrubbed
