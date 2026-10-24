"""Tests for the connect gate (connect_gate.py), on Home Assistant's real Bluetooth stack.

The radios are real habluetooth scanners (two remote ones, the proxies, and a local
adapter) registered with Home Assistant's Bluetooth manager, so the paths the gate reads,
habluetooth's own path scoring, its connects in progress and its per-address failure
counts are all the real ones. Only a radio's slot report (``get_allocations``) is set by
the test, as a proxy's report (or BlueZ's slot manager) sets it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Iterator
import contextlib
from datetime import timedelta
import logging
from types import SimpleNamespace
from unittest.mock import patch

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak_retry_connector import Allocations
from bluetooth_data_tools import monotonic_time_coarse
from govee_ble_local import Device
from habluetooth import HaBleakClientWrapper, get_manager
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BaseHaRemoteScanner,
    BaseHaScanner,
    HaBluetoothConnector,
    api as bluetooth_api,
)
from homeassistant.components.light import DATA_COMPONENT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.govee_ble_local import connect_gate
from custom_components.govee_ble_local.connect_gate import (
    _GATE,
    Admission,
    ConnectGate,
    _Radio,
    async_connect_slot,
    async_link_up,
    async_note_deferred_off,
    async_note_releasing,
    async_release_waiters,
)
from custom_components.govee_ble_local.const import (
    CONNECT_GATE_AVOID_SECONDS,
    CONNECT_GATE_DEAF_SECONDS,
    CONNECT_GATE_MAX_WAIT_SECONDS,
    CONNECT_GATE_PATH_FAILURES,
    CONNECT_GATE_RELEASE_GRACE_SECONDS,
    CONNECT_GATE_RSSI_FLOOR,
    CONNECT_GATE_TICK_SECONDS,
    DOMAIN,
    MAX_CONCURRENT_POLLS,
)
from custom_components.govee_ble_local.coordinator import (
    GoveeBleLocalCoordinator,
    _poll_semaphore,
)

from .conftest import make_device
from .const import ADDRESS

ATHOM = "00:00:5E:00:53:10"
HCI0 = "00:00:5E:00:53:14"
BT_PROXY = "00:00:5E:00:53:11"

BULB = "5C:E7:53:00:00:01"
BULB_2 = "5C:E7:53:00:00:02"
BULB_3 = "5C:E7:53:00:00:03"
BULB_4 = "5C:E7:53:00:00:04"
NIGHTSTAND = "00:00:5E:00:53:12"
NIGHTSTAND_2 = "00:00:5E:00:53:13"

MAIN = "light.test_ceiling"


class _Slots:
    """A radio's slot report, set by the test, and habluetooth's connect bookkeeping.

    ``slots=None`` reports no slot count at all (``get_allocations`` returns None).
    """

    source: str
    slots: int | None
    free: int
    allocated: list[str]

    def get_allocations(self) -> Allocations | None:
        if self.slots is None:
            return None
        return Allocations(self.source, self.slots, self.free, list(self.allocated))

    def start_connect(self, address: str, *, reserve: bool = True) -> None:
        """habluetooth starts a connect through this radio; the radio reserves a slot."""
        self._add_connecting(address)  # type: ignore[attr-defined]
        if reserve:
            self.take(address)

    def finish_connect(self, address: str, *, connected: bool = True) -> None:
        """habluetooth's connect ends; a failed one gives the reserved slot back."""
        self._finished_connecting(address, connected)  # type: ignore[attr-defined]
        if not connected and address in self.allocated:
            self.give_back(address)

    def take(self, address: str) -> None:
        """Report ``address`` holding a slot (a proxy lists it from the reservation on)."""
        self.allocated.append(address)
        self.free -= 1

    def give_back(self, address: str) -> None:
        """Report ``address``'s slot free again."""
        self.allocated.remove(address)
        self.free += 1

    def go_deaf(self, seconds: float = CONNECT_GATE_DEAF_SECONDS) -> None:
        """Have heard nothing for ``seconds`` (as a proxy whose connects hang)."""
        self._last_detection = monotonic_time_coarse() - seconds


class Radio(_Slots, BaseHaRemoteScanner):
    """A proxy: a connectable remote scanner."""

    def __init__(self, source: str, name: str, slots: int | None) -> None:
        super().__init__(
            source,
            name,
            # As bleak-esphome's: a connect only while it reports a free slot.
            HaBluetoothConnector(client=object, source=source, can_connect=self._can_connect),  # type: ignore[arg-type]
            True,
        )
        self.slots = slots
        self.free = slots or 0
        self.allocated = []

    def _can_connect(self) -> bool:
        return self.free > 0

    def hear(self, address: str, rssi: int) -> None:
        """Hear an advertisement from ``address`` at ``rssi`` (no name: nothing discovers it)."""
        self._async_on_advertisement(
            address, rssi, None, [], {}, {}, None, {}, monotonic_time_coarse()
        )


class LocalRadio(_Slots, BaseHaScanner):
    """A local adapter (BlueZ): its slots come from bleak-retry-connector's slot manager."""

    def __init__(self, source: str, name: str, slots: int) -> None:
        super().__init__(source, name, connectable=True)
        self.slots = slots
        self.free = slots
        self.allocated = []
        self._heard: dict[str, tuple[BLEDevice, AdvertisementData]] = {}

    @property
    def discovered_devices(self) -> list[BLEDevice]:
        return [device for device, _ in self._heard.values()]

    @property
    def discovered_devices_and_advertisement_data(
        self,
    ) -> dict[str, tuple[BLEDevice, AdvertisementData]]:
        return dict(self._heard)

    @property
    def discovered_addresses(self) -> list[str]:
        return list(self._heard)

    def get_discovered_device_advertisement_data(
        self, address: str
    ) -> tuple[BLEDevice, AdvertisementData] | None:
        return self._heard.get(address)

    def hear(self, address: str, rssi: int) -> None:
        """Hear an advertisement from ``address`` at ``rssi``."""
        self._heard[address] = (
            BLEDevice(address, None, {}),
            AdvertisementData(None, {}, {}, [], None, rssi, ()),  # type: ignore[arg-type]
        )
        self._last_detection = monotonic_time_coarse()


@pytest.fixture
async def radios(hass: HomeAssistant, enable_bluetooth: None) -> AsyncGenerator[SimpleNamespace]:
    """Production's radios: a 3-slot ESPHome proxy, the local adapter, a software proxy."""
    made = SimpleNamespace(
        athom=Radio(ATHOM, "athom", 3),
        hci0=LocalRadio(HCI0, "hci0", 5),
        bt_proxy=Radio(BT_PROXY, "bt-proxy", 3),
    )
    cancels = []
    for radio in vars(made).values():
        cancels.append(radio.async_setup())
        cancels.append(bluetooth.async_register_scanner(hass, radio))
    # The real lookup, not conftest's stand-in.
    with patch(
        "homeassistant.components.bluetooth.async_scanner_devices_by_address",
        bluetooth_api.async_scanner_devices_by_address,
    ):
        yield made
    for cancel in cancels:
        cancel()


def name(radio: _Slots) -> str:
    """The radio's name as Home Assistant (and the gate's debug line) shows it."""
    return radio.name  # type: ignore[attr-defined, no-any-return]


class CallFailed(Exception):
    """The library call inside the gate's slot failed (``Call.finish(fail=True)``)."""


class Call:
    """A command (or poll) that takes the gate's slot and holds it until finished.

    ``last``: a turn-off of a device already off, served after the other commands.
    """

    def __init__(
        self, hass: HomeAssistant, address: str, *, poll: bool = False, last: bool = False
    ) -> None:
        self.address = address
        self.admission: Admission | None = None
        self.outcome: str | None = None
        self._finish = asyncio.Event()
        self._fail = False
        self.task = hass.async_create_background_task(
            self._run(hass, poll, last), f"gate call {address}", eager_start=True
        )

    async def _run(self, hass: HomeAssistant, poll: bool, last: bool) -> None:
        async with async_connect_slot(hass, self.address, poll=poll, last=last) as admission:
            self.admission = admission
            self.outcome = admission.outcome
            await self._finish.wait()
            if self._fail:
                raise CallFailed

    @property
    def admitted(self) -> bool:
        return self.admission is not None

    @property
    def radio(self) -> str | None:
        assert self.admission is not None
        return self.admission.radio_name

    async def finish(self, *, fail: bool = False) -> None:
        """End the call: done, or with ``fail`` failed (its library call raised in the slot)."""
        self._fail = fail
        self._finish.set()
        with contextlib.suppress(CallFailed):
            await self.task


@pytest.fixture
async def calls() -> AsyncGenerator[list[Call]]:
    """Calls a test starts; any it leaves holding the gate are cancelled at the end."""
    started: list[Call] = []
    yield started
    for call in started:
        call.task.cancel()
    await asyncio.gather(*(call.task for call in started), return_exceptions=True)


@pytest.fixture
def start(hass: HomeAssistant, calls: list[Call]) -> Callable[..., Call]:
    """Start a call to an address through the gate (eagerly: it queues at once)."""

    def _start(address: str, *, poll: bool = False, last: bool = False) -> Call:
        call = Call(hass, address, poll=poll, last=last)
        calls.append(call)
        return call

    return _start


async def settle() -> None:
    """Let admitted calls resume."""
    for _ in range(5):
        await asyncio.sleep(0)


