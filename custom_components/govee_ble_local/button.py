"""Button platform: run the device self-test / session capture from HA."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import GoveeBleLocalEntity
from .services import async_self_test_entry

if TYPE_CHECKING:
    from . import GoveeBleLocalConfigEntry

_LOGGER = logging.getLogger(__name__)

# Shares the device's single BLE connection with the other command platforms.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeBleLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the per-device self-test button."""
    async_add_entities([GoveeBleLocalSelfTestButton(entry)])


class GoveeBleLocalSelfTestButton(GoveeBleLocalEntity, ButtonEntity):
    """Runs the full-capability self-test and stores the captured session.

    The capture is surfaced in the config-entry diagnostics; the same run is also
    available (with the report returned inline) via the ``capture_session`` service.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "self_test"

    def __init__(self, entry: GoveeBleLocalConfigEntry) -> None:
        """Initialize the self-test button."""
        super().__init__(entry, "self_test")
        self._entry = entry

    async def async_press(self) -> None:
        """Run the self-test and log a one-line summary."""
        report = await async_self_test_entry(self.hass, self._entry)
        passed = sum(1 for s in report["steps"] if s["ok"])
        _LOGGER.info(
            "%s self-test: %s (%d/%d steps ok, %d frames captured%s)",
            self._address,
            "PASS" if report["ok"] else "FAIL",
            passed,
            len(report["steps"]),
            len(report["frames"]),
            "" if report["restored"] else ", state NOT fully restored",
        )
