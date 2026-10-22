"""Tests for the switch platform: the smart-plug power switch and zone switches."""

from __future__ import annotations

import dataclasses
import logging
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from bleak.exc import BleakError
from govee_ble_local import (
    Device,
    GoveeBleAuthError,
    GoveeBleConnectionError,
    GoveeBleHandshakeError,
    GoveeBleNotSupported,
    GoveeBleTimeout,
    Zone,
    supported_skus,
)
from govee_ble_local.wire import build
from homeassistant.components.light import DATA_COMPONENT as LIGHT_DATA_COMPONENT
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.const import (
    DOMAIN,
    UNAVAILABLE_AFTER_FAILURES,
    ZONE_OFF_FRESH_SECONDS,
)

from .conftest import connection, make_device, make_power_only_light
from .const import ADDRESS

PLUG = "switch.test_ceiling"


def _every_sku() -> list[Any]:
    """(device_sku, allow_modelled) for every SKU the library supports, then the opt-in-only ones.

    The opt-in list is the library's own (supported_skus with allow_modelled, minus without),
    never a fixed one: an opted-in device is set up through the flow's confirmation, so it gets
    the same entity checks.
    """
    default = supported_skus()
    opt_in = sorted(set(supported_skus(allow_modelled=True)) - set(default))
    return [
        *(pytest.param(sku, False, id=sku) for sku in default),
        *(pytest.param(sku, True, id=f"{sku}-opted_in") for sku in opt_in),
    ]


# -- helpers --------------------------------------------------------------------------


def _eid(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    """Resolve an entity id from its unique id."""
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, f"no {domain} entity with unique id {unique_id}"
    return entity_id


def _switches(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, er.RegistryEntry]:
    """The entry's registered switch entities keyed by unique id."""
    return {
        e.unique_id: e
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.domain == "switch"
    }


def _lights(hass: HomeAssistant, entry: MockConfigEntry) -> list[er.RegistryEntry]:
    return [
        e
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.domain == "light"
    ]


def _seed(entry: MockConfigEntry, **fields: object) -> None:
    """Mutate the live device state, then publish it like a poll/advert would."""
    state = entry.runtime_data.device.state
    for key, value in fields.items():
        setattr(state, key, value)
    entry.runtime_data.coordinator.async_publish_device_state()


async def _call(hass: HomeAssistant, service: str, entity_id: str) -> None:
    await hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)


def _fail(device: Device, error: BaseException, *, after: int | None = None) -> None:
    """Make the fake transport raise ``error`` (after letting ``after`` frames through)."""
    conn = connection(device)
    conn.error = error
    conn.fail_after = after


def _govee_flows(hass: HomeAssistant) -> list[dict[str, Any]]:
    """In-progress config flows of this integration (ignores the Bluetooth stack's own)."""
    return [f for f in hass.config_entries.flow.async_progress() if f["handler"] == DOMAIN]


# -- 10. smart plug -------------------------------------------------------------------


