"""Tests for the self-test runner, session capture and the warning ring buffer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from dataclasses import replace
import json
import logging
from typing import Any
from unittest.mock import DEFAULT, AsyncMock, Mock, call

from govee_ble_local import (
    Capability,
    Device,
    GoveeBleAuthError,
    GoveeBleConnectionError,
    Segment,
    Zone,
)
from govee_ble_local.scenes import load_scenes
import pytest

from custom_components.govee_ble_local import capture
from custom_components.govee_ble_local.capture import (
    LogCapture,
    SelfTestBusyError,
    _sanitize_frames,
    async_run_self_test,
    session_capture,
)

from .conftest import make_device
from .const import ADDRESS, PLUG_ADDRESS, PLUG_NAME

OTHER_ADDRESS = "11:22:33:44:55:66"
ROOT_LOGGER = "govee_ble_local"
FRAMES_LOGGER = "govee_ble_local.frames"
INTEGRATION_LOGGER = "custom_components.govee_ble_local"
AFTERNOON_CODE = load_scenes("H60A6")["Afternoon"].code

H60A6_STEPS = [
    "power_on",
    "brightness",
    "rgb",
    "color_temp",
    "scene:Afternoon",
    "scene:Aurora",
    "scene:Blessing",
    "segment_0_rgb",
    "segment_0_brightness",
    "segment_0_color_temp",
    "zone_power_on:main",
    "zone_rgb:main",
    "zone_color_temp:main",
    "zone_power_off:main",
    "zone_power_on:background",
    "zone_rgb:background",
    "zone_color_temp:background",
    "zone_power_off:background",
    "power_off",
]

_RECORDED = (
    "set_power",
    "set_brightness",
    "set_rgb",
    "set_color_temp",
    "set_scene",
    "set_segment_rgb",
    "set_segment_brightness",
    "set_zone_power",
    "set_zone_rgb",
    "set_gradual",
)


# -- helpers ---------------------------------------------------------------


def _prime(device: Device, *, is_on: bool | None = True, **fields: Any) -> None:
    """Give the device a known pre-test state (what the initial read-back finds)."""
    device.state.is_on = is_on
    for name, value in fields.items():
        setattr(device.state, name, value)


def _record_calls(device: Device) -> Mock:
    """Attach the wrapped command mocks to one manager so call order is observable."""
    manager = Mock()
    for name in _RECORDED:
        manager.attach_mock(getattr(device, name), name)
    return manager


def _calls(manager: Mock) -> list[tuple[str, tuple[Any, ...]]]:
    return [(c[0], c[1]) for c in manager.mock_calls]


def _restore_calls(manager: Mock) -> list[tuple[str, tuple[Any, ...]]]:
    """The calls after the exercise (the power_off step is the first set_power(False))."""
    calls = _calls(manager)
    return calls[calls.index(("set_power", (False,))) + 1 :]


def _fail_on_call(mock: AsyncMock, number: int, error: BaseException) -> None:
    """Make the ``number``-th call (1-based) raise; every other call runs for real."""
    count = 0

    def _side_effect(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal count
        count += 1
        if count == number:
            raise error
        return DEFAULT

    mock.side_effect = _side_effect


def _step(report: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in report["steps"] if s["step"] == name)


@pytest.fixture
def lib_logger() -> Generator[logging.Logger]:
    """The shared library logger at a known level, restored afterwards."""
    logger = logging.getLogger(ROOT_LOGGER)
    saved = (logger.level, logger.propagate, list(logger.handlers))
    logger.setLevel(logging.WARNING)
    logger.propagate = True
    yield logger
    logger.setLevel(saved[0])
    logger.propagate = saved[1]
    logger.handlers[:] = saved[2]


class _Collector(logging.Handler):
    """A root-logger handler recording what Home Assistant's log would receive."""

    def __init__(self) -> None:
        super().__init__(logging.NOTSET)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{record.levelname} {record.getMessage()}")


@pytest.fixture
def root_log(lib_logger: logging.Logger) -> Generator[_Collector]:
    """Collect everything that reaches the root logger's handlers."""
    collector = _Collector()
    root = logging.getLogger()
    root.addHandler(collector)
    yield collector
    root.removeHandler(collector)


# -- full-surface runs ------------------------------------------------------


