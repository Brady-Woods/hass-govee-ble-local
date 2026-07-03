"""BLE client for the Govee H60A6: encryption, handshake, and command sending."""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

try:
    from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4
except ImportError:  # cryptography < 43
    from cryptography.hazmat.primitives.ciphers.algorithms import ARC4

from .const import MAX_COLOR_TEMP_KELVIN, MIN_COLOR_TEMP_KELVIN, NOTIFY_CHAR_UUID, PSK, WRITE_CHAR_UUID

_LOGGER = logging.getLogger(__name__)

DISCONNECT_DELAY = 2  # seconds of inactivity before dropping the BLE connection
# bleak-retry-connector's own default (4) with generous per-attempt backoff is
# what HA's own BLE guidance recommends - transient connection failures are
# expected and normal, not something to fail fast on. Multiple lights sharing
# one adapter is handled instead by releasing connections quickly
# (DISCONNECT_DELAY) and staggering each light's poll schedule (see
# __init__.py), not by cutting retry resilience.
CONNECT_MAX_ATTEMPTS = 4
STATUS_CHUNK_TIMEOUT = 2  # seconds to wait for each status chunk
# The original status query trigger (`ac 03 02 41 30`) only ever returns
# chunks 0x00-0x04+0xFF. The real app instead sends `ac 03 03 41 30 a5`
# (PROTOCOL.md 5.3) - a different 3rd byte and one extra trailing byte -
# which additionally returns chunks 0x05-0x08, carrying per-segment
# status (see SEGMENT_COUNT below). STATUS_CHUNK_REQUIRED is what a status
# query must have to be considered successful (unchanged from before, so
# existing reliability for MAC/hw-version/brightness/scene-id/zone-state
# isn't put at risk by this addition); STATUS_CHUNK_ACCEPTED additionally
# captures 0x05-0x08 when they show up, without requiring them - if a
# future capture ever finds a mode where they're absent, status queries
# for everything else keep working, only per-segment data goes missing.
STATUS_CHUNK_REQUIRED = (0x00, 0x01, 0x02, 0x03, 0x04, 0xFF)
STATUS_CHUNK_ACCEPTED = STATUS_CHUNK_REQUIRED + (0x05, 0x06, 0x07, 0x08)
METADATA_FIELD_TIMEOUT = 2  # seconds to wait for each `ab` metadata field chunk
SEGMENT_COUNT = 12  # confirmed via live testing (PROTOCOL.md 4.2/5.3) - bits/records 0-11


def _aes_ecb(key16: bytes, block16: bytes, encrypt: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key16), modes.ECB())
    op = cipher.encryptor() if encrypt else cipher.decryptor()
    return op.update(block16) + op.finalize()


def _rc4(key16: bytes, data: bytes) -> bytes:
    cipher = Cipher(ARC4(key16), mode=None)
    enc = cipher.encryptor()
    return enc.update(data) + enc.finalize()


def _checksum(body19: bytes) -> bytes:
    x = 0
    for b in body19:
        x ^= b
    return bytes([x])


def _build_plaintext(prefix: bytes) -> bytes:
    body = prefix + b"\x00" * (19 - len(prefix))
    return body + _checksum(body)


def _encrypt_packet(key16: bytes, plaintext20: bytes) -> bytes:
    return _aes_ecb(key16, plaintext20[:16], True) + _rc4(key16, plaintext20[16:20])


def _decrypt_packet(key16: bytes, ciphertext20: bytes) -> bytes:
    return _aes_ecb(key16, ciphertext20[:16], False) + _rc4(key16, ciphertext20[16:20])


def _format_mac(mac_bytes: bytes) -> str:
    return ":".join(f"{b:02X}" for b in mac_bytes)


def _parse_metadata_field_text(raw: bytes) -> str | None:
    """Extract the ASCII text value from a reassembled `ab` metadata field
    response (PROTOCOL.md 8). Response format: a 5-byte header (chunk
    count, an unexplained byte, a fixed 0x01, and the field id that was
    queried) followed by an ASCII string, zero-padded to the end of the
    last chunk. Returns None if there's nothing past the header, or if it
    doesn't decode cleanly as non-empty ASCII."""
    if len(raw) <= 5:
        return None
    value = raw[5:].rstrip(b"\x00")
    try:
        return value.decode("ascii") or None
    except UnicodeDecodeError:
        return None


