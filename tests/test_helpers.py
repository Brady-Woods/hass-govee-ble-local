"""Tests for the small shared helpers."""

from __future__ import annotations

from collections.abc import Callable
import dataclasses

from bleak.exc import BleakError
from govee_ble_local import Capability, Device, GoveeBleConnectionError, GoveeBleError, Zone
from homeassistant.core import Context, HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.govee_ble_local.helpers import (
    BLE_ERRORS,
    async_context_is_person,
    brightness_to_pct,
    clean_mac,
    clean_text,
    has_fixture_light,
    has_power_switch,
    parse_secret,
    pct_to_brightness,
)

from .conftest import make_device, make_power_only_light
from .const import SECRET_HEX


def _with(sku: str, **profile: object) -> Callable[[], Device]:
    """Build ``sku``'s device with these profile fields replaced."""

    def build() -> Device:
        device = make_device(sku)
        device.profile = dataclasses.replace(device.profile, **profile)
        return device

    return build


@pytest.mark.parametrize(
    ("build", "light", "switch"),
    [
        (_with("H5083"), False, True),
        (make_power_only_light, True, False),
        (_with("H6006"), True, False),
        (_with("H6006", capabilities=frozenset({Capability.BRIGHTNESS}), relay=True), True, False),
        (_with("H5083", zones=(Zone("a", power_index=0), Zone("b", power_index=1))), False, False),
        (_with("H6006", capabilities=frozenset({Capability.SCENES})), False, False),
    ],
    ids=[
        "relay_plug",
        "power_only_light",
        "light_fixture",
        "dimmable_relay",
        "zoned_relay_plug",
        "no_power",
    ],
)
def test_fixture_light_or_power_switch(
    build: Callable[[], Device], light: bool, switch: bool
) -> None:
    """A light capability, or power without a relay, makes a light; only a relay plug a switch.

    A zoned relay plug is switched per zone, and a device with neither power nor a light
    capability gets neither. No device gets both.
    """
    device = build()
    assert has_fixture_light(device) is light
    assert has_power_switch(device) is switch


@pytest.mark.parametrize(
    "mac",
    [None, "", "00:00:00:00:00:00", "00-00-00-00-00-00", "000000000000", " 00:00:00:00:00:00 "],
)
def test_clean_mac_placeholder(mac: str | None) -> None:
    """Absent and all-zero MACs (any separator) are treated as missing."""
    assert clean_mac(mac) is None


@pytest.mark.parametrize("mac", ["AA:BB:CC:DD:EE:05", "98-17-3c-b1-a2-d1", "00:00:00:00:00:01"])
def test_clean_mac_real(mac: str) -> None:
    """A real MAC is returned exactly as given."""
    assert clean_mac(mac) == mac


@pytest.mark.parametrize("value", [None, "", "0", "0.0", "0.00", "0.0.0", " "])
def test_clean_text_placeholder(value: str | None) -> None:
    """Empty and all-zero version/serial placeholders become None."""
    assert clean_text(value) is None


@pytest.mark.parametrize("value", ["1.02.03", "0.0.1", "SN-0001", "10"])
def test_clean_text_real(value: str) -> None:
    """Meaningful version strings are kept unchanged."""
    assert clean_text(value) == value


@pytest.mark.parametrize(("pct", "brightness"), [(0, 0), (100, 255), (1, 3), (50, 128)])
def test_pct_to_brightness(pct: int, brightness: int) -> None:
    """Device percent maps onto the HA 0..255 range."""
    assert pct_to_brightness(pct) == brightness


@pytest.mark.parametrize(("pct", "brightness"), [(101, 255), (150, 255), (255, 255), (-5, 0)])
def test_pct_to_brightness_clamps_out_of_range_readback(pct: int, brightness: int) -> None:
    """A read-back percent outside 0..100 can't become a brightness outside 0..255."""
    assert pct_to_brightness(pct) == brightness


