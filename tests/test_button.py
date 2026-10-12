"""Tests for the self-test button."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import DEFAULT

from govee_ble_local import Device, GoveeBleAuthError, GoveeBleConnectionError
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local import capture
from custom_components.govee_ble_local.const import DOMAIN

from .const import ADDRESS

UID = f"{ADDRESS}_self_test"
BUTTON_LOGGER = "custom_components.govee_ble_local.button"


def _button_id(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, UID)
    assert entity_id is not None
    return entity_id


async def _press(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": _button_id(hass)}, blocking=True
    )


def _summaries(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == BUTTON_LOGGER and r.levelno == logging.INFO
    ]


async def test_button_is_a_diagnostic_entity(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The self-test button is registered as a diagnostic entity of the device."""
    entry = er.async_get(hass).async_get(_button_id(hass))
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert entry.translation_key == "self_test"
    assert entry.original_name == "Run self-test"


async def test_press_runs_self_test_stores_report_and_logs_summary(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: Device,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pressing the button exercises the device, stores the report and logs an INFO summary."""
    mock_device.state.is_on = True
    mock_device.set_power.side_effect = lambda on: (
        logging.getLogger("govee_ble_local.frames").debug(
            "%s tx power plain=3301 wire=3301 enc=none", ADDRESS
        )
        or DEFAULT
    )

    with caplog.at_level(logging.INFO, logger=BUTTON_LOGGER):
        await _press(hass)

    report = setup_integration.runtime_data.coordinator.last_self_test
    assert report is not None
    assert report["sku"] == "H60A6"
    assert report["address"] == ADDRESS
    assert report["ok"] is True
    mock_device.set_rgb.assert_awaited()
    mock_device.set_scene_by_name.assert_awaited()
    total = len(report["steps"])
    assert len(report["frames"]) == 3  # power_on, power_off and the restore
    assert _summaries(caplog) == [
        f"{ADDRESS} self-test: PASS ({total}/{total} steps ok, 3 frames captured)"
    ]


async def test_press_logs_fail_when_a_step_fails(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: Device,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed step is reflected in the summary (and the run itself still completes)."""
    mock_device.state.is_on = True
    mock_device.set_rgb.side_effect = GoveeBleConnectionError("no slot")

    with caplog.at_level(logging.INFO, logger=BUTTON_LOGGER):
        await _press(hass)

    report = setup_integration.runtime_data.coordinator.last_self_test
    assert report is not None
    total = len(report["steps"])
    assert _summaries(caplog) == [
        f"{ADDRESS} self-test: FAIL ({total - 1}/{total} steps ok, 0 frames captured)"
    ]


async def test_press_warns_when_state_was_not_fully_restored(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: Device,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the restore fails the summary says the device state was NOT fully restored."""
    mock_device.state.is_on = True
    calls = 0

    def _fail_restore(on: bool) -> Any:
        nonlocal calls
        calls += 1
        if calls == 3:  # power_on, power_off, then the restore of the original state
            raise GoveeBleConnectionError("link lost")
        return DEFAULT

    mock_device.set_power.side_effect = _fail_restore

    with caplog.at_level(logging.INFO, logger=BUTTON_LOGGER):
        await _press(hass)

    report = setup_integration.runtime_data.coordinator.last_self_test
    assert report is not None
    assert report["restored"] is False
    [summary] = _summaries(caplog)
    assert summary.startswith(f"{ADDRESS} self-test: PASS (")
    assert summary.endswith(", state NOT fully restored)")


async def test_press_while_another_self_test_runs_is_a_busy_error(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """A second self-test is refused with a translated error and sends nothing."""
    async with capture._SELF_TEST_LOCK:
        with pytest.raises(HomeAssistantError) as exc_info:
            await _press(hass)

    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == "self_test_busy"
    mock_device.set_power.assert_not_awaited()
    assert setup_integration.runtime_data.coordinator.last_self_test is None


async def test_press_with_rejected_secret_starts_reauth(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """A rejected secret raises a translated error and starts the reauth flow."""
    mock_device.set_brightness.side_effect = GoveeBleAuthError("rejected")

    with pytest.raises(HomeAssistantError) as exc_info:
        await _press(hass)
    await hass.async_block_till_done()

    assert exc_info.value.translation_key == "secret_rejected"
    assert exc_info.value.translation_placeholders == {"device_name": "Test Ceiling"}
    assert setup_integration.runtime_data.coordinator.last_self_test is None
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [(f["context"]["source"], f["context"]["entry_id"]) for f in flows] == [
        (SOURCE_REAUTH, setup_integration.entry_id)
    ]
