"""Tests for the slot-aware BLE connection scheduling helpers."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.const import DOMAIN, RESERVED_CONNECTION_SLOTS
from custom_components.govee_ble_local.scheduling import (
    async_connect_semaphore,
    async_loaded_entry_count,
    async_slots_exhausted,
    async_usable_slots,
    dynamic_interval,
)

_SCHED = "custom_components.govee_ble_local.scheduling"


def _scanner(free: int) -> SimpleNamespace:
    return SimpleNamespace(get_allocations=lambda: SimpleNamespace(free=free))


async def test_usable_slots_sums_allocations_across_scanners(hass: HomeAssistant) -> None:
    """Sums .free across every connectable scanner reporting allocations, minus
    the reserved headroom."""
    with patch(
        f"{_SCHED}.bluetooth.async_current_scanners",
        return_value=[_scanner(2), _scanner(3)],
        create=True,
    ):
        assert await async_usable_slots(hass) == 5 - RESERVED_CONNECTION_SLOTS


async def test_usable_slots_falls_back_to_scanner_count_without_allocations(
    hass: HomeAssistant,
) -> None:
    """When no scanner exposes allocation data (or async_current_scanners isn't
    available on this HA version), fall back to the coarser connectable
    scanner count."""
    with (
        patch(f"{_SCHED}.bluetooth.async_current_scanners", return_value=[], create=True),
        patch(f"{_SCHED}.bluetooth.async_scanner_count", return_value=4),
    ):
        assert await async_usable_slots(hass) == 4 - RESERVED_CONNECTION_SLOTS


async def test_usable_slots_never_below_one(hass: HomeAssistant) -> None:
    """A lone device is never fully starved, even if raw free slots are at or
    below the reserved headroom."""
    with patch(f"{_SCHED}.bluetooth.async_scanner_count", return_value=0):
        assert await async_usable_slots(hass) == 1


async def test_slots_exhausted_true_at_or_below_reserved(hass: HomeAssistant) -> None:
    """Exhausted as soon as raw free slots would eat into the reserved
    headroom - a stricter threshold than async_usable_slots' floor-at-1."""
    with patch(f"{_SCHED}.bluetooth.async_scanner_count", return_value=RESERVED_CONNECTION_SLOTS):
        assert await async_slots_exhausted(hass) is True
    with patch(f"{_SCHED}.bluetooth.async_scanner_count", return_value=RESERVED_CONNECTION_SLOTS + 1):
        assert await async_slots_exhausted(hass) is False


async def test_raw_free_slots_fails_open_without_bluetooth_manager(
    hass: HomeAssistant,
) -> None:
    """If the bluetooth manager isn't set up (isolated test scenario), assume
    plenty of room rather than blocking every poll."""

    def _raise(*_a: object, **_k: object) -> int:
        raise RuntimeError("BluetoothManager has not been set")

    with patch(f"{_SCHED}.bluetooth.async_scanner_count", side_effect=_raise):
        assert await async_slots_exhausted(hass) is False
        assert await async_usable_slots(hass) > 1


def test_dynamic_interval_unchanged_when_slots_suffice() -> None:
    base = timedelta(seconds=120)
    assert dynamic_interval(base, device_count=3, usable_slots=5) == base
    assert dynamic_interval(base, device_count=3, usable_slots=3) == base


def test_dynamic_interval_lengthens_proportionally() -> None:
    base = timedelta(seconds=120)
    # ceil(7 / 2) == 4
    assert dynamic_interval(base, device_count=7, usable_slots=2) == base * 4


def test_dynamic_interval_floors_inputs_at_one() -> None:
    base = timedelta(seconds=120)
    assert dynamic_interval(base, device_count=0, usable_slots=0) == base


async def test_loaded_entry_count_counts_only_loaded_entries(hass: HomeAssistant) -> None:
    """Only entries in the LOADED state count toward device_count."""
    from homeassistant.config_entries import ConfigEntryState

    loaded = MockConfigEntry(
        domain=DOMAIN,
        data={"address": "AA:BB:CC:DD:EE:01"},
        state=ConfigEntryState.LOADED,
    )
    not_loaded = MockConfigEntry(
        domain=DOMAIN,
        data={"address": "AA:BB:CC:DD:EE:02"},
        state=ConfigEntryState.SETUP_ERROR,
    )
    loaded.add_to_hass(hass)
    not_loaded.add_to_hass(hass)
    assert async_loaded_entry_count(hass) == 1


async def test_connect_semaphore_created_once_and_reused(hass: HomeAssistant) -> None:
    """The domain semaphore is created lazily on first use and the SAME
    instance is returned on subsequent calls (not re-created each time)."""
    with patch(f"{_SCHED}.bluetooth.async_scanner_count", return_value=5):
        first = await async_connect_semaphore(hass)
        second = await async_connect_semaphore(hass)
    assert first is second
    assert isinstance(first, asyncio.Semaphore)


async def test_connect_semaphore_sized_from_usable_slots(hass: HomeAssistant) -> None:
    """The semaphore's initial capacity reflects usable_slots at creation time."""
    with patch(f"{_SCHED}.bluetooth.async_scanner_count", return_value=3):
        semaphore = await async_connect_semaphore(hass)
    # usable_slots = 3 - RESERVED_CONNECTION_SLOTS; capacity == that many acquires
    # before a further acquire blocks.
    expected = max(1, 3 - RESERVED_CONNECTION_SLOTS)
    acquired = 0
    for _ in range(expected):
        assert semaphore.locked() is False or acquired < expected
        await semaphore.acquire()
        acquired += 1
    assert semaphore.locked()
