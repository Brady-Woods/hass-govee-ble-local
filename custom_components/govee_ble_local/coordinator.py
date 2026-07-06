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
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
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
        self._address = address
        self._base_interval = timedelta(seconds=POLL_INTERVAL_SECONDS)
        self._max_interval = timedelta(seconds=MAX_POLL_INTERVAL_SECONDS)
        self._consecutive_failures = 0
        # Read-only diagnostics surfaced by the sensor/binary_sensor platforms.
        self.rssi: int | None = None
        self.total_failures = 0

    async def _async_update_data(self) -> DeviceState:
        # Sample the last advertisement's RSSI (no connection needed) so the
        # signal-strength sensor tracks even a device we then fail to connect to.
        self._sample_rssi()

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
            self.total_failures += 1
            self._apply_backoff()
            raise UpdateFailed(f"Error communicating with device: {err}") from err
        else:
            if self._consecutive_failures:
                self._consecutive_failures = 0
                self.update_interval = self._base_interval
            return data

    @callback
    def _async_refresh_finished(self) -> None:
        # DataUpdateCoordinator suppresses listener updates on *consecutive*
        # failures (no data change), which would freeze our diagnostic entities
        # (connection failures, poll interval, signal strength) during exactly
        # the outage they're meant to show. The base still notifies on the first
        # failure and on recovery, so we only need to cover the suppressed case.
        if self._consecutive_failures >= 2:
            self.async_update_listeners()

    def _sample_rssi(self) -> None:
        """Refresh ``rssi`` from the most recent advertisement. Guarded so a
        coordinator built without the HA bluetooth stack (direct unit tests)
        can't break a poll."""
        try:
            info = bluetooth.async_last_service_info(
                self.hass, self._address, connectable=True
            )
        except RuntimeError:
            # Bluetooth manager not set up (only happens in isolated unit tests;
            # in production the integration depends on the bluetooth stack).
            return
        if info is not None:
            self.rssi = info.rssi

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
