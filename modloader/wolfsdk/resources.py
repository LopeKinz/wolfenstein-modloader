"""Reader/writer for id Tech 5 .index/.resources archives (Wolfenstein: TNO).

Format, reverse-engineered from the retail data files:

  master.index : magic 0x03 'S' 'E' 'R', BE u32 pair-count, then 2*count
                 LE-length-prefixed ASCII names (index, resources, ...).

  chunkN.index : same magic, 0x2C-byte header, then a stream of entries:
                   LE u32 len + type   (e.g. "weapon", "image")
                   LE u32 len + name   (e.g. "weapon/knife_base")
                   LE u32 len + path   (e.g. "generated/decls/weapon/...")
                   BE u32 offset       into the paired .resources
                   BE u32 usize        uncompressed size
                   BE u32 csize        on-disk size
                   variable tail       (mostly zeros; longer for audio types)

  payload      : raw DEFLATE (wbits=-15) when csize != usize, else stored
                 verbatim. There is no checksum anywhere in an entry, which is
                 what makes in-place patching safe and reversible.

The tail length varies by asset type and is not fully understood. We never
need to know it: the index is kept as its original byte buffer and a patch
overwrites only the 12 bytes of offset/usize/csize at a known position. Every
unknown byte is preserved verbatim, so saving an unedited index reproduces
the input file exactly.
"""

import base64
import hashlib
import struct
import zlib
from pathlib import Path

MAGIC = b"\x03SER"
HEADER_SIZE = 0x2C
SIZE_FIELDS = 12  # BE u32 offset, usize, csize

# Every payload in a .resources starts on a 16-byte boundary (verified: 100% of
# 15069 entries in chunk0, with 0-15 bytes of padding between them), and the
# data area begins after a 16-byte file header.
ALIGNMENT = 16
HEADER_DATA_SIZE = 16


class ResourceError(Exception):
    pass


class Entry:
    """One asset. `pos` is where its offset/usize/csize triple lives."""

    __slots__ = ("type", "name", "path", "offset", "usize", "csize", "pos")

    def __init__(self, type_, name, path, offset, usize, csize, pos):
        self.type = type_
        self.name = name
        self.path = path
        self.offset = offset
        self.usize = usize
        self.csize = csize
        self.pos = pos

    @property
    def stored_raw(self):
        return self.csize == self.usize

    def __repr__(self):
        return "<Entry %s:%s %dB @%d>" % (self.type, self.name, self.usize, self.offset)


def _read_str(buf, pos):
    if pos + 4 > len(buf):
        return None
    n = struct.unpack_from("<I", buf, pos)[0]
    if not (0 < n <= 1024) or pos + 4 + n > len(buf):
        return None
    raw = buf[pos + 4 : pos + 4 + n]
    if not all(0x20 <= b < 0x7F for b in raw):
        return None
    return raw.decode("ascii"), pos + 4 + n


class ResourceIndex:
    """A chunkN.index file, held as its original bytes plus a parsed view."""

    def __init__(self, path, buf, entries):
        self.path = Path(path)
        self.buf = bytearray(buf)
        self.entries = entries

    @classmethod
    def load(cls, path, data_size=None):
        path = Path(path)
        buf = path.read_bytes()
        if buf[:4] != MAGIC:
            raise ResourceError("%s: bad magic %r" % (path.name, buf[:4]))
        entries = []
        pos = HEADER_SIZE
        limit = len(buf) - SIZE_FIELDS
        while pos < limit:
            start = pos
            trio = []
            for _ in range(3):
                got = _read_str(buf, pos)
                if got is None:
                    break
                s, pos = got
                trio.append(s)
            if len(trio) == 3 and pos + SIZE_FIELDS <= len(buf):
                offset, usize, csize = struct.unpack_from(">3I", buf, pos)
                # Plausibility gate: a real entry never stores more compressed
                # bytes than uncompressed ones, and never points past the data
                # file. This is what keeps us aligned across the variable tail.
                if (
                    0 < usize < (1 << 31)
                    and 0 < csize <= usize
                    and (data_size is None or offset + csize <= data_size)
                ):
                    entries.append(Entry(trio[0], trio[1], trio[2], offset, usize, csize, pos))
                    pos += SIZE_FIELDS
                    continue
            pos = start + 1
        return cls(path, buf, entries)

    def set_sizes(self, entry, offset, usize, csize):
        """Rewrite one entry's location in place, in both buffer and object."""
        struct.pack_into(">3I", self.buf, entry.pos, offset, usize, csize)
        entry.offset, entry.usize, entry.csize = offset, usize, csize

    def save(self, path=None):
        Path(path or self.path).write_bytes(bytes(self.buf))

    def __len__(self):
        return len(self.entries)


