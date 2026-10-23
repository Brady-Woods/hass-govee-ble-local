"""The shared test double stays a faithful stand-in for the library's transport."""

from __future__ import annotations

import inspect

from bleak.backends.device import BLEDevice
from govee_ble_local import GoveeBleError
from govee_ble_local.transport.connection import GoveeConnection
import pytest

from .conftest import IO_METHODS, REACHED_REAL_BLUETOOTH, FakeConnection
from .const import ADDRESS, LOCAL_NAME


def test_fake_connection_covers_every_io_method() -> None:
    """Every public coroutine of the library's connection is one FakeConnection replaces.

    FakeConnection inherits everything else (the library's non-I/O code runs as is). A new
    coroutine would reach for the radio from the tests: add it to IO_METHODS and override it.
    """
    coroutines = {
        name
        for name, member in inspect.getmembers(GoveeConnection, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }
    assert coroutines == IO_METHODS
    assert all(name in vars(FakeConnection) for name in IO_METHODS)


def test_fake_connection_bookkeeping_is_none_of_the_librarys_names() -> None:
    """What FakeConnection keeps for the tests (``sent``, ``queries``, ``error``, ...), and its
    own helpers, use no name the library's connection has.

    The library's code runs on FakeConnection as is: an attribute, property or method of the
    same name in the library would be overwritten by the fake's bookkeeping, or answer in its
    place, and tests would read the wrong thing. Rename the fake's, the day this fails.
    """
    own = set(FakeConnection.__static_attributes__) | {
        name for name in vars(FakeConnection) if not name.startswith("__")
    }
    own -= IO_METHODS
    assert "sent" in own  # the fake's instance attributes are found
    library = set(dir(GoveeConnection)).union(
        vars(GoveeConnection(BLEDevice(address=ADDRESS, name=LOCAL_NAME, details={}))),
        *(getattr(cls, "__static_attributes__", ()) for cls in GoveeConnection.__mro__),
    )
    assert not own & library, f"FakeConnection's {sorted(own & library)} are the library's too"


async def test_the_librarys_own_connect_reaches_no_real_bluetooth(
    no_real_bluetooth: list[str],
) -> None:
    """The library's own connect, run on a real GoveeConnection, is refused and recorded.

    So would be any I/O path a test reaches that FakeConnection doesn't replace (conftest's
    ``no_real_bluetooth``). This fails, by name, the day the library brings its links up
    other than through the name conftest refuses.
    """
    link = GoveeConnection(BLEDevice(address=ADDRESS, name=LOCAL_NAME, details={}))
    with pytest.raises((AssertionError, GoveeBleError), match=REACHED_REAL_BLUETOOTH):
        await link.connect()
    assert no_real_bluetooth
    assert all(attempt.startswith(REACHED_REAL_BLUETOOTH) for attempt in no_real_bluetooth)
    no_real_bluetooth.clear()  # reached on purpose
    await link.close()
