"""Tests for the device-info cleaning helpers."""
from __future__ import annotations

import pytest

from custom_components.govee_ble_local.helpers import clean_mac, clean_text


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("00:00:00:00:00:00", None),
        ("000000000000", None),
        ("A4:C1:38:AA:BB:CC", "A4:C1:38:AA:BB:CC"),
    ],
)
def test_clean_mac(value: str | None, expected: str | None) -> None:
    assert clean_mac(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("0", None),
        ("0.0", None),
        ("0.00", None),
        ("0.0.0", None),
        ("1.02.05", "1.02.05"),
        ("GV-ABC123", "GV-ABC123"),
    ],
)
def test_clean_text(value: str | None, expected: str | None) -> None:
    assert clean_text(value) == expected
