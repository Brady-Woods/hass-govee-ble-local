"""Shared constants and helpers for the Govee BLE Local tests."""
from __future__ import annotations

from govee_ble_local import Capability, Zone

# crc32("AA:BB:CC:DD:EE:05") % 8 == 0, so setup incurs no stagger sleep.
ADDRESS = "AA:BB:CC:DD:EE:05"
TITLE = "Test Ceiling"
LOCAL_NAME = "GVH60A6ABCD"

# The H60A6's full capability set + zones, as the v2 library reports them.
H60A6_CAPS = frozenset(
    {
        Capability.POWER,
        Capability.BRIGHTNESS,
        Capability.RGB,
        Capability.COLOR_TEMP,
        Capability.SCENES,
        Capability.SEGMENTS,
    }
)
H60A6_ZONES = (
    Zone("main", power_index=1, segments=tuple(range(13))),
    Zone("background", power_index=0, segments=()),
)
SCENE_NAMES = ["Sunrise", "Forest", "Aurora"]
