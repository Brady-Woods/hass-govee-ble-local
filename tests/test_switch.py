"""Tests for the Govee BLE Local switch (per-zone) platform."""
from __future__ import annotations

from unittest.mock import AsyncMock

from govee_ble_local.profile import Capabilities, DeviceProfile
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_h60a6 import GoveeH60A6RuntimeData
from custom_components.govee_h60a6.const import DOMAIN
from custom_components.govee_h60a6.coordinator import GoveeH60A6Coordinator
from custom_components.govee_h60a6.switch import async_setup_entry

from .const import ADDRESS, TITLE, make_status


async def test_switch_states(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Each zone switch reflects the polled zone state."""
    registry = er.async_get(hass)
    upper = registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_zone_1")
    lower = registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_zone_0")
    assert upper is not None and lower is not None
    assert hass.states.get(upper).state == "on"  # zone_upper_on
    assert hass.states.get(lower).state == "off"  # zone_lower_on


async def test_switch_turn_on_off(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """Toggling a zone switch drives set_zone with the right index/state."""
    registry = er.async_get(hass)
    upper = registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_zone_1")

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": upper}, blocking=True
    )
    mock_client.set_zone.assert_awaited_with(1, True)

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": upper}, blocking=True
    )
    mock_client.set_zone.assert_awaited_with(1, False)


async def test_switch_setup_skips_unmapped_zone(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A profile zone with no ZONE_META mapping is skipped, not fatal."""
    profile = DeviceProfile(
        sku="Z", name="Z", local_name_prefixes=("Z",),
        capabilities=Capabilities(zones=("upper", "bogus")),
    )
    coordinator = GoveeH60A6Coordinator(hass, mock_client, ADDRESS)
    coordinator.data = make_status()
    entry = MockConfigEntry(domain=DOMAIN, title=TITLE, data={"address": ADDRESS})
    entry.runtime_data = GoveeH60A6RuntimeData(
        client=mock_client, coordinator=coordinator, profile=profile, serial_number="SN"
    )

    added: list[object] = []
    await async_setup_entry(hass, entry, added.extend)
    assert len(added) == 1  # only "upper" mapped; "bogus" skipped
