#!/usr/bin/env python3
"""
Standalone regression tests for the Govee H60A6 BLE protocol implementation.

Run directly: python3 test_protocol.py
(or: python3 -m unittest test_protocol -v)

Requires only the Python standard library plus `cryptography` (a real
dependency of the integration itself - see manifest.json). Does NOT require
`bleak`, `bleak_retry_connector`, `aiohttp`, or Home Assistant to be
installed: those are stubbed just enough (see _install_stubs below) for the
real production modules (client.py, scene_library.py) to import cleanly,
so these tests exercise the actual shipped logic rather than a
reimplementation that could silently drift from it.

See PROTOCOL.md in this directory for the full protocol writeup these
tests are meant to validate.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

INTEGRATION_DIR = Path(__file__).resolve().parent


def _install_stubs() -> None:
    """Install minimal fake modules for dependencies we don't need for
    pure-protocol-logic testing, so client.py/scene_library.py can be
    imported without bleak/aiohttp/homeassistant actually installed."""

    if "bleak" not in sys.modules:
        bleak_pkg = types.ModuleType("bleak")
        backends_pkg = types.ModuleType("bleak.backends")
        # Mark backends as a package so `bleak.backends.<x>` submodule imports
        # (e.g. bleak.backends.characteristic) resolve.
        backends_pkg.__path__ = []  # type: ignore[attr-defined]
        device_mod = types.ModuleType("bleak.backends.device")
        characteristic_mod = types.ModuleType("bleak.backends.characteristic")
        exc_mod = types.ModuleType("bleak.exc")

        class BLEDevice:  # noqa: D101 - test stub
            def __init__(self, address: str = "00:00:00:00:00:00") -> None:
                self.address = address

        class BleakGATTCharacteristic:  # noqa: D101 - test stub
            pass

        class BleakError(Exception):
            pass

        device_mod.BLEDevice = BLEDevice
        characteristic_mod.BleakGATTCharacteristic = BleakGATTCharacteristic
        exc_mod.BleakError = BleakError
        backends_pkg.device = device_mod
        backends_pkg.characteristic = characteristic_mod
        bleak_pkg.backends = backends_pkg
        bleak_pkg.exc = exc_mod

        sys.modules["bleak"] = bleak_pkg
        sys.modules["bleak.backends"] = backends_pkg
        sys.modules["bleak.backends.device"] = device_mod
        sys.modules["bleak.backends.characteristic"] = characteristic_mod
        sys.modules["bleak.exc"] = exc_mod

    if "bleak_retry_connector" not in sys.modules:
        brc = types.ModuleType("bleak_retry_connector")

        class BleakClientWithServiceCache:  # noqa: D101 - test stub
            pass

        async def establish_connection(*_args, **_kwargs):
            raise NotImplementedError("stub - not exercised by these tests")

        brc.BleakClientWithServiceCache = BleakClientWithServiceCache
        brc.establish_connection = establish_connection
        sys.modules["bleak_retry_connector"] = brc

    if "aiohttp" not in sys.modules:
        aiohttp_mod = types.ModuleType("aiohttp")

        class ClientTimeout:  # noqa: D101 - test stub
            def __init__(self, *_args, **_kwargs) -> None:
                pass

        class ClientError(Exception):
            pass

        aiohttp_mod.ClientTimeout = ClientTimeout
        aiohttp_mod.ClientError = ClientError
        sys.modules["aiohttp"] = aiohttp_mod

    if "homeassistant" not in sys.modules:
        ha_pkg = types.ModuleType("homeassistant")
        ha_core = types.ModuleType("homeassistant.core")
        ha_helpers = types.ModuleType("homeassistant.helpers")
        ha_aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")

        class HomeAssistant:  # noqa: D101 - test stub
            pass

        def async_get_clientsession(_hass):
            raise NotImplementedError("stub - not exercised by these tests")

        ha_core.HomeAssistant = HomeAssistant
        ha_aiohttp_client.async_get_clientsession = async_get_clientsession
        ha_helpers.aiohttp_client = ha_aiohttp_client
        ha_pkg.core = ha_core
        ha_pkg.helpers = ha_helpers

        sys.modules["homeassistant"] = ha_pkg
        sys.modules["homeassistant.core"] = ha_core
        sys.modules["homeassistant.helpers"] = ha_helpers
        sys.modules["homeassistant.helpers.aiohttp_client"] = ha_aiohttp_client


def _load_real_modules():
    """Load the actual const.py/client.py/scene_library.py from this
    directory as a fake package, so their `from .const import ...`-style
    relative imports resolve without needing the full custom_components
    package tree (which would pull in the real homeassistant package)."""
    pkg_name = "_govee_h60a6_under_test"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(INTEGRATION_DIR)]
    sys.modules[pkg_name] = pkg

    def _load(module_name: str):
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{module_name}", INTEGRATION_DIR / f"{module_name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{module_name}"] = module
        spec.loader.exec_module(module)
        return module

    const = _load("const")
    client = _load("client")
    scene_library = _load("scene_library")
    return const, client, scene_library


_install_stubs()
const, client_mod, scene_library_mod = _load_real_modules()

GoveeH60A6Client = client_mod.GoveeH60A6Client
BLEDevice = sys.modules["bleak.backends.device"].BLEDevice
build_scene_chunks = scene_library_mod.build_scene_chunks


def make_client(address: str = "5C:E7:53:F4:74:57") -> "GoveeH60A6Client":
    return GoveeH60A6Client(BLEDevice(address))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Real captured data fixtures (see PROTOCOL.md for provenance of each)
# ---------------------------------------------------------------------------

# Real captured Graffiti scene: full base64 scenceParam from Govee's public
# scene-library API, and the real a3-chunk burst the app sent for it.
GRAFFITI_SCENCEPARAM_B64 = (
    "UFQAAgEAAP9BJQABZABkMjICAAAAAGRkAQEH/wAA/38A//8AAP8AAAD/AP//iwD/JQABZAAy"
    "MjICAAAAAGRkAQEHiwD/AP//AAD/AP8A//8A/38A/wAA"
)
GRAFFITI_REAL_CHUNKS_HEX = [
    "a300010658540002010000ff4125000155006400",
    "a301323202000000006464010107ff0000ff7fd8",
    "a30200ffff0000ff000000ff00ffff8b00ff25f0",
    "a3030001550032323202000000006464010107c3",
    "a3048b00ff00ffff0000ff00ff00ffff00ff7f53",
    "a3ff00ff000000000000000000000000000000a3",
]

# Real captured Cornfield scene (decoded scenceParam bytes, re-encoded to
# base64 for input to build_scene_chunks) and its real a3-chunk burst.
CORNFIELD_SCENCEPARAM_HEX = (
    "5042000201ffd000641c000164ce0032320400000000506403010"
    "4ffe700ffff00e2de00ff9e001c0001649c0032320400000000"
    "2a64040104ffbe00c2d408ffc500ffd400"
)
CORNFIELD_SCENCEPARAM_B64 = base64.b64encode(bytes.fromhex(CORNFIELD_SCENCEPARAM_HEX)).decode()
CORNFIELD_REAL_CHUNKS_HEX = [
    "a30001055842000201ffd000641c00015ace007c",
    "a301323204000000005064030104ffe700ffff8c",
    "a30200e2de00ff9e001c00015a9c003232040023",
    "a3030000002a64040104ffbe00c2d408ffc5008a",
    "a3ffffd400000000000000000000000000000077",
]

# Real captured status-query chunks, two different device modes (same
# physical device, address 5C:E7:53:F4:74:57). This is the regression test
# for the mode-dependent layout bug documented in PROTOCOL.md section 5.1.
STATUS_CHUNKS_SCENE_MODE = {
    0xFF: bytes.fromhex("0000800000008041020201300201010000"),
    0x01: bytes.fromhex("07065774f453e75c0711105674f453e75c"),
    0x02: bytes.fromhex("db2d0100290104030000070d115ce753f4"),
    0x03: bytes.fromhex("74560100290104031104001e0f0f1207ff"),
    0x04: bytes.fromhex("640000800f002310000000800000008000"),
}
STATUS_CHUNKS_RGB_MODE = {
    0xFF: bytes.fromhex("0000008000000080410202013002010100"),
    0x01: bytes.fromhex("0707065774f453e75c0711105674f453e7"),
    0x02: bytes.fromhex("5cdb2d0100290104030000070d115ce753"),
    0x03: bytes.fromhex("f474560100290104031104001e0f0f1207"),
    0x04: bytes.fromhex("ff640000800f0023100000008000000080"),
}

# Real capture (device D4:13:68:21:D0:75, solid green set via
# set_rgb_color(0, 255, 0)) using the app's exact status-query bytes
# (PROTOCOL.md 5.3) - the first real capture obtained through this
# project's own updated client.py rather than hand-assembled from a
# btsnoop log. Includes the full 0x00-0x08+0xFF chunk set, so this is the
# regression test for per-segment status parsing end to end.
STATUS_CHUNKS_WITH_SEGMENTS = {
    0xFF: bytes.fromhex("002900ff00a505043200ff000000000000"),
    0x00: bytes.fromhex("0a000c0300010101040101050415010000"),
    0x01: bytes.fromhex("07070675d0216813d407111074d0216813"),
    0x02: bytes.fromhex("d478040100290104030000070d11d41368"),
    0x03: bytes.fromhex("21d0740100290104031104001e0f0f1207"),
    0x04: bytes.fromhex("00640000800f0023100000008000000080"),
    0x05: bytes.fromhex("00000080000000804102020130020001a5"),
    0x06: bytes.fromhex("11013200ff003c00ff004600ff005100ff"),
    0x07: bytes.fromhex("00a511025a00ff006400ff000100ff0005"),
    0x08: bytes.fromhex("00ff00a511030a00ff001300ff001d00ff"),
}
# Expected (brightness_pct, r, g, b) per segment, index = bit position,
# read directly off the live client.get_status() output for the capture
# above - all green, with each segment's individually-set brightness (a
# leftover from an earlier, unrelated per-segment brightness test)
# undisturbed by the solid-color command.
STATUS_SEGMENTS_EXPECTED = [
    (50, 0, 255, 0), (60, 0, 255, 0), (70, 0, 255, 0), (81, 0, 255, 0),
    (90, 0, 255, 0), (100, 0, 255, 0), (1, 0, 255, 0), (5, 0, 255, 0),
    (10, 0, 255, 0), (19, 0, 255, 0), (29, 0, 255, 0), (41, 0, 255, 0),
]


# ---------------------------------------------------------------------------
# Pure crypto/framing primitives
# ---------------------------------------------------------------------------


class TestChecksumAndFraming(unittest.TestCase):
    def test_checksum_is_xor_of_all_bytes(self):
        body = bytes([0x33, 0x30, 0x01, 0x01]) + b"\x00" * 15
        checksum = client_mod._checksum(body)
        expected = 0
        for b in body:
            expected ^= b
        self.assertEqual(checksum, bytes([expected]))

    def test_build_plaintext_pads_to_19_bytes_plus_checksum(self):
        pt = client_mod._build_plaintext(bytes([0x33, 0x04, 50]))
        self.assertEqual(len(pt), 20)
        self.assertEqual(pt[:3], bytes([0x33, 0x04, 50]))
        self.assertEqual(pt[3:19], b"\x00" * 16)
        # Verify checksum byte matches XOR of the first 19 bytes.
        x = 0
        for b in pt[:19]:
            x ^= b
        self.assertEqual(pt[19], x)

    def test_build_plaintext_rejects_oversized_prefix_gracefully(self):
        # A 19-byte prefix (the max for a single a3 chunk: opcode+seq+17
        # payload bytes) should produce zero padding and just append the
        # checksum.
        prefix = bytes(range(19))
        pt = client_mod._build_plaintext(prefix)
        self.assertEqual(len(pt), 20)
        self.assertEqual(pt[:19], prefix)


class TestEncryptionRoundTrip(unittest.TestCase):
    def test_aes_ecb_round_trip(self):
        key = b"0123456789abcdef"
        block = b"the 16 byte data"
        ct = client_mod._aes_ecb(key, block, encrypt=True)
        pt = client_mod._aes_ecb(key, ct, encrypt=False)
        self.assertEqual(pt, block)
        self.assertNotEqual(ct, block)

    def test_rc4_is_symmetric(self):
        key = b"0123456789abcdef"
        data = b"four"
        ct = client_mod._rc4(key, data)
        pt = client_mod._rc4(key, ct)  # RC4 is a stream cipher: same op decrypts
        self.assertEqual(pt, data)

    def test_full_packet_round_trip(self):
        key = b"0123456789abcdef"
        plaintext = client_mod._build_plaintext(bytes([0x33, 0x30, 0x01, 0x01]))
        ciphertext = client_mod._encrypt_packet(key, plaintext)
        self.assertEqual(len(ciphertext), 20)
        decrypted = client_mod._decrypt_packet(key, ciphertext)
        self.assertEqual(decrypted, plaintext)

    def test_handshake_session_key_derivation_matches_real_capture(self):
        # Real captured handshake: app writes [0xE7,0x01]+zeros encrypted
        # with the PSK; device replies with [0xE7,0x01,<16-byte session
        # key>,...] also PSK-encrypted. Verified against a real capture
        # from this exact device (see PROTOCOL.md section 3.1).
        real_notify_ct = bytes.fromhex("0f36c6ee11ee832630b469d6b18e4a659ccc980a")
        plain = client_mod._decrypt_packet(const.PSK, real_notify_ct)
        self.assertEqual(plain[0], 0xE7)
        self.assertEqual(plain[1], 0x01)
        session_key = plain[2:18]
        self.assertEqual(len(session_key), 16)
        # Checksum must also be valid - if the PSK/algorithm were wrong,
        # this would not hold by chance (1/256 odds).
        x = 0
        for b in plain[:19]:
            x ^= b
        self.assertEqual(plain[19], x)


# ---------------------------------------------------------------------------
# Command construction (mocking send_command to capture what would be sent,
# so clamping/byte-layout logic is tested without a real BLE connection)
# ---------------------------------------------------------------------------


class TestCommandConstruction(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        self.client.send_command = AsyncMock(return_value=None)

    def _sent_prefix(self) -> bytes:
        self.assertTrue(self.client.send_command.called)
        (prefix,), _kwargs = self.client.send_command.call_args
        return prefix

    def test_set_zone_upper_on(self):
        run(self.client.set_zone(const.ZONE_UPPER, True))
        self.assertEqual(self._sent_prefix(), bytes([0x33, 0x30, const.ZONE_UPPER, 1]))

    def test_set_zone_lower_off(self):
        run(self.client.set_zone(const.ZONE_LOWER, False))
        self.assertEqual(self._sent_prefix(), bytes([0x33, 0x30, const.ZONE_LOWER, 0]))

    def test_set_brightness_normal(self):
        run(self.client.set_brightness_pct(50))
        self.assertEqual(self._sent_prefix(), bytes([0x33, 0x04, 50]))

    def test_set_brightness_clamps_above_100(self):
        run(self.client.set_brightness_pct(255))
        self.assertEqual(self._sent_prefix(), bytes([0x33, 0x04, 100]))

    def test_set_brightness_clamps_below_0(self):
        run(self.client.set_brightness_pct(-10))
        self.assertEqual(self._sent_prefix(), bytes([0x33, 0x04, 0]))

    def test_set_scene_bare_activation(self):
        run(self.client.set_scene((0x84, 0x4A)))
        self.assertEqual(self._sent_prefix(), bytes([0x33, 0x05, 0x04, 0x84, 0x4A]))

    def test_set_rgb_color_matches_real_captured_red(self):
        # Real captured command for "selected red" (see PROTOCOL.md section 4).
        run(self.client.set_rgb_color(0xFF, 0x00, 0x00))
        expected = bytes([0x33, 0x05, 0x15, 0x01, 0xFF, 0x00, 0x00, 0, 0, 0, 0, 0, 0xFF, 0x1F])
        self.assertEqual(self._sent_prefix(), expected)

    def test_set_rgb_color_matches_real_captured_blue(self):
        run(self.client.set_rgb_color(0x00, 0x00, 0xFF))
        expected = bytes([0x33, 0x05, 0x15, 0x01, 0x00, 0x00, 0xFF, 0, 0, 0, 0, 0, 0xFF, 0x1F])
        self.assertEqual(self._sent_prefix(), expected)

    def test_set_color_temp_min_matches_real_capture(self):
        # Real captured "max warmth" command: kelvin bytes 0a 8c (=2700),
        # tint ff ae 54.
        run(self.client.set_color_temp_kelvin(2700))
        prefix = self._sent_prefix()
        self.assertEqual(prefix[0:4], bytes([0x33, 0x05, 0x15, 0x01]))
        self.assertEqual(prefix[4:7], bytes([0xFF, 0xFF, 0xFF]))
        self.assertEqual(prefix[7:9], bytes([0x0A, 0x8C]))  # 2700 big-endian
        self.assertEqual(prefix[12:14], bytes([0xFF, 0x1F]))

    def test_set_color_temp_max_matches_real_capture(self):
        # Real captured "max cool" command: kelvin bytes 19 64 (=6500).
        run(self.client.set_color_temp_kelvin(6500))
        prefix = self._sent_prefix()
        self.assertEqual(prefix[7:9], bytes([0x19, 0x64]))  # 6500 big-endian

    def test_set_color_temp_clamps_to_device_range(self):
        run(self.client.set_color_temp_kelvin(1000))
        prefix = self._sent_prefix()
        kelvin = (prefix[7] << 8) | prefix[8]
        self.assertEqual(kelvin, const.MIN_COLOR_TEMP_KELVIN)

        self.client.send_command.reset_mock()
        run(self.client.set_color_temp_kelvin(9000))
        prefix = self._sent_prefix()
        kelvin = (prefix[7] << 8) | prefix[8]
        self.assertEqual(kelvin, const.MAX_COLOR_TEMP_KELVIN)

    def test_set_segment_color_matches_real_capture_bit5_red(self):
        # Real captured command (PROTOCOL.md 4.2): bit 5 (mask 0x0020) set
        # to red while tapping through individual segments in the app.
        # Full 20-byte capture (with checksum) was
        # "33051501ff0000000000000020000000000000fd"; drop the trailing
        # checksum byte since _sent_prefix() returns the pre-checksum
        # prefix passed into send_command().
        run(self.client.set_segment_color(0x0020, 0xFF, 0x00, 0x00))
        expected = bytes.fromhex("33051501ff0000000000000020000000000000fd")[:-1]
        self.assertEqual(self._sent_prefix(), expected)

    def test_set_segment_color_matches_real_capture_bit11_purple(self):
        # Real captured command: bit 11 (mask 0x0800) set to purple
        # (0x8b, 0x00, 0xff) - confirms the mask byte shifts into the
        # second (high) byte correctly, not just the first. Full capture:
        # "330515018b00ff0000000000000800000000005e".
        run(self.client.set_segment_color(0x0800, 0x8B, 0x00, 0xFF))
        expected = bytes.fromhex("330515018b00ff0000000000000800000000005e")[:-1]
        self.assertEqual(self._sent_prefix(), expected)

    def test_set_segment_brightness_matches_real_capture(self):
        # Real captured command (PROTOCOL.md 4.2): 100% brightness, mask
        # 0x0020 (same segment/bit as the red color test above). Full
        # capture: "3305150264200000000000000000000000000065".
        run(self.client.set_segment_brightness(0x0020, 100))
        expected = bytes.fromhex("3305150264200000000000000000000000000065")[:-1]
        self.assertEqual(self._sent_prefix(), expected)

    def test_set_segment_brightness_clamps_to_0_100(self):
        run(self.client.set_segment_brightness(0x0001, 255))
        self.assertEqual(self._sent_prefix()[4], 100)

        self.client.send_command.reset_mock()
        run(self.client.set_segment_brightness(0x0001, -10))
        self.assertEqual(self._sent_prefix()[4], 0)

    def test_set_segment_color_mask_byte_order_is_little_endian(self):
        # mask 0x1234 -> low byte 0x34 first, high byte 0x12 second
        run(self.client.set_segment_color(0x1234, 1, 2, 3))
        prefix = self._sent_prefix()
        self.assertEqual(prefix[12:14], bytes([0x34, 0x12]))


class TestKelvinToRgb(unittest.TestCase):
    def test_warmest_close_to_real_capture(self):
        # Real captured tint at 2700K: (255, 174, 84). Formula isn't exact
        # (it's a generic approximation, not device-specific), but should
        # land within a small tolerance per channel - see PROTOCOL.md 4.1.
        r, g, b = client_mod._kelvin_to_rgb(2700)
        self.assertEqual(r, 255)
        self.assertLess(abs(g - 174), 10)
        self.assertLess(abs(b - 84), 10)

    def test_coolest_close_to_real_capture(self):
        r, g, b = client_mod._kelvin_to_rgb(6500)
        self.assertEqual(r, 255)
        self.assertLess(abs(g - 249), 10)
        self.assertLess(abs(b - 251), 10)

    def test_output_always_in_byte_range(self):
        for kelvin in (2700, 3000, 4000, 5000, 6500):
            for channel in client_mod._kelvin_to_rgb(kelvin):
                self.assertGreaterEqual(channel, 0)
                self.assertLessEqual(channel, 255)


# ---------------------------------------------------------------------------
# Scene chunk building - verified against real captured uploads
# ---------------------------------------------------------------------------


class TestBuildSceneChunks(unittest.TestCase):
    def _diff_count(self, chunks: list[bytes], real_hex: list[str]) -> int:
        content = b"".join(c[2:19] for c in chunks)  # strip opcode+seq+checksum boundary handling below
        real = b"".join(bytes.fromhex(h)[2:19] for h in real_hex)
        return sum(1 for a, b in zip(content, real) if a != b)

    def test_graffiti_chunk_count_matches_real_capture(self):
        chunks = build_scene_chunks(GRAFFITI_SCENCEPARAM_B64)
        self.assertEqual(len(chunks), len(GRAFFITI_REAL_CHUNKS_HEX))

    def test_graffiti_header_matches_real_capture(self):
        chunks = build_scene_chunks(GRAFFITI_SCENCEPARAM_B64)
        real_first = bytes.fromhex(GRAFFITI_REAL_CHUNKS_HEX[0])
        # chunk format: [0xA3, seq, ...17 payload bytes...] (checksum not
        # included in build_scene_chunks output - that's added later by
        # _build_plaintext/_encrypt_packet).
        self.assertEqual(chunks[0][0], 0xA3)
        self.assertEqual(chunks[0][1], 0x00)
        self.assertEqual(chunks[0][2], 0x01)  # constant header marker
        self.assertEqual(chunks[0][3], len(chunks))  # chunk count
        # This must match the real capture's header exactly (both bytes
        # are structural, not content-dependent, so no tolerance needed).
        self.assertEqual(chunks[0][2], real_first[2])
        self.assertEqual(chunks[0][3], real_first[3])

    def test_graffiti_last_chunk_uses_ff_terminator(self):
        chunks = build_scene_chunks(GRAFFITI_SCENCEPARAM_B64)
        self.assertEqual(chunks[-1][1], 0xFF)
        for c in chunks[:-1]:
            self.assertNotEqual(c[1], 0xFF)

    def test_graffiti_flag_bit_matches_real_capture(self):
        # The critical fix from PROTOCOL.md section 6.3: byte 0 of the raw
        # scenceParam must have bit 0x08 set, or the scene silently fails
        # to render despite acking normally. Confirm our output's flag
        # byte matches what the real app actually sent.
        chunks = build_scene_chunks(GRAFFITI_SCENCEPARAM_B64)
        real_first = bytes.fromhex(GRAFFITI_REAL_CHUNKS_HEX[0])
        # chunks[0][4] is byte 0 of the (header-prefixed) content, i.e. the
        # first byte of the flag-patched scenceParam data.
        our_flag_byte = chunks[0][4]
        real_flag_byte = real_first[4]
        self.assertEqual(our_flag_byte, real_flag_byte)
        self.assertTrue(our_flag_byte & 0x08, "flag bit must be set")

    def test_graffiti_content_matches_real_capture_within_known_tolerance(self):
        # Expect near-exact match; the only legitimate differences are the
        # device's saved speed preference (not something we replicate -
        # we send the API's default) at up to 2 positions. See PROTOCOL.md
        # section 6.3 and the original investigation's diff analysis.
        chunks = build_scene_chunks(GRAFFITI_SCENCEPARAM_B64)
        diffs = self._diff_count(chunks, GRAFFITI_REAL_CHUNKS_HEX)
        self.assertLessEqual(diffs, 2, f"expected <=2 byte differences (speed-preference only), got {diffs}")

    def test_cornfield_content_matches_real_capture_within_known_tolerance(self):
        chunks = build_scene_chunks(CORNFIELD_SCENCEPARAM_B64)
        self.assertEqual(len(chunks), len(CORNFIELD_REAL_CHUNKS_HEX))
        diffs = self._diff_count(chunks, CORNFIELD_REAL_CHUNKS_HEX)
        self.assertLessEqual(diffs, 2, f"expected <=2 byte differences (speed-preference only), got {diffs}")

    def test_all_chunks_are_17_bytes_of_payload(self):
        chunks = build_scene_chunks(GRAFFITI_SCENCEPARAM_B64)
        for c in chunks:
            self.assertEqual(len(c), 19)  # opcode(1) + seq(1) + payload(17)

    def test_small_scene_still_produces_valid_single_chunk(self):
        tiny_data = base64.b64encode(bytes(range(5))).decode()
        chunks = build_scene_chunks(tiny_data)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][1], 0xFF)  # only chunk is also the last chunk

    def test_exact_17_byte_boundary_scene(self):
        # content = 2-byte header + 15 data bytes = 17 bytes exactly -> 1 chunk.
        data = base64.b64encode(bytes(range(15))).decode()
        chunks = build_scene_chunks(data)
        self.assertEqual(len(chunks), 1)

    def test_just_over_17_byte_boundary_scene(self):
        # content = 2-byte header + 16 data bytes = 18 bytes -> 2 chunks.
        data = base64.b64encode(bytes(range(16))).decode()
        chunks = build_scene_chunks(data)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[1][1], 0xFF)


# ---------------------------------------------------------------------------
# Status parsing - the mode-dependent-layout regression test
# ---------------------------------------------------------------------------


class TestParseStatus(unittest.TestCase):
    def setUp(self):
        self.client = make_client("5C:E7:53:F4:74:57")

    def test_scene_mode_mac_and_hw_version(self):
        status = self.client._parse_status(STATUS_CHUNKS_SCENE_MODE)
        self.assertEqual(status.ble_mac, "5C:E7:53:F4:74:57")
        self.assertEqual(status.wifi_mac, "5C:E7:53:F4:74:56")
        self.assertEqual(status.hardware_version, "1.04.03")

    def test_rgb_mode_mac_and_hw_version_not_corrupted(self):
        # This is the actual regression test for the bug documented in
        # PROTOCOL.md section 5.1: chunk 0x00 is entirely absent in this
        # real capture, and everything else shifts by one byte. Before the
        # anchor-based fix, this produced a garbled MAC and hw_version
        # like "41.01.04" instead of "1.04.03".
        status = self.client._parse_status(STATUS_CHUNKS_RGB_MODE)
        self.assertEqual(status.ble_mac, "5C:E7:53:F4:74:57")
        self.assertEqual(status.wifi_mac, "5C:E7:53:F4:74:56")
        self.assertEqual(status.hardware_version, "1.04.03")

    def test_both_modes_agree_on_static_device_facts(self):
        # MAC address and hardware version are static device properties -
        # they must never differ between the two modes for the same device.
        scene_status = self.client._parse_status(STATUS_CHUNKS_SCENE_MODE)
        rgb_status = self.client._parse_status(STATUS_CHUNKS_RGB_MODE)
        self.assertEqual(scene_status.ble_mac, rgb_status.ble_mac)
        self.assertEqual(scene_status.wifi_mac, rgb_status.wifi_mac)
        self.assertEqual(scene_status.hardware_version, rgb_status.hardware_version)

    def test_brightness_and_scene_id_only_available_with_chunk_00(self):
        # RGB mode's real capture has no chunk 0x00, so these fields must
        # be reported as unknown rather than guessed from misaligned data.
        rgb_status = self.client._parse_status(STATUS_CHUNKS_RGB_MODE)
        self.assertIsNone(rgb_status.brightness_pct)
        self.assertIsNone(rgb_status.scene_id)

    def test_zone_state_shift_applied_when_chunk_00_absent(self):
        # These two fixtures happen to have upper==lower, so this only
        # confirms parsing doesn't crash and produces a bool for both modes'
        # chunk layouts (chunk 0x00 present vs absent) - see
        # test_zone_upper_lower_byte_assignment below for the test that
        # actually locks in which byte is which zone.
        for chunks in (STATUS_CHUNKS_SCENE_MODE, STATUS_CHUNKS_RGB_MODE):
            status = self.client._parse_status(chunks)
            self.assertIsInstance(status.zone_upper_on, bool)
            self.assertIsInstance(status.zone_lower_on, bool)

    def test_zone_upper_lower_full_truth_table(self):
        # Full 4-state truth table captured live on two devices (2026-07-03)
        # by stepping set_zone through every (upper, lower) combination and
        # reading the terminator chunk each time. byte 14 = LOWER zone,
        # byte 15 = UPPER zone; byte 13 is a static 0x02 that does NOT track
        # power (an earlier revision read it as a zone, which is why HA
        # reported a zone ON even when the light was physically off). This
        # test encodes all four real states so the mapping can't silently
        # regress to a single-state coincidence again.
        #
        # Real terminator bytes (from the capture), byte13=02 constant:
        #   U=0 L=0 -> ...30 02 00 00   U=1 L=0 -> ...30 02 00 01
        #   U=0 L=1 -> ...30 02 01 00   U=1 L=1 -> ...30 02 01 01
        base = bytes.fromhex("00000080000000804102020130020000a5")
        for upper, lower in ((0, 0), (1, 0), (0, 1), (1, 1)):
            term = bytearray(base)
            term[13] = 0x02  # static marker, must be ignored
            term[14] = lower
            term[15] = upper
            chunks = {0x00: bytes(17), 0x05: bytes(term)}  # chunk00 present -> shift 0
            status = self.client._parse_status(chunks)
            self.assertEqual(
                status.zone_upper_on, bool(upper), f"upper wrong for U={upper} L={lower}"
            )
            self.assertEqual(
                status.zone_lower_on, bool(lower), f"lower wrong for U={upper} L={lower}"
            )

    def test_empty_chunks_produces_all_none_without_raising(self):
        status = self.client._parse_status({})
        self.assertIsNone(status.ble_mac)
        self.assertIsNone(status.wifi_mac)
        self.assertIsNone(status.hardware_version)
        self.assertIsNone(status.brightness_pct)
        self.assertIsNone(status.scene_id)
        self.assertIsNone(status.zone_upper_on)
        self.assertIsNone(status.zone_lower_on)

    def test_wrong_device_mac_not_found_in_stream_gives_none(self):
        # A client for a *different* address querying the same chunks
        # should not find its own MAC in the stream (sanity check that the
        # anchor search is address-specific, not just "any MAC-shaped
        # bytes").
        other_client = make_client("AA:BB:CC:DD:EE:FF")
        status = other_client._parse_status(STATUS_CHUNKS_SCENE_MODE)
        self.assertIsNone(status.ble_mac)
        self.assertIsNone(status.wifi_mac)
        self.assertIsNone(status.hardware_version)

    def test_unrecognized_extra_chunk_key_is_harmless(self):
        # Simulates the real-world observation of an extra/unexpected
        # chunk type showing up (PROTOCOL.md section 5) - parsing should
        # simply ignore chunk keys it doesn't recognize rather than
        # misparsing around them.
        chunks_with_extra = dict(STATUS_CHUNKS_SCENE_MODE)
        chunks_with_extra[0x07] = b"\xaa" * 17  # unrecognized chunk type
        status = self.client._parse_status(chunks_with_extra)
        self.assertEqual(status.ble_mac, "5C:E7:53:F4:74:57")
        self.assertEqual(status.hardware_version, "1.04.03")

    def test_short_chunk_ff_does_not_raise(self):
        chunks = {0xFF: b"\x00" * 5}  # too short to contain zone-state bytes
        status = self.client._parse_status(chunks)
        self.assertIsNone(status.zone_upper_on)
        self.assertIsNone(status.zone_lower_on)

    def test_segments_none_when_chunks_05_08_absent(self):
        # Neither fixture includes chunks 0x05-0x08 (both predate this
        # project ever requesting them - PROTOCOL.md 5.3) - segments must
        # stay None, not raise or return a bogus partial list.
        status = self.client._parse_status(STATUS_CHUNKS_SCENE_MODE)
        self.assertIsNone(status.segments)
        status = self.client._parse_status(STATUS_CHUNKS_RGB_MODE)
        self.assertIsNone(status.segments)

    def test_segments_populated_from_real_capture(self):
        # Full end-to-end real capture (device D4:13:68:21:D0:75, solid
        # green) - this device's MAC must be used or the unrelated
        # MAC-anchor search (for ble_mac/wifi_mac/hw_version) would fail,
        # though that's not what this test is checking.
        client = make_client("D4:13:68:21:D0:75")
        status = client._parse_status(STATUS_CHUNKS_WITH_SEGMENTS)
        self.assertIsNotNone(status.segments)
        self.assertEqual(len(status.segments), 12)
        for i, (expected, segment) in enumerate(zip(STATUS_SEGMENTS_EXPECTED, status.segments)):
            brightness, r, g, b = expected
            self.assertEqual(segment.index, i)
            self.assertEqual(segment.brightness_pct, brightness)
            self.assertEqual((segment.r, segment.g, segment.b), (r, g, b))

    def test_zone_state_still_populated_with_fuller_chunk_set(self):
        # Regression test for a real bug found live: switching
        # _query_status_chunks() to the fuller trigger (PROTOCOL.md 5.3)
        # changed what ends up tagged chunk 0xFF - the bytes that used to
        # carry zone state there in the shorter response are now tagged
        # 0x05 instead (0xFF becomes the tail of segment data). Naively
        # keeping the old "always read zone bits from chunk 0xFF" logic
        # silently produced zone_upper_on=None/zone_lower_on=None with
        # this fuller chunk set instead of a wrong-but-present value. This
        # doesn't assert the *correct* value (that's section 5.2's
        # separate, still-unresolved mystery) - only that a real,
        # fuller-response capture doesn't regress to None.
        client = make_client("D4:13:68:21:D0:75")
        status = client._parse_status(STATUS_CHUNKS_WITH_SEGMENTS)
        self.assertIsNotNone(status.zone_upper_on)
        self.assertIsNotNone(status.zone_lower_on)


class TestParseSegmentRecords(unittest.TestCase):
    """Direct tests of the pure _parse_segment_records helper, isolated
    from the rest of _parse_status."""

    def test_real_capture_all_12_segments(self):
        segments = client_mod._parse_segment_records(STATUS_CHUNKS_WITH_SEGMENTS)
        self.assertIsNotNone(segments)
        self.assertEqual(len(segments), 12)
        self.assertEqual(segments[0].brightness_pct, 50)
        self.assertEqual((segments[0].r, segments[0].g, segments[0].b), (0, 255, 0))
        # bit/record 11 specifically - confirmed via live testing to be
        # the one at the far end of the range, spanning the 0xFF chunk.
        self.assertEqual(segments[11].brightness_pct, 41)
        self.assertEqual((segments[11].r, segments[11].g, segments[11].b), (0, 255, 0))

    def test_missing_any_of_05_08_gives_none(self):
        for missing in (0x05, 0x06, 0x07, 0x08):
            chunks = {k: v for k, v in STATUS_CHUNKS_WITH_SEGMENTS.items() if k != missing}
            self.assertIsNone(
                client_mod._parse_segment_records(chunks),
                f"expected None when chunk 0x{missing:02x} is missing",
            )

    def test_missing_0xff_gives_none(self):
        # 0x05-0x08 alone (68 bytes) aren't enough to reach record 11
        # (needs some of 0xFF too) - must not return a truncated 11-item
        # list silently.
        chunks = {k: v for k, v in STATUS_CHUNKS_WITH_SEGMENTS.items() if k != 0xFF}
        self.assertIsNone(client_mod._parse_segment_records(chunks))

    def test_empty_chunks_gives_none(self):
        self.assertIsNone(client_mod._parse_segment_records({}))


class TestFormatMac(unittest.TestCase):
    def test_format_mac_uppercase_colon_separated(self):
        result = client_mod._format_mac(bytes([0x5C, 0xE7, 0x53, 0xF4, 0x74, 0x57]))
        self.assertEqual(result, "5C:E7:53:F4:74:57")


class TestParseMetadataFieldText(unittest.TestCase):
    def test_real_capture_serial_number_field(self):
        # Real capture of "ab" field 0x05's reassembled response
        # (PROTOCOL.md 8) - two independently captured sessions produced
        # this identical value, strong evidence it's stable.
        header = bytes([0x02, 0x00, 0x04, 0x01, 0x05])
        raw = header + b"F19130565FE741AF" + b"\x00" * 13
        self.assertEqual(client_mod._parse_metadata_field_text(raw), "F19130565FE741AF")

    def test_empty_response_gives_none(self):
        self.assertIsNone(client_mod._parse_metadata_field_text(b""))

    def test_header_only_no_value_gives_none(self):
        self.assertIsNone(client_mod._parse_metadata_field_text(bytes([0x01, 0x00, 0x00, 0x01, 0x05])))

    def test_non_ascii_payload_gives_none_not_a_crash(self):
        header = bytes([0x02, 0x00, 0x04, 0x01, 0x05])
        raw = header + bytes([0xFF, 0xFE, 0xFD])
        self.assertIsNone(client_mod._parse_metadata_field_text(raw))


if __name__ == "__main__":
    unittest.main(verbosity=2)
