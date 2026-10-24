"""Runs the plan through Home Assistant, one device and one call at a time, and restores.

Safety rules, in the order they apply:

* Nothing changes without ``--run``; a dry run only reads.
* Daytime only (07:00-21:00 America/Los_Angeles) unless overridden, checked before every
  step: a run that reaches 21:00 restores the device it is on and stops.
* A plug, or a per-outlet relay, is switched only once the user has named it at the prompt.
* The journal (every planned device's start state, private, owner-only, fsynced) is written
  before the first change, and each device's record again just before its test. It says a
  device was put back only once the final check found nothing different.
* Each device is restored in a ``finally``: after its steps, on a failure, on Ctrl-C,
  SIGTERM or SIGHUP (a dropped SSH session). Output can never stop a restore: a terminal
  that hung up makes a print fail (EIO), so every line also goes to ``run.log`` in the run
  directory, a failed print only stops further prints, SIGHUP points stdout and stderr at
  the log, and the restore starts before anything is printed. While a restore runs,
  signals only print a warning; three Ctrl-C abandon it, and ``--restore-from`` puts the
  journal back later.
* One device at a time, one service call at a time, never two in flight. Before the next
  device the harness waits until the last one's link is released (the library keeps a
  link up 30 s after a write), so at most one link is up through any proxy.
* A device the fragile proxy (the software proxy that wedged before) can reach, by its
  Connection source or by having passed on its advertisements, that fails a command over
  Bluetooth, or takes 30 s for one, stops the run once it is restored. A refusal Home
  Assistant or the integration answers without the radio (a validation error) doesn't
  count. The harness never touches the proxy itself.

Settling follows the integration: a service call returns once the integration's command
finished (its frames acked, the state written), so the state is read at once; a read-back
poll (``homeassistant.update_entity``) runs only once Home Assistant's refresh debouncer
has cooled down since the last one, so it blocks through a real poll; a device whose state
isn't read back (optimistic) has its on/off confirmed from its advertisement after the
library's advertisement-suppression window, and its other steps count as unverified. A
device the library hasn't read yet is polled once first, so it isn't taken for optimistic.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
import datetime as dt
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import FrameType
from typing import Any
from zoneinfo import ZoneInfo

from .ha_api import HAError, RestClient, ServiceError, WebSocketClient, scrub_token
from .inventory import (
    ROLE_FIXTURE,
    ROLE_PLUG,
    ROLE_SEGMENT,
    ROLE_SELF_TEST,
    ROLE_ZONE_LIGHT,
    ROLE_ZONE_SWITCH,
    DeviceDesc,
    Inventory,
    refresh_device,
    scanner_name,
    snapshot_of,
)
from .plan import DevicePlan, Kind, RestoreCall, Step, build_plan, restore_calls, zone_kelvin
from .report import DeviceResult, RunReport, StepResult, role_label, save, write_private
from .verdict import Verdict, judge, library_diffs, snapshot_diffs

TZ = ZoneInfo("America/Los_Angeles")
DAY_START, DAY_END = dt.time(7, 0), dt.time(21, 0)
WINDOW_TEXT = "07:00-21:00 America/Los_Angeles"
# Home Assistant's refresh debouncer (update_coordinator.REQUEST_REFRESH_DEFAULT_COOLDOWN):
# an update_entity inside it returns at once and the poll runs later.
REFRESH_COOLDOWN_SECONDS = 10.0
# The library ignores the advertised on/off this long after a command or a read
# (govee-ble-local const.ADVERT_SUPPRESS_SECONDS), then the next advertisement counts.
ADVERT_SUPPRESS_SECONDS = 15.0
ADVERT_MARGIN_SECONDS = 4.0
# The library keeps a link up this long after a write (its ACTIVE_IDLE_DELAY).
LINK_HOLD_SECONDS = 30.0
FRAGILE_SLOW_SECONDS = 30.0
SELF_TEST_TIMEOUT = 600.0
VALIDATION_ERROR = "service_validation_error"
_ABANDON_PRESSES = 3
RESTORE_ROLES = (ROLE_FIXTURE, ROLE_ZONE_LIGHT, ROLE_ZONE_SWITCH, ROLE_SEGMENT, ROLE_PLUG)
RELAY_ROLES = (ROLE_PLUG, ROLE_ZONE_SWITCH)


class AbortRunError(Exception):
    """The user (Ctrl-C), a signal or a safety rule stopped the run."""


class AbandonRestoreError(Exception):
    """The user pressed Ctrl-C three times during a restore."""


def in_daytime(now: dt.datetime) -> bool:
    """Return whether ``now`` is inside the test window, in America/Los_Angeles."""
    local = now.astimezone(TZ).time()
    return DAY_START <= local < DAY_END


def _named(record: Mapping[str, Any], entity_id: str) -> str:
    """Return the report's name for an entity of a journal record (its role, not its id)."""
    info = record.get("entities", {}).get(entity_id)
    if info is None:
        return "entity"
    return role_label(info["role"], info.get("zone"), info.get("segment"))