async def test_full_surface_run_on_h60a6() -> None:
    """Every capability is exercised, read-backs match and the state is restored."""
    device = make_device("H60A6")
    _prime(device)

    report = await async_run_self_test(device)

    assert [s["step"] for s in report["steps"]] == H60A6_STEPS
    assert all(s["acked"] and s["ok"] for s in report["steps"])
    assert report["ok"] is True
    assert report["restored"] is True
    assert report["restore_errors"] == []
    assert report["address"] == ADDRESS
    assert report["sku"] == "H60A6"
    assert report["encryption"] == "none"
    assert report["capabilities"] == [
        "brightness",
        "color_temp",
        "power",
        "rgb",
        "scenes",
        "segments",
    ]
    # Steps with a read-back were verified; colour-temp steps have none.
    assert _step(report, "rgb")["readback_ok"] is True
    assert _step(report, "segment_0_color_temp")["readback_ok"] is None
    assert json.loads(json.dumps(report)) == report


async def test_run_covers_scenes_capped_to_first_three() -> None:
    """Only the first three scenes (catalog order) are activated."""
    device = make_device("H60A6")
    _prime(device)

    report = await async_run_self_test(device)

    scene_steps = [s["step"] for s in report["steps"] if s["step"].startswith("scene:")]
    assert scene_steps == [f"scene:{name}" for name in device.scene_names[:3]]
    assert len(device.scene_names) > 3


async def test_h61a8_exercises_gradual_without_color_temp() -> None:
    """A gradual-capable strip toggles the flag and restores it; no colour-temp steps."""
    device = make_device("H61A8")
    _prime(device, gradual=False)
    # The real H61A8 profile has no per-segment brightness: the library raises
    # GoveeBleNotSupported, which the self-test records as a skipped step.
    report = await async_run_self_test(device)

    names = [s["step"] for s in report["steps"]]
    assert names == [
        "power_on",
        "brightness",
        "rgb",
        *(f"scene:{name}" for name in device.scene_names[:3]),
        "segment_0_rgb",
        "segment_0_brightness",
        "gradual",
        "power_off",
    ]
    assert report["capabilities"] == ["brightness", "power", "rgb", "scenes", "segments"]
    unsupported = _step(report, "segment_0_brightness")
    assert unsupported["acked"] is False
    assert unsupported["skipped"].startswith("not supported:")
    assert unsupported["ok"] is True
    assert report["ok"] is True
    assert report["restored"] is True
    # Toggled on for the test, put back to its original value afterwards.
    assert device.set_gradual.await_args_list == [call(True), call(False)]
    assert device.state.gradual is False


async def test_h61a8_self_test_completes_against_real_library() -> None:
    """The self-test of an H61A8 finishes (an unsupported step is recorded, not fatal)."""
    device = make_device("H61A8")
    _prime(device, gradual=False)

    report = await async_run_self_test(device)

    assert "gradual" in [s["step"] for s in report["steps"]]
    assert report["restored"] is True


async def test_plug_runs_power_steps_only() -> None:
    """A smart plug has no colour or zones: only power on/off are exercised."""
    device = make_device("H5083", address=PLUG_ADDRESS, name=PLUG_NAME)
    _prime(device)

    report = await async_run_self_test(device)

    assert [s["step"] for s in report["steps"]] == ["power_on", "power_off"]
    assert report["capabilities"] == ["power"]
    assert report["ok"] is True
    assert report["restored"] is True
    assert device.state.is_on is True


# -- read-back and step errors ----------------------------------------------


async def test_readback_mismatch_fails_the_step_and_run() -> None:
    """A value the device reports differently from the command fails that step."""
    device = make_device("H60A6")
    _prime(device)
    real_update = device.update.side_effect

    async def _reverting_update() -> Any:
        state = await real_update()
        if state.brightness == capture._TEST_BRIGHTNESS:
            state.brightness = 7  # the device reports something else
        return state

    device.update.side_effect = _reverting_update

    report = await async_run_self_test(device)

    brightness = _step(report, "brightness")
    assert brightness["acked"] is True
    assert brightness["readback_ok"] is False
    assert brightness["ok"] is False
    assert report["ok"] is False
    # Only that step failed.
    assert [s["step"] for s in report["steps"] if not s["ok"]] == ["brightness"]
    assert report["restored"] is True


