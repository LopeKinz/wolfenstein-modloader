"""Applying and reverting asset changes against the installed game.

Strategy
--------
Payloads are written *inside the slot the entry already occupies*, so offsets
never move. This is not an optimisation but a hard requirement: payloads sit
at 16-byte aligned offsets in ascending order, and the engine validates that
ordering. Appending to the end of the file instead makes the game abort with

    FATAL ERROR: idBackgroundLoader::BeginBackgroundLoads: FileTable is out of order

A slot holds the entry's compressed bytes plus up to 15 bytes of alignment
padding, which is usually enough headroom for an edited decl. When a
replacement genuinely does not fit, that one archive is rewritten end to end
in the original order, which keeps the invariant at the cost of copying the
file once.

Reverting therefore restores, in order: full copies of any rewritten
.resources, the original bytes of individually overwritten slots, and the
pristine .index files.

Applying always reverts first, then writes the complete mod set. That makes
apply idempotent and keeps the on-disk state matching the journal exactly.
"""

import base64
import contextlib
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

try:  # Windows in production, fcntl keeps the module testable elsewhere.
    import msvcrt
except ImportError:  # pragma: no cover - exercised on non-Windows hosts
    msvcrt = None
    import fcntl

from .resources import load_master, plan_in_place, Archive

JOURNAL_NAME = "journal.json"
LOCK_NAME = "lock"

# Seconds to wait before asking a file Windows calls "in use" again: a virus
# scanner looking at a file that was just written lets go within about that
# long. wolfsdk.tncpatch waits by the same list.
RETRY_DELAYS = (0.1, 0.2, 0.4, 0.8, 1.6)


class PatchError(Exception):
    pass


def _retry(fn, *args, **kw):
    """fn(*args, **kw), asked again while Windows says the file is in use.

    os.replace onto a name fails while *any* other handle is open on that
    file -- a scanner reading the journal that was just written is enough
    (measured, see tncpatch's module head). The last PermissionError goes
    through to the caller.
    """
    for delay in RETRY_DELAYS:
        try:
            return fn(*args, **kw)
        except PermissionError:
            time.sleep(delay)
    return fn(*args, **kw)


