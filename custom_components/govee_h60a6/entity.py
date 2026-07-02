"""Shared base entity for the Govee H60A6 integration."""
from __future__ import annotations

import logging
from typing import Any, Coroutine

from bleak.exc import BleakError
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

try:
    from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH_LE as CONNECTION_BLE
except ImportError:  # older HA core
    from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH as CONNECTION_BLE

from .const import DOMAIN
from .coordinator import GoveeH60A6Coordinator

_LOGGER = logging.getLogger(__name__)


class GoveeH60A6Entity(CoordinatorEntity[GoveeH60A6Coordinator]):
    """Base entity providing shared device info built from polled status."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: GoveeH60A6Coordinator, address: str, device_name: str
    ) -> None:
        super().__init__(coordinator)
        self._address = address
        self._device_name = device_name

    async def _run_client_command(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run a client BLE call, turning a BleakError into a clean UI error.

        Without this, a connection failure would surface to the user as a
        raw Python traceback in the service-call error toast instead of a
        readable message.
        """
        try:
            return await coro
        except BleakError as err:
            _LOGGER.debug("BLE command to %s failed: %s", self._address, err)
            raise HomeAssistantError(
                f"Could not communicate with {self._device_name}: {err}"
            ) from err

    @property
    def device_info(self) -> DeviceInfo:
        status = self.coordinator.data
        connections = {(CONNECTION_BLE, self._address)}
        if status and status.wifi_mac:
            connections.add((CONNECTION_NETWORK_MAC, status.wifi_mac))

        return DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            connections=connections,
            name=self._device_name,
            manufacturer="Govee",
            model="H60A6",
            hw_version=status.hardware_version if status else None,
        )
