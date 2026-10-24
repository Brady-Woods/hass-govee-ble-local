"""Command line: ``python3 -m ha_field_test [--run | --restore-from JOURNAL] ...``.

Run it on the Home Assistant host (docs/field-test-ha.md). The default is a dry run: it
reads the inventory, prints the masked plan and saves it, and makes no service call.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
import sys

from .ha_api import (
    DEFAULT_TOKEN_FILE,
    DEFAULT_URL,
    HAError,
    RestClient,
    WebSocketClient,
    read_token,
)
from .inventory import DeviceDesc, Inventory, fetch_raw, library_version, parse_inventory
from .plan import PLUG_CAVEAT, build_plan
from .report import DeviceResult, RunReport, render_markdown, save
from .runner import TZ, WINDOW_TEXT, AbortRunError, Journal, Runner, ask, in_daytime

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
DEFAULT_OUT = "~/govee-bench/ha_field_test_runs"


def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 -m ha_field_test",
        description="Field test of govee_ble_local through Home Assistant. Dry run by default.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read and print the plan (default)")
    mode.add_argument("--run", action="store_true", help="run the plan (asks first)")
    mode.add_argument("--restore-from", metavar="JOURNAL", help="put a run's devices back")
    parser.add_argument("--all", action="store_true", help="with --restore-from: every device")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="LABEL|SKU",
        help="test only these devices (repeatable): 'H60A6 #2' or 'H60A6'",
    )
    parser.add_argument(
        "--expect-version", metavar="X.Y.Z", help="the integration version (required with --run)"
    )
    parser.add_argument(
        "--expect-library",
        metavar="X.Y.Z",
        help="the library version the manifest requirement names (required with --run)",
    )
    parser.add_argument(
        "--no-readback", action="store_true", help="don't force a read-back after each step"
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="also press each device's Run self-test button after its restore",
    )
    parser.add_argument(
        "--outside-daytime-ok",
        action="store_true",
        help=f"allow a run or restore outside {WINDOW_TEXT} (the user's explicit override)",
    )
    parser.add_argument(
        "--no-tmux",
        action="store_true",
        help="allow --run outside tmux or screen (a dropped SSH session then ends it)",
    )
    parser.add_argument(
        "--fragile-proxy",
        default="bt-proxy",
        metavar="NAME",
        help="stop the run on trouble through the scanner whose name contains NAME",
    )
    parser.add_argument(
        "--scan-seconds",
        type=float,
        default=10.0,
        help="how long to collect advertisements (which scanners hear each device)",
    )
    parser.add_argument("--out", default=DEFAULT_OUT, help="where each run's directory goes")
    parser.add_argument("--url", default=DEFAULT_URL, help="Home Assistant on this host")
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE, help="owner-only token file")
    return parser.parse_args(argv)


def _select(devices: list[DeviceDesc], only: list[str]) -> list[DeviceDesc]:
    if not only:
        return devices
    wanted = {o.strip().upper() for o in only}
    return [d for d in devices if d.label.upper() in wanted or d.sku.upper() in wanted]


def _print_inventory(inventory: Inventory) -> None:
    mask = inventory.masker.text
    print(
        f"Home Assistant {inventory.ha_version}, integration {inventory.integration_version}: "
        f"{len(inventory.devices)} govee_ble_local devices"
    )
    for device in inventory.devices:
        via = mask(device.source_label or "-")
        print(
            f"  {device.label:10s} {device.area_label or '-':8s} via {via:16s}"
            f" {'ok' if device.available else 'NOT TESTABLE'}"
        )
    for name, why in inventory.not_testable:
        print(f"  not testable: {name}: {why}")
    for name, why in inventory.unmanaged:
        print(f"  heard, no entry: {name}: {why}")


def _confirm_plugs(devices: list[DeviceDesc]) -> set[str]:
    """List each plug with its name, area and state; return the ones the user allows.

    The one place the harness shows a real name and area: the user has to know what a plug
    feeds before saying it may be switched off. It is printed to the terminal only, never
    saved; the reports name the plug by its label.
    """
    plugs = [d for d in devices if d.is_plug and d.available]
    if not plugs:
        return set()
    print("\nPlugs found. Switching one cuts power to whatever it feeds, then restores it.")
    print(f"Note: {PLUG_CAVEAT}.")
    for n, device in enumerate(plugs, 1):
        # The user's own name for it (name_by_user) is the last one the device has.
        name = device.names[-1] if device.names else device.title
        relays = [device.plug] if device.plug is not None else device.outlets
        states = ", ".join(f"{r.zone or 'relay'}: {r.state}" for r in relays if r is not None)
        print(f"  {n}) {name}  area: {device.area or '-'}  {states}  ({device.label})")
    answer = ask("Which plugs may be switched? Numbers, comma-separated (Enter = none): ")
    chosen: set[str] = set()
    for part in filter(None, (p.strip() for p in answer.split(","))):
        if not part.isdigit() or not 1 <= int(part) <= len(plugs):
            raise AbortRunError(f"not a plug number: {part!r}")
        chosen.add(plugs[int(part) - 1].label)
    return chosen


def _dry_run(
    inventory: Inventory, devices: list[DeviceDesc], report: RunReport, out_dir: Path
) -> int:
    for device in devices:
        plan = build_plan(device, plug_allowed=True)
        result = DeviceResult.from_plan(plan, inventory.scanner_labels)
        if device.is_plug and not plan.excluded:
            result.skipped.insert(0, "plug: these steps run only if you name it at the prompt")
        report.devices.append(result)
    report.notes.append("dry run: inventory read, no service calls made")
    paths = save(report, inventory.masker, out_dir, stem="plan")
    print(inventory.masker.text(render_markdown(report)))
    print("saved: " + ", ".join(str(p) for p in paths))
    return EXIT_OK


def _daytime_gate(args: argparse.Namespace) -> str | None:
    if not args.outside_daytime_ok and not in_daytime(dt.datetime.now(dt.UTC)):
        return f"outside the test window ({WINDOW_TEXT}); --outside-daytime-ok overrides"
    return None


def _gates(args: argparse.Namespace, inventory: Inventory) -> str | None:
    """Return why a run may not start, or None."""
    if not args.expect_version or not args.expect_library:
        return "--run needs --expect-version and --expect-library (the build under test)"
    if inventory.integration_version != args.expect_version:
        return (
            f"Home Assistant runs integration {inventory.integration_version}, "
            f"not {args.expect_version}: install the release candidate first (runbook step 2)"
        )
    if library_version(inventory.requirements) != args.expect_library:
        return (
            f"the integration's requirement ({', '.join(inventory.requirements) or 'none'}) "
            f"doesn't name library {args.expect_library} exactly (runbook step 2)"
        )
    if not args.no_tmux and not (os.environ.get("TMUX") or os.environ.get("STY")):
        return "run it inside tmux or screen, so a dropped SSH session can't end it (--no-tmux)"
    return _daytime_gate(args)


def _restore(args: argparse.Namespace, runner: Runner) -> int:
    """Put a journal's devices back, once the user has seen what that changes and agreed."""
    if (why := _daytime_gate(args)) is not None:
        print(f"refusing to restore: {why}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        journal = Journal.load(Path(args.restore_from).expanduser())
    except (OSError, ValueError) as err:
        print(
            f"refusing to restore: can't read that journal ({type(err).__name__})", file=sys.stderr
        )
        return EXIT_REFUSED
    labels = runner.restore_plan(journal, everything=args.all)
    if not labels:
        print("nothing to put back: every tested device is marked restored (--all re-checks)")
        return EXIT_OK
    print("These devices go back to their state at the start of that run:")
    for label in labels:
        relays = [
            f"{info.get('zone') or 'relay'} {info['snapshot'].get('state')}"
            for info in journal.data["devices"][label]["entities"].values()
            if info["role"] in ("plug", "zone_switch")
        ]
        extra = f" (relays: {', '.join(relays)}, asked one by one)" if relays else ""
        print(f"  {label}{extra}")
    if ask("Type RESTORE to put them back: ") != "RESTORE":
        print("nothing changed")
        return EXIT_REFUSED

    def confirm(label: str, entity: str, state: str) -> bool:
        return ask(f"  {label}: set the {entity} to {state}? [y/N] ").lower() == "y"

    outcomes = runner.restore_from(journal, everything=args.all, confirm_relay=confirm)
    done = ("put back", "already as found")
    return EXIT_OK if all(o in done for o in outcomes.values()) else EXIT_FAILED


def _run(args: argparse.Namespace, runner: Runner, devices: list[DeviceDesc]) -> int:
    if (why := _gates(args, runner.inventory)) is not None:
        print(f"refusing to start: {why}", file=sys.stderr)
        return EXIT_REFUSED
    plugs = _confirm_plugs(devices)
    runner.report.plugs_confirmed = sorted(plugs)
    planned = [d for d in devices if not build_plan(d, plug_allowed=d.label in plugs).excluded]
    print(
        f"\nAbout to test {len(planned)} devices one at a time: "
        + ", ".join(d.label for d in planned)
    )
    print("Every device is put back as found after its test, and on Ctrl-C.")
    # Home Assistant's API doesn't say which library is installed: the user checks it
    # (runbook step 2's read-only command) and says so here; the report records it.
    installed = ask("Installed govee-ble-local version (runbook step 2's check): ")
    if installed != args.expect_library:
        print(f"not started: {installed!r} isn't the expected {args.expect_library}")
        return EXIT_REFUSED
    runner.report.library_installed = installed
    if ask("Type START to begin: ") != "START":
        print("not started")
        return EXIT_REFUSED
    runner.run(devices, plugs)
    failed = any(
        r.counts().get("fail") or r.counts().get("error") or (r.restore and not r.restore.get("ok"))
        for r in runner.report.devices
    )
    return EXIT_FAILED if failed or runner.report.stopped else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run the harness."""
    args = _args(argv)
    mode = "run" if args.run else "restore" if args.restore_from else "dry run"
    try:
        token = read_token(args.token_file)
        rest = RestClient(args.url, token)
        ws = WebSocketClient(args.url, token)
        ws.connect()
    except (HAError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_REFUSED
    now = dt.datetime.now(TZ)
    runs = Path(args.out).expanduser()
    runs.mkdir(mode=0o700, parents=True, exist_ok=True)
    out_dir = runs / f"{now:%Y%m%d-%H%M%S}-{mode.replace(' ', '-')}"
    out_dir.mkdir(mode=0o700)
    report: RunReport | None = None
    inventory: Inventory | None = None
    try:
        inventory = parse_inventory(fetch_raw(ws, rest, scan_seconds=args.scan_seconds))
        devices = _select(inventory.devices, args.only)
        report = RunReport.from_inventory(
            inventory,
            fragile_proxy=args.fragile_proxy,
            mode=mode,
            started=now.isoformat(timespec="seconds"),
            expected_version=args.expect_version,
            window=WINDOW_TEXT + (" (overridden)" if args.outside_daytime_ok else ""),
        )
        _print_inventory(inventory)
        runner = Runner(
            ws,
            rest,
            inventory,
            out_dir=out_dir,
            report=report,
            readback=not args.no_readback,
            self_test=args.self_test,
            daytime_override=args.outside_daytime_ok,
            fragile_proxy=args.fragile_proxy,
        )
        if args.restore_from:
            return _restore(args, runner)
        if not args.run:
            return _dry_run(inventory, devices, report, out_dir)
        code = _run(args, runner, devices)
    except AbortRunError as err:
        print(f"stopped: {err}", file=sys.stderr)
        return EXIT_REFUSED
    finally:
        ws.close()
        if report is not None and inventory is not None and mode == "run":
            # Whatever ended the run, the report shows what happened up to there.
            try:
                save(report, inventory.masker, out_dir)
            except OSError as err:
                print(f"could not save the report: {err}", file=sys.stderr)
    print(f"report: {out_dir / 'report.md'}")
    return code


if __name__ == "__main__":
    sys.exit(main())
