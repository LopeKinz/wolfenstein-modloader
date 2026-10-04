"""Writer side of the IDCL container: recompute the hashes a reader checks.

`idcl.Archive` can already lay a container out again byte for byte
(`rebuild()`), but it copies the header and the entry table verbatim. That was
correct while the hashes were unsolved and it is wrong now: four fields are
content-derived, and three of them are checked by the engine.

What is recomputed, in the only order that works:

  1. entry +0x50  MurmurHash64B(decompressed payload, seed 0xDEADBEEF), the
                  length taken from usize at +0x48. Checked in
                  idResourceStorage::ReadResourceData when the cvar
                  `resourceStorage_checkDataCheckSum` is set -- a FatalError,
                  not a warning.
  2. entry +0x60  follows +0x50 *only where it already equalled it*, see
                  hash60_rule() below.
     dep   +0x18  the dependency records that carried an entry's old +0x60
                  get the new one.
  3. header +0x20 FarmHash64 over the metadata, +0x78 through the trailing
                  IDCL marker -- which contains the entry and dependency
                  tables, so it can only be computed once those are final.
  4. header +0x08 FarmHash64(header[0x10:0x78]) through the kMul finalizer,
                  mixed with the version. header+0x20 sits *inside* that
                  range, so +0x08 must come last. Writing it before +0x20
                  produces a file the engine rejects with "corrupt header";
                  tools/probe_idclwriter.py --negative shows exactly that.

The hash implementations live in wolfsdk/idclhash.py, which the probes that
derived them from the disassembly import too: one implementation of a
load-bearing hash, not two that can drift apart.

Two things the docstring of idcl.py states more strongly than this project can
measure, corrected here because the writer has to act on them:

  * `+0x60 == +0x50 except for image/extkisclule/cfile/compfile` is not the
    whole list -- `cm` also differs (21 entries over the four archives
    surveyed). hash60_rule() therefore does not look at the type at all: it
    keeps a +0x60 that already differed and moves one that did not. That is
    no-op by construction on an unmodified container, which is what makes the
    round-trip control able to fail.
  * "the same value has to go into dep+0x18 of every dependency record that
    references the entry" holds for the dependency records that carry a
    64-bit hash (all 233 local `skeleton` references in gameresources match),
    but most dep+0x18 values are 32-bit and match nothing: 3508 of 3741 local
    dep_type=2 references in gameresources (material, renderParm, sound) do
    not carry the entry's +0x60 and never did. Blanket-rewriting them by name
    would corrupt the file, so propagation only touches records that actually
    held the old value.
"""

import shutil
import struct
from pathlib import Path

from wolfsdk import idcl
from wolfsdk.idcl import DEPENDENCY_SIZE, ENTRY_SIZE, HEADER_SIZE, MAGIC
from wolfsdk.idclhash import DATA_SEED, farmhash64, header_hash, metadata_size, murmur64b


def decompressed(archive, entry, oo=None):
    """The bytes the engine hashes, or None when this build cannot produce them.

    None means compression mode 4, whose 12-byte prefix has no decoder here.
    A caller must leave such an entry's +0x50 alone rather than guess it.
    """
    raw = archive.read_raw(entry)
    if entry.compression == idcl.COMP_STORED:
        return raw
    if entry.compression == idcl.COMP_KRAKEN:
        if oo is None:
            raise idcl.IdclError("%s: compression 2 needs an Oodle handle" % entry.name)
        return oo.decompress(raw, entry.usize)
    return None


def hash60_rule(old_50, old_60, new_50):
    """What +0x60 becomes when the payload hash moves from old_50 to new_50.

    +0x60 is not read by any code path we traced, so it is free -- but it is
    mirrored into dep+0x18 and a builder that scrambles it makes those records
    stale. Where it equalled +0x50 it is a copy of the data hash and follows
    it. Where it did not, it is something we cannot derive (image, cm, cfile,
    compfile, extkisclule), so it is kept: preserving an unknown beats
    inventing one.
    """
    return new_50 if old_60 == old_50 else old_60


