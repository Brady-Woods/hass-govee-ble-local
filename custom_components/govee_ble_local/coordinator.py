"""Connection coordinator for a Govee BLE device, shared by all entities.

``Device.update()`` connects and reads back real state for devices that support
it (profile ``readback`` of ``status``/``polled``/``plug``) and otherwise returns
the device's best-known (optimistic) :class:`DeviceState`. This coordinator runs
that poll on a slow, jittered cadence with capped exponential backoff; on/off is
also tracked passively from advertisements (no connection) in ``__init__``.

Availability is driven by *reachability* (is any scanner currently seeing the
device's advertisements?), not by whether the last poll succeeded: a poll can
fail for transient reasons (a busy proxy, a dropped link) while the device is
perfectly controllable, and marking it unavailable then blocks the very
commands that would succeed.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Any

from bleak.exc import BleakError
from govee_ble_local import Device, DeviceState, GoveeBleAuthError, GoveeBleError
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    LAST_SEEN_RESOLUTION_SECONDS,
    MAX_CONCURRENT_POLLS,
    MAX_POLL_INTERVAL_SECONDS,
    POLL_INTERVAL_SECONDS,
    POLL_JITTER_SECONDS,
    UNAVAILABLE_AFTER_FAILURES,
)

_LOGGER = logging.getLogger(__name__)

_POLL_SEMAPHORE_KEY = "poll_semaphore"
# Backoff doubles per consecutive failure; beyond this many doublings the cap
# (MAX_POLL_INTERVAL_SECONDS) is reached anyway, and an uncapped exponent
# overflows timedelta (an unreachable device used to hit OverflowError here
# after ~30 failures, turning every later poll into an "Unexpected error").
_MAX_BACKOFF_STEPS = 8


def _poll_semaphore(hass: HomeAssistant) -> asyncio.Semaphore:
    """Domain-wide cap on simultaneous polls. A fixed size: the HA Bluetooth
    manager already routes each connect to a scanner with a free slot (and
    bleak-retry-connector backs off on out-of-slots); this only keeps the
    integration's own background polling from launching a connect storm."""
    domain_data: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    semaphore = domain_data.get(_POLL_SEMAPHORE_KEY)
    if not isinstance(semaphore, asyncio.Semaphore):
        semaphore = domain_data[_POLL_SEMAPHORE_KEY] = asyncio.Semaphore(
            MAX_CONCURRENT_POLLS
        )
    return semaphore


class GoveeBleLocalCoordinator(DataUpdateCoordinator[DeviceState]):
    """Polls one device and tracks its reachability."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: Device,
        address: str,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{address}",
            update_interval=timedelta(seconds=POLL_INTERVAL_SECONDS),
        )
        self._device = device
        self._address = address
        self._base_interval = timedelta(seconds=POLL_INTERVAL_SECONDS)
        self._max_interval = timedelta(seconds=MAX_POLL_INTERVAL_SECONDS)
        self.consecutive_failures = 0
        # Read-only diagnostics surfaced by the sensor/binary_sensor platforms.
        self.rssi: int | None = None
        self.total_failures = 0
        # "Last seen" = most recent advertisement (passive, no connection);
        # "last connected" = most recent successful poll. They answer different
        # questions: reachability vs. successful control.
        self.last_seen: datetime | None = None
        self.last_connected: datetime | None = None
        # Most recent self-test report (button / capture_session service), for diagnostics.
        self.last_self_test: dict[str, Any] | None = None

    # -- reachability -------------------------------------------------------

    def device_present(self) -> bool:
        """True if any connectable scanner is currently seeing the device."""
        try:
            return bluetooth.async_address_present(
                self.hass, self._address, connectable=True
            )
        except RuntimeError:
            # Bluetooth manager not set up (isolated unit tests only).
            return True

    @property
    def device_available(self) -> bool:
        """Entity availability: reachable over BLE, or still polling fine.

        A device that is advertising stays available through transient poll
        failures (commands can still reach it); one that has vanished from every
        scanner goes unavailable once polls have also failed a few times."""
        if self.device_present():
            return True
        return self.consecutive_failures < UNAVAILABLE_AFTER_FAILURES

    @callback
    def note_advertisement_seen(self) -> bool:
        """Record a passive advertisement. Returns True when the diagnostic
        `last_seen` value moved enough to be worth publishing (advertisements
        arrive several times a second; writing a state per advertisement would
        flood the recorder)."""
        now = dt_util.utcnow()
        previous = self.last_seen
        self.last_seen = now
        return previous is None or (
            now - previous
        ).total_seconds() >= LAST_SEEN_RESOLUTION_SECONDS

    # -- polling ------------------------------------------------------------

    async def _async_update_data(self) -> DeviceState:
        self._sample_rssi()
        if not self.device_present():
            # Nothing can reach it: a connect would just burn ~1 min of
            # bleak-retry-connector attempts (and a poll slot) to fail.
            raise self._record_failure(f"{self._address} is not advertising; poll skipped")

        async with _poll_semaphore(self.hass):
            try:
                data = await self._device.update()
            except GoveeBleAuthError as err:
                # Starts HA's reauth flow (config_flow.async_step_reauth).
                raise ConfigEntryAuthFailed(str(err)) from err
            except (BleakError, GoveeBleError, TimeoutError) as err:
                raise self._record_failure(
                    f"Error communicating with device: {err}"
                ) from err
        self.last_connected = dt_util.utcnow()
        self.consecutive_failures = 0
        self.update_interval = self._jittered(self._base_interval)
        return data

    def _record_failure(self, message: str) -> UpdateFailed:
        self.consecutive_failures += 1
        self.total_failures += 1
        self._apply_backoff()
        # DataUpdateCoordinator only notifies listeners when a failure *changes*
        # last_update_success; push explicitly so the diagnostic entities
        # (failure count, poll interval, availability) track an ongoing outage.
        self.async_update_listeners()
        return UpdateFailed(message)

    @staticmethod
    def _jittered(interval: timedelta) -> timedelta:
        """Random jitter so devices sharing a nominal interval don't drift into
        lockstep and contend for the same connection slots."""
        return interval + timedelta(seconds=random.uniform(*POLL_JITTER_SECONDS))

    def _apply_backoff(self) -> None:
        """Lengthen the poll interval after consecutive failures: the first
        failure keeps the base cadence (grace for a transient blip), then it
        doubles up to the cap. Reset on the first success."""
        steps = min(max(0, self.consecutive_failures - 1), _MAX_BACKOFF_STEPS)
        self.update_interval = min(
            self._jittered(self._base_interval * (2**steps)), self._max_interval
        )
        _LOGGER.debug(
            "%s: %d consecutive failures, next poll in %ds",
            self.name,
            self.consecutive_failures,
            self.update_interval.total_seconds(),
        )

    def _sample_rssi(self) -> None:
        """Refresh ``rssi`` from the most recent advertisement (no connection)."""
        try:
            info = bluetooth.async_last_service_info(
                self.hass, self._address, connectable=True
            )
        except RuntimeError:
            return
        if info is not None:
            self.rssi = info.rssi
