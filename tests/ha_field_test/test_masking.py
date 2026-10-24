"""The inventory's neutral labels, and the masking: nothing that identifies a home gets out.

All identifiers are made up: MACs from the RFC 7042 documentation range, invented names.
"""

from __future__ import annotations

from typing import Any

import pytest

from tools.ha_field_test import ha_api
from tools.ha_field_test.ha_api import ServiceError
from tools.ha_field_test.inventory import RawInventory, library_version, parse_inventory
from tools.ha_field_test.masking import Masker

LIGHT = "00:00:5E:00:53:01"
PLUG = "00:00:5E:00:53:02"
OUTLETS = "00:00:5E:00:53:04"
UNMANAGED = "00:00:5E:00:53:03"
LOCAL = "00:00:5E:00:53:A1"
PROXY_1 = "00:00:5E:00:53:B1"
PROXY_2 = "00:00:5E:00:53:B2"
SERIAL = "00:00:5E:00:53:F1:AA:01"
REVERSED = ":".join(reversed(LIGHT.split(":")))  # device-info replies carry it reversed


def _entity(address: str, key: str, entity_id: str, device_id: str, **extra: Any) -> dict:
    return {
        "platform": "govee_ble_local",
        "unique_id": f"{address}_{key}",
        "entity_id": entity_id,
        "device_id": device_id,
        "disabled_by": None,
        **extra,
    }


def _raw() -> RawInventory:
    entry = {"domain": "govee_ble_local", "state": "loaded"}
    return RawInventory(
        entries=[
            {**entry, "entry_id": "entry-light", "title": "GVH60A6ABCD"},
            {**entry, "entry_id": "entry-plug", "title": "ihoment_H5083_ABCE"},
            {**entry, "entry_id": "entry-outlets", "title": "ihoment_H5086_ABCF"},
            {**entry, "entry_id": "entry-retry", "title": "GVH6008ABD5", "state": "setup_retry"},
            {**entry, "entry_id": "e3", "title": "GVH60A6ABD2", "disabled_by": "user"},
            {**entry, "entry_id": "e4", "title": "Govee_H6199_ABD3", "source": "ignore"},
        ],
        devices=[
            {
                "id": "d1",
                "identifiers": [["govee_ble_local", LIGHT]],
                "config_entry_id": "entry-light",
                "model": "H60A6",
                "name": "GVH60A6ABCD",
                "name_by_user": "Test Light",
                "area_id": "test_room",
                "serial_number": SERIAL,
                "connections": [["bluetooth", LIGHT]],
            },
            {
                "id": "d2",
                "identifiers": [["govee_ble_local", PLUG]],
                "primary_config_entry": "entry-plug",
                "model": "H5083",
                "name": "ihoment_H5083_ABCE",
                "area_id": "other_room",
            },
            {
                "id": "d3",
                "identifiers": [["govee_ble_local", OUTLETS]],
                "config_entry_id": "entry-outlets",
                "model": "H5086",
                "name": "ihoment_H5086_ABCF",
                "area_id": "loft",
            },
            {
                "id": "d4",
                "identifiers": [["govee_ble_local", "00:00:5E:00:53:05"]],
                "config_entry_id": "entry-retry",
                "model": "H6008",
                "name": "GVH6008ABD5",
            },
        ],
        entities=[
            _entity(LIGHT, "light", "light.renamed_test_lamp", "d1"),
            _entity(LIGHT, "zone_main_light", "light.gvh60a6abcd_main_light", "d1"),
            _entity(
                LIGHT, "segment_3", "light.gvh60a6abcd_segment_4", "d1", disabled_by="integration"
            ),
            _entity(LIGHT, "connection_source", "sensor.gvh60a6abcd_connection_source", "d1"),
            _entity(PLUG, "power", "switch.ihoment_h5083_abce", "d2"),
            _entity(OUTLETS, "zone_1", "switch.ihoment_h5086_abcf_outlet_1", "d3"),
            _entity(OUTLETS, "zone_2", "switch.ihoment_h5086_abcf_outlet_2", "d3"),
        ],
        areas=[
            {"area_id": "test_room", "name": "Test Room"},
            {"area_id": "other_room", "name": "Other Room"},
            {"area_id": "loft", "name": "Loft"},
        ],
        states={
            "light.renamed_test_lamp": {"state": "off", "attributes": {}},
            "light.gvh60a6abcd_main_light": {"state": "off", "attributes": {}},
            "sensor.gvh60a6abcd_connection_source": {"state": PROXY_2},
            "switch.ihoment_h5083_abce": {"state": "on", "attributes": {}},
            "switch.ihoment_h5086_abcf_outlet_1": {"state": "on", "attributes": {}},
            "switch.ihoment_h5086_abcf_outlet_2": {"state": "off", "attributes": {}},
        },
        scanners=[
            {
                "source": PROXY_2,
                "name": f"bt-proxy ({PROXY_2})",
                "adapter": "bt-proxy",
                "scanner_type": "remote",
            },
            {"source": LOCAL, "name": f"hci0 ({LOCAL})", "adapter": "hci0", "scanner_type": "usb"},
            {
                "source": PROXY_1,
                "name": f"test-gateway-abc123 ({PROXY_1})",
                "adapter": "test-gateway-abc123",
                "scanner_type": "remote",
            },
        ],
        diagnostics={
            "entry-light": {
                "custom_components": {
                    "govee_ble_local": {
                        "version": "1.7.0rc1",
                        "requirements": ["govee-ble-local>=1.7.0rc1,<2"],
                    }
                },
                "data": {
                    "device": {"sku": "H60A6", "capabilities": ["power", "rgb"], "zones": ["main"]},
                    "state": {"is_on": False, "optimistic": False},
                },
            },
            "entry-plug": {"data": {"device": {"sku": "H5083"}, "state": {"optimistic": True}}},
            "entry-outlets": {
                "data": {"device": {"sku": "H5086", "zones": ["1", "2"]}, "state": {}}
            },
        },
        adverts=[
            {
                "address": UNMANAGED,
                "name": "Govee_H6199_ABD4",
                "manufacturer_data": {"34818": "ec0001"},
                "source": PROXY_1,
            },
            {"address": LIGHT, "name": "GVH60A6ABCD", "source": LOCAL},
            {"address": LIGHT, "name": "GVH60A6ABCD", "source": PROXY_2},
            {"address": "00:00:5E:00:53:09", "name": "TV", "manufacturer_data": {"76": "0215"}},
        ],
        ha_version="2026.10.2",
    )