async def test_ble_error_in_step_is_recorded_not_raised() -> None:
    """A BLE failure marks the step (error text, not acked) and the run carries on."""
    device = make_device("H60A6")
    _prime(device)
    device.set_rgb.side_effect = GoveeBleConnectionError("no slot")

    report = await async_run_self_test(device)

    rgb = _step(report, "rgb")
    assert rgb["acked"] is False
    assert rgb["error"] == "no slot"
    assert rgb["readback_ok"] is None
    assert rgb["ok"] is False
    assert report["ok"] is False
    assert len(report["steps"]) == len(H60A6_STEPS)


async def test_ble_errors_on_reads_are_tolerated() -> None:
    """Failing read-backs (initial and per step) leave steps judged on tracked state."""
    device = make_device("H60A6")
    _prime(device)
    device.update.side_effect = GoveeBleConnectionError("read failed")

    report = await async_run_self_test(device)

    assert device.update.await_count == len(H60A6_STEPS) + 1
    assert report["ok"] is True
    assert _step(report, "brightness")["readback_ok"] is True


async def test_initial_power_unknown_skips_power_off() -> None:
    """With no known starting power, the device is never switched off."""
    device = make_device("H60A6")
    assert device.state.is_on is None

    report = await async_run_self_test(device)

    power_off = _step(report, "power_off")
    assert power_off["skipped"] == "initial power state unknown"
    assert power_off["acked"] is False
    assert power_off["ok"] is True
    assert report["ok"] is True
    assert call(False) not in device.set_power.await_args_list
    # Nor is a power state restored that was never known.
    assert device.set_power.await_args_list == [call(True)]
    device.set_zone_power.assert_awaited()
    assert all(c.args[1] is not None for c in device.set_zone_power.await_args_list)


async def test_unreported_value_leaves_readback_unknown() -> None:
    """A value the device does not report is neither a pass nor a fail of the read-back."""
    device = make_device("H60A6")
    _prime(device)
    real_update = device.update.side_effect

    async def _silent_update() -> Any:
        state = await real_update()
        state.brightness = None  # the device reports no brightness
        return state

    device.update.side_effect = _silent_update

    report = await async_run_self_test(device)

    brightness = _step(report, "brightness")
    assert brightness["acked"] is True
    assert brightness["readback_ok"] is None
    assert brightness["ok"] is True
    assert report["ok"] is True


async def test_profile_without_color_temp_skips_all_colour_temp_steps() -> None:
    """Without the capability there are no whole-device, segment or zone colour-temp steps."""
    device = make_device("H60A6")
    _prime(device)
    device.profile = replace(
        device.profile, capabilities=device.capabilities - {Capability.COLOR_TEMP}
    )

    report = await async_run_self_test(device)

    assert [s["step"] for s in report["steps"]] == [
        name for name in H60A6_STEPS if "color_temp" not in name
    ]
    assert report["ok"] is True


async def test_profile_without_power_runs_zone_steps_and_restores_zones_individually() -> None:
    """With no whole-device power the zones are switched and put back one by one."""
    device = make_device("H60A6")
    _prime(device, zone_power={0: True, 1: False})
    device.profile = replace(device.profile, capabilities=device.capabilities - {Capability.POWER})
    manager = _record_calls(device)

    report = await async_run_self_test(device)

    names = [s["step"] for s in report["steps"]]
    assert "power_on" not in names
    assert "power_off" not in names
    assert "zone_power_on:main" in names
    assert report["restored"] is True
    device.set_power.assert_not_awaited()
    assert _calls(manager)[-2:] == [
        ("set_zone_power", ("main", True)),
        ("set_zone_power", ("background", False)),
    ]


async def test_zone_without_segments_is_only_power_cycled() -> None:
    """A zone with no segments cannot take a colour: only its power is exercised."""
    device = make_device("H60A6")
    _prime(device)
    device.profile = replace(device.profile, zones=(Zone("lone", power_index=0, segments=()),))

    report = await async_run_self_test(device)

    zone_steps = [s["step"] for s in report["steps"] if s["step"].startswith("zone_")]
    assert zone_steps == ["zone_power_on:lone", "zone_power_off:lone"]
    assert report["ok"] is True


# -- restore ----------------------------------------------------------------


