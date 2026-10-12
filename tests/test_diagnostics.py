"""Tests for the config-entry diagnostics (redaction by key and by value)."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from govee_ble_local import Device
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.govee_ble_local.const import CONF_SECRET, CONF_SKU, DOMAIN

from .const import ADDRESS, SECRET_HEX, TITLE

REDACTED = "**REDACTED**"
WIFI_MAC = "11:22:33:44:55:66"
BLE_MAC = "77:88:99:AA:BB:CC"


def _octets(mac: str) -> list[str]:
    return mac.split(":")


def _forms(mac: str) -> list[str]:
    """Every spelling of a MAC the scrubber must catch."""
    octets = _octets(mac)
    compact = "".join(octets)
    reversed_compact = "".join(reversed(octets))
    return [
        mac,
        mac.lower(),
        mac.upper(),
        "-".join(octets),
        compact.lower(),
        compact.upper(),
        reversed_compact.lower(),
        reversed_compact.upper(),
    ]


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """An entry carrying a secret key (which must never appear in diagnostics)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=ADDRESS,
        data={"address": ADDRESS, CONF_SKU: "H60A6", CONF_SECRET: SECRET_HEX},
    )


async def test_diagnostics_structure(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    setup_integration: MockConfigEntry,
    mock_device: Device,
) -> None:
    """Diagnostics describe the device and coordinator, with identifying entry data redacted."""
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, setup_integration)

    assert diagnostics["entry"] == {
        "title": TITLE,
        "data": {"address": REDACTED, "sku": "H60A6", "secret": REDACTED},
    }
    assert diagnostics["device"] == {
        "sku": "H60A6",
        "capabilities": ["brightness", "color_temp", "power", "rgb", "scenes", "segments"],
        "zones": ["main", "background"],
        "scene_count": len(mock_device.scene_names),
    }
    assert diagnostics["device"]["scene_count"] > 3
    coordinator = diagnostics["coordinator"]
    assert coordinator["last_update_success"] is True
    assert coordinator["consecutive_failures"] == 0
    assert coordinator["total_failures"] == 0
    assert coordinator["device_available"] is True
    assert coordinator["update_interval"] == str(
        setup_integration.runtime_data.coordinator.update_interval
    )
    assert "is_on" in diagnostics["state"]
    assert diagnostics["recent_warnings"] == []


async def test_diagnostics_without_self_test(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    setup_integration: MockConfigEntry,
) -> None:
    """Before any self-test has run, ``last_self_test`` is null."""
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, setup_integration)

    assert diagnostics["last_self_test"] is None


