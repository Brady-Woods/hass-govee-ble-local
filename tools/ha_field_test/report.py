"""The run's records and its masked Markdown and JSON reports (pure apart from saving)."""

from __future__ import annotations

from collections.abc import Mapping
import contextlib
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .inventory import (
    ROLE_FIXTURE,
    ROLE_PLUG,
    ROLE_SEGMENT,
    ROLE_ZONE_LIGHT,
    ROLE_ZONE_SWITCH,
    EntityDesc,
    Inventory,
)
from .masking import Masker
from .plan import DevicePlan, Step


def role_label(role: str, zone: str | None = None, segment: int | None = None) -> str:
    """Return how the report names an entity: by its role, never its id.

    An entity id is made from the device's name when the entity was created, which can be a
    name the masking doesn't know any more (a rename since): it never goes in a report.
    """
    if role == ROLE_FIXTURE:
        return "light"
    if role == ROLE_ZONE_LIGHT:
        return f"zone {zone} (light)"
    if role == ROLE_ZONE_SWITCH:
        return f"zone {zone} (switch)"
    if role == ROLE_SEGMENT:
        return f"segment {(segment or 0) + 1}"
    if role == ROLE_PLUG:
        return "plug relay"
    return role


def entity_label(entity: EntityDesc) -> str:
    """Return how the report names ``entity`` (:func:`role_label`)."""
    return role_label(entity.role, entity.zone, entity.segment)


@dataclass
class StepResult:
    """One step as planned and, once run, what happened."""

    step_id: str
    entity: str
    service: str
    data: dict[str, Any]
    kind: str
    expected: str
    note: str = ""
    verdict: str = "planned"
    notes: list[str] = field(default_factory=list)
    readback: str = ""
    seconds: float | None = None
    source: str | None = None

    @classmethod
    def planned(cls, step: Step) -> StepResult:
        """Return the record of a step that hasn't run."""
        return cls(
            step_id=step.step_id,
            entity=entity_label(step.entity),
            service=f"{step.domain}.{step.service}",
            data=dict(step.data),
            kind=str(step.kind),
            expected=step.expect.describe(),
            note=step.note,
        )


