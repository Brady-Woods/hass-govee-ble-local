"""Connection coordinator for a Govee BLE device, shared by all entities.

``Device.update()`` connects and reads back real state for devices that support
it (profile ``readback`` of ``status``/``polled``) and otherwise returns the
device's best-known (optimistic) :class:`DeviceState`. This coordinator manages
that connection + availability tracking on a slow cadence; on/off is also
tracked passively from advertisements (no connection) in ``__init__``.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import Any

from bleak.exc import BleakError
from bleak_retry_connector import BleakOutOfConnectionSlotsError
from govee_ble_local import Device, DeviceState, GoveeBleError
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    MAX_POLL_INTERVAL_SECONDS,
    OUT_OF_SLOTS_BACKOFF_STEPS,
    POLL_INTERVAL_SECONDS,
    POLL_JITTER_SECONDS,
)
from .scheduling import (
    async_connect_semaphore,
    async_loaded_entry_count,
    async_slots_exhausted,
    async_usable_slots,
    dynamic_interval,
)

_LOGGER = logging.getLogger(__name__)


class GoveeBleLocalCoordinator(DataUpdateCoordinator[DeviceState]):
    """Keeps the BLE connection warm and tracks availability."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: Device,
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
        # "Last seen" = most recent advertisement (passive, no connection),
        # stamped by the advertisement callback in __init__. "Last connected" =
        # most recent successful connect-poll. The two answer different
        # questions: reachability vs. successful control.
        self.last_seen: datetime | None = None
        self.last_connected: datetime | None = None
        # Most recent self-test report (button / capture_session service), for diagnostics.
        self.last_self_test: dict[str, Any] | None = None

    async def _async_update_data(self) -> DeviceState:
        # Sample the last advertisement's RSSI (no connection needed) so the
        # signal-strength sensor tracks even a device we then fail to connect to.
        self._sample_rssi()

        # If the shared BLE connection-slot pool has essentially no room left
        # (reserved headroom already eaten into), don't even attempt a connect -
        # it would almost certainly fail with BleakOutOfConnectionSlotsError and
        # only add to the contention every other device sharing the pool is also
        # experiencing. Treat this the same as actually hitting that error.
        if await async_slots_exhausted(self.hass):
            raise self._record_out_of_slots(
                f"{self.name}: no free BLE connection slots; skipped"
            )

        # Cap how many devices, integration-wide, may be simultaneously mid-connect
        # so this integration's own polling can't monopolize the shared slot pool.
        semaphore = await async_connect_semaphore(self.hass)
        async with semaphore:
            # BleakError (connection drops, no response, out-of-slots, etc.) is an
            # expected/recoverable failure mode for a BLE device, not a bug. HA's
            # DataUpdateCoordinator only treats UpdateFailed as "expected" - other
            # exceptions get logged as a full "Unexpected error" traceback.
            # The library wraps connect/handshake failures in GoveeBleError
            # (GoveeBleConnectionError etc.), which is NOT a BleakError subclass,
            # so it must be caught explicitly or HA logs it as an "unexpected
            # error". TimeoutError is caught too: a stalled handshake response
            # times out via asyncio.wait_for with a bare TimeoutError.
            try:
                data = await self._device.update()
            except BleakOutOfConnectionSlotsError as err:
                # The whole pool was out of slots for this attempt (not just a
                # "this device" style error) - retrying quickly is nearly
                # guaranteed to fail again while every other device is also
                # contending for the same exhausted pool.
                raise self._record_out_of_slots(
                    f"Error communicating with device: {err}"
                ) from err
            except (BleakError, GoveeBleError, TimeoutError) as err:
                self._consecutive_failures += 1
                self.total_failures += 1
                self._apply_backoff()
                raise UpdateFailed(f"Error communicating with device: {err}") from err
            else:
                self.last_connected = dt_util.utcnow()
                self._consecutive_failures = 0
                await self._reset_interval_to_dynamic_base()
                return data

    def _record_out_of_slots(self, message: str) -> UpdateFailed:
        """Record a slot-exhaustion event (hit live, or detected proactively before
        even trying) and skip the usual 1-failure grace, jumping straight several
        exponential-backoff steps ahead - see OUT_OF_SLOTS_BACKOFF_STEPS."""
        self._consecutive_failures += 1 + OUT_OF_SLOTS_BACKOFF_STEPS
        self.total_failures += 1
        self._apply_backoff()
        return UpdateFailed(message)

    async def _reset_interval_to_dynamic_base(self) -> None:
        """After a success, retarget the poll interval to the base cadence
        stretched for the current device-count/slot-availability ratio (see
        `scheduling.dynamic_interval`), plus jitter so recovering devices don't
        all snap back to the exact same cadence and re-converge over uptime."""
        device_count = async_loaded_entry_count(self.hass)
        usable_slots = await async_usable_slots(self.hass)
        target = dynamic_interval(self._base_interval, device_count, usable_slots)
        self.update_interval = self._jittered(target)

    @staticmethod
    def _jittered(interval: timedelta) -> timedelta:
        """Add random jitter so devices sharing the same nominal interval don't
        stay in lockstep. Applied on every success (dynamic-base reset) and every
        backoff step - the only place intervals are assigned deterministically
        without it is the initial one-time startup stagger in __init__.py."""
        return interval + timedelta(seconds=random.uniform(*POLL_JITTER_SECONDS))

    @callback
    def note_advertisement_seen(self) -> None:
        """Record that a passive advertisement just arrived (device reachable)."""
        self.last_seen = dt_util.utcnow()

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
        interval = min(self._jittered(self._base_interval * 2**steps), self._max_interval)
        self.update_interval = interval
        _LOGGER.debug(
            "%s: %d consecutive failures, next poll in %ds",
            self.name,
            self._consecutive_failures,
            interval.total_seconds(),
        )
