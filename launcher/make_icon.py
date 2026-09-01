#!/usr/bin/env python3
"""Generate launcher/app.ico (16/32/48 px, PNG-compressed, Vista+ format).

Icon: blue rounded square with a white subtitle-style chat bubble
(rounded rect + tail + three dots). Pure pixel math + supersampling,
PNG encoding via zlib (stdlib only) so rc.exe accepts the file.
"""
import math
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "app.ico")
SIZES = [16, 32, 48]
SS = 6  # supersample factor per pixel


def sd_round_rect(px, py, x0, y0, x1, y1, r):
    cx = max(x0 + r, min(px, x1 - r))
    cy = max(y0 + r, min(py, y1 - r))
    return math.hypot(px - cx, py - cy) - r


def sd_circle(px, py, cx, cy, r):
    return math.hypot(px - cx, py - cy) - r


def tri_halfplanes(px, py, a, b, c):
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    d1, d2, d3 = side(a, b, (px, py)), side(b, c, (px, py)), side(c, a, (px, py))
    if d1 >= 0 and d2 >= 0 and d3 >= 0:
        return 0.0
    return max(min(d1, 0.0), min(d2, 0.0), min(d3, 0.0))


def covered(px, py, size):
    """1.0 if the subpixel (px,py) is opaque, else 0.0."""
    if sd_round_rect(px, py, 0, 0, 32, 32, 7.0) > 0:
        return 0.0
    if sd_round_rect(px, py, 6.5, 5.5, 25.5, 21.5, 4.0) < 0:
        return 0.0
    if tri_halfplanes(px, py, (9.0, 20.0), (5.5, 26.5), (15.0, 21.0)) > 0:
        return 0.0
    for cx, cy in ((12.0, 13.5), (16.0, 13.5), (20.0, 13.5)):
        if sd_circle(px, py, cx, cy, 1.7) < 0:
            return 0.0
    return 1.0


def rgba_rows(size):
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            acc = 0.0
            for sy in range(SS):
                for sx in range(SS):
                    px = (x + (sx + 0.5) / SS) * (32.0 / size)
                    py = (y + (sy + 0.5) / SS) * (32.0 / size)
                    acc += covered(px, py, size)
            cov = acc / (SS * SS)
            blue = int(0x1E + (0x3B - 0x1E) * y / max(size - 1, 1))
            row += bytes((0x40, 0x82, blue, int(round(cov * 255))))
        rows.append(bytes(row))
    return rows


def png_encode(rows, size):
    def chunk(tag, payload):
        block = tag + payload
        return struct.pack(">I", len(payload)) + block + \
            struct.pack(">I", zlib.crc32(block) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + row for row in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def make_ico():
    images = [(size, png_encode(rgba_rows(size), size)) for size in SIZES]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for size, data in images:
        out += struct.pack("<BBBBHHII", size & 0xFF, size & 0xFF, 0, 0,
                           1, 32, len(data), offset)
        offset += len(data)
    for _, data in images:
        out += data
    with open(OUT, "wb") as f:
        f.write(out)
    print("wrote %s (%d bytes, %d sizes)" % (OUT, len(out), len(images)))


if __name__ == "__main__":
    make_ico()