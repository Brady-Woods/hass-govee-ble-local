"""Button platform: run the device self-test / session capture from HA."""
from __future__ import annotations

import logging

from govee_ble_local import Device
from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GoveeBleLocalConfigEntry
from .capture import async_run_self_test
from .coordinator import GoveeBleLocalCoordinator
from .entity import GoveeBleLocalEntity

_LOGGER = logging.getLogger(__name__)

# Shares the device's single BLE connection with the other command platforms.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeBleLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the per-device self-test button."""
    async_add_entities(
        [
            GoveeBleLocalSelfTestButton(
                entry.runtime_data.coordinator,
                entry.runtime_data.device,
                entry.data["address"],
                entry.title,
            )
        ]
    )


class GoveeBleLocalSelfTestButton(GoveeBleLocalEntity, ButtonEntity):
    """Runs the full-capability self-test and stores the captured session.

    The capture is surfaced in the config-entry diagnostics; the same run is also
    available (with the report returned inline) via the ``capture_session`` service.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "self_test"

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        device: Device,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator, address, device_name, device.sku)
        self._device = device
        self._attr_unique_id = f"{address}_self_test"

    async def async_press(self) -> None:
        report = await self._run_client_command(async_run_self_test(self._device))
        self.coordinator.last_self_test = report
        passed = sum(1 for s in report["steps"] if s["ok"])
        _LOGGER.info(
            "%s self-test: %s (%d/%d steps ok, %d frames captured)",
            self._address,
            "PASS" if report["ok"] else "FAIL",
            passed,
            len(report["steps"]),
            len(report["frames"]),
        )