MASKING = [
    # A device's identifiers, in every spelling, become its label.
    (f"connect to {LIGHT} failed", "connect to H60A6 #1 failed"),
    (f"to 00-00-5e-00-53-01, 00005E005301, {REVERSED}", "to H60A6 #1, H60A6 #1, H60A6 #1"),
    (f"serial {SERIAL}", "serial H60A6 #1"),
    ("GVH60A6ABCD and Test Light", "H60A6 #1 and H60A6 #1"),
    ("light.gvh60a6abcd_main_light", "H60A6 #1"),
    ("light.gvh60a6abcd_segment_9", "light.h60a6_1_segment_9"),
    # An entity id renamed since, and an entry id, from Home Assistant's error text.
    ("light.renamed_test_lamp is unavailable", "H60A6 #1 is unavailable"),
    ("entry entry-light failed", "entry H60A6 #1 failed"),
    # Scanners by their labels: name, ESPHome hostname, address.
    (f"through bt-proxy ({PROXY_2}) or test-gateway-abc123", "through proxy B or proxy A"),
    # Areas by number, as whole words only.
    (
        "Test Room lamp, the loft lamp, a Lofty idea",
        "area 3 lamp, the area 1 lamp, a Lofty idea",
    ),
    # Anything else: no byte of a MAC left, not even its vendor prefix.
    ("via 00:00:5E:00:53:99 or /dev_00_00_5E_00_53_97", "via <mac> or /dev_<mac>"),
    ("heard ihoment_H6006_ABAA and GVH6008ABBB", "heard ihoment_H6006_xxxx and GVH6008xxxx"),
    ("frame 3305150101ff00 at 2700 K, rgb_ff0000", "frame <hex> at 2700 K, rgb_ff0000"),
    ("a Lamp in the light", "a Lamp in the light"),
]


def test_inventory_and_masking(tmp_path) -> None:
    """The inventory's labels, and what the masker makes of every kind of identifier."""
    inv = parse_inventory(_raw())
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

    # The token: owner-only, never in any text, never sent anywhere but localhost, and
    # never along a redirect.
    token_file = tmp_path / "token"
    token_file.write_text("made-up.test-token-value\n")
    token_file.chmod(0o644)
    with pytest.raises(ha_api.HAError, match="owner-only"):
        ha_api.read_token(str(token_file))
    token_file.chmod(0o600)
    token = ha_api.read_token(str(token_file))
    assert inv.masker.text(f"Bearer {token}") == "Bearer <token>"
    with pytest.raises(ha_api.HAError, match="only http"):
        ha_api.RestClient("http://ha.example.net:8123", token)
    with pytest.raises(ha_api.HAError, match="redirect"):
        ha_api._NoRedirect().redirect_request()
