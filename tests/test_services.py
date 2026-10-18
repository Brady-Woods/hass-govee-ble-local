"""Tests for the capture_session service action."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import DEFAULT, AsyncMock, patch

from govee_ble_local import Device, GoveeBleAuthError
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_ON
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from probatio import Invalid
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.govee_ble_local import capture
from custom_components.govee_ble_local.capture import SelfTestBusyError
from custom_components.govee_ble_local.const import CONF_SKU, DOMAIN, SERVICE_CAPTURE_SESSION
from custom_components.govee_ble_local.services import async_register_services

from .conftest import make_device
from .const import ADDRESS, PLUG_ADDRESS, PLUG_NAME

MAIN_LIGHT = "light.test_ceiling"
RUNNER = "custom_components.govee_ble_local.services.async_run_self_test"


def _device_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    [device] = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return device.id


async def _capture_session(
    hass: HomeAssistant,
    target: dict[str, Any],
    *,
    context: Context | None = None,
) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_CAPTURE_SESSION,
        {},
        target=target,
        blocking=True,
        context=context,
        return_response=True,
    )
    assert response is not None
    return dict(response)


async def _add_plug_entry(
    hass: HomeAssistant, bluetooth: SimpleNamespace
) -> tuple[MockConfigEntry, Device]:
    """Set up a second entry (an H5083 plug) next to the loaded H60A6."""
    plug = make_device("H5083", address=PLUG_ADDRESS, name=PLUG_NAME)
    plug.state.is_on = True
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test Plug",
        unique_id=PLUG_ADDRESS,
        data={"address": PLUG_ADDRESS, CONF_SKU: "H5083"},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.govee_ble_local.create_device", return_value=plug):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry, plug


async def test_capture_session_by_device_returns_and_stores_report(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """Targeting a device runs the self-test, returns the report and stores it."""
    mock_device.state.is_on = True

    response = await _capture_session(hass, {"device_id": _device_id(hass, setup_integration)})

    assert response == {"results": [setup_integration.runtime_data.last_self_test]}
    report = response["results"][0]
    assert report["address"] == ADDRESS
    assert report["sku"] == "H60A6"
    assert report["ok"] is True
    assert report["restored"] is True
    mock_device.set_rgb.assert_awaited()


async def test_capture_session_by_entity_and_state_refresh(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """An entity target resolves to its device; entity states are refreshed afterwards."""
    mock_device.state.is_on = True
    coordinator = setup_integration.runtime_data.coordinator

    with patch.object(
        coordinator,
        "async_publish_device_state",
        wraps=coordinator.async_publish_device_state,
    ) as publish:
        response = await _capture_session(hass, {"entity_id": MAIN_LIGHT})

    assert response["results"][0]["address"] == ADDRESS
    publish.assert_called_once_with()
    assert hass.states.get(MAIN_LIGHT).state == STATE_ON


async def test_capture_session_runs_every_targeted_device(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """Several targeted devices each get their own report, stored on their own entry."""
    plug_entry, _plug = await _add_plug_entry(hass, mock_bluetooth)

    response = await _capture_session(
        hass,
        {"device_id": [_device_id(hass, setup_integration), _device_id(hass, plug_entry)]},
    )

    reports = {r["address"]: r for r in response["results"]}
    assert set(reports) == {ADDRESS, PLUG_ADDRESS}
    assert reports[PLUG_ADDRESS]["sku"] == "H5083"
    assert [s["step"] for s in reports[PLUG_ADDRESS]["steps"]] == ["power_on", "power_off"]
    assert setup_integration.runtime_data.last_self_test == reports[ADDRESS]
    assert plug_entry.runtime_data.last_self_test == reports[PLUG_ADDRESS]


async def test_capture_session_requires_admin(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: Device,
    hass_read_only_user: MockUser,
    hass_admin_user: MockUser,
) -> None:
    """Only administrators may run it; a read-only user is refused before anything is sent."""
    with pytest.raises(Unauthorized):
        await _capture_session(
            hass,
            {"device_id": _device_id(hass, setup_integration)},
            context=Context(user_id=hass_read_only_user.id),
        )
    mock_device.set_power.assert_not_awaited()
    assert setup_integration.runtime_data.last_self_test is None

    response = await _capture_session(
        hass,
        {"device_id": _device_id(hass, setup_integration)},
        context=Context(user_id=hass_admin_user.id),
    )
    assert response["results"][0]["address"] == ADDRESS


async def test_entity_of_another_domain_is_no_match(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Targeting an entity that doesn't belong to this integration raises a clear error."""
    other = er.async_get(hass).async_get_or_create("sensor", "other_integration", "unique")

    with pytest.raises(ServiceValidationError) as exc_info:
        await _capture_session(hass, {"entity_id": other.entity_id})

    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == "no_matching_device"


