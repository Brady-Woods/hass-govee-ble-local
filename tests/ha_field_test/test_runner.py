"""The field-test runner against a fake Home Assistant: journal first, always restore.

All identifiers are made up (builders.py): MACs from the RFC 7042 documentation range.
"""

from __future__ import annotations

from collections.abc import Callable
import copy
import dataclasses
import datetime as dt
import json
import os
import signal
import stat
import sys
from typing import Any
import urllib.request

import pytest

from tools.ha_field_test import ha_api
from tools.ha_field_test.ha_api import HAError, ServiceError
from tools.ha_field_test.inventory import (
    DeviceDesc,
    Inventory,
    library_version,
    parse_inventory,
    scanner_labels,
)
from tools.ha_field_test.masking import Masker
from tools.ha_field_test.plan import TINT_KELVIN, TINT_RGB, Kind, build_plan
from tools.ha_field_test.report import RunReport
from tools.ha_field_test.runner import AbandonRestoreError, AbortRunError, Journal, Runner

from .builders import (
    ADDRESS,
    AREA,
    LOCAL_ADAPTER,
    LOCAL_ADAPTER_NAME,
    TITLE,
    USER_NAME,
    device,
    light,
    plug,
    self_test_button,
    zone_light,
)
from .registry import LOCAL, MASKING, PROXY_1, PROXY_2, raw_inventory

FIXTURE = "light.gvh60a6abcd"
MAIN = "light.gvh60a6abcd_zone_main_light"
BACK = "light.gvh60a6abcd_zone_background_light"
PLUG = "switch.ihoment_h5083_abce"
BUTTON = "button.gvh60a6abcd_self_test"
FRAGILE = "00:00:5E:00:53:B2"
SCANNERS = [
    {"source": LOCAL_ADAPTER, "name": LOCAL_ADAPTER_NAME, "adapter": "hci0", "scanner_type": "usb"},
    {
        "source": FRAGILE,
        "name": f"bt-proxy ({FRAGILE})",
        "adapter": "bt-proxy",
        "scanner_type": "remote",
    },
]
CAPS = {
    "supported_color_modes": ["color_temp", "rgb"],
    "min_color_temp_kelvin": 2700,
    "max_color_temp_kelvin": 6500,
}
ONOFF = {"supported_color_modes": ["onoff"]}
FIXTURE_CAPS = {**CAPS, "effect_list": ["off", "Sunrise", "Aurora"], "supported_features": 4}
COLOUR_KWARGS = ("brightness", "brightness_pct", "rgb_color", "color_temp_kelvin", "effect")
RGB = {"color_mode": "rgb", "color_temp_kelvin": None}
START = {
    FIXTURE: {**RGB, "rgb_color": [255, 111, 30], "brightness": 128, "effect": "off"},
    MAIN: {**RGB, "rgb_color": [1, 2, 3], "brightness": 51},
    BACK: {**RGB, "rgb_color": [4, 5, 6], "brightness": 77},
}
BLE_FAILURE = ServiceError(
    "home_assistant_error", "x", "govee_ble_local", "ble_command_failed", {"error": "timed out"}
)
DAY, NIGHT = (dt.datetime(2026, 10, 23, h, tzinfo=dt.UTC) for h in (19, 5))  # 12:00, 22:00 PDT
RESTORE_FIXTURE = {"entity_id": FIXTURE, "rgb_color": [255, 111, 30], "brightness": 128}


