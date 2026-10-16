"""Shared base entity for the Govee BLE Local integration."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
import logging
from typing import TYPE_CHECKING, Any

from govee_ble_local import GoveeBleAuthError, GoveeBleNotSupported
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import COMMAND_TIMEOUT_SECONDS, DOMAIN, MANUFACTURER, ZONE_TRANSLATION_KEYS
from .coordinator import GoveeBleLocalCoordinator
from .helpers import BLE_ERRORS, COMMAND_REJECTED

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
        # Read-back device info (versions, serial, Wi-Fi MAC) is kept current
        # by __init__._async_track_device_registry, not here: entity device info
        # is only applied when the entity is added.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            connections={(CONNECTION_BLUETOOTH, self._address)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=self._device.sku,
        )

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

    @callback
    def _async_publish_command_state(self) -> None:
        """Push optimistic state after a command to EVERY entity of this device.

        A command often changes state other entities show (whole-fixture off
        turns the zone lights/switches off too), so writing only this entity's
        state would leave its siblings stale until the next poll.
        """
        self.coordinator.async_publish_device_state()

    async def _run_client_command[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Run a device BLE call with a deadline, translating its failures.

        A rejected secret starts reauth; an unsupported request is the caller's
        mistake (ServiceValidationError); a BleakError, a library GoveeBleError
        or a timeout becomes a clean "could not communicate" error instead of a
        raw traceback in the service-call toast.

        A rejected secret or command is logged at WARNING, where the always-on
        diagnostics capture (capture.LogCapture) sees it: the library leaves
        logging it to the caller.
        """
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
                return await coro
        except GoveeBleAuthError as err:
            _LOGGER.warning("%s rejected its secret key: %s", self._address, err)
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="secret_rejected",
                translation_placeholders={"device_name": self._device_name},
            ) from err
        except GoveeBleNotSupported as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_supported",
                translation_placeholders={"device_name": self._device_name, "error": str(err)},
            ) from err
        except COMMAND_REJECTED as err:
            _LOGGER.warning("%s rejected a command: %s", self._address, err)
            raise self._command_failed(err) from err
        except BLE_ERRORS as err:
            _LOGGER.debug("BLE command to %s failed: %s", self._address, err)
            raise self._command_failed(err) from err

    def _command_failed(self, err: Exception) -> HomeAssistantError:
        """Return the "could not communicate" error for a failed command."""
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="ble_command_failed",
            translation_placeholders={
                "device_name": self._device_name,
                # A bare TimeoutError (our deadline) has no message.
                "error": str(err) or type(err).__name__,
            },
        )
