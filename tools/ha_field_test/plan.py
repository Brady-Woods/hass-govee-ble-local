"""The per-entity test plan and the restore plan, derived from capabilities (pure).

Nothing here knows a model: each entity's steps come from what Home Assistant says the
entity supports (its ``supported_color_modes``, Kelvin range, ``effect_list`` and
``supported_features``) and, for the device, what the integration's diagnostics say the
library offers. A step also says what it should leave behind, including the refusals and
conversions Home Assistant or the integration should make (a Kelvin to a light without
COLOR_TEMP, a scene the device doesn't have), so those are recorded as expected outcomes,
not failures.

The order follows the integration's behaviour:

* Power first, ending on, so every later step finds the fixture lit; the zones after the
  fixture (a fixture power-on relights every zone), the segments after the zones (a
  segment of a zone that's off stays dark).
* A plain fixture ``turn_on`` always sends power, but a colour or brightness change of a
  lit fixture doesn't: each colour step therefore tests the change alone.
* The restore sets colours while the fixture is lit and switches it off last, because an
  off fixture keeps its colours and lights back up in them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from . import colour
from .inventory import (
    ROLE_FIXTURE,
    ROLE_PLUG,
    ROLE_SEGMENT,
    ROLE_ZONE_LIGHT,
    ROLE_ZONE_SWITCH,
    DeviceDesc,
    EntityDesc,
)

RED: colour.RGB = (255, 0, 0)
GREEN: colour.RGB = (0, 255, 0)
MAGENTA: colour.RGB = (255, 0, 255)
# The Govee app's 4000 K tint colour (0xFFD5A1). Picked as RGB, the library sends it as the
# colour temperature on a device whose Kelvin range holds 4000 K, as the app does
# (govee-ble-local wire/build.py ``rgb_as_kelvin``), and reports that Kelvin.
TINT_RGB: colour.RGB = (255, 213, 161)
TINT_KELVIN = 4000
# A scene no device has: the integration refuses it (light.py, ``unknown_effect``).
NO_SUCH_SCENE = "Field test: no such scene"
OUT_OF_RANGE_KELVIN = 1000  # added to the top of the range: the integration clamps it
LOW_BRIGHTNESS_PCT, HIGH_BRIGHTNESS_PCT, ZONE_BRIGHTNESS_PCT = 25, 100, 50
EFFECT_OFF = "off"
FEATURE_EFFECT = 4  # LightEntityFeature.EFFECT
# HA's brightness_supported: any colour mode but these.
_NO_BRIGHTNESS_MODES = frozenset({"onoff", "unknown"})


class Kind(StrEnum):
    """What a step checks."""

    CHANGE = "change"  # the entity should end up in the requested state
    CONVERSION = "conversion"  # HA or the integration should turn the request into another
    REFUSAL = "refusal"  # the service call should fail, changing nothing
    CHANGE_OR_REFUSAL = "change or refusal"  # either outcome is an answer (see _either)


@dataclass(frozen=True)
class Expect:
    """What a step should leave behind (None: not checked)."""

    state: str | None = None
    color_mode: str | None = None
    brightness: int | None = None
    rgb: colour.RGB | None = None
    kelvin: int | None = None
    effect: str | None = None
    error_key: str | None = None  # the refusal's translation key (or WebSocket error code)
    unchanged: bool = False  # a refusal: the state must be as it was

    def describe(self) -> str:
        """Return a short text of the expectation, for the plan."""
        if self.error_key is not None:
            return f"refused ({self.error_key}), state unchanged"
        parts = [
            f"{name}={value}"
            for name, value in (
                ("state", self.state),
                ("mode", self.color_mode),
                ("brightness", self.brightness),
                ("rgb", self.rgb),
                ("kelvin", self.kelvin),
                ("effect", self.effect),
            )
            if value is not None
        ]
        return ", ".join(parts) or "accepted"


@dataclass(frozen=True)
class Step:
    """One service call to one entity, and what it should do."""

    step_id: str
    entity: EntityDesc
    service: str
    data: Mapping[str, Any]
    expect: Expect
    kind: Kind = Kind.CHANGE
    note: str = ""
    power: bool = False  # an on/off step: confirmed from the advertisement when optimistic

    @property
    def domain(self) -> str:
        """The service domain: the entity's own."""
        return self.entity.domain

    def call_data(self) -> dict[str, Any]:
        """Return the service data, with the entity."""
        return {"entity_id": self.entity.entity_id, **self.data}