class Archive:
    """A chunkN.index + chunkN.resources pair."""

    def __init__(self, index_path, resources_path):
        self.resources_path = Path(resources_path)
        self.data_size = self.resources_path.stat().st_size
        self.index = ResourceIndex.load(index_path, self.data_size)
        self._fh = None
        self._slots = None
        self._by_name = {}
        for e in self.index.entries:
            self._by_name.setdefault((e.type, e.name), e)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    @property
    def entries(self):
        return self.index.entries

    def find(self, type_, name):
        return self._by_name.get((type_, name))

    def read(self, entry):
        """Return the decompressed bytes for `entry`.

        The read handle is cached: extracting the full archive means ~200k
        reads, and reopening a 500 MB file each time dominates the runtime.
        """
        if self._fh is None:
            self._fh = open(self.resources_path, "rb")
        fh = self._fh
        fh.seek(entry.offset)
        raw = fh.read(entry.csize)
        if len(raw) != entry.csize:
            raise ResourceError("%s: short read (%d/%d)" % (entry.name, len(raw), entry.csize))
        if entry.stored_raw:
            return raw
        data = zlib.decompressobj(-15).decompress(raw, entry.usize)
        if len(data) != entry.usize:
            raise ResourceError("%s: got %dB, expected %dB" % (entry.name, len(data), entry.usize))
        return data

    def slot_size(self, entry):
        """Bytes available to `entry` before the next entry begins.

        Payloads are packed sequentially at 16-byte aligned offsets, so each
        entry owns its compressed bytes plus up to 15 bytes of alignment
        padding. That padding is free space we may use.
        """
        if self._slots is None:
            ordered = sorted(self.entries, key=lambda e: e.offset)
            self._slots = {}
            for i, e in enumerate(ordered):
                end = ordered[i + 1].offset if i + 1 < len(ordered) else self.data_size
                self._slots[id(e)] = max(0, end - e.offset)
        return self._slots.get(id(entry), 0)

    def write_in_place(self, entry, data, plan=None):
        """Overwrite `entry` in its slot, keeping offset *and* csize identical.

        Two layout properties matter to the engine, and both were learned the
        hard way from live crashes:

        * Offsets must ascend, or the background loader aborts with
          "FileTable is out of order".
        * Payloads must stay packed end to end. Shrinking csize leaves a gap,
          and the loader then mis-addresses *later* entries -- the symptom is
          an unrelated asset failing with "No address, error: 487".

        Our zlib compresses better than the game's packer, so even rewriting
        identical content yields a shorter stream. We therefore pad back out
        to the original csize and leave the size field untouched: a deflate
        stream ends at its final block, so the padding is never read.

        Returns False when the new payload genuinely cannot fit. `plan` is the
        result a caller already got from plan_in_place(); deflate_exact is the
        most expensive function here and running it twice per asset doubles the
        cost of a full apply.
        """
        got = plan or plan_in_place(entry, data)
        if got is None:
            return False
        packed, usize = got
        self.close()
        with open(self.resources_path, "r+b") as fh:
            fh.seek(entry.offset)
            fh.write(packed)
        # offset and csize deliberately unchanged -- only usize moves.
        self.index.set_sizes(entry, entry.offset, usize, entry.csize)
        return True

    def read_slot(self, entry):
        """The raw bytes currently occupying this entry's slot."""
        size = self.slot_size(entry)
        if self._fh is None:
            self._fh = open(self.resources_path, "rb")
        self._fh.seek(entry.offset)
        return self._fh.read(size)

    def restore_slot(self, offset, raw):
        self.close()
        with open(self.resources_path, "r+b") as fh:
            fh.seek(offset)
            fh.write(raw)

    def rebuild(self, payloads):
        """Rewrite the whole .resources in offset order, applying `payloads`.

        Used when a replacement is too large for its slot. Everything is laid
        out again from the start, keeping entries in their original order and
        on 16-byte boundaries, so the ascending-offset invariant still holds.
        """
        ordered = sorted(self.entries, key=lambda e: e.offset)
        self.close()
        tmp = self.resources_path.with_suffix(self.resources_path.suffix + ".wolfsdk_tmp")
        updates = []
        applied = set()
        with open(self.resources_path, "rb") as src, open(tmp, "wb") as dst:
            src.seek(0)
            dst.write(src.read(HEADER_DATA_SIZE))
            for entry in ordered:
                data = payloads.get((entry.type, entry.name))
                if data is None:
                    src.seek(entry.offset)
                    packed = src.read(entry.csize)
                    usize = entry.usize
                else:
                    # Honour the entry's existing storage mode. csize == usize
                    # is the only marker that says "stored verbatim", and the
                    # types that ship that way (.bswf, .bimage, .lang, .dct,
                    # .png) would silently flip to deflate if we compressed
                    # them here.
                    packed = data if entry.stored_raw else _deflate(data)
                    usize = len(data)
                    applied.add((entry.type, entry.name))
                pos = dst.tell()
                pad = (-pos) % ALIGNMENT
                if pad:
                    dst.write(b"\0" * pad)
                    pos += pad
                dst.write(packed)
                updates.append((entry, pos, usize, len(packed)))
        missed = set(payloads) - applied
        if missed:
            # A payload keyed by a name no entry carries would be dropped
            # silently and the journal would still claim success. Checked
            # before the swap, so a bad call leaves the archive untouched --
            # apply() rolls back, but direct callers in tools/ do not.
            tmp.unlink()
            raise ResourceError(
                "%s: %d payload(s) matched no entry: %s"
                % (self.resources_path.name, len(missed),
                   ", ".join("%s:%s" % k for k in sorted(missed))[:200]))
        tmp.replace(self.resources_path)
        for entry, offset, usize, csize in updates:
            self.index.set_sizes(entry, offset, usize, csize)
        self.data_size = self.resources_path.stat().st_size
        self._slots = None
        return self.data_size


