"""Connection coordinator for a Govee BLE device, shared by all entities.

``Device.update()`` connects and reads back real state for devices that support
it (profile ``readback`` of ``status``/``polled``/``plug``) and otherwise returns
the device's best-known (optimistic) :class:`DeviceState`. This coordinator runs
that poll on a slow, jittered cadence with capped exponential backoff; on/off is
also tracked passively from advertisements (no connection).

Availability is driven by *reachability* (is any connectable scanner currently
seeing the device's advertisements?), not by whether the last poll succeeded: a
poll can fail for transient reasons (a busy proxy, a dropped link) while the
device is perfectly controllable, and marking it unavailable then blocks the
very commands that would succeed.

Two notification channels keep state writes proportional to what changed:
``async_update_listeners`` reaches every entity (device state or availability
changed), ``async_update_diagnostics`` only the diagnostic entities (last seen,
RSSI, failure count, poll interval).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import math
import random
from typing import TYPE_CHECKING, Any

from govee_ble_local import Device, DeviceState, GoveeBleAuthError
from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import (
    DOMAIN,
    LAST_SEEN_RESOLUTION_SECONDS,
    MAX_CONCURRENT_POLLS,
    MAX_POLL_INTERVAL_SECONDS,
    POLL_INTERVAL_SECONDS,
    POLL_JITTER_SECONDS,
    UNAVAILABLE_AFTER_FAILURES,
)
from .helpers import BLE_ERRORS

if TYPE_CHECKING:
    from . import GoveeBleLocalConfigEntry

_LOGGER = logging.getLogger(__name__)

_POLL_SEMAPHORE: HassKey[asyncio.Semaphore] = HassKey(f"{DOMAIN}_poll_semaphore")

# Backoff doubles per consecutive failure until it reaches the cap; more
# doublings than that only risk overflowing timedelta.
_MAX_BACKOFF_STEPS = math.ceil(math.log2(MAX_POLL_INTERVAL_SECONDS / POLL_INTERVAL_SECONDS))


def _poll_semaphore(hass: HomeAssistant) -> asyncio.Semaphore:
    """Return the domain-wide cap on simultaneous polls.

    The HA Bluetooth manager already routes each connect to a scanner with a
    free slot (and bleak-retry-connector backs off on out-of-slots); this only
    keeps the integration's own background polling from launching a connect
    storm.
    """
    if (semaphore := hass.data.get(_POLL_SEMAPHORE)) is None:
        semaphore = hass.data[_POLL_SEMAPHORE] = asyncio.Semaphore(MAX_CONCURRENT_POLLS)
    return semaphore


class GoveeBleLocalCoordinator(DataUpdateCoordinator[DeviceState]):
    """Polls one device and tracks its reachability."""

    config_entry: GoveeBleLocalConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GoveeBleLocalConfigEntry,
        device: Device,
    ) -> None:
        """Initialize the coordinator for one config entry's device."""
        address: str = entry.data[CONF_ADDRESS]
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
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
        # "last connected" = most recent poll that read real state back. They
        # answer different questions: reachability vs. successful control.
        self.last_seen: datetime | None = None
        self.last_connected: datetime | None = None
        # Most recent self-test report (button / capture_session service), for diagnostics.
        self.last_self_test: dict[str, Any] | None = None
        self._diagnostic_listeners: list[CALLBACK_TYPE] = []
        # When last_seen/RSSI were last pushed to the diagnostic entities.
        self._diagnostics_published_at: datetime | None = None
        # Availability as last pushed to the entities (None = never pushed).
        self._published_available: bool | None = None
        # A poll was skipped because the device wasn't advertising; when it
        # reappears, poll right away instead of waiting out the backoff.
        self._skipped_absent = False

    # -- reachability -------------------------------------------------------

    def address_present(self, *, connectable: bool = True) -> bool:
        """Return True if a scanner (connectable by default) currently sees the device."""
        return bluetooth.async_address_present(
            self.hass, self._address, connectable=connectable
        )

    def last_service_info(self) -> bluetooth.BluetoothServiceInfoBleak | None:
        """Return the most recent connectable advertisement, if any."""
        return bluetooth.async_last_service_info(self.hass, self._address, connectable=True)

    @property
    def device_available(self) -> bool:
        """Entity availability: reachable over BLE, or still polling fine.

        A device that is advertising stays available through transient poll
        failures (commands can still reach it); one that has vanished from every
        connectable scanner goes unavailable once polls have also failed (or
        been skipped) a few times.
        """
        if self.address_present():
            return True
        return self.consecutive_failures < UNAVAILABLE_AFTER_FAILURES

    # -- notifications ------------------------------------------------------

    @callback
    def async_update_listeners(self) -> None:
        """Push to every entity, remembering the availability that was pushed."""
        self._published_available = self.device_available
        super().async_update_listeners()

    @callback
    def async_add_diagnostic_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Listen for diagnostic-only changes; returns the unsubscribe callback."""
        self._diagnostic_listeners.append(update_callback)

        @callback
        def _remove() -> None:
            self._diagnostic_listeners.remove(update_callback)

        return _remove

    @callback
    def async_update_diagnostics(self) -> None:
        """Push a diagnostic-only change (no device state changed)."""
        for update_callback in list(self._diagnostic_listeners):
            update_callback()

    @callback
    def async_publish_device_state(self) -> None:
        """Publish the device's (in-place mutated) state to every entity.

        Unlike ``async_set_updated_data`` this neither reschedules the poll
        timer nor marks a failing poll as successful: passive advertisement
        changes must not postpone the next poll or re-arm HA's log-once gate.
        """
        self.data = self._device.state
        self.async_update_listeners()

    @callback
    def _async_publish_status(self) -> None:
        """Push a reachability change to everyone, anything else to diagnostics."""
        if self.device_available != self._published_available:
            self.async_update_listeners()
        else:
            self.async_update_diagnostics()

    # -- passive (advertisement) path ---------------------------------------

    @callback
    def async_handle_advertisement(
        self, service_info: bluetooth.BluetoothServiceInfoBleak
    ) -> None:
        """Take on/off, RSSI and the freshest BLEDevice from an advertisement.

        Only pushes to the entities when something they show actually changed:
        advertisements arrive several times a second per device.
        """
        now = dt_util.utcnow()
        # Due relative to the last PUBLISH (not the last advertisement, which
        # arrives every few seconds), or straight away when RSSI comes back.
        diagnostics_due = (
            self._diagnostics_published_at is None
            or self.rssi is None
            or (now - self._diagnostics_published_at).total_seconds()
            >= LAST_SEEN_RESOLUTION_SECONDS
        )
        self.last_seen = now
        self.rssi = service_info.rssi
        # Keep the freshest BLEDevice (best scanner/proxy path) for the next connection.
        self._device.update_ble_device(service_info.device)

        if self._skipped_absent and self.address_present():
            # Back after polls were skipped for its absence: drop the backoff and
            # reconcile the rich state now instead of up to MAX_POLL_INTERVAL later.
            self._skipped_absent = False
            self.update_interval = self._jittered(self._base_interval)
            self.config_entry.async_create_background_task(
                self.hass, self.async_request_refresh(), name=f"{self.name}_return_poll"
            )

        if self._device.ingest_advertisement(service_info.advertisement):
            self.async_publish_device_state()
        elif self.device_available != self._published_available:
            self.async_update_listeners()
        elif diagnostics_due:
            self.async_update_diagnostics()
        else:
            return
        # Every branch above reached the diagnostic entities.
        self._diagnostics_published_at = now

    @callback
    def async_handle_unavailable(self, *, connectable: bool) -> None:
        """React to the device dropping out of the (connectable) scanner history."""
        if not connectable:
            # No scanner at all sees it any more: the last RSSI is meaningless.
            self.rssi = None
        self.async_update_listeners()

    # -- polling ------------------------------------------------------------

    async def _async_update_data(self) -> DeviceState:
        if not self.address_present():
            # Nothing can reach it: a connect would just burn ~1 min of
            # bleak-retry-connector attempts (and a poll slot) to fail.
            self._skipped_absent = True
            raise self._record_failure(f"{self._address} is not advertising; poll skipped")

        async with _poll_semaphore(self.hass):
            try:
                data = await self._device.update()
            except GoveeBleAuthError as err:
                # Starts HA's reauth flow (config_flow.async_step_reauth); HA stops
                # scheduling polls until the entry is reloaded with a new secret.
                raise ConfigEntryAuthFailed(str(err)) from err
            except BLE_ERRORS as err:
                self.total_failures += 1
                raise self._record_failure(f"Error communicating with device: {err}") from err
            except Exception as err:
                # A library bug must still back off like any failed poll instead
                # of hammering the device at the base cadence.
                _LOGGER.exception("Unexpected error polling %s", self._address)
                self.total_failures += 1
                raise self._record_failure(f"Unexpected error: {err!r}") from err
        if not data.optimistic:
            self.last_connected = dt_util.utcnow()
        self.consecutive_failures = 0
        self._skipped_absent = False
        self.update_interval = self._jittered(self._base_interval)
        return data

    def _record_failure(self, message: str) -> UpdateFailed:
        self.consecutive_failures += 1
        self._apply_backoff()
        # DataUpdateCoordinator only notifies listeners when a failure *changes*
        # last_update_success; push explicitly so the diagnostic entities
        # (failure count, poll interval) and availability track an ongoing outage.
        self._async_publish_status()
        return UpdateFailed(message)

    @staticmethod
    def _jittered(interval: timedelta) -> timedelta:
        """Add random jitter to a poll interval.

        Devices sharing a nominal interval would otherwise drift into lockstep
        and contend for the same connection slots.
        """
        return interval + timedelta(seconds=random.uniform(*POLL_JITTER_SECONDS))  # noqa: S311

    def _apply_backoff(self) -> None:
        """Lengthen the poll interval after consecutive failures.

        The first failure keeps the base cadence (grace for a transient blip),
        then it doubles up to the cap. Jitter is added after capping so devices
        sitting at the cap still don't poll in lockstep. Reset on success.
        """
        steps = min(max(0, self.consecutive_failures - 1), _MAX_BACKOFF_STEPS)
        self.update_interval = self._jittered(
            min(self._base_interval * (2**steps), self._max_interval)
        )
        _LOGGER.debug(
            "%s: %d consecutive failures, next poll in %ds",
            self.name,
            self.consecutive_failures,
            self.update_interval.total_seconds(),
        )
