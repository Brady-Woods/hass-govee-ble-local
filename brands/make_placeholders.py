#!/usr/bin/env python3
"""Generate PLACEHOLDER brand assets at the sizes home-assistant/brands expects.

Pure standard library (zlib) — no Pillow. Produces transparent-cornered RGBA PNGs:
  icon.png       256x256   icon@2x.png   512x512   (square)
  logo.png       256x128   logo@2x.png   512x256   (rectangular)

These are intentionally generic placeholders — replace them with real artwork before
submitting to https://github.com/home-assistant/brands (see README.md in this folder).

    python3 brands/make_placeholders.py
"""
from __future__ import annotations

import pathlib
import struct
import zlib

# Govee-ish blue tile + soft white mark. Tweak freely.
TILE = (37, 99, 235, 255)      # #2563EB
MARK = (255, 255, 255, 235)

# Geometry, as fractions of the shorter side.
MARGIN = 0.06          # tile inset from the canvas edge
CORNER_RADIUS = 0.22   # tile corner radius
RING_RADIUS = 0.26     # outer radius of the hollow ring mark
RING_THICKNESS = 0.045
DOT_RADIUS = 0.06      # solid centre dot

SIZES = {
    "icon.png": (256, 256),
    "icon@2x.png": (512, 512),
    "logo.png": (256, 128),
    "logo@2x.png": (512, 256),
}


def _png(width: int, height: int, pixels: bytes) -> bytes:
    """Encode raw filtered RGBA scanlines as a PNG file."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(pixels, 9))
            + chunk(b"IEND", b""))


def _in_rounded_rect(
    x: float, y: float, rect: tuple[float, float, float, float], radius: float
) -> bool:
    """Return True if (x, y) lies inside ``rect`` (x0, y0, x1, y1) with rounded corners."""
    x0, y0, x1, y1 = rect
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + radius), x1 - radius)
    cy = min(max(y, y0 + radius), y1 - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius


def render(width: int, height: int) -> bytes:
    """Render the placeholder (rounded tile with a ring mark) as PNG bytes."""
    s = min(width, height)
    m = s * MARGIN
    tile = (m, m, width - m, height - m)
    ccx, ccy = width / 2, height / 2  # centered mark
    ring_outer = s * RING_RADIUS
    ring_inner = ring_outer - s * RING_THICKNESS
    dot = s * DOT_RADIUS
    rows = bytearray()
    for py in range(height):
        rows.append(0)                # PNG filter: none
        for px in range(width):
            x, y = px + 0.5, py + 0.5
            pixel: tuple[int, int, int, int] = (0, 0, 0, 0)
            if _in_rounded_rect(x, y, tile, s * CORNER_RADIUS):
                d = ((x - ccx) ** 2 + (y - ccy) ** 2) ** 0.5
                pixel = MARK if ring_inner <= d <= ring_outer or d <= dot else TILE
            rows.extend(pixel)
    return _png(width, height, bytes(rows))


def main() -> None:
    """Write every placeholder asset next to this script."""
    here = pathlib.Path(__file__).parent
    for name, (w, h) in SIZES.items():
        (here / name).write_bytes(render(w, h))
        print("wrote", name, f"{w}x{h}")


if __name__ == "__main__":
    main()
