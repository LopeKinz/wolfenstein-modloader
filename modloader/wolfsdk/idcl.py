"""Reader for id Tech 6 IDCL .resources archives (Wolfenstein II: TNC).

Unlike the id Tech 5 pair in resources.py, an id Tech 6 archive is
self-contained: header, tables and payloads all live in one file. Everything
below was measured against all 24 retail .resources files; see
tools/probe_idcl.py for the derivation and tools/verify_idcl.py for the proof.

Header (little endian throughout, unlike the BE size fields of id Tech 5):

  0x00 char[4]  'IDCL'
  0x04 u32      version, 12 on retail
  0x08 u64      header hash over [0x10:0x78], solved -- see below
  0x10 u32      0          | constant across all 24 files, purpose unknown
  0x14 u32      1          |
  0x18 u32      0xFFFFFFFF |
  0x1C u32      255        |
  0x20 u64      metadata_hash: FarmHash64 from addr_entries through the
                trailing metadata 'IDCL' marker (marker included)
  0x28 u32      num_entries
  0x2C u32      num_dependencies
  0x30 u32      num_dep_indexes
  0x34 u32      num_string_indexes
  0x38 u32      unknown_0x38, 0 in every retail file
  0x3C u32      unknown_0x3C, 0 in every retail file
  0x40 u32      strings_size
  0x44 u32      unknown_0x44, 0 in every retail file
  0x48 u64      addr_strings
  0x50 u64      addr_0x50, always equal to addr_dependencies
  0x58 u64      addr_entries, always 120 (= sizeof header)
  0x60 u64      addr_dependencies
  0x68 u64      addr_dep_indexes
  0x70 u64      addr_data

The regions are contiguous and their sizes are implied by the counts, which is
what makes the layout falsifiable rather than merely plausible:

  addr_entries      + 144 * num_entries        == addr_strings
  addr_strings      + strings_size             == addr_dependencies
  addr_dependencies + 32 * num_dependencies    == addr_dep_indexes
  addr_dep_indexes  + 4 * num_dep_indexes
                    + 8 * num_string_indexes   == metadata_magic
  metadata_magic    + 4 ('IDCL')               <= addr_data  (padded to 64 KiB)

String table at addr_strings: u64 count, count * u64 offset, then that many
NUL-terminated UTF-8 blobs. The offsets are relative to the end of the offset
array, not to addr_strings.

Names are reached through two hops, not one. path_string_indexes (u64 each,
right behind the dep indexes) is a flat pool of string-table indexes; an entry
owns a run of that pool and indexes *within* its run. The indirection buys
deduplication: many entries share one type string. Runs partition the pool
exactly -- sum of every entry's num_strings equals num_string_indexes in all 24
files -- and the same holds for num_deps over the dep-index pool, which is how
we know the runs are neither shared nor overlapping.

A run is 2 long (type, name) for every entry but one: gameresources' entry
'image env/default_hdr_prefiltered.bimage' carries a third string. That single
entry is why num_string_indexes is 2 * num_entries in 23 files and one more in
the 24th, and it is why num_strings is read per entry instead of assumed.

Entry, 144 bytes:

  0x00 u64  type_string    | relative to string_index, into path_string_indexes
  0x08 u64  name_string    |
  0x10 i64  desc_string    | -1 when absent, which is every retail entry
  0x18 u64  dep_index      start of this entry's run in the dep-index pool
  0x20 u64  string_index   start of this entry's run in path_string_indexes
  0x28 u64  unknown_0x28   0 in every retail entry
  0x30 u64  unknown_0x30   0 in every retail entry
  0x38 u64  offset         absolute file offset of the payload
  0x40 u64  csize          bytes on disk
  0x48 u64  usize          bytes after decompression
  0x50 u64  data_hash
  0x58 u64  timestamp_us   Unix microseconds. Named, not guessed: across the
                           whole game the values span 2017-06-02..2017-10-30,
                           i.e. exactly the build window of a title released
                           2017-10-27, and nothing else about the number moves.
  0x60 u64  hash_0x60      equals data_hash for ~80% of entries, differs for
                           the rest and is occasionally 1. Not understood, so
                           it keeps its address name.
  0x68 u32  version        per-resource-type format revision, not the archive
                           version: 172 of the 174 types in the game use a
                           single value, and the two that do not ('image' with
                           10..14, 'renderProgResource' with 45/46) are exactly
                           the types you would expect to have been revised
  0x6C u32  flags
  0x70 u8   compression    0 = stored, 2 = Oodle Kraken (payload starts 0x8C),
                           4 = Kraken behind a 12-byte prefix (see below)
  0x71 u8   unknown_0x71   0 in every retail entry
  0x72 u16  unknown_0x72   0 / 65 / 202, correlates with nothing we checked
  0x74 u32  unknown_0x74   0 in every retail entry
  0x78 u64  unknown_0x78   0 in every retail entry
  0x80 u32  num_strings    length of this entry's run in path_string_indexes
  0x84 u32  num_deps       length of this entry's run in the dep-index pool
  0x88 u64  unknown_0x88   0 in every retail entry

Dependency, 32 bytes: u64 type_string, u64 name_string (both direct
string-table indexes, no pool hop), u32 dep_type, u32 dep_subtype, u64 hash.

Payloads are NOT readable with the standard library. compression == 2 is Oodle
Kraken, which has no stdlib or pure-python decoder; this module therefore
parses the table and hands back raw bytes only for stored entries.

compression == 4 is rare (25 entries in the whole game) and stays undecoded.
Every one of them begins with the same 12 bytes, 01000000 00000001 00008000,
followed by a normal 0x8C Kraken frame; read as LE u32 that is (1, 16 MiB,
8 MiB), which reads like a block header. All 25 have usize above 16 MiB -- but
10 other entries above 16 MiB use plain mode 2, so size alone does not pick the
mode and we do not claim to know what does.

Writing
-------
Everything below was measured over all 24 retail archives (178077 entries,
zero exceptions) before a single byte was written; tools/verify_idcl_write.py
re-checks it on every file this module produces.

  * The entry table is already in ascending offset order -- table index order
    *is* offset order. id Tech 5 only required the offsets to ascend (appending
    a payload aborts that engine with "FileTable is out of order"); IDCL keeps
    the stronger property, so a writer that preserves table order preserves
    the ordering invariant for free.
  * Payloads are packed end to end with nothing but alignment padding between
    them: for every entry, offset == align_up(previous_end, k) where k is the
    largest of 4096, 256, 64 that divides that offset. Which entry gets which
    k is not derivable from type, flags or compression, so a rebuild reuses the
    alignment the entry already had. That is enough to reproduce the original
    offsets exactly, because k divides the offset and the offset is the first
    multiple of k at or after the previous end.
  * All 197 MB of padding between payloads is 0x00, and the last payload ends
    exactly on the file size -- there is no trailer.

In-place therefore keeps the offset, always. A shorter replacement is
zero-padded out to csize: OodleLZ_Decompress stops at the end of its frame
and ignores whatever follows (105 of 105 padded round trips decoded
bit-exact, with zero and with junk padding), so the padding is never read.

csize is the one table field that does move, and only upwards. The padding
behind a payload is 188 MiB of slack the archives already own -- median 200
bytes per entry, and Kraken leaves so little inside csize itself that 0 of 12
`damage` and 0 of 25 `entityDef` test edits fit there while 12 and 23 fit in
csize + gap. Spending it costs nothing that moves: the gap ends on the next
offset, which is already k-aligned, so align_up(previous end, k) lands on
that same offset for every length in between and no later entry shifts by a
byte. Growth only, for the mirror image of the same rule -- a *shorter* csize
pulls align_up below the next offset and would make the loader mis-address
later entries, which is exactly what resources.py ran into on id Tech 5 ("No
address, error: 487"). A replacement that outgrows csize + gap, or shrinks,
falls back to rebuild(). Three things are measured per write rather than
taken from the survey, because a measurement over 24 archives is not a
guarantee about the 25th state of one of them: that the gap bytes really are
0x00, that no second entry starts at the same offset, and that the new length
fits. See plan_in_slot() and gap_of().

Two fields we cannot maintain, stated plainly rather than hidden:
data_hash is content-derived -- 14 of 14 groups of byte-identical payloads in
chunk_8 share one hash, none differ -- but it matches none of crc32, adler32,
crc64 (ISO and ECMA), FNV-1a, MurmurHash64A, MurmurHash3_x64_128, xxHash64,
MD5, SHA-1, FarmHash Fingerprint64 or CityHash64 over either the stored or the
decompressed bytes.  CityHash/FarmHash over the type/name metadata and both
halves of short-input CityHash128 also miss.  The retail executable does
contain CityHash constants k1 and k0 at file offsets 0x25C8330/0x25C8338, so
that family is present but is not used in any of those direct forms.

That sweep is superseded.  As of 2026-09-23 both header hashes and the entry
data hash are solved, and a writer must RECOMPUTE them rather than preserve
them:

  entry +0x50  MurmurHash64B(decompressed payload, seed 0xDEADBEEF), length
               taken from usize at +0x48.  4499/4499 entries over five
               archives.  The sweep above missed it because it tested
               MurmurHash64A and only seed 0.  It is read on payload access,
               not at mount, and only when the cvar
               resourceStorage_checkDataCheckSum is set (default 0); on
               mismatch the engine raises a fatal error rather than a warning.
  entry +0x60  equal to +0x50 for every type except image/extkisclule/cfile/
               compfile.  No reader recomputes it, so a builder may choose it
               freely -- but the same value has to go into dep+0x18 of every
               dependency record that references the entry, or the stale check
               fires.
  header +0x20 FarmHash64 over the metadata from +0x78 through the trailing
               IDCL marker.
  header +0x08 FarmHash64(header[0x10:0x78]) run through a three-round kMul
               finalizer mixed with the version; 28/28 archives.  Build order
               matters: finalize the entry table, then +0x20, then +0x08 over
               the finished header.  Versions 11 and 12 are both accepted;
               v11 carries no header hash at all.

tools/probe_idclhash.py and tools/probe_datahash.py derive and re-verify these
against the retail corpus, each with its own negative controls;
tools/probe_tnc_hash.py reproduces the older focused probe.  The attribute
names unknown_0x08/unknown_0x20 below are kept so existing callers keep
working -- they are no longer unknown.
"""