@dataclass
class DevicePlan:
    """The steps for one device, what was left out and why, and the restore caveats."""

    device: DeviceDesc
    steps: list[Step] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    excluded: str | None = None  # why the whole device is left out, if it is


def brightness_of(pct: int) -> int:
    """Return the HA brightness Home Assistant and the integration settle on for ``pct`` %.

    HA turns brightness_pct into round(255 * pct / 100); the integration sends
    ``max(1, round(b / 255 * 100))`` and shows ``round(pct / 100 * 255)`` back.
    """
    sent = max(1, round(round(255 * pct / 100) / 255 * 100))
    return round(sent / 100 * 255)


def _modes(entity: EntityDesc) -> set[str]:
    return {str(m) for m in entity.attributes.get("supported_color_modes") or ()}


def kelvin_span(entity: EntityDesc) -> tuple[int, int] | None:
    """Return the entity's Kelvin range, if it offers colour temperature."""
    if "color_temp" not in _modes(entity):
        return None
    lo = entity.attributes.get("min_color_temp_kelvin")
    hi = entity.attributes.get("max_color_temp_kelvin")
    if not isinstance(lo, int) or not isinstance(hi, int) or lo > hi:
        return None
    return lo, hi


def has_brightness(entity: EntityDesc) -> bool:
    """HA's brightness_supported."""
    modes = _modes(entity)
    return bool(modes - _NO_BRIGHTNESS_MODES)


def has_rgb(entity: EntityDesc) -> bool:
    """Whether the entity takes RGB (the integration's lights use ColorMode.RGB)."""
    return "rgb" in _modes(entity)


def scenes(entity: EntityDesc) -> list[str]:
    """Return the scenes a light offers (its effect list without "off"), if any."""
    features = entity.attributes.get("supported_features") or 0
    if not isinstance(features, int) or not features & FEATURE_EFFECT:
        return []
    return [str(e) for e in entity.attributes.get("effect_list") or () if e != EFFECT_OFF]


def _power_steps(entity: EntityDesc, prefix: str) -> list[Step]:
    """Off then on (from on), or on, off, on (from off): both transitions, ending lit."""
    on = Step(f"{prefix}.on", entity, "turn_on", {}, Expect(state="on"), power=True)
    off = Step(f"{prefix}.off", entity, "turn_off", {}, Expect(state="off"), power=True)
    if entity.state == "on":
        return [off, on]
    first_on = Step(f"{prefix}.on_first", entity, "turn_on", {}, Expect(state="on"), power=True)
    return [first_on, off, on]


def _colour_steps(entity: EntityDesc, prefix: str, colours: list[colour.RGB]) -> list[Step]:
    """RGB steps, or the conversion HA makes for a light without RGB."""
    span = kelvin_span(entity)
    if has_rgb(entity):
        return [
            Step(
                f"{prefix}.rgb_{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}",
                entity,
                "turn_on",
                {"rgb_color": list(rgb)},
                Expect(state="on", color_mode="rgb", rgb=rgb),
            )
            for rgb in colours
        ]
    rgb = colours[0]
    if span is not None:
        kelvin = max(span[0], min(span[1], colour.rgb_as_kelvin(rgb)))
        return [
            Step(
                f"{prefix}.rgb_on_ct_light",
                entity,
                "turn_on",
                {"rgb_color": list(rgb)},
                Expect(state="on", color_mode="color_temp", kelvin=kelvin),
                Kind.CONVERSION,
                "no RGB mode: HA converts the colour to a Kelvin (xy, McCamy), "
                "the integration clamps it to the range",
            )
        ]
    return [
        Step(
            f"{prefix}.rgb_dropped",
            entity,
            "turn_on",
            {"rgb_color": list(rgb)},
            Expect(state="on"),
            Kind.CONVERSION,
            "no colour mode: HA drops the colour (a plain turn_on)",
        )
    ]