@dataclass
class DeviceResult:
    """One device: its plan, the steps' results, the restore and what the integration logged."""

    label: str
    sku: str
    area: str | None
    source: str | None
    heard_by: list[str]
    capabilities: list[str]
    zones: list[str]
    segments: str
    scenes: int | None
    optimistic: bool
    steps: list[StepResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    excluded: str | None = None
    restore: dict[str, Any] = field(default_factory=dict)
    warnings: list[Any] = field(default_factory=list)
    self_test: dict[str, Any] | None = None
    stopped: str | None = None

    @classmethod
    def from_plan(cls, plan: DevicePlan, labels: Mapping[str, str] | None = None) -> DeviceResult:
        """Return the record of a device's plan, before anything runs.

        ``labels``: the scanners' labels by address, to name those that heard the device.
        """
        device = plan.device
        labels = labels or {}
        segments = device.role(ROLE_SEGMENT)
        return cls(
            label=device.label,
            sku=device.sku,
            area=device.area_label,
            source=device.source_label,
            heard_by=sorted(labels.get(s, "an unknown scanner") for s in device.heard_by),
            capabilities=sorted(device.capabilities),
            zones=list(device.zones),
            segments=f"{len(segments)} ({sum(1 for s in segments if s.enabled)} enabled)",
            scenes=device.scene_count,
            optimistic=device.optimistic,
            steps=[StepResult.planned(s) for s in plan.steps],
            skipped=list(plan.skipped),
            caveats=list(plan.caveats),
            excluded=plan.excluded,
        )

    def counts(self) -> dict[str, int]:
        """Count the steps by verdict."""
        out: dict[str, int] = {}
        for step in self.steps:
            out[step.verdict] = out.get(step.verdict, 0) + 1
        return out


@dataclass
class RunReport:
    """The whole run."""

    mode: str
    started: str
    ha_version: str | None
    integration_version: str | None
    expected_version: str | None
    window: str
    scanners: list[str]
    requirements: list[str] = field(default_factory=list)
    library_installed: str | None = None  # as the user confirmed it at the start prompt
    devices: list[DeviceResult] = field(default_factory=list)
    not_testable: list[list[str]] = field(default_factory=list)
    unmanaged: list[list[str]] = field(default_factory=list)
    plugs_confirmed: list[str] = field(default_factory=list)
    finished: str | None = None
    stopped: str | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_inventory(
        cls, inventory: Inventory, *, fragile_proxy: str = "", **kwargs: Any
    ) -> RunReport:
        """Return a report header from the inventory (scanners by their neutral labels)."""
        scanners = sorted(
            inventory.scanners, key=lambda s: inventory.scanner_labels[str(s["source"])]
        )
        return cls(
            ha_version=inventory.ha_version,
            integration_version=inventory.integration_version,
            requirements=list(inventory.requirements),
            scanners=[
                inventory.scanner_labels[str(s["source"])]
                + (
                    " (a failure through it stops the run)"
                    if fragile_proxy and fragile_proxy in str(s.get("name") or "")
                    else ""
                )
                for s in scanners
            ],
            not_testable=[list(x) for x in inventory.not_testable],
            unmanaged=[list(x) for x in inventory.unmanaged],
            **kwargs,
        )


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _data_text(data: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in data.items()) or "-"


def _restore_text(restore: dict[str, Any]) -> str:
    if not restore:
        return "-"
    if restore.get("never_started"):
        return "never started"
    if restore.get("ok"):
        return "yes"
    return "NO: " + "; ".join(str(d) for d in restore.get("diffs", []) + restore.get("errors", []))


def render_markdown(report: RunReport) -> str:  # noqa: C901, PLR0912 - one block per section
    """Return the report as Markdown (mask it before saving)."""
    out = [
        f"# Home Assistant field test: {report.mode}, {report.started}",
        "",
        f"- Home Assistant {report.ha_version}, integration {report.integration_version}"
        + (f" (expected {report.expected_version})" if report.expected_version else ""),
        f"- Library requirement: {', '.join(report.requirements) or '-'}"
        + (
            f"; installed {report.library_installed} (confirmed at the start prompt)"
            if report.library_installed
            else ""
        ),
        f"- Window: {report.window}",
        f"- Scanners: {', '.join(report.scanners) or '-'}",
    ]
    if report.plugs_confirmed:
        out.append(f"- Plugs confirmed for toggling: {', '.join(report.plugs_confirmed)}")
    if report.finished:
        out.append(f"- Finished: {report.finished}")
    if report.stopped:
        out.append(f"- **Stopped early:** {report.stopped}")
    out += [f"- {note}" for note in report.notes]
    out += [
        "",
        "## Summary",
        "",
        "| Device | Area | Through | Steps | Pass | Unverified | Fail | Error | Restored |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for dev in report.devices:
        counts = dev.counts()
        out.append(
            "| "
            + " | ".join(
                _cell(v)
                for v in (
                    dev.label,
                    dev.area,
                    dev.source,
                    dev.excluded or len(dev.steps),
                    counts.get("pass", 0),
                    counts.get("unverified", 0),
                    counts.get("fail", 0),
                    counts.get("error", 0),
                    _restore_text(dev.restore),
                )
            )
            + " |"
        )
    if report.not_testable:
        out += ["", "## Not testable on this route", ""]
        out += [f"- {name}: {why}" for name, why in report.not_testable]
    if report.unmanaged:
        out += ["", "## Govee devices Home Assistant hears without an entry", ""]
        out += [f"- {name}: {why}" for name, why in report.unmanaged]
    for dev in report.devices:
        out += [
            "",
            f"## {dev.label}",
            "",
            f"- Area {dev.area or '-'}, through {dev.source or '-'}"
            + (f", heard by {', '.join(dev.heard_by)}" if dev.heard_by else "")
            + (", state tracked optimistically (no read-back)" if dev.optimistic else ""),
            f"- Capabilities: {', '.join(dev.capabilities) or '-'}; zones: "
            f"{', '.join(dev.zones) or '-'}; segments: {dev.segments}; scenes: {dev.scenes}",
        ]
        if dev.excluded:
            out.append(f"- Left out: {dev.excluded}")
        if dev.stopped:
            out.append(f"- **Stopped:** {dev.stopped}")
        out += [f"- Skipped: {s}" for s in dev.skipped]
        out += [f"- Restore caveat: {c}" for c in dev.caveats]
        if dev.steps:
            out += [
                "",
                "| # | Entity | Step | Kind | Data | Expected | Verdict | Notes |",
                "|---|---|---|---|---|---|---|---|",
            ]
            for n, s in enumerate(dev.steps, 1):
                notes = "; ".join(s.notes) or s.note
                if s.readback:
                    notes = f"{notes} [{s.readback}]" if notes else f"[{s.readback}]"
                if s.seconds is not None:
                    notes = f"{notes} ({s.seconds:.1f} s)"
                out.append(
                    "| "
                    + " | ".join(
                        _cell(v)
                        for v in (
                            n,
                            s.entity,
                            s.step_id,
                            s.kind,
                            f"{s.service} {_data_text(s.data)}",
                            s.expected,
                            s.verdict.upper(),
                            notes,
                        )
                    )
                    + " |"
                )
        if dev.restore:
            out += ["", f"Restored: {_restore_text(dev.restore)}"]
        if dev.self_test is not None:
            out.append(f"Self-test: {json.dumps(dev.self_test)}")
        if dev.warnings:
            out += ["", "Warnings the integration captured during the test:", ""]
            out += [f"- `{json.dumps(w)}`" for w in dev.warnings]
    return "\n".join(out) + "\n"


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` owner-only, atomically and durably.

    A fresh temp file, flushed and fsynced, renamed over ``path``, then the directory
    fsynced: the journal must survive the host losing power during a run.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def save(report: RunReport, masker: Masker, out_dir: Path, stem: str = "report") -> list[Path]:
    """Save the masked Markdown and JSON reports; return their paths."""
    md = out_dir / f"{stem}.md"
    js = out_dir / f"{stem}.json"
    write_private(md, masker.text(render_markdown(report)))
    write_private(js, json.dumps(masker.data(asdict(report)), indent=1) + "\n")
    return [md, js]