async def test_restore_order_colour_then_brightness_then_power() -> None:
    """Colour is put back first, brightness next, power last (with only the differing zone)."""
    device = make_device("H60A6")
    _prime(
        device,
        brightness=40,
        rgb_color=(1, 2, 3),
        zone_power={0: True, 1: False},
    )
    manager = _record_calls(device)

    report = await async_run_self_test(device)

    assert report["restored"] is True
    assert _restore_calls(manager) == [
        ("set_rgb", ((1, 2, 3),)),
        ("set_brightness", (40,)),
        ("set_power", (True,)),
        ("set_zone_power", ("background", False)),
    ]
    assert device.state.brightness == 40
    assert device.state.rgb_color == (1, 2, 3)
    assert device.zone_is_on("main") is True
    assert device.zone_is_on("background") is False


async def test_restore_static_colours_segments_and_zones() -> None:
    """A colour-temperature state is restored along with segment 0 and per-zone colours."""
    device = make_device("H60A6")
    _prime(
        device,
        brightness=70,
        color_temp_kelvin=3000,
        segments=[
            Segment(index=0, rgb=(9, 9, 9), brightness=33),
            Segment(index=12, rgb=(8, 8, 8), brightness=20),
        ],
        zone_power={0: True, 1: True},
    )
    manager = _record_calls(device)

    report = await async_run_self_test(device)

    assert report["restored"] is True
    assert _restore_calls(manager) == [
        ("set_color_temp", (3000,)),
        ("set_segment_rgb", ([0], (9, 9, 9))),
        ("set_segment_brightness", ([0], 33)),
        ("set_zone_rgb", ("main", (8, 8, 8))),
        ("set_zone_rgb", ("background", (9, 9, 9))),
        ("set_brightness", (70,)),
        ("set_power", (True,)),
    ]


async def test_restore_to_off_ends_with_power_off_and_no_zone_power() -> None:
    """A device that was off ends switched off; zones follow the whole-device switch."""
    device = make_device("H60A6")
    _prime(device, is_on=False, brightness=40, zone_power={0: False, 1: False})
    manager = _record_calls(device)

    report = await async_run_self_test(device)

    restore = _restore_calls(manager)
    assert report["restored"] is True
    assert restore[-1] == ("set_power", (False,))
    assert "set_zone_power" not in [name for name, _ in restore]
    assert device.state.is_on is False


async def test_restore_active_scene_skips_zone_and_segment_colours() -> None:
    """A scene repaints every segment, so restoring it never writes colours over it."""
    device = make_device("H60A6")
    _prime(
        device,
        scene_code=AFTERNOON_CODE,
        segments=[Segment(index=0, rgb=(9, 9, 9), brightness=33)],
        zone_power={0: True, 1: True},
    )
    manager = _record_calls(device)

    report = await async_run_self_test(device)

    assert report["restored"] is True
    assert _restore_calls(manager) == [
        ("set_scene", (AFTERNOON_CODE,)),
        ("set_power", (True,)),
    ]
    assert device.active_scene == "Afternoon"


async def test_restore_failure_does_not_abandon_later_actions() -> None:
    """One failing restore action is reported while the rest are still attempted."""
    device = make_device("H60A6")
    _prime(
        device,
        brightness=40,
        segments=[
            Segment(index=0, rgb=(1, 1, 1), brightness=5),
            Segment(index=12, rgb=(2, 2, 2), brightness=5),
        ],
        zone_power={0: True, 1: True},
    )
    manager = _record_calls(device)
    # Two exercise calls (main, background) then the two restore calls: fail the last.
    _fail_on_call(device.set_zone_rgb, 4, GoveeBleConnectionError("boom"))

    report = await async_run_self_test(device)

    assert report["ok"] is True  # the steps themselves all passed
    assert report["restored"] is False
    assert report["restore_errors"] == ["zone_rgb:background: boom"]
    assert _restore_calls(manager)[-2:] == [
        ("set_brightness", (40,)),
        ("set_power", (True,)),
    ]


