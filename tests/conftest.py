"""Shared fixtures for the Govee BLE Local integration tests.

Devices are REAL library ``Device`` objects built from the shipped device
profiles (zones, segments, capabilities, scene catalog, state bookkeeping and
``GoveeBleNotSupported`` errors are the library's own); only the BLE transport
is replaced by :class:`FakeConnection`, which records frames and can be made to
fail. ``Device.update`` returns the same (mutable) state object the real one
does; tests simulate a read-back by mutating ``device.state`` first.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
import math
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, PropertyMock, patch

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
import govee_ble_local
from govee_ble_local import Device, GoveeBleCommandRejected, GoveeBleError, create_device
from govee_ble_local.models import Encryption
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.govee_ble_local import capture as capture_module
from custom_components.govee_ble_local.const import CONF_ALLOW_MODELLED, CONF_SKU, DOMAIN
from custom_components.govee_ble_local.coordinator import GoveeBleLocalCoordinator

from .const import ADDRESS, LOCAL_NAME, TITLE, govee_mfg, pact_mfg

pytest_plugins = ["pytest_homeassistant_custom_component"]

# Public async Device methods wrapped in AsyncMock(wraps=...) so tests can assert
# calls while the library's real behaviour still runs.
_WRAPPED_COMMANDS = (
    "turn_on",
    "turn_off",
    "set_power",
    "set_brightness",
    "set_rgb",
    "set_color_temp",
    "set_segment_rgb",
    "set_segment_brightness",
    "set_segment_color_temp",
    "set_zone_power",
    "set_zone_rgb",
    "set_zone_color_temp",
    "set_scene",
    "set_scene_by_name",
    "set_gradual",
    "read_secret",
    "stop",
)


class FakeConnection:
    """Stand-in for the library's GoveeConnection: records frames, no BLE.

    Set ``error`` to make every write/query raise it; ``fail_after`` lets that
    many more frames through first (a command that fails part-way).
    """

    def __init__(self) -> None:
        self.encryption = Encryption.NONE
        self.sent: list[bytes] = []
        # The request frame of every read (query) the library made, answered or not.
        self.queries: list[bytes] = []
        self.error: BaseException | None = None
        self.fail_after: int | None = None
        self.closed = False
        # The default BLE write size (ATT MTU 23 - 3); newer libraries size
        # multi-packet uploads to it.
        self.write_payload_size = 20

    def _write(self, frames: list[bytes]) -> None:
        for frame in frames:
            if self.error is not None:
                if not self.fail_after:
                    raise self.error
                self.fail_after -= 1
            self.sent.append(frame)

    async def send(
        self, frame: bytes, *, expect_ack: bool = True, connect_budget: Any = None
    ) -> bytes | None:
        self._write([frame])
        return None

    async def send_many(self, frames: list[bytes]) -> None:
        self._write(list(frames))

    async def send_unacked(
        self, frames: list[bytes] | Callable[[int], list[bytes]], *, gap: float = 0.03
    ) -> None:
        # Newer libraries pass a function of the link's write size (MTU-sized uploads).
        self._write(list(frames(self.write_payload_size) if callable(frames) else frames))

    async def query(self, *args: Any, **_kwargs: Any) -> list[bytes]:
        if args:
            self.queries.append(args[0])
        if self.error is not None:
            raise self.error
        return []

    async def connect(self) -> None:
        """Connect and set the session up (no link here: nothing to do)."""

    async def close(self) -> None:
        self.closed = True

    def update_ble_device(self, ble_device: BLEDevice) -> None:
        """Accept a fresher BLEDevice (no-op)."""


def make_device(
    sku: str = "H60A6",
    *,
    address: str = ADDRESS,
    name: str = LOCAL_NAME,
    secret: bytes | None = None,
    allow_modelled: bool = False,
) -> Device:
    """Build a real library Device for ``sku`` on a FakeConnection.

    ``allow_modelled`` builds an opt-in-only ("modelled") model, as setup does for an entry
    that opted in; it is passed only when set, so the 1.2.1 floor still builds the rest.
    """
    device = create_device(
        BLEDevice(address=address, name=name, details={}),
        sku,
        None,
        secret=secret,
        **({"allow_modelled": True} if allow_modelled else {}),
    )
    device._conn = FakeConnection()  # type: ignore[assignment]
    for method in _WRAPPED_COMMANDS:
        setattr(device, method, AsyncMock(wraps=getattr(device, method)))

    async def _update(**_kwargs: Any) -> Any:
        # Like a real read-back poll: the state object is updated in place and
        # is no longer merely optimistic.
        device.state.optimistic = False
        return device.state

    device.update = AsyncMock(side_effect=_update)  # type: ignore[method-assign]
    return device


@contextmanager
def runtime_segment_count(count: int) -> Iterator[None]:
    """Give every Device a runtime segment count other than its profile's.

    Newer libraries have ``Device.segment_count`` (set when a device reports its own
    count); it is patched on the class, so this works on the 1.2.1 floor too, which has
    no such attribute. Because ``create=True`` makes the attribute, this can't show the
    library really has it: tests of the real API call ``Device.set_segment_count``.
    """
    with patch.object(
        Device, "segment_count", new_callable=PropertyMock, return_value=count, create=True
    ):
        yield


def rejected_write(address: str = ADDRESS) -> GoveeBleCommandRejected:
    """A write rejection as the library raises it (its message names the device)."""
    return GoveeBleCommandRejected(f"{address}: device rejected write 0x33 (status 0x01)")


def connection(device: Device) -> FakeConnection:
    """Return a test device's fake transport."""
    conn = device._conn
    assert isinstance(conn, FakeConnection)
    return conn


