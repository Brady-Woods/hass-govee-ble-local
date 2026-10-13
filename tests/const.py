"""Shared constants for the Govee BLE Local tests."""

from __future__ import annotations

ADDRESS = "AA:BB:CC:DD:EE:05"
TITLE = "Test Ceiling"
LOCAL_NAME = "GVH60A6ABCD"
PLUG_ADDRESS = "AA:BB:CC:DD:EE:07"
PLUG_NAME = "GVH5083EF01"

# Govee manufacturer data (bleak form): company id 0x88<hdr>, value starting
# with the 0xEC marker; byte 4 is the passive on/off flag.
GOVEE_COMPANY_ID = 0x8843


def govee_mfg(*, on: bool | None = True) -> dict[int, bytes]:
    """Govee manufacturer data carrying the given passive on/off flag."""
    flag = 0 if on is None else (1 if on else 0)
    payload = bytes([0xEC, 0x00, 0x01, 0x01, flag, 0x00])
    if on is None:
        payload = payload[:4]  # too short to carry the on/off byte
    return {GOVEE_COMPANY_ID: payload}


SECRET_HEX = "a1b2c3d4e5f60718"
