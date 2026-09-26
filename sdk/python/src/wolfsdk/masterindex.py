"""``base/master.index`` (SPEC §2)."""

from __future__ import annotations

import struct
from typing import List, Tuple

from .errors import FormatError

MAGIC = b"\x03SER"


def parse_master_index(data: bytes) -> List[Tuple[str, str]]:
    if data[:4] != MAGIC:
        raise FormatError("master.index: bad magic")
    if len(data) < 8:
        raise FormatError("master.index: truncated header")
    (count,) = struct.unpack_from(">I", data, 4)
    pos, names = 8, []
    for _ in range(count * 2):
        if pos + 4 > len(data):
            raise FormatError("master.index: truncated")
        (length,) = struct.unpack_from("<I", data, pos)
        pos += 4
        if pos + length > len(data):
            raise FormatError("master.index: truncated name")
        raw = data[pos : pos + length]
        names.append(raw.split(b"\x00", 1)[0].decode("ascii", "replace"))
        pos += length
    return [(names[i], names[i + 1]) for i in range(0, len(names), 2)]


def build_master_index(pairs: List[Tuple[str, str]]) -> bytes:
    out = bytearray(MAGIC + struct.pack(">I", len(pairs)))
    for pair in pairs:
        for name in pair:
            raw = name.encode("ascii") + b"\x00"
            out += struct.pack("<I", len(raw)) + raw
    return bytes(out)
