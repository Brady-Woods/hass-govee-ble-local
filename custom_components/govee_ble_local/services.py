"""Service actions and the shared self-test runner for Govee BLE Local."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

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

from .capture import SelfTestBusyError, async_run_self_test
from .const import DOMAIN, SERVICE_CAPTURE_SESSION

if TYPE_CHECKING:
    from . import GoveeBleLocalConfigEntry


async def async_self_test_entry(
    hass: HomeAssistant, entry: GoveeBleLocalConfigEntry
) -> dict[str, Any]:
    """Run the self-test against one entry's device and record the report.

    Raises HomeAssistantError (translated) when another self-test is running or
    the device rejects its secret (which also starts reauth). The entities are
    refreshed afterwards either way: the test drives the device through many
    states and the restore may not have fully succeeded.
    """
    data = entry.runtime_data
    try:
        report = await async_run_self_test(data.device)
    except SelfTestBusyError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="self_test_busy"
        ) from err
    except GoveeBleAuthError as err:
        entry.async_start_reauth(hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="secret_rejected",
            translation_placeholders={"device_name": entry.title},
        ) from err
    finally:
        data.coordinator.async_publish_device_state()
    data.coordinator.last_self_test = report
    return report


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the integration-wide services once."""
    if hass.services.has_service(DOMAIN, SERVICE_CAPTURE_SESSION):
        return

    async def _async_capture_session(call: ServiceCall) -> ServiceResponse:
        """Run the self-test against each targeted device; return the reports.

        A device that can't be tested contributes an ``{ok: False, error}``
        entry instead of aborting the reports already collected.
        """
        results: list[dict[str, Any]] = []
        for entry_id in await async_extract_config_entry_ids(call):
            target = hass.config_entries.async_get_entry(entry_id)
            if target is None or target.domain != DOMAIN:
                continue  # a device target can pull in other integrations' entries
            address = target.data[CONF_ADDRESS]
            if target.state is not ConfigEntryState.LOADED:
                results.append({"ok": False, "address": address, "error": "not loaded"})
                continue
            try:
                results.append(await async_self_test_entry(hass, target))
            except HomeAssistantError as err:
                results.append({"ok": False, "address": address, "error": str(err)})
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
