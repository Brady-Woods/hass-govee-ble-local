"""Small shared helpers for surfacing device-info safely."""
from __future__ import annotations


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
    """Return a meaningful version/serial string, or None for a zero/empty
    placeholder (e.g. ``"0"``, ``"0.0"``, ``"0.00"``, ``"0.0.0"``)."""
    if not value:
        return None
    if set(value) <= {"0", ".", " "}:
        return None
    return value
