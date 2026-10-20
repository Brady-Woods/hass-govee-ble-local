"""The manifest's Bluetooth matchers: valid for Home Assistant, following the library's discovery.

The matchers mirror the installed library's own MANUFACTURER_IDS and LOCAL_NAME_PREFIXES. Matching
isn't support: discovery may catch devices the library can't drive (a Govee sensor, or a model a
later library release admits), and the config flow aborts those with ``not_supported`` (see
test_config_flow).
"""

from __future__ import annotations

from fnmatch import fnmatchcase
import importlib.metadata
import json
from pathlib import Path

import govee_ble_local.const as lib_const
from govee_ble_local.identify import sku_from_local_name

# Home Assistant's own check, run when it builds the matchers: no wildcard in the first
# LOCAL_NAME_MIN_MATCH_LENGTH (3) characters of a local_name pattern.
from homeassistant.components.bluetooth.match import _local_name_to_index_key
from packaging.version import Version
import pytest

MANIFEST = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "govee_ble_local"
    / "manifest.json"
)
MATCHERS: list[dict[str, object]] = json.loads(MANIFEST.read_text(encoding="utf-8"))["bluetooth"]
MANUFACTURER_IDS = {m["manufacturer_id"] for m in MATCHERS if "manufacturer_id" in m}
LOCAL_NAMES = {str(m["local_name"]) for m in MATCHERS if "local_name" in m}
LIBRARY_IDS = set(lib_const.MANUFACTURER_IDS)
# The manifest follows the library's lists as of govee-ble-local 1.4.0; an older library (the
# manifest floor) lists fewer IDs, every one of them still matched.
LIBRARY_IS_CURRENT = Version(importlib.metadata.version("govee-ble-local")) >= Version("1.4.0")
# HA can't match a prefix shorter than three characters, and the library has one: "GV". The
# library reads a plain "GV" name the app's way, as "H" + its next four characters
# (BaseBleProcessor.g), so only "GV" + a digit can name a model ("GV6047..." is an H6047); its own
# GVH / GVR prefixes cover the rest. (B7080 / B7081 / B7082 advertise as "Govee_B7080_..." or
# "GBK_...", not "GVB...".)
DIGITS = "0123456789"


def _expected_local_names() -> set[str]:
    names: set[str] = set()
    for prefix in lib_const.LOCAL_NAME_PREFIXES:
        if len(prefix) >= 3:
            names.add(f"{prefix}*")
        else:
            names |= {f"{prefix}{digit}*" for digit in DIGITS}
    return names


@pytest.mark.parametrize("digit", DIGITS)
def test_a_plain_gv_name_is_read_as_an_h_model(digit: str) -> None:
    """Pins the reading the GV + digit matchers rely on to the library's own parser."""
    assert sku_from_local_name(f"GV{digit}0471A2B") == f"H{digit}047"


def test_each_matcher_has_one_key() -> None:
    """One key per matcher: HA ANDs the keys inside one, and these are alternatives."""
    assert all(len(matcher) == 1 for matcher in MATCHERS)


@pytest.mark.parametrize("pattern", sorted(LOCAL_NAMES))
def test_local_name_matchers_are_valid_for_home_assistant(pattern: str) -> None:
    _local_name_to_index_key(pattern)  # raises ValueError when the pattern is too broad


def test_manufacturer_ids_follow_the_library() -> None:
    if LIBRARY_IS_CURRENT:
        assert MANUFACTURER_IDS == LIBRARY_IDS
    else:
        assert LIBRARY_IDS < MANUFACTURER_IDS


def test_local_names_follow_the_library() -> None:
    assert _expected_local_names() == LOCAL_NAMES


@pytest.mark.parametrize(
    ("name", "matched"),
    [
        ("GV60471A2B", True),  # plain GV + digit: an H6047
        ("GVH60A6ABCD", True),
        ("GVR1234ABCD", True),
        ("Govee_B7080_1A2B", True),  # how B7080 / B7081 / B7082 advertise
        ("GBK_B7081_1A2B", True),
        ("GVB70801A2B", False),  # read as "HB708", a model that doesn't exist
    ],
)
def test_advert_names_are_matched(name: str, matched: bool) -> None:
    assert any(fnmatchcase(name, pattern) for pattern in LOCAL_NAMES) is matched