async def tick(hass: HomeAssistant) -> None:
    """Let one tick of the gate's polling pass, and admitted calls resume."""
    fire_tick(hass)
    await settle()


def fire_tick(hass: HomeAssistant) -> None:
    """Run the gate's due tick now, before any task runs on."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=CONNECT_GATE_TICK_SECONDS))


def gate(hass: HomeAssistant) -> ConnectGate:
    return hass.data[_GATE]


# -- admission ----------------------------------------------------------------------


async def test_a_lone_command_goes_at_once_through_the_best_path(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    radios.athom.hear(BULB, -60)
    radios.hci0.hear(BULB, -75)
    call = start(BULB)
    assert call.admitted  # no wait, no loop iteration
    assert call.outcome == "admitted"
    assert call.radio == name(radios.athom)
    assert call.admission is not None
    assert call.admission.waited == 0
    await call.finish()
    assert not gate(hass)._tickets
    assert gate(hass)._tick is None


async def test_a_device_no_scanner_sees_goes_at_once(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """Nothing to judge it by: it goes, and takes no ticket."""
    radios.athom.hear(BULB, -60)
    first = start(BULB)
    unseen = start(BULB_2)
    assert unseen.outcome == "unseen"
    assert unseen.radio is None
    assert list(gate(hass)._tickets) == [BULB]
    await unseen.finish()
    await first.finish()


async def test_never_more_admitted_than_the_radio_has_effectively_free(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """A radio's reported free count, less our connects it doesn't list yet, bounds admission.

    The in-flight limit is lifted here so the free count alone decides; a proxy that hasn't
    reported a connect yet must not be handed another for the slot that connect will take.
    """
    radios.athom.take("11:22:33:44:55:66")  # something else holds one of the three slots
    devices = [BULB, BULB_2, BULB_3, BULB_4]
    with patch.object(connect_gate, "CONNECT_GATE_IN_FLIGHT_PER_PROXY", 10):
        for device in devices:
            radios.athom.hear(device, -60)
        calls = [start(device) for device in devices]
        assert [call.admitted for call in calls] == [True, True, False, False]

        # The proxy lists the first one; its free count drops with it. The second one
        # isn't listed yet: it still takes the last slot.
        radios.athom.start_connect(BULB)
        await tick(hass)
        assert [call.admitted for call in calls] == [True, True, False, False]

        # The first one is done: its slot is free again, and the third goes.
        radios.athom.finish_connect(BULB)
        await calls[0].finish()
        radios.athom.give_back(BULB)
        await tick(hass)
        assert [call.admitted for call in calls[1:]] == [True, True, False]
        for call in calls[1:3]:
            await call.finish()
        await tick(hass)
        assert calls[3].admitted
        await calls[3].finish()


async def test_a_radio_that_reports_no_free_slot_takes_none(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """Even with nothing of ours under way: a full proxy is never handed a connect."""
    for slot in range(3):
        radios.athom.take(f"11:22:33:44:55:{slot:02X}")
    radios.athom.hear(NIGHTSTAND, -70)
    call = start(NIGHTSTAND)
    await tick(hass)
    assert not call.admitted
    radios.athom.give_back("11:22:33:44:55:00")
    await tick(hass)
    assert call.outcome == "admitted"
    await call.finish()


async def test_one_connect_in_flight_per_radio(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """A proxy with free slots still gets one of our connects at a time, until it is up."""
    for device in (NIGHTSTAND, NIGHTSTAND_2):
        radios.athom.hear(device, -70)
    first, second = start(NIGHTSTAND), start(NIGHTSTAND_2)
    assert first.admitted
    assert not second.admitted

    radios.athom.start_connect(NIGHTSTAND)  # reserved, still connecting
    await tick(hass)
    assert not second.admitted
    radios.athom.finish_connect(NIGHTSTAND)  # the link is up; the command writes on
    await tick(hass)
    assert second.outcome == "admitted"
    await first.finish()
    await second.finish()


async def test_a_local_adapter_takes_connects_side_by_side(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """BlueZ runs pending connects side by side: only its free slots bound ours."""
    for slot in range(2):
        radios.hci0.take(f"11:22:33:44:55:{slot:02X}")
    devices = [BULB, BULB_2, BULB_3, BULB_4]
    for device in devices:
        radios.hci0.hear(device, -60)
    calls = [start(device) for device in devices]
    assert [call.admitted for call in calls] == [True, True, True, False]
    radios.hci0.start_connect(BULB)  # listed: no longer an unlisted ticket
    await tick(hass)
    assert not calls[3].admitted  # the slot it took is gone
    await calls[1].finish()
    await settle()
    assert calls[3].radio == name(radios.hci0)
    for call in calls:
        call.task.cancel()


async def test_a_proxy_counts_anyone_s_connect_in_progress(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """Another integration's connect through the proxy holds ours back as one of ours would."""
    radios.athom.hear(NIGHTSTAND, -70)
    radios.athom.start_connect("11:22:33:44:55:AA")
    call = start(NIGHTSTAND)
    assert not call.admitted
    radios.athom.finish_connect("11:22:33:44:55:AA")
    await tick(hass)
    assert call.outcome == "admitted"
    await call.finish()