def _parse_segment_records(chunks: dict[int, bytes]) -> list["GoveeH60A6Segment"] | None:
    """Extract per-segment (brightness, r, g, b) state from status chunks
    0x05-0x08 (+ the tail end of 0xFF) - PROTOCOL.md 5.3.

    Confirmed structure via byte-for-byte comparison of two real captures
    with different colors set: a fixed 19-byte header (all of chunk 0x05,
    then the first 2 bytes of chunk 0x06 - identical regardless of color),
    followed by 3 groups of [4 records of 4 bytes each][a fixed 3-byte
    marker]. Record layout within each 4-byte group is
    `[brightness_pct, r, g, b]`. Bit-to-record mapping confirmed identical
    (record N = segment/bit N) by live-testing bits 0, 1, 5, and 11 -
    spanning the full range - and observing exactly that record change
    color each time, no others.

    Returns None if the accepted chunks aren't all present or the
    reassembled stream is too short to contain all 12 records - this is
    treated as "segment data unavailable this poll", not a hard failure,
    since the fields this project has relied on since before this feature
    existed (MAC, hw version, brightness, scene ID, zone state) don't
    depend on these chunks at all (see STATUS_CHUNK_REQUIRED).
    """
    if any(k not in chunks for k in (0x05, 0x06, 0x07, 0x08)):
        _LOGGER.debug(
            "Segment records unavailable: missing chunk(s) from %s (have %s)",
            {0x05, 0x06, 0x07, 0x08} - set(chunks),
            sorted(chunks.keys()),
        )
        return None
    stream = b"".join(chunks.get(k, b"") for k in (0x05, 0x06, 0x07, 0x08, 0xFF))

    header_len = 19
    group_size = 4 * 4  # 4 records x 4 bytes
    marker_len = 3
    records_needed = header_len + 2 * (group_size + marker_len) + group_size
    if len(stream) < records_needed:
        # Chunk 0xFF's length varies poll to poll (it's whatever's left
        # over after all preceding chunks, not a fixed size) - this is a
        # real, seen-live case, not hypothetical: on a host with more BLE
        # contention (PROTOCOL.md's long-documented adapter-sharing
        # issues), 0xFF sometimes arrives short by a few bytes, which
        # isn't caught by the missing-chunk-key check above since all of
        # 0x05-0x08 can still be fully present.
        _LOGGER.debug(
            "Segment records unavailable: reassembled stream too short "
            "(%d bytes, need %d) - chunk 0xFF was likely truncated this poll",
            len(stream),
            records_needed,
        )
        return None

    segments = []
    pos = header_len
    for _group in range(3):
        for _ in range(4):
            brightness, r, g, b = stream[pos : pos + 4]
            segments.append(GoveeH60A6Segment(len(segments), brightness, r, g, b))
            pos += 4
        pos += marker_len
    return segments


def _kelvin_to_rgb(kelvin: int) -> tuple[int, int, int]:
    """Approximate the black-body RGB tint for a color temperature.

    Verified against two real captured reference points from this device:
    2700K -> (255, 174, 84) real vs (255, 167, 87) computed, 6500K ->
    (255, 249, 251) real vs (255, 254, 250) computed - within a few units
    per channel, which is what the device actually expects (this is just a
    cosmetic tint sent alongside the raw Kelvin value, not the primary
    driver of the resulting color).
    """
    temp = kelvin / 100.0
    if temp <= 66:
        red = 255.0
    else:
        red = 329.698727446 * ((temp - 60) ** -0.1332047592)
    if temp <= 66:
        green = 99.4708025861 * math.log(temp) - 161.1195681661
    else:
        green = 288.1221695283 * ((temp - 60) ** -0.0755148492)
    if temp >= 66:
        blue = 255.0
    elif temp <= 19:
        blue = 0.0
    else:
        blue = 138.5177312231 * math.log(temp - 10) - 305.0447927307

    def clamp(v: float) -> int:
        return max(0, min(255, round(v)))

    return (clamp(red), clamp(green), clamp(blue))


