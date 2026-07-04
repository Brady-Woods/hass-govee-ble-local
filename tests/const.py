"""Shared constants and helpers for the Govee BLE Local tests."""
from __future__ import annotations

from typing import Any

from govee_ble_local import GoveeBleStatus

# crc32("AA:BB:CC:DD:EE:05") % 8 == 0, so setup incurs no stagger sleep.
ADDRESS = "AA:BB:CC:DD:EE:05"
TITLE = "Test Ceiling"
LOCAL_NAME = "GVH60A6ABCD"
WIFI_MAC = "11:22:33:44:55:66"


def make_status(**overrides: Any) -> GoveeBleStatus:
    """Build a GoveeBleStatus with sensible defaults, overridable per-test."""
    defaults: dict[str, Any] = {
        "zone_upper_on": True,
        "zone_lower_on": False,
        "brightness_pct": 40,
        "scene_id": None,
        "hardware_version": "1.00.00",
        "wifi_mac": WIFI_MAC,
    }
    defaults.update(overrides)
    return GoveeBleStatus(**defaults)
