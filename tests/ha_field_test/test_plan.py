"""The field-test plan and restore: built from capabilities, one table row per shape.

All identifiers are made up (builders.py): MACs from the RFC 7042 documentation range.
"""

from __future__ import annotations

from typing import Any

import pytest

from tools.ha_field_test import colour
from tools.ha_field_test.plan import PLUG_CAVEAT, TINT_RGB, Kind, build_plan, restore_calls

from .builders import device, light, outlet, plug, segment, zone_light

FIXTURE_ON = [
    "light.off",
    "light.on",
    "light.brightness_25",
    "light.brightness_100",
    "light.rgb_ff0000",
    "light.white_tint_pick",
    "light.kelvin_min",
    "light.kelvin_mid",
    "light.kelvin_max",
    "light.kelvin_above_range",
    "light.scene",
    "light.scene_off",
    "light.scene_unknown",
]
ZONE = ["off", "on", "brightness_50", "rgb_00ff00", "kelvin_mid"]


def _entity(
    role: str,
    snap: dict,
    *,
    domain: str = "light",
    zone: str | None = None,
    segment: int | None = None,
) -> dict:
    return {"role": role, "domain": domain, "zone": zone, "segment": segment, "snapshot": snap}


def _record(entities: dict, *, library: dict | None = None, lit: dict | None = None) -> dict:
    return {"entities": entities, "library_state": library or {}, "lit": lit or {}}


LIT_RGB = {"state": "on", "color_mode": "rgb", "rgb_color": [1, 2, 3], "brightness": 50}
SEGMENTS = [{"index": 0, "rgb": [9, 9, 9], "brightness": 40}, {"index": 1, "rgb": [0, 0, 0]}]
CASES: dict[str, dict[str, Any]] = {
    # The plan: a device's steps, and {step id: (kind, expected mode, kelvin, rgb)}.
    "rgb + colour temperature + scenes, found on": {
        "device": device(light()),
        "steps": FIXTURE_ON,
        "expect": {
            # The app's 4000 K tint colour picked as RGB goes out as 4000 K.
            "light.white_tint_pick": (Kind.CONVERSION, "color_temp", 4000, None),
            # Above the range: the integration clamps to the top of it.
            "light.kelvin_above_range": (Kind.CONVERSION, "color_temp", 6500, None),
            "light.scene_unknown": (Kind.REFUSAL, None, None, None),
        },
    },
    "found off: on, off, on first": {
        "device": device(light(state="off")),
        "steps": ["light.on_first", *FIXTURE_ON],
    },
    "rgb only: Home Assistant turns a Kelvin into RGB": {
        "device": device(light(modes=("rgb",), span=None)),
        "steps": [*FIXTURE_ON[:6], "light.kelvin_on_rgb_light", *FIXTURE_ON[10:]],
        "expect": {
            "light.kelvin_on_rgb_light": (Kind.CONVERSION, "rgb", None, colour.kelvin_as_rgb(2700)),
            "light.white_tint_pick": (Kind.CHANGE, "rgb", None, TINT_RGB),
        },
    },
    "colour temperature only: RGB becomes a clamped Kelvin": {
        "device": device(light(modes=("color_temp",))),
        "steps": [*FIXTURE_ON[:4], "light.rgb_on_ct_light", *FIXTURE_ON[6:]],
        "expect": {
            "light.rgb_on_ct_light": (
                Kind.CONVERSION,
                "color_temp",
                max(2700, min(6500, colour.rgb_as_kelvin((255, 0, 0)))),
                None,
            )
        },
    },
    "on/off only: Home Assistant drops what it can't do": {
        "device": device(light(modes=("onoff",), span=None, effects=None)),
        "steps": [
            "light.off",
            "light.on",
            "light.brightness_dropped",
            "light.rgb_dropped",
            "light.kelvin_dropped",
            "light.scene_dropped",
        ],
        "expect": {"light.rgb_dropped": (Kind.CONVERSION, None, None, None)},
    },
    "zones after the fixture, in the device's zone order": {
        "device": device(
            light(), zone_light("background"), zone_light("main"), zones=("main", "background")
        ),
        "steps": [
            *FIXTURE_ON,
            *(f"zone_main.{s}" for s in ZONE),
            *(f"zone_background.{s}" for s in ZONE),
        ],
    },
    "an on/off zone (no segments): power, and what Home Assistant drops": {
        "device": device(light(), zone_light("main", modes=("onoff",), span=None), zones=("main",)),
        "steps": [
            *FIXTURE_ON,
            "zone_main.off",
            "zone_main.on",
            "zone_main.brightness_dropped",
            "zone_main.rgb_dropped",
        ],
        "expect": {"zone_main.rgb_dropped": (Kind.CONVERSION, None, None, None)},
    },
    "enabled segments only; their Kelvin may be refused": {
        "device": device(light(), segment(0, enabled=True, state="on"), segment(1)),
        "steps": [
            *FIXTURE_ON,
            "segment_1.rgb_ff00ff",
            "segment_1.brightness_50",
            "segment_1.kelvin_mid",
            "segment_1.off",
            "segment_1.on",
        ],
        "expect": {"segment_1.kelvin_mid": (Kind.CHANGE_OR_REFUSAL, "color_temp", 4600, None)},
    },
    "a plug, only once named": {
        "device": device(plug(state="on"), label="H5083 #1", sku="H5083"),
        "plugs": True,
        "steps": ["plug.off", "plug.on"],
    },
    "a per-outlet relay is a plug too": {
        "device": device(
            outlet("1", "on"), outlet("2", "off"), label="H5086 #1", sku="H5086", zones=("1", "2")
        ),
        "plugs": True,
        "steps": ["outlet_1.off", "outlet_1.on", "outlet_2.on", "outlet_2.off"],
    },
    # The restore of a journal record: colours while lit, then whatever was off switched
    # off; a fixture found on first.
    "restore: lit fixture, one zone off": {
        "record": _record(
            {
                "f": _entity(
                    "light",
                    {
                        "state": "on",
                        "color_mode": "rgb",
                        "rgb_color": [255, 111, 30],
                        "brightness": 128,
                    },
                ),
                "z_main": _entity(
                    "zone_light",
                    {"state": "on", "color_mode": "color_temp", "color_temp_kelvin": 3000},
                    zone="main",
                ),
                "z_back": _entity("zone_light", {"state": "off"}, zone="background"),
            },
            lit={"z_back": LIT_RGB},
        ),
        "calls": [
            ("f", "turn_on", {"rgb_color": [255, 111, 30], "brightness": 128}),
            ("z_main", "turn_on", {"color_temp_kelvin": 3000}),
            ("z_back", "turn_on", {"rgb_color": [1, 2, 3], "brightness": 50}),
            ("z_back", "turn_off", {}),
        ],
    },
    # Found off: lit with the colour it kept, zones and segments given theirs back, then
    # off. A segment is blacked out only if its own colour was black.
    "restore: fixture found off, zones and segments": {
        "record": _record(
            {
                "f": _entity("light", {"state": "off"}),
                "z_main": _entity("zone_light", {"state": "off"}, zone="main"),
                "s0": _entity("segment", {"state": "off"}, segment=0),
                "s1": _entity("segment", {"state": "off"}, segment=1),
            },
            library={"brightness": 5, "color_temp_kelvin": 2700, "segments": SEGMENTS},
            lit={"z_main": LIT_RGB},
        ),
        "calls": [
            ("f", "turn_on", {"color_temp_kelvin": 2700, "brightness_pct": 5}),
            ("z_main", "turn_on", {"rgb_color": [1, 2, 3], "brightness": 50}),
            ("s0", "turn_on", {"rgb_color": [9, 9, 9], "brightness": 102}),
            ("s1", "turn_off", {}),
            ("f", "turn_off", {}),
        ],
    },
    # An on/off zone found on has no colour to give back: its power alone, which the
    # fixture's colour restore wouldn't send. A zone in Kelvin gets its Kelvin back.
    "restore: on/off zone and a zone in Kelvin, fixture found on": {
        "record": {
            **_record(
                {
                    "f": _entity(
                        "light",
                        {
                            "state": "on",
                            "color_mode": "color_temp",
                            "color_temp_kelvin": 3000,
                            "brightness": 128,
                        },
                    ),
                    "z_left": _entity(
                        "zone_light",
                        {
                            "state": "on",
                            "color_mode": "rgb",
                            "rgb_color": [9, 9, 9],
                            "brightness": 64,
                        },
                        zone="left",
                    ),
                    "z_right": _entity("zone_light", {"state": "on"}, zone="right"),
                },
                library={"zone_color_temp_kelvin": {"0": 2200}},
            ),
            "zones": ["left", "right"],
        },
        "calls": [
            ("f", "turn_on", {"color_temp_kelvin": 3000, "brightness": 128}),
            ("z_left", "turn_on", {"color_temp_kelvin": 2200, "brightness": 64}),
            ("z_right", "turn_on", {}),
        ],
    },
    "restore: fixture found off, nothing kept": {
        "record": _record({"f": _entity("light", {"state": "off"})}),
        "calls": [("f", "turn_off", {})],
    },
    "restore: a scene": {
        "record": _record(
            {"f": _entity("light", {"state": "on", "effect": "Aurora", "brightness": 200})}
        ),
        "calls": [("f", "turn_on", {"effect": "Aurora", "brightness": 200})],
    },
    "restore: relays as found": {
        "record": _record(
            {
                "p": _entity("plug", {"state": "off"}, domain="switch"),
                "o1": _entity("zone_switch", {"state": "on"}, domain="switch", zone="1"),
            }
        ),
        "calls": [("o1", "turn_on", {}), ("p", "turn_off", {})],
    },
}


