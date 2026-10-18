"""Manifest requirement policy (CLAUDE.md): one `govee-ble-local>=<floor>,<<next major>` range.

An exact pin made 1.4.0 fail to load on Home Assistant 2026.10 (govee-ble-local#8); this keeps
the requirement a semver range over the library's current major series.
"""

from __future__ import annotations

import json
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version
import pytest

MANIFEST = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "govee_ble_local"
    / "manifest.json"
)


def _requirements() -> list[str]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["requirements"]


def test_only_requirement_is_the_library() -> None:
    reqs = _requirements()
    assert [Requirement(r).name for r in reqs] == ["govee-ble-local"], (
        "the manifest may only require govee-ble-local; Home Assistant's own dependencies "
        "come from Home Assistant (CLAUDE.md rule 4)"
    )


def _assert_semver_range(requirement: str) -> None:
    req = Requirement(requirement)
    assert not req.extras, (
        f"{req}: an extra installs more packages into Home Assistant's environment "
        "(CLAUDE.md rules 1 and 4)"
    )
    # A dict keyed by operator would hide a second bound (`>=1.2.1,<1.5,<2`).
    assert len(req.specifier) == 2, f"{req}: needs exactly one '>=' and one '<'"
    specs = {s.operator: Version(s.version) for s in req.specifier}
    assert sorted(specs) == ["<", ">="], (
        f"{req}: must be '>=<floor>,<<next major>', never an exact pin (CLAUDE.md rule 1)"
    )
    floor, ceiling = specs[">="], specs["<"]
    assert ceiling == Version(str(floor.major + 1)), (
        f"{req}: the upper bound must be the floor's next major ({floor.major + 1}), "
        "so every non-breaking library release is accepted"
    )
    assert not req.marker
    assert not req.url


def test_library_requirement_is_a_semver_range() -> None:
    _assert_semver_range(_requirements()[0])


@pytest.mark.parametrize(
    "requirement",
    [
        "govee-ble-local==1.2.1",
        "govee-ble-local>=1.2.1",
        "govee-ble-local>=1.2.1,<3",
        "govee-ble-local>=1.2.1,<1.5,<2",
        "govee-ble-local>=1.2.1,<2,<1.5",
        "govee-ble-local>=1.0,>=1.2.1,<2",
        "govee-ble-local[test]>=1.2.1,<2",
        'govee-ble-local>=1.2.1,<2; python_version >= "3.13"',
    ],
)
def test_policy_rejects_off_policy_requirements(requirement: str) -> None:
    with pytest.raises(AssertionError):
        _assert_semver_range(requirement)