def _tint_step(entity: EntityDesc, prefix: str) -> Step:
    """Return the white tint pick: the app's 4000 K tint colour picked as RGB."""
    span = kelvin_span(entity)
    if span is not None and span[0] <= TINT_KELVIN <= span[1]:
        return Step(
            f"{prefix}.white_tint_pick",
            entity,
            "turn_on",
            {"rgb_color": list(TINT_RGB)},
            Expect(state="on", color_mode="color_temp", kelvin=TINT_KELVIN),
            Kind.CONVERSION,
            "a Kelvin tint colour picked as RGB goes out as that colour temperature, "
            "as the Govee app sends it (library rgb_as_kelvin)",
        )
    return Step(
        f"{prefix}.white_tint_pick",
        entity,
        "turn_on",
        {"rgb_color": list(TINT_RGB)},
        Expect(state="on", color_mode="rgb", rgb=TINT_RGB),
        note="no Kelvin range holding 4000 K: the tint stays an RGB colour",
    )


def _kelvin_steps(entity: EntityDesc, prefix: str, *, full: bool) -> list[Step]:
    """Min, mid and max of the range (or mid only), and the clamp; or HA's conversion."""
    span = kelvin_span(entity)
    if span is None:
        kelvin = 2700
        if has_rgb(entity):
            rgb = colour.kelvin_as_rgb(kelvin)
            return [
                Step(
                    f"{prefix}.kelvin_on_rgb_light",
                    entity,
                    "turn_on",
                    {"color_temp_kelvin": kelvin},
                    Expect(state="on", color_mode="rgb", rgb=rgb),
                    Kind.CONVERSION,
                    "no COLOR_TEMP mode: HA converts the Kelvin to an RGB colour",
                )
            ]
        return [
            Step(
                f"{prefix}.kelvin_dropped",
                entity,
                "turn_on",
                {"color_temp_kelvin": kelvin},
                Expect(state="on"),
                Kind.CONVERSION,
                "no colour mode: HA drops the Kelvin (a plain turn_on)",
            )
        ]
    lo, hi = span
    mid = (lo + hi) // 2
    points = [("min", lo), ("mid", mid), ("max", hi)] if full else [("mid", mid)]
    steps = [
        Step(
            f"{prefix}.kelvin_{name}",
            entity,
            "turn_on",
            {"color_temp_kelvin": kelvin},
            Expect(state="on", color_mode="color_temp", kelvin=kelvin),
        )
        for name, kelvin in points
    ]
    if full:
        steps.append(
            Step(
                f"{prefix}.kelvin_above_range",
                entity,
                "turn_on",
                {"color_temp_kelvin": hi + OUT_OF_RANGE_KELVIN},
                Expect(state="on", color_mode="color_temp", kelvin=hi),
                Kind.CONVERSION,
                "above the range: the integration clamps it to the maximum",
            )
        )
    return steps


def _brightness_steps(entity: EntityDesc, prefix: str, levels: list[int]) -> list[Step]:
    if has_brightness(entity):
        return [
            Step(
                f"{prefix}.brightness_{pct}",
                entity,
                "turn_on",
                {"brightness_pct": pct},
                Expect(state="on", brightness=brightness_of(pct)),
            )
            for pct in levels
        ]
    return [
        Step(
            f"{prefix}.brightness_dropped",
            entity,
            "turn_on",
            {"brightness_pct": levels[0]},
            Expect(state="on"),
            Kind.CONVERSION,
            "an on/off light: HA drops the brightness (a plain turn_on)",
        )
    ]


