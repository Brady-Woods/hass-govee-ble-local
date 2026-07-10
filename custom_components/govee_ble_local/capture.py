"""Diagnostic capture helpers for the Govee BLE Local integration.

Consumes the library's logging surfaces (v3):
  * the ``govee_ble_local.*`` level hierarchy (ERROR/WARNING/INFO/DEBUG), and
  * the filesystem-free ``govee_ble_local.frames`` logger (one line per TX/RX frame,
    ``"<addr> <dir> <label> plain=<hex> wire=<hex> enc=<mode>"``).

Two consumers:
  * :class:`LogCapture` — an always-on, address-scoped ring buffer of WARNING+ records
    surfaced in the downloadable diagnostics (closes the library's silent-drop gap: an
    unrecognised RX frame / empty status / rejection now shows up).
  * :func:`async_run_self_test` — an in-HA device integration test that exercises the full
    capability surface with per-step ACK/read-back tracking, restores the original state, and
    returns the captured frames (feed to ``govee-ble-analyze --from-frames-log``).
"""
from __future__ import annotations

import logging
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

from bleak.exc import BleakError
from govee_ble_local import Capability, Device, GoveeBleError

_LOGGER = logging.getLogger(__name__)

_ROOT_LOGGER = "govee_ble_local"
_FRAMES_LOGGER = "govee_ble_local.frames"

# BLE errors that mean "command didn't land" rather than a bug.
_BLE_ERRORS = (BleakError, GoveeBleError, TimeoutError)

# Cap scene coverage: scene upload is the most failure-prone path, but activating every scene
# would be slow, so exercise a representative sample.
_MAX_SCENES = 3


class _RingHandler(logging.Handler):
    """A bounded in-memory log sink (keeps the last ``maxlen`` records)."""

    def __init__(self, maxlen: int) -> None:
        super().__init__()
        self.buffer: deque[dict[str, str]] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(
                {
                    "logger": record.name,
                    "level": record.levelname,
                    "message": record.getMessage(),
                }
            )
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)


class LogCapture:
    """Always-on, address-scoped WARNING+ ring buffer for one device.

    Attached to the ``govee_ble_local`` logger for the life of the config entry; cheap because
    the handler only accepts WARNING and above. ``records()`` returns just the lines that
    mention this device's address, for the diagnostics dump.
    """

    def __init__(self, address: str, *, maxlen: int = 200) -> None:
        self._address = address
        self._handler = _RingHandler(maxlen)
        self._handler.setLevel(logging.WARNING)
        self._logger = logging.getLogger(_ROOT_LOGGER)
        self._logger.addHandler(self._handler)

    def records(self) -> list[str]:
        return [
            f"{r['level']} {r['message']}"
            for r in self._handler.buffer
            if self._address in r["message"]
        ]

    def detach(self) -> None:
        self._logger.removeHandler(self._handler)


@contextmanager
def session_capture(*, maxlen: int = 4000) -> Iterator[_RingHandler]:
    """Temporarily capture the full ``govee_ble_local`` flow + frame stream.

    Raises the ``govee_ble_local`` logger to DEBUG (the child ``.frames`` logger inherits it,
    so its ``isEnabledFor`` guard opens and frames propagate up) and attaches a fresh ring
    buffer; both the level and the handler are restored on exit.
    """
    logger = logging.getLogger(_ROOT_LOGGER)
    handler = _RingHandler(maxlen)
    handler.setLevel(logging.DEBUG)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _match(actual: Any, expected: Any) -> bool | None:
    """Tri-state read-back check: None when the device reports nothing (unknown), else
    whether the read-back value equals what we commanded."""
    if actual is None:
        return None
    return bool(actual == expected)


def _snapshot(device: Device) -> dict[str, Any]:
    state = device.state
    return {
        "is_on": state.is_on,
        "brightness": state.brightness,
        "rgb_color": state.rgb_color,
        "color_temp_kelvin": state.color_temp_kelvin,
    }


async def _restore(device: Device, snap: dict[str, Any]) -> None:
    """Best-effort return to the pre-test state; never raises."""
    caps = device.capabilities
    try:
        if snap["color_temp_kelvin"] is not None and Capability.COLOR_TEMP in caps:
            await device.set_color_temp(snap["color_temp_kelvin"])
        elif snap["rgb_color"] is not None and Capability.RGB in caps:
            await device.set_rgb(snap["rgb_color"])
        if snap["brightness"] is not None and Capability.BRIGHTNESS in caps:
            await device.set_brightness(snap["brightness"])
        if snap["is_on"] is not None and Capability.POWER in caps:
            await device.set_power(snap["is_on"])
    except _BLE_ERRORS as err:
        _LOGGER.debug("%s: self-test restore failed: %s", device.address, err)


