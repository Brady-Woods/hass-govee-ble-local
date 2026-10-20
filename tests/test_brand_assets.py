"""Brand images: the copy that ships and the brands/ staging copy stay identical.

Home Assistant 2026.3+ serves custom_components/govee_ble_local/brand/; brands/ keeps a
copy next to the generator. Regenerating (or replacing the art) must update both.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "custom_components" / "govee_ble_local" / "brand"
STAGING = ROOT / "brands"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "make_placeholders", STAGING / "make_placeholders.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="generator")
def generator_fixture() -> ModuleType:
    return _load_generator()


def test_copies_hold_the_same_images(generator: ModuleType) -> None:
    names = sorted(generator.SIZES)
    assert sorted(p.name for p in SHIPPED.glob("*.png")) == names
    assert sorted(p.name for p in STAGING.glob("*.png")) == names
    for name in names:
        assert (SHIPPED / name).read_bytes() == (STAGING / name).read_bytes(), (
            f"{name}: brand/ and brands/ differ; update both copies"
        )


def test_generator_writes_the_shipped_copy_first(generator: ModuleType) -> None:
    assert generator.OUT_DIRS == (SHIPPED, STAGING)


def test_generator_writes_every_copy(
    generator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dirs = (tmp_path / "shipped", tmp_path / "staging")
    for out_dir in out_dirs:
        out_dir.mkdir()
    monkeypatch.setattr(generator, "OUT_DIRS", out_dirs)
    # Rendering is slow pure Python and not what this checks.
    monkeypatch.setattr(generator, "render", lambda w, h: f"{w}x{h}".encode())

    generator.main()

    for out_dir in out_dirs:
        assert {p.name: p.read_bytes() for p in out_dir.iterdir()} == {
            name: f"{w}x{h}".encode() for name, (w, h) in generator.SIZES.items()
        }