def _deflate(data):
    """Compress the way the game's own packer does.

    Crucially this ends with Z_SYNC_FLUSH, not Z_FINISH: 14035 of chunk0's
    15069 payloads are *unterminated* deflate streams ending in the sync
    marker 00 00 ff ff, because the engine knows each entry's uncompressed
    size and simply inflates that many bytes. Feeding it a BFINAL-terminated
    stream instead makes its reader fail with

        WARNING: zlib inflate error

    and every subsequent resource read fails with it -- which manifests far
    away from the patched asset, as unrelated models and decls "not found".
    """
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    packed = co.compress(data) + co.flush(zlib.Z_SYNC_FLUSH)
    # Storing verbatim is legal and is what the game does whenever
    # compression would not pay for itself.
    return data if len(packed) >= len(data) else packed


def _filler(length, seed):
    """Deterministic, incompressible ASCII of exactly `length` bytes.

    Base64 of a hash chain: it cannot be compressed, so adding N characters
    grows the deflate stream by very close to N, which makes the search in
    deflate_exact converge in a couple of iterations.
    """
    out = bytearray()
    digest = hashlib.sha256(seed).digest()
    while len(out) < length:
        digest = hashlib.sha256(digest).digest()
        out += base64.b64encode(digest)
    return bytes(out[:length])


def deflate_exact(data, target):
    """Compress `data` to exactly `target` bytes. Returns (packed, usize) or None.

    Padding the *output* with zero bytes produces a valid stream followed by
    garbage, and the engine will not load an archive written that way. Padding
    the *input* with a decl comment instead keeps the deflate stream complete
    and self-terminating at exactly the right length -- and decls ignore
    `//` comments, so the content is unchanged as far as the game is
    concerned. (The shipped decls contain such comments themselves.)
    """
    packed = _deflate(data)
    if len(packed) > target:
        return None
    if len(packed) == target:
        return packed, len(data)
    if b"\x00" in data[:512]:
        return None  # binary payload: a text comment would corrupt it

    # Compressed length does not move smoothly with padding length, so a
    # guided search overshoots. Decls are a few kB, so simply trying every
    # padding length is fast and always finds the exact hit when one exists.
    # Incompressible filler grows the stream ~1:1; spaces grow it far more
    # slowly, and between them every reachable length is covered.
    seed = data[:64]
    for filler in (_filler(512, seed), b" " * 4096):
        for k in range(len(filler) + 1):
            padded = data + b"\n// " + filler[:k] + b"\n"
            packed = _deflate(padded)
            if len(packed) == target:
                return packed, len(padded)
            if len(packed) > target + 64:
                break  # overshooting badly; try the other filler
    return None


def plan_in_place(entry, data):
    """What to write into `entry`'s slot, as (payload, usize), or None.

    Whether a slot is written raw or deflated is dictated by the entry we are
    replacing, never by the file extension: csize == usize is the only thing
    that marks a payload as stored verbatim, and the shipped data uses it for
    whole types (.bswf, .bimage, .lang, .dct, .png in type "file") while
    .script/.entities arrive deflated. Re-deflating a verbatim entry would
    change its storage mode behind the engine's back, so such an entry only
    accepts a replacement of exactly the same length -- then offset, csize and
    usize all stay as they were and nothing but the bytes moves.
    """
    if entry.stored_raw:
        return (data, len(data)) if len(data) == entry.csize else None
    return deflate_exact(data, entry.csize)


def load_master(base_dir):
    """Parse base/master.index into a list of (index_name, resources_name)."""
    base_dir = Path(base_dir)
    buf = (base_dir / "master.index").read_bytes()
    if buf[:4] != MAGIC:
        raise ResourceError("master.index: bad magic")
    (count,) = struct.unpack_from(">I", buf, 4)
    names, pos = [], 8
    for _ in range(count * 2):
        got = _read_str(buf, pos)
        if got is None:
            raise ResourceError("master.index: malformed entry at %d" % pos)
        s, pos = got
        names.append(s)
    return list(zip(names[0::2], names[1::2]))


def open_all(base_dir):
    """Open every archive listed in master.index."""
    base_dir = Path(base_dir)
    return [Archive(base_dir / i, base_dir / r) for i, r in load_master(base_dir)]