import collections
import os
import struct
from pathlib import Path

MAGIC = b"IDCL"
VERSION = 12
HEADER_SIZE = 120
ENTRY_SIZE = 144
DEPENDENCY_SIZE = 32

COMP_STORED = 0
COMP_KRAKEN = 2
COMP_KRAKEN_BLOCKS = 4  # 12-byte prefix then Kraken; trigger unknown


class IdclError(Exception):
    pass


class Header:
    __slots__ = (
        "version", "unknown_0x08", "unknown_0x20",
        "num_entries", "num_dependencies", "num_dep_indexes", "num_string_indexes",
        "unknown_0x38", "unknown_0x3C", "strings_size", "unknown_0x44",
        "addr_strings", "addr_0x50", "addr_entries",
        "addr_dependencies", "addr_dep_indexes", "addr_data",
    )

    def __init__(self, buf):
        if buf[:4] != MAGIC:
            raise IdclError("bad magic %r" % buf[:4])
        (self.version,) = struct.unpack_from("<I", buf, 0x04)
        (self.unknown_0x08,) = struct.unpack_from("<Q", buf, 0x08)
        (self.unknown_0x20,) = struct.unpack_from("<Q", buf, 0x20)
        (self.num_entries, self.num_dependencies, self.num_dep_indexes,
         self.num_string_indexes, self.unknown_0x38,
         self.unknown_0x3C) = struct.unpack_from("<6I", buf, 0x28)
        (self.strings_size, self.unknown_0x44) = struct.unpack_from("<2I", buf, 0x40)
        (self.addr_strings, self.addr_0x50, self.addr_entries, self.addr_dependencies,
         self.addr_dep_indexes, self.addr_data) = struct.unpack_from("<6Q", buf, 0x48)

    @property
    def addr_string_indexes(self):
        """Not a header field: the pool sits right behind the dep indexes.

        Derived rather than read because no header slot holds it, and guessing
        which of the unknown u64s might is how a parser drifts off the rails.
        """
        return self.addr_dep_indexes + 4 * self.num_dep_indexes


