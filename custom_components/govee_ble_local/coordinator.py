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
RSSI, failure count, poll interval). Last seen / RSSI are refreshed from HA's
advertisement history on a timer (``async_refresh_presence``): the
advertisement callback only fires when an advertisement's content changes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import math
import random
from typing import TYPE_CHECKING

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

_BASE_INTERVAL = timedelta(seconds=POLL_INTERVAL_SECONDS)
_MAX_INTERVAL = timedelta(seconds=MAX_POLL_INTERVAL_SECONDS)
# Backoff doubles per consecutive failure until it reaches the cap; more
# doublings than that only risk overflowing timedelta.
_MAX_BACKOFF_STEPS = math.ceil(math.log2(_MAX_INTERVAL / _BASE_INTERVAL))


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


@callback
def _async_subscribe(
    listeners: list[CALLBACK_TYPE], update_callback: CALLBACK_TYPE
) -> CALLBACK_TYPE:
    """Add ``update_callback`` to ``listeners``; return the callback that removes it."""
    listeners.append(update_callback)

    @callback
    def _remove() -> None:
        listeners.remove(update_callback)

    return _remove


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
            update_interval=_BASE_INTERVAL,
        )
        self._device = device
        self._address = address
        self.consecutive_failures = 0
        # Read-only diagnostics surfaced by the sensor/binary_sensor platforms.
        self.rssi: int | None = None
        self.total_failures = 0
        # The poll interval before jitter (base or backed off): what the
        # diagnostics show, so they only change when the cadence does.
        self.nominal_interval = _BASE_INTERVAL
        # "Last seen" = most recent advertisement any scanner heard (passive, no
        # connection); "last connected" = most recent poll that read real state
        # back. They answer different questions: reachability vs. successful control.
        self.last_seen: datetime | None = None
        self.last_connected: datetime | None = None
        self._diagnostic_listeners: list[CALLBACK_TYPE] = []
        self._state_listeners: list[CALLBACK_TYPE] = []
        # Receive time (monotonic) of the advertisement last_seen/RSSI came from.
        self._presence_time: float | None = None
        # Availability as last pushed to the entities (None = never pushed).
        self._published_available: bool | None = None
        # A poll was skipped because the device wasn't advertising; when it
        # reappears, poll right away instead of waiting out the backoff.
        self._skipped_absent = False
        # The last poll was rejected for the secret: HA schedules no more polls.
        self._auth_failed = False
        # A command already dropped the backoff during this failure streak.
        self._command_reset_used = False

    # -- reachability -------------------------------------------------------

    @callback
    def async_address_present(self, *, connectable: bool = True) -> bool:
        """Return True if a scanner (connectable by default) currently sees the device."""
        return bluetooth.async_address_present(self.hass, self._address, connectable=connectable)

    @callback
    def async_last_service_info(
        self, *, connectable: bool = True
    ) -> bluetooth.BluetoothServiceInfoBleak | None:
        """Return the advertisement HA keeps for a (connectable by default) scanner, if any."""
        return bluetooth.async_last_service_info(self.hass, self._address, connectable=connectable)

    @property
    def polls_scheduled(self) -> bool:
        """Return whether HA schedules polls for this device.

        It doesn't while polling is disabled in the entry's system options, or
        after a rejected secret until a poll runs again (a reload with a new
        secret, or a manual refresh).
        """
        return not (self.config_entry.pref_disable_polling or self._auth_failed)

    @property
    def device_available(self) -> bool:
        """Entity availability: reachable over BLE, or still polling fine.

        A device that is advertising stays available through transient poll
        failures (commands can still reach it); one that has vanished from every
        connectable scanner goes unavailable once polls have also failed (or
        been skipped) a few times - or at once when no polls are scheduled to
        count those failures (see ``polls_scheduled``).
        """
        if self.async_address_present():
            return True
        if not self.polls_scheduled:
            return False
        return self.consecutive_failures < UNAVAILABLE_AFTER_FAILURES

    # -- notifications ------------------------------------------------------

    @callback
    def async_update_listeners(self) -> None:
        """Push to every entity and state listener, remembering the availability pushed.

        The entities come first, and a state listener that raises is logged
        rather than propagated, the way HA isolates coordinator listeners: a
        failing hook must not cost the entities their update.
        """
        self._published_available = self.device_available
        super().async_update_listeners()
        for update_callback in list(self._state_listeners):
            try:
                update_callback()
            except Exception:
                self.logger.exception(
                    "Unexpected error updating state listener %s for %s",
                    id(update_callback),
                    self.name,
                )

    @callback
    def async_add_state_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Follow every push to all entities; returns the unsubscribe callback.

        Unlike ``async_add_listener`` this doesn't keep HA's poll timer running
        (HA polls while the coordinator has listeners), so a hook that isn't an
        entity can't keep polling a device whose entities are all disabled.
        """
        return _async_subscribe(self._state_listeners, update_callback)

    @callback
    def async_add_diagnostic_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Listen for diagnostic-only changes; returns the unsubscribe callback."""
        return _async_subscribe(self._diagnostic_listeners, update_callback)

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
    def async_handle_advertisement(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        """Take on/off (and from a connectable scanner, the BLEDevice) from an advertisement.

        HA calls this from any scanner, but only when the advertisement's content
        changed, not for each repeat: last seen / RSSI therefore come from the
        history on a timer (``async_refresh_presence``), except when the device
        is heard again after no scanner did. Only pushes to the entities when
        something they show actually changed.
        """
        if service_info.connectable:
            # Keep the freshest BLEDevice (best scanner/proxy path) for the next
            # connection; a passive scanner's can't be connected through.
            self._device.update_ble_device(service_info.device)

        if self._skipped_absent and self.async_address_present():
            # Back after polls were skipped for its absence: drop the backoff and
            # reconcile the rich state now instead of up to MAX_POLL_INTERVAL later.
            self._skipped_absent = False
            self._set_poll_interval(_BASE_INTERVAL)
            self.config_entry.async_create_background_task(
                self.hass, self.async_request_refresh(), name=f"{self.name}_return_poll"
            )

        # Heard again after no scanner did: show it now, not at the next refresh.
        heard_again = False
        if self.rssi is None:
            heard_again = self._take_presence(service_info)
        if self._device.ingest_advertisement(service_info.advertisement):
            self.async_publish_device_state()
        elif self.device_available != self._published_available:
            self.async_update_listeners()
        elif heard_again:
            self.async_update_diagnostics()

    @callback
    def async_refresh_presence(self, _now: datetime | None = None) -> None:
        """Publish last seen / RSSI from the newest advertisement any scanner heard.

        Runs every LAST_SEEN_RESOLUTION_SECONDS (see __init__): HA's history
        records every advertisement, while the advertisement callback fires once
        for a device that keeps repeating the same one.
        """
        info = self.async_last_service_info(connectable=False)
        if info is not None and self._take_presence(info):
            self.async_update_diagnostics()

    def _take_presence(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> bool:
        """Take last seen / RSSI from an advertisement; False if it was already taken."""
        if service_info.time == self._presence_time:
            return False
        self._presence_time = service_info.time
        self.rssi = service_info.rssi
        # Stamped with the monotonic time it was received: a history entry can
        # be minutes old (e.g. one from the BlueZ cache at startup).
        age = max(0.0, bluetooth.MONOTONIC_TIME() - service_info.time)
        self.last_seen = dt_util.utcnow() - timedelta(seconds=age)
        return True

    @callback
    def async_handle_unavailable(self, *, connectable: bool) -> None:
        """React to the device dropping out of the (connectable) scanner history."""
        if not connectable:
            # No scanner at all sees it any more: the last RSSI is meaningless.
            self.rssi = None
        # HA drops both histories in one sweep, and usually only the diagnostics
        # change (connectivity, RSSI, connection source): push to everyone only
        # when availability flipped.
        self._async_publish_status()

    @callback
    def async_handle_command_success(self) -> None:
        """Drop the poll backoff: an entity command just reached the device.

        The link demonstrably works again, so the next poll comes at the base
        cadence instead of up to MAX_POLL_INTERVAL_SECONDS later. Only a
        backed-off interval is reset, so a stream of commands can't keep
        postponing the poll; the failure count is left for that poll to reset,
        so availability and the diagnostics stay driven by polls. The reset
        happens once per failure streak: when polls keep failing although
        commands land (a read-back that keeps failing), repeated commands must
        not hold the device at the base cadence. Nothing is re-armed while no
        polls are scheduled (``polls_scheduled``).
        """
        if (
            not self.polls_scheduled
            or self._command_reset_used
            or self.nominal_interval <= _BASE_INTERVAL
        ):
            return
        self._command_reset_used = True
        self._set_poll_interval(_BASE_INTERVAL)
        # HA armed the poll timer with the backed-off interval: re-arm it.
        self._schedule_refresh()
        self.async_update_diagnostics()

    # -- polling ------------------------------------------------------------

    async def _async_update_data(self) -> DeviceState:
        # Any poll that runs (a manual refresh after an auth failure included)
        # means polling is scheduled again.
        self._auth_failed = False
        if not self.async_address_present():
            # Nothing can reach it: a connect would just burn ~1 min of
            # bleak-retry-connector attempts (and a poll slot) to fail.
            self._skipped_absent = True
            self._async_note_failure()
            raise self._update_failed("poll_skipped_absent")

        async with _poll_semaphore(self.hass):
            try:
                data = await self._device.update()
            except GoveeBleAuthError as err:
                # Starts HA's reauth flow (config_flow.async_step_reauth); HA stops
                # scheduling polls until the entry is reloaded with a new secret.
                self._auth_failed = True
                # HA doesn't notify listeners when this follows a failed poll;
                # the poll interval (and availability) change regardless.
                self._async_publish_status()
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN,
                    translation_key="secret_rejected",
                    translation_placeholders={"device_name": self.config_entry.title},
                ) from err
            except BLE_ERRORS as err:
                self.total_failures += 1
                self._async_note_failure()
                # A bare TimeoutError has no message.
                raise self._update_failed(
                    "poll_failed", error=str(err) or type(err).__name__
                ) from err
            except Exception as err:
                # A library bug must still back off like any failed poll instead
                # of hammering the device at the base cadence.
                _LOGGER.exception("Unexpected error polling %s", self._address)
                self.total_failures += 1
                self._async_note_failure()
                raise self._update_failed("poll_failed", error=repr(err)) from err
        if not data.optimistic:
            self.last_connected = dt_util.utcnow()
        self.consecutive_failures = 0
        self._skipped_absent = False
        self._command_reset_used = False
        self._set_poll_interval(_BASE_INTERVAL)
        return data

    @callback
    def _async_note_failure(self) -> None:
        """Count a failed (or skipped) poll, back off, and publish the new status."""
        self.consecutive_failures += 1
        self._apply_backoff()
        # DataUpdateCoordinator only notifies listeners when a failure *changes*
        # last_update_success; push explicitly so the diagnostic entities
        # (failure count, poll interval) and availability track an ongoing outage.
        self._async_publish_status()

    def _update_failed(self, translation_key: str, **placeholders: str) -> UpdateFailed:
        """Build the translated error for a failed (or skipped) poll."""
        return UpdateFailed(
            translation_domain=DOMAIN,
            translation_key=translation_key,
            translation_placeholders={"address": self._address, **placeholders},
        )

    @staticmethod
    def _jittered(interval: timedelta) -> timedelta:
        """Add random jitter to a poll interval.

        Devices sharing a nominal interval would otherwise drift into lockstep
        and contend for the same connection slots.
        """
        return interval + timedelta(seconds=random.uniform(*POLL_JITTER_SECONDS))  # noqa: S311

    def _set_poll_interval(self, nominal: timedelta) -> timedelta:
        """Poll every ``nominal`` plus jitter; return the jittered interval HA schedules.

        Only HA's schedule is jittered: the diagnostics show ``nominal_interval``,
        which would otherwise take a new random value (a new state) every poll.
        """
        self.nominal_interval = nominal
        interval = self._jittered(nominal)
        self.update_interval = interval
        return interval

    def _apply_backoff(self) -> None:
        """Lengthen the poll interval after consecutive failures.

        The first failure keeps the base cadence (grace for a transient blip),
        then it doubles up to the cap. Jitter is added after capping so devices
        sitting at the cap still don't poll in lockstep. Reset on success.
        """
        steps = min(max(0, self.consecutive_failures - 1), _MAX_BACKOFF_STEPS)
        interval = self._set_poll_interval(min(_BASE_INTERVAL * (2**steps), _MAX_INTERVAL))
        _LOGGER.debug(
            "%s: %d consecutive failures, next poll in %ds",
            self.name,
            self.consecutive_failures,
            interval.total_seconds(),
        )
