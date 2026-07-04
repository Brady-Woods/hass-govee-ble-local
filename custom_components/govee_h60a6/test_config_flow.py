#!/usr/bin/env python3
"""Config-flow tests for the Govee H60A6 integration.

Run directly: python3 test_config_flow.py
(or: python3 -m unittest test_config_flow -v)

Same philosophy as test_protocol.py: stdlib-only, with just-enough stubs of
Home Assistant's config-entries/bluetooth machinery that the REAL
config_flow.py is imported and exercised (not a reimplementation). The stubs
model HA's documented flow-helper semantics (unique-id dedup, abort/return
shapes) so the branching logic in config_flow.py - discovery filtering,
dedup of already-configured devices, abort reasons, and the reconfigure
reachability check - is what's actually under test.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

INTEGRATION_DIR = Path(__file__).resolve().parent

# config_flow now imports govee_ble_local; make the sibling library repo's src
# importable so this stdlib test can load it without a pip install. (The
# profile layer needs only PyYAML, not bleak.)
_LIB_SRC = Path(__file__).resolve().parents[3] / "govee-ble-local" / "src"
if _LIB_SRC.is_dir():
    sys.path.insert(0, str(_LIB_SRC))


class _StubHass:
    """Minimal hass: runs executor jobs inline."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _AbortFlow(Exception):
    """Stand-in for homeassistant.data_entry_flow.AbortFlow."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _install_stubs() -> None:
    """Fake just the HA + voluptuous surface config_flow.py imports/uses."""
    if "voluptuous" not in sys.modules:
        vol = types.ModuleType("voluptuous")
        vol.Schema = lambda schema: schema
        vol.Required = lambda key: key
        vol.In = lambda container: container
        sys.modules["voluptuous"] = vol

    if "homeassistant" not in sys.modules:
        sys.modules["homeassistant"] = types.ModuleType("homeassistant")

    if "homeassistant.config_entries" not in sys.modules:
        ce = types.ModuleType("homeassistant.config_entries")

        ce.ConfigFlowResult = dict

        class ConfigFlow:
            """Minimal ConfigFlow base mirroring the helpers config_flow uses."""

            def __init_subclass__(cls, **kwargs: object) -> None:
                # Swallow `domain=...` from the class definition.
                super().__init_subclass__()

            async def async_set_unique_id(
                self, unique_id: str, *, raise_on_progress: bool = True
            ) -> None:
                self._unique_id = unique_id

            def _abort_if_unique_id_configured(self) -> None:
                if self._unique_id in getattr(self, "_configured_ids", set()):
                    raise _AbortFlow("already_configured")

            def _async_current_ids(self) -> set[str]:
                return getattr(self, "_configured_ids", set())

            def _set_confirm_only(self) -> None:
                pass

            def async_create_entry(self, *, title: str, data: dict) -> dict:
                return {"type": "create_entry", "title": title, "data": data}

            def async_show_form(
                self,
                *,
                step_id: str,
                data_schema: object = None,
                description_placeholders: dict | None = None,
            ) -> dict:
                return {
                    "type": "form",
                    "step_id": step_id,
                    "data_schema": data_schema,
                    "description_placeholders": description_placeholders,
                }

            def async_abort(self, *, reason: str) -> dict:
                return {"type": "abort", "reason": reason}

            def _get_reconfigure_entry(self) -> object:
                return self._reconfigure_entry

            def async_update_reload_and_abort(self, entry: object, *, data: dict) -> dict:
                return {"type": "abort", "reason": "reconfigure_successful", "data": data}

        ce.ConfigFlow = ConfigFlow
        sys.modules["homeassistant.config_entries"] = ce

    if "homeassistant.components" not in sys.modules:
        sys.modules["homeassistant.components"] = types.ModuleType(
            "homeassistant.components"
        )

    if "homeassistant.components.bluetooth" not in sys.modules:
        bt = types.ModuleType("homeassistant.components.bluetooth")

        class BluetoothServiceInfoBleak:
            def __init__(self, address: str, name: str, device: object = None) -> None:
                self.address = address
                self.name = name
                self.device = device

        # Test-controlled state.
        bt._discovered: list = []
        bt._ble_devices: dict = {}

        def async_discovered_service_info(hass: object) -> list:
            return bt._discovered

        def async_ble_device_from_address(
            hass: object, address: str, connectable: bool = True
        ) -> object:
            return bt._ble_devices.get(address)

        bt.BluetoothServiceInfoBleak = BluetoothServiceInfoBleak
        bt.async_discovered_service_info = async_discovered_service_info
        bt.async_ble_device_from_address = async_ble_device_from_address
        sys.modules["homeassistant.components.bluetooth"] = bt


def _load_real_config_flow():
    """Load the real const.py + config_flow.py as a fake package."""
    pkg_name = "_govee_h60a6_cf_under_test"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(INTEGRATION_DIR)]
    sys.modules[pkg_name] = pkg

    def _load(module_name: str):
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{module_name}", INTEGRATION_DIR / f"{module_name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{module_name}"] = module
        spec.loader.exec_module(module)
        return module

    _load("const")
    return _load("config_flow")


_install_stubs()
config_flow_mod = _load_real_config_flow()
bluetooth_stub = sys.modules["homeassistant.components.bluetooth"]
GoveeH60A6ConfigFlow = config_flow_mod.GoveeH60A6ConfigFlow
BluetoothServiceInfoBleak = bluetooth_stub.BluetoothServiceInfoBleak


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_flow(configured_ids=None, reconfigure_entry=None):
    flow = GoveeH60A6ConfigFlow()
    flow.hass = _StubHass()
    flow.context = {}
    flow._configured_ids = set(configured_ids or [])
    if reconfigure_entry is not None:
        flow._reconfigure_entry = reconfigure_entry
    return flow


class _FakeEntry:
    def __init__(self, address: str, title: str) -> None:
        self.data = {"address": address}
        self.title = title


def _reset_bluetooth():
    bluetooth_stub._discovered = []
    bluetooth_stub._ble_devices = {}


class TestBluetoothStep(unittest.TestCase):
    def setUp(self) -> None:
        _reset_bluetooth()

    def test_discovery_shows_confirm_form(self) -> None:
        flow = make_flow()
        info = BluetoothServiceInfoBleak("AA:BB:CC:DD:EE:FF", "GVH60A67457")
        result = run(flow.async_step_bluetooth(info))
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "bluetooth_confirm")
        self.assertEqual(result["description_placeholders"], {"name": "GVH60A67457"})
        self.assertEqual(flow.context["title_placeholders"], {"name": "GVH60A67457"})

    def test_confirm_creates_entry(self) -> None:
        flow = make_flow()
        info = BluetoothServiceInfoBleak("AA:BB:CC:DD:EE:FF", "GVH60A67457")
        run(flow.async_step_bluetooth(info))
        result = run(flow.async_step_bluetooth_confirm({}))
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["title"], "GVH60A67457")
        # SKU resolved via the profile system and stored on the entry.
        self.assertEqual(result["data"], {"address": "AA:BB:CC:DD:EE:FF", "sku": "H60A6"})

    def test_already_configured_aborts(self) -> None:
        flow = make_flow(configured_ids={"AA:BB:CC:DD:EE:FF"})
        info = BluetoothServiceInfoBleak("AA:BB:CC:DD:EE:FF", "GVH60A67457")
        with self.assertRaises(_AbortFlow) as ctx:
            run(flow.async_step_bluetooth(info))
        self.assertEqual(ctx.exception.reason, "already_configured")

    def test_unsupported_model_aborts(self) -> None:
        # Manufacturer-id discovery catches any Govee device; the profile
        # system rejects models it doesn't support.
        flow = make_flow()
        info = BluetoothServiceInfoBleak("AA:BB:CC:DD:EE:99", "GVH5179ABCD")
        result = run(flow.async_step_bluetooth(info))
        self.assertEqual(result["type"], "abort")
        self.assertEqual(result["reason"], "not_supported")


class TestUserStep(unittest.TestCase):
    def setUp(self) -> None:
        _reset_bluetooth()

    def test_lists_only_matching_unconfigured_devices(self) -> None:
        bluetooth_stub._discovered = [
            BluetoothServiceInfoBleak("AA:BB:CC:DD:EE:01", "GVH60A67457"),
            BluetoothServiceInfoBleak("AA:BB:CC:DD:EE:02", "GVH60A6D075"),
            BluetoothServiceInfoBleak("AA:BB:CC:DD:EE:03", "GVH5179XYZ"),  # wrong model
            BluetoothServiceInfoBleak("AA:BB:CC:DD:EE:04", "GVH60A6FF43"),
        ]
        # EE:04 already configured -> should be filtered out.
        flow = make_flow(configured_ids={"AA:BB:CC:DD:EE:04"})
        result = run(flow.async_step_user())
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "user")
        offered = result["data_schema"]["address"]
        self.assertIn("AA:BB:CC:DD:EE:01", offered)
        self.assertIn("AA:BB:CC:DD:EE:02", offered)
        self.assertNotIn("AA:BB:CC:DD:EE:03", offered)  # non-H60A6 excluded
        self.assertNotIn("AA:BB:CC:DD:EE:04", offered)  # already configured excluded

    def test_no_devices_aborts(self) -> None:
        bluetooth_stub._discovered = [
            BluetoothServiceInfoBleak("AA:BB:CC:DD:EE:03", "GVH5179XYZ"),
        ]
        flow = make_flow()
        result = run(flow.async_step_user())
        self.assertEqual(result["type"], "abort")
        self.assertEqual(result["reason"], "no_devices_found")

    def test_selection_creates_entry(self) -> None:
        flow = make_flow()
        flow._discovered_devices = {"AA:BB:CC:DD:EE:01": "GVH60A67457"}
        flow._discovered_skus = {"AA:BB:CC:DD:EE:01": "H60A6"}
        result = run(flow.async_step_user({"address": "AA:BB:CC:DD:EE:01"}))
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["title"], "GVH60A67457")
        self.assertEqual(result["data"], {"address": "AA:BB:CC:DD:EE:01", "sku": "H60A6"})


class TestReconfigureStep(unittest.TestCase):
    def setUp(self) -> None:
        _reset_bluetooth()

    def test_shows_form_first(self) -> None:
        entry = _FakeEntry("AA:BB:CC:DD:EE:01", "Kitchen")
        flow = make_flow(reconfigure_entry=entry)
        result = run(flow.async_step_reconfigure())
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "reconfigure")
        self.assertEqual(result["description_placeholders"], {"name": "Kitchen"})

    def test_success_when_device_reachable(self) -> None:
        entry = _FakeEntry("AA:BB:CC:DD:EE:01", "Kitchen")
        bluetooth_stub._ble_devices = {"AA:BB:CC:DD:EE:01": object()}
        flow = make_flow(reconfigure_entry=entry)
        result = run(flow.async_step_reconfigure({}))
        self.assertEqual(result["type"], "abort")
        self.assertEqual(result["reason"], "reconfigure_successful")

    def test_aborts_when_device_not_found(self) -> None:
        entry = _FakeEntry("AA:BB:CC:DD:EE:01", "Kitchen")
        bluetooth_stub._ble_devices = {}  # not reachable
        flow = make_flow(reconfigure_entry=entry)
        result = run(flow.async_step_reconfigure({}))
        self.assertEqual(result["type"], "abort")
        self.assertEqual(result["reason"], "not_found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