class Entry:
    __slots__ = (
        "index", "type", "name", "desc", "dep_index", "num_deps",
        "string_index", "num_strings",
        "offset", "csize", "usize", "data_hash", "timestamp_us", "hash_0x60",
        "version", "flags", "compression", "unknown_0x72",
    )

    @property
    def stored_raw(self):
        return self.compression == COMP_STORED

    def __repr__(self):
        return "<Entry %s:%s %dB @%d>" % (self.type, self.name, self.usize, self.offset)


class Dependency:
    __slots__ = ("type", "name", "dep_type", "dep_subtype", "hash")

    def __repr__(self):
        return "<Dependency %s:%s>" % (self.type, self.name)


class Archive:
    """One .resources file, parsed table-only.

    The table region is read once in full (7 MB even for gameresources) and
    kept; payloads are read on demand so a 700 MB archive stays cheap to open.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.file_size = self.path.stat().st_size
        self._deps_pool = None
        self._slots = None
        self._fh = open(self.path, "rb")
        try:
            self.header = Header(self._fh.read(HEADER_SIZE))
            if self.header.version != VERSION:
                raise IdclError("%s: unsupported version %d" % (self.path.name, self.header.version))
            self.strings = self._read_strings()
            self.entries = self._read_entries()
            self.dependencies = self._read_dependencies()
            metadata_end = (self.header.addr_dep_indexes
                            + 4 * self.header.num_dep_indexes
                            + 8 * self.header.num_string_indexes)
            marker = self._pread(metadata_end, 4)
            if marker != MAGIC:
                raise IdclError("%s: end of metadata magic is %r, expected %r" %
                                (self.path.name, marker, MAGIC))
        except Exception:
            self._fh.close()
            raise
        self._by_name = {}
        for e in self.entries:
            self._by_name.setdefault((e.type, e.name), []).append(e)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def _pread(self, offset, size):
        self._fh.seek(offset)
        buf = self._fh.read(size)
        if len(buf) != size:
            raise IdclError("%s: short read at %d (%d/%d)" % (self.path.name, offset, len(buf), size))
        return buf

    def _read_strings(self):
        h = self.header
        blob = self._pread(h.addr_strings, h.strings_size)
        (count,) = struct.unpack_from("<Q", blob, 0)
        offsets = struct.unpack_from("<%dQ" % count, blob, 8)
        base = 8 + 8 * count
        out = []
        for off in offsets:
            start = base + off
            end = blob.index(b"\0", start)
            out.append(blob[start:end].decode("utf-8", "replace"))
        return out

    def _read_entries(self):
        h = self.header
        self._table = self._pread(h.addr_entries, ENTRY_SIZE * h.num_entries)
        pool = struct.unpack_from(
            "<%dQ" % h.num_string_indexes,
            self._pread(h.addr_string_indexes, 8 * h.num_string_indexes), 0)
        self.string_indexes = pool
        strings = self.strings
        out = []
        for i in range(h.num_entries):
            pos = i * ENTRY_SIZE
            e = Entry()
            e.index = i
            t, n, d, e.dep_index, e.string_index = struct.unpack_from("<3q2Q", self._table, pos)
            # The relative hop is the whole point of the pool; resolving it here
            # means callers never see raw indexes and can't misread desc == -1.
            e.type = strings[pool[e.string_index + t]]
            e.name = strings[pool[e.string_index + n]]
            e.desc = strings[pool[e.string_index + d]] if d >= 0 else None
            (e.offset, e.csize, e.usize, e.data_hash, e.timestamp_us,
             e.hash_0x60) = struct.unpack_from("<6Q", self._table, pos + 0x38)
            e.version, e.flags = struct.unpack_from("<2I", self._table, pos + 0x68)
            e.compression = self._table[pos + 0x70]
            (e.unknown_0x72,) = struct.unpack_from("<H", self._table, pos + 0x72)
            e.num_strings, e.num_deps = struct.unpack_from("<2I", self._table, pos + 0x80)
            out.append(e)
        return out

    def _read_dependencies(self):
        h = self.header
        blob = self._pread(h.addr_dependencies, DEPENDENCY_SIZE * h.num_dependencies)
        out = []
        for i in range(h.num_dependencies):
            t, n = struct.unpack_from("<2Q", blob, i * DEPENDENCY_SIZE)
            dt, ds = struct.unpack_from("<2I", blob, i * DEPENDENCY_SIZE + 0x10)
            (dh,) = struct.unpack_from("<Q", blob, i * DEPENDENCY_SIZE + 0x18)
            d = Dependency()
            d.type, d.name = self.strings[t], self.strings[n]
            d.dep_type, d.dep_subtype, d.hash = dt, ds, dh
            out.append(d)
        return out

    def entry_bytes(self, entry):
        """The entry's raw 144 bytes, for auditing fields this module skips."""
        return self._table[entry.index * ENTRY_SIZE:(entry.index + 1) * ENTRY_SIZE]

    def dep_indexes(self):
        return self._dep_indexes()

    def _dep_indexes(self):
        if self._deps_pool is None:
            h = self.header
            self._deps_pool = struct.unpack_from(
                "<%dI" % h.num_dep_indexes,
                self._pread(h.addr_dep_indexes, 4 * h.num_dep_indexes), 0)
        return self._deps_pool

    def entry_dependencies(self, entry):
        """The Dependency objects this entry pulls in.

        The run bounds come from the entry itself rather than from the next
        entry's start, because entries with no dependencies all park on the
        same cursor and would otherwise look like they share a run.
        """
        pool = self._dep_indexes()
        return [self.dependencies[i]
                for i in pool[entry.dep_index:entry.dep_index + entry.num_deps]]

    def find(self, type_, name):
        """Every entry with this (type, name), in table order. Possibly empty.

        A list, not a single entry: 860 (type, name) pairs ship 5 or 6 copies
        inside one archive -- all of them renderProgResource, and the copies
        differ in unknown_0x72, so they are distinct resources the engine
        tells apart by a field this parser does not interpret. Returning only
        the first left 3869 entries unreachable, and a writer built on that
        would patch one copy and leave the others at their old content.
        """
        return self._by_name.get((type_, name), [])

    def read_raw(self, entry):
        """The payload exactly as stored, compressed or not."""
        return self._pread(entry.offset, entry.csize)

    def read(self, entry):
        if not entry.stored_raw:
            raise IdclError(
                "%s: compression %d (Oodle Kraken) has no stdlib decoder"
                % (entry.name, entry.compression))
        return self.read_raw(entry)

    def __len__(self):
        return len(self.entries)

    # -- writing ----------------------------------------------------------

    def _own(self, entry):
        """Reject an entry that came out of a different archive.

        Without this a payload keyed by a foreign entry would be written to
        that entry's offset in *this* file, which is a silent corruption
        rather than an error.
        """
        if not (0 <= entry.index < len(self.entries)) or self.entries[entry.index] is not entry:
            raise IdclError("%s: entry %r does not belong to this archive"
                            % (self.path.name, entry))

    def plan_in_place(self, entry, data, oo=None):
        """The exact `entry.csize` bytes to drop into its slot, or None.

        None means "does not fit, rebuild instead". Three ways to not fit, and
        all three are invariants every retail entry satisfies rather than
        preferences:

          * the packed bytes are longer than csize -- they would run into the
            next payload;
          * csize == usize would then claim "stored" for a compressed entry,
            and the mode byte says otherwise;
          * csize > usize never occurs in retail, and it would here whenever a
            replacement decompresses to less than the old compressed size.
        """
        raw, usize, comp = pack_payload(entry, data, oo)
        if comp == COMP_STORED:
            if len(raw) != entry.csize:
                return None
        elif len(raw) > entry.csize or usize <= entry.csize:
            return None
        return raw + b"\0" * (entry.csize - len(raw)), usize, comp

    def _slot_ends(self):
        """entry.index -> the first byte behind this payload that is not ours.

        The next payload's offset, or the file size behind the last one --
        but only for an entry that starts where no other entry does. 115 of
        the 178077 retail entries share an offset with a neighbour; all but
        four of them are 'cm' collision models with csize 0, which own no
        bytes at all and whose offset is somebody else's payload. For a
        shared offset the slot therefore ends where csize ends. Deciding
        which of them owns the space behind would be a guess, and the way a
        guess here goes wrong is that a write eats a payload still in use.
        """
        if self._slots is None:
            starts = sorted({e.offset for e in self.entries})
            after = dict(zip(starts, starts[1:] + [self.file_size]))
            alone = collections.Counter(e.offset for e in self.entries)
            self._slots = {
                e.index: after[e.offset] if alone[e.offset] == 1 else e.offset + e.csize
                for e in self.entries}
        return self._slots

    def gap_of(self, entry):
        """Free bytes behind this payload -- read off the file, not assumed.

        The 188 MiB of padding in the 24 retail archives is 0x00 throughout,
        which is what makes it spendable. That is a measurement of the files
        as they shipped, though, not a promise about the one on disk now, so
        every byte is re-read here and a single non-zero one puts the answer
        back to 0: it is either a payload the entry table does not describe
        the way we read it, or an edit somebody else made, and under both
        readings writing there destroys something. 0 means the caller keeps
        to csize and, if that does not fit, rebuilds.
        """
        self._own(entry)
        end = entry.offset + entry.csize
        gap = self._slot_ends()[entry.index] - end
        if gap <= 0:
            return 0
        return 0 if any(self._pread(end, gap)) else gap

    def plan_in_slot(self, entry, data, oo=None):
        """The whole slot: (window, csize, usize, compression), or None.

        Two stages, in this order, and the first one is plan_in_place()
        untouched: what already fits inside csize is written exactly as it
        was before this method existed, csize included, so the gap stays shut
        whenever it is not needed. Only a replacement that outgrows csize
        spends the gap -- and then csize moves with it, because a csize left
        at the old length turns the new payload into a truncated frame.

        Growth only. The gap ends on the next payload's offset, which is
        already aligned to the k that offset was laid out with, so for any
        new length between csize and csize + gap align_up(previous end, k)
        lands on that same offset and nothing after it shifts. A *shorter*
        csize is the mirror image and is refused: align_up would fall below
        the next offset and the layout rule that reproduces the whole file
        would no longer hold there.

        The window spans csize + gap rather than the new payload alone, so
        what the replacement does not cover goes back to being the zero fill
        retail has there instead of staying a tail of the old payload.
        """
        got = self.plan_in_place(entry, data, oo)
        if got is not None:
            raw, usize, comp = got
            return raw, entry.csize, usize, comp
        budget = entry.csize + self.gap_of(entry)
        raw, usize, comp = pack_payload(entry, data, oo)
        if not entry.csize < len(raw) <= budget:
            return None
        if comp != COMP_STORED and usize <= len(raw):
            return None   # csize > usize occurs nowhere in retail
        return raw + b"\0" * (budget - len(raw)), len(raw), usize, comp

    def write_in_place(self, changes, oo=None):
        """Overwrite whole slots in this archive's own file. All or nothing.

        `changes` maps Entry -> decompressed replacement bytes. Returns the
        number of entries written, or None when any one of them does not fit;
        in that case nothing was written at all and the caller should call
        rebuild(). Offsets, the file size and every byte outside the touched
        slots stay exactly as they were -- csize, usize and the compression
        byte of the changed entries move in the table, and csize only when
        the replacement outgrew it (see plan_in_slot).

        All of the planning happens before the file is opened for writing,
        which is what "all or nothing" rests on: the slot a plan measures is
        measured against the file as it stands, not against a file that an
        earlier plan in the same batch has already half-rewritten.
        """
        plans = []
        for entry, data in changes.items():
            self._own(entry)
            got = self.plan_in_slot(entry, data, oo)
            if got is None:
                return None
            plans.append((entry, got))

        table = bytearray(self._table)
        base = self.header.addr_entries
        self.close()
        try:
            with open(self.path, "r+b") as fh:
                for entry, (raw, csize, usize, comp) in plans:
                    fh.seek(entry.offset)
                    fh.write(raw)
                    pos = entry.index * ENTRY_SIZE
                    struct.pack_into("<2Q", table, pos + 0x40, csize, usize)
                    table[pos + 0x70] = comp
                    fh.seek(base + pos + 0x40)
                    fh.write(struct.pack("<2Q", csize, usize))
                    fh.seek(base + pos + 0x70)
                    fh.write(bytes((comp,)))
                    entry.csize, entry.usize, entry.compression = csize, usize, comp
            self._table = bytes(table)
        finally:
            self._fh = open(self.path, "rb")
        return len(plans)

    def rebuild(self, dest, changes=(), oo=None):
        """Write the whole archive to `dest`, replacing the payloads in `changes`.

        Entries keep their table order -- which is their offset order -- and
        each payload lands on the alignment its original offset had, so an
        empty `changes` reproduces the source file byte for byte. Header,
        string table, dependencies and both index pools are copied verbatim,
        which is also why every field this module does not parse survives.

        `self` still describes the *source* afterwards; returns the size of
        the file written.
        """
        changes = dict(changes)
        for entry in changes:
            self._own(entry)
        head = bytearray(self._pread(0, self.header.addr_data))
        base = self.header.addr_entries
        cur = self.header.addr_data
        with open(dest, "wb") as out:
            out.write(head)
            for e in self.entries:
                if e in changes:
                    raw, usize, comp = pack_payload(e, changes[e], oo)
                else:
                    raw, usize, comp = self.read_raw(e), e.usize, e.compression
                pos = _align_up(cur, alignment_of(e.offset))
                out.write(b"\0" * (pos - cur))
                out.write(raw)
                cur = pos + len(raw)
                struct.pack_into("<3Q", head, base + e.index * ENTRY_SIZE + 0x38,
                                 pos, len(raw), usize)
                head[base + e.index * ENTRY_SIZE + 0x70] = comp
            out.seek(0)
            out.write(head)
        return cur

    def apply(self, changes, oo=None):
        """Write `changes` into this archive's own file, in place if they fit.

        Returns "in-place" or "rebuild". The rebuild goes through a temporary
        file beside the archive and is swapped in with os.replace, so an
        interrupted write can never leave a half-written archive behind. The
        Archive re-reads itself afterwards, because a rebuild moves offsets.
        """
        if self.write_in_place(changes, oo) is not None:
            return "in-place"
        tmp = self.path.with_name(self.path.name + ".idcl_tmp")
        self.rebuild(tmp, changes, oo)
        self.close()
        os.replace(tmp, self.path)
        self.__init__(self.path)
        return "rebuild"