@pytest.mark.parametrize("device_sku", ["H5083"])
async def test_plug_has_exactly_one_power_switch(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A plug gets one outlet switch named after the device, and no light entities."""
    switches = _switches(hass, entry)
    assert set(switches) == {f"{ADDRESS}_power"}
    assert switches[f"{ADDRESS}_power"].entity_id == PLUG
    assert hass.states.async_all("light") == []

    state = hass.states.get(PLUG)
    assert state.attributes["friendly_name"] == "Test Ceiling"
    assert state.attributes["device_class"] == SwitchDeviceClass.OUTLET
    assert switches[f"{ADDRESS}_power"].original_device_class == SwitchDeviceClass.OUTLET


@pytest.mark.parametrize("device_sku", ["H5083"])
@pytest.mark.parametrize(
    ("is_on", "expected"), [(True, STATE_ON), (False, STATE_OFF), (None, STATE_UNKNOWN)]
)
async def test_plug_state_follows_device(
    hass: HomeAssistant, entry: MockConfigEntry, is_on: bool | None, expected: str
) -> None:
    """The switch shows the relay state read back / advertised by the device."""
    _seed(entry, is_on=is_on)
    assert hass.states.get(PLUG).state == expected


@pytest.mark.parametrize("device_sku", ["H5083"])
async def test_plug_turn_on_and_off(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """turn_on / turn_off drive the relay via set_power and update the shown state."""
    await _call(hass, "turn_on", PLUG)
    mock_device.set_power.assert_awaited_once_with(True)
    assert hass.states.get(PLUG).state == STATE_ON
    assert mock_device.state.is_on is True
    frames = connection(mock_device).sent
    assert frames[0] == build.switch(True, relay=True)
    # The 33 b5 time sync is the library's business: it sends one once per
    # connection (as the app does).
    assert all(frame[:2] == bytes([0x33, 0xB5]) for frame in frames[1:])
    assert len(frames) <= 2

    connection(mock_device).sent.clear()
    mock_device.set_power.reset_mock()
    await _call(hass, "turn_off", PLUG)
    mock_device.set_power.assert_awaited_once_with(False)
    assert hass.states.get(PLUG).state == STATE_OFF
    assert mock_device.state.is_on is False
    assert connection(mock_device).sent[0] == build.switch(False, relay=True)


@pytest.mark.parametrize("device_sku", ["H5083"])
@pytest.mark.parametrize(
    "error",
    [
        GoveeBleConnectionError("no connection"),
        GoveeBleTimeout("no ack"),
        GoveeBleHandshakeError("bad handshake"),
        BleakError("bleak says no"),
        TimeoutError(),
    ],
    ids=lambda e: type(e).__name__,
)
@pytest.mark.parametrize("service", ["turn_on", "turn_off"])
async def test_plug_ble_errors_are_translated(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_device: Device,
    service: str,
    error: BaseException,
) -> None:
    """Transport failures become a translated HomeAssistantError and leave the state alone."""
    # Start from the opposite state, so a failed command flipping it is caught.
    start_on = service == "turn_off"
    _seed(entry, is_on=start_on)
    _fail(mock_device, error)
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, service, PLUG)
    assert not isinstance(exc.value, ServiceValidationError)
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "ble_command_failed"
    assert exc.value.translation_placeholders["device_name"] == "Test Ceiling"
    assert exc.value.translation_placeholders["error"]
    assert hass.states.get(PLUG).state == (STATE_ON if start_on else STATE_OFF)
    assert _govee_flows(hass) == []


@pytest.mark.parametrize("device_sku", ["H5083"])
async def test_plug_time_sync_failure_is_best_effort(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """The relay frame is acked but the time-sync follow-up fails: library 1.2.0 treats
    the sync as best-effort, so the command succeeds and the state reflects the relay."""
    _seed(entry, is_on=False)
    _fail(mock_device, GoveeBleTimeout("no ack"), after=1)
    await _call(hass, "turn_on", PLUG)
    assert connection(mock_device).sent == [build.switch(True, relay=True)]
    assert hass.states.get(PLUG).state == STATE_ON


@pytest.mark.parametrize("device_sku", ["H5083"])
async def test_plug_not_supported_is_a_validation_error(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """GoveeBleNotSupported surfaces as ServiceValidationError."""
    _fail(mock_device, GoveeBleNotSupported("no relay"))
    with pytest.raises(ServiceValidationError) as exc:
        await _call(hass, "turn_on", PLUG)
    assert exc.value.translation_key == "not_supported"
    assert exc.value.translation_placeholders["error"] == "no relay"


@pytest.mark.parametrize("device_sku", ["H5083"])
async def test_plug_auth_error_starts_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, mock_device: Device
) -> None:
    """A rejected secret raises secret_rejected and starts a reauth flow."""
    _fail(mock_device, GoveeBleAuthError("rejected"))
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, "turn_off", PLUG)
    assert exc.value.translation_key == "secret_rejected"
    await hass.async_block_till_done()
    flows = _govee_flows(hass)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    assert flows[0]["context"]["entry_id"] == entry.entry_id


@pytest.mark.parametrize("device_sku", ["H5083"])
async def test_plug_unavailable_when_device_vanishes(
    hass: HomeAssistant, entry: MockConfigEntry, presence: SimpleNamespace
) -> None:
    """The plug goes unavailable once it's gone from every scanner and polls keep failing."""
    _seed(entry, is_on=True)
    presence.connectable = False
    entry.runtime_data.coordinator.consecutive_failures = UNAVAILABLE_AFTER_FAILURES
    entry.runtime_data.coordinator.async_update_listeners()
    assert hass.states.get(PLUG).state == STATE_UNAVAILABLE


@pytest.mark.parametrize("device_sku", ["H60A6", "H6047", "H6006"])
async def test_light_fixtures_have_no_power_switch(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Fixtures with colour-controllable lights (zoned or not) get no switch at all."""
    assert _switches(hass, entry) == {}
    assert hass.states.async_all("switch") == []


@pytest.mark.parametrize(("device_sku", "allow_modelled"), _every_sku())
async def test_every_profile_gets_one_entity_per_relay(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """Across every library profile, opt-in ones too, light.py, switch.py and the cleanup agree.

    Each zone is a light or a switch (never both or neither), the whole device
    gets at most one control (a light or an outlet switch, one if it has no
    zones), and the cleanup of earlier versions' switches only removes the ones
    a light replaced: a zone light, or the whole-fixture light.
    """
    registry = er.async_get(hass)
    mock_config_entry.add_to_hass(hass)
    # The switches earlier versions registered: one per zone, and the power switch.
    zone_switches = [f"{ADDRESS}_zone_{zone.name}" for zone in mock_device.zones]
    for unique_id in (*zone_switches, f"{ADDRESS}_power"):
        registry.async_get_or_create("switch", DOMAIN, unique_id, config_entry=mock_config_entry)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    switches = set(_switches(hass, mock_config_entry))
    lights = {e.unique_id for e in _lights(hass, mock_config_entry)}
    for zone in mock_device.zones:
        is_light = f"{ADDRESS}_zone_{zone.name}_light" in lights
        is_switch = f"{ADDRESS}_zone_{zone.name}" in switches
        assert is_light != is_switch, zone
    whole = (lights | switches) & {f"{ADDRESS}_light", f"{ADDRESS}_power"}
    assert len(whole) <= 1
    if not mock_device.zones:
        assert len(whole) == 1


@pytest.mark.parametrize(
    ("relay", "expected"),
    [(True, ("switch", "power")), (False, ("light", "light"))],
    ids=["relay_plug", "power_only_light"],
)
async def test_power_only_device_is_a_switch_only_as_a_relay_plug(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_bluetooth: SimpleNamespace,
    relay: bool,
    expected: tuple[str, str],
) -> None:
    """Power alone: a relay plug's profile gets the outlet switch, any other one an on/off
    light (govee-ble-local 1.7.0's H1162 family), and never both."""
    device = make_power_only_light()
    device.profile = dataclasses.replace(device.profile, relay=relay)
    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.govee_ble_local.create_device", return_value=device):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    domain, key = expected
    assert {
        (e.domain, e.unique_id)
        for e in er.async_entries_for_config_entry(er.async_get(hass), mock_config_entry.entry_id)
        if e.domain in ("light", "switch")
    } == {(domain, f"{ADDRESS}_{key}")}
    assert [state.entity_id for state in hass.states.async_all(("light", "switch"))] == [
        f"{domain}.test_ceiling"
    ]


# -- 11. zone switches ----------------------------------------------------------------


@pytest.fixture
async def zone_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_bluetooth: SimpleNamespace
) -> MockConfigEntry:
    """An entry around a device whose zones are on/off-only (no shipped SKU has any)."""
    device = make_device("H60A6")
    device.profile = dataclasses.replace(
        device.profile,
        zones=(Zone("main", power_index=0), Zone("mystery", power_index=1)),
    )
    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.govee_ble_local.create_device", return_value=device):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    return mock_config_entry


def _device(entry: MockConfigEntry) -> Device:
    device: Device = entry.runtime_data.device
    return device


async def test_one_switch_per_on_off_zone(hass: HomeAssistant, zone_entry: MockConfigEntry) -> None:
    """Each segment-less zone becomes a switch; no whole-device power switch is added."""
    switches = _switches(hass, zone_entry)
    assert set(switches) == {f"{ADDRESS}_zone_main", f"{ADDRESS}_zone_mystery"}
    # Segment-less zones are not lights either.
    assert not any("_zone_" in e.unique_id for e in _lights(hass, zone_entry))


async def test_zoned_power_only_device_has_no_whole_device_switch(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_bluetooth: SimpleNamespace
) -> None:
    """A power-only device with on/off zones (a multi-outlet plug) is switched per zone only.

    Unlike ``zone_entry`` (a light fixture, which never gets a power switch),
    this is the case the "no zones" condition of the power switch exists for.
    """
    device = make_device("H5083")
    device.profile = dataclasses.replace(
        device.profile, zones=(Zone("a", power_index=0), Zone("b", power_index=1))
    )
    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.govee_ble_local.create_device", return_value=device):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert set(_switches(hass, mock_config_entry)) == {f"{ADDRESS}_zone_a", f"{ADDRESS}_zone_b"}
    assert _lights(hass, mock_config_entry) == []


async def test_zone_switch_names(hass: HomeAssistant, zone_entry: MockConfigEntry) -> None:
    """Known zones use their translated name; an unknown zone is named after itself."""
    main = hass.states.get(_eid(hass, "switch", f"{ADDRESS}_zone_main"))
    mystery = hass.states.get(_eid(hass, "switch", f"{ADDRESS}_zone_mystery"))
    assert main.attributes["friendly_name"] == "Test Ceiling Main light"
    assert mystery.attributes["friendly_name"] == "Test Ceiling mystery"
    assert main.entity_id != mystery.entity_id
    assert mystery.entity_id.endswith("_mystery")


@pytest.mark.parametrize(
    ("zone", "index"), [("main", 0), ("mystery", 1)], ids=["translated", "untranslated"]
)
async def test_zone_switch_turn_on_and_off(
    hass: HomeAssistant, zone_entry: MockConfigEntry, zone: str, index: int
) -> None:
    """turn_on / turn_off drive set_zone_power for that zone and update its state."""
    device = _device(zone_entry)
    entity_id = _eid(hass, "switch", f"{ADDRESS}_zone_{zone}")
    _seed(zone_entry, is_on=True, zone_power={0: False, 1: False})

    await _call(hass, "turn_on", entity_id)
    device.set_zone_power.assert_awaited_once_with(zone, True)
    assert connection(device).sent == [build.zone_power(index, True)]
    assert hass.states.get(entity_id).state == STATE_ON

    device.set_zone_power.reset_mock()
    await _call(hass, "turn_off", entity_id)
    device.set_zone_power.assert_awaited_once_with(zone, False)
    assert connection(device).sent[-1] == build.zone_power(index, False)
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_zone_switch_leaves_sibling_zone_alone(
    hass: HomeAssistant, zone_entry: MockConfigEntry
) -> None:
    """Switching one zone doesn't change the other zone's switch."""
    main = _eid(hass, "switch", f"{ADDRESS}_zone_main")
    mystery = _eid(hass, "switch", f"{ADDRESS}_zone_mystery")
    _seed(zone_entry, is_on=True, zone_power={0: True, 1: True})
    await _call(hass, "turn_off", main)
    assert hass.states.get(main).state == STATE_OFF
    assert hass.states.get(mystery).state == STATE_ON


@pytest.mark.parametrize(
    ("is_on", "zone_power", "expected"),
    [
        (False, {0: True, 1: True}, STATE_OFF),  # fixture off beats a stale zone power
        (True, {0: True, 1: False}, STATE_ON),
        (None, {0: False, 1: True}, STATE_OFF),
        (True, {}, STATE_UNKNOWN),  # zone power not read yet
    ],
)
async def test_zone_switch_state(
    hass: HomeAssistant,
    zone_entry: MockConfigEntry,
    is_on: bool | None,
    zone_power: dict[int, bool],
    expected: str,
) -> None:
    """A zone switch is off while the fixture is off, else it shows the zone's own power."""
    _seed(zone_entry, is_on=is_on, zone_power=zone_power)
    assert hass.states.get(_eid(hass, "switch", f"{ADDRESS}_zone_main")).state == expected


@pytest.mark.parametrize(
    "error",
    [
        GoveeBleConnectionError("no connection"),
        GoveeBleTimeout("no ack"),
        GoveeBleHandshakeError("bad handshake"),
        BleakError("bleak says no"),
        TimeoutError(),
    ],
    ids=lambda e: type(e).__name__,
)
@pytest.mark.parametrize("service", ["turn_on", "turn_off"])
async def test_zone_switch_ble_errors_are_translated(
    hass: HomeAssistant, zone_entry: MockConfigEntry, service: str, error: BaseException
) -> None:
    """Transport failures on a zone switch become a translated error; state is unchanged."""
    entity_id = _eid(hass, "switch", f"{ADDRESS}_zone_mystery")
    # Start from the opposite state, so a failed command flipping it is caught.
    start_on = service == "turn_off"
    _seed(zone_entry, is_on=True, zone_power={0: start_on, 1: start_on})
    _fail(_device(zone_entry), error)
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, service, entity_id)
    assert exc.value.translation_key == "ble_command_failed"
    assert exc.value.translation_placeholders["error"]
    assert hass.states.get(entity_id).state == (STATE_ON if start_on else STATE_OFF)


async def test_zone_switch_auth_error_starts_reauth(
    hass: HomeAssistant, zone_entry: MockConfigEntry
) -> None:
    """A rejected secret on a zone switch starts reauth."""
    _fail(_device(zone_entry), GoveeBleAuthError("rejected"))
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, "turn_on", _eid(hass, "switch", f"{ADDRESS}_zone_main"))
    assert exc.value.translation_key == "secret_rejected"
    await hass.async_block_till_done()
    assert [f["context"]["source"] for f in _govee_flows(hass)] == ["reauth"]