@pytest.mark.parametrize("case", CASES)
def test_plan_and_restore(case: str) -> None:
    spec = CASES[case]
    if "record" in spec:
        calls = restore_calls(spec["record"])

        assert [(c.entity_id, c.service, dict(c.data)) for c in calls] == spec["calls"]
        # A call that says what it leaves is skipped while its entity already shows that, so
        # a device already as found is left alone; only lighting an off fixture, and
        # switching it off again, always go out.
        unconditional = [c for c in calls if c.target is None]
        assert all(c.entity_id == "f" and c.why != "as found" for c in unconditional)
        return
    dev = spec["device"]

    plan = build_plan(dev, plug_allowed=spec.get("plugs", False))

    assert [s.step_id for s in plan.steps] == spec["steps"]
    by_id = {s.step_id: s for s in plan.steps}
    for step_id, (kind, mode, kelvin, rgb) in spec.get("expect", {}).items():
        step = by_id[step_id]
        assert (step.kind, step.expect.color_mode, step.expect.kelvin, step.expect.rgb) == (
            kind,
            mode,
            kelvin,
            rgb,
        )
    if dev.is_plug:
        # Without the user's consent a plug is never planned at all.
        assert build_plan(dev).steps == []
        assert build_plan(dev).excluded == "a plug the user didn't confirm for toggling"
        assert dev.plug is None or PLUG_CAVEAT in plan.caveats
    assert build_plan(device(light(state="unavailable"))).steps == []
