"""Slot-aware BLE connection scheduling shared across all Govee BLE Local entries.

Home Assistant's Bluetooth manager tracks a finite pool of connection "slots" across
the local adapter and any ESPHome BLE proxies. With ~12+ Govee devices sharing that
pool, polling them all on a fixed interval with no cross-device awareness eventually
saturates it (confirmed live: a `BleakOutOfConnectionSlotsError` after 10 attempts
when a proxy went offline, mid-outage; see CHANGELOG). This module is the single
source of truth for "how many slots can THIS integration safely use right now",
consumed by every device's coordinator so they act as one coordinated pool of
demand instead of ~12 independent, oblivious pollers.
"""
from __future__ import annotations

import asyncio
import math
from datetime import timedelta

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import DOMAIN, RESERVED_CONNECTION_SLOTS

_SEMAPHORE_KEY = "connect_semaphore"

# Fail-open fallback when the Bluetooth manager isn't available at all (isolated
# unit tests only; in production the integration depends on the bluetooth stack,
# same assumption `coordinator._sample_rssi` already makes). Large enough that it
# never triggers the "exhausted" skip or artificially throttles the interval.
_ASSUME_PLENTY_OF_SLOTS = 99


async def _async_raw_free_slots(hass: HomeAssistant) -> int:
    """Total free BLE connection slots right now, summed across every connectable
    scanner that reports allocation data (local adapters + ESPHome proxies alike);
    falls back to the coarser connectable scanner count if no scanner exposes
    allocations, INCLUDING on an HA version old enough that per-scanner allocation
    introspection (`async_current_scanners` / `BaseHaScanner.get_allocations`)
    doesn't exist at all yet — accessed via `getattr` rather than a hard import so
    this integration keeps working on older Home Assistant, just with the coarser
    signal. No reservation subtracted — see `async_usable_slots` /
    `async_slots_exhausted` for the two ways this gets used."""
    try:
        list_scanners = getattr(bluetooth, "async_current_scanners", None)
        if list_scanners is not None:
            free_total = 0
            saw_allocations = False
            for scanner in list_scanners(hass):
                alloc = scanner.get_allocations()
                if alloc is None:
                    continue
                saw_allocations = True
                free_total += alloc.free
            if saw_allocations:
                return free_total
        return bluetooth.async_scanner_count(hass, connectable=True)
    except RuntimeError:
        # Bluetooth manager not set up (isolated unit tests only).
        return _ASSUME_PLENTY_OF_SLOTS


async def async_usable_slots(hass: HomeAssistant) -> int:
    """How many BLE connection slots this integration may use right now, for sizing
    the connect semaphore and the dynamic poll interval. Reserves
    `RESERVED_CONNECTION_SLOTS` for a manual connection (e.g. the Govee app) or
    another integration, and never returns less than 1 so a lone device isn't
    starved outright — see `async_slots_exhausted` for the "truly no room, don't
    even try" check that this floor deliberately can't express."""
    return max(1, await _async_raw_free_slots(hass) - RESERVED_CONNECTION_SLOTS)


async def async_slots_exhausted(hass: HomeAssistant) -> bool:
    """True if taking one more slot would eat into (or exceed) the reserved
    headroom — i.e. attempting a connect right now would almost certainly fail
    with BleakOutOfConnectionSlotsError. Cheaper to skip this poll than to try,
    fail, and add to the contention every other device is also experiencing."""
    return await _async_raw_free_slots(hass) <= RESERVED_CONNECTION_SLOTS


def dynamic_interval(base: timedelta, device_count: int, usable_slots: int) -> timedelta:
    """Lengthen `base` proportionally when devices outnumber usable slots.

    With N devices and S usable slots, spacing polls `ceil(N / S)` base-intervals
    apart keeps average concurrent demand within capacity. Never shortens `base`
    (slot abundance doesn't mean poll faster; `base` is already the app's steady
    cadence) and both inputs are floored at 1 to avoid a division error.
    """
    device_count = max(1, device_count)
    usable_slots = max(1, usable_slots)
    if device_count <= usable_slots:
        return base
    return base * math.ceil(device_count / usable_slots)


def async_loaded_entry_count(hass: HomeAssistant) -> int:
    """How many Govee BLE Local config entries are currently loaded (i.e. actively
    polling) — the "device_count" side of the dynamic-interval calculation."""
    return len(
        [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.state is ConfigEntryState.LOADED
        ]
    )


async def async_connect_semaphore(hass: HomeAssistant) -> asyncio.Semaphore:
    """Get (or lazily create) the domain-wide semaphore capping how many devices
    may be simultaneously mid-connect. Sized once, from live slot data, at first
    use; an integration reload naturally re-sizes it to current conditions since
    `hass.data[DOMAIN]` is cleared then repopulated per entry lifecycle.

    A plain `asyncio.Semaphore` can't be safely resized after creation, so this is
    a coarse, session-lived cap — the live `async_usable_slots` check before each
    connect attempt (see coordinator.py) is what actually reacts to slot counts
    changing (e.g. a proxy coming back online) within one HA run.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    semaphore = domain_data.get(_SEMAPHORE_KEY)
    if not isinstance(semaphore, asyncio.Semaphore):
        size = await async_usable_slots(hass)
        semaphore = asyncio.Semaphore(size)
        domain_data[_SEMAPHORE_KEY] = semaphore
    return semaphore