async def test_zone_switch_not_supported(hass: HomeAssistant, zone_entry: MockConfigEntry) -> None:
    """An unsupported zone request is a ServiceValidationError."""
    _fail(_device(zone_entry), GoveeBleNotSupported("unknown zone"))
    with pytest.raises(ServiceValidationError) as exc:
        await _call(hass, "turn_off", _eid(hass, "switch", f"{ADDRESS}_zone_main"))
    assert exc.value.translation_key == "not_supported"


async def test_zone_switches_send_nothing_right_after_the_fixtures_power_off(
    hass: HomeAssistant, zone_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """The fixture's power-off, acked, confirms every zone off: the zone switches turned off
    right after it send nothing more and are no command (no poll hold-off, no device I/O
    noted, no backoff reset); their state is written, and a debug line says why."""
    caplog.set_level(logging.DEBUG, logger="custom_components.govee_ble_local.switch")
    device = _device(zone_entry)
    coordinator = zone_entry.runtime_data.coordinator
    switches = [_eid(hass, "switch", f"{ADDRESS}_zone_{zone}") for zone in ("main", "mystery")]
    _seed(zone_entry, is_on=True, zone_power={0: True, 1: True})
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": "light.test_ceiling"}, blocking=True
    )
    written = {entity_id: hass.states.get(entity_id).last_reported for entity_id in switches}
    with (
        patch.object(coordinator, "async_note_command_started") as started,
        patch.object(coordinator, "async_note_device_io") as device_io,
        patch.object(coordinator, "async_handle_command_success") as success,
    ):
        for entity_id in switches:
            await _call(hass, "turn_off", entity_id)
    started.assert_not_called()
    device_io.assert_not_called()
    success.assert_not_called()
    assert connection(device).sent == [build.switch(False)]
    device.set_zone_power.assert_not_awaited()
    for entity_id in switches:
        assert hass.states.get(entity_id).state == STATE_OFF
        assert hass.states.get(entity_id).last_reported > written[entity_id]
    assert f"Zone mystery of {ADDRESS} was just confirmed off; not sending its power-off" in (
        caplog.text
    )


