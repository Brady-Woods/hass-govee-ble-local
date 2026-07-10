"""Tests for the diagnostic capture helpers (capture.py)."""
from __future__ import annotations

import logging

from bleak.exc import BleakError
from govee_ble_local import Capability, DeviceState

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
    assert any(n.startswith("zone_power:") for n in names)
    assert any(n.startswith("zone_rgb:") for n in names)
    # Scenes exercised for a SCENES-capable device.
    assert [n for n in names if n.startswith("scene:")]
    device.set_scene_by_name.assert_awaited()
    device.set_rgb.assert_awaited()
    # No read-back on the mock -> readback_ok stays None, so every acked step is ok.
    assert report["ok"] is True
    assert report["sku"] == "H60A6"


async def test_self_test_restores_original_state() -> None:
    """The pre-test power/brightness/colour is re-applied as the final commands."""
    device = make_device()
    device.state = DeviceState(is_on=True, brightness=40, rgb_color=(1, 2, 3))

    await async_run_self_test(device)

    # Restore runs last: colour, then brightness, then power.
    device.set_rgb.assert_awaited_with((1, 2, 3))
    device.set_brightness.assert_awaited_with(40)
    device.set_power.assert_awaited_with(True)


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
    assert names == ["power_on"]
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