def _scene_steps(entity: EntityDesc, prefix: str) -> list[Step]:
    offered = scenes(entity)
    if not offered:
        return []
    scene = offered[0]
    return [
        Step(
            f"{prefix}.scene",
            entity,
            "turn_on",
            {"effect": scene},
            Expect(state="on", effect=scene),
            note=f"the first of {len(offered)} scenes offered",
        ),
        Step(
            f"{prefix}.scene_off",
            entity,
            "turn_on",
            {"effect": EFFECT_OFF},
            Expect(state="on", effect=EFFECT_OFF),
            note="leaves the scene for a solid colour (light.py _exit_scene_colour)",
        ),
        Step(
            f"{prefix}.scene_unknown",
            entity,
            "turn_on",
            {"effect": NO_SUCH_SCENE},
            Expect(error_key="unknown_effect", unchanged=True),
            Kind.REFUSAL,
            "the integration refuses a scene the device doesn't offer",
        ),
    ]


def _fixture_steps(entity: EntityDesc) -> list[Step]:
    prefix = "light"
    steps = _power_steps(entity, prefix)
    steps += _brightness_steps(entity, prefix, [LOW_BRIGHTNESS_PCT, HIGH_BRIGHTNESS_PCT])
    steps += _colour_steps(entity, prefix, [RED])
    if has_rgb(entity):
        steps.append(_tint_step(entity, prefix))
    steps += _kelvin_steps(entity, prefix, full=True)
    steps += _scene_steps(entity, prefix)
    if not scenes(entity):
        steps.append(
            Step(
                f"{prefix}.scene_dropped",
                entity,
                "turn_on",
                {"effect": NO_SUCH_SCENE},
                Expect(state="on"),
                Kind.CONVERSION,
                "no effects offered: HA drops the effect (a plain turn_on)",
            )
        )
    return steps


def _zone_power_steps(entity: EntityDesc, prefix: str) -> list[Step]:
    """Return a zone's off then on: the fixture's power-on before them lit every zone."""
    return [
        Step(f"{prefix}.off", entity, "turn_off", {}, Expect(state="off"), power=True),
        Step(f"{prefix}.on", entity, "turn_on", {}, Expect(state="on"), power=True),
    ]


def _zone_light_steps(entity: EntityDesc) -> list[Step]:
    """Return a zone light's steps; an on/off zone (no segments) gets what HA drops for it."""
    prefix = f"zone_{entity.zone}"
    steps = _zone_power_steps(entity, prefix)
    steps += _brightness_steps(entity, prefix, [ZONE_BRIGHTNESS_PCT])
    steps += _colour_steps(entity, prefix, [GREEN])
    if has_rgb(entity) or kelvin_span(entity) is not None:
        steps += _kelvin_steps(entity, prefix, full=False)
    return steps


def _either(step: Step, note: str) -> Step:
    """Return ``step`` accepted as done, or as refused with ``not_supported`` and no change."""
    expect = Expect(**{**step.expect.__dict__, "error_key": "not_supported"})
    return Step(
        step.step_id,
        step.entity,
        step.service,
        step.data,
        expect,
        Kind.CHANGE_OR_REFUSAL,
        note,
        step.power,
    )


def _segment_steps(entity: EntityDesc) -> list[Step]:
    prefix = f"segment_{(entity.segment or 0) + 1}"
    steps: list[Step] = []
    if has_rgb(entity):
        steps += _colour_steps(entity, prefix, [MAGENTA])
    if has_brightness(entity):
        steps += _brightness_steps(entity, prefix, [ZONE_BRIGHTNESS_PCT])
    for step in _kelvin_steps(entity, prefix, full=False) if has_rgb(entity) else []:
        # A library release may refuse a segment's Kelvin on a model whose firmware can't
        # take it (not_supported): that is an answer, not a failure.
        steps.append(
            _either(step, "a library that can't set this model's segment Kelvin refuses it")
            if step.kind is Kind.CHANGE
            else step
        )
    steps += [
        Step(
            f"{prefix}.off",
            entity,
            "turn_off",
            {},
            Expect(state="off"),
            note="blacks the segment out (no per-segment power)",
        ),
        Step(
            f"{prefix}.on",
            entity,
            "turn_on",
            {},
            Expect(state="on"),
            note="brings back its last colour",
        ),
    ]
    return steps