async def test_restore_failure_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    """An incomplete restore is logged at WARNING with the device address."""
    device = make_device("H60A6")
    _prime(device, brightness=40)
    # power_on, power_off, then the restore's set_power(True) is the third call.
    _fail_on_call(device.set_power, 3, GoveeBleConnectionError("link lost"))

    with caplog.at_level(logging.WARNING, logger=capture.__name__):
        report = await async_run_self_test(device)

    assert report["restored"] is False
    assert report["restore_errors"] == ["power: link lost"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert ADDRESS in message
    assert "could not fully restore" in message
    assert "power: link lost" in message


# -- exceptions during a run ------------------------------------------------


async def test_unexpected_error_propagates_after_restore() -> None:
    """A non-BLE exception aborts the run, but the power state is still put back."""
    device = make_device("H60A6")
    _prime(device, is_on=False)
    device.set_brightness.side_effect = RuntimeError("bug")

    with pytest.raises(RuntimeError, match="bug"):
        await async_run_self_test(device)

    # power_on (exercise), then the restore's final word: the original state.
    assert device.set_power.await_args_list == [call(True), call(False)]
    assert device.state.is_on is False
    assert not capture._SELF_TEST_LOCK.locked()


async def test_auth_error_aborts_without_restore() -> None:
    """A rejected secret is re-raised and nothing more is sent (restore would be futile)."""
    device = make_device("H60A6")
    _prime(device, brightness=40, rgb_color=(1, 2, 3))
    device.set_brightness.side_effect = GoveeBleAuthError("rejected")

    with pytest.raises(GoveeBleAuthError):
        await async_run_self_test(device)

    assert device.set_power.await_args_list == [call(True)]  # only the exercise's
    device.set_rgb.assert_not_awaited()
    device.set_brightness.assert_awaited_once_with(capture._TEST_BRIGHTNESS)
    assert not capture._SELF_TEST_LOCK.locked()


async def test_auth_error_on_initial_read_is_raised() -> None:
    """A rejected secret during the very first read aborts before any step runs."""
    device = make_device("H60A6")
    device.update.side_effect = GoveeBleAuthError("rejected")

    with pytest.raises(GoveeBleAuthError):
        await async_run_self_test(device)

    device.set_power.assert_not_awaited()


async def test_auth_error_on_step_readback_is_raised() -> None:
    """A rejected secret surfacing in a step's read-back also aborts the run."""
    device = make_device("H60A6")
    real_update = device.update.side_effect
    calls = 0

    async def _update() -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:  # initial read passes; the first step's read-back fails
            raise GoveeBleAuthError("rejected")
        return await real_update()

    device.update.side_effect = _update

    with pytest.raises(GoveeBleAuthError):
        await async_run_self_test(device)

    device.set_power.assert_awaited_once_with(True)


async def test_concurrent_run_is_rejected_as_busy() -> None:
    """A second self-test while one is running raises SelfTestBusyError; the first finishes."""
    device = make_device("H60A6")
    _prime(device)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _gated_power(on: bool) -> Any:
        started.set()
        await release.wait()
        return DEFAULT

    device.set_power.side_effect = _gated_power
    first = asyncio.create_task(async_run_self_test(device))
    await started.wait()

    other = make_device("H60A6", address=OTHER_ADDRESS)
    with pytest.raises(SelfTestBusyError):
        await async_run_self_test(other)
    other.update.assert_not_awaited()

    release.set()
    report = await first
    assert report["ok"] is True
    assert not capture._SELF_TEST_LOCK.locked()
    # The lock is free again.
    assert (await async_run_self_test(other))["address"] == OTHER_ADDRESS


# -- frame capture and sanitisation -----------------------------------------


def _frame_lines(address: str) -> Callable[..., Any]:
    """A side effect that logs frame lines the way the library does."""

    def _emit(*_args: Any, **_kwargs: Any) -> Any:
        frames = logging.getLogger(FRAMES_LOGGER)
        frames.debug("%s tx power plain=3301 wire=deadbeef enc=none", address)
        frames.debug("%s tx cipher plain= wire=abcd enc=e7", address)
        frames.debug("%s rx status plain=aa wire=0f0f enc=none", OTHER_ADDRESS)
        return DEFAULT

    return _emit


async def test_secret_device_frames_drop_wire_and_handshake_lines() -> None:
    """For a secret-gated device the report carries no ciphertext or handshake lines."""
    device = make_device("H5083", address=PLUG_ADDRESS, name=PLUG_NAME)
    _prime(device)
    device.set_power.side_effect = _frame_lines(PLUG_ADDRESS)

    report = await async_run_self_test(device)

    assert report["frames"]
    assert not [f for f in report["frames"] if " wire=" in f or "cipher plain=" in f]
    assert set(report["frames"]) == {f"{PLUG_ADDRESS} tx power plain=3301 enc=none"}


async def test_plain_device_frames_keep_wire_field() -> None:
    """Without a secret nothing is stripped; only this device's lines are kept."""
    device = make_device("H60A6")
    _prime(device)
    device.set_power.side_effect = _frame_lines(ADDRESS)

    report = await async_run_self_test(device)

    assert f"{ADDRESS} tx power plain=3301 wire=deadbeef enc=none" in report["frames"]
    assert f"{ADDRESS} tx cipher plain= wire=abcd enc=e7" in report["frames"]
    assert not [f for f in report["frames"] if OTHER_ADDRESS in f]


async def test_log_lines_are_separate_from_frames_and_address_scoped() -> None:
    """Non-frame library records land in ``log`` as "LEVEL message", for this device only."""
    device = make_device("H60A6")
    _prime(device)

    def _emit(*_args: Any, **_kwargs: Any) -> Any:
        session = logging.getLogger("govee_ble_local.session")
        session.debug("%s handshake done", ADDRESS)
        session.warning("%s slow ack", ADDRESS)
        session.debug("%s handshake done", OTHER_ADDRESS)
        return DEFAULT

    device.set_power.side_effect = _emit

    report = await async_run_self_test(device)

    assert f"DEBUG {ADDRESS} handshake done" in report["log"]
    assert f"WARNING {ADDRESS} slow ack" in report["log"]
    assert not [line for line in report["log"] if OTHER_ADDRESS in line]
    assert report["frames"] == []


def test_sanitize_frames_unit() -> None:
    """Secret-gated lines lose wire= and handshake lines; others pass through untouched."""
    lines = [
        "A tx power plain=3301 wire=deadbeef enc=none",
        "A tx cipher plain= wire=abcd enc=e7",
        "A rx status plain=aa wire= enc=none",
    ]
    assert _sanitize_frames(lines, requires_secret=False) == lines
    assert _sanitize_frames(lines, requires_secret=True) == [
        "A tx power plain=3301 enc=none",
        "A rx status plain=aa enc=none",
    ]
    assert _sanitize_frames([], requires_secret=True) == []


# -- session_capture --------------------------------------------------------


async def test_session_capture_raises_level_and_keeps_debug_out_of_root(
    lib_logger: logging.Logger, root_log: _Collector
) -> None:
    """Debug records are captured but never reach HA's log; warnings still pass through."""
    child = logging.getLogger("govee_ble_local.session")
    handlers_before = list(lib_logger.handlers)

    with session_capture() as ring:
        assert lib_logger.level == logging.DEBUG
        assert lib_logger.propagate is False
        child.debug("quiet %s", 1)
        child.warning("loud %s", 2)

    assert [(r["level"], r["message"]) for r in ring.buffer] == [
        ("DEBUG", "quiet 1"),
        ("WARNING", "loud 2"),
    ]
    assert ring.buffer[0]["logger"] == "govee_ble_local.session"
    assert root_log.lines == ["WARNING loud 2"]
    assert lib_logger.level == logging.WARNING
    assert lib_logger.propagate is True
    assert lib_logger.handlers == handlers_before


async def test_session_capture_restores_state_when_body_raises(
    lib_logger: logging.Logger,
) -> None:
    """Level, propagation and handlers come back even if the body raises."""
    handlers_before = list(lib_logger.handlers)

    with pytest.raises(RuntimeError), session_capture():
        raise RuntimeError

    assert lib_logger.level == logging.WARNING
    assert lib_logger.propagate is True
    assert lib_logger.handlers == handlers_before


async def test_session_capture_keeps_level_changed_meanwhile(
    lib_logger: logging.Logger,
) -> None:
    """If something re-configured the level during the capture, it is not overwritten."""
    with session_capture():
        lib_logger.setLevel(logging.ERROR)

    assert lib_logger.level == logging.ERROR


async def test_session_capture_filters_by_address_and_bounds_buffer(
    lib_logger: logging.Logger,
) -> None:
    """Only records mentioning the address are kept, in a bounded ring."""
    child = logging.getLogger("govee_ble_local.session")

    with session_capture(maxlen=2, address=ADDRESS) as ring:
        child.debug("%s one", ADDRESS)
        child.debug("%s elsewhere", OTHER_ADDRESS)
        child.debug("%s two", ADDRESS)
        child.debug("%s three", ADDRESS)

    assert [r["message"] for r in ring.buffer] == [f"{ADDRESS} two", f"{ADDRESS} three"]


# -- LogCapture -------------------------------------------------------------


def _router_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if isinstance(h, capture._WarningRouter)]