# Payload alignments the game uses, largest first. Every entry in all 24 retail
# archives sits at align_up(previous_end, k) for the largest k here that divides
# its offset -- 178077 entries, no exception, no other k needed.
ALIGNMENTS = (4096, 256, 64)


def alignment_of(offset):
    """The alignment an existing payload offset was laid out with."""
    for k in ALIGNMENTS:
        if offset % k == 0:
            return k
    return 1


def _align_up(value, alignment):
    return -(-value // alignment) * alignment


def pack_payload(entry, data, oo=None):
    """`data` in the form `entry` stores it: (raw, usize, compression).

    The entry's own mode decides, never the file extension. Mode 4 is written
    back as plain mode 2: its 12-byte prefix is byte-identical in all 25
    entries that use it and decodes to nothing, so we can strip it but have no
    idea what would make the engine expect it, and writing a header we do not
    understand is worse than writing one we do.

    Compression that does not pay for itself falls back to stored, which is
    what the shipped data does too. Without it a small or incompressible
    payload would come out of Kraken *longer* than the input, and csize > usize
    occurs nowhere in retail.
    """
    data = bytes(data)
    if entry.compression != COMP_STORED:
        if oo is None:
            raise IdclError("%s: compression %d needs an Oodle handle"
                            % (entry.name, entry.compression))
        raw = oo.compress(data)
        if len(raw) < len(data):
            return raw, len(data), COMP_KRAKEN
    return data, len(data), COMP_STORED