def allow_adverts(device: Device) -> None:
    """End the library's post-command advertisement suppression window."""
    device._advert_ignore_until = 0.0


def make_service_info(
    *,
    address: str = ADDRESS,
    name: str = LOCAL_NAME,
    on: bool | None = True,
    manufacturer_data: dict[int, bytes] | None = None,
    rssi: int = -60,
    source: str = "local",
    connectable: bool = True,
    seen_at: float | None = None,
    advertisement: bool = False,
) -> BluetoothServiceInfoBleak:
    """Build a real BluetoothServiceInfoBleak for a Govee advertisement.

    ``seen_at`` is its (monotonic) receive time; by default, now. ``advertisement`` also
    attaches bleak's AdvertisementData, which the library's create_device reads the pact
    from; by default there is none.
    """
    mfg = govee_mfg(on=on) if manufacturer_data is None else manufacturer_data
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=rssi,
        manufacturer_data=mfg,
        service_data={},
        service_uuids=[],
        source=source,
        device=BLEDevice(address=address, name=name, details={}),
        advertisement=AdvertisementData(
            local_name=name,
            manufacturer_data=mfg,
            service_data={},
            service_uuids=[],
            tx_power=None,  # type: ignore[arg-type]
            rssi=rssi,
            platform_data=(),
        )
        if advertisement
        else None,
        connectable=connectable,
        time=time.monotonic() if seen_at is None else seen_at,
        tx_power=None,
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading of the custom integration in every test."""


@pytest.fixture(autouse=True)
def reset_log_capture() -> Generator[None]:
    """Drop warning captures left registered by entries a test didn't unload."""
    yield
    for capture in list(capture_module._ROUTER.captures):
        capture.detach()


@pytest.fixture(autouse=True)
def no_first_poll_stagger() -> Generator[None]:
    """Run the background first poll immediately (crc32 % 1 == 0)."""
    with (
        patch("custom_components.govee_ble_local.FIRST_POLL_STAGGER_SECONDS", 1),
        patch("custom_components.govee_ble_local.FIRST_POLL_STAGGER_PER_DEVICE_SECONDS", 0),
    ):
        yield


# ``presence.connectable_service_info`` while the connectable history holds
# whatever the any-scanner one does (the default).
SAME_AS_ANY_SCANNER = object()


