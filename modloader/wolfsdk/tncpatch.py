"""Applying and reverting asset changes against an installed Wolfenstein II.

Why this is not patch.py
------------------------
id Tech 5 keeps its file table in separate .index files, so part one can take
a restore point by copying a few MB of index data. An IDCL archive carries
header, entry table, strings, dependencies *and* payloads in one file, and the
24 retail archives weigh 3.40 GB together. Copying them whole is the obvious
restore point and the wrong one.

What gets copied instead is the region ``[0, addr_data)`` of every archive --
everything except the payloads. Measured over the retail install that is
51.6 MB, 1.48 % of 3.40 GB. It is a *sufficient* restore point only because of
the rule this module enforces on itself:

    a write never moves a payload and never changes the file size.

A replacement goes into the slot the entry already owns, which is ``csize``
plus the zero-filled gap up to the next payload -- not ``csize`` alone. That
distinction is the difference between "almost never fits" and "almost always
fits": Kraken leaves so little slack inside csize that 3 of 25 test edits to
``weapon`` decls fit there, while csize + gap takes the same 25 edits to 21.
The gaps are measured, not assumed: 178077 entries across all 24 archives, no
overlap anywhere, every gap byte 0x00.

``csize``, ``usize`` and the compression byte move with the payload, and all
three live inside the backed-up head region. So reverting is:

    1. write the pristine head back over ``[0, addr_data)``,
    2. write the journalled original bytes back over every window we touched.

and the result is the original file bit for bit -- tools/verify_tncpatch.py
checks that by sha256 against an untouched copy rather than taking it on
trust.

The one case that breaks the rule is a payload too large even for its gap.
Then that archive is rebuilt end to end and its offsets move, so that archive
-- and only that one -- is copied whole beforehand. The journal records which,
and revert() puts the copy back. A rebuild of gameresources.resources costs
702 MB of backup; a mod that triggers it should know.

That copy is the only net under a rebuild, so it is treated like one. It is
written to a stump name and renamed into place, because ``shutil.copy2``
straight onto the target leaves half a file behind when the disk fills up at
702 MB and the next run would trust it. Its size and sha256 go into the
manifest, and a copy that is missing or does not match is a loud refusal, not
a silent no-op: restoring a rebuilt archive from a half backup destroys it.

Detecting a foreign change (a Steam update)
-------------------------------------------
The manifest stamps every archive with its size and the sha256 of its head
region *as this module left it*, refreshed by apply() and revert() and by
nothing else. A deviation is therefore somebody else's work, and it reads the
same way whether the installation is patched or pristine. Part one lost that
property once by deriving "this is ours" from "a journal exists", which made
the check blind in exactly the state where it matters.

Exactly three stamps count as "as we left it", each of them a hash comparison
rather than an exemption: the current one in the manifest, the pristine one
the backup was taken from (an untouched installation is never a reason to
refuse anything), and the one a *completed* journal carries. The third exists
because journal and manifest are two files. A crash between marking the
journal complete and refreshing the manifest used to leave apply()'s own
writes looking foreign, after which revert() refused for good and the
installation stayed patched -- the same failure part one had to repair three
times. The completed journal is therefore written in one atomic step together
with the stamps measured right after the writes, so it vouches for exactly
the archives it names. The mirror image in revert() is covered by the
pristine stamp: a restored installation is pristine, whatever the manifest
still says.

A fourth state has no stamp at all and cannot get one: the one a revert that
died halfway leaves behind -- half a head written, half the slots back, or a
702 MB copy half on the disk. Refusing there was the worst outcome of the
three, because it refused the one operation that repairs it and then advised
deleting the backup. So revert() -- and only revert(), on the archives its
own journal names, and only after a first strict pass has already found
something -- asks a narrower question than "is this one of the three
stamps": a rebuilt archive is let through when it still matches the stamp
apply() took the moment that rebuild landed, when it still matches the full
copy taken before the rebuild, or when it is gone altogether (the copy
recreates it) -- but not merely because it was rebuilt, because
overwriting a *foreign* version from the copy destroys it just as surely as
writing an old entry table over new payloads does. Both answers are hashes
because apply() runs the rebuild under a second name for the same file
(os.link, so it costs nothing on a 702 MB archive) and stamps and journals
the result *before* moving that name onto the live one. The live name
therefore holds the pristine archive right up to that one instruction, and an
archive the journal calls rebuilt is either stamped or provably untouched --
never a third thing nothing accounts for. An archive written in place is let
through only when every byte its head deviates by lies in a window this
module wrote (csize, usize and the compression flag of a journalled entry).
A game update touches more than that, and it still gets the refusal.

mtime is deliberately not part of the comparison: it moves for reasons that
are not content changes, and every change a game update makes to an archive
shows up in the entry table, hence in the head hash. What a head stamp cannot
see is a foreign edit that changes payload bytes only, leaves the file length
alone and leaves the table alone.

That gap is not closed, it is made visible. revert() cannot undo such an edit
-- the restore point holds no payload bytes for entries this module never
wrote -- but it no longer stamps the result as the new original either.
apply() records the sha256 of every archive it writes in place *as it found
it*; revert() re-reads the archive afterwards and compares, and a deviation
is written into the manifest and reported by verify() from then on. So the
promise is one step narrower than "the result is the original file bit for
bit": the result is the file as this module found it, and where that is not
true, the module says so instead of going quiet.

Header hashes and .texdb blocks
-------------------------------
An in-place write that moves csize or usize changes the entry table, and
OpenContainer rejects an archive whose +0x20 (FarmHash64 over the metadata)
or +0x08 (over the header) no longer matches. apply() therefore recomputes
both after writing an archive, +0x20 first. They sit in the backed-up head,
so the revert above stays bit-exact.

A texture's streamed mips live in .texdb files, addressed by key. A mod
carries such a block as asset ``texdb:<key in 16 hex digits>``; apply()
writes it for *every* row with that key, one of two ways:

  * a block that fits the old block's place goes there, zero-filled to the
    old block's end. Table, size and every offset stay. The original bytes
    go into one backup file before the journal that names them.
  * a longer block is appended at the end of that .texdb and the row's
    offset -- 8 bytes in the table -- is pointed at it. A .texdb has no
    length field anywhere: the engine reads stored_size bytes (from the
    image's own mip table) at the row's offset (texdbkey.md, .text 0x7A19CC
    and 0x7A1C7B), and the mount checks only the magic and the table size.
    The old block stays where it was, unreferenced; count, build id and
    every other byte stay as they are. The journal records the file's
    original size and each row's position, old and new offset, and revert()
    writes the old offsets back and cuts the file to its original size --
    the original file bit for bit.

Append first, rows second, with an fsync between them: a crash leaves every
row on its original block plus a tail, or some rows moved and some not, and
revert() repairs both, because both of its steps are idempotent.

A game update is told apart by the table, as before, but the table now moves
by design. The journal stamps each touched .texdb's original size and table
hash; the table is hashed with every row apply() repointed read as its old
offset -- and only when it holds its old or its new offset -- and the size may
lie anywhere between the original and the end apply() appended up to. That
lets pristine, applied and every state a crash between the two leaves
through, and nothing else: a changed byte elsewhere in head or table, a third
offset in one of our rows or a size outside the range is somebody else's
work, and revert() refuses to write old blocks into it.

Map caches (textures.cache)
---------------------------
Every map folder carries a textures.cache -- 40 files under base/maps/** and
dlc/*/base/maps/**, 67 MB each, 2.69 GB together. It is a .texdb under
another magic (docs/texdb.md §8, tools/probe_texcache.py), the engine reads it
whole into memory on map load and searches it *before* every .texdb: a key
it carries never reaches the disk file. The pistol's 64-256 px mips sit in
all 40, so a skin written only into the .texdb shows retail at mid range.

texdb_hits() therefore answers with the caches' rows too, and apply() writes
them by the rules above -- window or append plus row, journal, stamps,
bit-exact revert. Journal names are paths from base/ ("maps/.../textures.cache",
"../dlc/dlc_2/base/maps/..."); a base/*.texdb keeps its bare file name, so an
older journal still reads. A cache that is gone at revert time -- a custom
map uninstalled -- is skipped rather than refused: there is nothing left to
overwrite wrongly, and the rest has to go back regardless.

Some caches are hardlinks: dlc_0 (the user's custom-map folder) links its
side files to dlc_2's. Writing in place there writes both, so a file with
more than one name is never written through. It gets a second name for its
original in the backup directory (os.link, no copy), the edits go into a
fresh copy beside it, and os.replace moves that onto the live name. Every
other name keeps the untouched original; revert() moves the backup name back,
which restores content, file identity and link count in one step. Cost: one
67 MB copy per such name while applied, against ~20-75 KB appended per
ordinary cache for the pistol skin.

Whole files: videos and sounds
------------------------------
Two asset types replace a file outside the .resources the same way, whole:
``video:<path from the game root>`` a loose base/bink or dlc/*/base/bink
video, and ``wem:<scope>/<language>/<media id>`` (the Studio's sound id) every
sound pack holding a copy of that Wwise medium or a Sound object naming it,
rebuilt by idclwrite with the banks wwise.replace_plan edits. The original
keeps a second name in the backup directory, the new file is written beside
the live one and moved onto it; the journal ("files") records the original's
file id before and the new one's before the move, so revert() moves the
original back exactly as for a relinked cache, and a live file that is
neither -- a game update -- is refused as foreign. Cost while applied: the
new files (a sound in base/ rewrites sound.pack, 1.7 GB).

A file somebody else holds open
-------------------------------
On Windows os.replace onto a name fails while *any* other handle is open on
that file, whatever its share mode -- measured: a reader sharing read, write
and delete blocks it, and so does a handle opened for attributes only. The
rebuild swap and the hardlink swap both replace a live name, and a scanner,
the running game or an Explorer preview was enough to break them halfway;
the rollback then tried to replace the same held file, failed as well, and
left .texdb blocks patched beside an archive that was not.

So apply() and revert() first ask every existing file they will replace,
remove or write (_refuse_held) -- archives and .texdb, and in the backup
directory the full copies, the .orig names, texdb.orig, journal and
manifest, plus any leftover scratch name a run writes and then moves (the
.tmp files, a stage or .idcl_tmp a held handle kept from being swept).
Each is asked with the open the operation itself makes, measured
on Windows 11 to fail in exactly the same cases: DELETE sharing everything
for an unlink or a rename away (a handle that shares delete does not stop
it, and one on another hardlink name does not either), read/write sharing
read/write for open(, "r+b"). No share mode predicts a replace, so that one
is asked exclusively. A read-only file passes both of the first two and is
still refused by unlink and replace, so it is looked at on its own and
named as what it is. A refusal comes before the first write and changes
nothing.

What the probe cannot see -- a handle for attributes only, or one taken
after it, typically on a file this run just wrote -- is met by retrying
every replace, unlink and in-place open for RETRY_DELAYS (patch._retry,
which _write_json uses too), and by the order of the writes: apply() does
the archives first and the .texdb side last, and _restore takes the .texdb
side back first. A .texdb is therefore patched only while every archive of
the run is, whichever step fails -- the mismatch the game cannot read, a
texture block patched beside the archive it belongs to still original,
does not occur. _restore also leaves a rebuilt archive alone that is still
its full copy byte for byte, which is what a failed swap leaves, so a
rollback never needs the file that just refused; and a full copy that
stays held after its records are gone is left for _drop_orphan_fulls
rather than stopping the restore halfway.

Whatever still fails is told in German with the held hint: a refused rename
names its source when that is the held file (a scanner on a fresh stage),
and a lock file or a table that cannot even be read gets the same sentence
instead of the C library's "Permission denied".

Two of these at once
--------------------
apply() and revert() hold an exclusive lock on the backup directory for their
whole run. Two interleaved apply() runs otherwise end up with one run's head
region over the other's payloads -- for a Kraken entry a decode error in the
game -- and nothing anywhere says so. The lock is an OS byte-range lock, not
a pid file: the kernel drops it when the process dies, so there is no orphan
to clean up and no guess about whether the owner is still alive.
"""

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import struct
import time
from array import array
from bisect import bisect_left
from pathlib import Path, PurePosixPath

