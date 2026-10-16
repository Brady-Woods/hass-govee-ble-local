"""Shared fixtures for the Govee BLE Local integration tests.

Devices are REAL library ``Device`` objects built from the shipped device
profiles (zones, segments, capabilities, scene catalog, state bookkeeping and
``GoveeBleNotSupported`` errors are the library's own); only the BLE transport
is replaced by :class:`FakeConnection`, which records frames and can be made to
fail. ``Device.update`` returns the same (mutable) state object the real one
does; tests simulate a read-back by mutating ``device.state`` first.
"""

from __future__ import annotations

from collections.abc import Generator
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from bleak.backends.device import BLEDevice
import govee_ble_local
from govee_ble_local import Device, GoveeBleError, create_device
from govee_ble_local.models import Encryption
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local import capture as capture_module
from custom_components.govee_ble_local.const import CONF_SKU, DOMAIN
from custom_components.govee_ble_local.coordinator import GoveeBleLocalCoordinator

from .const import ADDRESS, LOCAL_NAME, TITLE, govee_mfg

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
        self.error: BaseException | None = None
        self.fail_after: int | None = None
        self.closed = False

    def _write(self, frames: list[bytes]) -> None:
        for frame in frames:
            if self.error is not None:
                if not self.fail_after:
                    raise self.error
                self.fail_after -= 1
            self.sent.append(frame)

    async def send(self, frame: bytes, *, expect_ack: bool = True) -> bytes | None:
        self._write([frame])
        return None

    async def send_many(self, frames: list[bytes]) -> None:
        self._write(list(frames))

    async def send_unacked(self, frames: list[bytes], *, gap: float = 0.03) -> None:
        self._write(list(frames))

    async def query(self, *_args: Any, **_kwargs: Any) -> list[bytes]:
        if self.error is not None:
            raise self.error
        return []

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
) -> Device:
    """Build a real library Device for ``sku`` on a FakeConnection."""
    device = create_device(
        BLEDevice(address=address, name=name, details={}), sku, None, secret=secret
    )
    device._conn = FakeConnection()  # type: ignore[assignment]
    for method in _WRAPPED_COMMANDS:
        setattr(device, method, AsyncMock(wraps=getattr(device, method)))

    async def _update() -> Any:
        # Like a real read-back poll: the state object is updated in place and
        # is no longer merely optimistic.
        device.state.optimistic = False
        return device.state

    device.update = AsyncMock(side_effect=_update)  # type: ignore[method-assign]
    return device


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
) -> BluetoothServiceInfoBleak:
    """Build a real BluetoothServiceInfoBleak for a Govee advertisement."""
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=rssi,
        manufacturer_data=govee_mfg(on=on) if manufacturer_data is None else manufacturer_data,
        service_data={},
        service_uuids=[],
        source=source,
        device=BLEDevice(address=address, name=name, details={}),
        advertisement=None,
        connectable=connectable,
        time=time.monotonic(),
        tx_power=None,
    )


class StandInCommandRejected(GoveeBleError):
    """GoveeBleCommandRejected for a library that predates it (1.3.0 and older)."""


@pytest.fixture
def command_rejected() -> Generator[type[GoveeBleError]]:
    """The library's GoveeBleCommandRejected, as the integration's handlers catch it.

    A library older than the class gets a stand-in wired into the handlers, so the
    rejection handling is tested on every library version CI installs.
    """
    real: type[GoveeBleError] | None = getattr(govee_ble_local, "GoveeBleCommandRejected", None)
    if real is not None:
        yield real
        return
    with (
        patch(
            "custom_components.govee_ble_local.entity.COMMAND_REJECTED", (StandInCommandRejected,)
        ),
        patch(
            "custom_components.govee_ble_local.coordinator.COMMAND_REJECTED",
            (StandInCommandRejected,),
        ),
    ):
        yield StandInCommandRejected


def rejected_write(error: type[GoveeBleError], address: str = ADDRESS) -> GoveeBleError:
    """A write rejection as the library raises it (its message names the device)."""
    return error(f"{address}: device rejected write 0x33 (status 0x01)")


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
    with patch("custom_components.govee_ble_local.FIRST_POLL_STAGGER_SECONDS", 1):
        yield


@pytest.fixture(autouse=True)
def presence() -> Generator[SimpleNamespace]:
    """Control what the Bluetooth stack reports for the device.

    ``connectable``/``any`` drive ``coordinator.address_present`` and
    ``service_info`` drives ``coordinator.last_service_info``. The isolated test
    environment has no real scanners, which would otherwise read as "vanished".
    """
    state = SimpleNamespace(connectable=True, any=True, service_info=make_service_info())

    def _present(self: GoveeBleLocalCoordinator, *, connectable: bool = True) -> bool:
        return bool(state.connectable if connectable else state.any)

    def _last(self: GoveeBleLocalCoordinator) -> BluetoothServiceInfoBleak | None:
        return state.service_info  # type: ignore[no-any-return]

    with (
        patch.object(GoveeBleLocalCoordinator, "address_present", _present),
        patch.object(GoveeBleLocalCoordinator, "last_service_info", _last),
    ):
        yield state


@pytest.fixture
def device_sku() -> str:
    """SKU of the device under test (override per module/test)."""
    return "H60A6"


@pytest.fixture
def mock_device(device_sku: str) -> Generator[Device]:
    """Patch create_device to return a real Device on a fake transport."""
    device = make_device(device_sku)
    with patch("custom_components.govee_ble_local.create_device", return_value=device):
        yield device


@pytest.fixture
def mock_config_entry(device_sku: str) -> MockConfigEntry:
    """A config entry for the device under test."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=ADDRESS,
        data={"address": ADDRESS, CONF_SKU: device_sku},
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
    """Set the integration up and return the loaded config entry."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