async def test_empty_area_is_no_match(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """An area with no Govee devices matches nothing."""
    area = ar.async_get(hass).async_create("Empty room")

    with pytest.raises(ServiceValidationError) as exc_info:
        await _capture_session(hass, {"area_id": area.id})

    assert exc_info.value.translation_key == "no_matching_device"


async def test_area_target_resolves_devices_in_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """An area target runs the devices placed in that area."""
    area = ar.async_get(hass).async_create("Living room")
    dr.async_get(hass).async_update_device(_device_id(hass, setup_integration), area_id=area.id)

    response = await _capture_session(hass, {"area_id": area.id})

    assert [r["address"] for r in response["results"]] == [ADDRESS]


async def test_call_without_a_target_is_rejected_by_the_schema(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """The service is target-only: a call with no target never reaches the device."""
    with pytest.raises(Invalid):
        await _capture_session(hass, {})

    mock_device.set_power.assert_not_awaited()


async def test_other_integrations_entries_in_the_target_are_ignored(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """Entries of other integrations (and vanished ones) pulled in by a target are skipped."""
    other = MockConfigEntry(domain="other_integration")
    other.add_to_hass(hass)

    with patch(
        "custom_components.govee_ble_local.services.async_extract_config_entry_ids",
        new_callable=AsyncMock,
        return_value={other.entry_id, "does-not-exist", setup_integration.entry_id},
    ):
        response = await _capture_session(hass, {"device_id": _device_id(hass, setup_integration)})

    assert [r["address"] for r in response["results"]] == [ADDRESS]
    mock_device.set_power.assert_awaited()


async def test_unloaded_entry_reports_not_loaded(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """An entry that isn't loaded yields an error entry instead of aborting."""
    device_id = _device_id(hass, setup_integration)
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    mock_device.set_power.reset_mock()

    response = await _capture_session(hass, {"device_id": device_id})

    assert response == {
        "results": [
            {"ok": False, "address": ADDRESS, "reason": "not_loaded", "error": "not loaded"}
        ]
    }
    mock_device.set_power.assert_not_awaited()


async def test_busy_and_failed_devices_do_not_abort_the_others(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_bluetooth: SimpleNamespace,
) -> None:
    """A device that can't be tested contributes an error entry; the rest still report."""
    plug_entry, plug = await _add_plug_entry(hass, mock_bluetooth)

    async def _runner(device: Device) -> dict[str, Any]:
        if device is plug:
            raise SelfTestBusyError
        return await capture.async_run_self_test(device)

    with patch(RUNNER, side_effect=_runner):
        response = await _capture_session(
            hass,
            {"device_id": [_device_id(hass, setup_integration), _device_id(hass, plug_entry)]},
        )

    by_address = {r["address"]: r for r in response["results"]}
    assert by_address[ADDRESS]["ok"] is True
    busy = by_address[PLUG_ADDRESS]
    assert busy["ok"] is False
    assert busy["reason"] == "self_test_busy"
    assert "already running" in busy["error"]
    assert "steps" not in busy
    assert "reason" not in by_address[ADDRESS]


async def test_busy_when_another_self_test_is_running(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """With the (real) self-test lock held the device reports busy and keeps no report."""
    async with capture._SELF_TEST_LOCK:
        response = await _capture_session(hass, {"device_id": _device_id(hass, setup_integration)})

    [result] = response["results"]
    assert result["ok"] is False
    assert result["address"] == ADDRESS
    assert result["reason"] == "self_test_busy"
    assert "already running" in result["error"]
    assert setup_integration.runtime_data.last_self_test is None


async def test_device_not_seen_reports_an_error_without_running(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: Device,
    presence: SimpleNamespace,
) -> None:
    """A device no connectable scanner hears gets an error entry; nothing is sent."""
    presence.connectable = False

    response = await _capture_session(hass, {"device_id": _device_id(hass, setup_integration)})

    assert response == {
        "results": [
            {
                "ok": False,
                "address": ADDRESS,
                "reason": "self_test_device_not_found",
                "error": "Cannot run the self-test: no Bluetooth scanner that can connect to "
                # HA drops a translated message's final full stop.
                "Test Ceiling currently sees it. Make sure it is powered on and in range",
            }
        ]
    }
    mock_device.set_power.assert_not_awaited()
    assert setup_integration.runtime_data.last_self_test is None


async def test_refused_second_run_keeps_the_running_one_tracked(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_device: Device
) -> None:
    """A second request while a self-test runs is busy and doesn't replace the tracked run."""
    mock_device.state.is_on = True
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_rgb(*_args: Any) -> Any:
        started.set()
        await release.wait()
        return DEFAULT

    mock_device.set_rgb.side_effect = _slow_rgb  # type: ignore[attr-defined]
    target = {"device_id": _device_id(hass, setup_integration)}
    first = hass.async_create_task(_capture_session(hass, target))
    await started.wait()
    running = setup_integration.runtime_data.self_test
    assert running is not None
    assert not running.done()

    [busy] = (await _capture_session(hass, target))["results"]
    assert busy["reason"] == "self_test_busy"
    assert "already running" in busy["error"]
    assert setup_integration.runtime_data.self_test is running

    release.set()
    [report] = (await first)["results"]
    assert report["ok"] is True
    assert running.done()
    assert setup_integration.runtime_data.last_self_test == report


async def test_auth_error_reports_and_starts_reauth(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_device: Device,
) -> None:
    """A rejected secret becomes an error entry and starts the reauth flow."""
    mock_device.set_brightness.side_effect = GoveeBleAuthError("rejected")

    response = await _capture_session(hass, {"device_id": _device_id(hass, setup_integration)})
    await hass.async_block_till_done()

    [result] = response["results"]
    assert result["ok"] is False
    assert result["address"] == ADDRESS
    assert result["reason"] == "secret_rejected"
    assert "rejected its secret key" in result["error"]
    assert "Test Ceiling" in result["error"]
    assert setup_integration.runtime_data.last_self_test is None
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [(f["context"]["source"], f["context"]["entry_id"]) for f in flows] == [
        (SOURCE_REAUTH, setup_integration.entry_id)
    ]


async def test_untranslated_error_gets_the_fallback_reason(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """An error without a translation key still gets a machine-readable reason."""
    with patch(RUNNER, side_effect=HomeAssistantError("something broke")):
        response = await _capture_session(hass, {"device_id": _device_id(hass, setup_integration)})

    assert response == {
        "results": [
            {"ok": False, "address": ADDRESS, "reason": "unknown", "error": "something broke"}
        ]
    }


async def test_another_domains_translated_error_gets_the_fallback_reason(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Only this integration's translation keys become a reason (the README lists them)."""
    foreign = HomeAssistantError(
        translation_domain="homeassistant",
        translation_key="service_not_found",
        translation_placeholders={"domain": "light", "service": "nope"},
    )
    with patch(RUNNER, side_effect=foreign):
        response = await _capture_session(hass, {"device_id": _device_id(hass, setup_integration)})

    [result] = response["results"]
    assert result["ok"] is False
    assert result["address"] == ADDRESS
    assert result["reason"] == "unknown"


def test_register_services_is_idempotent(hass: HomeAssistant) -> None:
    """Registering twice keeps the single admin-only, response-only service."""
    async_register_services(hass)
    first: Callable[..., Any] = hass.services.async_services_for_domain(DOMAIN)[
        SERVICE_CAPTURE_SESSION
    ].job.target
    async_register_services(hass)

    assert hass.services.has_service(DOMAIN, SERVICE_CAPTURE_SESSION)
    assert (
        hass.services.async_services_for_domain(DOMAIN)[SERVICE_CAPTURE_SESSION].job.target is first
    )
    assert hass.services.supports_response(DOMAIN, SERVICE_CAPTURE_SESSION).value == "only"