try:                        # the lock primitive, whichever one this OS has
    import fcntl
    msvcrt = None
except ImportError:         # pragma: no cover - Windows
    import msvcrt
    fcntl = None

if os.name == "nt":         # the exclusive open behind _probe
    import ctypes
    from ctypes import wintypes
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

from . import cutsceneaudio, oodle, tncimage, wwise
from .idcl import Archive, COMP_KRAKEN, COMP_KRAKEN_BLOCKS, COMP_STORED, ENTRY_SIZE, IdclError, pack_payload
# Reused rather than copied: an atomic JSON write and a limited sha256 are
# exactly the two helpers part one got wrong twice, and a second copy here
# would be a second chance to get them wrong.
from .patch import _sha256, _write_json
# RETRY_DELAYS and _retry, looked up at call time: one list for both modules.
from . import patch as _patch

JOURNAL_NAME = "journal.json"
MANIFEST_NAME = "manifest.json"
BACKUP_DIR = "_wolfsdk_tnc"
HEAD_SUFFIX = ".head"
LOCK_NAME = "lock"
# Where a rebuild lands before it is stamped and swapped in. idcl.Archive
# builds under ".idcl_tmp" beside whatever it was opened on, so a rebuild
# staged as ".idcl_stage" leaves both names behind when it dies.
TMP_SUFFIX = ".idcl_tmp"
STAGE_SUFFIX = ".idcl_stage"
# Keeps the .resources ending: whatever a broken-off copy is called, it is
# still a stray archive in the backup directory and has to read as one.
PART_INFIX = ".unvollstaendig"

# A .texdb block is addressed as asset "texdb:<16 hex digits>", its key. The
# original bytes of every block apply() overwrites go into one file in the
# backup directory, written before the journal that names them.
TEXDB = "texdb"
TEXDB_BACKUP = "texdb.orig"
# The maps' in-memory texture databases (see the module head).
CACHE_NAME = "textures.cache"
CACHE_MAGIC = bytes.fromhex("064d5e3b229907ab")   # LE 0xab0799223b5e4d06, .text 0x7932C9
# A file with more than one name is rewritten under this suffix beside the
# live name and moved over it; its original keeps a name in the backup
# directory, "<path from base/ with / as _>" + LINK_SUFFIX.
NEW_SUFFIX = ".wolfsdk_neu"
LINK_SUFFIX = ".orig"
# Whole-file assets (see the module head).
VIDEO, WEM = "video", "wem"
WHOLE = (VIDEO, WEM)
# Header fields apply() refreshes after an in-place write: +0x20 hashes the
# entry table (csize/usize moved), +0x08 hashes the header including +0x20.
HEADER_HASHES = ((0x08, 0x10), (0x20, 0x28))

NO_BACKUP = "No backup found - nothing to revert."

# What a run does to a file, in the words a refusal uses for it. OVERWRITE
# is a scratch name that is written and then moved onto the real one.
REPLACE, REMOVE, WRITE, OVERWRITE = "replaced", "removed", "written", "overwritten"
READ, MOVE, OPEN = "read", "renamed", "opened"
NOT_STARTED = "Not started, nothing was changed:"
HELD = ("Another program is probably holding the file open: a virus scanner, "
        "the running game, an Explorer preview or a backup program. "
        "Close that program or wait a moment, then try again.")
READ_ONLY = ("Read-only means: open the file's Properties (right-click), "
             "untick \"Read-only\", then try again.")

FOREIGN = ("%s no longer matches the backup. Game update? "
           "Discard the backup and create a new one.")
# The same refusal, for the one case where FOREIGN's advice destroys the only
# way back. _resumable() turns down a rebuilt archive it cannot account for,
# but the hash-checked full copy of exactly that archive is lying in the
# backup directory. Follow FOREIGN there and the copy goes with the backup,
# ensure_backup() stamps the patched state as the new original, and verify()
# says "clean" from then on -- Steam's file check is the only way out.
# Copying the copy over the live archive by hand puts revert() back on its
# feet, and it then ends bit-exact original. The refusal itself does not
# move: what changes is only what the user is told to do about it.
RESCUE = ("%s no longer matches the backup, but it was rebuilt - the verified "
          "full copy is at %s. Copy that file over %s by hand, then revert "
          "again. Do NOT discard the backup: it holds the only copy of this "
          "archive.")


class TncPatchError(Exception):
    pass


class TncLinkError(TncPatchError, OSError):
    """os.link refused, so a rebuild cannot be staged.

    Also an OSError, and deliberately: what happened *is* a filesystem
    refusal, every caller that already handles OSError around a write keeps
    working, and code that only knows this module's own error type now sees
    it too. Before, the bare PermissionError(1, 'Incorrect function') that a
    library on exFAT or a network share produces came through untranslated,
    and every mod needing a rebuild died on a line the user cannot act on.
    """


def _precedence(archive_name):
    """Sort key: the newest patch_N archive first, then the rest in find() order (stable)."""
    m = re.match(r"patch_(\d+)", archive_name)
    return -int(m.group(1)) if m else 0


def _read_at(path, offset, size):
    with open(path, "rb") as fh:
        fh.seek(offset)
        buf = fh.read(size)
    if len(buf) != size:
        raise TncPatchError("%s: only read %d of %d bytes at %d"
                            % (Path(path).name, len(buf), size, offset))
    return buf


# The CreateFileW (access, share mode) each kind is probed with -- the open
# the operation itself makes, see the module head: DELETE sharing
# everything is DeleteFileW's and MoveFileExW's, GENERIC_READ|GENERIC_WRITE
# sharing read/write is open(, "r+b")'s. REPLACE has no such open and is
# asked exclusively: GENERIC_READ|DELETE, no sharing. OVERWRITE is both
# opens of a scratch name at once: open(, "wb") and the rename away.
_PROBES = {REPLACE: (0x80010000, 0), REMOVE: (0x00010000, 7), WRITE: (0xC0000000, 3),
           OVERWRITE: (0x40010000, 3)}


def _probe(path, kind):
    """Raise OSError when `path` cannot be `kind` (a key of _PROBES) now."""
    if os.name != "nt":
        if kind == WRITE:
            open(path, "r+b").close()
        return
    h = _k32.CreateFileW(str(path), *_PROBES[kind], None, 3, 0, None)   # OPEN_EXISTING
    if h == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    _k32.CloseHandle(h)


def _read_only(path):
    return os.path.isfile(path) and not os.access(path, os.W_OK)


def _why(exc):
    """The OS's own text for `exc`, without its full stop.

    open() fails through the C library, whose text is English on every
    system; its access-denied is given the way Windows words it.
    """
    if os.name == "nt" and isinstance(exc, PermissionError) and exc.winerror is None:
        return ctypes.FormatError(5).rstrip(". ")
    return (exc.strerror or str(exc)).rstrip(". ")


def _refusal(held, head=None):
    """TncPatchError naming each (path, kind, exc) of `held`, then what to do.

    A read-only file is called that, not "held": no program closing will
    free it. Reading and renaming it away work regardless, so for those
    the attribute says nothing.
    """
    ro = [k not in (READ, MOVE) and _read_only(p) for p, k, _e in held]
    lines = ["%s is read-only." % os.path.normpath(p) if r
             else "%s cannot be %s right now (%s)." % (os.path.normpath(p), k, _why(e))
             for (p, k, e), r in zip(held, ro)]
    if head:
        lines = [head] + ["  " + line for line in lines]
    if not all(ro):
        lines.append(HELD)
    if any(ro):
        lines.append(READ_ONLY)
    return TncPatchError("\n".join(lines))


def _refuse_held(targets):
    """TncPatchError naming every file of `targets` the run cannot touch now.

    `targets` are (path, kind) pairs. Asked before the first write, so a
    refusal changes nothing; a held file is asked again after each of
    RETRY_DELAYS, so a scanner that lets go soon does not cost the run. A
    read-only file is not waited for -- it does not go away by itself.
    """
    ro = [(p, k, None) for p, k in targets if _read_only(p)]
    todo = [(p, k) for p, k in targets if not _read_only(p)]
    for delay in _patch.RETRY_DELAYS + (None,):
        held = []
        for path, kind in todo:
            try:
                _probe(path, kind)
            except OSError as exc:
                held.append((path, kind, exc))
        if not held and not ro:
            return
        if delay is None or not held:
            raise _refusal(ro + held, NOT_STARTED)
        time.sleep(delay)
        todo = [(p, k) for p, k, _e in held]


def _culprit(exc):
    """(file, kind) a PermissionError is about.

    A refused rename is not always its target's fault: a scanner on the
    file that was just written -- a fresh .idcl_stage, a .tmp -- stops it
    just as well, and naming the live archive then sends the user after
    the wrong file. The source is asked the way the rename opens it.
    """
    src, dst = exc.filename, exc.filename2
    if dst is None:
        return src or "file", "used"
    try:
        _probe(src, REMOVE)
    except OSError:
        return src, MOVE
    return dst, REPLACE


def _swap(src, dst):
    """os.replace(src, dst), asked again while Windows says the file is in use."""
    try:
        return _patch._retry(os.replace, src, dst)
    except PermissionError as exc:
        raise _refusal([_culprit(exc) + (exc,)]) from exc


def _open_rw(path):
    """open(path, "r+b"), asked again while Windows says the file is in use."""
    return _patch._retry(open, path, "r+b")