def _switch_steps(entity: EntityDesc, prefix: str) -> list[Step]:
    """Return a relay's two transitions, ending as it started (a plug, an outlet)."""
    on = Step(f"{prefix}.on", entity, "turn_on", {}, Expect(state="on"), power=True)
    off = Step(f"{prefix}.off", entity, "turn_off", {}, Expect(state="off"), power=True)
    return [off, on] if entity.state == "on" else [on, off]


def _segment_caveat(device: DeviceDesc) -> str | None:
    """Return a restore caveat when segments had different colours and some are disabled."""
    segments = device.library_state.get("segments") or []
    colours = {tuple(s.get("rgb") or ()) for s in segments if isinstance(s, Mapping)}
    disabled = [e for e in device.role(ROLE_SEGMENT) if not e.enabled]
    if len(colours) > 1 and disabled:
        return (
            f"its {len(segments)} segments had {len(colours)} colours at the start and "
            f"{len(disabled)} segment lights are disabled: Home Assistant can only put back "
            "the enabled ones and each zone's colour, so the check reports what differs"
        )
    return None


PLUG_CAVEAT = (
    "a plug's one switch drives all of its outlets: an outlet that was off comes back on "
    "with the others (name only a plug whose outlets were all alike)"
)


def _in_zone_order(device: DeviceDesc, entities: list[EntityDesc]) -> list[EntityDesc]:
    """Return zone entities in the device's own zone order (the library's, power-index order)."""
    order = {name: i for i, name in enumerate(device.zones)}
    return sorted(entities, key=lambda e: (order.get(e.zone or "", len(order)), e.key))


def _plan_segments(device: DeviceDesc, plan: DevicePlan) -> None:
    """Add the enabled segment lights' steps; note the disabled ones."""
    segments = device.role(ROLE_SEGMENT)
    enabled = [s for s in segments if s.available]
    for segment in enabled:
        plan.steps += _segment_steps(segment)
    if segments and not enabled:
        plan.skipped.append(
            f"segments: none of its {len(segments)} segment lights is enabled "
            "(they are disabled by default)"
        )
    elif len(enabled) < len(segments):
        plan.skipped.append(
            f"segments: {len(segments) - len(enabled)} of {len(segments)} segment lights disabled"
        )


def _plan_relays(device: DeviceDesc, plan: DevicePlan) -> None:
    """Add a plug's or a per-outlet relay's steps (only once the user named it)."""
    if (plug := device.plug) is not None:
        plan.steps += _switch_steps(plug, "plug")
        plan.caveats.append(PLUG_CAVEAT)
    for outlet in _in_zone_order(device, device.outlets):
        if outlet.available:
            plan.steps += _switch_steps(outlet, f"outlet_{outlet.zone}")
        else:
            plan.skipped.append(f"outlet {outlet.zone}: no state ({outlet.state})")


def build_plan(device: DeviceDesc, *, plug_allowed: bool = False) -> DevicePlan:
    """Return the steps for one device, from its entities' capabilities."""
    plan = DevicePlan(device)
    if not device.available:
        plan.excluded = "not testable now (see the inventory)"
        return plan
    if device.is_plug:
        if not plug_allowed:
            plan.excluded = "a plug the user didn't confirm for toggling"
            return plan
        _plan_relays(device, plan)
    if (fixture := device.fixture) is not None:
        plan.steps += _fixture_steps(fixture)
        for zone in _in_zone_order(device, device.role(ROLE_ZONE_LIGHT)):
            if zone.available:
                plan.steps += _zone_light_steps(zone)
            else:
                plan.skipped.append(f"zone light {zone.zone}: no state ({zone.state})")
        _plan_segments(device, plan)
    if (caveat := _segment_caveat(device)) is not None:
        plan.caveats.append(caveat)
    if fixture is not None and fixture.state == "off" and device.library_state.get("scene_code"):
        plan.caveats.append(
            "it was off with a scene set: the scene isn't put back (only its colours are known)"
        )
    return plan


# -- restore ----------------------------------------------------------------------------

BLACK = [0, 0, 0]


