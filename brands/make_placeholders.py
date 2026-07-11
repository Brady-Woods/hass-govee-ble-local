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

import struct
import zlib

# Govee-ish blue tile + soft white mark. Tweak freely.
TILE = (37, 99, 235, 255)      # #2563EB
MARK = (255, 255, 255, 235)


def _png(width: int, height: int, pixels: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(pixels, 9))
            + chunk(b"IEND", b""))


def _rounded_rect(x: float, y: float, x0: float, y0: float, x1: float, y1: float,
                  rad: float) -> bool:
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + rad), x1 - rad)
    cy = min(max(y, y0 + rad), y1 - rad)
    return (x - cx) ** 2 + (y - cy) ** 2 <= rad * rad


def render(width: int, height: int) -> bytes:
    s = min(width, height)
    m = s * 0.06                      # margin
    rad = s * 0.22                    # tile corner radius
    ccx, ccy = width / 2, height / 2  # centered mark
    cr = s * 0.26                     # mark radius
    ring = s * 0.045                  # ring thickness (hollow circle)
    rows = bytearray()
    for py in range(height):
        rows.append(0)                # PNG filter: none
        for px in range(width):
            x, y = px + 0.5, py + 0.5
            r = g = b = a = 0
            if _rounded_rect(x, y, m, m, width - m, height - m, rad):
                r, g, b, a = TILE
                d = ((x - ccx) ** 2 + (y - ccy) ** 2) ** 0.5
                if cr - ring <= d <= cr:          # hollow ring
                    r, g, b, a = MARK
                elif d <= s * 0.06:               # center dot
                    r, g, b, a = MARK
            rows += bytes((r, g, b, a))
    return _png(width, height, bytes(rows))


def main() -> None:
    import pathlib
    here = pathlib.Path(__file__).parent
    for name, (w, h) in {
        "icon.png": (256, 256),
        "icon@2x.png": (512, 512),
        "logo.png": (256, 128),
        "logo@2x.png": (512, 256),
    }.items():
        (here / name).write_bytes(render(w, h))
        print("wrote", name, f"{w}x{h}")


if __name__ == "__main__":
    main()