def finalize(path, oo=None, data=True):
    """Recompute the four hashes of the container at `path`, in place.

    `data=False` skips step 1 and 2 and only redoes the two header hashes,
    which is what a caller that changed nothing but metadata wants. Only the
    first `addr_data` bytes are ever written; payloads are read, never moved.

    Returns a dict of counts -- entries, skipped (mode 4), data_hash and
    hash_0x60 fields changed, dependency records propagated to -- plus
    "changed", the (index, type, name, old_0x50, new_0x50) of every entry whose
    data hash moved. That list is the before-picture of a repair and it falls
    out of the pass that fixes it, so naming the broken entries costs nothing
    on top.
    """
    path = Path(path)
    stats = {"entries": 0, "skipped": 0, "data_hash": 0, "hash_0x60": 0,
             "deps": 0, "changed": []}

    with idcl.Archive(path) as a:
        h = a.header
        if h.addr_entries != HEADER_SIZE:
            raise idcl.IdclError("%s: entry table at %d, expected %d -- the "
                                 "metadata hash would cover the wrong bytes"
                                 % (path.name, h.addr_entries, HEADER_SIZE))
        stats["entries"] = len(a.entries)

        patch = {}        # entry index -> (data_hash, hash_0x60)
        moved60 = {}      # (type, name) -> (old_0x60, new_0x60)
        if data:
            for e in a.entries:
                buf = decompressed(a, e, oo)
                if buf is None:
                    stats["skipped"] += 1
                    continue
                new50 = murmur64b(buf, DATA_SEED)
                new60 = hash60_rule(e.data_hash, e.hash_0x60, new50)
                if new50 != e.data_hash or new60 != e.hash_0x60:
                    patch[e.index] = (new50, new60)
                    stats["hash_0x60"] += new60 != e.hash_0x60
                    if new50 != e.data_hash:
                        stats["data_hash"] += 1
                        stats["changed"].append((e.index, e.type, e.name,
                                                 e.data_hash, new50))
                    if new60 != e.hash_0x60:
                        moved60[(e.type, e.name)] = (e.hash_0x60, new60)

        with open(path, "rb") as fh:
            head = bytearray(fh.read(h.addr_data))
        base = h.addr_entries
        for index, (new50, new60) in patch.items():
            # +0x58 between them is the timestamp and stays untouched.
            struct.pack_into("<Q", head, base + index * ENTRY_SIZE + 0x50, new50)
            struct.pack_into("<Q", head, base + index * ENTRY_SIZE + 0x60, new60)

        # A (type, name) that several entries share -- 860 of them per archive,
        # all renderProgResource -- cannot be resolved to one entry, so there is
        # no way to know which +0x60 a dependency record meant. Left alone
        # rather than guessed.
        ambiguous = {k for k in moved60 if len(a.find(*k)) > 1}
        for key in ambiguous:
            del moved60[key]
        stats["ambiguous_0x60"] = len(ambiguous)

        # Only records that actually carried the old value; see the module
        # docstring for why matching by name alone would corrupt the file.
        for i, d in enumerate(a.dependencies):
            moved = moved60.get((d.type, d.name))
            if moved is not None and d.hash == moved[0]:
                struct.pack_into("<Q", head, h.addr_dependencies + i * DEPENDENCY_SIZE + 0x18,
                                 moved[1])
                stats["deps"] += 1

        meta_end = base + metadata_size(head)
        if bytes(head[meta_end - 4:meta_end]) != MAGIC:
            raise idcl.IdclError("%s: metadata does not end on %r at %d"
                                 % (path.name, MAGIC, meta_end - 4))
        struct.pack_into("<Q", head, 0x20, farmhash64(bytes(head[base:meta_end])))
        struct.pack_into("<Q", head, 0x08, header_hash(bytes(head[:HEADER_SIZE]), h.version))

    with open(path, "r+b") as fh:
        fh.write(head)
    return stats


def write(archive, dest, changes=(), oo=None, data=True):
    """Lay `archive` out into `dest` with `changes` applied, hashes recomputed.

    The layout is idcl.Archive.rebuild()'s -- table order, per-entry
    alignment, no trailer -- and finalize() then fixes what rebuild copies
    verbatim. With `changes` empty this reproduces the source file byte for
    byte, which is the control that proves both halves at once: the layout,
    because any drift shows up as a diff, and the hashes, because recomputing
    them has to land on the values the shipped file already carries.
    """
    archive.rebuild(dest, changes, oo)
    return finalize(dest, oo=oo, data=data)


def repair(src, dest, oo=None):
    """Copy a container and recompute its hashes -- payloads untouched.

    For a container whose payloads are already what they should be and whose
    hashes are not, which is what an authoring tool that predates the
    derivations leaves behind. The copy is byte-identical except for the entry
    table and the header, so no compressor is involved and no payload can
    change codec.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return finalize(dest, oo=oo, data=True)


def stale_data_hashes(path, oo=None):
    """Entries whose +0x50 does not match their payload. Empty == loadable.

    The check the engine runs with resourceStorage_checkDataCheckSum=1, as a
    library call rather than a probe script, so a writer can gate on it.
    """
    bad = []
    with idcl.Archive(path) as a:
        for e in a.entries:
            buf = decompressed(a, e, oo)
            if buf is not None and murmur64b(buf, DATA_SEED) != e.data_hash:
                bad.append(e)
    return bad


def stale_dep_hashes(archive):
    """Dependency records that hold a +0x60 no local entry carries any more.

    Conservative on purpose: a record is only reported when its (type, name)
    resolves to an entry *in this archive* and its hash is a 64-bit value
    that no entry of that name has. Most dep+0x18 values are 32-bit and
    unrelated to +0x60 (see the module docstring), so they are never flagged.
    """
    by_name = {}
    for e in archive.entries:
        by_name.setdefault((e.type, e.name), set()).add(e.hash_0x60)
    return [d for d in archive.dependencies
            if d.hash >> 32 and (d.type, d.name) in by_name
            and d.hash not in by_name[(d.type, d.name)]]