def journal_record(device: DeviceDesc, plan: DevicePlan) -> dict[str, Any]:
    """Return what the journal keeps for a device: enough to put it back (private)."""
    touched = {s.entity.entity_id for s in plan.steps}
    entities = {
        e.entity_id: {
            "role": e.role,
            "domain": e.domain,
            "zone": e.zone,
            "segment": e.segment,
            "snapshot": e.snapshot(),
        }
        for e in device.entities
        if e.role in RESTORE_ROLES
        and e.available
        and (not device.is_plug or e.role not in RELAY_ROLES or e.entity_id in touched)
    }
    return {
        "label": device.label,
        "entry_id": device.entry_id,
        "optimistic": device.optimistic,
        "zones": list(device.zones),
        "library_state": dict(device.library_state),
        "entities": entities,
        # What zone and segment lights showed as the test's first power-on lit them,
        # before any colour step: their colours, which Home Assistant hides while off.
        "lit": {},
        # Set once the device's test begins: a device never touched is never "put back"
        # (its start state would be stale by then, and overwrite the household's changes).
        "touched": False,
        "restored": False,
        "recorded": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }


class Journal:
    """The restore journal: private, owner-only, rewritten atomically and fsynced."""

    def __init__(self, path: Path, data: dict[str, Any] | None = None) -> None:
        self.path = path
        self.data: dict[str, Any] = data or {"devices": {}}

    @classmethod
    def load(cls, path: Path) -> Journal:
        """Read a journal back (for --restore-from)."""
        return cls(path, json.loads(path.read_text(encoding="utf-8")))

    def save(self) -> None:
        """Write it (before any change, and after each)."""
        # Private, but no credential belongs in it either.
        write_private(self.path, scrub_token(json.dumps(self.data, indent=1)) + "\n")

    def put(self, label: str, record: dict[str, Any]) -> None:
        """Record (or re-record) a device's start state and save."""
        self.data["devices"][label] = record
        self.save()

    def mark(self, label: str, *, restored: bool) -> None:
        """Note whether a device was put back, and save."""
        self.data["devices"][label]["restored"] = restored
        self.save()


class Console:
    """The harness's output: always to ``run.log``, and to the terminal while it lasts.

    A dropped SSH session hangs the terminal up, and a print then raises EIO. Output must
    never stop a restore: a failed print only ends printing (the log still has every line),
    and :meth:`detach` (on SIGHUP) points stdout and stderr at the log.
    """

    def __init__(
        self,
        log_path: Path | None,
        mask: Callable[[str], str],
        write: Callable[[str], None] | None = None,
    ) -> None:
        self.log_path, self._mask = log_path, mask
        self._write = write or self._print
        self.terminal = True

    @staticmethod
    def _print(text: str) -> None:
        print(text, flush=True)

    def say(self, text: str) -> None:
        """Log ``text`` (masked) and print it, if the terminal is still there."""
        text = self._mask(text)
        if self.log_path is not None:
            with suppress(OSError):
                fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(text + "\n")
        if self.terminal:
            try:
                self._write(text)
            except OSError:
                self.terminal = False

    def detach(self) -> None:
        """Stop printing, and send anything else written to stdout or stderr to the log."""
        self.terminal = False
        target = str(self.log_path) if self.log_path is not None else os.devnull
        with suppress(OSError):
            sys.stdout = sys.stderr = open(target, "a", encoding="utf-8")  # noqa: SIM115


@contextmanager
def signals_raise(console: Console) -> Iterator[None]:
    """While testing: Ctrl-C, SIGTERM and SIGHUP stop the run (and restore).

    Only the first: as it is raised, every handler becomes note-only, so a second signal
    while the first unwinds (before the restore's own handlers are in place) can't stop the
    restore. The run ends after that device either way.
    """
    sigs = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def _note(signum: int, _frame: FrameType | None) -> None:
        if signum == signal.SIGHUP:
            console.detach()
        console.say("  stopping: the device under test is put back first")

    def _raise(signum: int, _frame: FrameType | None) -> None:
        for sig in sigs:
            signal.signal(sig, _note)
        if signum == signal.SIGHUP:
            console.detach()
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise AbortRunError(f"signal {signal.Signals(signum).name}")

    previous = {s: signal.signal(s, _raise) for s in sigs}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextmanager
