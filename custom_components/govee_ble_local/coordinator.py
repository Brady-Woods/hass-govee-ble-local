"""Polling coordinator for a Govee BLE light, shared by all entities."""
from __future__ import annotations

import logging
from datetime import timedelta

from bleak.exc import BleakError
from govee_ble_local import GoveeBleClient, GoveeBleStatus
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, POLL_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)


class GoveeBleLocalCoordinator(DataUpdateCoordinator[GoveeBleStatus]):
    """Periodically polls the light over BLE so HA stays in sync with app changes."""

    def __init__(self, hass: HomeAssistant, client: GoveeBleClient, address: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{address}",
            update_interval=timedelta(seconds=POLL_INTERVAL_SECONDS),
        )
        self._client = client

    async def _async_update_data(self) -> GoveeBleStatus:
        # BleakError (connection drops, no response, out-of-slots, etc.) is an
        # expected/recoverable failure mode for a BLE device, not a bug. HA's
        # DataUpdateCoordinator only treats UpdateFailed as "expected" -
        # anything else gets logged as a full traceback under "Unexpected
        # error", which is exactly the noisy failure mode we want to avoid.
        try:
            return await self._client.get_status()
        except BleakError as err:
            raise UpdateFailed(f"Error communicating with device: {err}") from err