@pytest.fixture(autouse=True)
def presence() -> Generator[SimpleNamespace]:
    """Control what the Bluetooth stack reports for the device.

    ``connectable``/``any`` drive ``coordinator.async_address_present``, and
    ``service_info`` drives ``coordinator.async_last_service_info`` for the
    any-scanner history. The connectable history holds the same entry unless
    ``connectable_service_info`` is set (to another entry, or None). The isolated
    test environment has no real scanners, which would otherwise read as "vanished".
    """
    state = SimpleNamespace(
        connectable=True,
        any=True,
        service_info=make_service_info(),
        connectable_service_info=SAME_AS_ANY_SCANNER,
    )

    def _present(self: GoveeBleLocalCoordinator, *, connectable: bool = True) -> bool:
        return bool(state.connectable if connectable else state.any)

    def _last(
        self: GoveeBleLocalCoordinator, *, connectable: bool = True
    ) -> BluetoothServiceInfoBleak | None:
        if connectable and state.connectable_service_info is not SAME_AS_ANY_SCANNER:
            return state.connectable_service_info  # type: ignore[no-any-return]
        return state.service_info  # type: ignore[no-any-return]

    with (
        patch.object(GoveeBleLocalCoordinator, "async_address_present", _present),
        patch.object(GoveeBleLocalCoordinator, "async_last_service_info", _last),
    ):
        yield state


@pytest.fixture
def monotonic(presence: SimpleNamespace, freezer: Any) -> Generator[SimpleNamespace]:
    """Control the Bluetooth stack's monotonic clock, which ages advertisements.

    ``now`` starts at the default advertisement's receive time (just heard),
    rounded up to a whole second, so the steps tests take (whole and half
    seconds) add up exactly: from a fractional start, ``+= 30`` can land a
    rounding error short of a 30 s threshold, which made the threshold tests
    fail for some starting values. Freezes wall time too, and patches only
    once frozen: freezegun would otherwise swap its own clock back in when it
    starts.
    """
    start = float(math.ceil(presence.service_info.time))
    presence.service_info = make_service_info(seen_at=start)
    clock = SimpleNamespace(now=start)
    with patch("homeassistant.components.bluetooth.MONOTONIC_TIME", lambda: clock.now):
        yield clock


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Register entities that default to disabled (segment lights, noisy diagnostics) as enabled."""
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        return_value=True,
    ):
        yield


@dataclass(frozen=True)
class ModelledCase:
    """A model the library supports only as "modelled", at a pact its modelled row accepts."""

    sku: str
    pact: tuple[int, int]
    # A pact none of its modelled rows lists: the library serves it, but not there.
    unlisted_pact: tuple[int, int]

    @property
    def name(self) -> str:
        """The name it advertises."""
        return f"Govee_{self.sku}_ABCD"

    def info(
        self, *, manufacturer_data: dict[int, bytes] | None = None, advertisement: bool = False
    ) -> BluetoothServiceInfoBleak:
        """Its advert: at that pact, unless other manufacturer data is given."""
        mfg = pact_mfg(*self.pact) if manufacturer_data is None else manufacturer_data
        return make_service_info(name=self.name, manufacturer_data=mfg, advertisement=advertisement)


@pytest.fixture
def modelled() -> ModelledCase:
    """A model the installed library supports only as "modelled", at one of its rows' pacts.

    Taken from the library, never a fixed list: what counts as modelled moves. The pacts
    tried are each modelled row's own, read from the library's route table (a test-only
    look at a private name, imported here so a move fails only these tests, not the
    collection of every module). The unlisted pact is the first outside all of them.
    """
    from govee_ble_local.devices import profile as library_profiles  # noqa: PLC0415

    lib = govee_ble_local
    skus = sorted(set(lib.supported_skus(allow_modelled=True)) - set(lib.supported_skus()))
    routes = getattr(library_profiles, "_MODELLED", (None, {}))[1]
    grid = [(t, c) for t in range(1, 16) for c in range(1, 16)]
    for sku in skus:
        rows = routes.get(sku, ())
        if not rows or any(row.pacts is None for row in rows):
            continue  # a row that takes any pact leaves no unlisted one
        listed = sorted(set().union(*(row.pacts for row in rows)))
        unlisted = next((pact for pact in grid if pact not in listed), None)
        for pact in listed:
            if unlisted is not None and lib.is_supported_sku(sku, pact=pact, allow_modelled=True):
                assert not lib.is_supported_sku(sku, pact=pact)
                assert not lib.is_supported_sku(sku, pact=unlisted, allow_modelled=True)
                return ModelledCase(sku, pact, unlisted)
    pytest.fail("found no modelled-only model at one of its rows' pacts in the installed library")


@pytest.fixture
def broken_scene_store() -> Generator[SimpleNamespace]:
    """Make the library's scene store unreadable, as on a broken install (1.5.0 raises).

    Replaces the store loader every scene lookup goes through with an uncached one that
    raises, as the library's does for a missing or corrupt store, and records whether each
    read ran on the event loop (``on_loop``): there it would block on the file.
    """
    store = SimpleNamespace(
        error=GoveeBleError("scene catalog for H60A6 could not be read: FileNotFoundError()"),
        on_loop=[],
    )

    def _catalog(_sku: str) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            store.on_loop.append(False)
        else:
            store.on_loop.append(True)
        raise store.error

    with patch("govee_ble_local.scenes._catalog", _catalog):
        yield store


@pytest.fixture
def device_sku() -> str:
    """SKU of the device under test (override per module/test)."""
    return "H60A6"


@pytest.fixture
def allow_modelled() -> bool:
    """Whether the device under test is an opted-in "modelled" model (override per test)."""
    return False


@pytest.fixture
def mock_device(device_sku: str, allow_modelled: bool) -> Generator[Device]:
    """Patch create_device to return a real Device on a fake transport."""
    device = make_device(device_sku, allow_modelled=allow_modelled)
    with patch("custom_components.govee_ble_local.create_device", return_value=device):
        yield device


@pytest.fixture
def mock_config_entry(device_sku: str, allow_modelled: bool) -> MockConfigEntry:
    """A config entry for the device under test (with the opt-in, for a modelled model)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=ADDRESS,
        data={
            "address": ADDRESS,
            CONF_SKU: device_sku,
            **({CONF_ALLOW_MODELLED: True} if allow_modelled else {}),
        },
    )


