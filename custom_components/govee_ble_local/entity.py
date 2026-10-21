"""Shared base entity for the Govee BLE Local integration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
import logging
from typing import TYPE_CHECKING, Any

from govee_ble_local import GoveeBleAuthError, GoveeBleCommandRejected, GoveeBleNotSupported
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .connect_gate import async_connect_slot
from .const import COMMAND_TIMEOUT_SECONDS, DOMAIN, ZONE_TRANSLATION_KEYS
from .coordinator import GoveeBleLocalCoordinator
from .deferral import OffDeferral
from .helpers import (
    BLE_ERRORS,
    async_context_is_person,
    async_secret_rejected,
    zone_confirmed_off,
)

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
        # Only link to the device: __init__._async_track_device_registry creates
        # its registry record before the platforms load and keeps it current (a
        # second copy of the fields here would be re-applied on every entity add).
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, self._address)})
        # Turn-offs of the device while it already said it is off
        # (``_async_turn_device_off``).
        self._off_deferral = OffDeferral(self._address)

    async def async_added_to_hass(self) -> None:
        """Also drop a turn-off still waiting for other devices once the entity is removed.

        On every add: HA re-adds an entity whose id was renamed without building
        it again, and forgets the callbacks it ran for the removal.
        """
        await super().async_added_to_hass()
        self.async_on_remove(self._off_deferral.async_drop)

    async def async_request_call[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Queue a service call; a turn-off of this entity still waiting goes out first.

        HA queues the call behind the entity platform's PARALLEL_UPDATES slot,
        which a deferred turn-off (``_async_turn_device_off``) holds while it lets
        other devices go first. Ending that wait now sends the turn-off at once,
        as without the deferral, so a turn-on right after it isn't held up. Only
        this entity's whole-device turn-off: the device's other entities queued
        in the same "all off" (a fixture's zone lights) must not send it early.
        A waiting zone turn-off of the device goes out on any call to it: sent
        early, it costs one frame to a fixture already dark.
        """
        self._off_deferral.async_hurry()
        self.coordinator.async_hurry_zone_offs()
        return await super().async_request_call(coro)

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

    @asynccontextmanager
    async def _command(self) -> AsyncIterator[None]:
        """Wrap a command's device calls; then push state to EVERY entity of this device.

        A command often changes state other entities show (whole-fixture off
        turns the zone lights/switches off too), so writing only this entity's
        state would leave its siblings stale until the next poll. It is pushed
        after a failure part-way too: the library already applied what landed.
        The coordinator also notes that HA talked to the device (see
        ``async_note_device_io``), and holds background polls off while the
        command runs and for a while after: this device's, and every device's
        while commands to other devices run at the same time
        (``commands_holding_off``).

        Whether a person sent the command decides whether the device's link
        is kept up after it (coordinator ``_async_end_window``). It is read
        from the service call's context as the command starts: Home Assistant
        forgets an entity's context on a state write 5 s after the call.
        """
        # An in-memory lookup that never suspends: the command still starts in
        # the loop step it was called in (``deferral`` counts on that).
        person = await async_context_is_person(self.hass, self._context)
        self.coordinator.async_note_command_started(person=person)
        try:
            yield
        finally:
            self.coordinator.async_note_command_finished()
            self.coordinator.async_note_device_io()
            self.coordinator.async_publish_device_state()

    async def _async_turn_device_off(
        self, turn_off: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        """Turn the whole device off with the library call ``turn_off`` makes.

        A device that itself said it is off (``coordinator.power_confirmed_off``)
        lets the commands to other devices go first, for at most
        DEFER_OFF_MAX_SECONDS (see ``deferral``), and is then served after them at
        the connect gate. The turn-off is still sent, unless HA stops or the
        entity is removed meanwhile.
        """
        if not self.coordinator.power_confirmed_off:
            await self._async_send_device_off(turn_off)
            return
        async with self._off_deferral.async_turn_off(self.hass) as send:
            if send:
                await self._async_send_device_off(turn_off, last=True)

    async def _async_send_device_off(
        self, turn_off: Callable[[], Coroutine[Any, Any, None]], *, last: bool = False
    ) -> None:
        """Send a whole-device turn-off as a command (``_command``)."""
        async with self._command():
            await self._run_client_command(turn_off(), power_off=True, last=last)

    async def _async_turn_zone_off(self, zone_name: str) -> None:
        """Power a zone off, unless the device has just confirmed it off.

        "Turn off all the lights" turns a fixture off along with its zones, and
        the fixture's acked power-off confirms every zone off
        (``zone_confirmed_off``): the zone's own off frame would only hold the
        link longer, so nothing is sent. That is no command: polls aren't held
        off for it, and no device I/O is noted. Not while another command to the
        device runs (a fixture turn-on from another platform, say): it may switch
        the zone back on, and the zone-off must then come after it.

        A fixture that itself said it is off (``coordinator.power_confirmed_off``)
        is dark whatever its zones say, so the zone-off lets the other devices'
        commands go first, as a whole-device turn-off does (``deferral``), and is
        skipped if the fixture's own turn-off confirmed the zone off meanwhile.
        """
        if self._zone_off_redundant(zone_name):
            return
        if not self.coordinator.power_confirmed_off:
            await self._async_send_zone_off(zone_name)
            return
        untrack = self.coordinator.async_track_zone_off_wait(self._off_deferral.async_hurry)
        try:
            async with self._off_deferral.async_turn_off(self.hass) as send:
                untrack()
                if send and not self._zone_off_redundant(zone_name):
                    await self._async_send_zone_off(zone_name, last=True)
        finally:
            untrack()

    def _zone_off_redundant(self, zone_name: str) -> bool:
        """Return (and log) whether the device just confirmed the zone off, nothing in flight."""
        if self.coordinator.commands_running or not zone_confirmed_off(self._device, zone_name):
            return False
        _LOGGER.debug(
            "Zone %s of %s was just confirmed off; not sending its power-off",
            zone_name,
            self._address,
        )
        self.async_write_ha_state()
        return True

    async def _async_send_zone_off(self, zone_name: str, *, last: bool = False) -> None:
        """Send a zone's power-off as a command (``_command``)."""
        _LOGGER.debug("Turning zone %s off for %s", zone_name, self._address)
        async with self._command():
            await self._run_client_command(
                self._device.set_zone_power(zone_name, False), last=last, keeps_power=True
            )

    async def _run_client_command[T](
        self,
        coro: Coroutine[Any, Any, T],
        *,
        power_off: bool = False,
        last: bool = False,
        keeps_power: bool = False,
    ) -> T:
        """Run a device BLE call with a deadline, translating its failures.

        The call first waits its turn at the connect gate (connect_gate.py), so a
        mass command doesn't overbook a Bluetooth radio; the deadline covers
        that wait. A rejected secret starts reauth; an unsupported request is
        the caller's mistake (ServiceValidationError); a BleakError, a library
        GoveeBleError or a timeout becomes a clean "could not communicate" error
        instead of a raw traceback in the service-call toast. A command that
        lands tells the coordinator the link works again (it drops any poll
        backoff), and whether it was a whole-device turn-off (``power_off``).
        ``last``: a turn-off of a device already off, which the gate serves after
        the other commands. ``keeps_power``: it says nothing about the device's
        power (a zone's turn-off), so the device's last power word stands.

        A rejected secret or command is logged at WARNING, where the always-on
        diagnostics capture (capture.LogCapture) sees it: the library logs it
        only at DEBUG.
        """
        try:
            async with (
                asyncio.timeout(COMMAND_TIMEOUT_SECONDS),
                async_connect_slot(self.hass, self._address, last=last),
            ):
                result = await coro
        except GoveeBleAuthError as err:
            _LOGGER.warning("%s rejected its secret key: %s", self._address, err)
            raise async_secret_rejected(self.hass, self.coordinator.config_entry) from err
        except GoveeBleNotSupported as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_supported",
                translation_placeholders={"device_name": self._device_name, "error": str(err)},
            ) from err
        except BLE_ERRORS as err:
            if isinstance(err, GoveeBleCommandRejected):
                _LOGGER.warning("%s rejected a command: %s", self._address, err)
            else:
                _LOGGER.debug("BLE command to %s failed: %s", self._address, err)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="ble_command_failed",
                translation_placeholders={
                    "device_name": self._device_name,
                    # A bare TimeoutError (our deadline) has no message.
                    "error": str(err) or type(err).__name__,
                },
            ) from err
        finally:
            # Never started if the wait at the gate was cancelled: closed, it isn't
            # reported as a coroutine that was never awaited.
            coro.close()
        self.coordinator.async_handle_command_success(power_off=power_off, keeps_power=keeps_power)
        return result


class GoveeBleLocalDiagnosticEntity(GoveeBleLocalEntity):
    """Base for the read-only diagnostic entities (no BLE I/O of their own).

    They also follow the coordinator's diagnostic-only channel, and stay
    available while the device is unreachable: that's when they matter.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_added_to_hass(self) -> None:
        """Also follow the coordinator's diagnostic-only updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_diagnostic_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Report even when the device is unreachable (that's the interesting case)."""
        return True