async def test_log_capture_keeps_only_warnings_for_its_address(
    lib_logger: logging.Logger,
) -> None:
    """Only WARNING+ lines mentioning the address are kept, as "LEVEL message"."""
    lib_logger.setLevel(logging.DEBUG)  # let INFO reach the handler to prove it filters
    log_capture = LogCapture(ADDRESS)
    child = logging.getLogger("govee_ble_local.session")

    child.warning("%s rejected", ADDRESS)
    child.error("%s broke", ADDRESS)
    child.info("%s fine", ADDRESS)
    child.debug("%s detail", ADDRESS)
    child.warning("%s elsewhere", OTHER_ADDRESS)
    child.warning("no address at all")

    assert log_capture.records() == [f"WARNING {ADDRESS} rejected", f"ERROR {ADDRESS} broke"]
    # Returns a copy, oldest first.
    log_capture.records().clear()
    assert len(log_capture.records()) == 2


async def test_log_capture_keeps_the_integrations_own_warnings(
    lib_logger: logging.Logger,
) -> None:
    """The integration's WARNING lines (command and secret rejections) are captured too.

    Each record is kept once, whichever of the two loggers it comes through, and
    detaching the last capture leaves no handler on either.
    """
    integration_logger = logging.getLogger(INTEGRATION_LOGGER)
    log_capture = LogCapture(ADDRESS)
    assert len(_router_handlers(integration_logger)) == 1

    logging.getLogger(f"{INTEGRATION_LOGGER}.entity").warning("%s rejected a command", ADDRESS)
    logging.getLogger(f"{INTEGRATION_LOGGER}.entity").info("%s just info", ADDRESS)
    logging.getLogger("govee_ble_local.session").warning("%s unrecognised frame", ADDRESS)

    assert log_capture.records() == [
        f"WARNING {ADDRESS} rejected a command",
        f"WARNING {ADDRESS} unrecognised frame",
    ]
    log_capture.detach()
    assert _router_handlers(integration_logger) == []
    assert _router_handlers(lib_logger) == []


