"""Verdicts: does Home Assistant's state show what a step expected? (pure).

A device rounds what it reports (brightness percent steps, its own colour and Kelvin
resolution), so a read-back is matched within a tolerance and the value it showed is
recorded either way.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from .ha_api import ServiceError
from .plan import Expect, Kind, Step

BRIGHTNESS_TOLERANCE = 3  # of 255 (about 1 %)
RGB_TOLERANCE = 5  # per channel
KELVIN_TOLERANCE = 100


class Verdict(StrEnum):
    """The outcome of a step."""

    PASS = "pass"  # noqa: S105 - a verdict, not a password
    FAIL = "fail"
    ERROR = "error"  # the service call failed where it shouldn't have
    # Accepted, and Home Assistant shows it, but nothing read the device back or heard it
    # advertise: the state shown may only be the integration's own echo of the command.
    UNVERIFIED = "unverified"
    SKIPPED = "skipped"


def _close(actual: Any, wanted: Sequence[int], tolerance: int) -> bool:
    return (
        isinstance(actual, (list, tuple))
        and len(actual) == len(wanted)
        and all(
            isinstance(a, int) and abs(a - w) <= tolerance
            for a, w in zip(actual, wanted, strict=True)
        )
    )


def state_mismatches(expect: Expect, state: Mapping[str, Any] | None) -> list[str]:
    """Return what in ``state`` (an HA state object) differs from ``expect``."""
    if state is None:
        return ["no state"]
    attrs: Mapping[str, Any] = state.get("attributes") or {}
    out: list[str] = []
    if expect.state is not None and state.get("state") != expect.state:
        out.append(f"state {state.get('state')} (expected {expect.state})")
    if expect.color_mode is not None and attrs.get("color_mode") != expect.color_mode:
        out.append(f"color_mode {attrs.get('color_mode')} (expected {expect.color_mode})")
    if expect.brightness is not None:
        got = attrs.get("brightness")
        if not isinstance(got, int) or abs(got - expect.brightness) > BRIGHTNESS_TOLERANCE:
            out.append(f"brightness {got} (expected {expect.brightness})")
    if expect.rgb is not None and not _close(attrs.get("rgb_color"), expect.rgb, RGB_TOLERANCE):
        out.append(f"rgb_color {attrs.get('rgb_color')} (expected {list(expect.rgb)})")
    if expect.kelvin is not None:
        got = attrs.get("color_temp_kelvin")
        if not isinstance(got, int) or abs(got - expect.kelvin) > KELVIN_TOLERANCE:
            out.append(f"color_temp_kelvin {got} (expected {expect.kelvin})")
    if expect.effect is not None and attrs.get("effect") != expect.effect:
        out.append(f"effect {attrs.get('effect')!r} (expected {expect.effect!r})")
    return out


def judge(
    step: Step,
    error: ServiceError | None,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> tuple[Verdict, list[str]]:
    """Return the verdict of a step from its service error and the state before and after."""
    expect = step.expect
    if step.kind is Kind.CHANGE_OR_REFUSAL and error is not None:
        key = error.translation_key or error.code
        changed = before is not None and after is not None and before != after
        if key == expect.error_key and not changed:
            return Verdict.PASS, [f"refused: {key} (an accepted answer)"]
        return Verdict.ERROR, [error.describe()]
    if step.kind is Kind.REFUSAL:
        if error is None:
            return Verdict.FAIL, [f"accepted (expected a refusal: {expect.error_key})"]
        key = error.translation_key or error.code
        notes = [] if key == expect.error_key else [f"refused: {key} (expected {expect.error_key})"]
        if (
            expect.unchanged
            and before is not None
            and after is not None
            and before.get("state") != after.get("state")
        ):
            notes.append(f"state changed {before.get('state')} -> {after.get('state')}")
        return (Verdict.FAIL if notes else Verdict.PASS), notes or [f"refused: {key}"]
    if error is not None:
        return Verdict.ERROR, [error.describe()]
    mismatches = state_mismatches(expect, after)
    return (Verdict.FAIL, mismatches) if mismatches else (Verdict.PASS, [])


def snapshot_diffs(start: Mapping[str, Any], now: Mapping[str, Any] | None) -> list[str]:
    """Return how an entity's state now differs from its journal snapshot (restore check).

    The colour mode counts only with its value: a zoned fixture whose zones have their own
    colours shows its default mode with no colour at all (an H6047's bars).
    """
    if now is None:
        return ["no state"]
    attrs: Mapping[str, Any] = now.get("attributes") or {}
    if start.get("state") != now.get("state"):
        return [f"state {now.get('state')} (was {start.get('state')})"]
    if start.get("state") != "on":
        return []
    mode = start.get("color_mode")
    rgb, kelvin, brightness = (
        start.get("rgb_color"),
        start.get("color_temp_kelvin"),
        start.get("brightness"),
    )
    rgb_set = mode == "rgb" and isinstance(rgb, list) and len(rgb) == 3  # noqa: PLR2004
    kelvin_set = mode == "color_temp" and isinstance(kelvin, int)
    expect = Expect(
        color_mode=mode if rgb_set or kelvin_set else None,
        brightness=brightness if isinstance(brightness, int) else None,
        rgb=(rgb[0], rgb[1], rgb[2]) if rgb_set and isinstance(rgb, list) else None,
        kelvin=kelvin if kelvin_set and isinstance(kelvin, int) else None,
        effect=start.get("effect") if start.get("effect") not in (None, "off") else None,
    )
    out = state_mismatches(expect, {"state": now.get("state"), "attributes": attrs})
    return [m.replace("expected", "was") for m in out]


LIBRARY_KEYS = ("brightness", "color_temp_kelvin", "rgb_color")
SEGMENT_BRIGHTNESS_TOLERANCE = 2  # percent


def _segment_diffs(start: Mapping[str, Any], now: Mapping[str, Any]) -> list[str]:
    """Return how each segment's colour and brightness (the library's) differ from the start."""
    current = {s.get("index"): s for s in now.get("segments") or () if isinstance(s, Mapping)}
    out: list[str] = []
    for seg in start.get("segments") or ():
        if not isinstance(seg, Mapping):
            continue
        got = current.get(seg.get("index")) or {}
        if isinstance(seg.get("rgb"), list) and not _close(
            got.get("rgb"), seg["rgb"], RGB_TOLERANCE
        ):
            out.append(f"segment {seg.get('index', 0) + 1} rgb {got.get('rgb')} (was {seg['rgb']})")
        was, now_b = seg.get("brightness"), got.get("brightness")
        if isinstance(was, int) and (
            not isinstance(now_b, int) or abs(now_b - was) > SEGMENT_BRIGHTNESS_TOLERANCE
        ):
            out.append(f"segment {seg.get('index', 0) + 1} brightness {now_b} (was {was})")
    return out


def library_diffs(start: Mapping[str, Any], now: Mapping[str, Any]) -> list[str]:
    """Return how the library's state now differs from the start (the diagnostics' ``state``).

    What Home Assistant's entities can't show: the colour and brightness a fixture that is
    off lights back up in, each segment's colour and brightness (a disabled segment light
    shows nothing), and each zone's Kelvin. Not the zones' power while the fixture is off: a
    whole-fixture power-on relights every zone. Only what the start knew is checked.
    """
    out: list[str] = []
    zone_kelvin = now.get("zone_color_temp_kelvin") or {}
    for zone, was in (start.get("zone_color_temp_kelvin") or {}).items():
        got = zone_kelvin.get(zone)
        if not isinstance(got, int) or abs(got - was) > KELVIN_TOLERANCE:
            out.append(f"zone {zone} kelvin {got} (was {was})")
    out += _segment_diffs(start, now)
    for key in LIBRARY_KEYS:
        was, got = start.get(key), now.get(key)
        if was is None:
            continue
        if key == "color_temp_kelvin" and isinstance(got, int) and isinstance(was, int):
            same = abs(got - was) <= KELVIN_TOLERANCE
        elif key == "rgb_color" and isinstance(was, list):
            same = _close(got, was, RGB_TOLERANCE)
        else:
            same = got == was
        if not same:
            out.append(f"kept {key} {got} (was {was})")
    return out
