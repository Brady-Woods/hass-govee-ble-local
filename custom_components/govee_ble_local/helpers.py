"""Small shared helpers: BLE error classes, device-info cleaning, unit conversion, secrets."""

from types import ModuleType

from bleak.exc import BleakError
import govee_ble_local
from govee_ble_local import GoveeBleError

# Errors that mean "the BLE operation didn't land" rather than a bug: bleak's own,
# the library's (connect/handshake/timeout/not-supported - not BleakError
# subclasses), and a stalled await's bare TimeoutError.
BLE_ERRORS: tuple[type[Exception], ...] = (BleakError, GoveeBleError, TimeoutError)

SECRET_BYTES = 8


def optional_errors(module: ModuleType, name: str) -> tuple[type[Exception], ...]:
    """Return ``(module.<name>,)``, or ``()`` if this library version doesn't define it.

    For exceptions added after the manifest's library floor. ``except ()`` matches
    nothing, so on an older library the error falls through to the broader clause
    after it (it subclasses ``GoveeBleError``), as before.
    """
    error = getattr(module, name, None)
    return (error,) if error is not None else ()


# The device acknowledged a write with a non-zero status. Added after govee-ble-local
# 1.3.0, which logged the rejection at ERROR itself and raised a private class.
COMMAND_REJECTED = optional_errors(govee_ble_local, "GoveeBleCommandRejected")


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
    """Convert a device brightness percent (0..100) to HA brightness (0..255)."""
    return round(pct / 100 * 255)


def brightness_to_pct(brightness: int) -> int:
    """Convert HA brightness (1..255) to a device percent (1..100).

    HA 1-2 must not round down to 0, which the device treats as off.
    """
    return max(1, round(brightness / 255 * 100))


def parse_secret(raw: str) -> str | None:
    """Normalize a user-entered secret to 16 lowercase hex chars.

    Empty input returns None. Raises ValueError for non-hex input or a key that
    isn't exactly 8 bytes.
    """
    cleaned = raw.strip().replace(":", "").replace(" ", "").lower()
    if not cleaned:
        return None
    if len(bytes.fromhex(cleaned)) != SECRET_BYTES:
        raise ValueError(f"secret must be {SECRET_BYTES} bytes ({SECRET_BYTES * 2} hex chars)")
    return cleaned