def signals_deferred(console: Console) -> Iterator[list[int]]:
    """While restoring: signals are noted, not acted on; three Ctrl-C abandon the restore."""
    seen: list[int] = []

    def _note(signum: int, _frame: FrameType | None) -> None:
        seen.append(signum)
        if signum == signal.SIGHUP:
            console.detach()
        presses = sum(1 for s in seen if s == signal.SIGINT)
        if signum == signal.SIGINT and presses >= _ABANDON_PRESSES:
            raise AbandonRestoreError
        console.say(
            "  restoring: please wait"
            + (f" (Ctrl-C {_ABANDON_PRESSES - presses} more to abandon it)" if presses else "")
        )

    sigs = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {s: signal.signal(s, _note) for s in sigs}
    try:
        yield seen
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _reach_unknown(inventory: Inventory, fragile: list[dict[str, Any]]) -> str | None:
    """Return why the fragile proxy must count as reaching every device, if it must."""
    for scanner in fragile:
        source = str(scanner["source"])
        if scanner.get("connectable", True) and (
            inventory.discovered_error or not inventory.discovered.get(source)
        ):
            label = inventory.scanner_labels.get(source, "the fragile proxy")
            why = inventory.discovered_error or "it has discovered no device"
            return (
                f"{label}'s reach is unknown ({why}): every device counts as reachable through it"
            )
    return None


def _ble_failure(error: ServiceError | None) -> bool:
    """Return whether a call failed over the radio, not as a validation refusal."""
    return error is not None and error.code != VALIDATION_ERROR