@pytest.fixture
def mock_bluetooth(presence: SimpleNamespace) -> Generator[SimpleNamespace]:
    """Patch the Bluetooth lookups and registrations used during setup."""
    ble_device = presence.service_info.device
    with (
        patch(
            "homeassistant.components.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ) as ble_lookup,
        patch(
            "homeassistant.components.bluetooth.async_last_service_info",
            return_value=presence.service_info,
        ) as last_info,
        patch("homeassistant.components.bluetooth.async_register_callback") as register,
        patch("homeassistant.components.bluetooth.async_track_unavailable") as track_unavailable,
    ):
        yield SimpleNamespace(
            ble_lookup=ble_lookup,
            last_info=last_info,
            register=register,
            track_unavailable=track_unavailable,
        )


async def advance(hass: HomeAssistant, seconds: float) -> None:
    """Fire the timers due ``seconds`` from now and let the resulting work finish."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def advertisement_callback(bluetooth_mocks: SimpleNamespace) -> Any:
    """Return the advertisement callback the integration registered."""
    return bluetooth_mocks.register.call_args.args[1]


def unavailable_callback(bluetooth_mocks: SimpleNamespace, *, connectable: bool) -> Any:
    """Return the unavailable callback registered for the given history."""
    for call in bluetooth_mocks.track_unavailable.call_args_list:
        if call.kwargs.get("connectable") is connectable:
            return call.args[1]
    raise AssertionError(f"no unavailable tracker with connectable={connectable}")


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: Device,
    mock_bluetooth: SimpleNamespace,
) -> MockConfigEntry:
    """Set the integration up and return the loaded config entry, first poll done."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    # The first poll is an entry background task, which a plain
    # async_block_till_done() doesn't wait for.
    await hass.async_block_till_done(wait_background_tasks=True)
    return mock_config_entry


@pytest.fixture
def entry(setup_integration: MockConfigEntry) -> MockConfigEntry:
    """Short alias for ``setup_integration`` (the loaded entry, first poll done)."""
    return setup_integration
