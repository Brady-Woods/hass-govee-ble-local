"""The manifest's Bluetooth matchers: valid for Home Assistant, covering the library's discovery.

Discovery may match more than the installed library supports (a broad name prefix also catches
Govee sensors, and new models reach the matchers before a library release admits them); the
config flow then aborts with ``not_supported`` (see test_config_flow).
"""

from __future__ import annotations

from fnmatch import fnmatchcase
import json
from pathlib import Path

import govee_ble_local.const as lib_const

# Home Assistant's own check, run when it builds the matchers: no wildcard in the first
# LOCAL_NAME_MIN_MATCH_LENGTH (3) characters of a local_name pattern.
from homeassistant.components.bluetooth.match import _local_name_to_index_key
import pytest

MANIFEST = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "govee_ble_local"
    / "manifest.json"
)
MATCHERS: list[dict[str, object]] = json.loads(MANIFEST.read_text(encoding="utf-8"))["bluetooth"]
MANUFACTURER_IDS = {m["manufacturer_id"] for m in MATCHERS if "manufacturer_id" in m}
LOCAL_NAMES = [str(m["local_name"]) for m in MATCHERS if "local_name" in m]
# The library's two-letter "GV" prefix can't be a matcher on its own (HA needs three literal
# characters), so it is covered family by family: GVH / GVR lights, GVB (B7080, B7081, B7082).
GV_FAMILIES = ("GVB", "GVH", "GVR")


def _matches(name: str) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in LOCAL_NAMES)


def test_each_matcher_has_one_key() -> None:
    """One key per matcher: HA ANDs the keys inside one, and these are alternatives."""
    assert all(len(matcher) == 1 for matcher in MATCHERS)


@pytest.mark.parametrize("pattern", LOCAL_NAMES)
def test_local_name_matchers_are_valid_for_home_assistant(pattern: str) -> None:
    _local_name_to_index_key(pattern)  # raises ValueError when the pattern is too broad


def test_manufacturer_ids_cover_the_library() -> None:
    assert set(lib_const.MANUFACTURER_IDS) <= MANUFACTURER_IDS


def test_encrypted_header_variants_are_matched() -> None:
    """0x8841 / 0x8842: broadcast versions 1 and 2 with the encrypted bit set."""
    assert {0x8841, 0x8842} <= MANUFACTURER_IDS


@pytest.mark.parametrize("prefix", lib_const.LOCAL_NAME_PREFIXES)
def test_local_name_prefixes_of_the_library_are_covered(prefix: str) -> None:
    if len(prefix) >= 3:
        assert f"{prefix}*" in LOCAL_NAMES
    else:
        families = [family for family in GV_FAMILIES if family.startswith(prefix)]
        assert families, f"no matcher family covers the {prefix!r} prefix"
        assert all(f"{family}*" in LOCAL_NAMES for family in families)


@pytest.mark.parametrize(
    "name", ["GVB70801A2B", "GVB70811A2B", "GVB70821A2B", "GVH60A6ABCD", "GVR1234ABCD"]
)
def test_gv_named_models_are_matched(name: str) -> None:
    assert _matches(name)
