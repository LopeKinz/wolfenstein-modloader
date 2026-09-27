"""Game directory access: find, read, patch in place, rebuild (SPEC §5)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

from .chunkindex import ChunkIndex, Entry
from .compression import deflate_exact, deflate_sync, inflate, is_sync_flushed
from .errors import FormatError, NoFitError
from .masterindex import parse_master_index

RESOURCES_HEADER = b"\x03SER" + b"\x00" * 12


def looks_textual(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


@dataclass
class Chunk:
    index_name: str
    resources_name: str
    index_path: str
    resources_path: str
    index: ChunkIndex

    @property
    def entries(self) -> List[Entry]:
        return self.index.entries

    def save_index(self) -> None:
        with open(self.index_path, "wb") as f:
            f.write(self.index.to_bytes())


@dataclass
class Occurrence:
    chunk: Chunk
    entry: Entry


class Game:
    """A game installation (the directory that contains ``base/``)."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.base = os.path.join(root, "base")
        master = os.path.join(self.base, "master.index")
        with open(master, "rb") as f:
            self.pairs = parse_master_index(f.read())
        self.chunks: List[Chunk] = []
        for idx_name, res_name in self.pairs:
            ip = os.path.join(self.base, idx_name)
            rp = os.path.join(self.base, res_name)
            if not os.path.exists(ip) or not os.path.exists(rp):
                continue
            with open(ip, "rb") as f:
                data = f.read()
            ci = ChunkIndex(data, os.path.getsize(rp))
            self.chunks.append(Chunk(idx_name, res_name, ip, rp, ci))

    @classmethod
    def open(cls, root: str) -> "Game":
        return cls(root)

    def __iter__(self) -> Iterator[Occurrence]:
        for c in self.chunks:
            for e in c.entries:
                yield Occurrence(c, e)

    def find(self, type: str, name: str) -> List[Occurrence]:
        return [Occurrence(c, e) for c in self.chunks for e in c.index.find(type, name)]

    def keys(self) -> List[str]:
        seen: Dict[str, None] = {}
        for occ in self:
            seen.setdefault(occ.entry.key, None)
        return list(seen)

    # ----------------------------------------------------------- reading
    def read_raw(self, occ: Occurrence) -> bytes:
        e = occ.entry
        with open(occ.chunk.resources_path, "rb") as f:
            f.seek(e.offset)
            data = f.read(e.csize)
        if len(data) != e.csize:
            raise FormatError(f"{e.key}: slot truncated")
        return data

    def read(self, occ: Occurrence) -> bytes:
        raw = self.read_raw(occ)
        e = occ.entry
        return raw if not e.compressed else inflate(raw, e.usize)

    def read_asset(self, type: str, name: str) -> Optional[bytes]:
        occ = self.find(type, name)
        return self.read(occ[0]) if occ else None

    # ----------------------------------------------------------- writing
    def write_in_place(self, occ: Occurrence, data: bytes, pad: Optional[bool] = None) -> bytes:
        """Overwrite the entry's slot. Returns the previous slot bytes."""
        e = occ.entry
        original = self.read_raw(occ)
        if not e.compressed:
            if len(data) != e.csize:
                raise NoFitError(f"{e.key}: stored entry needs exactly {e.csize} bytes, got {len(data)}")
            stream, usize = data, e.usize
        else:
            if pad is None:
                pad = looks_textual(data)
            packed = deflate_exact(data, e.csize, pad)
            if packed is None:
                raise NoFitError(f"{e.key}: does not fit into its {e.csize}-byte slot")
            stream, payload = packed
            usize = len(payload)
        with open(occ.chunk.resources_path, "r+b") as f:
            f.seek(e.offset)
            f.write(stream)
        if usize != e.usize:
            occ.chunk.index.set_triple(e, e.offset, usize, e.csize)
            occ.chunk.save_index()
        return original

    def write_asset(self, type: str, name: str, data: bytes) -> List[Tuple[Occurrence, bytes]]:
        """Write ``data`` into every occurrence. Returns ``(occ, original)``."""
        occs = self.find(type, name)
        if not occs:
            raise KeyError(f"{type}:{name}")
        return [(o, self.write_in_place(o, data)) for o in occs]

    def rebuild(self, chunk: Chunk, replacements: Dict[int, bytes], dest: Optional[str] = None) -> None:
        """Rewrite ``chunk.resources`` in original order (SPEC §5.2)."""
        dest = dest or chunk.resources_path
        tmp = dest + ".tmp"
        slots: Dict[Tuple[int, int], List[Entry]] = {}
        for e in chunk.entries:
            slots.setdefault((e.offset, e.csize), []).append(e)
        with open(chunk.resources_path, "rb") as src, open(tmp, "wb") as out:
            out.write(RESOURCES_HEADER)
            cursor = len(RESOURCES_HEADER)
            updates = []
            for (offset, csize), group in sorted(slots.items()):
                repl = next((replacements[e.index] for e in group if e.index in replacements), None)
                if repl is None:
                    src.seek(offset)
                    payload = src.read(csize)
                    usize = group[0].usize
                else:
                    s = deflate_sync(repl)
                    payload, usize = (s, len(repl)) if len(s) < len(repl) else (repl, len(repl))
                for e in group:
                    updates.append((e, cursor, usize, len(payload)))
                out.write(payload)
                cursor += len(payload)
                pad = (-cursor) % 16
                out.write(b"\x00" * pad)
                cursor += pad
        os.replace(tmp, dest)
        for e, off, usize, csize in updates:
            chunk.index.set_triple(e, off, usize, csize)
        chunk.index.resources_size = os.path.getsize(dest)
        chunk.save_index()


def validate_chunk(chunk: Chunk, check_streams: bool = True) -> List[str]:
    """Return a list of invariant violations (empty = valid)."""
    problems: List[str] = []
    size = os.path.getsize(chunk.resources_path)
    last = -1
    seen = set()
    with open(chunk.resources_path, "rb") as f:
        for e in sorted(chunk.entries, key=lambda x: (x.offset, x.index)):
            if (e.offset, e.csize) in seen:
                continue
            seen.add((e.offset, e.csize))
            if e.offset <= last:
                problems.append(f"{e.key}: offset {e.offset} not ascending")
            last = e.offset
            if e.offset % 16:
                problems.append(f"{e.key}: offset {e.offset} not 16-byte aligned")
            if e.offset + e.csize > size:
                problems.append(f"{e.key}: slot exceeds resources file")
                continue
            if check_streams and e.compressed:
                f.seek(e.offset)
                if not is_sync_flushed(f.read(e.csize)):
                    problems.append(f"{e.key}: stream not sync-flushed")
    return problems
