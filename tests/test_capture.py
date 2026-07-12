"""Tests for the diagnostic capture helpers (capture.py)."""
from __future__ import annotations

import logging

from bleak.exc import BleakError
from govee_ble_local import Capability, DeviceState, Segment, Zone

from custom_components.govee_ble_local.capture import (
    LogCapture,
    async_run_self_test,
    session_capture,
)

from .conftest import make_device
from .const import ADDRESS

_FRAME_LINE = f"{ADDRESS} tx switch plain=3301 wire=3301 enc=none"


def test_session_capture_buffers_frames_and_restores_level() -> None:
    """The context opens DEBUG on the govee logger, buffers a frames line, and
    restores the previous level afterwards."""
    logger = logging.getLogger("govee_ble_local")
    logger.setLevel(logging.WARNING)

    with session_capture() as buffer:
        assert logger.level == logging.DEBUG
        logging.getLogger("govee_ble_local.frames").debug(_FRAME_LINE)

    assert logger.level == logging.WARNING
    frames = [r for r in buffer.buffer if r["logger"] == "govee_ble_local.frames"]
    assert any("plain=3301" in r["message"] for r in frames)


def test_log_capture_is_address_scoped() -> None:
    """The always-on buffer keeps only WARNING+ lines mentioning this device."""
    capture = LogCapture(ADDRESS)
    try:
        logger = logging.getLogger("govee_ble_local")
        logger.warning("%s: unrecognised RX frame aa1c", ADDRESS)
        logger.warning("AA:BB:CC:DD:EE:99: someone else's problem")
        records = capture.records()
        assert any("unrecognised RX frame" in r for r in records)
        assert not any("someone else" in r for r in records)
    finally:
        capture.detach()


async def test_self_test_full_surface() -> None:
    """A full-capability device exercises every capability (incl. scenes)."""
    device = make_device()  # H60A6: power/brightness/rgb/cct/scenes/segments + zones

    report = await async_run_self_test(device)

    names = [s["step"] for s in report["steps"]]
    assert "power_on" in names
    assert "brightness" in names
    assert "rgb" in names
    assert "color_temp" in names
    assert "segment_0_rgb" in names
    assert any(n.startswith("zone_power_on:") for n in names)
    assert any(n.startswith("zone_power_off:") for n in names)
    assert any(n.startswith("zone_rgb:") for n in names)
    # Scenes exercised for a SCENES-capable device.
    assert [n for n in names if n.startswith("scene:")]
    device.set_scene_by_name.assert_awaited()
    device.set_rgb.assert_awaited()
    assert "segment_0_brightness" in names
    # No read-back on the mock -> readback_ok stays None, so every acked step is ok.
    assert report["ok"] is True
    assert report["sku"] == "H60A6"


async def test_self_test_report_identifies_the_device() -> None:
    """The report carries the device's address, so results from multiple
    devices (e.g. via the capture_session service targeting several at once)
    are never ambiguous - not even for two units of the same SKU."""
    device = make_device()
    report = await async_run_self_test(device)
    assert report["address"] == ADDRESS


async def test_self_test_restores_original_state() -> None:
    """The pre-test power/brightness/colour is re-applied as the final commands."""
    device = make_device()
    device.state = DeviceState(is_on=True, brightness=40, rgb_color=(1, 2, 3))

    await async_run_self_test(device)

    # Restore runs last: colour, then brightness, then power.
    device.set_rgb.assert_awaited_with((1, 2, 3))
    device.set_brightness.assert_awaited_with(40)
    device.set_power.assert_awaited_with(True)


async def test_self_test_restores_zones_segments_and_scene() -> None:
    """The test actively changes zone power/colour, segment 0's colour, and the
    active scene - all of it must be restored, not just the whole-fixture
    power/brightness/colour fields."""
    zones = (
        Zone("main", power_index=0, segments=(12,)),
        Zone("background", power_index=1, segments=tuple(range(12))),
    )
    device = make_device(zones=zones)
    device.state = DeviceState(
        is_on=True,
        scene_code=42,
        segments=[
            Segment(index=12, rgb=(10, 20, 30), brightness=70),  # main
            Segment(index=0, rgb=(40, 50, 60), brightness=None),  # background + segment_0
        ],
        zone_power={0: True, 1: False},
    )
    device.zone_is_on = lambda name: {"main": True, "background": False}.get(name)

    await async_run_self_test(device)

    # Zone colour + power restored per zone.
    device.set_zone_rgb.assert_any_await("main", (10, 20, 30))
    device.set_zone_rgb.assert_any_await("background", (40, 50, 60))
    device.set_zone_power.assert_any_await("main", True)
    device.set_zone_power.assert_any_await("background", False)
    # Segment 0's original colour/brightness restored.
    device.set_segment_rgb.assert_any_await([0], (40, 50, 60))
    # The originally-active scene is restored (not left on whatever the last
    # scene step activated), and it is the LAST colour-affecting call - the
    # only thing that can run after it is the final power restore.
    device.set_scene.assert_awaited_with(42)
    device.set_power.assert_awaited_with(True)


async def test_self_test_restores_gradual_flag() -> None:
    """A gradual-capable device (e.g. H61A8) gets its original gradual flag
    restored after the test toggles it."""
    device = make_device(
        capabilities=frozenset(
            {Capability.POWER, Capability.BRIGHTNESS, Capability.RGB, Capability.SCENES}
        ),
        zones=(),
        scene_names=[],
    )
    device.profile.gradual = True
    device.state = DeviceState(is_on=True, gradual=True)

    report = await async_run_self_test(device)

    gradual_step = next(s for s in report["steps"] if s["step"] == "gradual")
    assert gradual_step["acked"] is True
    # Toggled to False during the test, then restored back to True.
    device.set_gradual.assert_any_await(False)
    device.set_gradual.assert_awaited_with(True)


async def test_self_test_skips_gradual_when_unsupported() -> None:
    """A non-gradual-capable device (the H60A6 default mock) never calls
    set_gradual at all."""
    device = make_device()  # profile.gradual = False by default
    report = await async_run_self_test(device)
    assert not [s for s in report["steps"] if s["step"] == "gradual"]
    device.set_gradual.assert_not_awaited()


async def test_self_test_marks_command_failure() -> None:
    """A command that raises a BLE error is recorded as not-acked and fails the run."""
    device = make_device()
    device.set_rgb.side_effect = BleakError("no ack")

    report = await async_run_self_test(device)

    rgb_step = next(s for s in report["steps"] if s["step"] == "rgb")
    assert rgb_step["acked"] is False
    assert rgb_step["ok"] is False
    assert "no ack" in rgb_step["error"]
    assert report["ok"] is False


async def test_self_test_skips_unsupported_capabilities() -> None:
    """A plug (POWER only, no scenes/segments/zones) runs only the power step."""
    device = make_device(
        capabilities=frozenset({Capability.POWER}), zones=(), scene_names=[], sku="H5083"
    )
    report = await async_run_self_test(device)

    names = [s["step"] for s in report["steps"]]
    assert names == ["power_on", "power_off"]
    assert not [n for n in names if n.startswith("scene:")]
    device.set_scene_by_name.assert_not_awaited()


async def test_self_test_scene_readback() -> None:
    """When the device reports the active scene, the scene step's read-back passes."""
    device = make_device(scene_names=["Sunrise"])
    device.active_scene = "Sunrise"
    report = await async_run_self_test(device)
    scene_step = next(s for s in report["steps"] if s["step"] == "scene:Sunrise")
    assert scene_step["readback_ok"] is True
    assert scene_step["ok"] is True
