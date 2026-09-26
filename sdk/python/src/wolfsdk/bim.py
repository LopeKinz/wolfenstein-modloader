"""BIM images: header, decode to RGBA, RGBA8 encode (SPEC §8)."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Tuple

from .errors import FormatError

MAGIC = b"\x09MIB"
HEADER_FIELDS = 13
DATA_START = 8 + HEADER_FIELDS * 4

FORMAT_RGBA8 = 3
FORMAT_A8 = 5
FORMAT_BC1 = 10
FORMAT_BC3 = 11
FORMAT_NAMES = {FORMAT_RGBA8: "RGBA8", FORMAT_A8: "A8", FORMAT_BC1: "BC1", FORMAT_BC3: "BC3"}


@dataclass
class Mip:
    width: int
    height: int
    data: bytes = field(repr=False)


@dataclass
class Bim:
    hash: int
    header: List[int]
    mips: List[Mip]

    @property
    def texture_type(self) -> int:
        return self.header[0]

    @property
    def width(self) -> int:
        return self.header[1]

    @property
    def height(self) -> int:
        return self.header[2]

    @property
    def depth(self) -> int:
        return self.header[3]

    @property
    def mip_count(self) -> int:
        return self.header[4]

    @property
    def format(self) -> int:
        return self.header[6]

    @property
    def format_name(self) -> str:
        return FORMAT_NAMES.get(self.format, f"unknown({self.format})")

    @property
    def base_width(self) -> int:
        return self.header[11]

    @property
    def base_height(self) -> int:
        return self.header[12]


def parse_bim(data: bytes) -> Bim:
    if len(data) < DATA_START or data[4:8] != MAGIC:
        raise FormatError("BIM: bad magic")
    (h,) = struct.unpack_from("<I", data, 0)
    header = list(struct.unpack_from(">13I", data, 8))
    mips, pos = [], DATA_START
    for _ in range(header[4]):
        if pos + 12 > len(data):
            break
        w, hh, size = struct.unpack_from(">III", data, pos)
        pos += 12
        if pos + size > len(data):
            break
        mips.append(Mip(w, hh, data[pos : pos + size]))
        pos += size
    if not mips:
        raise FormatError("BIM: no mip levels")
    return Bim(h, header, mips)


def expected_size(fmt: int, w: int, h: int) -> int:
    bw, bh = (w + 3) // 4, (h + 3) // 4
    if fmt == FORMAT_RGBA8:
        return w * h * 4
    if fmt == FORMAT_A8:
        return w * h
    if fmt == FORMAT_BC1:
        return bw * bh * 8
    if fmt == FORMAT_BC3:
        return bw * bh * 16
    raise FormatError(f"BIM: unsupported format code {fmt}")


def _rgb565(c: int) -> Tuple[int, int, int]:
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def _color_palette(block: bytes, four_color: bool):
    c0, c1 = struct.unpack_from("<HH", block, 0)
    p0, p1 = _rgb565(c0), _rgb565(c1)
    if c0 > c1 or four_color:
        p2 = tuple((2 * a + b) // 3 for a, b in zip(p0, p1)) + (255,)
        p3 = tuple((a + 2 * b) // 3 for a, b in zip(p0, p1)) + (255,)
    else:
        p2 = tuple((a + b) // 2 for a, b in zip(p0, p1)) + (255,)
        p3 = (0, 0, 0, 0)
    return [p0 + (255,), p1 + (255,), p2, p3], struct.unpack_from("<I", block, 4)[0]


def _alpha_palette(block: bytes):
    a0, a1 = block[0], block[1]
    if a0 > a1:
        pal = [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
    else:
        pal = [a0, a1] + [((5 - i) * a0 + i * a1) // 5 for i in range(1, 5)] + [0, 255]
    bits = int.from_bytes(block[2:8], "little")
    return pal, bits


def _decode_blocks(data: bytes, w: int, h: int, bc3: bool) -> bytes:
    out = bytearray(w * h * 4)
    bw, bh = (w + 3) // 4, (h + 3) // 4
    size = 16 if bc3 else 8
    for by in range(bh):
        for bx in range(bw):
            off = (by * bw + bx) * size
            blk = data[off : off + size]
            if bc3:
                apal, abits = _alpha_palette(blk[:8])
                pal, idx = _color_palette(blk[8:], True)
            else:
                pal, idx = _color_palette(blk, False)
            for t in range(16):
                x, y = bx * 4 + (t & 3), by * 4 + (t >> 2)
                if x >= w or y >= h:
                    continue
                r, g, b, a = pal[(idx >> (2 * t)) & 3]
                if bc3:
                    a = apal[(abits >> (3 * t)) & 7]
                o = (y * w + x) * 4
                out[o : o + 4] = bytes((r, g, b, a))
    return bytes(out)


def decode(bim: Bim, mip: int = 0) -> Tuple[int, int, bytes]:
    """Decode a mip level to ``(width, height, rgba)``."""
    m = bim.mips[mip]
    w, h, fmt = m.width, m.height, bim.format
    need = expected_size(fmt, w, h)
    if len(m.data) < need:
        raise FormatError(f"BIM: mip {mip} has {len(m.data)} bytes, needs {need}")
    if fmt == FORMAT_RGBA8:
        return w, h, bytes(m.data[:need])
    if fmt == FORMAT_A8:
        out = bytearray(w * h * 4)
        for i, v in enumerate(m.data[:need]):
            out[i * 4 : i * 4 + 4] = bytes((v, v, v, 255))
        return w, h, bytes(out)
    return w, h, _decode_blocks(m.data, w, h, fmt == FORMAT_BC3)


def encode_rgba8(template: Bim, width: int, height: int, rgba: bytes) -> bytes:
    """Build an RGBA8 BIM with one mip, header copied from ``template``."""
    if len(rgba) != width * height * 4:
        raise FormatError("RGBA buffer size does not match dimensions")
    header = list(template.header)
    header[1] = header[11] = width
    header[2] = header[12] = height
    header[4] = 1
    header[6] = FORMAT_RGBA8
    out = struct.pack("<I", template.hash) + MAGIC + struct.pack(">13I", *header)
    return out + struct.pack(">III", width, height, len(rgba)) + rgba


def build_bim(hash: int, header: List[int], mips: List[Tuple[int, int, bytes]]) -> bytes:
    """Serialise a BIM from raw parts (tests and synthetic data)."""
    out = bytearray(struct.pack("<I", hash) + MAGIC + struct.pack(">13I", *header))
    for w, h, data in mips:
        out += struct.pack(">III", w, h, len(data)) + data
    return bytes(out)