@contextlib.contextmanager
def _as_held():
    """A PermissionError out of apply() or revert() as a sentence to act on.

    A file another program opened without sharing even fails the first
    read, before _refuse_held is ever asked -- which changes nothing, but
    used to reach the user as a bare WinError.
    """
    try:
        yield
    except PermissionError as exc:
        raise _refusal([_culprit(exc) + (exc,)]) from exc


def _is_copy(path, full):
    """True when `path` is, byte for byte, the full copy `full` describes."""
    return (bool(full) and path.is_file() and path.stat().st_size == full["size"]
            and _sha256(path) == full["sha256"])


def gaps_of(archive):
    """entry.index -> free bytes between that payload and the next one.

    Read off the file rather than assumed: payloads are laid out in ascending
    offset order with nothing but zero padding between them, and the last one
    ends on the file size.
    """
    order = sorted(archive.entries, key=lambda e: e.offset)
    out = {}
    for i, e in enumerate(order):
        nxt = order[i + 1].offset if i + 1 < len(order) else archive.file_size
        out[e.index] = max(0, nxt - (e.offset + e.csize))
    return out


def _refresh_header_hashes(path):
    """Recompute +0x20 and then +0x08 of one archive, in place.

    OpenContainer rejects a container whose +0x20 (FarmHash64 over the entry
    table and the rest of the metadata) or +0x08 does not match
    (re_probes/agent_reports/idclhash.md), and every write that moves csize
    or usize changes the table. Both fields lie in the backed-up head, so
    revert() still restores them bit for bit. Imported here rather than at
    the top: the hash implementations live with the probes that derived them.
    """
    from .idclwrite import finalize
    finalize(path, data=False)


def _texdb_table_sha(path, rows=()):
    """sha256 of a .texdb's head and key table -- what a game update changes.

    `rows` are the offset fields apply() repointed, [position, old, new]. Each
    has to hold old or new and is hashed as old, so the answer is the
    pristine table's hash whether those rows are moved, back or half of each.
    None when one holds anything else, or when the table does not fit.
    """
    with open(path, "rb") as fh:
        head = fh.read(tncimage.TEXDB_TABLE)
        count = struct.unpack_from("<Q", head, 0x18)[0] if len(head) == tncimage.TEXDB_TABLE else 0
        if len(head) + 16 * count > os.fstat(fh.fileno()).st_size:
            return None
        table = bytearray(head + fh.read(16 * count))
    for pos, old, new in rows:
        if pos + 8 > len(table) or struct.unpack_from("<Q", table, pos)[0] not in (old, new):
            return None
        struct.pack_into("<Q", table, pos, old)
    return hashlib.sha256(table).hexdigest()


def _write_texdb(slots, blocks):
    """Write each block into its window, zero-filled to the window's end."""
    for slot, block in zip(slots, blocks):
        with _open_rw(slot["path"]) as fh:
            fh.seek(slot["offset"])
            fh.write(block + b"\0" * (slot["size"] - len(block)))


def _append_texdb(path, size, blocks, rows):
    """Append `blocks` at `size`, the file's old end, then repoint `rows`.

    The data is on the disk before the first row moves, so a crash in
    between leaves every row on its original block and only a tail to cut.
    """
    with _open_rw(path) as fh:
        fh.seek(size)
        fh.write(b"".join(blocks))
        fh.flush()
        os.fsync(fh.fileno())
        for pos, _old, new in rows:
            fh.seek(pos)
            fh.write(struct.pack("<Q", new))


def _write_new(live, keep, slots, blocks, app, tail):
    """Write a file that has other names as a new file; never through the link.

    `keep` becomes a second name of the original first, so it stays reachable
    whatever happens to the others; then a copy beside `live` takes the
    windows and the tail, is synced, and os.replace moves it onto `live`.
    """
    new = _keep_original(live, keep)
    shutil.copyfile(live, new)
    _write_texdb([dict(s, path=new) for s in slots], blocks)
    if app:
        _append_texdb(new, app["size"], tail, app["rows"])
    with _open_rw(new) as fh:
        os.fsync(fh.fileno())
    _swap(new, live)


def _keep_original(live, keep):
    """Give `live`'s original the second name `keep`; returns the scratch name the new file goes to."""
    _patch._retry(keep.unlink, missing_ok=True)    # no journal names it: apply() reverted first
    try:
        os.link(live, keep)
    except OSError as exc:
        raise TncPatchError(
            "%s is not written through, but no second name for the original can be "
            "created under %s (%s). Hard links need an NTFS drive."
            % (live, keep.parent, exc)) from exc
    return live.with_name(live.name + NEW_SUFFIX)


def _write_whole(live, keep, fill):
    """The new `live` as a new file (fill(path) writes it), the original kept as `keep`.
    Returns the scratch path, synced and not yet moved: the caller records its id first."""
    new = _keep_original(live, keep)
    fill(new)
    with _open_rw(new) as fh:
        os.fsync(fh.fileno())
    return new


def _forget_sounds():
    """Drop the per-process Wwise indexes: a replaced pack moves every offset in them."""
    wwise._index.cache_clear()
    cutsceneaudio.objects.cache_clear()


def _rebuild_pack(src, dest, changes):
    """A sound pack laid out again into `dest` with `changes` ({entry index: payload}),
    header hashes recomputed (stored entries: no Oodle)."""
    from .idclwrite import write
    with Archive(src) as a:
        write(a, dest, {a.entries[i]: d for i, d in changes.items()}, data=False)


class CacheSet(tncimage.TexdbSet):
    """The maps' textures.cache files: a .texdb key table under CACHE_MAGIC.

    Only the magic differs (tools/probe_texcache.py), so rows() and lookup()
    are TexdbSet's; this reads the table.
    """

    def _table(self, path):
        if path not in self._tables:
            try:
                with open(path, "rb") as fh:
                    head = fh.read(tncimage.TEXDB_TABLE)
                    size = os.fstat(fh.fileno()).st_size
                    count = struct.unpack_from("<Q", head, 0x18)[0] if len(head) == tncimage.TEXDB_TABLE else 0
                    if head[:8] != CACHE_MAGIC or tncimage.TEXDB_TABLE + 16 * count > size:
                        raise tncimage.TncImageError("%s is not a readable %s" % (path, CACHE_NAME))
                    rows = array("Q")
                    rows.frombytes(fh.read(16 * count))
                self._tables[path] = [rows[0::2], rows[1::2], None, size]
            except OSError as exc:
                self._tables[path] = tncimage.TncImageError("%s: %s" % (path, exc.strerror or exc))
            except tncimage.TncImageError as exc:
                self._tables[path] = exc
        return self._tables[path]


def _write_slots(archive, items):
    """Overwrite whole slot windows in one archive: payload and table fields.

    Leaves the archive closed; the caller re-opens through Installation.close().
    """
    base = archive.header.addr_entries
    path = archive.path
    archive.close()
    with _open_rw(path) as fh:
        for entry, _data, (window, csize, usize, comp) in items:
            fh.seek(entry.offset)
            fh.write(window)
            pos = base + entry.index * ENTRY_SIZE
            fh.seek(pos + 0x40)
            fh.write(struct.pack("<2Q", csize, usize))
            fh.seek(pos + 0x70)
            fh.write(bytes((comp,)))