async def test_diagnostics_entry_without_secret(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """An entry that has no secret stored still produces diagnostics."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=ADDRESS,
        data={"address": ADDRESS, CONF_SKU: "H60A6"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diagnostics["entry"]["data"] == {"address": REDACTED, "sku": "H60A6"}
    assert diagnostics["last_self_test"] is None


async def test_identifiers_are_redacted_by_key_and_by_value(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    setup_integration: MockConfigEntry,
) -> None:
    """No address, MAC, serial or secret survives, in any spelling, anywhere in the dump."""
    coordinator = setup_integration.runtime_data.coordinator
    coordinator.data.wifi_mac = WIFI_MAC
    coordinator.data.ble_mac = BLE_MAC
    coordinator.data.serial_number = "7"

    address_lower = ADDRESS.lower()
    address_compact = ADDRESS.replace(":", "")
    address_reversed = "".join(reversed(_octets(ADDRESS))).lower()
    wifi_compact = WIFI_MAC.replace(":", "")
    wifi_reversed = "".join(reversed(_octets(WIFI_MAC)))
    coordinator.last_self_test = {
        "ok": False,
        "restored": True,
        "restore_errors": [],
        "address": ADDRESS,
        "sku": "H60A6",
        "encryption": "none",
        "capabilities": ["power"],
        "steps": [
            {
                "step": "power_on",
                "ok": False,
                "acked": False,
                "error": f"timeout talking to {address_lower} after 7 tries",
            },
            {"step": "rgb", "ok": True, "acked": True, "error": None},
        ],
        "frames": [
            f"{address_lower} tx info plain=3301 wire=3301{SECRET_HEX} enc=none",
            f"{address_compact} rx info plain=aa{wifi_compact} wire={wifi_reversed} enc=none",
            f"AA-BB-CC-DD-EE-05 tx x plain=01{SECRET_HEX.upper()}02 wire={address_reversed}",
        ],
        "log": [
            f"INFO {ADDRESS}: session ready",
            f"DEBUG device info mac={address_reversed} ble={BLE_MAC.lower()}",
            f"WARNING wifi {WIFI_MAC.lower()} secret {SECRET_HEX}",
        ],
    }
    logging.getLogger("govee_ble_local").warning(
        "%s: unrecognised frame wire=%s wifi=%s", ADDRESS, SECRET_HEX, wifi_compact
    )

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, setup_integration)
    dump = json.dumps(diagnostics).lower()

    literals = [
        *_forms(ADDRESS),
        *_forms(WIFI_MAC),
        *_forms(BLE_MAC),
        SECRET_HEX,
        SECRET_HEX.upper(),
    ]
    assert not [literal for literal in literals if literal.lower() in dump]
    assert REDACTED.lower() in dump

    state = diagnostics["state"]
    assert state["wifi_mac"] == REDACTED
    assert state["ble_mac"] == REDACTED
    assert state["serial_number"] == REDACTED
    assert diagnostics["entry"]["data"]["secret"] == REDACTED

    report = diagnostics["last_self_test"]
    assert report["address"] == REDACTED
    assert report["steps"][0]["error"] == f"timeout talking to {REDACTED} after 7 tries"
    assert report["frames"][0] == f"{REDACTED} tx info plain=3301 wire=3301{REDACTED} enc=none"
    assert report["log"][0] == f"INFO {REDACTED}: session ready"
    # The warning captured for this device is present, with identifiers scrubbed.
    assert diagnostics["recent_warnings"] == [
        f"WARNING {REDACTED}: unrecognised frame wire={REDACTED} wifi={REDACTED}"
    ]


async def test_short_identifiers_do_not_over_redact(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    setup_integration: MockConfigEntry,
) -> None:
    """A short serial is redacted by key only; its digit is not scrubbed out of other text."""
    coordinator = setup_integration.runtime_data.coordinator
    coordinator.data.serial_number = "7"
    coordinator.data.firmware_version = "1.07.1"
    coordinator.last_self_test = {
        "ok": True,
        "address": ADDRESS,
        "steps": [{"step": "rgb", "ok": True, "error": "retry 7 of 17"}],
        "frames": ["x rx status plain=07 enc=none"],
        "log": ["INFO step 7 done"],
    }

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, setup_integration)

    assert diagnostics["state"]["serial_number"] == REDACTED
    assert diagnostics["state"]["firmware_version"] == "1.07.1"
    report = diagnostics["last_self_test"]
    assert report["steps"][0]["error"] == "retry 7 of 17"
    assert report["frames"] == ["x rx status plain=07 enc=none"]
    assert report["log"] == ["INFO step 7 done"]
    assert REDACTED not in json.dumps(report["log"] + report["frames"])


async def test_entry_with_only_short_identifiers_is_left_unscrubbed(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """With nothing long enough to match, text is passed through (keys are still redacted)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id="A:1",
        data={"address": "A:1", CONF_SKU: "H60A6"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.coordinator.last_self_test = {
        "address": "A:1",
        "log": ["INFO A:1 and A1 stay readable"],
    }

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diagnostics["entry"]["data"]["address"] == REDACTED
    assert diagnostics["last_self_test"] == {
        "address": REDACTED,
        "log": ["INFO A:1 and A1 stay readable"],
    }