@pytest.mark.parametrize("case", ["fixture_on", "confirmed_too_long_ago", "unconfirmed"])
async def test_zone_switch_off_goes_out_unless_the_device_just_confirmed_it(
    hass: HomeAssistant, zone_entry: MockConfigEntry, case: str
) -> None:
    """A zone switch's turn_off is spared only while the fixture isn't known on and the
    device confirmed the zone off less than ZONE_OFF_FRESH_SECONDS ago."""
    device = _device(zone_entry)
    _seed(zone_entry, is_on=True, zone_power={0: True, 1: True})
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": "light.test_ceiling"}, blocking=True
    )
    state = device.state
    if case == "fixture_on":
        _seed(zone_entry, is_on=True)
    elif case == "confirmed_too_long_ago":
        state.zone_power_confirmed = dict.fromkeys(
            state.zone_power_confirmed, time.monotonic() - ZONE_OFF_FRESH_SECONDS
        )
    else:
        _seed(zone_entry, zone_power_confirmed={})
    connection(device).sent.clear()
    await _call(hass, "turn_off", _eid(hass, "switch", f"{ADDRESS}_zone_main"))
    device.set_zone_power.assert_awaited_once_with("main", False)
    assert connection(device).sent == [build.zone_power(0, False)]


async def test_zone_switch_off_while_a_fixture_command_runs_is_sent(
    hass: HomeAssistant, zone_entry: MockConfigEntry
) -> None:
    """The switch platform doesn't wait for the light platform's commands: while one to the
    fixture is in flight (held open here; a turn-on that would light every zone, say), a
    zone just confirmed off still gets its zone-off, which then comes after that command."""
    device = _device(zone_entry)
    _seed(zone_entry, is_on=True, zone_power={0: True, 1: True})
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": "light.test_ceiling"}, blocking=True
    )
    fixture = hass.data[LIGHT_DATA_COMPONENT].get_entity("light.test_ceiling")
    connection(device).sent.clear()
    async with fixture._command():
        await _call(hass, "turn_off", _eid(hass, "switch", f"{ADDRESS}_zone_main"))
    device.set_zone_power.assert_awaited_once_with("main", False)
    assert connection(device).sent == [build.zone_power(0, False)]
