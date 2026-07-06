"""Shared base entity for the Govee BLE Local integration."""
from __future__ import annotations

import logging
from typing import Any, Coroutine

from bleak.exc import BleakError
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GoveeBleLocalCoordinator

_LOGGER = logging.getLogger(__name__)


class GoveeBleLocalEntity(CoordinatorEntity[GoveeBleLocalCoordinator]):
    """Base entity providing shared device info + BLE-error handling."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GoveeBleLocalCoordinator,
        address: str,
        device_name: str,
        model: str,
    ) -> None:
        super().__init__(coordinator)
        self._address = address
        self._device_name = device_name
        self._model = model

    async def _run_client_command(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run a device BLE call, turning a BleakError (or a stalled
        handshake's bare TimeoutError - not a BleakError subclass) into a clean
        UI error instead of a raw Python traceback in the service-call toast.
        """
        try:
            return await coro
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("BLE command to %s failed: %s", self._address, err)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="ble_command_failed",
                translation_placeholders={
                    "device_name": self._device_name,
                    "error": str(err),
                },
            ) from err

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            connections={(CONNECTION_BLUETOOTH, self._address)},
            name=self._device_name,
            manufacturer="Govee",
            model=self._model,
        )
