"""Diagnostic capture helpers for the Govee BLE Local integration.

Consumes the library's logging surfaces (v3):
  * the ``govee_ble_local.*`` level hierarchy (ERROR/WARNING/INFO/DEBUG), and
  * the filesystem-free ``govee_ble_local.frames`` logger (one line per TX/RX frame,
    ``"<addr> <dir> <label> plain=<hex> wire=<hex> enc=<mode>"``).

Two consumers:
  * :class:`LogCapture` — an always-on, address-scoped ring buffer of WARNING+ records
    surfaced in the downloadable diagnostics (closes the library's silent-drop gap). It
    listens on the library's logger, which logs unrecognised RX frames and empty status
    reads at WARNING, and on this integration's own: the library logs a rejected command
    or secret only at DEBUG and raises it (``GoveeBleCommandRejected`` /
    ``GoveeBleAuthError``), so the integration logs those at WARNING itself.
  * :func:`async_run_self_test` — an in-HA device integration test that exercises the full
    capability surface (power, brightness, RGB, colour temperature, scenes, segments, zones,
    the gradual flag) with per-step ACK/read-back tracking. It captures the device's full
    pre-test state and restores it afterwards - even when the test fails part-way - and
    returns the captured frames (feed to ``govee-ble-analyze --from-frames-log``) plus which
    physical device (``address``) it ran against.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from functools import partial
import logging
import re
from typing import Any, TypedDict

from govee_ble_local import (
    Capability,
    Device,
    DeviceState,
    GoveeBleAuthError,
    GoveeBleNotSupported,
    Segment,
    Zone,
)

from .const import DEFAULT_MAX_KELVIN, DEFAULT_MIN_KELVIN
from .helpers import BLE_ERRORS

_LOGGER = logging.getLogger(__name__)

_ROOT_LOGGER = "govee_ble_local"
_FRAMES_LOGGER = "govee_ble_local.frames"
# Where LogCapture listens: the library, and this integration (custom_components.govee_ble_local),
# which logs the command and secret rejections.
_CAPTURED_LOGGERS = (_ROOT_LOGGER, __package__)

# Cap scene coverage: scene upload is the most failure-prone path, but activating every scene
# would be slow, so exercise a representative sample.
_MAX_SCENES = 3

# Fixed test values (distinct from typical user settings so read-back is meaningful).
_TEST_BRIGHTNESS = 60
_TEST_RGB = (0, 128, 255)
_TEST_SEGMENT_RGB = (255, 0, 0)
_TEST_SEGMENT_BRIGHTNESS = 50
_TEST_ZONE_RGB = (0, 255, 0)

# One self-test at a time: session_capture raises the shared library logger to
# DEBUG for the run, which concurrent runs would interleave and clobber.
_SELF_TEST_LOCK = asyncio.Lock()

_WIRE_FIELD = re.compile(r" wire=[0-9a-f]*", re.IGNORECASE)


class SelfTestBusyError(Exception):
    """Another self-test is already running."""


class _WarningRouter(logging.Handler):
    """One WARNING+ handler for every :class:`LogCapture`.

    Formats each record once and appends it to the buffer of every capture whose
    address the message mentions (instead of one handler per device each
    formatting and discarding every other device's records).
    """

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.captures: dict[LogCapture, str] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            line = f"{record.levelname} {message}"
            for capture, address in self.captures.items():
                if address in message:
                    capture.buffer.append(line)
        except Exception:  # noqa: BLE001  # pragma: no cover - logging must never raise
            self.handleError(record)


_ROUTER = _WarningRouter()


class LogCapture:
    """Always-on, address-scoped WARNING+ ring buffer for one device.

    Registered on the ``govee_ble_local`` logger and this integration's own for the
    life of the config entry; keeps only lines mentioning this device's address (so a
    noisy neighbour can't evict them).
    """

    def __init__(self, address: str, *, maxlen: int = 200) -> None:
        """Start capturing WARNING+ records that mention ``address``."""
        self.buffer: deque[str] = deque(maxlen=maxlen)
        if not _ROUTER.captures:
            for name in _CAPTURED_LOGGERS:
                logging.getLogger(name).addHandler(_ROUTER)
        _ROUTER.captures[self] = address

    def records(self) -> list[str]:
        """Return the captured ``"LEVEL message"`` lines, oldest first."""
        return list(self.buffer)

    def detach(self) -> None:
        """Stop capturing (idempotent)."""
        _ROUTER.captures.pop(self, None)
        if not _ROUTER.captures:
            for name in _CAPTURED_LOGGERS:
                logging.getLogger(name).removeHandler(_ROUTER)


class _RingHandler(logging.Handler):
    """A bounded in-memory sink of ``{logger, level, message}`` records.

    Optionally keeps only records that mention ``address``.
    """

    def __init__(self, maxlen: int, address: str | None = None) -> None:
        super().__init__(logging.DEBUG)
        self.buffer: deque[dict[str, str]] = deque(maxlen=maxlen)
        self._address = address

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if self._address is not None and self._address not in message:
                return
            self.buffer.append(
                {"logger": record.name, "level": record.levelname, "message": message}
            )
        except Exception:  # noqa: BLE001  # pragma: no cover - logging must never raise
            self.handleError(record)


class _PassThroughHandler(logging.Handler):
    """Forward records at or above ``threshold`` to the root handlers.

    While capturing, the library logger runs at DEBUG with propagation off; this
    keeps everything the user would normally see flowing to Home Assistant's log
    without flooding it with the debug stream of every device.
    """

    def emit(self, record: logging.LogRecord) -> None:
        logging.getLogger().handle(record)


@contextmanager
def session_capture(
    *, maxlen: int = 4000, address: str | None = None
) -> Iterator[_RingHandler]:
    """Temporarily capture the full ``govee_ble_local`` flow + frame stream.

    Raises the ``govee_ble_local`` logger to DEBUG (the child ``.frames`` logger
    inherits it, so its ``isEnabledFor`` guard opens and frames propagate up) and
    attaches a fresh ring buffer. Records the user wouldn't normally see stay out
    of Home Assistant's log. Level, propagation and handlers are restored on exit
    - the level only if nobody changed it meanwhile.
    """
    logger = logging.getLogger(_ROOT_LOGGER)
    handler = _RingHandler(maxlen, address)
    passthrough = _PassThroughHandler(logger.getEffectiveLevel())
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    logger.addHandler(passthrough)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.removeHandler(passthrough)
        logger.propagate = previous_propagate
        if logger.level == logging.DEBUG:
            logger.setLevel(previous_level)


def _match(actual: Any, expected: Any) -> bool | None:
    """Compare a read-back value with what was commanded (tri-state).

    None when the device reports nothing (unknown), else whether they match.
    """
    if actual is None:
        return None
    return bool(actual == expected)


def _segment_at(state: DeviceState, index: int) -> Segment | None:
    return next((s for s in state.segments if s.index == index), None)


def _zone_colour(state: DeviceState, zone: Zone) -> tuple[int, int, int] | None:
    """Return a representative colour for a zone.

    Read from whichever of its segments the device reported (the same lookup
    `light.py`'s zone light uses).
    """
    seg = next((s for s in state.segments if s.index in zone.segments), None)
    return seg.rgb if seg else None


def _mid_kelvin(device: Device) -> int:
    lo = device.min_kelvin or DEFAULT_MIN_KELVIN
    hi = device.max_kelvin or DEFAULT_MAX_KELVIN
    return (lo + hi) // 2


class _Snapshot(TypedDict):
    is_on: bool | None
    brightness: int | None
    rgb_color: tuple[int, int, int] | None
    color_temp_kelvin: int | None
    scene_code: int | None
    gradual: bool | None
    zone_power: dict[str, bool | None]
    zone_colour: dict[str, tuple[int, int, int] | None]
    segment0_rgb: tuple[int, int, int] | None
    segment0_brightness: int | None


def _snapshot(device: Device) -> _Snapshot:
    """Capture everything the self-test might change, so it can be restored.

    Fields are None where the device has no read-back for them - `_restore`
    simply skips those (nothing to put back).
    """
    state = device.state
    segment0 = _segment_at(state, 0)
    zones: tuple[Zone, ...] = device.zones
    return {
        "is_on": state.is_on,
        "brightness": state.brightness,
        "rgb_color": state.rgb_color,
        "color_temp_kelvin": state.color_temp_kelvin,
        "scene_code": state.scene_code,
        "gradual": state.gradual,
        "zone_power": {zone.name: device.zone_is_on(zone.name) for zone in zones},
        "zone_colour": {zone.name: _zone_colour(state, zone) for zone in zones if zone.segments},
        "segment0_rgb": segment0.rgb if segment0 else None,
        "segment0_brightness": segment0.brightness if segment0 else None,
    }


class _Restorer:
    """Attempts restore actions independently, collecting the failures."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    async def attempt(self, label: str, action: Callable[[], Awaitable[None]]) -> None:
        """Run one restore action; a failure is recorded, never raised."""
        try:
            await action()
        except GoveeBleNotSupported:
            pass  # the test can't have changed what the device can't do
        except Exception as err:  # noqa: BLE001 - best effort: keep restoring the rest
            self.errors.append(f"{label}: {err}")


async def _restore_colour(device: Device, snap: _Snapshot, restorer: _Restorer) -> None:
    """Restore the active scene, or the static colours (whole device, then on top)."""
    caps = device.capabilities
    attempt = restorer.attempt
    if snap["scene_code"] is not None and Capability.SCENES in caps:
        # A scene repaints every segment, so no per-zone colour goes over it.
        await attempt("scene", partial(device.set_scene, snap["scene_code"]))
        return
    if snap["color_temp_kelvin"] is not None and Capability.COLOR_TEMP in caps:
        await attempt("color_temp", partial(device.set_color_temp, snap["color_temp_kelvin"]))
    elif snap["rgb_color"] is not None and Capability.RGB in caps:
        await attempt("rgb", partial(device.set_rgb, snap["rgb_color"]))
    # The whole-device colour uses the all-segment mask; per-segment/zone go on top.
    if snap["segment0_rgb"] is not None:
        await attempt("segment_0_rgb", partial(device.set_segment_rgb, [0], snap["segment0_rgb"]))
    if snap["segment0_brightness"] is not None:
        await attempt(
            "segment_0_brightness",
            partial(device.set_segment_brightness, [0], snap["segment0_brightness"]),
        )
    zones: tuple[Zone, ...] = device.zones
    for zone in zones:
        if (colour := snap["zone_colour"].get(zone.name)) is not None:
            await attempt(f"zone_rgb:{zone.name}", partial(device.set_zone_rgb, zone.name, colour))


async def _restore_power(device: Device, snap: _Snapshot, restorer: _Restorer) -> None:
    """Restore whole-device power (which switches every zone), then differing zones."""
    has_power = Capability.POWER in device.capabilities
    if snap["is_on"] is False and has_power:
        await restorer.attempt("power", partial(device.set_power, False))
        return
    whole_on = snap["is_on"] is True and has_power
    if whole_on:
        await restorer.attempt("power", partial(device.set_power, True))
    zones: tuple[Zone, ...] = device.zones
    for zone in zones:
        was_on = snap["zone_power"].get(zone.name)
        if was_on is None or (whole_on and was_on):
            continue
        await restorer.attempt(
            f"zone_power:{zone.name}", partial(device.set_zone_power, zone.name, was_on)
        )


async def _restore(device: Device, snap: _Snapshot) -> list[str]:
    """Best-effort return to the pre-test state; returns the failed actions.

    Every action is attempted on its own - one failure never abandons the rest.
    Order matters: colour first, then brightness and the gradual flag, then
    power LAST so its final word is the original on/off state.
    """
    restorer = _Restorer()
    await _restore_colour(device, snap, restorer)
    if snap["brightness"] is not None and Capability.BRIGHTNESS in device.capabilities:
        await restorer.attempt("brightness", partial(device.set_brightness, snap["brightness"]))
    if snap["gradual"] is not None and device.profile.gradual:
        await restorer.attempt("gradual", partial(device.set_gradual, snap["gradual"]))
    await _restore_power(device, snap, restorer)
    return restorer.errors


class _Steps:
    """Runs and records self-test steps against one device."""

    def __init__(self, device: Device) -> None:
        self._device = device
        self.results: list[dict[str, Any]] = []

    async def run(
        self,
        name: str,
        action: Callable[[], Awaitable[None]],
        verify: Callable[[], bool | None] | None = None,
    ) -> None:
        """Run one step: send, re-read state, then check the read-back.

        A rejected secret aborts the whole test (re-raised); a command the
        device's colour scheme doesn't support is recorded as skipped; any
        other BLE failure is recorded on the step.
        """
        entry: dict[str, Any] = {"step": name, "acked": False, "readback_ok": None, "error": None}
        try:
            await action()
            entry["acked"] = True
            try:
                await self._device.update()
            except GoveeBleAuthError:
                raise
            except BLE_ERRORS as err:
                _LOGGER.debug("%s: read-back after %s failed: %s", self._device.address, name, err)
            if verify is not None:
                entry["readback_ok"] = verify()
        except GoveeBleAuthError:
            raise
        except GoveeBleNotSupported as err:
            self.skip(name, f"not supported: {err}")
            return
        except BLE_ERRORS as err:
            entry["error"] = str(err)
        entry["ok"] = entry["acked"] and entry["readback_ok"] is not False
        self.results.append(entry)

    def skip(self, name: str, reason: str) -> None:
        """Record a step that was deliberately not run."""
        self.results.append(
            {
                "step": name,
                "acked": False,
                "readback_ok": None,
                "error": None,
                "skipped": reason,
                "ok": True,
            }
        )


def _state_is(device: Device, attr: str, expected: Any) -> bool | None:
    return _match(getattr(device.state, attr), expected)


def _segment0_is(device: Device, attr: str, expected: Any) -> bool | None:
    seg = _segment_at(device.state, 0)
    return _match(getattr(seg, attr) if seg else None, expected)


def _scene_is(device: Device, name: str) -> bool | None:
    return _match(device.active_scene, name)


def _zone_power_is(device: Device, name: str, expected: bool) -> bool | None:
    return _match(device.zone_is_on(name), expected)


def _zone_colour_is(device: Device, zone: Zone, expected: tuple[int, int, int]) -> bool | None:
    return _match(_zone_colour(device.state, zone), expected)


async def _exercise_fixture(device: Device, steps: _Steps) -> None:
    """Whole-fixture power-on, brightness, colour, colour temperature and scenes."""
    caps = device.capabilities
    if Capability.POWER in caps:
        await steps.run(
            "power_on", partial(device.set_power, True), partial(_state_is, device, "is_on", True)
        )
    if Capability.BRIGHTNESS in caps:
        await steps.run(
            "brightness",
            partial(device.set_brightness, _TEST_BRIGHTNESS),
            partial(_state_is, device, "brightness", _TEST_BRIGHTNESS),
        )
    if Capability.RGB in caps:
        await steps.run(
            "rgb",
            partial(device.set_rgb, _TEST_RGB),
            partial(_state_is, device, "rgb_color", _TEST_RGB),
        )
    if Capability.COLOR_TEMP in caps:
        kelvin = _mid_kelvin(device)
        await steps.run(
            "color_temp",
            partial(device.set_color_temp, kelvin),
            partial(_state_is, device, "color_temp_kelvin", kelvin),
        )
    if Capability.SCENES in caps:
        for name in device.scene_names[:_MAX_SCENES]:
            await steps.run(
                f"scene:{name}",
                partial(device.set_scene_by_name, name),
                partial(_scene_is, device, name),
            )


async def _exercise_segments(device: Device, steps: _Steps) -> None:
    """Segment 0 colour, brightness and (where supported) colour temperature."""
    if Capability.SEGMENTS not in device.capabilities or not device.profile.segments:
        return
    await steps.run(
        "segment_0_rgb",
        partial(device.set_segment_rgb, [0], _TEST_SEGMENT_RGB),
        partial(_segment0_is, device, "rgb", _TEST_SEGMENT_RGB),
    )
    await steps.run(
        "segment_0_brightness",
        partial(device.set_segment_brightness, [0], _TEST_SEGMENT_BRIGHTNESS),
        partial(_segment0_is, device, "brightness", _TEST_SEGMENT_BRIGHTNESS),
    )
    if Capability.COLOR_TEMP in device.capabilities:
        await steps.run(
            "segment_0_color_temp",
            partial(device.set_segment_color_temp, [0], _mid_kelvin(device)),
        )


async def _exercise_zones(device: Device, steps: _Steps) -> None:
    """Per zone: power on, colour (zones with segments), then power off."""
    zones: tuple[Zone, ...] = device.zones
    for zone in zones:
        # Power on first so a zone with segments can also be driven to a colour
        # while on; power off is tested last for this zone (the distinct off path).
        await steps.run(
            f"zone_power_on:{zone.name}",
            partial(device.set_zone_power, zone.name, True),
            partial(_zone_power_is, device, zone.name, True),
        )
        if zone.segments:
            await steps.run(
                f"zone_rgb:{zone.name}",
                partial(device.set_zone_rgb, zone.name, _TEST_ZONE_RGB),
                partial(_zone_colour_is, device, zone, _TEST_ZONE_RGB),
            )
            if Capability.COLOR_TEMP in device.capabilities:
                await steps.run(
                    f"zone_color_temp:{zone.name}",
                    partial(device.set_zone_color_temp, zone.name, _mid_kelvin(device)),
                )
        await steps.run(
            f"zone_power_off:{zone.name}",
            partial(device.set_zone_power, zone.name, False),
            partial(_zone_power_is, device, zone.name, False),
        )


async def _exercise(device: Device, steps: _Steps, *, power_known: bool) -> None:
    """Drive every capability the device declares, recording each step."""
    await _exercise_fixture(device, steps)
    await _exercise_segments(device, steps)
    await _exercise_zones(device, steps)
    if device.profile.gradual:
        toggled = not bool(device.state.gradual)
        await steps.run(
            "gradual",
            partial(device.set_gradual, toggled),
            partial(_state_is, device, "gradual", toggled),
        )
    if Capability.POWER in device.capabilities:
        if power_known:
            # Tested last so it doesn't disrupt the preconditions of the steps above.
            await steps.run(
                "power_off",
                partial(device.set_power, False),
                partial(_state_is, device, "is_on", False),
            )
        else:
            # The original power state is unknown, so it couldn't be restored:
            # don't leave the device off.
            steps.skip("power_off", "initial power state unknown")


def _sanitize_frames(lines: list[str], *, requires_secret: bool) -> list[str]:
    """Drop what could disclose an account-lock secret from captured frame lines.

    Defence in depth on top of the library's own masking: for secret-gated
    devices, keep only the decrypted (masked) ``plain=`` view - no ``wire=``
    ciphertext and no raw handshake lines.
    """
    if not requires_secret:
        return lines
    return [_WIRE_FIELD.sub("", line) for line in lines if " cipher plain=" not in line]


async def async_run_self_test(device: Device) -> dict[str, Any]:
    """Exercise the device's full capability surface and capture the BLE session.

    Returns a report ``{ok, restored, restore_errors, address, sku, encryption,
    capabilities, steps, frames, log}`` - ``address`` identifies which physical
    device this ran against (the ``capture_session`` service can target several
    devices of the same SKU in one call). Each step records ``acked`` (command
    returned without a BLE error) and ``readback_ok`` (tri-state: None when the
    device has no read-back). The device's full pre-test state is captured up
    front and restored at the end, also when a step raises.

    Raises SelfTestBusyError if another self-test is running, and
    GoveeBleAuthError if the device rejects its secret key.
    """
    if _SELF_TEST_LOCK.locked():
        raise SelfTestBusyError
    async with _SELF_TEST_LOCK:
        return await _run_self_test(device)


async def _run_self_test(device: Device) -> dict[str, Any]:
    caps = device.capabilities
    steps = _Steps(device)
    restore_errors: list[str] = []

    with session_capture(address=device.address) as capture:
        try:
            await device.update()
        except GoveeBleAuthError:
            raise
        except BLE_ERRORS as err:
            _LOGGER.debug("%s: self-test initial read failed: %s", device.address, err)
        snap = _snapshot(device)
        auth_failed = False
        try:
            await _exercise(device, steps, power_known=snap["is_on"] is not None)
        except GoveeBleAuthError:
            # Nothing lands without the secret; restoring would only retry the rejected key.
            auth_failed = True
            raise
        finally:
            if not auth_failed:
                restore_errors = await _restore(device, snap)
        if restore_errors:
            _LOGGER.warning(
                "%s: self-test could not fully restore the device state: %s",
                device.address,
                "; ".join(restore_errors),
            )

    frames = _sanitize_frames(
        [r["message"] for r in capture.buffer if r["logger"] == _FRAMES_LOGGER],
        requires_secret=device.profile.requires_secret,
    )
    log = [
        f"{r['level']} {r['message']}" for r in capture.buffer if r["logger"] != _FRAMES_LOGGER
    ]
    return {
        "ok": all(s["ok"] for s in steps.results),
        "restored": not restore_errors,
        "restore_errors": restore_errors,
        "address": device.address,
        "sku": device.sku,
        "encryption": device.encryption.value,
        "capabilities": sorted(c.value for c in caps),
        "steps": steps.results,
        "frames": frames,
        "log": log,
    }