@pytest.mark.parametrize(("brightness", "pct"), [(255, 100), (128, 50), (127, 50)])
def test_brightness_to_pct(brightness: int, pct: int) -> None:
    """HA brightness maps onto a device percent."""
    assert brightness_to_pct(brightness) == pct


@pytest.mark.parametrize("brightness", [1, 2])
def test_brightness_to_pct_never_zero(brightness: int) -> None:
    """Dim HA values must not round down to 0 (which the device treats as off)."""
    assert brightness_to_pct(brightness) == 1


def test_brightness_round_trip() -> None:
    """Every percent survives a round trip through HA brightness within 1."""
    for pct in range(1, 101):
        assert abs(brightness_to_pct(pct_to_brightness(pct)) - pct) <= 1


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_parse_secret_blank(raw: str) -> None:
    """Blank input means "no secret"."""
    assert parse_secret(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        SECRET_HEX,
        SECRET_HEX.upper(),
        "A1:B2:C3:D4:E5:F6:07:18",
        "a1 b2 c3 d4 e5 f6 07 18",
        f"  {SECRET_HEX.upper()}  ",
        "A1-B2-C3-D4-E5-F6-07-18",
        "a1b2c3d4\te5f60718",
        "a1b2\nc3d4\r\ne5f6\x0b0718",
    ],
    ids=["plain", "upper", "colons", "spaces", "padded", "dashes", "tab", "line_breaks"],
)
def test_parse_secret_normalizes(raw: str) -> None:
    """Case, separators and whitespace anywhere are normalized away (not stored)."""
    assert parse_secret(raw) == SECRET_HEX


@pytest.mark.parametrize("raw", ["not-hex!", "zz" * 8, "a1b2c3d4e5f6071"])
def test_parse_secret_not_hex(raw: str) -> None:
    """Non-hex (or odd-length) input is rejected."""
    with pytest.raises(ValueError, match="hexadecimal"):
        parse_secret(raw)


@pytest.mark.parametrize("raw", ["a1b2c3d4e5f6", "a1b2c3d4e5f607182a"])
def test_parse_secret_wrong_length(raw: str) -> None:
    """A valid-hex key that isn't exactly 8 bytes is rejected."""
    with pytest.raises(ValueError, match="8 bytes"):
        parse_secret(raw)


@pytest.mark.parametrize("error", [BleakError, GoveeBleError, TimeoutError])
def test_ble_errors_contains(error: type[Exception]) -> None:
    """BLE_ERRORS covers bleak's, the library's and a bare timeout."""
    assert error in BLE_ERRORS


def test_ble_errors_catches_subclasses() -> None:
    """A library subclass is caught by BLE_ERRORS in an except clause."""
    with pytest.raises(BLE_ERRORS):
        raise GoveeBleConnectionError("nope")


async def test_a_logged_in_users_context_is_a_persons(
    hass: HomeAssistant, hass_admin_user: MockUser, hass_read_only_user: MockUser
) -> None:
    """A user of Home Assistant (an admin or not) is a person."""
    assert await async_context_is_person(hass, Context(user_id=hass_admin_user.id))
    assert await async_context_is_person(hass, Context(user_id=hass_read_only_user.id))


@pytest.mark.usefixtures("hass_owner_user")  # so the user deleted below isn't the owner
async def test_contexts_without_a_person(hass: HomeAssistant) -> None:
    """No context, one without a user (an automation, a voice satellite), a system user's
    (Home Assistant Cloud, VoIP) and a user since deleted's are no person's."""
    system = await hass.auth.async_create_system_user("Home Assistant Cloud")
    deleted = await hass.auth.async_create_user("Gone")
    await hass.auth.async_remove_user(deleted)
    assert not await async_context_is_person(hass, None)
    assert not await async_context_is_person(hass, Context())
    assert not await async_context_is_person(hass, Context(user_id=system.id))
    assert not await async_context_is_person(hass, Context(user_id=deleted.id))
