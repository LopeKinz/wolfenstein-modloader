"""Minimal RGBA PNG encoder/decoder (SPEC §9)."""

from __future__ import annotations

import struct
import zlib
from typing import Tuple

from .errors import FormatError

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    if len(rgba) != width * height * 4:
        raise FormatError("RGBA buffer size does not match dimensions")
    stride = width * 4
    raw = b"".join(b"\x00" + rgba[y * stride : (y + 1) * stride] for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b"")


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(data: bytes) -> Tuple[int, int, bytes]:
    if data[:8] != SIGNATURE:
        raise FormatError("PNG: bad signature")
    pos, idat, ihdr = 8, bytearray(), None
    while pos + 8 <= len(data):
        (n,) = struct.unpack_from(">I", data, pos)
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if ihdr is None:
        raise FormatError("PNG: missing IHDR")
    w, h, depth, ctype, _, _, interlace = ihdr
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
    if depth != 8 or channels is None or interlace != 0:
        raise FormatError(f"PNG: unsupported (depth {depth}, colour type {ctype}, interlace {interlace})")
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as e:
        raise FormatError(f"PNG: {e}") from None
    bpp, stride = channels, w * channels
    if len(raw) < h * (stride + 1):
        raise FormatError("PNG: image data truncated")
    prev = bytearray(stride)
    pixels = bytearray()
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 0xFF
            elif f == 2:
                line[i] = (line[i] + b) & 0xFF
            elif f == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
            elif f == 4:
                line[i] = (line[i] + _paeth(a, b, c)) & 0xFF
            elif f != 0:
                raise FormatError(f"PNG: bad filter {f}")
        pixels += line
        prev = line
    if channels == 4:
        return w, h, bytes(pixels)
    out = bytearray(w * h * 4)
    for i in range(w * h):
        px = pixels[i * channels : (i + 1) * channels]
        if channels == 3:
            out[i * 4 : i * 4 + 4] = bytes((px[0], px[1], px[2], 255))
        elif channels == 1:
            out[i * 4 : i * 4 + 4] = bytes((px[0], px[0], px[0], 255))
        else:
            out[i * 4 : i * 4 + 4] = bytes((px[0], px[0], px[0], px[1]))
    return w, h, bytes(out)
