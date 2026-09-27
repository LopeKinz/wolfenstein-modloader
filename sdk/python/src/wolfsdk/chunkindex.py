"""``base/chunkN.index`` (SPEC §3).

The index is never re-serialised: the original buffer is kept and only the
12-byte offset/usize/csize triple of an entry is overwritten.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from .errors import FormatError

MAGIC = b"\x03SER"
ENTRIES_START = 0x2C
MAX_STRING = 1024


@dataclass
class Entry:
    index: int
    type: str
    name: str
    path: str
    offset: int
    usize: int
    csize: int
    start: int
    triple_offset: int
    trailer: bytes = field(repr=False, default=b"")

    @property
    def key(self) -> str:
        return f"{self.type}:{self.name}"

    @property
    def compressed(self) -> bool:
        return self.csize != self.usize

    @property
    def stream_offset(self) -> Optional[int]:
        """Offset into the streamed audio container (``sample`` entries)."""
        if len(self.trailer) < 24:
            return None
        return struct.unpack_from(">I", self.trailer, 20)[0]


def _string_at(buf: bytes, pos: int) -> Optional[Tuple[str, int]]:
    if pos + 4 > len(buf):
        return None
    (n,) = struct.unpack_from("<I", buf, pos)
    if n < 1 or n > MAX_STRING or pos + 4 + n > len(buf):
        return None
    raw = buf[pos + 4 : pos + 4 + n]
    if any(b < 0x20 or b > 0x7E for b in raw):
        return None
    return raw.decode("ascii"), pos + 4 + n


def _plausible(buf: bytes, pos: int, resources_size: Optional[int]):
    strings = []
    p = pos
    for _ in range(3):
        r = _string_at(buf, p)
        if r is None:
            return None
        strings.append(r[0])
        p = r[1]
    if p + 12 > len(buf):
        return None
    offset, usize, csize = struct.unpack_from(">III", buf, p)
    if not (0 < csize <= usize):
        return None
    if resources_size is not None:
        if offset + csize > resources_size:
            return None
    elif offset < 16:
        return None
    return strings, p, offset, usize, csize


class ChunkIndex:
    """A parsed chunk index backed by its original bytes."""

    def __init__(self, data: bytes, resources_size: Optional[int] = None) -> None:
        if data[:4] != MAGIC:
            raise FormatError("chunk index: bad magic")
        if len(data) < ENTRIES_START:
            raise FormatError("chunk index: truncated header")
        self.buffer = bytearray(data)
        self.resources_size = resources_size
        self.counter_a, self.counter_b = struct.unpack_from(">II", data, 0x20)
        self.entries: List[Entry] = []
        self._scan()

    @classmethod
    def parse(cls, data: bytes, resources_size: Optional[int] = None) -> "ChunkIndex":
        return cls(data, resources_size)

    def _scan(self) -> None:
        buf = bytes(self.buffer)
        pos, found = ENTRIES_START, []
        while pos < len(buf):
            r = _plausible(buf, pos, self.resources_size)
            if r is None:
                pos += 1
                continue
            (typ, name, path), tri, offset, usize, csize = r
            found.append([len(found), typ, name, path, offset, usize, csize, pos, tri])
            pos = tri + 12
        for i, f in enumerate(found):
            end = found[i + 1][7] if i + 1 < len(found) else len(buf)
            trailer = buf[f[8] + 12 : end]
            self.entries.append(Entry(*f, trailer=trailer))

    def __iter__(self) -> Iterator[Entry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def find(self, type: str, name: str) -> List[Entry]:
        return [e for e in self.entries if e.type == type and e.name == name]

    def by_key(self) -> Dict[str, List[Entry]]:
        out: Dict[str, List[Entry]] = {}
        for e in self.entries:
            out.setdefault(e.key, []).append(e)
        return out

    def set_triple(self, entry: Entry, offset: int, usize: int, csize: int) -> None:
        struct.pack_into(">III", self.buffer, entry.triple_offset, offset, usize, csize)
        entry.offset, entry.usize, entry.csize = offset, usize, csize

    def to_bytes(self) -> bytes:
        return bytes(self.buffer)


def build_chunk_index(entries, counter_a: int = 0, counter_b: Optional[int] = None) -> bytes:
    """Build a chunk index from ``(type, name, path, offset, usize, csize, trailer)``.

    Used for tests and synthetic archives; the game's own files are never
    re-serialised.
    """
    body = bytearray()
    for typ, name, path, offset, usize, csize, trailer in entries:
        for s in (typ, name, path):
            raw = s.encode("ascii")
            body += struct.pack("<I", len(raw)) + raw
        body += struct.pack(">III", offset, usize, csize) + trailer
    if counter_b is None:
        counter_b = len(entries)
    total = ENTRIES_START + len(body)
    head = MAGIC + struct.pack(">I", total - 32) + b"\x00" * 24
    head += struct.pack(">III", counter_a, counter_b, 0)
    return bytes(head + body)