class Installation:
    """The .resources archives in base/, plus backup and journal handling."""

    def __init__(self, base, backup=None, oo=None):
        self.base = Path(base)
        self.backup = Path(backup) if backup else self.base / BACKUP_DIR
        # Deliberately not self.backup: what two runs fight over is the
        # installation, not the folder their records happen to live in.
        # Installation(base, backup=X) and Installation(base) write the same
        # archives, so they have to exclude each other.
        self.lock_path = self.base / BACKUP_DIR / LOCK_NAME
        self._oodle = oo
        self._archives = None
        self._texdbs = None
        self._caches = None
        self._held = 0

    # -- exclusive access --------------------------------------------------

    @contextlib.contextmanager
    def _lock(self):
        """Exclusive access to the installation for one apply() or revert().

        Reentrant per Installation, because apply() reverts first and that is
        one operation, not two. Between two Installation objects -- in this
        process or another one -- it is not: the lock sits on the file handle,
        so a second opener is refused even inside the same interpreter.
        """
        if self._held:
            self._held += 1
            try:
                yield
            finally:
                self._held -= 1
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT)
        except PermissionError as exc:      # held or read-only; the lock comes before any write
            raise _refusal([(self.lock_path, OPEN, exc)], NOT_STARTED) from exc
        try:
            try:
                if msvcrt is not None:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise TncPatchError(
                    "Another operation is working on this installation "
                    "right now (%s). Wait for it to finish first." % self.lock_path)
            self._held = 1
            try:
                yield
            finally:
                self._held = 0
        finally:
            os.close(fd)        # releases the lock, crash or not

    # -- archives ----------------------------------------------------------

    @property
    def names(self):
        """Archive file names, read fresh so a new one from an update shows up."""
        return sorted(p.name for p in self.base.iterdir() if p.suffix == ".resources")

    @property
    def oodle(self):
        if self._oodle is None:
            self._oodle = oodle.load()
        return self._oodle

    @property
    def archives(self):
        if self._archives is None:
            self._archives = [Archive(self.base / n) for n in self.names]
        return self._archives

    def close(self):
        for a in self._archives or ():
            a.close()
        self._archives = None
        self._texdbs = None
        self._caches = None

    @property
    def texdbs(self):
        """Every base/*.texdb, as a tncimage.TexdbSet (tables read on demand)."""
        if self._texdbs is None:
            self._texdbs = tncimage.TexdbSet(sorted(self.base.glob("*.texdb")))
        return self._texdbs

    @property
    def caches(self):
        """Every map's textures.cache, base/ and each DLC, as a CacheSet."""
        if self._caches is None:
            self._caches = CacheSet(sorted(self.base.glob("maps/**/" + CACHE_NAME))
                                    + sorted(self.base.parent.glob("dlc/*/base/maps/**/" + CACHE_NAME)))
        return self._caches

    def rel(self, path):
        """Journal name of a .texdb or cache: its path from base/, '/'-separated.

        A base/*.texdb is its bare file name, as in journals written before
        the caches were.
        """
        return Path(os.path.relpath(path, self.base)).as_posix()

    def texdb_hits(self, name):
        """[{"path", "offset", "size", "row"}] -- every row of texdb key `name` (16 hex).

        Keys repeat within and across files and the engine takes whichever it
        finds first in its mount order, which nobody has measured; so, like
        find(), this answers with every copy and apply() writes them all --
        the .texdb rows first, then every map's textures.cache, which the
        engine searches before any .texdb. "row" is the file position of
        that row's offset field.
        """
        if len(name) != 16:
            return []
        try:
            key = int(name, 16)
        except ValueError:
            return []
        out = []
        for db in (self.texdbs, self.caches):
            try:
                rows = db.rows(key)
            except tncimage.TncImageError as exc:
                # The table reader keeps only the C library's English text;
                # a file somebody holds is asked again for the real reason.
                for p in db.paths:
                    try:
                        open(p, "rb").close()
                    except PermissionError as err:
                        raise _refusal([(p, READ, err)]) from exc
                    except OSError:
                        pass
                raise TncPatchError(str(exc))
            # rows() lists a file's rows of one key in table order, from the first.
            first = {}
            for n, (p, s, e) in enumerate(rows):
                if p not in first:
                    first[p] = n - bisect_left(db._table(p)[0], key)
                out.append({"path": p, "offset": s, "size": e - s,
                            "row": tncimage.TEXDB_TABLE + 16 * (n - first[p]) + 8})
        return out

    def find(self, type_, name):
        """[(archive, entry), ...] -- every copy of the asset, everywhere.

        Two kinds of duplicate, both measured on the retail data and both
        fatal if only the first hit is patched:

          * 5978 (type, name) pairs ship in more than one archive, because a
            patch_N archive overrides the chunk_N one it was built against;
          * 860 pairs appear 5 or 6 times *inside* one archive (all of them
            renderProgResource, distinguished by unknown_0x72).

        Together that is 3869 entries that idcl.Archive.find() used to drop on
        the floor. Patching one copy and leaving the rest is the same bug that
        made a texture mod half-effective in part one.

        Type "texdb" answers with texdb_hits() instead: a .texdb block has a
        key, not an archive entry. "video" answers [path] of the loose file,
        "wem" the medium's copies in the sound packs (wwise's index).
        """
        if type_ == TEXDB:
            return self.texdb_hits(name)
        if type_ == VIDEO:
            path = self._video(name)
            return [path] if path else []
        if type_ == WEM:
            try:
                index, _ = wwise._index(str(self.base.parent))
            except (wwise.WwiseError, IdclError, OSError) as exc:
                raise TncPatchError("Sound packs not readable: %s" % exc)
            return index.get(name, [])
        return [(a, e) for a in self.archives for e in a.find(type_, name)]

    def _video(self, name):
        """The loose video `name` names (base/bink/... or dlc/<x>/base/bink/..., from the
        game root), or None: anything else -- another folder, '..', a drive -- is no video."""
        parts = PurePosixPath(name).parts
        ok = (PurePosixPath(name).suffix.lower() in (".bk2", ".bik") and "\\" not in name and ":" not in name
              and ".." not in parts and (parts[:2] == ("base", "bink")
                                         or len(parts) > 4 and parts[0] == "dlc" and parts[2:4] == ("base", "bink")))
        path = self.base.parent.joinpath(*parts) if ok else None
        return path if path is not None and path.is_file() else None

    def read(self, type_, name):
        """The asset's bytes as they stand now, from the copy the game loads.

        "The" copy is a fiction in part two: find() answers with a list
        because 1701 retail (type, name) pairs exist more than once. Which of
        them the engine loads is not proven at runtime, but everything
        measurable points at the patch layer (docs/patchvorrang.md: of 1680
        base/patch collisions the patch copy is newer 1635 times, never
        older). So this reads the copy in the newest patch_N archive, else
        the first one found -- the copy the Studio shows. Reading hits[0]
        instead (chunk_* sorts before patch_*) turned 1635 edits into
        edits of the older base text.

        That is not the coin toss it looks like, because of what the caller
        does with the bytes: mod.build_payloads reads the asset, layers the
        mods over it and hands the result to apply(), which writes it into
        *every* copy. Whichever copy was read, all of them carry the same
        bytes afterwards, so for what the game ends up loading the precedence
        question does not arise. What it does cost is worth saying plainly:
        where two copies differ today, the other one's content is replaced by
        this one plus the edits, not merged with it.
        """
        hits = self.find(type_, name)
        if not hits:
            raise TncPatchError("Asset not found: %s:%s" % (type_, name))
        if type_ == TEXDB:
            return _read_at(hits[0]["path"], hits[0]["offset"], hits[0]["size"])
        if type_ == VIDEO:
            return hits[0].read_bytes()
        if type_ == WEM:
            whole = [c for c in hits if c["info"]["complete"]]
            if not whole:
                raise TncPatchError("Only the prefetched start of sound %s is stored." % name)
            c = wwise._best(whole)
            return _read_at(c["pack"], c["offset"], c["size"])
        archive, entry = min(hits, key=lambda h: _precedence(h[0].path.name))
        # IDCL's Archive parser deliberately stays stdlib-only and therefore
        # exposes Kraken payloads through read_raw().  The TNC patcher does
        # have the game's Oodle DLL available, however: without decoding here
        # every compressed decl (notably several goreBehavior assets) appears
        # uneditable even though the builder and extractor can read it.
        raw = archive.read_raw(entry)
        if entry.stored_raw:
            return raw
        return self.oodle.decompress(raw, entry.usize)

    # -- journal / manifest ------------------------------------------------

    @property
    def journal_path(self):
        return self.backup / JOURNAL_NAME

    @property
    def manifest_path(self):
        return self.backup / MANIFEST_NAME

    def read_journal(self):
        """The journal, or None *only* when there is none.

        Broken JSON must never read as "never patched": revert() would then do
        nothing at all and leave a patched installation behind with no record
        of what was changed.
        """
        try:
            raw = self.journal_path.read_bytes()
        except FileNotFoundError:
            return None
        except PermissionError as exc:
            raise _refusal([(self.journal_path, READ, exc)]) from exc
        except OSError as exc:
            raise TncPatchError("Journal not readable: %s" % exc)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise TncPatchError(
                "The journal is damaged (%s). The installation is probably "
                "patched; restore the backup in %s by hand."
                % (exc, self.backup))

    def is_modified(self):
        return self.read_journal() is not None

    def read_manifest(self):
        """The manifest, or None only when there is none. See read_journal."""
        try:
            raw = self.manifest_path.read_bytes()
        except FileNotFoundError:
            return None
        except PermissionError as exc:
            raise _refusal([(self.manifest_path, READ, exc)]) from exc
        except OSError as exc:
            raise TncPatchError("Backup folder not readable: %s" % exc)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise TncPatchError("The backup is damaged (%s): %s"
                                % (exc, self.manifest_path))

    def ensure_backup(self):
        """Copy the head region of every archive and stamp the live state.

        Refuses while a journal exists: that would capture an already modified
        installation as the restore point and make reverting impossible.

        Under the same lock as apply() and revert(), because it does the same
        kind of damage: it deletes full copies and overwrites the manifest. A
        run that slipped in beside an apply() used to take the only net out
        from under a rebuild in the window before the journal was written,
        after which revert() reported success without restoring anything.
        """
        with self._lock():
            if self.is_modified():
                raise TncPatchError(
                    "A journal already exists. Run 'revert' first, otherwise "
                    "a changed state would be saved as the original.")
            self.backup.mkdir(parents=True, exist_ok=True)
            # No journal, so no full copy can belong to anything. The records
            # go first, the files after -- nothing here ever deletes a
            # .resources the manifest still points at.
            self._drop_orphan_fulls()
            for stale in self.backup.iterdir():
                if stale.suffix == ".resources":
                    stale.unlink()
            manifest = {"created": time.time(), "archives": {}}
            for name in self.names:
                path = self.base / name
                with Archive(path) as a:
                    head_size = a.header.addr_data
                head = self.backup / (name + HEAD_SUFFIX)
                # Always rewritten, never reused: a head left over from an
                # older game version would be recorded as this version's
                # pristine state.
                head.write_bytes(_read_at(path, 0, head_size))
                info = {"head_size": head_size, "head_sha256": _sha256(head),
                        # The pristine stamp, never refreshed. head_sha256 is
                        # the head region of the untouched file, so size and
                        # hash together are what "nobody has touched this"
                        # looks like.
                        "orig_size": path.stat().st_size}
                manifest["archives"][name] = self._stamp(info, name)
            _write_json(self.manifest_path, manifest)
            return manifest

    def _drop_orphan_fulls(self):
        """Full copies that no journal claims any more.

        A full copy is the only net under one rebuild, and it belongs to the
        journal that ordered it. apply() writes the manifest entry first and
        the journal second, so a crash in between leaves a record with no
        owner -- and nothing ever looked at it again, because _restore only
        touches what journal["rebuilt"] names. Up to 702 MB per archive,
        lying there for good, and deleting it by hand locked the module out
        ("Vollkopie fehlt").

        The order is the same one _restore uses: record first, file second.
        A crash in between leaves an unused copy, the other way round leaves
        a record pointing at nothing.
        """
        manifest = self.read_manifest()
        if not manifest or self.read_journal() is not None:
            return
        if [n for n, info in manifest["archives"].items() if info.pop("full", None)]:
            _write_json(self.manifest_path, manifest)
        # Records gone, now every copy no record claims -- which covers the
        # one a crash between those two steps leaves behind as well. A stump
        # is left alone: it is the one file in here that still has something
        # to say, and verify() is where it says it. One somebody still holds
        # stays for the run after: sweeping is not worth refusing a run.
        for stray in self.backup.iterdir():
            if stray.suffix == ".resources" and PART_INFIX not in stray.name:
                with contextlib.suppress(OSError):
                    stray.unlink()

    def _strays(self):
        """What a rebuild that died leaves behind in base/.

        Three names: the staging archive, the tmp idcl builds under it, and
        -- from a journal an older version wrote -- the tmp beside the live
        name itself. Each one is up to a whole archive, 702 MB for
        gameresources.resources, and nothing else on the machine ever looks
        at them.

        Read off the directory rather than off journal["archives"], because
        the journal that ordered them is itself something a crash can leave
        half-written, and a stray whose journal is gone is exactly the one
        nobody would ever find. Only this module and idcl's rebuild produce
        these two suffixes beside an archive, so the name is the evidence.
        """
        return sorted(p for p in self.base.iterdir()
                      if p.is_file() and p.suffix in (STAGE_SUFFIX, TMP_SUFFIX))

    def _sweep(self):
        """Delete the strays. One somebody holds stays: sweeping is not worth
        failing a run over, and the run's own check asks for the names it
        needs."""
        for stray in self._strays():
            with contextlib.suppress(OSError):
                stray.unlink()

    def _stamp(self, info, name):
        """Record an archive's state as we are leaving it."""
        path = self.base / name
        info["last_size"] = path.stat().st_size
        info["last_head_sha256"] = _sha256(path, info["head_size"])
        return info

    def _stamp_all(self):
        manifest = self.read_manifest()
        if not manifest:
            return
        for name, info in manifest["archives"].items():
            if (self.base / name).is_file():
                self._stamp(info, name)
        _write_json(self.manifest_path, manifest)

    def _mark_foreign(self, names):
        """Record a deviation revert() could not undo, so it stays visible."""
        manifest = self.read_manifest()
        for name in names:
            manifest["archives"][name]["fremd"] = True
        _write_json(self.manifest_path, manifest)

    def _journal_stamps(self):
        """The stamps a *completed* journal vouches for. See the module head."""
        try:
            journal = self.read_journal()
        except TncPatchError:
            return {}
        if not journal or not journal.get("complete"):
            return {}
        return journal.get("stamps") or {}

    def verify(self, skip=(), strays=True):
        """Problems with the backup, or signs that somebody else changed the game.

        `skip` names archives whose stamp is knowingly stale. It is ever only
        what revert()'s _resumable() worked out archive by archive -- never
        "everything, because a journal exists".

        `strays` is off where this list is used as a gate rather than as a
        report. A leftover staging file is a disk-space finding, not a
        damaged backup: apply() and revert() delete it before they start,
        and refuse up front on one they cannot. Reporting it into a gate
        would refuse both of the operations that clear it -- the same shape
        _drop_orphan_fulls() already exists to avoid.
        """
        manifest = self.read_manifest()
        if not manifest:
            return ["No backup found."]
        known = manifest["archives"]
        problems = ["Archive is new since the backup: %s. Game update?" % n
                    for n in self.names if n not in known]
        for p in sorted(self.backup.iterdir()):
            if PART_INFIX not in p.name or p.suffix != ".resources":
                continue
            # Two very different things end up under this name. A copy that
            # died mid-way -- disk full -- is half an archive and a reason to
            # stop. A copy whose os.replace failed is *complete*: on Windows
            # that is the normal outcome when anybody holds the target open,
            # the installation is untouched, and refusing over it used to
            # advise deleting the only copy of a rebuilt archive.
            # Measured against the pristine stamp, not against the live
            # archive, so the answer does not change when a later apply()
            # patches that archive.
            info = known.get(p.name.replace(PART_INFIX, ""))
            if (info and p.stat().st_size == info.get("orig_size")
                    and _sha256(p, info["head_size"]) == info["head_sha256"]):
                continue
            problems.append(
                "Incomplete full copy in the backup folder: %s. The last run "
                "died while copying (disk full?). Delete the file and create "
                "the backup again." % p.name)
        # base/ is looked through for more than "is a new .resources here".
        # A staging file falls through every other raster in this method: it
        # rightly does not count as a new archive, it is not in the backup
        # directory, and self.names never names it -- so a whole archive,
        # measured at 589 KB on the bench and up to 702 MB on gameresources,
        # sat there with nlink == 1 while verify() said "clean". Since
        # _restore drops these unconditionally this is only diagnosis, but
        # without it nobody ever learns the disk went.
        for stray in (self._strays() if strays else ()):
            problems.append(
                "Leftover from an interrupted rebuild: %s (%s bytes). Reverting "
                "cleans it up, otherwise delete it by hand - the game does "
                "not need it." % (stray.name, format(stray.stat().st_size, ",")))
        stamps = self._journal_stamps()
        # Not _journal_stamps(): RESCUE speaks for a rebuild this module has
        # not finished taking back, so its journal has to be an unfinished
        # one -- and that is asked here rather than assumed. A *completed*
        # journal carries a stamp for every rebuild; if the live archive
        # matches none of them, somebody else wrote after us, and FOREIGN is
        # the right text. RESCUE there would have the user copy the
        # pre-update archive over the update's own and call it repaired.
        try:
            journal = self.read_journal() or {}
        except TncPatchError:
            journal = {}
        rebuilt = (set() if journal.get("complete")
                   else set(journal.get("rebuilt", ())))
        for name, info in sorted(known.items()):
            head = self.backup / (name + HEAD_SUFFIX)
            if not head.is_file():
                problems.append("Backup missing: %s" % name)
            elif head.stat().st_size != info["head_size"]:
                problems.append("Backup damaged (length): %s" % name)
            elif _sha256(head) != info["head_sha256"]:
                problems.append("Backup damaged (checksum): %s" % name)
            full = info.get("full")
            copy = self.backup / name
            rescuable = False
            if full:
                # Length only: the hash of a 702 MB copy is read where it
                # decides something, in _restore(), not on every status query.
                if not copy.is_file():
                    problems.append("Full copy missing: %s" % name)
                elif copy.stat().st_size != full["size"]:
                    problems.append("Full copy damaged (length): %s" % name)
                else:
                    rescuable = name in rebuilt
            if info.get("fremd"):
                problems.append(
                    "%s had payload bytes changed outside this program; that "
                    "could not be undone. Let Steam verify the game files, then "
                    "create the backup again." % name)
            live = self.base / name
            # Before the existence check, not after it. A rebuilt archive
            # that vanished -- Steam's file check, a scanner's quarantine --
            # has a hash-checked full copy behind it and _restore's
            # os.replace puts it back; reporting it here left revert()
            # refusing over the one thing it could repair, with the
            # installation staying patched. A missing *in-place* archive
            # never reaches skip, so it still gets the refusal it deserves.
            if name in skip:
                continue
            if not live.is_file():
                problems.append("Archive missing: %s" % name)
                continue
            # Every state this module is known to have left behind, each one a
            # measured pair and not a blanket exemption: what the manifest last
            # recorded, the pristine state, and what a completed journal
            # vouches for. Anything else is somebody else's work.
            left_by_us = {(info["last_size"], info["last_head_sha256"]),
                          (info.get("orig_size"), info["head_sha256"])}
            vouched = stamps.get(name)
            if vouched:
                left_by_us.add((vouched["last_size"], vouched["last_head_sha256"]))
            if (live.stat().st_size,
                    _sha256(live, info["head_size"])) not in left_by_us:
                problems.append(RESCUE % (name, copy, live) if rescuable
                                else FOREIGN % name)
        return problems + self._texdb_problems(journal)

    def _texdb_problems(self, journal):
        """A .texdb the journal wrote into that changed under us, or a lost backup.

        apply() changes a .texdb's table only in the rows it repointed and its
        size only up to the end it appended to (see the module head); anything
        else moving is somebody else's work -- a game update -- and writing the
        old blocks back into the new file would corrupt it. The windows and
        the appended tail are not compared: a revert that died halfway leaves
        some of them old and some new, and _restore() puts all of them back
        regardless.
        """
        out = []
        appends = journal.get("texdb_append") or {}
        for name, stamp in sorted((journal.get("texdb_stamps") or {}).items()):
            path = self.base / name
            app = appends.get(name) or {"end": stamp["size"], "rows": ()}
            if not path.is_file():
                # A cache that is gone leaves nothing to overwrite wrongly --
                # see the module head. A .texdb is part of the game.
                if not name.endswith(CACHE_NAME):
                    out.append("TexDB missing: %s" % name)
            elif (not stamp["size"] <= path.stat().st_size <= app["end"]
                  or _texdb_table_sha(path, app["rows"]) != stamp["table_sha256"]):
                out.append(FOREIGN % name)
        # A file written as a new one comes back from its backup name. That
        # name must still be the original: gone is fine only once the live
        # name is the original again (a revert that died after moving it).
        for name, info in sorted((journal.get("texdb_relinked") or {}).items()):
            live, keep = self.base / name, self.backup / info["link"]
            if keep.is_file():
                if keep.stat().st_ino != info["ino"]:
                    out.append("Backup damaged (no longer the original): %s" % keep)
            elif live.is_file() and live.stat().st_ino != info["ino"]:
                out.append("Backup missing: %s (original of %s)" % (keep, name))
        # A whole file: the live name holds the original or ours, nothing else.
        for name, info in sorted((journal.get("files") or {}).items()):
            live, keep = self.base / name, self.backup / info["link"]
            now = live.stat().st_ino if live.is_file() else None
            if keep.is_file() and keep.stat().st_ino != info["ino"]:
                out.append("Backup damaged (no longer the original): %s" % keep)
            elif not keep.is_file() and now != info["ino"]:
                out.append("Backup missing: %s (original of %s)" % (keep, name))
            elif now not in (None, info["ino"], info.get("new")):
                out.append(FOREIGN % name)
        if journal.get("texdb"):
            path = self.backup / TEXDB_BACKUP
            if not path.is_file() or path.stat().st_size != (journal.get("texdb_backup") or {}).get("size"):
                out.append("TexDB backup missing or damaged: %s" % path)
        return out

    # -- apply / revert ----------------------------------------------------

    def _plan(self, entry, data, gap):
        """(window, csize, usize, compression) for an in-place write, or None.

        The whole window -- csize plus the gap behind it -- is rewritten, so
        whatever follows the new payload goes back to the zero fill that
        retail has there rather than keeping a tail of the old payload.
        """
        budget = entry.csize + gap
        raw, usize, comp = pack_payload(entry, data, self.oodle)
        if len(raw) > budget:
            if comp != COMP_STORED:
                return None
            # 31125 of 178077 retail entries are stored. When one outgrows its
            # window, Kraken buys back roughly half on anything above ~150 B --
            # cheaper than rebuilding a 700 MB archive for one decl.
            packed = self.oodle.compress(data)
            if len(packed) > budget or len(packed) >= len(data):
                return None
            raw, comp = packed, COMP_KRAKEN
        return raw + b"\0" * (budget - len(raw)), len(raw), usize, comp

    def whole_files(self, payloads):
        """({journal name: (live path, fill(path))}, written, missing) for the video and
        sound payloads: each file they replace whole and how its new content is written.
        A sound that cannot be replaced is a TncPatchError before anything is written."""
        files, written, missing, edits = {}, [], [], {}
        for (type_, name), data in sorted(payloads.items()):
            if type_ not in WHOLE:
                continue
            key = "%s:%s" % (type_, name)
            if not self.find(type_, name):
                missing.append(key)
                continue
            written.append(key)
            if type_ == VIDEO:
                path = self._video(name)
                files[self.rel(path)] = (path, lambda p, d=data: p.write_bytes(d))
            else:
                edits[name] = data
        if edits:
            try:
                plan = wwise.replace_plan(self.base.parent, edits)
            except (wwise.WwiseError, IdclError, struct.error) as exc:
                raise TncPatchError("Sound not replaceable: %s" % exc)
            for pack, changes in plan.items():
                files[self.rel(pack)] = (pack, lambda p, s=pack, c=changes: _rebuild_pack(s, p, c))
        return files, written, missing

    def apply(self, payloads, mod_ids=()):
        """Write `payloads` ({(type, name): bytes}) into every copy of each asset.

        Reverts first when a journal exists, so applying twice in a row lands
        on the same bytes as applying once from pristine, and the on-disk
        state always matches the journal.
        """
        with self._lock(), _as_held():
            return self._apply(payloads, mod_ids)

    def _apply(self, payloads, mod_ids):
        if self.is_modified():
            self.revert()
        # A stage left by a run whose swap failed would stop os.link below
        # with FileExistsError; gone first, it costs nothing.
        self._sweep()
        # Before verify(), not after: a full copy that nothing claims is not
        # a problem to report, it is rubbish to sweep up -- and reporting it
        # is what used to block the next run.
        self._drop_orphan_fulls()
        if self.read_manifest() is None:
            self.ensure_backup()
        else:
            problems = self.verify(strays=False)
            if problems:
                raise TncPatchError("Backup unusable:\n  " + "\n  ".join(problems))

        written, missing, unsupported = [], [], []
        in_place, rebuilds, gaps = {}, {}, {}
        # .texdb blocks: every row of the key, in every .texdb and every map
        # cache. One that fits goes into its own window; a longer one is
        # appended to that file and the row repointed (see the module head).
        # appends[file] is what the journal records, tails[file] the bytes,
        # in the same order. A file with more than one name goes to relink:
        # it is written as a new file, never through the link.
        texdb_slots, texdb_blocks, appends, tails, relink = [], [], {}, {}, {}
        for (type_, name), data in payloads.items():
            if type_ != TEXDB:
                continue
            hits = self.texdb_hits(name)
            if not hits:
                missing.append("%s:%s" % (type_, name))
                continue
            for hit in hits:
                rel = self.rel(hit["path"])
                st = hit["path"].stat()
                if st.st_nlink > 1 and rel not in relink:
                    relink[rel] = {"link": rel.replace("../", "").replace("/", "_") + LINK_SUFFIX,
                                   "ino": st.st_ino}
                if len(data) <= hit["size"]:
                    texdb_slots.append(dict(hit, file=rel))
                    texdb_blocks.append(bytes(data))
                    continue
                app = appends.setdefault(rel, {"size": st.st_size, "end": st.st_size, "rows": []})
                app["rows"].append([hit["row"], hit["offset"], app["end"]])
                app["end"] += len(data)
                tails.setdefault(rel, []).append(bytes(data))
            written.append("%s:%s" % (type_, name))
        # The windows journalled and backed up are the in-place ones; a
        # relinked file keeps its whole original under its backup name.
        in_texdb = [(s, b) for s, b in zip(texdb_slots, texdb_blocks) if s["file"] not in relink]
        # Videos and sounds: whole files, original kept under a second name.
        whole, got, gone = self.whole_files(payloads)
        written += got
        missing += gone
        files = {n: {"link": n.replace("../", "").replace("/", "_") + LINK_SUFFIX, "ino": p.stat().st_ino}
                 for n, (p, _fill) in whole.items()}
        for (type_, name), data in payloads.items():
            if type_ == TEXDB or type_ in WHOLE:
                continue
            hits = self.find(type_, name)
            if not hits:
                missing.append("%s:%s" % (type_, name))
                continue
            for archive, entry in hits:
                if entry.compression == COMP_KRAKEN_BLOCKS:
                    # The 12-byte prefix of mode 4 is not understood; writing a
                    # header we cannot read back is worse than not writing.
                    unsupported.append("%s:%s@%s" % (type_, name, archive.path.name))
                    continue
                if archive not in gaps:
                    gaps[archive] = gaps_of(archive)
                plan = self._plan(entry, data, gaps[archive][entry.index])
                if plan is None:
                    rebuilds.setdefault(archive, {})[entry] = data
                else:
                    in_place.setdefault(archive, []).append((entry, data, plan))
            written.append("%s:%s" % (type_, name))

        # One archive is either written in place or rebuilt, never both: a
        # rebuild moves every offset in it, which would invalidate the slot
        # records the in-place writes journalled.
        for archive in list(in_place):
            if archive in rebuilds:
                for entry, data, _plan in in_place.pop(archive):
                    rebuilds[archive].setdefault(entry, data)

        journal = {
            "applied": time.time(),
            "mods": list(mod_ids),
            "assets": sorted(written),
            "missing": sorted(missing),
            "unsupported": sorted(unsupported),
            "archives": sorted(a.path.name for a in list(in_place) + list(rebuilds)),
            "rebuilt": sorted(a.path.name for a in rebuilds),
            "slots": [{"archive": a.path.name,
                       "offset": e.offset,
                       # Where _write_slots reaches into the entry table for
                       # this entry. It is the only place apply() changes the
                       # head, and revert() needs to know it to tell its own
                       # half-finished writes from somebody else's change --
                       # see _only_our_head_writes.
                       "field": a.header.addr_entries + e.index * ENTRY_SIZE,
                       "data": base64.b64encode(
                           _read_at(a.path, e.offset, len(plan[0]))).decode("ascii")}
                      for a, items in in_place.items() for e, _d, plan in items],
            # The whole archive as we found it, for the archives whose payload
            # bytes nothing else in the backup covers. revert() compares
            # against this and reports what it could not undo. A rebuilt
            # archive is left out: its full copy is the stronger record, and
            # hashing 702 MB to compare a copy with itself buys nothing.
            "pristine": {a.path.name: _sha256(a.path) for a in in_place},
            # Set, so _only_our_head_writes knows +0x08/+0x20 are ours too.
            "header_hashes": True,
            # Every in-place .texdb/cache window, in the order of their bytes
            # in TEXDB_BACKUP. "file" is the path from base/ (rel()).
            "texdb": [{"file": s["file"], "offset": s["offset"], "size": s["size"]}
                      for s, _b in in_texdb],
            # Per file: original size, the end apply() appends up to, and
            # every repointed row as [position, old offset, new offset].
            "texdb_append": appends,
            # Original size and table hash of every file touched either way;
            # revert() refuses when they moved other than by our own rows.
            "texdb_stamps": {n: {"size": (self.base / n).stat().st_size,
                                 "table_sha256": _texdb_table_sha(self.base / n)}
                             for n in sorted({s["file"] for s in texdb_slots} | set(appends))},
            # Files with more than one name, written as new files: the backup
            # name that holds the original, and the original's file id.
            "texdb_relinked": relink,
            # Videos and sound packs replaced whole, the same way; "new" is the
            # new file's id, recorded before it is moved onto the live name.
            "files": files,
            # Filled as the rebuilds land and again, for every archive, in the
            # atomic completion write below. Only the completed set counts as
            # a vouched stamp for verify(); the ones written mid-run exist so
            # that _resumable() has something to measure a rebuilt archive
            # against instead of waving it through.
            "stamps": {},
            "complete": False,
        }

        # Last thing before the first write. Our own handles go first, or
        # the exclusive probe finds us; everything below opens by path.
        self.close()
        # Scratch names this run and its rollback write and then move: the
        # json/texdb.orig temporaries, idcl's build file under the stage, and
        # the one _restore copies a full copy back over.
        linked = dict(relink, **files)
        scratch = ([self.backup / (n + ".tmp") for n in (MANIFEST_NAME, JOURNAL_NAME, TEXDB_BACKUP)]
                   + [self.base / (a.path.name + s) for a in rebuilds
                      for s in (TMP_SUFFIX, STAGE_SUFFIX + TMP_SUFFIX)]
                   + [self.base / (n + NEW_SUFFIX) for n in linked])
        _refuse_held([(a.path, REPLACE) for a in rebuilds]
                     + [(self.base / n, REPLACE) for n in linked]
                     # The manifest is replaced by every run; the rest is only
                     # there when something went wrong before -- a full copy
                     # or .orig name a held file kept from being swept.
                     + [(p, REPLACE) for p in [self.manifest_path, self.journal_path]
                        + [self.backup / TEXDB_BACKUP] * bool(in_texdb)
                        + [self.backup / a.path.name for a in rebuilds] if p.is_file()]
                     + [(p, REMOVE) for p in (self.backup / i["link"] for i in linked.values())
                        if p.is_file()]
                     # A stage the sweep above could not delete: os.link needs the name.
                     + [(p, REMOVE) for p in (a.path.with_name(a.path.name + STAGE_SUFFIX)
                                              for a in rebuilds) if p.is_file()]
                     + [(p, OVERWRITE) for p in scratch if p.is_file()]
                     + [(a.path, WRITE) for a in in_place]
                     + [(self.base / n, WRITE)
                        for n in sorted({s["file"] for s, _b in in_texdb} | set(appends))
                        if n not in relink])
        manifest = self.read_manifest()
        for archive in rebuilds:
            self._full_copy(archive.path, manifest["archives"][archive.path.name])
        if rebuilds:
            _write_json(self.manifest_path, manifest)
        # The original .texdb windows, before the journal that names them:
        # a crash in between leaves an unused file the next apply replaces.
        if in_texdb:
            orig = b"".join(_read_at(s["path"], s["offset"], s["size"]) for s, _b in in_texdb)
            tmp = self.backup / (TEXDB_BACKUP + ".tmp")
            tmp.write_bytes(orig)
            _swap(tmp, self.backup / TEXDB_BACKUP)
            journal["texdb_backup"] = {"size": len(orig),
                                       "sha256": _sha256(self.backup / TEXDB_BACKUP)}
        # On disk before the first byte of payload goes out: a crash at any
        # point from here on leaves a journal that describes everything that
        # could possibly have been written, so revert() can finish the job.
        _write_json(self.journal_path, journal)
        try:
            # Archives first, the .texdb side last, and _restore takes them
            # back the other way round (see the module head): a .texdb is
            # patched only while every archive of this run is, so no failing
            # step -- above all a swap refused by a handle no probe sees --
            # leaves texture blocks patched beside an original archive.
            for archive, changes in rebuilds.items():
                # Archive.apply() swaps its rebuild onto the name it was
                # opened under, so the stamp behind it can only ever describe
                # an archive that is already live. That stretch is not the
                # two instructions it reads like: the first journal write
                # names all N rebuilds at once and the stamps arrive one per
                # iteration, so every archive still ahead in this loop sat
                # there unstamped -- holding bytes nothing on disk accounts
                # for -- for the length of every rebuild before it. A
                # rebuild of gameresources.resources measures 0.55 s here
                # (702 MB at 1.3 GB/s, NVMe, warm), so a mod that rebuilds
                # all 24 archives held the last of them unstamped for
                # seconds, not for the two instructions it reads like -- and
                # _resumable() waved every one of them through on the
                # strength of the missing stamp alone.
                #
                # So the rebuild is handed a second name for the same bytes
                # and runs there, and the stamp and the journal go out before
                # that name is moved onto the live one. os.link rather than a
                # copy, because the archive can be 702 MB and a second name
                # for it costs nothing. The live name then only ever holds
                # the pristine archive or a rebuild the journal has already
                # described, and a missing stamp means "untouched" instead of
                # "unknown".
                name = archive.path.name
                live = archive.path
                stage = live.with_name(name + STAGE_SUFFIX)
                # Closed before the link exists: Windows refuses to replace
                # any name of a file that still has a handle open on it, and
                # after os.link the two names are one file.
                archive.close()
                try:
                    os.link(live, stage)
                except FileExistsError:
                    # A stage that appeared after the sweep and the check
                    # above -- a race, nothing else. Already says exactly
                    # that, and the rollback below sweeps it.
                    raise
                except OSError as exc:
                    # Hard links are an NTFS feature. On exFAT, on a network
                    # share or through a reparse point the call comes back as
                    # PermissionError(1, 'Incorrect function') -- true, and
                    # useless to whoever is looking at it. The rollback below
                    # is unaffected; only the sentence changes.
                    raise TncLinkError(
                        "%s has to be rebuilt, but the file system under %s "
                        "cannot create hard links (%s). Move the game to an "
                        "NTFS drive - on exFAT or a network drive, no mod "
                        "that rebuilds an archive can work."
                        % (name, self.base, exc)) from exc
                staged = Archive(stage)
                try:
                    # Re-keyed, because Archive._own() rejects an entry that
                    # came out of a different object. rebuild() keeps the
                    # table order, so the index still names the same entry.
                    staged.apply({staged.entries[e.index]: d
                                  for e, d in changes.items()}, self.oodle)
                finally:
                    staged.close()
                # A rebuild moves offsets, so its table hash is stale as well.
                _refresh_header_hashes(stage)
                head_size = manifest["archives"][name]["head_size"]
                journal["stamps"][name] = {
                    "head_size": head_size,
                    "last_size": stage.stat().st_size,
                    "last_head_sha256": _sha256(stage, head_size)}
                _write_json(self.journal_path, journal)
                _swap(stage, live)
            for archive, items in in_place.items():
                _write_slots(archive, items)
                _refresh_header_hashes(archive.path)
            _write_texdb([s for s, _b in in_texdb], [b for _s, b in in_texdb])
            for n, app in appends.items():
                if n not in relink:
                    _append_texdb(self.base / n, app["size"], tails[n], app["rows"])
            for n, info in relink.items():
                mine = [(s, b) for s, b in zip(texdb_slots, texdb_blocks) if s["file"] == n]
                _write_new(self.base / n, self.backup / info["link"], [s for s, _b in mine],
                           [b for _s, b in mine], appends.get(n), tails.get(n))
            for n, (live, fill) in whole.items():
                new = _write_whole(live, self.backup / files[n]["link"], fill)
                files[n]["new"] = new.stat().st_ino
                _write_json(self.journal_path, journal)
                _swap(new, live)
        except Exception:
            # Straight to _restore: the state was verified clean before the
            # first write, so nothing here needs re-checking -- and checking
            # would fail, because the deviation it would find is ours.
            self.close()
            self._restore(journal)
            _patch._retry(self.journal_path.unlink, missing_ok=True)
            self._drop_texdb_backup()
            self._stamp_all()
            _forget_sounds()
            raise
        self.close()
        _forget_sounds()
        # One atomic write closes the window that cost part one three repairs:
        # the journal is marked complete only together with the stamps of what
        # it just wrote, so if the manifest below never gets refreshed, the
        # journal still says what the archives are supposed to look like.
        arch = manifest["archives"]
        journal["stamps"] = {n: self._stamp({"head_size": arch[n]["head_size"]}, n)
                             for n in journal["archives"]}
        journal["complete"] = True
        _write_json(self.journal_path, journal)
        self._stamp_all()
        return journal

    def _full_copy(self, path, info):
        """Copy one archive whole into the backup and record it in `info`.

        Via a stump and os.replace, so the copy under its real name is either
        absent or complete. A disk that fills up mid-copy leaves the stump,
        and verify() refuses on it instead of the next run silently trusting
        half a backup.
        """
        dst = self.backup / path.name
        part = dst.with_name(dst.stem + PART_INFIX + dst.suffix)
        shutil.copy2(path, part)
        _swap(part, dst)
        info["full"] = {"size": dst.stat().st_size, "sha256": _sha256(dst)}

    def _restore(self, journal):
        """Undo what `journal` describes. Unconditional and repeatable.

        Every step is idempotent, so a crash in the middle of a revert is
        undone by running it again. What makes that safe is a *positive*
        record: a name leaves journal["rebuilt"] only once its archive is
        back on disk. Reading a missing manifest record as "already done"
        was the other way round -- it made a rebuilt archive that nothing
        had restored look finished, and revert() then reported success
        while the installation stayed patched.

        The .texdb side goes back first and the archives after it, the
        reverse of apply(): whichever step fails, a .texdb is patched only
        while every archive still is (see the module head).
        """
        manifest = self.read_manifest() or {"archives": {}}
        # Every original is checked whole before anything goes back: a
        # damaged copy written over the live file makes the loss permanent,
        # and refusing halfway would leave one part reverted and the other
        # not.
        texdb = journal.get("texdb") or ()
        orig = b""
        if texdb:
            info = journal.get("texdb_backup") or {}
            path = self.backup / TEXDB_BACKUP
            if (not path.is_file() or path.stat().st_size != info.get("size")
                    or _sha256(path) != info.get("sha256")):
                raise TncPatchError(
                    "The TexDB backup %s is missing or damaged. Get the "
                    "affected .texdb files back with Steam's \"Verify "
                    "integrity of game files\"." % path)
            orig = path.read_bytes()
        rebuilt = set(journal.get("rebuilt", ()))
        for name in sorted(rebuilt):
            full = manifest["archives"].get(name, {}).get("full")
            copy = self.backup / name
            # The archive was rebuilt, nothing else holds its payloads, and
            # going quiet here is what makes the loss permanent and invisible.
            if not full or not copy.is_file():
                raise TncPatchError(
                    "%s was rebuilt, but the full copy %s is missing. The "
                    "archive cannot be restored without it - get it back "
                    "with Steam's \"Verify integrity of game files\"."
                    % (name, copy))
            if (copy.stat().st_size != full["size"]
                    or _sha256(copy) != full["sha256"]):
                raise TncPatchError(
                    "The full copy %s does not match the backup "
                    "(%s instead of %s bytes). It will not be restored; "
                    "get %s back with Steam's \"Verify integrity of game "
                    "files\"."
                    % (copy, format(copy.stat().st_size, ","), format(full["size"], ","), name))
        # Ahead of the copies below, not behind them. That loop used to
        # raise on a full copy that is missing or does not match, and
        # everything behind the raise was unreachable for good: the staging
        # file stayed at full archive size -- 702 MB for gameresources -- and
        # every later revert() died at the same instruction without ever
        # reaching it. Nothing here depends on the restore having happened,
        # unlinking a stray is idempotent, and it frees exactly the space the
        # copy below is about to ask for. The unlink itself is suppressed:
        # on Windows a scanner or a backup tool holding the file open makes
        # it fail, and a housekeeping error must not replace the real one.
        # Nothing depends on the stray being gone; the next run takes it.
        self._sweep()
        # A file that is gone is skipped: verify() refuses a missing .texdb
        # before this runs, and a cache somebody removed has nothing left to
        # take back.
        relinked = journal.get("texdb_relinked") or {}
        pos = 0
        for slot in texdb:
            path = self.base / slot["file"]
            if slot["file"] not in relinked and path.is_file():
                with _open_rw(path) as fh:
                    fh.seek(slot["offset"])
                    fh.write(orig[pos:pos + slot["size"]])
            pos += slot["size"]
        # Rows back before the tail goes: no row ever points past the end.
        for name, app in sorted((journal.get("texdb_append") or {}).items()):
            path = self.base / name
            if name in relinked or not path.is_file():
                continue
            with _open_rw(path) as fh:
                for row, old, _new in app["rows"]:
                    fh.seek(row)
                    fh.write(struct.pack("<Q", old))
                fh.truncate(app["size"])
        # A file written as a new one -- relinked cache or whole video/sound
        # file: its backup name goes back onto the live name -- original
        # content, file id and link count in one move.
        # Each step is idempotent: once the backup name is gone, it is back.
        for name, info in sorted(dict(relinked, **(journal.get("files") or {})).items()):
            live, keep = self.base / name, self.backup / info["link"]
            with contextlib.suppress(OSError):
                live.with_name(live.name + NEW_SUFFIX).unlink()
            if not keep.is_file():
                continue
            if keep.stat().st_ino != info["ino"]:
                # Not the original but a leftover _write_new could not
                # delete, so it never touched the live name. Moved over it,
                # this would destroy the file it is meant to restore.
                with contextlib.suppress(OSError):
                    _patch._retry(keep.unlink)
                continue
            if not live.is_file() or os.path.samefile(live, keep):
                _patch._retry(keep.unlink)      # removed by the user, or never replaced
            else:
                _swap(keep, live)
        for name in journal.get("archives", ()):
            if name in rebuilt:
                continue  # the full copy puts the head back
            head = self.backup / (name + HEAD_SUFFIX)
            live = self.base / name
            if head.is_file() and live.is_file():
                with _open_rw(live) as fh:
                    fh.write(head.read_bytes())
        for slot in journal.get("slots", ()):
            if slot["archive"] in rebuilt:
                continue
            with _open_rw(self.base / slot["archive"]) as fh:
                fh.seek(slot["offset"])
                fh.write(base64.b64decode(slot["data"]))
        for name in sorted(rebuilt):
            info = manifest["archives"][name]
            copy = self.backup / name
            # Over a stump and os.replace, exactly the way _full_copy takes
            # it: a copy2 straight onto the live archive leaves half of a
            # 702 MB file lying there when the disk fills up, and the next
            # run would find an archive that is neither patched nor original.
            # Not at all when the live archive already is the copy: that is
            # what a swap that failed -- because somebody holds the file --
            # leaves behind, and replacing it would fail on the same handle.
            if not _is_copy(self.base / name, info["full"]):
                tmp = self.base / (name + TMP_SUFFIX)
                shutil.copy2(copy, tmp)
                _swap(tmp, self.base / name)
            # Struck from the journal only now, and the manifest record
            # before the file it points at: every crash point in this block
            # repeats a step instead of skipping one.
            journal["rebuilt"] = [n for n in journal["rebuilt"] if n != name]
            _write_json(self.journal_path, journal)
            del info["full"]
            _write_json(self.manifest_path, manifest)
            # No record names the copy any more, so a scanner still reading
            # the fresh 702 MB is no reason to stop the restore halfway:
            # _drop_orphan_fulls() takes it on the next run.
            with contextlib.suppress(OSError):
                _patch._retry(copy.unlink)

    def _restore_targets(self, journal):
        """(path, kind): every existing file revert() would replace, remove or write."""
        known = (self.read_manifest() or {"archives": {}})["archives"]
        rebuilt = sorted(journal.get("rebuilt", ()))
        relinked = journal.get("texdb_relinked") or {}
        out = []
        for n in rebuilt:
            if not _is_copy(self.base / n, (known.get(n) or {}).get("full")):
                # Put back over a scratch name, which a restore whose swap
                # failed can leave behind -- and the sweep, if it is held.
                out += [(self.base / n, REPLACE), (self.base / (n + TMP_SUFFIX), OVERWRITE)]
        out += [(self.backup / n, REMOVE) for n in rebuilt]         # the full copies
        for name, info in sorted(dict(relinked, **(journal.get("files") or {})).items()):
            live, keep = self.base / name, self.backup / info["link"]
            # A second name that is still the live file only gets unlinked,
            # and unlinking another name works while the file is held.
            if live.is_file() and keep.is_file() and not os.path.samefile(live, keep):
                out.append((live, REPLACE))
            out.append((keep, REMOVE))
        # The journal is rewritten per rebuilt archive, else only removed.
        out += [(self.journal_path, REPLACE if rebuilt else REMOVE), (self.manifest_path, REPLACE),
                (self.backup / TEXDB_BACKUP, REMOVE)]
        out += [(self.backup / (n + ".tmp"), OVERWRITE)
                for n in [MANIFEST_NAME] + [JOURNAL_NAME] * bool(rebuilt)]
        write = [n for n in journal.get("archives", ()) if n not in rebuilt]
        write += [s["file"] for s in journal.get("texdb") or () if s["file"] not in relinked]
        write += [n for n in journal.get("texdb_append") or {} if n not in relinked]
        out += [(self.base / n, WRITE) for n in dict.fromkeys(write)]
        return [(p, k) for p, k in out if p.is_file()]

    def _drop_texdb_backup(self):
        """Remove TEXDB_BACKUP once no journal names it; a held one stays.

        Nothing reads it without a journal, and the next apply() that needs
        it replaces it -- so a scanner reading it is no reason to report a
        finished revert as failed.
        """
        with contextlib.suppress(OSError):
            _patch._retry((self.backup / TEXDB_BACKUP).unlink, missing_ok=True)

    def _only_our_head_writes(self, name, journal, info):
        """True when this archive's head deviates only where apply() wrote.

        The state a torn revert leaves behind -- pristine up to the byte the
        write died on, patched after it -- matches none of the three stamps,
        and refusing there is what made an interrupted revert unrepeatable
        and sent the user off to delete the only copy of an archive.

        It is still telling apart from somebody else's change, because what
        this module puts into a head is known to the byte: csize, usize and
        the compression flag of the entries the journal names, and nothing
        else. Outside those windows the head has to be the pristine head,
        byte for byte; where it is not, a game update was here and writing
        the old entry table back over new payloads would be the end of that
        archive. A journal from an older version carries no windows, so it
        gets the strict answer.
        """
        live = self.base / name
        head = self.backup / (name + HEAD_SUFFIX)
        # An in-place write never changes the length -- it goes into a window
        # the file already owns. A different length is therefore not a
        # half-finished write of ours, and it also makes the read below unsafe.
        if not head.is_file() or live.stat().st_size != info.get("orig_size"):
            return False
        pristine = bytearray(head.read_bytes())
        current = bytearray(_read_at(live, 0, len(pristine)))
        ours = False
        for slot in journal.get("slots", ()):
            if slot["archive"] != name or "field" not in slot:
                continue
            ours = True
            for lo, hi in ((0x40, 0x50), (0x70, 0x71)):   # csize+usize, comp
                pristine[slot["field"] + lo:slot["field"] + hi] = b"\0" * (hi - lo)
                current[slot["field"] + lo:slot["field"] + hi] = b"\0" * (hi - lo)
        if ours and journal.get("header_hashes"):
            for lo, hi in HEADER_HASHES:
                pristine[lo:hi] = current[lo:hi] = b"\0" * (hi - lo)
        return pristine == current

    def _resumable(self, journal):
        """Archives whose current state says nothing, because _restore replaces it.

        Loosened per archive and per reason, never as "a journal exists".

        A rebuilt archive is the asymmetric case and used to be waved through
        wholesale, on the grounds that _restore overwrites it from a
        hash-checked copy anyway. That holds for a copy of ours that died
        halfway; it does not hold for a *foreign* version. Overwriting one
        with the full copy replaces somebody else's file with the one we
        took before the rebuild and then stamps the result as clean, while
        the very same substitution on an archive written in place is refused.
        So the question asked here is measured, from the record apply() wrote
        the moment each rebuild landed: the live archive is let through when
        it still matches that stamp, and when it is gone altogether, because
        _restore's os.replace creates it from the copy. Anything else -- a
        different size, a different head, plain rubbish -- gets the refusal.

        An archive without a stamp used to keep a blanket exemption, on the
        grounds that it was the one whose rebuild had landed and whose
        journal write had not -- two instructions wide. It was not: the first
        journal write names all N planned rebuilds at once and the stamps
        arrive one at a time behind each rebuild, so *every archive still
        ahead in that loop* was unstamped, for as long as the rebuilds
        before it took. _apply() now rebuilds under a staging name and stamps
        and journals the result before moving it into place, which makes a
        missing stamp mean something measurable instead: the live archive is
        still the one the full copy was taken from. So the question here is a
        hash either way -- the stamp, or that copy. Anything else is somebody
        else's file, and overwriting it from the copy destroys it. The one
        journal this cannot speak for is one an older version of this module
        left behind, where a rebuild landed and its stamp did not; there the
        refusal is wrong, and revert() says so instead of guessing.

        An archive written in place is exempt only where every deviation is
        one of our own head writes. Everything the journal does not name
        keeps the full check.
        """
        known = (self.read_manifest() or {"archives": {}})["archives"]
        stamps = journal.get("stamps") or {}
        skip = set()
        for name in journal.get("rebuilt", ()):
            live = self.base / name
            if not live.is_file():
                skip.add(name)      # _restore's os.replace creates it
                continue
            size = live.stat().st_size
            stamp = stamps.get(name)
            if (stamp and size == stamp["last_size"]
                    and _sha256(live, stamp["head_size"])
                    == stamp["last_head_sha256"]):
                skip.add(name)
                continue
            # No stamp, or a rebuild whose os.replace never ran: then the
            # only state that is ours is the one the full copy was taken
            # from. Whole file, not the head -- the copy is the pristine
            # archive and the head alone would miss a payload-only change.
            # Paid for once, in revert()'s second pass and only after the
            # strict one has already found something.
            full = (known.get(name) or {}).get("full")
            if full and size == full["size"] and _sha256(live) == full["sha256"]:
                skip.add(name)
        for name in journal.get("archives", ()):
            info = known.get(name)
            if name in skip or not info or not (self.base / name).is_file():
                continue
            if self._only_our_head_writes(name, journal, info):
                skip.add(name)
        return skip

    def revert(self):
        """Restore the installation to its pristine state. True if it did anything.

        Refuses on any problem it cannot attribute to itself: writing pristine
        entry tables over payloads that a game update replaced produces offsets
        pointing at the wrong bytes, with no way back.

        An installation nobody ever backed up is refused *before* the lock,
        because taking the lock is itself a write: mkdir plus O_CREAT put
        base/_wolfsdk_tnc/lock into a game directory this module has never
        touched, and the very next line then says there is nothing to undo.
        Empty folder, empty file, no damage -- and still exactly the thing
        this module promises not to do.

        The unlocked look is safe because of what it can and cannot decide.
        It never grants anything: everything past it, including the same
        question asked again, still runs under the lock, so two runs can no
        more meet here than before. It can only be stale in one direction --
        a backup appearing between the look and the lock -- and the whole
        cost of that is the refusal below for a backup that now exists,
        repeated by pressing the button again. A check whose worst outcome is
        "the lock gets taken after all" would be the mirror case and equally
        harmless; one whose worst outcome is "two runs proceed" would not be,
        and this is not that check, because it decides nothing but the
        refusal it is standing in front of.
        """
        if self.read_manifest() is None:
            raise TncPatchError(NO_BACKUP)
        with self._lock(), _as_held():
            # Binding, and not the same question twice: the backup can be
            # gone again by the time the lock is here, and only this answer
            # was taken under exclusion.
            if self.read_manifest() is None:
                raise TncPatchError(NO_BACKUP)
            journal = self.read_journal()
            if journal is None:
                # Nothing to take back means nothing to protect. Refusing
                # here -- over a 'fremd' mark an earlier revert left, say --
                # only locks the user out of an installation that is already
                # in the state this call would leave it in.
                return False
            # Before the gate below, because the gate is a refusal often
            # enough -- a full copy somebody deleted, say -- and _restore,
            # which is where the strays would otherwise be dropped, never
            # runs then. That left a whole staged archive lying in base/
            # after every refused revert, invisible and permanent. Dropping
            # it is safe whichever way the gate goes: a stray is either a
            # second name for the live archive (os.link, unlinking one name
            # keeps the other) or a rebuild the full copy already covers.
            # Suppressed, because on Windows anybody holding the file open --
            # a scanner reading the 702 MB stray, a backup tool, an Explorer
            # preview -- makes unlink fail, and that raised before verify(),
            # _resumable() or _restore() ever ran: the whole revert died on
            # housekeeping and the installation stayed patched. Not sweeping
            # costs nothing but the space; not reverting costs the user the
            # only way back.
            self._sweep()
            # strays=False, because the sweep just ran: whatever is still
            # there is a file that could not be deleted, and refusing over it
            # would be a message blocking the very operation that fixes the
            # state it complains about. Measured: with the report on, a held
            # handle on an unrelated .idcl_tmp left every archive patched.
            # The stray is still reported by a plain verify() -- a status run
            # names it, a rescue run does not stumble over it.
            problems = self.verify(strays=False)
            if problems:
                # Second pass, and only now, so the normal run pays nothing:
                # a revert that died halfway leaves archives in a state no
                # stamp covers, and those are exactly the ones _restore is
                # about to overwrite anyway.
                problems = self.verify(skip=self._resumable(journal),
                                       strays=False)
            if problems:
                raise TncPatchError("Backup unusable:\n  " + "\n  ".join(problems))
            self.close()
            # A revert stopped halfway by a held file used to leave some
            # archives back and the .texdb blocks patched. Asked first, it
            # stays applied and consistent until the file is free.
            _refuse_held(self._restore_targets(journal))
            self._restore(journal)
            _forget_sounds()        # packs moved back: every cached offset is stale
            # What this module wrote is back; whether the archive as a whole is
            # back is a different question, and this is the only place that can
            # still answer it. A payload byte somebody else changed survives
            # the restore -- it must not survive the next stamp unnoticed.
            foreign = [n for n, digest in (journal.get("pristine") or {}).items()
                       if (self.base / n).is_file() and _sha256(self.base / n) != digest]
            _patch._retry(self.journal_path.unlink)
            self._drop_texdb_backup()
            self._stamp_all()
            if foreign:
                self._mark_foreign(foreign)
            return True
