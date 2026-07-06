"""Connection coordinator for a Govee BLE device, shared by all entities.

The v2 library has no aggregate status read-back: ``GoveeDevice.update()``
ensures the connection is alive and returns the device's best-known
(optimistic) :class:`DeviceState`. This coordinator therefore exists mainly for
connection management + availability tracking on a slow cadence; entities track
their own optimistic state from the commands they issue.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from bleak.exc import BleakError
from govee_ble_local import DeviceState, GoveeBleError, GoveeDevice
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, MAX_POLL_INTERVAL_SECONDS, POLL_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)


class GoveeBleLocalCoordinator(DataUpdateCoordinator[DeviceState]):
    """Keeps the BLE connection warm and tracks availability."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: GoveeDevice,
        address: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{address}",
            update_interval=timedelta(seconds=POLL_INTERVAL_SECONDS),
        )
        self._device = device
        self._base_interval = timedelta(seconds=POLL_INTERVAL_SECONDS)
        self._max_interval = timedelta(seconds=MAX_POLL_INTERVAL_SECONDS)
        self._consecutive_failures = 0

    async def _async_update_data(self) -> DeviceState:
        # BleakError (connection drops, no response, out-of-slots, etc.) is an
        # expected/recoverable failure mode for a BLE device, not a bug. HA's
        # DataUpdateCoordinator only treats UpdateFailed as "expected" - other
        # exceptions get logged as a full "Unexpected error" traceback.
        # The library wraps connect/handshake failures in GoveeBleError
        # (GoveeBleConnectionError etc.), which is NOT a BleakError subclass, so
        # it must be caught explicitly or HA logs it as an "unexpected error".
        # TimeoutError is caught too: a stalled handshake response times out via
        # asyncio.wait_for with a bare TimeoutError.
        try:
            data = await self._device.update()
        except (BleakError, GoveeBleError, TimeoutError) as err:
            self._consecutive_failures += 1
            self._apply_backoff()
            raise UpdateFailed(f"Error communicating with device: {err}") from err
        else:
            if self._consecutive_failures:
                self._consecutive_failures = 0
                self.update_interval = self._base_interval
            return data

    def _apply_backoff(self) -> None:
        """Lengthen this device's poll interval after consecutive connect
        failures so a chronically-unreachable device stops launching a
        connection-retry storm every base interval and starving the shared
        adapter. One failure keeps the base cadence (grace for a transient
        blip); after that the interval doubles up to the cap. HA reads
        ``update_interval`` when scheduling the next refresh, so this takes
        effect on the following poll. Reset on the first success.
        """
        steps = max(0, self._consecutive_failures - 1)
        interval = min(self._base_interval * 2**steps, self._max_interval)
        if interval != self.update_interval:
            self.update_interval = interval
            _LOGGER.debug(
                "%s: %d consecutive failures, next poll in %ds",
                self.name,
                self._consecutive_failures,
                interval.total_seconds(),
            )
