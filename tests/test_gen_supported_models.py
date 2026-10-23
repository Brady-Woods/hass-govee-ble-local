"""The logic of scripts/gen_supported_models.py, on rows built here (not the library's catalogue).

The README's block itself is checked against the floor library by the script's ``--check``.
"""

from __future__ import annotations

import dataclasses
import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from govee_ble_local import Capability, DeviceProfile


def _generator() -> ModuleType:
    """Load scripts/gen_supported_models.py (a script, not a package module)."""
    path = Path(__file__).resolve().parent.parent / "scripts" / "gen_supported_models.py"
    spec = importlib.util.spec_from_file_location("gen_supported_models", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_model_served_only_at_some_pacts_is_listed_with_them() -> None:
    """A model with no profile without a pact is described at the pacts it is served at.

    Library 1.6.1 has none; its device-list release lists such models in ``supported_skus``
    (``profile_for`` refuses them without a pact). Built here: one profile at each of a model's
    pacts gives one row naming them, different ones a row per pact; the README then says what
    "only at protocol" means.
    """
    generator = _generator()
    light = DeviceProfile(skus=("H0PA1",), capabilities=frozenset({Capability.POWER}))
    segmented = dataclasses.replace(
        light, capabilities=light.capabilities | {Capability.SEGMENTS}, segments=6
    )
    served = {
        "H0PA0": {None: light},
        "H0PA1": {(2, 1): light},
        "H0PA2": {(10, 1): light, (10, 2): light, (10, 3): light},
        "H0PA3": {(10, 1): light, (10, 2): segmented},
    }

    def _profile_for(
        sku: str, *, pact: tuple[int, int] | None = None, allow_modelled: bool = False
    ) -> DeviceProfile | None:
        return served[sku].get(pact)

    _, on_off = generator._describe(light)
    _, segments = generator._describe(segmented)
    with (
        patch.object(generator, "profile_for", _profile_for),
        patch.object(generator, "supported_skus", lambda *, allow_modelled=False: sorted(served)),
    ):
        assert generator._features("H0PA0") == ("Light", on_off, False)
        assert generator._features("H0PA1") == ("Light", f"{on_off}; only at protocol (2,1)", True)
        assert generator._features("H0PA2") == (
            "Light",
            f"{on_off}; only at protocol (10,1), (10,2) or (10,3)",
            True,
        )
        assert generator._features("H0PA3") == (
            "Light",
            f"at protocol (10,1): {on_off}; at protocol (10,2): {segments}",
            True,
        )
        block = generator.render()
    assert f"| H0PA1 | Light | {on_off}; only at protocol (2,1) | Supported |" in block
    assert "A model supported only at some protocol versions is set up once" in block
