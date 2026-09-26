"""Audio descriptors and streamed containers (SPEC §10)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import BinaryIO, List, Union

from .errors import FormatError

ENGLISH_PREFIX = "sound/vo/english/"


@dataclass
class SampleInfo:
    language: str
    hash: int
    length: int
    granule: int
    format_tag: int
    channels: int
    sample_rate: int
    avg_bytes_per_sec: int
    block_align: int
    bits_per_sample: int

    @property
    def duration(self) -> float:
        return self.granule / self.sample_rate if self.sample_rate else 0.0


def _block(data: bytes, off: int, language: str) -> SampleInfo:
    if off + 42 > len(data):
        raise FormatError("bsnf: truncated sample block")
    h, _, granule, _, length = struct.unpack_from(">IIIII", data, off)
    tag, ch, rate, avg, align, bits = struct.unpack_from("<HHIIHH", data, off + 20)
    return SampleInfo(language, h, length, granule, tag, ch, rate, avg, align, bits)


def parse_bsnf(data: bytes) -> List[SampleInfo]:
    if data[:4] != b"bsnf" or len(data) < 8:
        raise FormatError("bsnf: bad magic")
    (count,) = struct.unpack_from(">I", data, 4)
    if count == 1:
        return [_block(data, 0x20, "")]
    out = []
    for i in range(count):
        p = 8 + i * 24
        if p + 24 > len(data):
            raise FormatError("bsnf: truncated language table")
        name = data[p : p + 16].split(b"\x00", 1)[0].decode("ascii", "replace")
        _, off = struct.unpack_from(">II", data, p + 16)
        out.append(_block(data, off, name))
    return out


def stream_container_for(asset_name: str) -> str:
    return "english.streamed" if asset_name.startswith(ENGLISH_PREFIX) else "streamed.resources"


def ogg_stream_length(src: Union[bytes, BinaryIO], offset: int) -> int:
    """Walk Ogg pages from ``offset`` up to and including the EOS page."""
    def read(pos: int, n: int) -> bytes:
        if isinstance(src, (bytes, bytearray, memoryview)):
            return bytes(src[pos : pos + n])
        src.seek(pos)
        return src.read(n)

    pos = offset
    while True:
        head = read(pos, 27)
        if len(head) < 27 or head[:4] != b"OggS":
            raise FormatError(f"Ogg: no page at offset {pos}")
        flags, nseg = head[5], head[26]
        table = read(pos + 27, nseg)
        if len(table) < nseg:
            raise FormatError("Ogg: truncated segment table")
        pos += 27 + nseg + sum(table)
        if flags & 0x04:
            return pos - offset
