"""Small shared helpers: entity partition, zone state, device info, units, secrets, pacts."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from bleak.exc import BleakError
from govee_ble_local import Capability, GoveeBleError
from govee_ble_local.identify import parse_manufacturer_data
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import DEFAULT_MAX_KELVIN, DEFAULT_MIN_KELVIN, DOMAIN, ZONE_OFF_FRESH_SECONDS

if TYPE_CHECKING:
    from govee_ble_local import Device, Zone
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

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


def advertised_pact(service_info: BluetoothServiceInfoBleak | None) -> tuple[int, int] | None:
    """Return the ``(pact_type, pact_code)`` a set-up device's advertisement carries, or None.

    The name plays no part: a device already set up may show any name in Home Assistant's
    merged record (another device's advertisement merged in, say), and its Govee
    manufacturer data decides, as the app's control screen reads a bound device found by its
    stored address and as ``create_device`` reads it (``parse_manufacturer_data`` keeps its
    signature and results through govee-ble-local 1.x). ``identify``, which keys on a
    Govee-style name as the app's add-device scan does, takes its pact from the same parse.
    The library's public ``bound_advertisement`` replaces this once the floor reaches the
    release that ships it.
    """
    if service_info is None:
        return None
    if (fields := parse_manufacturer_data(service_info.manufacturer_data)) is not None:
        _encrypted, _version, pact_type, pact_code = fields
        return pact_type, pact_code
    return None


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


def zone_confirmed_off(device: Device, zone_name: str) -> bool:
    """Return whether the device itself said, a moment ago, that the zone is off.

    Only while the whole fixture isn't known on, and by a confirmation less than
    ZONE_OFF_FRESH_SECONDS old. The library stamps a zone's power as confirmed
    (``zone_power_confirmed``, on ``time.monotonic()``) only when the device
    reported it or acked a write that sets it, as an acked whole-fixture power-off
    does for every zone. It drops the stamp when anything else changes the zone's
    power (an advertisement, a scene), when a write may have lit the zone (a
    brightness, colour or segment write wakes a fixture not known on), and when a
    power write may have landed unacked. A zone off by an advertisement or by the
    library's assumption alone has none.
    """
    if device.state.is_on is True or device.zone_is_on(zone_name) is not False:
        return False
    power_index = next(z.power_index for z in device.zones if z.name == zone_name)
    confirmed = device.state.zone_power_confirmed.get(power_index)
    return confirmed is not None and time.monotonic() - confirmed < ZONE_OFF_FRESH_SECONDS


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


async def async_context_is_person(hass: HomeAssistant, context: Context | None) -> bool:
    """Return whether a command came from a person: a user of Home Assistant, not a system user.

    The context's user is the one logged in to the frontend or the companion
    app (a dashboard, a more-info dialog, a widget; typed or phone Assist looks
    the same), or a long-lived token's owner. An automation, a voice satellite
    or HomeKit has none; a script, a scene or a light group passes on the
    context of what called it (from a dashboard, the user's). Home Assistant
    Cloud's Alexa and Google Assistant, and VoIP phones, act as system users.
    A user since deleted isn't a person either.
    """
    if context is None or context.user_id is None:
        return False
    user = await hass.auth.async_get_user(context.user_id)
    return user is not None and not user.system_generated