async def test_log_capture_ring_buffer_is_bounded(lib_logger: logging.Logger) -> None:
    """The newest ``maxlen`` lines are kept; older ones are evicted."""
    log_capture = LogCapture(ADDRESS, maxlen=3)
    child = logging.getLogger("govee_ble_local.session")

    for i in range(5):
        child.warning("%s line %d", ADDRESS, i)

    assert log_capture.records() == [f"WARNING {ADDRESS} line {i}" for i in (2, 3, 4)]


async def test_log_captures_share_one_handler_and_route_per_address(
    lib_logger: logging.Logger,
) -> None:
    """Several captures use a single handler, each receiving only its own device's lines."""
    first = LogCapture(ADDRESS)
    second = LogCapture(OTHER_ADDRESS)
    third = LogCapture(PLUG_ADDRESS)
    child = logging.getLogger("govee_ble_local.session")
    assert len(_router_handlers(lib_logger)) == 1

    child.warning("%s one", ADDRESS)
    child.warning("%s two", OTHER_ADDRESS)

    assert first.records() == [f"WARNING {ADDRESS} one"]
    assert second.records() == [f"WARNING {OTHER_ADDRESS} two"]
    assert third.records() == []

    first.detach()
    second.detach()
    assert len(_router_handlers(lib_logger)) == 1  # still needed by the third
    child.warning("%s three", PLUG_ADDRESS)
    assert third.records() == [f"WARNING {PLUG_ADDRESS} three"]
    assert first.records() == [f"WARNING {ADDRESS} one"]  # no longer appended to

    third.detach()
    assert _router_handlers(lib_logger) == []


async def test_log_capture_detach_is_idempotent(lib_logger: logging.Logger) -> None:
    """Detaching twice is harmless and never removes another capture's handler."""
    first = LogCapture(ADDRESS)
    second = LogCapture(OTHER_ADDRESS)

    first.detach()
    first.detach()
    assert len(_router_handlers(lib_logger)) == 1

    second.detach()
    second.detach()
    assert _router_handlers(lib_logger) == []


async def test_restore_skips_commands_the_device_cannot_do() -> None:
    """Restoring a value through an unsupported command is not a restore failure."""
    device = make_device("H61A8")
    _prime(device, gradual=False, segments=[Segment(index=0, rgb=(9, 9, 9), brightness=40)])

    report = await async_run_self_test(device)

    assert call([0], 40) in device.set_segment_brightness.await_args_list
    assert report["restored"] is True
    assert report["restore_errors"] == []
