"""Diagnostics support for the Govee BLE Local integration."""

from __future__ import annotations

from dataclasses import asdict
import re
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from .const import CONF_SECRET

if TYPE_CHECKING:
    from govee_ble_local import DeviceState

    from . import GoveeBleLocalConfigEntry

# Identifiers of a specific physical unit (and the account-lock key) stay out of
# shared diagnostics dumps. The title is the advertised name by default, which
# ends in the last two bytes of the MAC.
TO_REDACT = {CONF_ADDRESS, "ble_mac", "wifi_mac", "serial_number", CONF_SECRET, "title"}
_SEPARATORS = re.compile(r"[:\-]")
_HEX = re.compile(r"[0-9A-Fa-f]+")
# Shorter values (e.g. a serial like "1") would over-redact unrelated text.
_MIN_IDENTIFIER_LEN = 6
# Captured library text (frame lines, log lines, warnings) carries frame payloads
# as hex. A multi-chunk reply splits a MAC or serial across lines, where no
# by-value pattern can find it, so every hex run this long goes. The raw frames
# stay available to admins in the capture_session response.
_HEX_RUN = re.compile(r"[0-9A-Fa-f]{8,}")


def _state_dump(state: DeviceState) -> dict[str, Any]:
    """Return the device state, its zone confirmations as ages.

    ``zone_power_confirmed`` holds monotonic times, which mean nothing outside
    this process: the dump says how many seconds ago each zone was confirmed.
    """
    dump = asdict(state)
    now = time.monotonic()
    dump["zone_power_confirmed_seconds_ago"] = {
        index: round(now - at, 1) for index, at in dump.pop("zone_power_confirmed").items()
    }
    return dump


def _identifier_pattern(values: list[str]) -> re.Pattern[str] | None:
    """Match each identifier in free text, case-insensitively.

    A hex value (a MAC, a serial, the secret) matches with ``:``/``-``/no
    separators between its bytes, in either byte order (device-info replies
    embed identifiers reversed, and frame hex is lower-case).
    """
    alternatives: set[str] = set()
    for value in values:
        compact = _SEPARATORS.sub("", value)
        if len(compact) < _MIN_IDENTIFIER_LEN:
            continue
        alternatives.add(re.escape(value))
        alternatives.add(re.escape(compact))
        if len(compact) % 2 == 0 and _HEX.fullmatch(compact):
            octets = [compact[i : i + 2] for i in range(0, len(compact), 2)]
            for ordered in (octets, octets[::-1]):
                alternatives.update(separator.join(ordered) for separator in ("", ":", "-"))
    if not alternatives:
        return None
    # Longest first so a full form wins over a substring form.
    return re.compile("|".join(sorted(alternatives, key=len, reverse=True)), re.IGNORECASE)


def _scrub(value: Any, pattern: re.Pattern[str] | None) -> Any:
    """Return ``value`` with every match of ``pattern`` in its strings (recursively) redacted."""
    if pattern is None:
        return value
    if isinstance(value, str):
        return pattern.sub(REDACTED, value)
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
    # hex or error messages, so every string is also scrubbed by value. Hex runs
    # in captured text go first: an identifier match inside a run would leave
    # its neighbours' fragments behind. The title is among them: Home Assistant's
    # error for a failed poll names the device by it.
    identifiers = [
        value
        for value in (
            entry.title,
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
            "poll_policy": coordinator.poll_policy.value,
            "consecutive_failures": coordinator.consecutive_failures,
            "total_failures": coordinator.total_failures,
            "slow_poll_steps": coordinator.slow_poll_steps,
            "last_poll_seconds": coordinator.last_poll_seconds,
            "device_available": coordinator.device_available,
        },
        "device": {
            "sku": device.sku,
            "capabilities": sorted(c.value for c in device.capabilities),
            "zones": [z.name for z in device.zones],
            # Counted live (the scenes a device offers can change); not read at all when
            # the scene store couldn't be read at setup, as each read would hit the file.
            "scene_count": None if data.scene_store_error else len(device.scene_names),
            "scene_store_error": data.scene_store_error,
        },
        "state": _state_dump(state),
        "recent_warnings": _scrub(data.log_capture.records(), _HEX_RUN),
        "last_self_test": _scrub(data.last_self_test, _HEX_RUN),
    }
    scrubbed: dict[str, Any] = _scrub(
        async_redact_data(diagnostics, TO_REDACT), _identifier_pattern(identifiers)
    )
    return scrubbed