def _write_json(path, obj):
    """Write JSON so that a crash can never leave a half-parsed file behind.

    A truncated journal used to read as "no journal at all", i.e. as a pristine
    installation, and revert then restored only the indexes.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), "utf-8")
    _retry(os.replace, tmp, path)


def _sha256(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        read = 0
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            if limit is not None and read + len(chunk) > limit:
                chunk = chunk[: limit - read]
            h.update(chunk)
            read += len(chunk)
            if limit is not None and read >= limit:
                break
    return h.hexdigest()


class Installation:
    """The set of archives in base/, plus backup and journal handling."""

    def __init__(self, game):
        self.game = game
        self.base = Path(game.base)
        self.backup = Path(game.backup)
        self.pairs = load_master(self.base)
        self._archives = None
        self._lookup = None
        self._variants = None
        self.lock_path = self.backup / LOCK_NAME
        self._held = 0

    # -- exclusive access --------------------------------------------------

    @contextlib.contextmanager
    def _lock(self):
        """Exclude concurrent writers, while remaining reentrant per object."""
        if self._held:
            self._held += 1
            try:
                yield
            finally:
                self._held -= 1
            return

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT)
        try:
            try:
                if msvcrt is not None:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - Windows is the supported runtime
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise PatchError(
                    "Another operation is working on this installation right now "
                    "(%s). Wait for it to finish first." % self.lock_path)
            self._held = 1
            try:
                yield
            finally:
                self._held = 0
        finally:
            os.close(fd)

    # -- lazy archive access ----------------------------------------------

    @property
    def archives(self):
        if self._archives is None:
            self._archives = [Archive(self.base / i, self.base / r) for i, r in self.pairs]
        return self._archives

    def close(self):
        for a in self._archives or []:
            a.close()
        self._archives = None
        self._lookup = None
        self._variants = None

    @property
    def lookup(self):
        """(type, name) -> [(archive, entry), ...]

        An asset can appear in more than one chunk; every copy must be patched
        or the game may load whichever one it reaches first.

        Built alongside it: (type, path) -> [name, ...], the spellings an
        asset path appears under. Scanning the ~200k-key table per lookup
        instead would cost minutes over a full mod set.
        """
        if self._lookup is None:
            table, variants = {}, {}
            for a in self.archives:
                for e in a.entries:
                    key = (e.type, e.name)
                    if key not in table:
                        table[key] = []
                        path = self._path_key(e.name)
                        if path:
                            variants.setdefault((e.type, path), []).append(e.name)
                    table[key].append((a, e))
            self._lookup, self._variants = table, variants
        return self._lookup

    @staticmethod
    def _path_key(name):
        """The asset path inside a name that may carry option syntax.

        Image names appear as a bare path, as "hqcompress <path>", and as
        "shrink( <path>, 512)". Keying on the last whitespace token handles
        the first two and fails badly on the third: the last token is "512)",
        so every shrink() image sharing that number collapses into one group.
        Measured against the retail data, 16 of 40 groups were contaminated
        this way and the worst held 74 unrelated paths -- and apply() writes
        a payload into every member of the group.

        A path is the one token containing a slash. No slash (constantcolor(
        1, 1, 1, 1)) or several, and the name gets no group at all: an exact
        match still works, and nothing foreign comes along.
        """
        paths = [p.strip("(),") for p in name.split() if "/" in p]
        return paths[0].lower() if len(paths) == 1 else None

    def find(self, type_, name):
        """Every entry that is this asset, under any of its spellings.

        Image names carry option prefixes ("hqcompress foo", "linear foo",
        "shrink( foo, 512)"), and the retail data holds 40 groups where the
        same path ships under more than one of them -- e.g. both
        "textures/common/flat_local" and "hqcompressnormal
        textures/common/flat_local". Returning just the first match left the
        other spellings at their original content, so a texture mod patched
        only half of what the game may load. The exact match stays in front
        because read() takes hits[0] as the canonical copy.
        """
        hits = list(self.lookup.get((type_, name), ()))
        path = self._path_key(name)
        if not path:
            return hits  # no single path inside the name, so no safe grouping
        for ename in self._variants.get((type_, path), ()):
            if ename != name:
                hits.extend(self._lookup[(type_, ename)])
        return hits

    def read(self, type_, name):
        hits = self.find(type_, name)
        if not hits:
            raise PatchError("asset not found: %s:%s" % (type_, name))
        a, e = hits[0]
        return a.read(e)

    # -- journal / backup --------------------------------------------------

    @property
    def journal_path(self):
        return self.backup / JOURNAL_NAME

    def read_journal(self):
        """The journal, or None if there is none. Never None for a broken one.

        Only a missing file means "never patched". Any other failure used to
        read as pristine, and revert() then restored the indexes over archives
        that were still rewritten -- exactly the offset mismatch this module
        exists to avoid.
        """
        try:
            raw = self.journal_path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PatchError("Journal not readable: %s" % exc)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise PatchError(
                "The journal is damaged (%s). The installation is probably "
                "patched; please restore the backup in %s by hand."
                % (exc, self.backup))

    def is_modified(self):
        return self.read_journal() is not None

    def ensure_backup(self):
        with self._lock():
            return self._ensure_backup()

    def _ensure_backup(self):
        """Snapshot pristine indexes and original data lengths, once.

        Refuses to run while a journal exists: that would capture an already
        modified state as the restore point and make reverting impossible.
        """
        if self.is_modified():
            raise PatchError(
                "A journal already exists. Run 'revert' first, "
                "otherwise a modified state would be saved as the original."
            )
        self.backup.mkdir(parents=True, exist_ok=True)
        manifest = {"created": time.time(), "indexes": {}, "resources": {}}
        for index_name, res_name in self.pairs:
            src = self.base / index_name
            dst = self.backup / index_name
            if not dst.exists():
                shutil.copy2(src, dst)
            manifest["indexes"][index_name] = {
                "size": dst.stat().st_size,
                "sha256": _sha256(dst),
            }
            res = self.base / res_name
            # "size" is the pristine length revert() truncates back to and must
            # stay pristine; the "last_*" stamp tracks what we left behind.
            manifest["resources"][res_name] = self._stamp(
                {"size": res.stat().st_size}, res_name)
        _write_json(self.manifest_path, manifest)
        return manifest

    @property
    def manifest_path(self):
        return self.backup / "manifest.json"

    def _stamp(self, info, name):
        st = (self.base / name).stat()
        info["last_size"], info["last_mtime"] = st.st_size, st.st_mtime
        return info

    def _record_resources(self):
        """Re-stamp every .resources with the state we are leaving it in.

        Size and mtime both move when we patch, so they can only identify an
        outside change if the stamp is refreshed whenever *we* are the author.
        apply() and revert() are the only two places that write data files, so
        anything that moved afterwards was someone else -- a game update.
        """
        manifest = self.read_manifest()
        if not manifest:
            return
        for name, info in manifest["resources"].items():
            self._stamp(info, name)
        _write_json(self.manifest_path, manifest)

    def read_manifest(self):
        try:
            return json.loads(self.manifest_path.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def verify_backup(self, ours=False):
        """Check the backup still matches this installation. Returns problems.

        `ours` is set by exactly one caller: apply()'s own rollback. There the
        .resources stamps are known to be stale, because apply() took them
        before writing and refreshes them only on success -- judging our own
        half-finished write as a game update is what killed the rollback.

        Deriving that from "a journal exists" instead was too broad and cost
        the check its whole purpose: a Steam update landing while mods are
        installed would have passed unnoticed, and revert() would then have
        laid pristine indexes over foreign data -- the offset mismatch this
        module exists to prevent.
        """
        manifest = self.read_manifest()
        if not manifest:
            return ["No backup found."]
        problems = []
        # While a journal exists the live index legitimately differs from the
        # pristine one, so it can only be hashed in the unpatched state.
        patched = self.journal_path.is_file()
        replaced = ("%s no longer matches the backup. Game update? "
                    "Discard the backup and create a new one.")
        for name, info in manifest["indexes"].items():
            path = self.backup / name
            if not path.is_file():
                problems.append("Backup missing: %s" % name)
                continue
            if path.stat().st_size != info["size"]:
                problems.append("Backup damaged: %s" % name)
            elif info.get("sha256") and _sha256(path) != info["sha256"]:
                # The pristine copy is the only way back, and its hash has been
                # sitting unused in the manifest since it was written.
                problems.append("Backup damaged (checksum): %s" % name)
            live = self.base / name
            if not live.is_file():
                problems.append("Index file missing: %s" % name)
            elif live.stat().st_size != info["size"]:
                # Patching rewrites 12 bytes inside the index buffer and never
                # resizes it, so a different length on disk was not us.
                problems.append(replaced % name)
            elif (not patched and info.get("sha256")
                  and _sha256(live) != info["sha256"]):
                # Unpatched, so the live index must still be the pristine one.
                # Size alone lets an update through that keeps the length, and
                # the hash to catch it has been in the manifest all along --
                # applied until now only to the backup copy, never to the file
                # that actually gets read.
                problems.append(replaced % name)
        for name, info in manifest["resources"].items():
            path = self.base / name
            if not path.is_file():
                problems.append("Data file missing: %s" % name)
                continue
            if ours:
                continue
            st = path.stat()
            if "last_mtime" not in info:
                # Manifest from before stamp tracking: a rebuild grows the file
                # legitimately, so only shrinkage proves an outside change.
                if st.st_size < info["size"]:
                    problems.append(
                        "%s is smaller than recorded in the backup. Game update? "
                        "Discard the backup and create a new one." % name)
            elif st.st_size != info["last_size"] or abs(st.st_mtime - info["last_mtime"]) > 1:
                problems.append(replaced % name)
        return problems

    # -- apply / revert ----------------------------------------------------

    def revert(self, rollback=False):
        """Restore the installation to its pristine state.

        `rollback=True` marks the call apply() makes to undo its own failed
        write; only then are stale .resources stamps expected.

        Three kinds of change have to be undone, in this order:
          1. whole .resources files that were rewritten (restore the copy),
          2. individual slots that were overwritten in place (write the
             original bytes back), and
          3. the .index files (restore the pristine copies).
        """
        with self._lock():
            return self._revert(rollback)

    def _revert(self, rollback=False):
        manifest = self.read_manifest()
        if not manifest:
            raise PatchError("No backup found - nothing to revert.")
        # Every problem blocks: restoring pristine indexes over data files that
        # a game update replaced produces stale offsets with no way back.
        journal = self.read_journal()
        # An incomplete transaction deliberately has stale resource stamps:
        # the write-ahead journal was persisted before the first archive byte.
        recovering = bool(journal and journal.get("state") == "applying")
        problems = self.verify_backup(ours=rollback or recovering)
        if problems:
            raise PatchError("Backup unusable:\n  " + "\n  ".join(problems))
        journal = journal or {}
        self.close()

        for name in journal.get("rebuilt", []):
            copy = self.backup / name
            if copy.is_file():
                shutil.copy2(copy, self.base / name)
                copy.unlink()

        rebuilt = set(journal.get("rebuilt", []))
        for slot in journal.get("slots", []):
            if slot["resource"] in rebuilt:
                continue  # already covered by the full copy
            raw = base64.b64decode(slot["data"])
            with open(self.base / slot["resource"], "r+b") as fh:
                fh.seek(slot["offset"])
                fh.write(raw)

        # Older journals grew the file by appending; trim that back.
        for name, info in manifest["resources"].items():
            path = self.base / name
            if path.is_file() and path.stat().st_size > info["size"]:
                with open(path, "r+b") as fh:
                    fh.truncate(info["size"])

        for name in manifest["indexes"]:
            shutil.copy2(self.backup / name, self.base / name)
        self._record_resources()
        if self.journal_path.exists():
            self.journal_path.unlink()
        self._archives = None
        self._lookup = None
        self._variants = None
        return True

    def apply(self, payloads, mod_ids=()):
        """Write `payloads` into the archives.

        payloads: {(type, name): bytes}

        Entries are written inside the slot they already occupy so that their
        offsets never move. The engine's background loader rejects an archive
        whose entry offsets are not ascending -- appending to the end of the
        file produces exactly that and makes the game abort with
        "idBackgroundLoader::BeginBackgroundLoads: FileTable is out of order".

        A replacement too large for its slot forces a full rewrite of that
        archive, which is correct but copies the whole file, so the original
        is backed up first.
        """
        with self._lock():
            return self._apply(payloads, mod_ids)

    def _apply(self, payloads, mod_ids):
        if self.is_modified():
            self.revert()
        self.ensure_backup()

        written, missing = [], []
        # (archive, entry, data) triples grouped by what they need
        in_place, rebuilds = [], {}

        for (type_, name), data in payloads.items():
            hits = self.find(type_, name)
            if not hits:
                missing.append("%s:%s" % (type_, name))
                continue
            for archive, entry in hits:
                # Must fit in the entry's own compressed size, not merely in
                # the slot: shrinking csize would unpack the file layout and
                # mis-address every later entry. The plan is carried to
                # write_in_place instead of being recomputed there -- packing
                # is the most expensive thing this module does.
                plan = plan_in_place(entry, data)
                if plan is not None:
                    in_place.append((archive, entry, data, plan))
                else:
                    # Key by the entry's real name, not the requested one.
                    # Image assets carry option prefixes ("hqcompress foo"),
                    # and rebuild() matches on entry.name -- keying by the
                    # lookup name silently drops the payload.
                    rebuilds.setdefault(archive, {})[(entry.type, entry.name)] = data
            written.append("%s:%s" % (type_, name))

        slots = []
        touched = set()
        # Finish every recovery record before changing the installation. A
        # killed process cannot run the except block below, so an in-memory
        # slot copy is not a backup. The durable, atomic journal is.
        for archive, entry, _data, _plan in in_place:
            if archive in rebuilds:
                continue
            slots.append({
                "resource": archive.resources_path.name,
                "offset": entry.offset,
                "data": base64.b64encode(archive.read_slot(entry)).decode("ascii"),
            })
        for archive in rebuilds:
            self._backup_resources(archive)
        journal = self._write_journal(
            mod_ids, written, missing, slots, list(rebuilds), state="applying")

        try:
            for archive, entry, data, plan in in_place:
                if archive in rebuilds:
                    rebuilds[archive][(entry.type, entry.name)] = data
                    continue
                archive.write_in_place(entry, data, plan)
                touched.add(archive)

            for archive, archive_payloads in rebuilds.items():
                archive.rebuild(archive_payloads)
                touched.add(archive)

            for archive in touched:
                archive.index.save()
        except Exception:
            # A failure mid-write leaves a half-patched install; roll back.
            self.revert(rollback=True)
            raise

        self._record_resources()
        journal = self._write_journal(
            mod_ids, written, missing, slots, list(rebuilds), state="applied")
        self.close()
        return journal

    def _backup_resources(self, archive):
        """Keep a full copy of one .resources before it gets rewritten."""
        dst = self.backup / archive.resources_path.name
        if not dst.exists():
            shutil.copy2(archive.resources_path, dst)

    def _write_journal(self, mod_ids, written, missing, slots, rebuilt,
                       state="applied"):
        journal = {
            "applied": time.time(),
            "state": state,
            "mods": list(mod_ids),
            "assets": sorted(written),
            "missing": sorted(missing),
            "slots": slots,
            "rebuilt": [a.resources_path.name for a in rebuilt],
        }
        _write_json(self.journal_path, journal)
        return journal