async def test_a_failed_connect_keeps_its_radio_until_the_call_ends(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """A connect that failed may be retried within the same call: the radio stays ours."""
    for device in (NIGHTSTAND, NIGHTSTAND_2):
        radios.athom.hear(device, -70)
    first, second = start(NIGHTSTAND), start(NIGHTSTAND_2)
    radios.athom.start_connect(NIGHTSTAND)
    radios.athom.finish_connect(NIGHTSTAND, connected=False)
    await tick(hass)
    assert not second.admitted
    await first.finish()
    await settle()  # given back: the waiting call goes without waiting for the tick
    assert second.outcome == "admitted"
    await second.finish()


async def test_a_slot_freeing_is_seen_within_a_tick(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """Home Assistant signals no slot freeing: the gate re-reads the radios every tick."""
    for device in (NIGHTSTAND, NIGHTSTAND_2):
        radios.athom.hear(device, -70)
    first, second = start(NIGHTSTAND), start(NIGHTSTAND_2)
    radios.athom.start_connect(NIGHTSTAND)
    timer = gate(hass)._tick
    assert timer is not None
    assert timer.when() - hass.loop.time() == pytest.approx(CONNECT_GATE_TICK_SECONDS, abs=0.05)
    radios.athom.finish_connect(NIGHTSTAND)
    await tick(hass)
    assert second.admitted
    assert gate(hass)._tick is None  # nothing waits: no polling
    await first.finish()
    await second.finish()


async def test_a_connected_device_skips_the_queue(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """A device whose link is up needs no connect: it goes past waiting calls, ticketless."""
    for device in (NIGHTSTAND, NIGHTSTAND_2, BULB):
        radios.athom.hear(device, -70)
    radios.athom.take(BULB)  # the bulb's link from its last command is still up
    first, waiting = start(NIGHTSTAND), start(NIGHTSTAND_2)
    linked = start(BULB)
    assert not waiting.admitted
    assert linked.outcome == "linked"
    assert linked.radio == name(radios.athom)
    assert set(gate(hass)._tickets) == {NIGHTSTAND}
    await linked.finish()
    await first.finish()
    await waiting.finish()


async def test_a_second_call_to_a_connecting_device_shares_its_ticket(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """Calls to one device share its connect (the library runs them one after another)."""
    radios.athom.hear(NIGHTSTAND, -70)
    first, second = start(NIGHTSTAND), start(NIGHTSTAND)
    assert second.outcome == "joined"
    assert second.radio == name(radios.athom)
    ticket = gate(hass)._tickets[NIGHTSTAND]
    assert ticket.holders == 2
    await first.finish()
    assert gate(hass)._tickets[NIGHTSTAND] is ticket
    await second.finish()
    assert not gate(hass)._tickets


async def test_addresses_match_the_allocated_list_whatever_their_case(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    radios.athom.hear(BULB, -70)
    radios.athom.allocated.append(BULB.lower())
    call = start(BULB)
    assert call.outcome == "linked"
    await call.finish()


async def test_a_device_s_link_is_up_while_a_radio_lists_it(
    hass: HomeAssistant, radios: SimpleNamespace
) -> None:
    """What the deferral asks before a turn-off waits: then waiting saves no slot."""
    assert not async_link_up(hass, BULB)  # no radio hears it
    radios.athom.hear(BULB, -60)
    radios.hci0.hear(BULB, -70)
    radios.athom.take("11:22:33:44:55:00")  # another device's link
    assert not async_link_up(hass, BULB)
    radios.hci0.take(BULB.lower())  # through its other path, whatever the case
    assert async_link_up(hass, BULB)
    radios.hci0.give_back(BULB.lower())  # the link dropped
    assert not async_link_up(hass, BULB)
    radios.athom.start_connect(BULB)  # reserved: coming up
    assert async_link_up(hass, BULB)


# -- paths --------------------------------------------------------------------------


def _busy_athom(radios: SimpleNamespace) -> None:
    radios.athom.start_connect("11:22:33:44:55:AA")


def _full_athom(radios: SimpleNamespace) -> None:
    for slot in range(3):
        radios.athom.take(f"11:22:33:44:55:{slot:02X}")


def _last_slot_athom(radios: SimpleNamespace) -> None:
    for slot in range(2):
        radios.athom.take(f"11:22:33:44:55:{slot:02X}")


def _failing_athom(radios: SimpleNamespace) -> None:
    for _ in range(2):
        radios.athom.start_connect(BULB, reserve=False)
        radios.athom.finish_connect(BULB, connected=False)


def _busy_hci0(radios: SimpleNamespace) -> None:
    radios.hci0.start_connect("11:22:33:44:55:AA")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (None, "athom"),
        (_busy_athom, "hci0"),
        (_full_athom, "hci0"),
        (_last_slot_athom, "athom"),
        (_failing_athom, "hci0"),
        (_busy_hci0, "athom"),
    ],
    ids=["idle", "athom-connecting", "athom-full", "athom-last-slot", "athom-failing", "hci0-busy"],
)
async def test_the_prediction_is_habluetooth_s_own_pick(
    hass: HomeAssistant,
    radios: SimpleNamespace,
    state: Callable[[SimpleNamespace], None] | None,
    expected: str,
) -> None:
    """The gate's predicted path is the one habluetooth's client wrapper picks to connect."""
    radios.athom.hear(BULB, -60)
    radios.hci0.hear(BULB, -75)
    radios.bt_proxy.hear(BULB, -80)
    if state is not None:
        state(radios)
    paths = bluetooth.async_scanner_devices_by_address(hass, BULB, True)
    predicted, _score = ConnectGate(hass)._predict(paths, {})
    picked = HaBleakClientWrapper(
        BLEDevice(BULB, None, {})
    )._async_get_best_available_backend_and_device(get_manager())
    assert predicted is not None
    assert predicted.scanner is picked.scanner is getattr(radios, expected)


async def test_a_weaker_usable_path_is_taken_rather_than_waiting(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """An H6006 bulb is fine through the local adapter: it doesn't wait for the busy proxy.

    habluetooth sees the proxy's connect in progress and scores the adapter higher,
    so that is where the bulb's connect goes, at once.
    """
    radios.athom.hear(NIGHTSTAND, -70)
    radios.athom.hear(NIGHTSTAND_2, -70)
    radios.athom.hear(BULB, -60)
    radios.hci0.hear(BULB, -75)
    nightstand = start(NIGHTSTAND)
    radios.athom.start_connect(NIGHTSTAND)
    waiting = start(NIGHTSTAND_2)  # a command waiting for the proxy holds up no other command
    bulb = start(BULB)
    assert not waiting.admitted
    assert bulb.outcome == "admitted"
    assert bulb.radio == name(radios.hci0)
    assert bulb.admission is not None
    assert bulb.admission.waited == 0
    for call in (nightstand, waiting, bulb):
        call.task.cancel()


async def test_a_device_waits_while_habluetooth_would_still_pick_the_busy_radio(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """Our ticket on the proxy isn't habluetooth's connect yet: it would send the bulb there
    too. Once it sees that connect, it would pick the adapter, and the bulb goes."""
    radios.athom.hear(BULB, -60)
    radios.hci0.hear(BULB, -75)
    radios.athom.hear(BULB_2, -60)
    radios.hci0.hear(BULB_2, -75)
    first, second = start(BULB), start(BULB_2)
    assert first.radio == name(radios.athom)
    assert not second.admitted
    radios.athom.start_connect(BULB)
    await tick(hass)
    assert second.radio == name(radios.hci0)
    await first.finish()
    await second.finish()


async def test_a_device_with_no_usable_alternative_waits_for_its_radio(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """A nightstand at -96 dBm on the adapter waits for the full proxy, not the adapter.

    That is production's failure: refused by the proxy, the connect went down the
    hopeless path and waited out a 20 s timeout there.
    """
    for slot in range(3):
        radios.athom.take(f"11:22:33:44:55:{slot:02X}")
    radios.athom.hear(NIGHTSTAND, -70)
    radios.hci0.hear(NIGHTSTAND, -96)
    call = start(NIGHTSTAND)
    await tick(hass)
    assert not call.admitted
    radios.athom.give_back("11:22:33:44:55:00")
    await tick(hass)
    assert call.radio == name(radios.athom)
    await call.finish()


@pytest.mark.parametrize("rssi", [CONNECT_GATE_RSSI_FLOOR, CONNECT_GATE_RSSI_FLOOR - 1])
async def test_the_rssi_floor(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call], rssi: int
) -> None:
    """A path at the floor is usable; one below it isn't."""
    for slot in range(3):
        radios.athom.take(f"11:22:33:44:55:{slot:02X}")
    radios.athom.hear(NIGHTSTAND, -60)
    radios.hci0.hear(NIGHTSTAND, rssi)
    call = start(NIGHTSTAND)
    assert call.admitted is (rssi >= CONNECT_GATE_RSSI_FLOOR)
    call.task.cancel()


async def test_a_path_that_keeps_failing_is_not_usable(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """CONNECT_GATE_PATH_FAILURES failed connects through a radio since one worked: the device
    waits for its other path instead."""
    for slot in range(5):
        radios.hci0.take(f"11:22:33:44:55:{slot:02X}")
    radios.athom.hear(BULB, -60)
    radios.hci0.hear(BULB, -70)
    for _ in range(CONNECT_GATE_PATH_FAILURES - 1):
        radios.athom.start_connect(BULB, reserve=False)
        radios.athom.finish_connect(BULB, connected=False)
    call = start(BULB)
    assert call.radio == name(radios.athom)  # still usable
    await call.finish()
    radios.athom.start_connect(BULB, reserve=False)
    radios.athom.finish_connect(BULB, connected=False)
    call = start(BULB)
    await tick(hass)
    assert not call.admitted
    radios.hci0.give_back("11:22:33:44:55:00")
    radios.hci0.give_back("11:22:33:44:55:01")
    await tick(hass)
    assert call.radio == name(radios.hci0)
    await call.finish()


async def test_a_failing_path_habluetooth_still_prefers_is_not_waited_out(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """Its usable path is free but scores lower (its last slot): waiting would change nothing,
    so the device goes down the path habluetooth picks, as it would without the gate."""
    for slot in range(4):
        radios.hci0.take(f"11:22:33:44:55:{slot:02X}")
    radios.athom.hear(BULB, -60)
    radios.hci0.hear(BULB, -70)
    for _ in range(CONNECT_GATE_PATH_FAILURES):
        radios.athom.start_connect(BULB, reserve=False)
        radios.athom.finish_connect(BULB, connected=False)
    call = start(BULB)
    assert call.outcome == "admitted"
    assert call.radio == name(radios.athom)
    await call.finish()


async def test_a_weak_path_is_waited_out_while_a_connect_runs_on_the_usable_one(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """Someone else's connect on the proxy makes habluetooth pick the -96 dBm adapter: the
    nightstand waits until that connect is done, then goes through the proxy."""
    radios.athom.hear(NIGHTSTAND, -70)
    radios.hci0.hear(NIGHTSTAND, -96)
    radios.athom.start_connect("11:22:33:44:55:AA")
    call = start(NIGHTSTAND)
    assert not call.admitted
    radios.athom.finish_connect("11:22:33:44:55:AA")
    await tick(hass)
    assert call.radio == name(radios.athom)
    await call.finish()


async def test_a_device_with_no_usable_path_takes_the_best_one(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    radios.hci0.hear(NIGHTSTAND, -96)
    radios.bt_proxy.hear(NIGHTSTAND, -92)
    call = start(NIGHTSTAND)
    assert call.radio == name(radios.bt_proxy)
    await call.finish()


async def test_a_ticket_follows_the_device_to_the_radio_it_turned_up_on(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """habluetooth chose again inside the connect: the predicted radio is free for others."""
    radios.athom.hear(BULB, -60)
    radios.hci0.hear(BULB, -75)
    radios.athom.hear(NIGHTSTAND, -70)
    bulb = start(BULB)
    nightstand = start(NIGHTSTAND)
    assert bulb.radio == name(radios.athom)
    assert not nightstand.admitted
    radios.hci0.start_connect(BULB)
    await tick(hass)
    assert nightstand.radio == name(radios.athom)
    ticket = gate(hass)._tickets[BULB]
    assert ticket.scanner is radios.hci0
    await bulb.finish()
    await nightstand.finish()


async def test_a_link_lost_and_reconnecting_holds_its_radio_again(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """The library reconnects within the call: that connect is under way like the first.

    BULB, waiting for a full proxy, only keeps the gate re-reading the radios.
    """
    for slot in range(3):
        radios.bt_proxy.take(f"11:22:33:44:55:{slot:02X}")
    radios.bt_proxy.hear(BULB, -60)
    radios.athom.hear(NIGHTSTAND, -70)
    nightstand = start(NIGHTSTAND)
    waiting = start(BULB)
    ticket = gate(hass)._tickets[NIGHTSTAND]
    radios.athom.start_connect(NIGHTSTAND)
    radios.athom.finish_connect(NIGHTSTAND)
    await tick(hass)
    assert ticket.connected
    radios.athom.give_back(NIGHTSTAND)  # the link dropped
    await tick(hass)
    assert not ticket.connected
    radios.athom.start_connect(NIGHTSTAND)  # the library connects again
    await tick(hass)
    assert not ticket.connected
    radios.athom.finish_connect(NIGHTSTAND)
    await tick(hass)
    assert ticket.connected
    waiting.task.cancel()
    await nightstand.finish()


@pytest.mark.parametrize("slots", [None, 0])
async def test_a_radio_without_a_slot_report_still_takes_one_connect_at_a_time(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call], slots: int | None
) -> None:
    """No report, or a count of 0 (Home Assistant's "none"): no free count to go by, and no
    allocated list to see a connect come up in, so our connect counts until its call ends."""
    radios.bt_proxy.slots = slots
    radios.bt_proxy.free = 0
    radios.bt_proxy.hear(BULB, -60)
    radios.bt_proxy.hear(BULB_2, -60)
    first, second = start(BULB), start(BULB_2)
    assert first.radio == name(radios.bt_proxy)
    radios.bt_proxy.start_connect(BULB, reserve=False)
    radios.bt_proxy.finish_connect(BULB)
    await tick(hass)
    assert not second.admitted
    await first.finish()
    await settle()
    assert second.radio == name(radios.bt_proxy)
    await second.finish()


# -- deaf radios ------------------------------------------------------------------------


async def judge_a_later_call(start: Callable[..., Call]) -> None:
    """Have the gate judge (and end) a call to the nightstand, heard only through the Athom.

    Its pass reads only the Athom: any other radio it finds stuck, a release check found.
    """
    call = start(NIGHTSTAND)
    assert call.outcome == "admitted"
    await call.finish()


@contextlib.contextmanager
def counting_reads(radio: BaseHaScanner) -> Iterator[SimpleNamespace]:
    """Count the gate's reads of ``radio`` (a pass reads each scanner once)."""
    reads = SimpleNamespace(count=0)
    read = ConnectGate._radio

    def _counted(scanner: BaseHaScanner, radios: dict[str, _Radio]) -> _Radio:
        if scanner is radio and scanner.source not in radios:
            reads.count += 1
        return read(scanner, radios)

    with patch.object(ConnectGate, "_radio", staticmethod(_counted)):
        yield reads


async def test_a_deaf_radio_does_not_draw_the_overflow(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """The measured burst: the H6052's best path (the Athom proxy) is busy, so habluetooth
    would send it to bt-proxy, which reports every slot free but has heard nothing for a
    while (its connects hang). It waits the second or two for the Athom instead."""
    radios.athom.hear(NIGHTSTAND, -60)
    radios.athom.hear(BULB, -47)
    radios.bt_proxy.hear(BULB, -74)
    radios.hci0.hear(BULB, -78)
    nightstand = start(NIGHTSTAND)
    radios.athom.start_connect(NIGHTSTAND)
    radios.bt_proxy.go_deaf()
    bulb = start(BULB)
    assert not bulb.admitted
    assert ATHOM not in gate(hass)._avoided
    assert BT_PROXY in gate(hass)._avoided
    radios.athom.finish_connect(NIGHTSTAND)
    await tick(hass)
    assert bulb.radio == name(radios.athom)
    for call in (nightstand, bulb):
        await call.finish()


async def test_a_radio_that_hears_again_stays_avoided_for_a_while(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A proxy's hearing comes back before its connects work again: it is avoided for
    CONNECT_GATE_AVOID_SECONDS (logged once), then usable again."""
    radios.athom.hear(NIGHTSTAND, -60)
    for device in (BULB, BULB_2):
        radios.athom.hear(device, -47)
        radios.bt_proxy.hear(device, -74)
    holder = start(NIGHTSTAND)
    radios.athom.start_connect(NIGHTSTAND, reserve=False)  # hangs: the Athom stays busy
    radios.bt_proxy.go_deaf(15)
    with caplog.at_level(logging.DEBUG, logger=connect_gate.__name__):
        first = start(BULB)
        await tick(hass)
        await tick(hass)
    assert caplog.text.count(f"{name(radios.bt_proxy)} has heard nothing for 15 s") == 1
    assert not first.admitted
    first.task.cancel()
    radios.bt_proxy.hear(BULB_2, -74)  # hearing again
    monotonic.now += CONNECT_GATE_AVOID_SECONDS - 1
    second = start(BULB_2)
    assert not second.admitted
    monotonic.now += 1
    await tick(hass)
    assert second.radio == name(radios.bt_proxy)
    for call in (holder, second):
        call.task.cancel()


async def test_a_connect_coming_up_through_an_avoided_radio_ends_its_avoidance(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """A pass seeing the device come up through the avoided proxy ends its avoidance, and
    the call failing later (its link gone by then) doesn't bring it back: it came up there."""
    radios.bt_proxy.hear(BULB, -60)
    radios.athom.hear(NIGHTSTAND, -60)
    radios.athom.hear(NIGHTSTAND_2, -60)
    holder = start(NIGHTSTAND)
    waiting = start(NIGHTSTAND_2)  # keeps the gate re-reading the radios
    radios.bt_proxy.go_deaf()
    bulb = start(BULB)  # no usable path: it goes through the avoided proxy all the same
    assert BT_PROXY in gate(hass)._avoided
    assert bulb.radio == name(radios.bt_proxy)
    radios.bt_proxy.start_connect(BULB)  # still deaf throughout
    await tick(hass)
    assert BT_PROXY in gate(hass)._avoided  # reserved, not up yet
    radios.bt_proxy.finish_connect(BULB)
    await tick(hass)
    assert BT_PROXY not in gate(hass)._avoided
    radios.bt_proxy.give_back(BULB)  # the link drops again: no longer listed
    await bulb.finish(fail=True)
    monotonic.now += CONNECT_GATE_RELEASE_GRACE_SECONDS
    await tick(hass)  # the waiting call's pass runs the due check
    assert BT_PROXY not in gate(hass)._avoided  # it came up there: not avoided as it ends
    for call in (holder, waiting):
        call.task.cancel()


async def test_a_device_goes_to_an_avoided_radio_when_waiting_cannot_help(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """The H61A8: bt-proxy is its best path by far; its usable alternative, the adapter, is
    busy but heard worse, so habluetooth would pick bt-proxy even once the adapter is free.
    Holding it would only add the wait: it goes, as it would without the gate."""
    radios.bt_proxy.hear(BULB, -55)
    radios.hci0.hear(BULB, -76)
    radios.hci0.start_connect("11:22:33:44:55:AA")
    radios.bt_proxy.go_deaf()
    bulb = start(BULB)
    assert BT_PROXY in gate(hass)._avoided
    assert bulb.radio == name(radios.bt_proxy)
    await bulb.finish()


@pytest.mark.parametrize(
    "case",
    [(True, True, False), (True, False, True), (False, False, True), (False, True, True)],
    ids=["connecting-normally", "connecting-with-no-slot", "idle", "idle-with-a-link"],
)
async def test_a_deaf_radio_is_avoided_only_when_not_connecting_normally(
    hass: HomeAssistant,
    radios: SimpleNamespace,
    start: Callable[..., Call],
    case: tuple[bool, bool, bool],
) -> None:
    """An ESPHome proxy stops scanning for a slow connect, but lists the slot it reserved:
    that one isn't stuck. Deaf with no slot listed for its connect, or deaf and idle, is."""
    in_progress, listed, avoided = case
    radios.athom.hear(NIGHTSTAND, -60)
    if in_progress:
        radios.athom.start_connect("11:22:33:44:55:AA", reserve=listed)
    elif listed:
        radios.athom.take("11:22:33:44:55:AA")  # a link from earlier
    radios.athom.go_deaf()
    call = start(NIGHTSTAND)
    assert (ATHOM in gate(hass)._avoided) is avoided
    call.task.cancel()


async def test_a_reconnected_proxy_is_not_avoided_for_its_old_scanner(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """A proxy that reconnects registers a new scanner: the old one's silence, read once a
    call on it has failed, says nothing about the new one."""
    radios.athom.hear(NIGHTSTAND, -70)
    source = "D8:BC:38:00:00:01"
    old = Radio(source, "proxy", 3)
    unsetup, unregister = old.async_setup(), bluetooth.async_register_scanner(hass, old)
    old.hear(BULB, -60)
    call = start(BULB)
    assert call.radio == name(old)
    unregister()
    unsetup()
    new = Radio(source, "proxy", 3)
    unsetup, unregister = new.async_setup(), bluetooth.async_register_scanner(hass, new)
    new.hear(BULB, -60)
    old.go_deaf()
    await call.finish(fail=True)
    monotonic.now += CONNECT_GATE_RELEASE_GRACE_SECONDS
    await judge_a_later_call(start)
    assert source not in gate(hass)._avoided
    unregister()
    unsetup()


async def test_a_device_waits_for_a_local_adapter_mid_connect_rather_than_an_avoided_proxy(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """The adapter has room, but someone's connect through it makes habluetooth pick the
    avoided proxy for now: the device waits for that connect to end."""
    radios.hci0.hear(BULB, -60)
    radios.bt_proxy.hear(BULB, -70)
    radios.hci0.start_connect("11:22:33:44:55:AA")
    radios.bt_proxy.go_deaf()
    bulb = start(BULB)
    assert not bulb.admitted
    radios.hci0.finish_connect("11:22:33:44:55:AA")
    await tick(hass)
    assert bulb.radio == name(radios.hci0)
    await bulb.finish()


async def test_a_busy_path_heard_only_as_well_as_the_pick_scores_is_not_waited_for(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """At a tie habluetooth needn't prefer the busy path once it's free: no wait."""
    for slot in range(5):
        radios.hci0.take(f"11:22:33:44:55:{slot:02X}")
    radios.bt_proxy.hear(BULB, -70)
    radios.hci0.hear(BULB, -70)
    radios.bt_proxy.go_deaf()
    bulb = start(BULB)
    assert bulb.radio == name(radios.bt_proxy)
    await bulb.finish()


async def test_a_usable_path_whose_last_slot_our_ticket_takes_counts_as_busy(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """The Athom's last free slot is the nightstand's, whose connect hasn't started: habluetooth
    still sees a free slot there, but the bulb would only get it once that one is listed."""
    radios.athom.take("11:22:33:44:55:00")
    radios.athom.take("11:22:33:44:55:01")
    radios.athom.hear(NIGHTSTAND, -60)
    radios.athom.hear(BULB, -51)
    radios.bt_proxy.hear(BULB, -50)
    for _ in range(CONNECT_GATE_PATH_FAILURES - 1):
        radios.bt_proxy.start_connect(BULB, reserve=False)
        radios.bt_proxy.finish_connect(BULB, connected=False)
    radios.bt_proxy.go_deaf()
    nightstand = start(NIGHTSTAND)
    assert nightstand.radio == name(radios.athom)
    bulb = start(BULB)
    assert not bulb.admitted
    for call in (nightstand, bulb):
        call.task.cancel()


async def test_an_avoidance_is_extended_while_the_radio_stays_stuck(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    radios.bt_proxy.hear(BULB, -60)
    radios.bt_proxy.go_deaf()
    call = start(BULB)
    first = gate(hass)._avoided[BT_PROXY]
    await call.finish()
    monotonic.now += CONNECT_GATE_AVOID_SECONDS - 1
    call = start(BULB)
    assert gate(hass)._avoided[BT_PROXY] == first + CONNECT_GATE_AVOID_SECONDS - 1
    await call.finish()


async def test_a_radio_deaf_after_a_lone_call_failed_is_avoided_once_the_grace_is_over(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """No other call waited, so no pass read the proxy while the call ran. Its radio is
    checked by the first call judged CONNECT_GATE_RELEASE_GRACE_SECONDS after it failed, not
    before: a proxy that just gave up a slow connect may not have heard anything yet."""
    radios.bt_proxy.hear(BULB, -60)
    radios.athom.hear(NIGHTSTAND, -70)
    call = start(BULB)
    assert call.radio == name(radios.bt_proxy)
    radios.bt_proxy.go_deaf()
    await call.finish(fail=True)
    assert BT_PROXY not in gate(hass)._avoided
    assert gate(hass)._tick is None  # nothing waits: no timer is armed for the check
    monotonic.now += CONNECT_GATE_RELEASE_GRACE_SECONDS - 1
    await judge_a_later_call(start)
    assert BT_PROXY not in gate(hass)._avoided
    monotonic.now += 1
    await judge_a_later_call(start)
    assert BT_PROXY in gate(hass)._avoided


async def test_a_lone_call_that_succeeds_never_gets_its_deaf_radio_avoided(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """Its connect worked: the proxy reading deaf as the call ends says nothing of it."""
    radios.bt_proxy.hear(BULB, -60)
    radios.athom.hear(NIGHTSTAND, -70)
    call = start(BULB)
    radios.bt_proxy.go_deaf()
    await call.finish()
    monotonic.now += CONNECT_GATE_RELEASE_GRACE_SECONDS
    await judge_a_later_call(start)
    assert BT_PROXY not in gate(hass)._avoided


async def test_a_lone_failed_call_s_radio_that_hears_again_within_the_grace_is_not_avoided(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """An ESPHome proxy pauses scanning while it connects: deaf as the call failed, it is
    scanning again by the time it is checked, so it isn't stuck."""
    radios.bt_proxy.hear(BULB, -60)
    radios.athom.hear(NIGHTSTAND, -70)
    call = start(BULB)
    radios.bt_proxy.go_deaf()
    await call.finish(fail=True)
    monotonic.now += CONNECT_GATE_RELEASE_GRACE_SECONDS - 1
    radios.bt_proxy.hear(BULB, -60)  # scanning again
    monotonic.now += 1
    await judge_a_later_call(start)
    assert BT_PROXY not in gate(hass)._avoided


async def test_a_lone_failed_call_whose_device_came_up_on_its_deaf_radio_does_not_avoid_it(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """The bulb is listed on the deaf proxy with no connect in progress: its connect came up
    there (the call failed after), so connects through the proxy work. No pass saw it up."""
    radios.bt_proxy.hear(BULB, -60)
    radios.athom.hear(NIGHTSTAND, -70)
    call = start(BULB)
    radios.bt_proxy.start_connect(BULB)
    radios.bt_proxy.finish_connect(BULB)  # the link is up
    radios.bt_proxy.go_deaf()
    await call.finish(fail=True)
    monotonic.now += CONNECT_GATE_RELEASE_GRACE_SECONDS
    await judge_a_later_call(start)
    assert BT_PROXY not in gate(hass)._avoided


@pytest.mark.parametrize("failed", [False, True], ids=["done", "failed"])
async def test_no_pass_judges_a_radio_stuck_within_the_grace_after_a_call_on_it_ends(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
    failed: bool,
) -> None:
    """In a burst: the proxy paused scanning for the bulb's slow connect, and calls heard
    only through it wait. Neither the pass the bulb's call ending runs nor the ticks within
    CONNECT_GATE_RELEASE_GRACE_SECONDS judge it stuck, though each reads it; the first pass
    after that, with the proxy still deaf, does."""
    for device in (BULB, BULB_2, BULB_3):
        radios.bt_proxy.hear(device, -60)
    bulb = start(BULB)
    assert bulb.radio == name(radios.bt_proxy)
    later, last = start(BULB_2), start(BULB_3)
    radios.bt_proxy.start_connect(BULB)
    radios.bt_proxy.go_deaf(12)
    await tick(hass)
    assert BT_PROXY not in gate(hass)._avoided  # connecting normally
    radios.bt_proxy.finish_connect(BULB, connected=not failed)
    with counting_reads(radios.bt_proxy) as reads:
        await bulb.finish(fail=failed)
        assert reads.count == 1  # the pass its release ran (it admitted the next call)
        assert later.radio == name(radios.bt_proxy)
        assert BT_PROXY not in gate(hass)._avoided
        monotonic.now += CONNECT_GATE_RELEASE_GRACE_SECONDS - 1
        await tick(hass)
        assert reads.count == 2
        assert BT_PROXY not in gate(hass)._avoided
        monotonic.now += 1
        await tick(hass)
        assert reads.count > 2
    assert BT_PROXY in gate(hass)._avoided
    assert not last.admitted
    for call in (later, last):
        call.task.cancel()


async def test_a_radio_s_release_grace_is_forgotten_once_over(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """The first pass after it ends drops it, though that pass doesn't read the radio."""
    radios.hci0.hear(BULB, -60)
    radios.athom.hear(NIGHTSTAND, -70)
    call = start(BULB)
    await call.finish()
    assert gate(hass)._released == {HCI0: monotonic.now + CONNECT_GATE_RELEASE_GRACE_SECONDS}
    monotonic.now += CONNECT_GATE_RELEASE_GRACE_SECONDS - 1
    await judge_a_later_call(start)
    assert HCI0 in gate(hass)._released
    monotonic.now += 1
    await judge_a_later_call(start)
    assert gate(hass)._released.keys() == {ATHOM}  # the later call's own


async def test_a_capped_connect_holds_its_radio_while_a_connect_is_in_progress_there(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """Past the cap a ticket stops counting, but not while its radio is still connecting:
    a second connect would join the hung one."""
    for device in (NIGHTSTAND, NIGHTSTAND_2):
        radios.athom.hear(device, -70)
    hung = start(NIGHTSTAND)
    radios.athom.start_connect(NIGHTSTAND, reserve=False)
    monotonic.now += CONNECT_GATE_MAX_WAIT_SECONDS
    waiting = start(NIGHTSTAND_2)
    await tick(hass)
    assert not waiting.admitted
    radios.athom.finish_connect(NIGHTSTAND, connected=False)
    await tick(hass)
    assert waiting.outcome == "admitted"
    for call in (hung, waiting):
        await call.finish()


# -- serving order --------------------------------------------------------------------


async def test_single_path_devices_go_first(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """When the proxy frees, the nightstand (no other usable path) gets it before the bulb
    that queued earlier but can also use the adapter."""
    for device in (BULB, BULB_2, NIGHTSTAND):
        radios.athom.hear(device, -60 if device != NIGHTSTAND else -70)
    radios.hci0.hear(BULB, -70)
    radios.hci0.hear(BULB_2, -70)
    radios.hci0.hear(NIGHTSTAND, -96)
    holder = start(BULB_2)  # takes the proxy
    radios.athom.start_connect(BULB_2)
    for slot in range(5):
        radios.hci0.take(f"11:22:33:44:55:{slot:02X}")  # the adapter is full
    bulb = start(BULB)
    nightstand = start(NIGHTSTAND)
    await tick(hass)
    assert not bulb.admitted
    assert not nightstand.admitted

    radios.athom.finish_connect(BULB_2)
    await holder.finish()
    await settle()
    assert nightstand.radio == name(radios.athom)
    assert not bulb.admitted
    await nightstand.finish()
    await settle()
    assert bulb.radio == name(radios.athom)
    await bulb.finish()


async def test_polls_yield_to_commands(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """A poll that queued first still goes after a command for the same radio."""
    for device in (NIGHTSTAND, NIGHTSTAND_2, BULB):
        radios.athom.hear(device, -70)
    holder = start(BULB)
    poll = start(NIGHTSTAND, poll=True)
    command = start(NIGHTSTAND_2)
    radios.athom.start_connect(BULB)
    radios.athom.finish_connect(BULB)
    await tick(hass)
    assert command.outcome == "admitted"
    assert not poll.admitted
    radios.athom.start_connect(NIGHTSTAND_2)
    radios.athom.finish_connect(NIGHTSTAND_2)
    await tick(hass)
    assert poll.outcome == "admitted"
    for call in (holder, poll, command):
        await call.finish()


async def test_a_poll_needing_a_connect_waits_while_any_command_does(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """Even on another radio: a burst of commands has the radios first."""
    radios.athom.hear(NIGHTSTAND, -70)
    radios.athom.hear(NIGHTSTAND_2, -70)
    radios.hci0.hear(BULB, -60)
    radios.hci0.take(BULB_2)
    radios.hci0.hear(BULB_2, -60)
    holder = start(NIGHTSTAND)
    command = start(NIGHTSTAND_2)
    poll = start(BULB, poll=True)
    linked_poll = start(BULB_2, poll=True)
    assert not command.admitted
    assert not poll.admitted
    assert linked_poll.outcome == "linked"  # it needs no connect
    radios.athom.start_connect(NIGHTSTAND)
    radios.athom.finish_connect(NIGHTSTAND)
    await tick(hass)
    assert command.admitted
    assert poll.radio == name(radios.hci0)
    for call in (holder, command, poll, linked_poll):
        await call.finish()


async def test_a_command_with_a_choice_still_goes_before_a_single_path_poll(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """Single-path first applies among commands and among polls, not across them."""
    for slot in range(5):
        radios.hci0.take(f"11:22:33:44:55:{slot:02X}")
    radios.athom.hear(NIGHTSTAND, -70)
    radios.athom.hear(NIGHTSTAND_2, -70)
    radios.athom.hear(BULB, -60)
    radios.hci0.hear(BULB, -70)  # usable, but full
    holder = start(NIGHTSTAND_2)
    poll = start(NIGHTSTAND, poll=True)
    command = start(BULB)
    radios.athom.start_connect(NIGHTSTAND_2)
    radios.athom.finish_connect(NIGHTSTAND_2)
    await tick(hass)
    assert command.radio == name(radios.athom)
    assert not poll.admitted
    for call in (holder, poll, command):
        call.task.cancel()


async def test_a_waiting_poll_holds_up_no_other_poll(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    radios.athom.hear(NIGHTSTAND, -70)
    radios.athom.hear(NIGHTSTAND_2, -70)
    radios.hci0.hear(BULB, -60)
    holder = start(NIGHTSTAND)
    waiting = start(NIGHTSTAND_2, poll=True)
    other = start(BULB, poll=True)
    assert not waiting.admitted
    assert other.radio == name(radios.hci0)
    for call in (holder, waiting, other):
        call.task.cancel()


async def test_a_poll_needing_a_connect_waits_while_a_turn_off_is_deferred(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """A turn-off waiting in the deferral is a command to come: a poll waits for it as it
    would for a command at the gate, though its own radio is free."""
    radios.hci0.hear(BULB, -60)
    async_note_deferred_off(hass)
    poll = start(BULB, poll=True)
    await tick(hass)
    assert not poll.admitted
    poll.task.cancel()


async def test_a_poll_held_for_a_deferred_turn_off_goes_at_the_tick_after_it_ends(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """Its end judges nothing at once (the turn-off it ends queues right after it, and is
    served first): the next tick judges the poll held for it."""
    radios.hci0.hear(BULB, -60)
    end = async_note_deferred_off(hass)
    poll = start(BULB, poll=True)
    assert not poll.admitted
    end()
    await settle()
    assert not poll.admitted
    await tick(hass)
    assert poll.outcome == "admitted"
    assert poll.radio == name(radios.hci0)
    await poll.finish()


async def test_a_deferred_turn_off_sent_as_it_ends_goes_before_the_poll_held_for_it(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """The deferral ends the turn-off's count and queues it in the same step: the turn-off,
    a command, takes the proxy's one connect in flight, not the poll queued before it."""
    for device in (NIGHTSTAND, NIGHTSTAND_2):
        radios.athom.hear(device, -70)
    end = async_note_deferred_off(hass)
    poll = start(NIGHTSTAND, poll=True)
    assert not poll.admitted  # the Athom has room: held for the turn-off alone
    end()
    off = start(NIGHTSTAND_2, last=True)  # in the same step, as the deferral sends it
    assert off.outcome == "admitted"
    assert off.radio == name(radios.athom)
    await tick(hass)
    assert not poll.admitted  # the turn-off's connect is the one in flight
    radios.athom.start_connect(NIGHTSTAND_2)
    radios.athom.finish_connect(NIGHTSTAND_2)
    await tick(hass)
    assert poll.outcome == "admitted"
    for call in (off, poll):
        await call.finish()


async def test_ending_a_deferred_turn_off_twice_is_harmless(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """The second end counts nothing off: a turn-off deferred later still holds a poll."""
    radios.hci0.hear(BULB, -60)
    end = async_note_deferred_off(hass)
    end()
    end()
    async_note_deferred_off(hass)
    poll = start(BULB, poll=True)
    assert not poll.admitted
    poll.task.cancel()


async def test_a_turn_off_of_a_device_already_off_goes_after_other_commands_before_polls(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """With one slot on the proxy, a ``last`` command queued before a normal command goes
    after it, and still before a poll queued before both."""
    _last_slot_athom(radios)
    for device in (NIGHTSTAND, NIGHTSTAND_2, BULB, BULB_2):
        radios.athom.hear(device, -70)
    holder = start(NIGHTSTAND)
    poll = start(NIGHTSTAND_2, poll=True)
    already_off = start(BULB, last=True)
    command = start(BULB_2)
    assert [call.admitted for call in (poll, already_off, command)] == [False, False, False]
    await holder.finish()
    await settle()
    assert command.outcome == "admitted"
    assert not already_off.admitted
    await command.finish()
    await settle()
    assert already_off.outcome == "admitted"
    assert not poll.admitted
    await already_off.finish()
    await settle()
    assert poll.outcome == "admitted"
    await poll.finish()


async def test_a_device_linked_through_any_of_its_paths_skips_the_queue(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """Linked through its last path (habluetooth keeps its scanners in a set: any order)."""
    radios.athom.hear(BULB, -60)
    radios.hci0.hear(BULB, -70)
    radios.bt_proxy.hear(BULB, -80)
    last = bluetooth.async_scanner_devices_by_address(hass, BULB, True)[-1].scanner
    last.take(BULB)
    call = start(BULB)
    assert call.outcome == "linked"
    assert call.radio == last.name
    await call.finish()


# -- the cap --------------------------------------------------------------------------


async def test_a_call_goes_ahead_after_the_cap(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """Past CONNECT_GATE_MAX_WAIT_SECONDS a call goes as it would without the gate: down the
    path habluetooth picks (the weak adapter, the proxy being full), ticketed there."""
    for slot in range(3):
        radios.athom.take(f"11:22:33:44:55:{slot:02X}")
    radios.athom.hear(NIGHTSTAND, -70)
    radios.hci0.hear(NIGHTSTAND, -96)
    call = start(NIGHTSTAND)
    monotonic.now += CONNECT_GATE_MAX_WAIT_SECONDS - 0.5
    await tick(hass)
    assert not call.admitted
    monotonic.now += 0.5
    await tick(hass)
    assert call.outcome == "capped"
    assert call.radio == name(radios.hci0)
    assert call.admission is not None
    assert call.admission.waited == CONNECT_GATE_MAX_WAIT_SECONDS
    assert gate(hass)._tickets[NIGHTSTAND].scanner is radios.hci0
    await call.finish()


async def test_a_poll_behind_waiting_commands_still_goes_after_the_cap(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """Its cap holds even while commands still wait (and keep it behind them)."""
    for slot in range(3):
        radios.athom.take(f"11:22:33:44:55:{slot:02X}")
    radios.athom.hear(NIGHTSTAND, -70)
    radios.athom.hear(NIGHTSTAND_2, -70)
    radios.hci0.hear(BULB, -60)
    first = start(NIGHTSTAND)
    poll = start(BULB, poll=True)
    assert not poll.admitted
    monotonic.now += CONNECT_GATE_MAX_WAIT_SECONDS / 2
    later = start(NIGHTSTAND_2)
    first.task.cancel()
    await settle()
    monotonic.now += CONNECT_GATE_MAX_WAIT_SECONDS / 2
    await tick(hass)
    assert not later.admitted
    assert poll.outcome == "capped"
    for call in (later, poll):
        call.task.cancel()


async def test_a_capped_call_with_every_radio_full_is_ticketed_on_its_best_usable_path(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """The proxy heard best keeps failing: the ticket goes on the best of the others."""
    for slot in range(3):
        radios.athom.take(f"11:22:33:44:55:{slot:02X}")
        radios.bt_proxy.take(f"11:22:33:44:55:{slot:02X}")
    for slot in range(5):
        radios.hci0.take(f"11:22:33:44:55:{slot:02X}")
    radios.bt_proxy.hear(NIGHTSTAND, -60)
    radios.athom.hear(NIGHTSTAND, -70)
    radios.hci0.hear(NIGHTSTAND, -80)
    for _ in range(CONNECT_GATE_PATH_FAILURES):
        radios.bt_proxy.start_connect(NIGHTSTAND, reserve=False)
        radios.bt_proxy.finish_connect(NIGHTSTAND, connected=False)
    call = start(NIGHTSTAND)
    monotonic.now += CONNECT_GATE_MAX_WAIT_SECONDS
    await tick(hass)
    assert call.outcome == "capped"
    assert call.radio == name(radios.athom)
    await call.finish()


async def test_a_capped_call_with_no_usable_path_is_ticketed_on_its_best_path(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    for slot in range(5):
        radios.hci0.take(f"11:22:33:44:55:{slot:02X}")
    for slot in range(3):
        radios.bt_proxy.take(f"11:22:33:44:55:{slot:02X}")
    radios.hci0.hear(NIGHTSTAND, -96)
    radios.bt_proxy.hear(NIGHTSTAND, -92)
    call = start(NIGHTSTAND)
    monotonic.now += CONNECT_GATE_MAX_WAIT_SECONDS
    await tick(hass)
    assert call.outcome == "capped"
    assert call.radio == name(radios.bt_proxy)
    await call.finish()


async def test_a_connect_stops_holding_its_radio_after_the_cap(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """A call stuck that long can't be told from a slow connect: the radio is free again."""
    for device in (NIGHTSTAND, NIGHTSTAND_2):
        radios.athom.hear(device, -70)
    stuck = start(NIGHTSTAND)
    monotonic.now += CONNECT_GATE_MAX_WAIT_SECONDS - 1
    waiting = start(NIGHTSTAND_2)
    await tick(hass)
    assert not waiting.admitted
    monotonic.now += 1
    await tick(hass)
    assert waiting.outcome == "admitted"
    assert waiting.admission is not None
    assert waiting.admission.waited == 1
    await stuck.finish()
    await waiting.finish()


async def test_calls_capped_together_go_one_per_radio_per_tick(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """A mass command's calls queue together and reach the cap together: they go ahead one
    per radio per tick, not as the burst the gate exists to break up."""
    for slot in range(3):
        radios.athom.take(f"11:22:33:44:55:{slot:02X}")
    devices = [NIGHTSTAND, NIGHTSTAND_2, BULB]
    for device in devices:
        radios.athom.hear(device, -70)
        radios.bt_proxy.hear(device, -96)
    calls = [start(device) for device in devices]
    monotonic.now += CONNECT_GATE_MAX_WAIT_SECONDS
    for went in (1, 2, 3):
        await tick(hass)
        assert sum(call.admitted for call in calls) == went
        assert all(call.outcome in (None, "capped") for call in calls)
    for call in calls:
        call.task.cancel()


async def test_a_radio_just_given_a_connect_takes_no_capped_call_in_the_same_pass(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """A pass admits a command to the Athom proxy, then finds a poll for it past the cap:
    the poll goes ahead at the next tick, not on top of the connect just let through."""
    radios.athom.hear(NIGHTSTAND, -70)
    radios.athom.hear(NIGHTSTAND_2, -70)
    radios.athom.start_connect("11:22:33:44:55:AA")  # someone else's: the proxy is busy
    poll = start(NIGHTSTAND, poll=True)
    monotonic.now += CONNECT_GATE_MAX_WAIT_SECONDS - 1
    command = start(NIGHTSTAND_2)
    assert not command.admitted
    monotonic.now += 1
    radios.athom.finish_connect("11:22:33:44:55:AA")
    await tick(hass)
    assert command.outcome == "admitted"
    assert not poll.admitted
    await tick(hass)
    assert poll.outcome == "capped"
    for call in (command, poll):
        await call.finish()


async def test_a_failing_gate_lets_calls_through_unjudged(
    hass: HomeAssistant,
    radios: SimpleNamespace,
    start: Callable[..., Call],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A gate that can't read the scanners (a changed Bluetooth API, say) mustn't hold up the
    commands: they go ahead, and the error is logged in full once."""
    radios.athom.hear(NIGHTSTAND, -60)
    radios.athom.hear(NIGHTSTAND_2, -60)

    def _broken() -> Allocations:
        raise RuntimeError("no allocations today")

    with patch.object(radios.athom, "get_allocations", _broken):
        first = start(NIGHTSTAND)
        second = start(NIGHTSTAND_2)
    assert (first.outcome, second.outcome) == ("unjudged", "unjudged")
    assert first.radio is None
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "connect gate failed" in errors[0].getMessage()
    assert errors[0].exc_info is not None
    assert not gate(hass)._waiters
    assert gate(hass)._tick is None
    await first.finish()
    await second.finish()


async def test_a_call_judged_before_a_failure_in_the_same_pass_keeps_its_admission(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """A pass admits one call, then fails judging the next: the first keeps its ticket, the
    rest go ahead unjudged."""
    radios.athom.hear(NIGHTSTAND, -60)
    radios.hci0.hear(BULB, -60)
    radios.athom.start_connect("11:22:33:44:55:AA")
    first = start(NIGHTSTAND)
    assert not first.admitted
    radios.athom.finish_connect("11:22:33:44:55:AA")
    judge = ConnectGate._async_decide

    def _flaky(self: ConnectGate, waiter: object, *args: object, **kwargs: object) -> object:
        if getattr(waiter, "address", None) == BULB:
            raise RuntimeError("a bug judging this one")
        return judge(self, waiter, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(ConnectGate, "_async_decide", _flaky):
        second = start(BULB)
    await settle()
    assert (first.outcome, second.outcome) == ("admitted", "unjudged")
    assert gate(hass)._tickets.keys() == {NIGHTSTAND}
    for call in (first, second):
        await call.finish()


async def test_a_gate_failing_as_a_call_ends_lets_the_call_end(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    radios.athom.hear(NIGHTSTAND, -60)
    call = start(NIGHTSTAND)
    assert call.outcome == "admitted"

    def _broken(*_args: object) -> list[object]:
        raise RuntimeError("no paths today")

    with patch("homeassistant.components.bluetooth.async_scanner_devices_by_address", _broken):
        await call.finish()
    assert not gate(hass)._tickets


# -- cancellation, cleanup and the debug line -----------------------------------------


async def test_a_call_cancelled_while_waiting_leaves_the_queue(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    for device in (NIGHTSTAND, NIGHTSTAND_2):
        radios.athom.hear(device, -70)
    first, second = start(NIGHTSTAND), start(NIGHTSTAND_2)
    assert gate(hass)._tick is not None
    second.task.cancel()
    # A pass before the cancelled call has run on leaves it be, though there's room now.
    radios.athom.start_connect(NIGHTSTAND)
    radios.athom.finish_connect(NIGHTSTAND)
    fire_tick(hass)
    assert gate(hass)._tickets.keys() == {NIGHTSTAND}
    await settle()
    assert not gate(hass)._waiters
    assert gate(hass)._tick is None
    await first.finish()


async def test_a_call_cancelled_once_admitted_gives_its_ticket_back(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """Admitted by a tick, cancelled before it resumed: the radio isn't left held."""
    for device in (NIGHTSTAND, NIGHTSTAND_2, BULB):
        radios.athom.hear(device, -70)
    first, second = start(NIGHTSTAND), start(NIGHTSTAND_2)
    radios.athom.start_connect(NIGHTSTAND)
    radios.athom.finish_connect(NIGHTSTAND)
    fire_tick(hass)  # this pass admits the second
    assert gate(hass)._tickets.keys() == {NIGHTSTAND, NIGHTSTAND_2}
    assert not second.admitted  # it hasn't resumed yet
    second.task.cancel()
    await settle()
    assert not second.admitted
    assert gate(hass)._tickets.keys() == {NIGHTSTAND}
    third = start(BULB)
    assert third.outcome == "admitted"
    await first.finish()
    await third.finish()


async def test_releasing_an_address_lets_all_its_waiting_calls_go_at_once(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
) -> None:
    """Its entry is unloading: every call to it still waiting goes now, ticketless, before
    its link closes. Calls to other devices wait on."""
    _full_athom(radios)
    radios.athom.hear(NIGHTSTAND, -70)
    radios.athom.hear(NIGHTSTAND_2, -70)
    first, second = start(NIGHTSTAND), start(NIGHTSTAND)
    other = start(NIGHTSTAND_2)
    monotonic.now += 2
    async_release_waiters(hass, NIGHTSTAND)
    await settle()
    assert (first.outcome, second.outcome) == ("released", "released")
    assert first.radio is None
    assert first.admission is not None
    assert first.admission.waited == 2
    assert not other.admitted
    assert not gate(hass)._tickets
    assert gate(hass)._tick is not None  # the other call still waits
    for call in (first, second):
        await call.finish()


async def test_releasing_the_last_waiting_calls_disarms_the_tick(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    _full_athom(radios)
    radios.athom.hear(NIGHTSTAND, -70)
    call = start(NIGHTSTAND)
    assert gate(hass)._tick is not None
    async_release_waiters(hass, NIGHTSTAND)
    assert gate(hass)._tick is None
    await settle()
    assert call.outcome == "released"
    await call.finish()


async def test_each_call_logs_its_wait_radio_and_time_to_done(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
    caplog: pytest.LogCaptureFixture,
) -> None:
    radios.athom.hear(BULB, -60)
    radios.hci0.hear(BULB, -75)
    with caplog.at_level(logging.DEBUG, logger=connect_gate.__name__):
        call = start(BULB)
        monotonic.now += 1.5
        await call.finish()
    assert (
        f"{BULB}: command admitted by the connect gate after 0.00 s, radio {name(radios.athom)}; "
        "done 1.50 s later"
    ) in caplog.text


async def test_the_debug_line_names_the_radio_a_moved_connect_turned_up_on(
    hass: HomeAssistant,
    monotonic: SimpleNamespace,
    radios: SimpleNamespace,
    start: Callable[..., Call],
    caplog: pytest.LogCaptureFixture,
) -> None:
    radios.athom.hear(BULB, -60)
    radios.hci0.hear(BULB, -75)
    radios.athom.hear(NIGHTSTAND, -70)
    with caplog.at_level(logging.DEBUG, logger=connect_gate.__name__):
        bulb, nightstand = start(BULB, poll=True), start(NIGHTSTAND)
        radios.hci0.start_connect(BULB)
        await tick(hass)
        bulb.task.cancel()
        await settle()
    assert (
        f"{BULB}: poll admitted by the connect gate after 0.00 s, radio {name(radios.athom)} "
        f"(seen on {name(radios.hci0)}); failed 0.00 s later"
    ) in caplog.text
    await nightstand.finish()


# -- wiring: entity commands and coordinator polls ------------------------------------


async def test_a_light_command_waits_for_a_slot(
    hass: HomeAssistant,
    radios: SimpleNamespace,
    entry: MockConfigEntry,
    mock_device: Device,
) -> None:
    """An entity command's library call waits at the gate, and goes as a slot frees."""
    for slot in range(3):
        radios.athom.take(f"11:22:33:44:55:{slot:02X}")
    radios.athom.hear(ADDRESS, -70)
    mock_device.turn_off.reset_mock()  # type: ignore[attr-defined]
    command = hass.async_create_task(
        hass.services.async_call("light", "turn_off", {"entity_id": MAIN}, blocking=True)
    )
    await tick(hass)
    mock_device.turn_off.assert_not_awaited()  # type: ignore[attr-defined]
    radios.athom.give_back("11:22:33:44:55:00")
    await tick(hass)
    await command
    mock_device.turn_off.assert_awaited_once()  # type: ignore[attr-defined]
    assert not gate(hass)._tickets


async def test_a_light_command_goes_through_the_gate_as_a_command(
    hass: HomeAssistant,
    radios: SimpleNamespace,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    radios.athom.hear(ADDRESS, -70)
    with caplog.at_level(logging.DEBUG, logger=connect_gate.__name__):
        await hass.services.async_call("light", "turn_off", {"entity_id": MAIN}, blocking=True)
    assert f"{ADDRESS}: command admitted by the connect gate" in caplog.text


async def test_a_light_command_s_deadline_covers_its_wait_at_the_gate(
    hass: HomeAssistant, radios: SimpleNamespace, entry: MockConfigEntry, mock_device: Device
) -> None:
    for slot in range(3):
        radios.athom.take(f"11:22:33:44:55:{slot:02X}")
    radios.athom.hear(ADDRESS, -70)
    with (
        patch("custom_components.govee_ble_local.entity.COMMAND_TIMEOUT_SECONDS", 0.05),
        pytest.raises(HomeAssistantError, match="TimeoutError"),
    ):
        await hass.services.async_call("light", "turn_off", {"entity_id": MAIN}, blocking=True)
    mock_device.turn_off.assert_not_awaited()  # type: ignore[attr-defined]
    assert not gate(hass)._waiters


async def test_a_poll_waiting_for_a_poll_place_holds_no_ticket(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """It takes its ticket once it has a place: queued for one, it must not hold a radio."""
    radios.athom.hear(BULB, -60)
    device = make_device(address=BULB)
    config_entry = MockConfigEntry(domain=DOMAIN, unique_id=BULB, data={"address": BULB})
    config_entry.add_to_hass(hass)
    coordinator = GoveeBleLocalCoordinator(hass, config_entry, device)
    semaphore = _poll_semaphore(hass)
    for _ in range(MAX_CONCURRENT_POLLS):
        await semaphore.acquire()
    refresh = hass.async_create_task(coordinator.async_refresh())
    await settle()
    assert not gate(hass)._tickets if _GATE in hass.data else True
    for _ in range(MAX_CONCURRENT_POLLS):
        semaphore.release()
    await refresh
    device.update.assert_awaited_once()  # type: ignore[attr-defined]


async def test_a_command_cancelled_at_the_gate_closes_its_call(
    hass: HomeAssistant, radios: SimpleNamespace, entry: MockConfigEntry
) -> None:
    """The library call never started: it is closed, not left unawaited."""
    for slot in range(3):
        radios.athom.take(f"11:22:33:44:55:{slot:02X}")
    radios.athom.hear(ADDRESS, -70)
    started = False

    async def _call() -> None:
        nonlocal started
        started = True

    light = hass.data[DATA_COMPONENT].get_entity(MAIN)
    assert light is not None
    coro = _call()
    task = hass.async_create_task(light._run_client_command(coro))
    await settle()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coro.cr_frame is None  # closed
    assert not started


async def test_polls_go_through_the_gate_behind_commands(
    hass: HomeAssistant, radios: SimpleNamespace, start: Callable[..., Call]
) -> None:
    """A background poll waits while a command does, though its own radio is free."""
    radios.athom.hear(NIGHTSTAND, -70)
    radios.athom.hear(NIGHTSTAND_2, -70)
    radios.hci0.hear(BULB, -60)
    holder, command = start(NIGHTSTAND), start(NIGHTSTAND_2)
    device = make_device(address=BULB)
    config_entry = MockConfigEntry(domain=DOMAIN, unique_id=BULB, data={"address": BULB})
    config_entry.add_to_hass(hass)
    coordinator = GoveeBleLocalCoordinator(hass, config_entry, device)
    refresh = hass.async_create_task(coordinator.async_refresh())
    await tick(hass)
    device.update.assert_not_awaited()  # type: ignore[attr-defined]
    radios.athom.start_connect(NIGHTSTAND)
    radios.athom.finish_connect(NIGHTSTAND)
    await tick(hass)
    assert command.admitted
    await refresh
    device.update.assert_awaited_once()  # type: ignore[attr-defined]
    await holder.finish()
    await command.finish()


async def test_a_link_being_released_counts_as_down(
    hass: HomeAssistant, radios: SimpleNamespace
) -> None:
    """While the integration releases a device's link, the radio still lists it, but it's going.

    A call to the device connects again (judged, with a ticket), so another device's connect
    through the same proxy waits for it, as without a link: two pending connects would break
    the proxy's one-at-a-time limit. Its link isn't up either, so a turn-off of it already off
    still waits its turn (``async_link_up``). Once the release ends, the gate judges again.
    """
    radios.athom.hear(NIGHTSTAND, -70)
    radios.athom.hear(BULB, -70)
    radios.athom.take(NIGHTSTAND)  # the old link, listed while it's torn down
    end = async_note_releasing(hass, NIGHTSTAND)
    assert not async_link_up(hass, NIGHTSTAND)

    again = Call(hass, NIGHTSTAND)
    await tick(hass)
    assert again.outcome == "admitted"  # a new connect, not the old link
    other = Call(hass, BULB)
    await tick(hass)
    assert not other.admitted  # behind the device's connect on the same proxy

    end()
    end()  # twice is harmless
    assert async_link_up(hass, NIGHTSTAND)  # listed, and no longer being released
    for _ in range(5):  # no tick: the end judges the waiting calls again itself
        await asyncio.sleep(0)
    assert other.admitted  # the device's ticket now shows its link up
    await again.finish()
    await other.finish()


async def test_overlapping_releases_of_a_link_end_with_the_last(
    hass: HomeAssistant, radios: SimpleNamespace
) -> None:
    """Two releases of one device's link (a window's, then a put-off one): it counts as being
    released until both have ended."""
    radios.athom.hear(NIGHTSTAND, -70)
    radios.athom.take(NIGHTSTAND)
    first, second = async_note_releasing(hass, NIGHTSTAND), async_note_releasing(hass, NIGHTSTAND)
    elsewhere = async_note_releasing(hass, BULB_2)  # another device's release: no effect here
    first()
    assert not async_link_up(hass, NIGHTSTAND)
    second()
    assert async_link_up(hass, NIGHTSTAND)
    elsewhere()


async def test_a_listed_link_not_being_released_is_used(
    hass: HomeAssistant, radios: SimpleNamespace
) -> None:
    """The same listed link with no release running: a call to the device uses it (linked)."""
    radios.athom.hear(NIGHTSTAND, -70)
    radios.athom.take(NIGHTSTAND)
    assert async_link_up(hass, NIGHTSTAND)
    call = Call(hass, NIGHTSTAND)
    await tick(hass)
    assert call.outcome == "linked"
    await call.finish()


async def test_the_gate_is_one_per_home_assistant(hass: HomeAssistant) -> None:
    async_connect_slot(hass, BULB)
    first = hass.data[_GATE]
    async_connect_slot(hass, BULB_2)
    assert hass.data[_GATE] is first
