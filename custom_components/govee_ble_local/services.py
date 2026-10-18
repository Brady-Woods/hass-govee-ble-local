"""Service actions and the shared self-test runner for Govee BLE Local."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict, cast

from govee_ble_local import GoveeBleAuthError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import (
    async_extract_config_entry_ids,
    async_register_admin_service,
)
from homeassistant.util.json import JsonObjectType

from .capture import SelfTestBusyError, SelfTestReport, async_run_self_test
from .const import DOMAIN, SERVICE_CAPTURE_SESSION
from .helpers import async_secret_rejected

if TYPE_CHECKING:
    from . import GoveeBleLocalConfigEntry

# Machine-readable ``reason`` of a capture_session entry for a device that couldn't be
# tested: an unloaded entry, else the translation key of this integration's error that
# stopped it (self_test_device_not_found, self_test_busy, secret_rejected). Any other
# error, including another domain's translated one, maps to unknown.
_REASON_NOT_LOADED = "not_loaded"
_REASON_UNKNOWN = "unknown"


class _CaptureFailure(TypedDict):
    """The capture_session entry for a device that couldn't be tested."""

    ok: Literal[False]
    address: str
    reason: str  # stable code to branch on
    error: str  # human-readable message (English)


type _CaptureResult = SelfTestReport | _CaptureFailure


async def async_self_test_entry(
    hass: HomeAssistant, entry: GoveeBleLocalConfigEntry
) -> SelfTestReport:
    """Run the self-test against one entry's device and record the report.

    Raises HomeAssistantError (translated) when no connectable scanner sees the
    device, another self-test is running, or the device rejects its secret
    (which also starts reauth). Once a run has started the entities are
    refreshed afterwards either way: the test drives the device through many
    states and the restore may not have fully succeeded.
    """
    data = entry.runtime_data
    if not data.coordinator.async_address_present():
        # Like a skipped poll: every step would only burn connect retries until
        # it timed out, holding the (integration-wide) self-test lock meanwhile.
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="self_test_device_not_found",
            translation_placeholders={"device_name": entry.title},
        )
    task = entry.async_create_task(hass, async_run_self_test(data.device), "self_test")
    if data.self_test is None or data.self_test.done():
        # Tracked so an unload waits for the restore. A run already going is
        # kept: this one is then refused as busy.
        data.self_test = task
    try:
        report = await task
    except SelfTestBusyError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="self_test_busy"
        ) from err
    except GoveeBleAuthError as err:
        raise async_secret_rejected(hass, entry) from err
    finally:
        data.coordinator.async_publish_device_state()
    data.last_self_test = report
    return report


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the integration-wide services once."""
    if hass.services.has_service(DOMAIN, SERVICE_CAPTURE_SESSION):
        return

    async def _async_capture_session(call: ServiceCall) -> ServiceResponse:
        """Run the self-test against each targeted device; return the reports.

        A device that can't be tested contributes an ``{ok: False, address,
        reason, error}`` entry instead of aborting the reports already collected.
        """
        results: list[_CaptureResult] = []
        for entry_id in await async_extract_config_entry_ids(call):
            target = hass.config_entries.async_get_entry(entry_id)
            if target is None or target.domain != DOMAIN:
                continue  # a device target can pull in other integrations' entries
            address = target.data[CONF_ADDRESS]
            if target.state is not ConfigEntryState.LOADED:
                results.append(
                    {
                        "ok": False,
                        "address": address,
                        "reason": _REASON_NOT_LOADED,
                        "error": "not loaded",
                    }
                )
                continue
            try:
                results.append(await async_self_test_entry(hass, target))
            except HomeAssistantError as err:
                reason = _REASON_UNKNOWN
                if err.translation_domain == DOMAIN and err.translation_key:
                    reason = err.translation_key
                results.append(
                    {
                        "ok": False,
                        "address": address,
                        "reason": reason,
                        "error": str(err),
                    }
                )
        if not results:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_matching_device"
            )
        # Reports are plain JSON data (str/int/bool/None, lists and dicts).
        return cast(JsonObjectType, {"results": results})

    # Admin-only: the self-test switches lights/plug relays and returns raw frames.
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_CAPTURE_SESSION,
        _async_capture_session,
        # Target-only service (device/entity/area).
        schema=cv.make_entity_service_schema({}),
        supports_response=SupportsResponse.ONLY,
    )
