"""Small shared helpers: entity partition, device-info cleaning, unit conversion, secrets."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bleak.exc import BleakError
from govee_ble_local import Capability, GoveeBleError
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import DEFAULT_MAX_KELVIN, DEFAULT_MIN_KELVIN, DOMAIN

if TYPE_CHECKING:
    from govee_ble_local import Device, Zone

    from . import GoveeBleLocalConfigEntry

# Errors that mean "the BLE operation didn't land" rather than a bug: bleak's own,
# the library's (connect/handshake/timeout/not-supported - not BleakError
# subclasses), and a stalled await's bare TimeoutError.
BLE_ERRORS: tuple[type[Exception], ...] = (BleakError, GoveeBleError, TimeoutError)

SECRET_BYTES = 8

# Which platform owns a device or a zone is decided once, below, for light.py,
# switch.py and the legacy zone-switch cleanup in __init__.py, so each relay gets
# exactly one entity. Any of these capabilities makes a device a light fixture;
# one with none of them (e.g. a smart plug) is a switch instead.
_FIXTURE_LIGHT_CAPABILITIES = frozenset(
    {Capability.BRIGHTNESS, Capability.RGB, Capability.COLOR_TEMP}
)


def has_fixture_light(device: Device) -> bool:
    """Return True if the device gets a whole-fixture light."""
    return bool(device.capabilities & _FIXTURE_LIGHT_CAPABILITIES)


def has_power_switch(device: Device) -> bool:
    """Return True if the device gets a whole-device power switch.

    Only a device that is neither a light fixture nor zoned (a zoned device is
    switched per zone).
    """
    return (
        not device.zones
        and not has_fixture_light(device)
        and Capability.POWER in device.capabilities
    )


def is_colour_zone(zone: Zone) -> bool:
    """Return True if the zone has segments: a light, else an on/off-only switch."""
    return bool(zone.segments)


def kelvin_range(device: Device) -> tuple[int, int]:
    """Return the device's colour-temperature range, or the defaults where it gives none."""
    return (device.min_kelvin or DEFAULT_MIN_KELVIN, device.max_kelvin or DEFAULT_MAX_KELVIN)


def mid_kelvin(device: Device) -> int:
    """Return the neutral middle of the device's colour-temperature range."""
    lo, hi = kelvin_range(device)
    return (lo + hi) // 2


def clean_mac(mac: str | None) -> str | None:
    """Return a real MAC address, or None for an absent / all-zero placeholder.

    The library's device-info read-back can yield ``00:00:00:00:00:00`` when a
    device answers the query with an empty/zeroed field; that must not become a
    device-registry connection.
    """
    if not mac:
        return None
    normalized = mac.replace(":", "").replace("-", "").strip().lower()
    if not normalized or set(normalized) <= {"0"}:
        return None
    return mac


def clean_text(value: str | None) -> str | None:
    """Return a meaningful version/serial string, or None for a placeholder.

    Placeholders are empty or all-zero values such as ``"0"``, ``"0.0"`` or
    ``"0.0.0"``.
    """
    if not value:
        return None
    if set(value) <= {"0", ".", " "}:
        return None
    return value


def pct_to_brightness(pct: int) -> int:
    """Convert a device brightness percent (0..100) to HA brightness (0..255).

    The percent is read back from the device and clamped first, so a stray
    out-of-range report can't publish a brightness outside 0..255.
    """
    return round(max(0, min(100, pct)) / 100 * 255)


def brightness_to_pct(brightness: int) -> int:
    """Convert HA brightness (1..255) to a device percent (1..100).

    HA 1-2 must not round down to 0, which the device treats as off.
    """
    return max(1, round(brightness / 255 * 100))


def parse_secret(raw: str) -> str | None:
    """Normalize a user-entered secret to 16 lowercase hex chars.

    Whitespace (anywhere) and ``:`` / ``-`` separators are dropped. Empty input
    returns None. Raises ValueError for non-hex input or a key that isn't
    exactly 8 bytes.
    """
    # bytes.fromhex skips whitespace, so it must be gone before validating:
    # otherwise e.g. an embedded tab would pass and be stored.
    cleaned = "".join(raw.split()).replace(":", "").replace("-", "").lower()
    if not cleaned:
        return None
    if len(bytes.fromhex(cleaned)) != SECRET_BYTES:
        raise ValueError(f"secret must be {SECRET_BYTES} bytes ({SECRET_BYTES * 2} hex chars)")
    return cleaned


@callback
def async_secret_rejected(
    hass: HomeAssistant, entry: GoveeBleLocalConfigEntry
) -> HomeAssistantError:
    """Start reauth for a device that rejected its secret; return the error to raise.

    Shared by every command path (entity commands, the self-test) so they map a
    ``GoveeBleAuthError`` the same way.
    """
    entry.async_start_reauth(hass)
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="secret_rejected",
        translation_placeholders={"device_name": entry.title},
    )
