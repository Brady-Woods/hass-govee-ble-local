"""Tests for the Govee BLE Local switch (per-zone, and whole-device power)
platform."""
from __future__ import annotations

from unittest.mock import AsyncMock

from govee_ble_local import Capability, Zone
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local import GoveeBleLocalRuntimeData
from custom_components.govee_ble_local.capture import LogCapture
from custom_components.govee_ble_local.const import DOMAIN
from custom_components.govee_ble_local.coordinator import GoveeBleLocalCoordinator
from custom_components.govee_ble_local.switch import (
    GoveeBleLocalPowerSwitch,
    GoveeBleLocalZoneSwitch,
    async_setup_entry,
)

from .conftest import make_device
from .const import ADDRESS, TITLE


def _entry_with(hass: HomeAssistant, device: AsyncMock) -> MockConfigEntry:
    coordinator = GoveeBleLocalCoordinator(hass, device, ADDRESS)
    coordinator.last_update_success = True
    entry = MockConfigEntry(domain=DOMAIN, title=TITLE, data={"address": ADDRESS})
    entry.runtime_data = GoveeBleLocalRuntimeData(
        device=device, coordinator=coordinator, log_capture=LogCapture(ADDRESS)
    )
    return entry


async def test_onoff_zone_is_switch_colour_zone_is_not(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """An on/off-only zone (no segments) is a switch; a colour-controllable zone
    (has segments) is a light instead, so it has no switch entity."""
    registry = er.async_get(hass)
    # H60A6 test fixture: `background` has no segments -> switch.
    assert (
        registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_zone_background")
        is not None
    )
    # `main` has segments -> it's a zone light, not a switch.
    assert registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_zone_main") is None


async def test_zone_switch_turn_on_off(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: AsyncMock
) -> None:
    """Toggling a zone switch drives set_zone_power with the zone name/state."""
    registry = er.async_get(hass)
    bg = registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_zone_background")
    assert bg is not None

    await hass.services.async_call("switch", "turn_on", {"entity_id": bg}, blocking=True)
    mock_device.set_zone_power.assert_awaited_with("background", True)
    assert hass.states.get(bg).state == "on"

    await hass.services.async_call("switch", "turn_off", {"entity_id": bg}, blocking=True)
    mock_device.set_zone_power.assert_awaited_with("background", False)
    assert hass.states.get(bg).state == "off"


async def test_zoned_device_gets_no_power_switch(hass: HomeAssistant) -> None:
    """A device with zones is on/off-controlled via those, not an additional
    whole-device power switch."""
    device = make_device()  # H60A6: has zones
    entry = _entry_with(hass, device)
    added: list[object] = []
    await async_setup_entry(hass, entry, added.extend)
    assert all(isinstance(e, GoveeBleLocalZoneSwitch) for e in added)
    assert not any(isinstance(e, GoveeBleLocalPowerSwitch) for e in added)
    await entry.runtime_data.coordinator.async_shutdown()


async def test_h6047_bars_are_lights_not_switches(hass: HomeAssistant) -> None:
    """The H6047 bars have segments, so they're colour zone-lights (light.py),
    not switches - the switch platform creates nothing for it."""
    device = make_device(
        zones=(
            Zone("left", power_index=0, segments=tuple(range(0, 5))),
            Zone("right", power_index=1, segments=tuple(range(5, 10))),
        ),
        sku="H6047",
    )
    entry = _entry_with(hass, device)
    added: list[object] = []
    await async_setup_entry(hass, entry, added.extend)
    assert added == []
    await entry.runtime_data.coordinator.async_shutdown()


async def test_power_switch_created_for_plug(hass: HomeAssistant) -> None:
    """A device with no zones and no light capability (e.g. a plug) gets a
    single whole-device power switch whose state is read back from the device
    (the plug profile polls its relay) and nudged optimistically on command."""
    device = make_device(
        capabilities=frozenset({Capability.POWER}), zones=(), sku="H5083"
    )
    # The real plug polls its relay state into DeviceState; the switch reads
    # coordinator.data. Model set_power mutating that shared state, as the
    # library's Device.set_power does.
    state = device.state
    device.set_power = AsyncMock(side_effect=lambda on: setattr(state, "is_on", on))

    entry = _entry_with(hass, device)
    entry.runtime_data.coordinator.data = state
    added: list[object] = []
    await async_setup_entry(hass, entry, added.extend)
    assert len(added) == 1
    switch = added[0]
    assert isinstance(switch, GoveeBleLocalPowerSwitch)
    switch.hass = hass
    switch.entity_id = "switch.test_plug"

    assert switch.is_on is None  # unknown until first poll / command

    # A poll that reads the relay ON is reflected without any command.
    state.is_on = True
    assert switch.is_on is True

    await switch.async_turn_off()
    device.set_power.assert_awaited_with(False)
    assert switch.is_on is False

    await switch.async_turn_on()
    device.set_power.assert_awaited_with(True)
    assert switch.is_on is True
    await entry.runtime_data.coordinator.async_shutdown()