@dataclass
class GoveeH60A6Segment:
    """One individually-addressable segment's current state (PROTOCOL.md
    4.2/5.3). `index` is both the record position in the status response
    and the bit position in the set_segment_color/brightness bitmask -
    confirmed identical via live testing (setting bit N always changed
    record N, checked at N=0,1,5,11 spanning the full range)."""

    index: int
    brightness_pct: int
    r: int
    g: int
    b: int


@dataclass
class GoveeH60A6Status:
    zone_upper_on: bool | None = None
    zone_lower_on: bool | None = None
    brightness_pct: int | None = None
    scene_id: tuple[int, int] | None = None
    hardware_version: str | None = None
    ble_mac: str | None = None
    wifi_mac: str | None = None
    segments: list[GoveeH60A6Segment] | None = None


class GoveeH60A6Client:
    """Maintains an on-demand encrypted BLE session with the light."""

    def __init__(self, ble_device: BLEDevice) -> None:
        self._ble_device = ble_device
        self._client: BleakClientWithServiceCache | None = None
        self._session_key: bytes | None = None
        self._lock = asyncio.Lock()
        self._notify_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._expire_task: asyncio.Task[None] | None = None

    def update_ble_device(self, ble_device: BLEDevice) -> None:
        self._ble_device = ble_device

    def _on_notify(self, _characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
        self._notify_queue.put_nowait(bytes(data))

    async def _connect(self) -> None:
        if self._client is not None and self._client.is_connected:
            return
        _LOGGER.debug("Connecting to Govee H60A6 %s", self._ble_device.address)
        try:
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self._ble_device.address,
                disconnected_callback=self._on_disconnect,
                max_attempts=CONNECT_MAX_ATTEMPTS,
            )
            await self._client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
            await self._handshake()
        except BleakError:
            _LOGGER.error("Failed to connect to %s", self._ble_device.address, exc_info=True)
            raise
        _LOGGER.debug("Connected and authenticated with %s", self._ble_device.address)

    def _on_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        _LOGGER.debug("Govee H60A6 %s disconnected", self._ble_device.address)
        self._session_key = None

    async def _drain_notify_queue(self) -> None:
        while not self._notify_queue.empty():
            self._notify_queue.get_nowait()

    async def _handshake(self) -> None:
        await self._drain_notify_queue()
        assert self._client is not None

        _LOGGER.debug("Starting handshake with %s", self._ble_device.address)
        tx1 = _encrypt_packet(PSK, _build_plaintext(bytes([0xE7, 0x01])))
        await self._client.write_gatt_char(WRITE_CHAR_UUID, tx1, response=False)
        rx1 = await asyncio.wait_for(self._notify_queue.get(), timeout=10)
        rx1_plain = _decrypt_packet(PSK, rx1)
        if rx1_plain[0] != 0xE7 or rx1_plain[1] != 0x01:
            _LOGGER.warning(
                "Unexpected handshake response from %s: %s",
                self._ble_device.address,
                rx1_plain.hex(),
            )
            raise BleakError(f"Unexpected handshake response: {rx1_plain.hex()}")
        self._session_key = rx1_plain[2:18]
        _LOGGER.debug("Session key established for %s", self._ble_device.address)

        tx2 = _encrypt_packet(PSK, _build_plaintext(bytes([0xE7, 0x02])))
        await self._client.write_gatt_char(WRITE_CHAR_UUID, tx2, response=False)
        try:
            await asyncio.wait_for(self._notify_queue.get(), timeout=3)
        except asyncio.TimeoutError:
            _LOGGER.debug("No TX2 ack from %s (usually harmless)", self._ble_device.address)
        await self._drain_notify_queue()

    def _cancel_disconnect_timer(self) -> None:
        if self._disconnect_timer is not None:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None

    def _schedule_disconnect(self) -> None:
        self._cancel_disconnect_timer()
        loop = asyncio.get_running_loop()
        self._disconnect_timer = loop.call_later(DISCONNECT_DELAY, self._on_disconnect_timer)

    def _on_disconnect_timer(self) -> None:
        # Timer fired: mark it consumed and kick off the actual disconnect as
        # a task. The task takes the operation lock, so it can't tear the
        # connection down in the middle of an in-flight command (the bug this
        # replaced: the old lambda called disconnect() straight from the timer
        # with no lock, so it could null self._client while a concurrent
        # get_status/send_command was still using it, producing a spurious
        # BleakError and a failed poll).
        self._disconnect_timer = None
        self._expire_task = asyncio.create_task(self._async_timed_disconnect())

    async def _async_timed_disconnect(self) -> None:
        async with self._lock:
            # If an operation started after the timer fired, it scheduled a
            # fresh timer while we were waiting for the lock - so the device
            # isn't actually idle anymore and we must not disconnect.
            if self._disconnect_timer is not None:
                return
            await self._disconnect_locked()

    async def send_command(self, prefix: bytes) -> bytes | None:
        """Connect if needed, send one command, return the decrypted ack (or None)."""
        async with self._lock:
            await self._connect()
            assert self._client is not None and self._session_key is not None
            # Cancel any pending idle-disconnect now that we're actively using
            # the connection, so it can't fire out from under an ack wait that
            # takes a few seconds (which would otherwise kill mid-sequence
            # operations like a multi-chunk scene upload).
            self._cancel_disconnect_timer()
            # Clear any stale/late notification left over from a previous
            # operation before writing - otherwise a leftover packet (e.g. a
            # scene-upload ack that arrived just after that call's own
            # timeout) gets wrongly consumed as *this* command's ack, and the
            # real ack for this command (if it arrives) becomes the next
            # call's stale packet. This chains into misattributed acks and
            # occasionally a genuinely wrong reading (e.g. a stale scene ID)
            # under repeated back-to-back use. Every other write path
            # (_handshake, _query_status_chunks, set_scene_full's upload)
            # already does this; this was the one gap.
            await self._drain_notify_queue()

            plaintext = _build_plaintext(prefix)
            ciphertext = _encrypt_packet(self._session_key, plaintext)
            _LOGGER.debug("Sending command %s to %s", plaintext.hex(), self._ble_device.address)
            await self._client.write_gatt_char(WRITE_CHAR_UUID, ciphertext, response=False)

            ack = None
            try:
                resp = await asyncio.wait_for(self._notify_queue.get(), timeout=3)
                ack = _decrypt_packet(self._session_key, resp)
                _LOGGER.debug("Ack for %s: %s", plaintext.hex(), ack.hex())
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "No ack notification for command %s to %s",
                    prefix.hex(),
                    self._ble_device.address,
                )

            self._schedule_disconnect()
            return ack

    async def set_zone(self, zone: int, on: bool) -> None:
        await self.send_command(bytes([0x33, 0x30, zone, 1 if on else 0]))

    async def set_brightness_pct(self, pct: int) -> None:
        pct = max(0, min(100, pct))
        await self.send_command(bytes([0x33, 0x04, pct]))

    async def set_rgb_color(self, r: int, g: int, b: int) -> None:
        await self.send_command(
            bytes([0x33, 0x05, 0x15, 0x01, r, g, b, 0x00, 0x00, 0x00, 0x00, 0x00, 0xFF, 0x1F])
        )

    async def set_color_temp_kelvin(self, kelvin: int) -> None:
        kelvin = max(MIN_COLOR_TEMP_KELVIN, min(MAX_COLOR_TEMP_KELVIN, kelvin))
        approx_r, approx_g, approx_b = _kelvin_to_rgb(kelvin)
        kelvin_hi = (kelvin >> 8) & 0xFF
        kelvin_lo = kelvin & 0xFF
        await self.send_command(
            bytes(
                [
                    0x33, 0x05, 0x15, 0x01,
                    0xFF, 0xFF, 0xFF,
                    kelvin_hi, kelvin_lo,
                    approx_r, approx_g, approx_b,
                    0xFF, 0x1F,
                ]
            )
        )

    async def set_segment_color(self, segment_mask: int, r: int, g: int, b: int) -> None:
        """Set RGB color on one or more individually-addressable segments.

        NEWLY DISCOVERED (see PROTOCOL.md 4.2) - distinct from
        set_rgb_color's solid-color command (which always uses a fixed
        `ff 1f` trailer and no bitmask). `segment_mask` is a 16-bit
        little-endian bitmask; bits 0-11 were confirmed via a real capture
        of the app's per-segment picker (12 individually-addressable
        segments, tapped one at a time). Bits 12-15 are untested - not
        confirmed unused, just never observed set in the capture this was
        derived from. Which physical bit maps to which physical LED/zone
        position has NOT been confirmed - only the command format has.
        """
        mask_lo = segment_mask & 0xFF
        mask_hi = (segment_mask >> 8) & 0xFF
        await self.send_command(
            bytes(
                [
                    0x33, 0x05, 0x15, 0x01,
                    r, g, b,
                    0x00, 0x00, 0x00, 0x00, 0x00,
                    mask_lo, mask_hi,
                    0x00, 0x00, 0x00, 0x00, 0x00,
                ]
            )
        )

    async def set_segment_brightness(self, segment_mask: int, pct: int) -> None:
        """Set brightness (0-100) on one or more individually-addressable
        segments. NEWLY DISCOVERED (see PROTOCOL.md 4.2), same bitmask
        scheme as set_segment_color but a different sub-opcode (0x02) and
        the mask sits immediately after the single brightness byte rather
        than after an RGB triplet. Captured mid-slider-drag in the app, so
        the exact interaction between multiple simultaneous bits and this
        value is not yet confirmed - see PROTOCOL.md 4.2 for what's still
        untested.
        """
        pct = max(0, min(100, pct))
        mask_lo = segment_mask & 0xFF
        mask_hi = (segment_mask >> 8) & 0xFF
        await self.send_command(
            bytes(
                [
                    0x33, 0x05, 0x15, 0x02,
                    pct,
                    mask_lo, mask_hi,
                    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                ]
            )
        )

    async def set_scene(self, scene_id: tuple[int, int]) -> None:
        """Bare scene activation only. Works if the device already has this
        scene's data cached from prior use; otherwise may silently no-op.
        Prefer set_scene_full() when the effect data is known."""
        await self.send_command(bytes([0x33, 0x05, 0x04, scene_id[0], scene_id[1]]))

    async def set_scene_full(self, scene_code: int, scenceParam_b64: str) -> bool:
        """Upload the full effect data, then activate it. Guaranteed correct
        regardless of whether the device has seen this scene before.

        Matches the real app's behavior exactly (confirmed from BLE capture):
        all chunks are fired back-to-back with no per-chunk ack wait, and the
        device sends exactly one completion notification after the full
        burst, not one per chunk. Waiting per-chunk (the old approach) let
        the gaps between chunks blow past the device's own reassembly
        window, corrupting the upload.

        Returns whether the upload ack was actually seen (diagnostic only -
        the upload+activation proceed identically either way; see the
        no-ack handling below). Not meaningful to production callers, only
        to test_scene_switching.py's investigation of whether success
        actually depends on this ack.
        """
        from .scene_library import build_scene_chunks  # local import avoids a hard dep for bare-mode users

        chunks = build_scene_chunks(scenceParam_b64)
        async with self._lock:
            await self._connect()
            assert self._client is not None and self._session_key is not None
            self._cancel_disconnect_timer()
            await self._drain_notify_queue()

            _LOGGER.debug(
                "Uploading %d scene data chunks (burst) for code %d to %s",
                len(chunks),
                scene_code,
                self._ble_device.address,
            )
            for chunk_prefix in chunks:
                plaintext = _build_plaintext(chunk_prefix)
                ciphertext = _encrypt_packet(self._session_key, plaintext)
                await self._client.write_gatt_char(WRITE_CHAR_UUID, ciphertext, response=False)

            ack_received = False
            try:
                resp = await asyncio.wait_for(self._notify_queue.get(), timeout=3)
                ack = _decrypt_packet(self._session_key, resp)
                ack_received = True
                _LOGGER.debug("Scene upload ack from %s: %s", self._ble_device.address, ack.hex())
            except asyncio.TimeoutError:
                # Previously raised BleakError here and aborted the whole
                # activation. Found to be wrong: an independently-developed
                # Govee BLE project (Beshelmek/govee_ble_lights) never waits
                # for a command ack at all - it fires writes and moves on -
                # and works reliably. Live testing here confirmed our own
                # ack-wait was producing false failures under repeated/rapid
                # scene switching (most common failure mode in
                # test_scene_switching.py), and a late-arriving ack that
                # missed this window was then getting misattributed to the
                # *next* operation (see the drain-queue fix in
                # send_command() above, added for the same reason). The
                # write itself already went out; proceed to activation
                # regardless rather than treating an absent/late ack as
                # proof the upload failed.
                _LOGGER.debug(
                    "No ack after uploading scene data to %s - proceeding to "
                    "activate anyway (see send_command's queue-drain comment; "
                    "acks are not reliably tied 1:1 to the write that "
                    "triggered them)",
                    self._ble_device.address,
                )

            self._schedule_disconnect()

        low = scene_code & 0xFF
        high = (scene_code >> 8) & 0xFF
        await self.set_scene((low, high))

        # The device needs a moment to finish internally processing a large
        # scene upload before it can reliably answer anything else. Hitting
        # it with an immediate status query after a big upload (e.g. 22+
        # chunks) has been observed to get no response at all. Scale the
        # settle time with upload size since small scenes don't need it.
        settle_time = min(0.2 + 0.1 * len(chunks), 2.0)
        await asyncio.sleep(settle_time)
        return ack_received

    async def _query_status_chunks(self) -> dict[int, bytes]:
        assert self._client is not None and self._session_key is not None
        await self._drain_notify_queue()
        # `ac 03 03 41 30 a5` - the real app's exact bytes (PROTOCOL.md
        # 5.3), not the `ac 03 02 41 30` this project used originally.
        # That difference (3rd byte, one extra trailing byte) is what
        # unlocks chunks 0x05-0x08 (per-segment status) in addition to the
        # original 0x00-0x04+0xFF.
        plaintext = _build_plaintext(bytes([0xAC, 0x03, 0x03, 0x41, 0x30, 0xA5]))
        ciphertext = _encrypt_packet(self._session_key, plaintext)
        _LOGGER.debug("Requesting status from %s", self._ble_device.address)
        await self._client.write_gatt_char(WRITE_CHAR_UUID, ciphertext, response=False)

        chunks: dict[int, bytes] = {}
        try:
            # Wait for the specific chunk keys we actually parse, not just
            # "any N chunks" - the device can include extra chunk types we
            # don't recognize (observed: an unexpected chunk appeared after
            # exercising color temp/calibration), and counting those toward
            # our target caused us to stop early while still missing one of
            # the chunks we need (corrupting MAC/hw-version parsing).
            # Only STATUS_CHUNK_REQUIRED gates completion - 0x05-0x08 are
            # captured opportunistically (STATUS_CHUNK_ACCEPTED) without
            # being required, so a device/mode that doesn't return them
            # doesn't break everything else this query is relied on for.
            while not set(STATUS_CHUNK_REQUIRED).issubset(chunks):
                resp = await asyncio.wait_for(
                    self._notify_queue.get(), timeout=STATUS_CHUNK_TIMEOUT
                )
                pt = _decrypt_packet(self._session_key, resp)
                if pt[0] != 0xAC:
                    continue
                if pt[1] not in STATUS_CHUNK_ACCEPTED:
                    _LOGGER.debug(
                        "Ignoring unrecognized status chunk 0x%02x from %s",
                        pt[1],
                        self._ble_device.address,
                    )
                    continue
                chunks[pt[1]] = pt[2:19]
        except asyncio.TimeoutError:
            _LOGGER.debug(
                "Status query from %s incomplete: got chunks %s",
                self._ble_device.address,
                sorted(chunks.keys()),
            )
        # Note: whether the opportunistic segment-data chunks (0x05-0x08)
        # came through fully is logged inside _parse_segment_records
        # itself, once it's clear whether it's a missing-chunk case or a
        # too-short-reassembled-stream case (both seen live - see there).
        return chunks

    async def get_status(self) -> GoveeH60A6Status:
        """Query current device status (zones, brightness, scene, versions, MACs)."""
        async with self._lock:
            await self._connect()
            self._cancel_disconnect_timer()

            chunks = await self._query_status_chunks()
            if not chunks:
                # The device can be briefly unresponsive right after a large
                # operation (e.g. a big scene upload), and may even drop the
                # connection outright during that window. One quick retry
                # avoids flagging the whole entity unavailable over a
                # transient blip, but we must re-establish the connection
                # first if it dropped, or the retry write will just throw.
                _LOGGER.debug(
                    "Empty status response from %s, retrying once", self._ble_device.address
                )
                await asyncio.sleep(0.5)
                await self._connect()
                chunks = await self._query_status_chunks()

            self._schedule_disconnect()

            if not chunks:
                raise BleakError(f"No status response from {self._ble_device.address}")

            status = self._parse_status(chunks)
            _LOGGER.debug("Status from %s: %s", self._ble_device.address, status)
            return status

    async def _query_metadata_field(self, field_id: int) -> bytes:
        """Query a device metadata field via the `ab` opcode (PROTOCOL.md 8).

        Returns the raw reassembled multi-chunk payload, header bytes
        included - callers are expected to know how to interpret their
        specific field. Returns b"" if the device never responds (no
        chunk 0xFF seen within the timeout).
        """
        assert self._client is not None and self._session_key is not None
        await self._drain_notify_queue()
        plaintext = _build_plaintext(bytes([0xAB, 0x01, field_id]))
        ciphertext = _encrypt_packet(self._session_key, plaintext)
        await self._client.write_gatt_char(WRITE_CHAR_UUID, ciphertext, response=False)

        chunks: dict[int, bytes] = {}
        try:
            while 0xFF not in chunks:
                resp = await asyncio.wait_for(
                    self._notify_queue.get(), timeout=METADATA_FIELD_TIMEOUT
                )
                pt = _decrypt_packet(self._session_key, resp)
                if pt[0] != 0xAB:
                    continue
                chunks[pt[1]] = pt[2:19]
        except asyncio.TimeoutError:
            _LOGGER.debug(
                "Metadata field 0x%02x query from %s incomplete: got chunks %s",
                field_id,
                self._ble_device.address,
                sorted(chunks.keys()),
            )

        if not chunks:
            return b""
        ordered_seqs = sorted(k for k in chunks if k != 0xFF)
        if 0xFF in chunks:
            ordered_seqs.append(0xFF)
        return b"".join(chunks[s] for s in ordered_seqs)

    async def get_serial_number(self) -> str | None:
        """Query the device's serial/UID string.

        Uses `ab` field 0x05 (PROTOCOL.md 8) - confirmed stable across two
        independently captured sessions (identical value both times).
        Returns None if the device doesn't respond or the payload doesn't
        decode cleanly - this is a "nice to have" field, not worth raising
        an error over if it's unavailable.
        """
        async with self._lock:
            await self._connect()
            self._cancel_disconnect_timer()
            raw = await self._query_metadata_field(0x05)
            self._schedule_disconnect()

        value = _parse_metadata_field_text(raw)
        if value is None:
            _LOGGER.debug(
                "Serial number field from %s did not parse cleanly: %s",
                self._ble_device.address,
                raw.hex(),
            )
        return value

    def _parse_status(self, chunks: dict[int, bytes]) -> GoveeH60A6Status:
        # The device's status layout is mode-dependent: when it's in
        # RGB/color-temp mode (vs. scene mode), chunk 0x00 is omitted
        # entirely and every subsequent chunk's content shifts by exactly
        # one byte, confirmed by comparing real captures in both modes.
        # Fixed byte offsets silently produced garbage (wrong MAC/hw
        # version) whenever the light was in color mode. Instead of relying
        # on absolute offsets, locate our own known BLE MAC address
        # (reversed, as it appears on the wire) as an anchor point and use
        # relative offsets from there - those stayed stable across both
        # modes when verified against real data.
        status = GoveeH60A6Status()

        chunk00 = chunks.get(0x00)
        has_chunk00 = chunk00 is not None

        # Regression found and fixed live (2026-07-02): switching
        # _query_status_chunks() to the fuller trigger (5.3) changed what
        # ends up tagged chunk 0xFF - with more total data now in the
        # response, the same underlying bytes that used to be the
        # terminator chunk in the shorter response are now tagged 0x05
        # instead (confirmed byte-for-byte: bytes 0-12 identical between
        # the old chunk_ff and the new chunk_05 across multiple captures).
        # 0xFF in the fuller response is now the tail end of segment data,
        # not zone state. Prefer 0x05 (the fuller query's real terminator
        # equivalent); fall back to 0xFF for safety if a future capture
        # ever returns the shorter structure again.
        chunk_ff = chunks.get(0x05) or chunks.get(0xFF)
        if chunk_ff is not None and len(chunk_ff) >= 16:
            shift = 0 if has_chunk00 else 1
            # BUG FOUND AND FIXED (2026-07-02): these two were swapped. The
            # main light entity's is_on (zone_upper_on OR zone_lower_on)
            # masked this for a long time since it doesn't care which byte
            # is which, and the per-zone switches were assumed correct
            # without ever being independently verified against a known,
            # asymmetric physical state. Confirmed live: commanded
            # upper=ON/lower=OFF via set_zone, then read status directly
            # over BLE from Bazzite (isolated from core's own concurrent
            # polling, which would otherwise contend for the device's
            # single connection slot) - byte 13+shift read True and
            # byte 14+shift read False, while the user visually confirmed
            # the UPPER zone was the one actually lit. So byte 13+shift is
            # upper, not lower.
            status.zone_upper_on = bool(chunk_ff[13 + shift])
            status.zone_lower_on = bool(chunk_ff[14 + shift])

        if has_chunk00 and len(chunk00) >= 16:
            status.brightness_pct = chunk00[10]
            status.scene_id = (chunk00[14], chunk00[15])

        stream = b"".join(chunks.get(k, b"") for k in (0x01, 0x02, 0x03, 0x04, 0xFF))
        own_mac_bytes = bytes(int(b, 16) for b in self._ble_device.address.split(":"))
        anchor = stream.find(own_mac_bytes[::-1])
        if anchor != -1:
            status.ble_mac = _format_mac(own_mac_bytes)
            wifi_mac_bytes = stream[anchor + 9 : anchor + 15]
            if len(wifi_mac_bytes) == 6:
                status.wifi_mac = _format_mac(wifi_mac_bytes[::-1])
            hw_bytes = stream[anchor + 20 : anchor + 23]
            if len(hw_bytes) == 3:
                status.hardware_version = f"{hw_bytes[0]}.{hw_bytes[1]:02d}.{hw_bytes[2]:02d}"
        else:
            _LOGGER.debug(
                "Could not locate own BLE MAC in status stream from %s: %s",
                self._ble_device.address,
                stream.hex(),
            )

        status.segments = _parse_segment_records(chunks)

        return status

    async def disconnect(self) -> None:
        """Cancel any pending idle timer and tear down the connection.

        Takes the operation lock so it waits for any in-flight command to
        finish rather than pulling the connection out from under it. Called
        on config-entry unload.
        """
        self._cancel_disconnect_timer()
        async with self._lock:
            await self._disconnect_locked()

    async def _disconnect_locked(self) -> None:
        """Actual teardown. Caller must hold self._lock."""
        if self._client is not None and self._client.is_connected:
            _LOGGER.debug("Disconnecting from %s (idle)", self._ble_device.address)
            await self._client.disconnect()
        self._client = None
        self._session_key = None