def _light_restore_data(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return the turn_on data that puts a lit light back as ``snapshot`` shows it."""
    data: dict[str, Any] = {}
    effect = snapshot.get("effect")
    if effect and effect != EFFECT_OFF:
        data["effect"] = effect
    elif snapshot.get("color_mode") == "color_temp" and snapshot.get("color_temp_kelvin"):
        data["color_temp_kelvin"] = snapshot["color_temp_kelvin"]
    elif snapshot.get("color_mode") == "rgb" and snapshot.get("rgb_color"):
        data["rgb_color"] = list(snapshot["rgb_color"])
    if isinstance(snapshot.get("brightness"), int) and snapshot["brightness"] > 0:
        data["brightness"] = snapshot["brightness"]
    return data


def _library_restore_data(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the turn_on data for the colours an off fixture keeps (from diagnostics)."""
    data: dict[str, Any] = {}
    if state.get("color_temp_kelvin"):
        data["color_temp_kelvin"] = state["color_temp_kelvin"]
    elif state.get("rgb_color"):
        data["rgb_color"] = list(state["rgb_color"])
    if isinstance(state.get("brightness"), int) and state["brightness"] > 0:
        data["brightness_pct"] = state["brightness"]
    return data


@dataclass(frozen=True)
class RestoreCall:
    """One call of the restore; skipped while the entity already shows ``target``."""

    entity_id: str
    domain: str
    service: str
    data: Mapping[str, Any]
    why: str
    target: Mapping[str, Any] | None = None


def _library_segment(start: Mapping[str, Any], index: int | None) -> Mapping[str, Any]:
    """Return a segment's colour and brightness at the start, from the diagnostics."""
    for segment in (start.get("library_state") or {}).get("segments") or ():
        if isinstance(segment, Mapping) and segment.get("index") == index:
            return segment
    return {}


def _colour_target(
    entity_id: str, info: Mapping[str, Any], start: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """Return what a zone or segment light showed lit, before the test changed it.

    Its own snapshot if it was lit; else the snapshot taken as the test's first power-on
    lit it (``start["lit"]``, before any colour step); else, for a segment, its colour and
    brightness in the library's state. None when none is known.
    """
    snap: Mapping[str, Any] = info["snapshot"]
    if info["role"] == ROLE_ZONE_LIGHT and (kelvin := zone_kelvin(start, info.get("zone"))):
        # The library knows the zone's own Kelvin (an H6047 bar); its light may only show
        # a representative segment's RGB, so the Kelvin is what goes back.
        seen = snap if snap.get("state") == "on" else (start.get("lit") or {}).get(entity_id) or {}
        target: dict[str, Any] = {
            "state": "on",
            "color_mode": "color_temp",
            "color_temp_kelvin": kelvin,
        }
        if isinstance(seen.get("brightness"), int):
            target["brightness"] = seen["brightness"]
        return target
    if snap.get("state") == "on" and (snap.get("rgb_color") or snap.get("color_temp_kelvin")):
        return snap
    if lit := (start.get("lit") or {}).get(entity_id):
        return dict(lit)
    segment = _library_segment(start, info.get("segment")) if info["role"] == ROLE_SEGMENT else {}
    if segment.get("rgb") and list(segment["rgb"]) != BLACK:
        lit_segment: dict[str, Any] = {
            "state": "on",
            "color_mode": "rgb",
            "rgb_color": segment["rgb"],
        }
        if isinstance(segment.get("brightness"), int) and segment["brightness"] > 0:
            lit_segment["brightness"] = round(segment["brightness"] / 100 * 255)
        return lit_segment
    return None


def zone_kelvin(start: Mapping[str, Any], zone: str | None) -> int | None:
    """Return a zone's own Kelvin at the start, from the library's state, if it had one.

    ``zone_color_temp_kelvin`` is keyed by power index; the journal lists the zones in
    power-index order (H60A6: 0 main, 1 background; H6047: left, right).
    """
    zones = list(start.get("zones") or ())
    kelvins = (start.get("library_state") or {}).get("zone_color_temp_kelvin") or {}
    if zone not in zones:
        return None
    kelvin = kelvins.get(str(zones.index(zone)))
    return kelvin if isinstance(kelvin, int) else None


def _colour_calls(start: Mapping[str, Any]) -> list[RestoreCall]:
    """Return the calls that give each zone, then each enabled segment, its colour back.

    Made while the fixture is lit. A segment is blacked out only if its own colour was
    black: one that showed off because its zone or the fixture was off keeps its colour.
    """
    entities: Mapping[str, Mapping[str, Any]] = start.get("entities", {})
    calls: list[RestoreCall] = []
    for role in (ROLE_ZONE_LIGHT, ROLE_SEGMENT):
        for entity_id, info in entities.items():
            if info["role"] != role:
                continue
            segment = _library_segment(start, info.get("segment"))
            if role == ROLE_SEGMENT and list(segment.get("rgb") or ()) == BLACK:
                off = {"state": "off"}
                calls.append(RestoreCall(entity_id, "light", "turn_off", {}, "blacked out", off))
            elif (target := _colour_target(entity_id, info, start)) is not None:
                data = _light_restore_data(target)
                calls.append(RestoreCall(entity_id, "light", "turn_on", data, "its colour", target))
    return calls


def _power_calls(
    entities: Mapping[str, Mapping[str, Any]], coloured: set[str]
) -> list[RestoreCall]:
    """Return the calls that switch zones, outlets and plug relays back on or off as found.

    A zone light found on and given its colour back is lit by that call already; one with
    no colour to give back (an on/off zone) gets its power alone, which a fixture's colour
    restore wouldn't send (light.py: a lit fixture's colour change sends no power).
    """
    calls: list[RestoreCall] = []
    for role in (ROLE_ZONE_LIGHT, ROLE_ZONE_SWITCH, ROLE_PLUG):
        for entity_id, info in entities.items():
            state = snap_state(info)
            if info["role"] != role or state not in ("on", "off"):
                continue
            if role == ROLE_ZONE_LIGHT and state == "on" and entity_id in coloured:
                continue
            service = "turn_on" if state == "on" else "turn_off"
            target = {"state": state}
            calls.append(RestoreCall(entity_id, info["domain"], service, {}, "as found", target))
    return calls


def restore_calls(start: Mapping[str, Any]) -> list[RestoreCall]:
    """Return the calls that put a device back as the journal's ``start`` record shows it.

    ``start`` is a journal record (runner.journal_record). Colours are set while things
    are lit, and only then is anything switched off:

    1. The fixture, lit, with its colour and brightness: as found, or, for one found off,
       the colour it kept while off (from the diagnostics).
    2. Each zone light's colour, then each enabled segment's (:func:`_colour_calls`).
    3. Zones and outlets that were off, switched off; outlets and plug relays as found.
    4. A fixture that was off, switched off: it keeps its colours, and a whole-fixture
       power-on relights every zone whatever their own power (bench, 2026-10-21).

    The runner skips a call while its entity already shows its ``target``.
    """
    entities: Mapping[str, Mapping[str, Any]] = start.get("entities", {})
    fixture = next((e for e, v in entities.items() if v["role"] == ROLE_FIXTURE), None)
    found_on = fixture is not None and snap_state(entities[fixture]) == "on"
    colours = _colour_calls(start) if fixture is not None else []
    calls: list[RestoreCall] = []
    if fixture is not None and found_on:
        snap = entities[fixture]["snapshot"]
        data = _light_restore_data(snap)
        calls.append(RestoreCall(fixture, "light", "turn_on", data, "as found", snap))
    elif fixture is not None:
        data = _library_restore_data(start.get("library_state") or {})
        if data or colours:
            calls.append(RestoreCall(fixture, "light", "turn_on", data, "lit, its kept colour"))
    calls += colours
    power = _power_calls(entities, {c.entity_id for c in colours if c.service == "turn_on"})
    calls += (
        power
        if found_on or fixture is None
        else [c for c in power if entities[c.entity_id]["role"] == ROLE_PLUG]
    )
    if fixture is not None and not found_on:
        calls.append(RestoreCall(fixture, "light", "turn_off", {}, "off, as found", None))
    return calls


def snap_state(info: Mapping[str, Any]) -> str | None:
    """Return the on/off state a journal entity record holds."""
    state = info.get("snapshot", {}).get("state")
    return str(state) if state is not None else None