async def async_run_self_test(device: Device) -> dict[str, Any]:
    """Exercise the device's full capability surface and capture the BLE session.

    Returns a report ``{ok, sku, encryption, capabilities, steps, frames, log}``. Each step
    records ``acked`` (command returned without a BLE error) and ``readback_ok`` (tri-state:
    None when the device has no read-back). Restores the original state at the end.
    """
    caps = device.capabilities
    steps: list[dict[str, Any]] = []

    with session_capture() as capture:
        try:
            await device.update()
        except _BLE_ERRORS as err:
            _LOGGER.debug("%s: self-test initial read failed: %s", device.address, err)
        snap = _snapshot(device)

        async def step(
            name: str,
            action: Callable[[], Awaitable[None]],
            verify: Callable[[], bool | None] | None = None,
        ) -> None:
            entry: dict[str, Any] = {
                "step": name,
                "acked": False,
                "readback_ok": None,
                "error": None,
            }
            try:
                await action()
                entry["acked"] = True
                try:
                    await device.update()
                except _BLE_ERRORS:
                    pass
                if verify is not None:
                    entry["readback_ok"] = verify()
            except _BLE_ERRORS as err:
                entry["error"] = str(err)
            entry["ok"] = entry["acked"] and entry["readback_ok"] is not False
            steps.append(entry)

        if Capability.POWER in caps:
            await step("power_on", lambda: device.set_power(True),
                       lambda: _match(device.state.is_on, True))
        if Capability.BRIGHTNESS in caps:
            await step("brightness", lambda: device.set_brightness(60),
                       lambda: _match(device.state.brightness, 60))
        if Capability.RGB in caps:
            await step("rgb", lambda: device.set_rgb((0, 128, 255)),
                       lambda: _match(device.state.rgb_color, (0, 128, 255)))
        if Capability.COLOR_TEMP in caps:
            kelvin = _mid_kelvin(device)
            await step("color_temp", lambda: device.set_color_temp(kelvin),
                       lambda: _match(device.state.color_temp_kelvin, kelvin))
        if Capability.SCENES in caps:
            for name in list(device.scene_names)[:_MAX_SCENES]:

                def _scene(n: str = name) -> Awaitable[None]:
                    return device.set_scene_by_name(n)

                def _scene_ok(n: str = name) -> bool | None:
                    return _match(device.active_scene, n)

                await step(f"scene:{name}", _scene, _scene_ok)
        if Capability.SEGMENTS in caps and device.profile.segments:
            await step("segment_0_rgb", lambda: device.set_segment_rgb([0], (255, 0, 0)))
            if Capability.COLOR_TEMP in caps:
                await step(
                    "segment_0_color_temp",
                    lambda: device.set_segment_color_temp([0], _mid_kelvin(device)),
                )
        for zone in device.zones:

            def _zone_on(name: str = zone.name) -> Awaitable[None]:
                return device.set_zone_power(name, True)

            def _zone_on_ok(name: str = zone.name) -> bool | None:
                return _match(device.zone_is_on(name), True)

            await step(f"zone_power:{zone.name}", _zone_on, _zone_on_ok)
            if zone.segments:

                def _zone_rgb(name: str = zone.name) -> Awaitable[None]:
                    return device.set_zone_rgb(name, (0, 255, 0))

                await step(f"zone_rgb:{zone.name}", _zone_rgb)
                if Capability.COLOR_TEMP in caps:

                    def _zone_cct(name: str = zone.name) -> Awaitable[None]:
                        return device.set_zone_color_temp(name, _mid_kelvin(device))

                    await step(f"zone_color_temp:{zone.name}", _zone_cct)

        await _restore(device, snap)

    frames = [r["message"] for r in capture.buffer if r["logger"] == _FRAMES_LOGGER]
    log = [
        f"{r['level']} {r['message']}"
        for r in capture.buffer
        if r["logger"] != _FRAMES_LOGGER
    ]
    ok = all(s["ok"] for s in steps) if steps else True
    return {
        "ok": ok,
        "sku": device.sku,
        "encryption": device.profile.encryption.value,
        "capabilities": sorted(c.value for c in caps),
        "steps": steps,
        "frames": frames,
        "log": log,
    }


def _mid_kelvin(device: Device) -> int:
    lo = device.min_kelvin or 2700
    hi = device.max_kelvin or 6500
    return (lo + hi) // 2
