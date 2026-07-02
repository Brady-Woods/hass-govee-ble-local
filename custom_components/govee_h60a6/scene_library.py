"""Fetch the full scene/effect library from Govee's public app API.

This gives us the exact effect data (`scenceParam`, base64-encoded) needed
to build a correct `a3`-chunked upload for any scene, instead of relying on
the device already having that scene cached from prior app use.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

LIBRARY_URL = "https://app2.govee.com/appsku/v1/light-effect-libraries"
REQUEST_TIMEOUT = 10


@dataclass
class SceneData:
    scene_code: int
    scenceParam: str  # base64, kept as-is until upload time


async def async_fetch_scene_library(hass: HomeAssistant, sku: str) -> dict[str, SceneData]:
    """Return {scene_name: SceneData}. Returns an empty dict on any failure."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            LIBRARY_URL,
            params={"sku": sku},
            headers={"AppVersion": "5.6.01"},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("Could not fetch Govee scene library for %s: %s", sku, err)
        return {}

    scenes: dict[str, SceneData] = {}
    try:
        for category in payload["data"]["categories"]:
            for scene in category["scenes"]:
                effects = scene.get("lightEffects") or []
                if not effects:
                    continue
                effect = effects[0]
                scenes[scene["sceneName"]] = SceneData(
                    scene_code=effect["sceneCode"],
                    scenceParam=effect["scenceParam"],
                )
    except (KeyError, TypeError, IndexError) as err:
        _LOGGER.warning("Unexpected Govee scene library response shape: %s", err)
        return {}

    _LOGGER.debug("Fetched %d scenes from Govee library for %s", len(scenes), sku)
    return scenes


def build_scene_chunks(scenceParam_b64: str) -> list[bytes]:
    """Split a scene's effect data into the `a3`-chunk payload sequence.

    Each returned entry is a 19-byte prefix: [0xA3, seq_byte, <=17 data bytes],
    ready to be checksummed+encrypted+sent as-is (seq bytes: 0x00, 0x01, ...
    for all but the last chunk, which always uses 0xFF).
    """
    data = bytearray(base64.b64decode(scenceParam_b64))
    # Byte 0 of the API's raw scenceParam is an "unconfirmed template" flag;
    # the device silently no-ops (falls back to off) unless this bit is set,
    # even though it still acks the upload/activation and updates scene_id.
    # Confirmed empirically: the real app always sends this bit set.
    data[0] |= 0x08
    data = bytes(data)
    content_len = 2 + len(data)
    chunk_count = -(-content_len // 17)  # ceiling division
    content = bytes([0x01, chunk_count]) + data

    chunks: list[bytes] = []
    num_pieces = -(-len(content) // 17)
    for i in range(num_pieces):
        piece = content[i * 17 : (i + 1) * 17]
        piece = piece + b"\x00" * (17 - len(piece))
        seq = 0xFF if i == num_pieces - 1 else i
        chunks.append(bytes([0xA3, seq]) + piece)
    return chunks