class Runner:
    """Runs device plans and restores them (see the module docstring for the rules)."""

    def __init__(
        self,
        ws: WebSocketClient,
        rest: RestClient,
        inventory: Inventory,
        *,
        out_dir: Path,
        report: RunReport,
        readback: bool = True,
        self_test: bool = False,
        daytime_override: bool = False,
        fragile_proxy: str = "bt-proxy",
        log: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self.ws, self.rest, self.inventory = ws, rest, inventory
        self.out_dir, self.report = out_dir, report
        self.readback, self.self_test = readback, self_test
        self.daytime_override, self.fragile_proxy = daytime_override, fragile_proxy
        self.console = Console(out_dir / "run.log", inventory.masker.text, log)
        self._sleep, self._clock = sleep, clock
        self.journal = Journal(out_dir / "journal.json")
        self._last_poll: dict[str, float] = {}
        self._last_command = 0.0
        self.stop: str | None = None
        self._fragile_stop: str | None = None
        self._colour_changed = False
        self._sent = False  # whether anything was sent to the device under test
        # The fragile proxy's address too: the stop rule mustn't depend on a name alone.
        fragile = [
            s
            for s in inventory.scanners
            if fragile_proxy and fragile_proxy in str(s.get("name") or "")
        ]
        self._fragile_sources = frozenset(str(s["source"]) for s in fragile)
        # Its reach unknown (the Bluetooth diagnostics unread, or it discovered nothing while
        # it takes connections): the safe side is that it can reach every device.
        self._fragile_everywhere = _reach_unknown(inventory, fragile)
        if self._fragile_everywhere:
            report.notes.append(self._fragile_everywhere)

    # -- helpers -------------------------------------------------------------

    def _say(self, text: str) -> None:
        self.console.say(text)

    def _save_report(self) -> None:
        save(self.report, self.inventory.masker, self.out_dir)

    def _fragile(self, device: DeviceDesc) -> bool:
        """Return whether a connect to the device may go through the fragile proxy."""
        if self._fragile_everywhere:
            return True
        name = device.source_name or ""
        named = bool(self.fragile_proxy) and self.fragile_proxy in name
        return named or device.fragile(self._fragile_sources)

    def _call(
        self, domain: str, service: str, data: dict[str, Any], *, retry: bool = False
    ) -> tuple[ServiceError | None, float]:
        """Make one service call; ``retry`` an idempotent one once on a new connection.

        An interrupted call (Ctrl-C, a lost link) can leave the connection mid-frame. A
        restore call or a read-back poll sets a state or only reads, so making it again is
        safe; a test step is never retried (its failure is what the test reports).
        """
        started = time.monotonic()
        try:
            try:
                error = self.ws.call_service(domain, service, data)
            except (HAError, OSError, ValueError):
                if not retry:
                    raise
                self.ws.connect()
                error = self.ws.call_service(domain, service, data)
        finally:
            self._last_command = time.monotonic()
        return error, time.monotonic() - started

    def _trouble(
        self, device: DeviceDesc | None, what: str, error: ServiceError | None, seconds: float
    ) -> bool:
        """Note a radio failure or a slow call to a device the fragile proxy can reach.

        Every call counts: a test step, a read-back poll, a restore call, the self-test.
        The run then ends once the device is restored (a test step stops at once).
        """
        if device is None or not self._fragile(device):
            return False
        if not (_ble_failure(error) or seconds >= FRAGILE_SLOW_SECONDS):
            return False
        if self._fragile_stop is None:
            self._fragile_stop = (
                f"{what} to {device.label} "
                + ("failed" if error is not None else f"took {seconds:.0f} s")
                + ": the fragile proxy can reach it, so the run stops before the proxy"
                " wedges (check it; the harness doesn't touch it)"
            )
        self.stop = self.stop or self._fragile_stop
        return True

    def _poll(self, device: DeviceDesc) -> str:
        """Poll the device to read it back, spaced past HA's refresh debouncer; say how it went."""
        main = device.main
        if main is None:
            return "no read-back"
        wait = self._last_poll.get(device.label, 0.0) + REFRESH_COOLDOWN_SECONDS + 0.5
        if (left := wait - time.monotonic()) > 0:
            self._sleep(left)
        error, seconds = self._call(
            "homeassistant", "update_entity", {"entity_id": main.entity_id}, retry=True
        )
        self._last_poll[device.label] = time.monotonic()
        self._trouble(device, "a read-back poll", error, seconds)
        if error is not None:
            return f"read-back refused: {error.code}"
        coordinator = (self.rest.diagnostics(device.entry_id).get("data") or {}).get(
            "coordinator"
        ) or {}
        return "read back" if coordinator.get("last_update_success") else "read-back poll failed"

    def _advert_times(self, address: str) -> tuple[float | None, float | None]:
        """Return Home Assistant's clock now and when it last heard the device advertise.

        Both on Home Assistant's own monotonic clock, the one advertisements are stamped
        with: "now" is the newest advertisement from any device (they arrive many a second).
        """
        events = self.ws.subscribe_snapshot(
            {"type": "bluetooth/subscribe_advertisements"}, settle=1.0
        )
        adverts = [a for e in events for a in (e or {}).get("add") or ()]
        stamped = [a for a in adverts if isinstance(a.get("time"), (int, float))]
        now = max((float(a["time"]) for a in stamped), default=None)
        own = [
            float(a["time"])
            for a in stamped
            if str(a.get("address", "")).upper() == address.upper()
        ]
        return now, max(own, default=None)

    def _library_state(self, entry_id: str) -> Mapping[str, Any]:
        data = self.rest.diagnostics(entry_id).get("data") or {}
        state: Mapping[str, Any] = data.get("state") or {}
        return state

    def _warnings(self, device: DeviceDesc) -> list[Any]:
        try:
            data = self.rest.diagnostics(device.entry_id).get("data") or {}
        except HAError:
            return []
        return list(data.get("recent_warnings") or [])

    def _check_daytime(self) -> None:
        if not self.daytime_override and not in_daytime(self._clock()):
            raise AbortRunError(f"outside the test window ({WINDOW_TEXT})")

    # -- one step ------------------------------------------------------------

    def _run_step(
        self, device: DeviceDesc, step: Step, result: StepResult, record: dict[str, Any]
    ) -> None:
        self._check_daytime()
        before = self.rest.state(step.entity.entity_id)
        by_advert = self.readback and device.optimistic and step.power
        self._sent = True
        error, seconds = self._call(step.domain, step.service, step.call_data())
        # The call's end on Home Assistant's clock: the advertisement that confirms the step
        # must come after the library's suppression window from there.
        call_end = self._advert_times(device.address)[0] if by_advert and error is None else None
        after = self.rest.state(step.entity.entity_id)
        verdict, notes = judge(step, error, before, after)
        how = ""
        if error is None and step.kind is not Kind.REFUSAL:
            if self.readback and not device.optimistic:
                how = self._poll(device)
            elif by_advert:
                self._sleep(ADVERT_SUPPRESS_SECONDS + ADVERT_MARGIN_SECONDS)
                # Only an advertisement the library didn't ignore counts.
                later = self._advert_times(device.address)[1]
                fresh = (
                    call_end is not None
                    and later is not None
                    and later >= call_end + ADVERT_SUPPRESS_SECONDS
                )
                how = "advertisement" if fresh else "no advertisement heard after the command"
            if how in ("read back", "advertisement"):
                readback = self.rest.state(step.entity.entity_id)
                rb_verdict, rb_notes = judge(step, None, before, readback)
                if rb_verdict is not Verdict.PASS and verdict is Verdict.PASS:
                    verdict = rb_verdict
                notes += [f"read back: {n}" for n in rb_notes]
            elif verdict is Verdict.PASS:
                verdict = Verdict.UNVERIFIED
                notes.append(f"shown by Home Assistant only ({how or 'no read-back'})")
        if error is None and step.power and step.service == "turn_on":
            self._capture_lit(record)
        elif error is None and not step.power and step.kind is not Kind.REFUSAL:
            self._colour_changed = True
        result.verdict, result.notes, result.readback = str(verdict), notes, how
        result.seconds = round(seconds, 1)
        result.source = device.source_label
        self._say(
            f"  {result.verdict.upper():10s} {step.step_id:28s} {result.expected[:60]}"
            + (f"  ({'; '.join(notes)})" if notes else "")
        )
        if self._trouble(device, "a command", error, seconds) or self._fragile_stop:
            raise AbortRunError(self._fragile_stop)

    def _capture_lit(self, record: dict[str, Any]) -> None:
        """Keep what each zone and segment light shows now that a power-on lit it.

        Only until the test's first colour step: Home Assistant hides a light's colour
        while it is off, so this is the colour a light found off (or in a zone found off)
        had, which the restore gives back before switching it off again.
        """
        if self._colour_changed:
            return
        for entity_id, info in record["entities"].items():
            if info["role"] not in (ROLE_ZONE_LIGHT, ROLE_SEGMENT) or entity_id in record["lit"]:
                continue
            if info["snapshot"].get("state") == "on":
                continue
            now = self.rest.state(entity_id)
            if now is not None and now.get("state") == "on":
                record["lit"][entity_id] = snapshot_of(now)
        self.journal.save()

    # -- restore -------------------------------------------------------------

    def restore(self, record: Mapping[str, Any], device: DeviceDesc | None) -> dict[str, Any]:
        """Put one device back as its journal record shows it; check, and retry once.

        A device that already matches its record is left alone: nothing is sent.
        """
        if not self._diffs_now(record):
            return {"ok": True, "calls": 0, "sent": 0, "errors": [], "diffs": [], "attempts": 0}
        calls = restore_calls(record)
        outcome: dict[str, Any] = {"calls": len(calls), "errors": [], "diffs": []}
        for attempt in (1, 2):
            outcome["errors"], outcome["sent"] = self._apply(calls, record, device)
            outcome["diffs"] = self._verify(record, device)
            outcome["attempts"] = attempt
            if not outcome["diffs"] and not outcome["errors"]:
                break
        outcome["ok"] = not outcome["diffs"] and not outcome["errors"]
        return outcome

    def _restore_retrying(self, record: Mapping[str, Any], device: DeviceDesc) -> dict[str, Any]:
        """Restore; if the restore itself raises, once more on a new connection.

        A device left with failed calls, or that couldn't be restored at all, ends the run:
        no other device is changed while one is left unrestored.
        """
        try:
            try:
                outcome = self.restore(record, device)
            except (HAError, OSError, ValueError):
                self.ws.connect()
                outcome = self.restore(record, device)
        except (HAError, OSError, ValueError) as err:
            outcome = {"ok": False, "errors": [f"{type(err).__name__}"], "diffs": []}
        if outcome.get("errors"):
            self.stop = self.stop or f"{device.label} could not be fully put back"
        return outcome

    def _apply(
        self, calls: list[RestoreCall], record: Mapping[str, Any], device: DeviceDesc | None
    ) -> tuple[list[str], int]:
        """Make the restore's calls, in order, skipping one whose entity already matches."""
        errors: list[str] = []
        sent = 0
        for call in calls:
            if call.target is not None and not snapshot_diffs(
                call.target, self.rest.state(call.entity_id)
            ):
                continue
            data = {"entity_id": call.entity_id, **call.data}
            error, seconds = self._call(call.domain, call.service, data, retry=True)
            sent += 1
            self._trouble(device, "a restore call", error, seconds)
            if error is not None:
                errors.append(
                    f"{call.service} {_named(record, call.entity_id)}: {error.describe()}"
                )
        return errors, sent

    def _diffs_now(self, record: Mapping[str, Any]) -> list[str]:
        """Return how the device differs from its journal record, without waiting."""
        diffs: list[str] = []
        for entity_id, info in record["entities"].items():
            snap = info["snapshot"]
            if info["role"] == ROLE_ZONE_LIGHT and zone_kelvin(record, info.get("zone")):
                # Its light may show a segment's RGB for a zone in Kelvin: the library's
                # zone Kelvin is what's checked (library_diffs), the light only on or off.
                snap = {"state": snap.get("state")}
            name = _named(record, entity_id)
            diffs += [f"{name}: {d}" for d in snapshot_diffs(snap, self.rest.state(entity_id))]
        if any(i["role"] == ROLE_FIXTURE for i in record["entities"].values()):
            # What the entities can't show: kept colours, segments, zone Kelvin.
            kept = library_diffs(
                record.get("library_state") or {}, self._library_state(str(record["entry_id"]))
            )
            diffs += [f"light: {d}" for d in kept]
        return diffs

    def _verify(self, record: Mapping[str, Any], device: DeviceDesc | None) -> list[str]:
        if device is not None and self.readback and not record.get("optimistic"):
            self._poll(device)
        # Any advertisement that would flip the on/off back lands within this window.
        self._sleep(ADVERT_SUPPRESS_SECONDS + ADVERT_MARGIN_SECONDS)
        return self._diffs_now(record)

    # -- one device ----------------------------------------------------------

    def _prepare(self, device: DeviceDesc) -> DeviceDesc:
        """Re-read a device just before its test; poll one the library hasn't read yet."""
        device = refresh_device(device, self.ws, self.rest)
        if self.readback and device.optimistic:
            # A device the library hasn't read yet is optimistic too: read it once, then
            # look again, so its steps aren't all taken for the integration's own echo.
            self._poll(device)
            device = refresh_device(device, self.ws, self.rest)
        source = device.source
        return DeviceDesc(
            **{
                **device.__dict__,
                "source_name": scanner_name(self.inventory.scanners, source),
                "source_label": self.inventory.scanner_labels.get(source, "an unknown scanner")
                if source
                else None,
            }
        )

    def run_device(self, device: DeviceDesc, *, plug_allowed: bool) -> DeviceResult:
        """Test one device: fresh start state, journal, steps, then always restore.

        Sets ``self.stop`` when the run must end after this device (Ctrl-C, a signal, a
        safety rule, a lost connection). Raises only before the device's journal record
        is written, when nothing has been sent to it.
        """
        self._check_daytime()
        # Gap after the previous device, so its link is released before this one connects.
        if (left := self._last_command + LINK_HOLD_SECONDS - time.monotonic()) > 0:
            self._sleep(left)
        device = self._prepare(device)
        plan = build_plan(device, plug_allowed=plug_allowed)
        result = DeviceResult.from_plan(plan, self.inventory.scanner_labels)
        if plan.excluded or not plan.steps:
            return result
        self._say(f"== {device.label} ({device.area_label or '-'}, through {device.source_label})")
        warnings_before = {json.dumps(w, sort_keys=True) for w in self._warnings(device)}
        record = {**journal_record(device, plan), "touched": True}
        self._colour_changed = self._sent = False
        try:
            self.journal.put(device.label, record)
            for step, step_result in zip(plan.steps, result.steps, strict=True):
                self._run_step(device, step, step_result, record)
        except (KeyboardInterrupt, AbortRunError, HAError, OSError, ValueError) as err:
            self.stop = self._fragile_stop or (
                "Ctrl-C" if isinstance(err, KeyboardInterrupt) else f"{type(err).__name__}: {err}"
            )
            result.stopped = self.stop
        finally:
            self._finish_device(device, record, result, warnings_before)
        return result

    def _finish_device(
        self,
        device: DeviceDesc,
        record: Mapping[str, Any],
        result: DeviceResult,
        warnings_before: set[str],
    ) -> None:
        """Restore the device (signals deferred, before any output), self-test it if asked.

        Nothing is put back on a device nothing was sent to. The journal says a device was
        put back only once the last check, after the self-test if one ran, found nothing
        different. The warnings are read here too, so a signal can't lose the results.
        """
        with signals_deferred(self.console) as seen:
            try:
                if not self._sent:
                    result.restore = {"ok": True, "sent": 0, "why": "nothing was sent to it"}
                else:
                    result.restore = self._restore_retrying(record, device)
                if self.self_test and self.stop is None and not seen and result.restore["ok"]:
                    result.self_test = self._self_test_then_restore(device, record, result)
                result.warnings = [
                    w
                    for w in self._warnings(device)
                    if json.dumps(w, sort_keys=True) not in warnings_before
                ]
            except AbandonRestoreError:
                result.restore = {
                    **result.restore,
                    "ok": False,
                    "errors": ["abandoned: use --restore-from"],
                }
                self.stop = self.stop or "restore abandoned (Ctrl-C x3)"
            except (OSError, ValueError):
                result.warnings = []
            with suppress(AbandonRestoreError):
                self.journal.mark(device.label, restored=bool(result.restore.get("ok")))
                self._say(
                    f"  {device.label} restored: {'yes' if result.restore.get('ok') else 'NO'}"
                )
        if seen and self.stop is None:
            self.stop = "interrupted during the restore"

    def _self_test_then_restore(
        self, device: DeviceDesc, record: Mapping[str, Any], result: DeviceResult
    ) -> dict[str, Any]:
        """Run the self-test (inside the window), then always restore and check again.

        A self-test call that fails or times out may still be running in Home Assistant:
        no other device is started after it.
        """
        if not self.daytime_override and not in_daytime(self._clock()):
            self.stop = self.stop or f"outside the test window ({WINDOW_TEXT})"
            return {"ran": False, "why": "outside the test window"}
        try:
            outcome = self._run_self_test(device)
        except (HAError, OSError, ValueError) as err:
            outcome = {"ran": False, "error": type(err).__name__}
            self.stop = self.stop or "the self-test call failed (it may still be running)"
        # The self-test drives the device through many states: put it back again and
        # check, whatever the self-test says it restored.
        result.restore = {**self._restore_retrying(record, device), "after_self_test": True}
        return outcome

    def _run_self_test(self, device: DeviceDesc) -> dict[str, Any]:
        """Press the integration's Run self-test button; record what it says."""
        # A button's state is the time it was last pressed, "unknown" until then.
        button = next(
            (
                e
                for e in device.entities
                if e.role == ROLE_SELF_TEST and e.enabled and e.state not in (None, "unavailable")
            ),
            None,
        )
        if button is None:
            return {"ran": False, "why": "no self-test button"}
        self._say("  running the integration's self-test")
        error = self.ws.call_service(
            "button", "press", {"entity_id": button.entity_id}, SELF_TEST_TIMEOUT
        )
        self._last_command = time.monotonic()
        # It takes long by design: only a radio failure counts toward the fragile proxy.
        self._trouble(device, "the self-test", error, 0.0)
        last = (self.rest.diagnostics(device.entry_id).get("data") or {}).get(
            "last_self_test"
        ) or {}
        steps = last.get("steps") or []
        return {
            "ran": error is None,
            "error": None if error is None else error.describe(),
            "ok": last.get("ok"),
            "restored": last.get("restored"),
            "steps_ok": f"{sum(1 for s in steps if s.get('ok'))}/{len(steps)}",
        }

    # -- the run -------------------------------------------------------------

    def run(self, devices: list[DeviceDesc], plugs: set[str]) -> None:
        """Test ``devices`` in order; stop at the first abort, always restoring first."""
        try:
            self._run(devices, plugs)
        finally:
            self.report.stopped = self.stop
            self.report.finished = dt.datetime.now(TZ).isoformat(timespec="seconds")
            with suppress(OSError):
                self._save_report()

    def _run(self, devices: list[DeviceDesc], plugs: set[str]) -> None:
        plans = {d.label: build_plan(d, plug_allowed=d.label in plugs) for d in devices}
        records = {
            label: journal_record(plan.device, plan)
            for label, plan in plans.items()
            if not plan.excluded and plan.steps
        }
        self.journal.data = {"started": self.report.started, "devices": records}
        self.journal.save()  # every start state, before the first change
        self._say(f"journal: {self.journal.path} (private: unmasked, owner-only)")
        with signals_raise(self.console):
            for n, device in enumerate(devices):
                if self.stop is not None:
                    # The run stopped: the devices after it were never reached.
                    for later in devices[n:]:
                        self.report.devices.append(self._partial(later, "never started"))
                    break
                plan = plans[device.label]
                if plan.excluded or not plan.steps:
                    # Not loaded, unreachable or an unconfirmed plug: never touched at all.
                    self.report.devices.append(
                        DeviceResult.from_plan(plan, self.inventory.scanner_labels)
                    )
                    continue
                try:
                    result = self.run_device(device, plug_allowed=device.label in plugs)
                except (AbortRunError, KeyboardInterrupt) as err:
                    # Before any change to this device: nothing to put back.
                    self.stop = str(err) or "Ctrl-C"
                    self.report.devices.append(self._partial(device, self.stop))
                    continue
                except HAError as err:
                    # It couldn't be read just before its test; nothing was changed.
                    self.report.devices.append(self._partial(device, f"not read: {err}"))
                    continue
                self.report.devices.append(result)
                self._save_report()

    def _partial(self, device: DeviceDesc, why: str) -> DeviceResult:
        """Return the record of a device the run stopped at or never reached."""
        result = DeviceResult.from_plan(build_plan(device), self.inventory.scanner_labels)
        result.stopped = why
        record = self.journal.data.get("devices", {}).get(device.label, {})
        if record.get("touched"):
            result.restore = {"ok": bool(record.get("restored"))}
        else:
            result.restore = {"never_started": True}
        return result

    def restore_plan(self, journal: Journal, *, everything: bool = False) -> list[str]:
        """Return the labels ``restore_from`` would consider (tested, not yet put back)."""
        return [
            label
            for label, record in journal.data.get("devices", {}).items()
            if record.get("touched") and (everything or not record.get("restored"))
        ]

    def restore_from(
        self,
        journal: Journal,
        *,
        everything: bool = False,
        confirm_relay: Callable[[str, str, str], bool] = lambda _l, _e, _s: False,
    ) -> dict[str, str]:
        """Re-apply a journal; return each device's outcome.

        Only devices whose test began (a device never touched has a stale start state), not
        yet put back unless ``everything``. A device already as found is left alone. A
        device whose config entry is gone, or whose record names entities that aren't its
        own govee_ble_local entities now, is refused. A plug relay or outlet is set only if
        ``confirm_relay(label, entity, state)`` says so. One device's failure doesn't stop
        the others.
        """
        outcomes: dict[str, str] = {}
        for label in self.restore_plan(journal, everything=everything):
            record = journal.data["devices"][label]
            device = next(
                (d for d in self.inventory.devices if d.entry_id == record.get("entry_id")), None
            )
            if device is None:
                outcomes[label] = "refused: its config entry is gone"
                continue
            if set(record["entities"]) - {e.entity_id for e in device.entities}:
                outcomes[label] = "refused: the journal names entities that aren't the device's"
                continue
            try:
                if not self._diffs_now(record):
                    journal.mark(label, restored=True)
                    outcomes[label] = "already as found"
                    continue
                # As in a run: the last device's link is released before this one connects.
                if (left := self._last_command + LINK_HOLD_SECONDS - time.monotonic()) > 0:
                    self._sleep(left)
                relays = {
                    eid: info
                    for eid, info in record["entities"].items()
                    if info["role"] in RELAY_ROLES
                    and not confirm_relay(
                        label, _named(record, eid), str(info["snapshot"]["state"])
                    )
                }
                kept = {k: v for k, v in record["entities"].items() if k not in relays}
                if not kept:
                    outcomes[label] = "left as it is: its relays weren't confirmed"
                    continue
                with signals_deferred(self.console):
                    outcome = self.restore({**record, "entities": kept}, device)
                journal.mark(label, restored=bool(outcome.get("ok")) and not relays)
                outcomes[label] = "put back" if outcome.get("ok") else f"NOT put back: {outcome}"
            except (HAError, OSError, ValueError, AbandonRestoreError) as err:
                outcomes[label] = f"failed: {type(err).__name__}"
            self._say(f"  {label}: {outcomes[label]}")
            if self._fragile_stop:
                # A radio failure through the fragile proxy: no more connects at all.
                for rest in self.restore_plan(journal, everything=everything):
                    outcomes.setdefault(rest, f"not attempted: {self._fragile_stop}")
                break
        return outcomes


def ask(prompt: str) -> str:
    """Read an answer from the terminal (the run needs one: ``ssh -t``)."""
    if not sys.stdin.isatty():
        raise AbortRunError("needs an interactive terminal (ssh -t)")
    return input(prompt).strip()
