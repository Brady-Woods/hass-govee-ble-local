"""Tests for the Govee BLE Local switch (per-zone, and whole-device power)
platform."""
from __future__ import annotations

from unittest.mock import AsyncMock

from govee_ble_local import Capability
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local import GoveeBleLocalRuntimeData
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
    entry.runtime_data = GoveeBleLocalRuntimeData(device=device, coordinator=coordinator)
    return entry


async def test_zone_switches_created(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The H60A6 gets one switch entity per named zone."""
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_zone_main") is not None
    assert (
        registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_zone_background")
        is not None
    )


async def test_zone_switch_turn_on_off(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: AsyncMock
) -> None:
    """Toggling a zone switch drives set_zone_power with the zone name/state."""
    registry = er.async_get(hass)
    main = registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_zone_main")
    assert main is not None

    await hass.services.async_call("switch", "turn_on", {"entity_id": main}, blocking=True)
    mock_device.set_zone_power.assert_awaited_with("main", True)
    assert hass.states.get(main).state == "on"

    await hass.services.async_call("switch", "turn_off", {"entity_id": main}, blocking=True)
    mock_device.set_zone_power.assert_awaited_with("main", False)
    assert hass.states.get(main).state == "off"


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


async def test_power_switch_created_for_plug(hass: HomeAssistant) -> None:
    """A device with no zones and no light capability (e.g. a plug) gets a
    single whole-device power switch, tracked optimistically."""
    device = make_device(
        capabilities=frozenset({Capability.POWER}), zones=(), sku="H5083"
    )
    entry = _entry_with(hass, device)
    added: list[object] = []
    await async_setup_entry(hass, entry, added.extend)
    assert len(added) == 1
    switch = added[0]
    assert isinstance(switch, GoveeBleLocalPowerSwitch)
    switch.hass = hass
    switch.entity_id = "switch.test_plug"

    assert switch.is_on is None  # optimistic only

    await switch.async_turn_on()
    device.set_power.assert_awaited_with(True)
    assert switch.is_on is True

    await switch.async_turn_off()
    device.set_power.assert_awaited_with(False)
    assert switch.is_on is False
    await entry.runtime_data.coordinator.async_shutdown()