class FakeHA:
    """Home Assistant for one zoned light and a plug, as the integration behaves.

    An off light shows no colour, and keeps it. A plain fixture turn-on, or one of a
    fixture that is off, sends power and relights every zone; a colour change of a lit
    fixture sends none and leaves its zones as they are (light.py). A zone's turn-on
    lights the fixture. A zone found ``onoff`` has no colour at all (an H60A6 at pact 1,1).
    """

    def __init__(self, *, lit: bool, plug_state: str | None = None, onoff_back: bool = False):
        self.colours = copy.deepcopy(START)
        if onoff_back:
            self.colours[BACK] = {}
        self.caps = {e: FIXTURE_CAPS if e == FIXTURE else CAPS for e in START}
        self.caps[BACK] = ONOFF if onoff_back else CAPS
        self.lit = dict.fromkeys(START, lit)
        self.switches = {PLUG: plug_state} if plug_state else {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_on: dict[Any, Any] = {}
        self.journal_path: Any = None
        self.journal_at_first_change: dict[str, Any] | None = None
        self.connects = 0
        self.diagnostics_read: list[str] = []
        self.advert_clock, self.advert_step = 100.0, 25.0

    def connect(self) -> None:
        self.connects += 1

    def state(self, entity_id: str) -> dict[str, Any] | None:
        if entity_id == BUTTON:
            return {"state": "unknown", "attributes": {}}  # a button never pressed
        if entity_id in self.switches:
            return {"state": self.switches[entity_id], "attributes": {}}
        if entity_id not in self.colours:
            return None
        attrs: dict[str, Any] = dict(self.caps[entity_id])
        shown = self.colours[entity_id]
        attrs.update(shown if self.lit[entity_id] else dict.fromkeys(shown))
        return {"state": "on" if self.lit[entity_id] else "off", "attributes": attrs}

    def command(self, message: dict[str, Any], timeout: float = 30) -> Any:
        assert message["type"] == "get_states"
        everything = [*self.colours, *self.switches, BUTTON]
        return [{"entity_id": e, **(self.state(e) or {})} for e in everything]

    def subscribe_snapshot(self, message: dict[str, Any], settle: float = 2.0) -> list[Any]:
        assert message["type"] == "bluetooth/subscribe_advertisements"
        # Stamped on Home Assistant's clock: another device's advertisement now, and the
        # device's own every advert_step seconds.
        self.advert_clock += self.advert_step
        adverts = [
            {"address": "00:00:5E:00:53:99", "time": self.advert_clock + 1},
            {"address": ADDRESS, "time": self.advert_clock},
        ]
        return [{"add": adverts}]

    def diagnostics(self, entry_id: str) -> dict[str, Any]:
        self.diagnostics_read.append(entry_id)
        if entry_id == "entry-retry":
            raise HAError("Home Assistant answered HTTP 500")
        fixture = self.colours[FIXTURE]
        mode = fixture["color_mode"]
        library = {
            "optimistic": entry_id == "entry-plug",
            "is_on": self.lit[FIXTURE],
            "brightness": round(fixture["brightness"] / 255 * 100),
            "rgb_color": fixture.get("rgb_color") if mode == "rgb" else None,
            "color_temp_kelvin": fixture.get("color_temp_kelvin") if mode == "color_temp" else None,
        }
        return {
            "data": {
                "coordinator": {"last_update_success": True},
                "state": library,
                "recent_warnings": [],
                "last_self_test": {"ok": True, "steps": []},
            }
        }

    def call_service(
        self, domain: str, service: str, data: dict[str, Any], timeout: float = 300
    ) -> ServiceError | None:
        n = len(self.calls)
        self.calls.append((domain, service, dict(data)))
        if self.journal_at_first_change is None and service != "update_entity":
            self.journal_at_first_change = json.loads(self.journal_path.read_text())
        key = service if service == "press" else n
        if data == RESTORE_FIXTURE and "restore" in self.fail_on:
            key = "restore"  # the lit fixture's restore call
        if (
            fail := self.fail_on.pop(key, None) if key == "restore" else self.fail_on.get(key)
        ) is not None:
            if isinstance(fail, ServiceError):
                return fail
            if isinstance(fail, BaseException):
                raise fail
            fail()
        if domain in ("homeassistant", "button"):
            return None
        if domain == "switch":
            self.switches[data["entity_id"]] = "on" if service == "turn_on" else "off"
            return None
        return self._light(data["entity_id"], service, data)

    def _light(self, entity_id: str, service: str, data: dict[str, Any]) -> ServiceError | None:
        colours = self.colours[entity_id]
        if data.get("effect") not in (None, *FIXTURE_CAPS["effect_list"]):
            return ServiceError(
                "service_validation_error", "no", "govee_ble_local", "unknown_effect"
            )
        if service == "turn_off":
            for target in START if entity_id == FIXTURE else [entity_id]:
                self.lit[target] = False
            return None
        if entity_id == FIXTURE:
            if not (data.keys() & set(COLOUR_KWARGS)) or not self.lit[FIXTURE]:
                self.lit.update(dict.fromkeys(START, True))  # power: every zone back
        else:
            self.lit[entity_id] = self.lit[FIXTURE] = True
        if not colours:
            return None  # an on/off zone: Home Assistant dropped any colour
        if "brightness_pct" in data:
            colours["brightness"] = round(255 * data["brightness_pct"] / 100)
        colours["brightness"] = data.get("brightness", colours["brightness"])
        rgb, kelvin = data.get("rgb_color"), data.get("color_temp_kelvin")
        if rgb is not None and tuple(rgb) == TINT_RGB:
            rgb, kelvin = None, TINT_KELVIN
        if rgb is not None:
            colours.update(color_mode="rgb", rgb_color=list(rgb), color_temp_kelvin=None)
        if kelvin is not None:
            colours.update(
                color_mode="color_temp",
                color_temp_kelvin=max(2700, min(6500, kelvin)),
                rgb_color=None,
            )
        if "effect" in data:
            colours["effect"] = data["effect"]
        return None


def _lamp(*, lit: bool, onoff_back: bool = False, **fields: Any) -> DeviceDesc:
    state = "on" if lit else "off"
    back = (
        zone_light("background", state=state, modes=("onoff",), span=None)
        if onoff_back
        else zone_light("background", state=state)
    )
    return device(
        light(state=state, **(START[FIXTURE] if lit else {})),
        zone_light("main", state=state),
        back,
        self_test_button(),
        zones=("main", "background"),
        **fields,
    )


def _plug_device() -> DeviceDesc:
    return device(
        plug(state="on"),
        label="H5083 #1",
        sku="H5083",
        entry_id="entry-plug",
        names=("ihoment_H5083_ABCE",),
        hex_ids=("00:00:5E:00:53:02",),
    )


def _runner(
    ha: FakeHA,
    tmp_path: Any,
    devices: list[DeviceDesc],
    discovered: dict[str, list[str]] | None = None,
    **kwargs: Any,
) -> tuple[Runner, list[float]]:
    slept: list[float] = []
    labels = scanner_labels(SCANNERS)
    masker = Masker()
    for dev in devices:
        masker.add(dev.label, names=(*dev.names, dev.entry_id), hex_ids=dev.hex_ids)
    for scanner in SCANNERS:
        masker.add(
            labels[scanner["source"]],
            names=(scanner["name"], scanner["adapter"]),
            hex_ids=(scanner["source"],),
        )
    masker.add_area("area 1", AREA)
    inventory = Inventory(
        devices=devices,
        not_testable=[],
        unmanaged=[],
        scanners=SCANNERS,
        ha_version="2026.10.2",
        integration_version="1.7.0",
        masker=masker,
        scanner_labels=labels,
        # The fragile proxy's own discovered devices: its reach is known.
        discovered={FRAGILE: ["00:00:5E:00:53:99"]} if discovered is None else discovered,
    )
    report = RunReport.from_inventory(
        inventory,
        fragile_proxy="bt-proxy",
        mode="run",
        started="now",
        expected_version="1.7.0",
        window="w",
    )
    kwargs.setdefault("daytime_override", True)
    runner = Runner(
        ha,
        ha,
        inventory,
        out_dir=tmp_path,
        report=report,  # type: ignore[arg-type]
        log=lambda _text: None,
        sleep=slept.append,
        **kwargs,
    )
    ha.journal_path = runner.journal.path
    return runner, slept


def test_fake_ha_run(tmp_path, monkeypatch) -> None:  # noqa: PLR0915 - one run, end to end
    """The inventory and its masking, a full run, its journal and reports, then --restore-from.

    The inventory comes from a made-up registry (registry.py); the run from a fake Home
    Assistant that follows the integration's zone relight rule.
    """
    inv = parse_inventory(raw_inventory())
    by_label = {d.label: d for d in inv.devices}

    assert list(by_label) == ["H5083 #1", "H5086 #1", "H6008 #1", "H60A6 #1"]
    # Scanners: local first, then proxies, each in address order; whatever order they come in.
    assert inv.scanner_labels == {LOCAL: "local adapter", PROXY_1: "proxy A", PROXY_2: "proxy B"}
    lamp, outlets, retry = by_label["H60A6 #1"], by_label["H5086 #1"], by_label["H6008 #1"]
    assert (lamp.area_label, lamp.area, lamp.source_label) == ("area 3", "Test Room", "proxy B")
    # Every scanner that passed on its advertisements: the fragile-proxy rule's paths.
    assert lamp.heard_by == frozenset({LOCAL, PROXY_2})
    assert lamp.fragile(frozenset({PROXY_2}))
    assert not by_label["H5083 #1"].fragile(frozenset({PROXY_2}))
    # A per-outlet relay is a plug, with its outlets; a plug found optimistic.
    assert outlets.is_plug
    assert [o.zone for o in outlets.outlets] == ["1", "2"]
    assert by_label["H5083 #1"].optimistic
    # Not loaded: never testable, so a run never reads it before its test.
    assert not retry.available
    assert inv.not_testable == [
        ("H60A6 (entry #1)", "config entry disabled (user)"),
        ("H6199 (entry #2)", "discovery ignored in Home Assistant"),
        ("H6008 #1", "config entry setup_retry"),
    ]
    assert inv.unmanaged == [
        ("H6199 (heard #1)", "heard through proxy A, no govee_ble_local entry")
    ]
    assert (inv.integration_version, inv.requirements) == (
        "1.7.0rc1",
        ["govee-ble-local>=1.7.0rc1,<2"],
    )

    # Masking: every identifier the inventory knows becomes its label.
    for text, masked in MASKING:
        assert inv.masker.text(text) == masked, text
        assert inv.masker.data({text: [text]}) == {masked: [masked]}
    # Short names too, as whole words only; never the reports' own words.
    masker = Masker()
    masker.add("H6052 #1", names=("Lamp", "Ivy"), hex_ids=("1",))
    assert (
        masker.text("Ivy is on; Ivyleaf, a lamp, the light")
        == "H6052 #1 is on; Ivyleaf, a lamp, the light"
    )
    # Errors are reported by key and the underlying error, never the message that names
    # the device by its entry title.
    error = ServiceError(
        "home_assistant_error",
        "Could not reach GVH60A6ABCD: timed out",
        "govee_ble_local",
        "ble_command_failed",
        {"device_name": "GVH60A6ABCD", "error": "timed out"},
    )
    assert error.describe() == "ble_command_failed: timed out"
    # The library version a requirement names: a wheel URL's, a pin or a floor.
    assert (
        library_version(["govee-ble-local @ https://x/govee_ble_local-1.7.0rc1-py3-none-any.whl"])
        == "1.7.0rc1"
    )
    assert library_version(["govee-ble-local==1.7.0"]) == "1.7.0"
    assert inv.requirements == ["govee-ble-local>=1.7.0rc1,<2"]
    assert library_version(inv.requirements) == "1.7.0rc1"

    # Credentials: one owner-only file, made-up values here. Refused while group or others
    # can read it (the error names the fix, never a value); absent, a clean skip; every
    # value it holds is kept out of all text; never sent anywhere but localhost, and never
    # along a redirect.
    credentials = tmp_path / "credentials"
    credentials.write_text(
        "# made up\nha_token = made-up.test-token\nplug_secret = 0011223344556677\n"
    )
    credentials.chmod(0o644)
    with pytest.raises(ha_api.HAError, match="chmod 600") as refused:
        ha_api.load_credentials(str(credentials))
    assert "made-up" not in str(refused.value)
    credentials.chmod(0o600)
    loaded = ha_api.load_credentials(str(credentials))
    assert loaded == {"ha_token": "made-up.test-token", "plug_secret": "0011223344556677"}
    assert (
        inv.masker.text("Bearer made-up.test-token, 0011223344556677") == "Bearer <token>, <token>"
    )
    bare = tmp_path / "ha_token"
    bare.write_text("made-up.bare-token\n")
    bare.chmod(0o600)
    assert ha_api.read_token(str(bare)) == "made-up.bare-token"  # the older one-token form
    with pytest.raises(ha_api.CredentialsMissingError, match="Credentials"):
        ha_api.load_credentials(str(tmp_path / "absent"))
    with pytest.raises(ha_api.HAError, match="only http"):
        ha_api.RestClient("http://ha.example.net:8123", loaded["ha_token"])
    with pytest.raises(ha_api.HAError, match="redirect"):
        ha_api._NoRedirect().redirect_request()
    # No proxy either, whatever the environment names: the token would go to it in clear.
    monkeypatch.setenv("http_proxy", "http://proxy.example.net:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.net:3128")
    client = ha_api.RestClient("http://127.0.0.1:8123", loaded["ha_token"])

    def proxied(opener: urllib.request.OpenerDirector) -> list[Any]:
        return [h for h in opener.handlers if getattr(h, "proxies", None)]

    assert proxied(urllib.request.build_opener())  # what urllib would do on its own
    assert proxied(client._opener) == []

    # The run: a zoned light found off (its zones keep their colours, which HA hides), and a
    # plug.
    lamp, plug_dev = _lamp(lit=False), _plug_device()
    ha = FakeHA(lit=False, plug_state="on")
    runner, slept = _runner(ha, tmp_path, [plug_dev, lamp])

    runner.run([plug_dev, lamp], plugs={"H5083 #1"})

    plug_result, lamp_result = runner.report.devices
    # Every step answered as planned: the plug by an advertisement after the library's
    # suppression window, the light read back.
    assert {s.step_id: s.verdict for s in lamp_result.steps if s.verdict != "pass"} == {}
    assert [(s.step_id, s.verdict, s.readback) for s in plug_result.steps] == [
        ("plug.off", "pass", "advertisement"),
        ("plug.on", "pass", "advertisement"),
    ]
    # Put back as found: the light off, every zone with the colour it kept while off.
    assert (lamp_result.restore["ok"], plug_result.restore["ok"]) == (True, True)
    assert ha.colours == START
    assert not any(ha.lit.values())
    assert ha.switches[PLUG] == "on"
    # The journal held every start state before the first change; it is private and says
    # each device was put back; it kept the zones' colours as the first power-on lit them.
    first = ha.journal_at_first_change
    assert first is not None
    assert set(first["devices"]) == {"H5083 #1", "H60A6 #1"}
    journal = json.loads(runner.journal.path.read_text())
    assert all(d["restored"] for d in journal["devices"].values())
    assert set(journal["devices"]["H60A6 #1"]["lit"]) == {MAIN, BACK}
    assert stat.S_IMODE(runner.journal.path.stat().st_mode) == 0o600
    assert any(s >= 10 for s in slept)  # read-backs spaced past HA's refresh debouncer
    # The reports name nothing real: no address, name, area, scanner name or entity id.
    saved = (tmp_path / "report.json").read_text() + (tmp_path / "report.md").read_text()
    for identifier in (
        ADDRESS,
        "00:00:5E",
        TITLE,
        USER_NAME,
        AREA,
        "hci0",
        "bt-proxy",
        FIXTURE,
        "entry-1",
    ):
        assert identifier not in saved
    assert "proxy A (a failure through it stops the run)" in saved

    # --restore-from on that journal: everything already as found, so nothing is sent.
    calls = len(ha.calls)
    old = Journal.load(runner.journal.path)
    for record in old.data["devices"].values():
        record["restored"] = False
    assert runner.restore_from(old) == {
        "H5083 #1": "already as found",
        "H60A6 #1": "already as found",
    }
    assert len(ha.calls) == calls
    # Changed since: the light goes back; the plug relay only with the user's yes; a
    # record naming another device's entities, or a gone entry, is refused.
    ha.colours[FIXTURE]["rgb_color"] = [0, 0, 255]  # the colour it keeps while off
    ha.switches[PLUG] = "off"
    foreign = copy.deepcopy(old.data["devices"]["H60A6 #1"])
    foreign["entities"]["light.someone_elses"] = foreign["entities"].pop(FIXTURE)
    gone = {**copy.deepcopy(old.data["devices"]["H60A6 #1"]), "entry_id": "entry-gone"}
    old.data["devices"].update({"foreign": foreign, "gone": gone})
    for record in old.data["devices"].values():
        record["restored"] = False

    assert runner.restore_from(old) == {
        "H5083 #1": "left as it is: its relays weren't confirmed",
        "H60A6 #1": "put back",
        "foreign": "refused: the journal names entities that aren't the device's",
        "gone": "refused: its config entry is gone",
    }
    assert ha.colours == START
    assert ha.switches[PLUG] == "off"
    # A restore call through the fragile proxy that times out ends the recovery after that
    # device, which is still put back (its idempotent calls retry): it says why, and names
    # each device it leaves.
    runner.inventory.devices = [
        dataclasses.replace(d, heard_by=frozenset({FRAGILE})) if d is lamp else d
        for d in runner.inventory.devices
    ]
    ha.colours[FIXTURE]["rgb_color"] = [0, 0, 255]
    again = Journal.load(runner.journal.path)
    again.data["devices"] = {
        label: {**again.data["devices"][label], "restored": False}
        for label in ("H60A6 #1", "H5083 #1")
    }
    ha.fail_on[len(ha.calls)] = TimeoutError("no reply from Home Assistant in time")

    assert runner.restore_from(again) == {
        "H60A6 #1": "put back",
        "H5083 #1": "not attempted: the recovery stopped (fragile proxy)",
    }
    assert ha.colours == START
    log = (tmp_path / "run.log").read_text()
    assert "stopping: a restore call to H60A6 #1 raised TimeoutError" in log
    assert "H5083 #1: not attempted" in log


def _hang_up(terminal: dict[str, bool]) -> Callable[[], None]:
    def hang_up() -> None:
        terminal["gone"] = True
        os.kill(os.getpid(), signal.SIGHUP)

    return hang_up


def _terminate_twice() -> None:
    try:
        os.kill(os.getpid(), signal.SIGTERM)  # os.kill runs the handler before it returns
    except AbortRunError:
        os.kill(os.getpid(), signal.SIGTERM)  # a second one while the first unwinds
        raise


GUARDS: dict[str, dict[str, Any]] = {
    # A stop restores first, whatever stopped it; the journal says "restored" only if the
    # last check found nothing different.
    "Ctrl-C mid test": {"fail": {3: KeyboardInterrupt()}, "stopped": "Ctrl-C"},
    # The light.on step loses the link; the restore's first call finds it mid-frame.
    "a lost connection, the restore reconnects": {
        "fail": {2: OSError("reset"), 3: OSError("mid-frame")},
        "stopped": "OSError: reset",
        "connects": 1,
    },
    "SIGHUP: the terminal is gone, output can't stop the restore": {
        "fail": {3: "hang up"},
        "stopped": "signal SIGHUP",
    },
    # The first signal makes every handler note-only, so the second can't cut the unwind
    # short before the restore's own handlers are in place.
    "SIGTERM twice: the second only noted": {
        "fail": {3: _terminate_twice},
        "stopped": "signal SIGTERM",
        "log": "stopping: the device under test is put back first",
    },
    "the window closes mid device": {"night_after": 3, "stopped": "outside the test window"},
    "a radio failure the fragile proxy could carry": {
        "fragile": True,
        "fail": {2: BLE_FAILURE},
        "stopped": "fragile proxy",
    },
    # The restore's retry puts it back; the run still ends after this device.
    "a radio failure in a restore call through the fragile proxy": {
        "fragile": True,
        "fail": {"restore": BLE_FAILURE},
        "stopped": "a restore call",
    },
    "a validation refusal through the fragile proxy doesn't stop it": {"fragile": True},
    # Raised, not answered: whether it reached the radio is unknown, so it counts.
    "a call through the fragile proxy that raises": {
        "fragile": True,
        "fail": {2: OSError("timed out")},
        "stopped": "fragile proxy",
    },
    # Its reach unknown (it discovered nothing): it counts as reaching every device.
    "the fragile proxy's reach unknown: a radio failure stops the run": {
        "discovered": {},
        "fail": {2: BLE_FAILURE},
        "stopped": "fragile proxy",
        "note": "proxy A's reach is unknown (it has discovered no device)",
    },
    # An optimistic plug's advertisement inside the library's suppression window after the
    # call proves nothing: unverified, not a pass.
    "an advertisement inside the suppression window": {
        "devices": "plug, then light",
        "plugs": {"H5083 #1"},
        "advert_step": 10,
        "unverified": ["plug.off", "plug.on"],
    },
    "an on/off zone left off by an interrupted step is switched back on": {
        "onoff_back": True,
        "interrupt_after": "zone_background.off",
        "stopped": "Ctrl-C",
    },
    # A plug the user named, after a stop: never started, still a confirmed plug.
    "a stop before a confirmed plug": {
        "devices": "light, then plug",
        "plugs": {"H5083 #1"},
        "fail": {3: KeyboardInterrupt()},
        "stopped": "Ctrl-C",
    },
    "a self-test abandoned with Ctrl-C x3": {
        "self_test": True,
        "fail": {"press": AbandonRestoreError()},
        "stopped": "abandoned",
        "restored": False,
    },
    "a self-test call that fails is followed by the restore": {
        "self_test": True,
        "fail": {"press": OSError("timeout")},
        "stopped": "self-test call failed",
    },
    "the window closes before the self-test": {
        "self_test": True,
        "night_after": "the last step",
        "stopped": "outside the test window",
    },
    # Consent: nothing touched at all.
    "an unconfirmed plug": {"devices": "plug", "untouched": True},
    "outside the window": {
        "night_after": 0,
        "stopped": "outside the test window",
        "untouched": True,
    },
    "an entry Home Assistant hasn't loaded": {"devices": "retry", "untouched": True},
}


@pytest.mark.parametrize("case", GUARDS)
def test_guards(case: str, tmp_path, monkeypatch) -> None:
    spec = GUARDS[case]
    lamp = _lamp(
        lit=True,
        onoff_back=spec.get("onoff_back", False),
        heard_by=frozenset({FRAGILE}) if spec.get("fragile") else frozenset(),
    )
    devices = {
        "plug": [_plug_device()],
        "plug, then light": [_plug_device(), lamp],
        "light, then plug": [lamp, _plug_device()],
        "retry": [_lamp(lit=True, entry_id="entry-retry", entry_state="setup_retry")],
    }.get(spec.get("devices", ""), [lamp, _lamp(lit=True, label="H60A6 #2")])
    ha = FakeHA(lit=True, plug_state="on", onoff_back=spec.get("onoff_back", False))
    ha.advert_step = spec.get("advert_step", ha.advert_step)
    terminal = {"gone": False}
    checks = iter(range(10**6))
    night_after = spec.get("night_after")
    if night_after == "the last step":
        night_after = 1 + len(build_plan(lamp).steps)  # a check per device, then per step
    monkeypatch.setattr(sys, "stdout", sys.stdout)  # put back what a SIGHUP's detach replaces
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    runner, _ = _runner(
        ha,
        tmp_path,
        devices,
        discovered=spec.get("discovered"),
        daytime_override=night_after is None,
        self_test=spec.get("self_test", False),
        clock=lambda: NIGHT if night_after is not None and next(checks) >= night_after else DAY,
    )

    def write(_text: str) -> None:
        if terminal["gone"]:
            raise OSError(5, "Input/output error")

    runner.console._write = write
    for n, fail in spec.get("fail", {}).items():
        ha.fail_on[n] = _hang_up(terminal) if fail == "hang up" else fail
    if after := spec.get("interrupt_after"):
        ha.fail_on[_call_after(lamp, after)] = KeyboardInterrupt()

    runner.run(devices, plugs=spec.get("plugs", set()))

    if spec.get("untouched"):
        assert [c for c in ha.calls if c[0] != "homeassistant"] == []
        assert "entry-retry" not in ha.diagnostics_read
        assert ha.switches[PLUG] == "on"
        return
    stopped = spec.get("stopped")
    assert stopped in (runner.report.stopped or "") if stopped else runner.report.stopped is None
    if stopped:
        # The next device never started, and the report says so.
        assert [(d.restore, d.excluded) for d in runner.report.devices[1:]] == [
            ({"never_started": True}, None)
        ]
    for result in runner.report.devices:
        for step in result.steps:
            assert (step.verdict == "unverified") is (step.step_id in spec.get("unverified", ()))
    if note := spec.get("note"):
        assert any(note in n for n in runner.report.notes)
    # Put back first: the light as found, and every zone lit again.
    assert ha.colours == ({**START, BACK: {}} if spec.get("onoff_back") else START)
    assert all(ha.lit.values())
    journal = json.loads(runner.journal.path.read_text())
    assert journal["devices"]["H60A6 #1"]["restored"] is spec.get("restored", True)
    assert (tmp_path / "report.json").exists()
    log = (tmp_path / "run.log").read_text()
    assert "H60A6 #1 restored: " in log
    assert spec.get("log", "") in log
    if "SIGHUP" in case:
        assert not runner.console.terminal
    assert ha.connects == spec.get("connects", 0)


def _call_after(dev: DeviceDesc, step_id: str) -> int:
    """Return the number of the call right after ``step_id`` and its read-back.

    Each step is one call, then a read-back poll unless it is a refusal.
    """
    calls = 0
    for step in build_plan(dev).steps:
        calls += 1 if step.kind is Kind.REFUSAL else 2
        if step.step_id == step_id:
            return calls
    raise AssertionError(step_id)
