"""Shared base entity for the Govee BLE Local integration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
import logging
from typing import TYPE_CHECKING, Any

from govee_ble_local import GoveeBleAuthError, GoveeBleNotSupported
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import COMMAND_TIMEOUT_SECONDS, DOMAIN, ZONE_TRANSLATION_KEYS
from .coordinator import GoveeBleLocalCoordinator
from .helpers import BLE_ERRORS, async_secret_rejected

if TYPE_CHECKING:
    from . import GoveeBleLocalConfigEntry

_LOGGER = logging.getLogger(__name__)


class GoveeBleLocalEntity(CoordinatorEntity[GoveeBleLocalCoordinator]):
    """Base entity providing shared identity, device info and BLE-error handling."""

    _attr_has_entity_name = True

    def __init__(self, entry: GoveeBleLocalConfigEntry, key: str) -> None:
        """Initialize the entity; ``key`` makes the unique id ``{address}_{key}``."""
        super().__init__(entry.runtime_data.coordinator)
        self._device = entry.runtime_data.device
        self._address: str = entry.data[CONF_ADDRESS]
        self._device_name = entry.title
        self._attr_unique_id = f"{self._address}_{key}"
        # Only link to the device: __init__._async_track_device_registry creates
        # its registry record before the platforms load and keeps it current (a
        # second copy of the fields here would be re-applied on every entity add).
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, self._address)})

    @property
    def available(self) -> bool:
        """Reachability, not last-poll success (see coordinator.device_available)."""
        return self.coordinator.device_available

    def _set_zone_name(self, zone_name: str) -> None:
        """Name a per-zone entity: translated for known zones, else the raw zone name."""
        if (key := ZONE_TRANSLATION_KEYS.get(zone_name)) is not None:
            self._attr_translation_key = key
        else:
            self._attr_name = zone_name

    @asynccontextmanager
    async def _command(self) -> AsyncIterator[None]:
        """Wrap a command's device calls; then push state to EVERY entity of this device.

        A command often changes state other entities show (whole-fixture off
        turns the zone lights/switches off too), so writing only this entity's
        state would leave its siblings stale until the next poll. It is pushed
        after a failure part-way too: the library already applied what landed.
        The coordinator also notes that HA talked to the device (see
        ``async_note_device_io``), and holds background polls off while the
        command runs and for a while after (``command_holdoff``).
        """
        self.coordinator.async_note_command_started()
        try:
            yield
        finally:
            self.coordinator.async_note_command_finished()
            self.coordinator.async_note_device_io()
            self.coordinator.async_publish_device_state()

    async def _run_client_command[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Run a device BLE call with a deadline, translating its failures.

        A rejected secret starts reauth; an unsupported request is the caller's
        mistake (ServiceValidationError); a BleakError, a library GoveeBleError
        or a timeout becomes a clean "could not communicate" error instead of a
        raw traceback in the service-call toast. A command that lands tells the
        coordinator the link works again (it drops any poll backoff).
        """
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
                result = await coro
        except GoveeBleAuthError as err:
            raise async_secret_rejected(self.hass, self.coordinator.config_entry) from err
        except GoveeBleNotSupported as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_supported",
                translation_placeholders={"device_name": self._device_name, "error": str(err)},
            ) from err
        except BLE_ERRORS as err:
            _LOGGER.debug("BLE command to %s failed: %s", self._address, err)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="ble_command_failed",
                translation_placeholders={
                    "device_name": self._device_name,
                    # A bare TimeoutError (our deadline) has no message.
                    "error": str(err) or type(err).__name__,
                },
            ) from err
        self.coordinator.async_handle_command_success()
        return result


class GoveeBleLocalDiagnosticEntity(GoveeBleLocalEntity):
    """Base for the read-only diagnostic entities (no BLE I/O of their own).

    They also follow the coordinator's diagnostic-only channel, and stay
    available while the device is unreachable: that's when they matter.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_added_to_hass(self) -> None:
        """Also follow the coordinator's diagnostic-only updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_diagnostic_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Report even when the device is unreachable (that's the interesting case)."""
        return True
