"""Mod packages: manifest, load order, conflict reporting, payload building.

A mod is a folder containing `mod.json`:

    {
      "id": "example.buffed_shotgun",
      "name": "Buffed Shotgun",
      "version": "1.0.0",
      "author": "you",
      "description": "Harder-hitting buckshot.",
      "priority": 100,
      "spiel": "tno",
      "assets": {
        "damage:damage/tungsten/buckshot": {
          "set": { "edit.damageParms.maxDamage": 99 }
        },
        "weapon:weapon/shotgun_base": { "file": "decls/shotgun_base.decl" }
      }
    }

`set` edits named values in place and leaves the rest of the decl untouched;
`file` replaces the asset wholesale. Mods are layered in priority order over
the pristine asset, lowest first, so two mods touching different values of the
same decl compose cleanly. Only same-value or replace-vs-replace collisions
are real conflicts, and those are reported rather than silently resolved.

`spiel` says which game the mod is for: "tno" (The New Order, id Tech 5) or
"tnc" (The New Colossus, id Tech 6). It is optional and defaults to "tno",
because every manifest written before part two existed is a part one mod.
The two games share neither asset names nor archive format, so a set of mods
is always filtered by title before it is layered -- see `for_title`, and
`installation_for` for the patcher each title gets.
"""

import hashlib
import json
import shutil
from pathlib import Path

from . import cvars, tncview
from . import tnccrypt
from .decl import DeclError, apply_patch
from .idcl import COMP_KRAKEN_BLOCKS
from .patch import Installation as TnoInstallation, PatchError
from .resources import plan_in_place
from .tncpatch import Installation as TncInstallation, TncPatchError, TEXDB, WHOLE, gaps_of

# Both patchers raise their own error type, and every caller that reaches one
# through installation_for() can now reach the other. One name so a new title
# is a change here rather than in every except clause.
PATCH_ERRORS = (PatchError, TncPatchError)

MANIFEST = "mod.json"
REQUIRED = ("id", "name")
SUPPORTED_GAMES = ("tno", "tnc")


def installation_for(game):
    """The patcher that can write this title's archives.

    A switch on the title key is the whole abstraction. id Tech 5 keeps its
    file table in separate .index files and backs those up; id Tech 6 carries
    the table inside each archive and backs up head regions. A common
    interface over the two would have to pretend they are alike, and the only
    thing they actually share is the handful of method names below.
    """
    if game.title.key == "tnc":
        return TncInstallation(game.base)
    return TnoInstallation(game)


def for_title(mods, title_key):
    """The mods written for one title. A manifest without `spiel` means tno."""
    return [m for m in mods if m.spiel == title_key]


def read_asset(install, type_, name):
    """The asset's current bytes, from whichever Installation this is.

    Both patchers have read() now, so the only thing left to do here is to
    make them fail alike: each raises its own error for an asset that is not
    there, and a mod has no business telling the two apart.
    """
    try:
        return install.read(type_, name)
    except PATCH_ERRORS as exc:
        raise ModError(str(exc))


class ModError(Exception):
    pass


class Mod:
    def __init__(self, root, data):
        self.root = Path(root)
        self.data = data
        self.id = data["id"]
        self.name = data["name"]
        self.version = data.get("version", "0.0.0")
        self.author = data.get("author", "")
        self.description = data.get("description", "")
        self.priority = int(data.get("priority", 100))
        # Which game this mod is for. Manifests written before part two
        # existed say nothing, and all of them are New Order mods.
        self.spiel = data.get("spiel", "tno")
        self.assets = data.get("assets", {})
        self.cvars = data.get("cvars", {})

    @classmethod
    def load(cls, root):
        root = Path(root)
        path = root / MANIFEST
        try:
            data = json.loads(path.read_text("utf-8"))
        except OSError as exc:
            raise ModError("%s: %s" % (path, exc))
        except ValueError as exc:
            raise ModError("%s: invalid JSON (%s)" % (path, exc))
        for key in REQUIRED:
            if key not in data:
                raise ModError("%s: field '%s' is missing" % (path, key))
        if not isinstance(data.get("id"), str) or not data["id"].strip():
            raise ModError("%s: field 'id' must be a non-empty string" % path)
        if not isinstance(data.get("name"), str) or not data["name"].strip():
            raise ModError("%s: field 'name' must be a non-empty string" % path)
        if data.get("spiel", "tno") not in SUPPORTED_GAMES:
            raise ModError("%s: 'spiel' must be %s" %
                           (path, " or ".join(repr(x) for x in SUPPORTED_GAMES)))
        try:
            int(data.get("priority", 100))
        except (TypeError, ValueError):
            raise ModError("%s: 'priority' must be a whole number" % path)
        assets = data.get("assets", {})
        if not isinstance(assets, dict):
            raise ModError("%s: 'assets' must be an object" % path)
        for asset, spec in assets.items():
            if not isinstance(asset, str) or ":" not in asset:
                raise ModError("%s: asset key must look like 'type:name', not %r" %
                               (data["id"], asset))
            if not isinstance(spec, dict):
                raise ModError("%s: %s must be an object" % (data["id"], asset))
            if not spec.get("file") and not spec.get("set"):
                raise ModError("%s: %s has neither 'file' nor 'set'" %
                               (data["id"], asset))
            if "set" in spec and not isinstance(spec["set"], dict):
                raise ModError("%s: %s.set must be an object" %
                               (data["id"], asset))
            rel = spec.get("file")
            if rel is not None:
                if not isinstance(rel, str) or not rel.strip():
                    raise ModError("%s: %s.file must be a relative path" %
                                   (data["id"], asset))
                candidate = (root / rel).resolve()
                try:
                    candidate.relative_to(root.resolve())
                except ValueError:
                    raise ModError("%s: %s.file points outside the mod folder: %s" %
                                   (data["id"], asset, rel))
        cvars = data.get("cvars", {})
        if not isinstance(cvars, dict):
            raise ModError("%s: 'cvars' must be an object" % path)
        for key, value in cvars.items():
            if not isinstance(key, str) or not key.strip():
                raise ModError("%s: cvar name must be a non-empty string" % path)
            if isinstance(value, (dict, list)) or value is None:
                raise ModError("%s: cvar '%s' needs a single value (text, number or true/false)" % (path, key))
        return cls(root, data)

    def targets(self):
        """The (type, name) assets this mod touches."""
        out = []
        for key in self.assets:
            if ":" not in key:
                raise ModError("%s: asset key must look like 'type:name', not %r" % (self.id, key))
            type_, name = key.split(":", 1)
            out.append((type_, name))
        return out

    def content_for(self, key):
        """The replacement bytes for a `file` entry, if any."""
        spec = self.assets[key]
        rel = spec.get("file")
        if not rel:
            return None
        path = self.root / rel
        if not path.is_file():
            raise ModError("%s: file is missing: %s" % (self.id, rel))
        return path.read_bytes()

    def __repr__(self):
        return "<Mod %s v%s>" % (self.id, self.version)


def discover(mods_dir):
    """Every mod folder under `mods_dir`, sorted by priority then id."""
    mods_dir = Path(mods_dir)
    found, errors = [], []
    if not mods_dir.is_dir():
        return found, errors
    for child in sorted(mods_dir.iterdir()):
        if not (child / MANIFEST).is_file():
            continue
        try:
            found.append(Mod.load(child))
        except ModError as exc:
            errors.append(str(exc))
    found.sort(key=lambda m: (m.priority, m.id))
    seen = set()
    unique = []
    for mod in found:
        if mod.id in seen:
            errors.append("Duplicate mod id: %s (%s)" % (mod.id, mod.root))
            continue
        seen.add(mod.id)
        unique.append(mod)
    return unique, errors


class Conflict:
    def __init__(self, asset, kind, detail, mods):
        self.asset = asset
        self.kind = kind  # "replace" or "value"
        self.detail = detail
        self.mods = mods

    def __str__(self):
        return "%s: %s (%s)" % (self.asset, self.detail, ", ".join(self.mods))


# Asset types two mods cannot share. A decl layers value by value; a texture
# is one blob, so the lower mod would vanish silently -- or worse, with the
# mips in separate texdb assets, the game would show half of each skin.
EXCLUSIVE_TYPES = ("image", TEXDB)


def _asset_id(mod, key):
    """What `key` names in the game, the way the patchers look it up.

    Every retail name is lower case (0 exceptions among 178077 TNC and 203843
    TNO entries, measured), tncpatch.texdb_hits reads a 16-character texdb key
    with int(name, 16) -- so "6E3B...", "6e3b..." and "0xa761..." for
    "00a761..." are one key --, and part one's find() lower-cases a name and
    groups every spelling of its path ("hqcompress textures/x" writes
    "textures/x" too). Comparing raw keys misses all of that.
    """
    type_, name = key.split(":", 1)
    name = name.lower()
    if type_ == TEXDB and len(name) == 16:
        try:
            name = "%016x" % int(name, 16)
        except ValueError:
            pass    # not a key; build_payloads says "existiert nicht"
    elif mod.spiel == "tno":
        name = TnoInstallation._path_key(name) or name
    return type_, name


def _by_asset(mods):
    """{asset id: [(mod, key as the mod spells it), ...]}, mods in the given order."""
    out = {}
    for mod in mods:
        for key in mod.assets:
            out.setdefault(_asset_id(mod, key), []).append((mod, key))
    return out


def texture_clashes(mods):
    """Errors the manifests alone rule out, so apply_mods and the cli/gui
    pre-checks refuse before anything is touched: one per group of mods that
    write the same image/texdb asset, and one per asset spelled two ways
    (_asset_id) -- each spelling would be layered alone and the last one
    written would drop the other's edits.
    """
    groups, spelled = {}, []
    for (type_, _name), pairs in sorted(_by_asset(mods).items()):
        owners = tuple({m.id: m for m, _k in pairs}.values())
        spellings = sorted({k for _m, k in pairs})
        if type_ in EXCLUSIVE_TYPES and len(owners) > 1:
            groups.setdefault(owners, []).append(pairs[0][1])
        elif len(spellings) > 1:
            spelled.append("%s: the same asset is spelled differently (%s) - "
                           "please spell it the same way everywhere"
                           % (", ".join(sorted(m.id for m in owners)), " / ".join(spellings)))
    out = []
    for owners, keys in groups.items():
        names = [m.name for m in owners]
        # Two mods may share a display name; then only the id tells them apart.
        quoted = ["'%s'" % m.name if names.count(m.name) == 1 else "'%s' (%s)" % (m.name, m.id)
                  for m in owners]
        out.append("%s and %s change the same textures (%d %s, e.g. %s). "
                   "They can't be used together - please tick only one of them."
                   % (", ".join(quoted[:-1]), quoted[-1], len(keys),
                      "asset" if len(keys) == 1 else "assets", keys[0]))
    return out + spelled


def build_payloads(install, mods):
    """Layer `mods` over pristine assets.

    Returns (payloads, conflicts, errors) where payloads maps
    (type, name) -> bytes ready to be written by patch.Installation.apply.

    The installation must be in its pristine state, otherwise the base content
    read here would already include a previous run's edits.

    Two mods writing the same image or texdb asset are an error, not a
    conflict, and so is one asset under two spellings (texture_clashes);
    decls keep "highest priority wins".
    """
    payloads = {}
    conflicts = []
    errors = texture_clashes(mods)

    # Ordered by key as before, so the payload order -- and the appended
    # texdb blocks -- stay what they were for manifests with one spelling.
    for pairs in sorted(_by_asset(mods).values(), key=lambda p: p[0][1]):
        key = pairs[0][1]
        type_, name = key.split(":", 1)
        contributors = [m for m, _k in pairs]
        if (type_ in EXCLUSIVE_TYPES and len({m.id for m in contributors}) > 1
                or len({k for _m, k in pairs}) > 1):
            continue  # texture_clashes() already said so
        if not install.find(type_, name):
            errors.append("%s: asset does not exist in the game (%s)" %
                          (", ".join(m.id for m in contributors), key))
            continue

        replacers = [m for m in contributors if m.assets[key].get("file")]
        if len(replacers) > 1:
            conflicts.append(Conflict(
                key, "replace",
                "several mods replace the same file completely; the highest priority wins",
                [m.id for m in replacers],
            ))

        seen_paths = {}
        for mod in contributors:
            for path in (mod.assets[key].get("set") or {}):
                if path in seen_paths:
                    conflicts.append(Conflict(
                        key, "value",
                        "both set '%s'; the highest priority wins" % path,
                        [seen_paths[path], mod.id],
                    ))
                seen_paths[path] = mod.id

        try:
            data = read_asset(install, type_, name)
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (key, exc))
            continue

        try:
            for mod in contributors:  # already priority-ordered
                spec = mod.assets[key]
                replacement = mod.content_for(key)
                if replacement is not None:
                    data = replacement
                edits = spec.get("set")
                if edits:
                    text = data.decode("utf-8")
                    data = apply_patch(text, edits).encode("utf-8")
        except (DeclError, ModError, UnicodeDecodeError) as exc:
            errors.append("%s (%s): %s" % (key, mod.id, exc))
            continue

        payloads[(type_, name)] = data

    return payloads, conflicts, errors


def applied_ids(install):
    """The mod ids the installation currently carries, per its journal."""
    journal = install.read_journal()
    return set(journal["mods"]) if journal else set()


RUNNING = ("%s is running. Please quit the game completely and try again - "
           "while it runs, Windows keeps its archives open and mods can only "
           "be half written.")


def running_game(install):
    """The exe of the game at this installation while it runs, else None.

    Asks tasklist (Windows, stdlib), for the executables that actually sit in
    the installation's folder -- so a copy of base/ elsewhere is never
    blocked. Undetectable counts as not running: the patchers roll back on a
    failed write, a broken check must not lock the loader for good.
    """
    import subprocess
    from .game import TITLES
    root = Path(install.base).parent
    for exe in sorted({t.exe for t in TITLES.values() if (root / t.exe).is_file()}):
        try:
            # Bytes, not text: the German "keine Aufgaben ... ausgeführt" is
            # OEM-encoded, and a text decode failing in the reader thread
            # hands back stdout=None.
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq %s" % exe, "/FO", "CSV", "/NH"],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or b""
        except (OSError, subprocess.SubprocessError):
            continue
        if ('"%s"' % exe).lower().encode() in out.lower():
            return exe
    return None


def refuse_while_running(install):
    """ModError with a message while the game at `install` runs."""
    exe = running_game(install)
    if exe:
        raise ModError(RUNNING % exe)


# Journal and manifest rewrites (each beside a .tmp twin), index saves, and
# the slack a nearly full disk needs anyway.
SPACE_RESERVE = 16 << 20
MB = 1 << 20    # what Explorer calls a MB


def space_needed(install, payloads):
    """(bytes, rebuilt archive names): what apply(payloads) adds to the disk at its peak.

    Mirrors both patchers from a pristine installation, with their own
    in-place decision (_plan / plan_in_place), so it rebuilds exactly where
    apply() would:
      * an archive that must be rebuilt: its full backup copy, plus the
        rebuild beside the original until the swap -- the largest one at a
        time, grown by at most the payloads (gameresources.resources: 2x 702 MB);
      * a slot written in place: its original bytes, base64, in the journal
        (4/3, and _write_json's .tmp twin) -- 3x the window;
      * TNC .texdb/cache rows: the old window in texdb.orig or the appended
        block, and a whole new copy of every hardlinked file;
      * the first apply ever: the head regions (TNC) or .index copies (TNO);
      * TNC videos and sound packs replaced whole: the new file beside the
        original (at most its old size plus the payloads).
    """
    tnc = isinstance(install, TncInstallation)
    need, rebuild, gaps, relinked, whole = SPACE_RESERVE, {}, {}, set(), []
    if tnc:
        if install.read_manifest() is None:
            need += sum(a.header.addr_data for a in install.archives)
        files = install.whole_files(payloads)[0]
        whole = sorted(p.name for p, _fill in files.values())
        need += (sum(p.stat().st_size for p, _fill in files.values())
                 + sum(len(d) for (t, _n), d in payloads.items() if t in WHOLE))
    else:
        need += sum((install.base / i).stat().st_size for i, _r in install.pairs
                    if not (install.backup / i).exists())
    for (type_, name), data in payloads.items():
        if tnc and type_ in WHOLE:
            continue
        if tnc and type_ == TEXDB:
            for hit in install.texdb_hits(name):
                st = hit["path"].stat()
                if st.st_nlink > 1 and hit["path"] not in relinked:
                    relinked.add(hit["path"])
                    need += st.st_size
                need += max(len(data), hit["size"])
            continue
        for archive, entry in install.find(type_, name):
            if tnc:
                if entry.compression == COMP_KRAKEN_BLOCKS:
                    continue  # apply() skips these too
                if archive not in gaps:
                    gaps[archive] = gaps_of(archive)
                plan = install._plan(entry, data, gaps[archive][entry.index])
                path, window = archive.path, plan and len(plan[0])
            else:
                plan = plan_in_place(entry, data)
                path, window = archive.resources_path, plan and archive.slot_size(entry)
            if plan is None:
                rebuild[path.name] = path.stat().st_size
            else:
                need += 3 * window
    if rebuild:
        need += (sum(rebuild.values()) + max(rebuild.values())
                 + sum(len(d) for d in payloads.values()))
    return need, sorted(rebuild) + whole


def space_problem(install, payloads):
    """Refusal when the drive under `install` lacks room for apply(), else None.

    ponytail: one drive, the game's; a backup directory moved to another
    drive (Installation(base, backup=X)) is counted against the game's.
    """
    need, rebuilt = space_needed(install, payloads)
    base = Path(install.base)
    free = shutil.disk_usage(base).free
    if need <= free:
        return None
    drive = base.resolve().drive or base.resolve().anchor
    why = (" Most of that is the backup copy and the rebuild of %s."
           % ", ".join(rebuilt)) if rebuilt else ""
    return ("Not enough disk space: %s MB needed, %s MB free on drive %s.%s "
            "Please free up some space and apply again."
            % (format(-(-need // MB), ","), format(free // MB, ","), drive, why))


def apply_mods(install, mods):
    """Revert to pristine, layer the mods, write them. Returns a report.

    Refuses while the game runs: Windows then refuses to replace an open
    archive, and a mod that needs a rebuild ends half written. Refuses before
    touching anything when two mods write the same texture or one asset is
    spelled two ways, and before the first write of the apply when the drive
    is too full for it.
    """
    refuse_while_running(install)
    clashes = texture_clashes(mods)
    if clashes:
        return {"ok": False, "conflicts": [], "errors": clashes, "journal": None}
    reverted = install.is_modified()
    if reverted:
        install.revert()
    payloads, conflicts, errors = build_payloads(install, mods)
    if not errors:
        problem = space_problem(install, payloads)
        errors = [problem] if problem else []
    if errors:
        if reverted:
            # build_payloads needs pristine data, so the old mod set is gone.
            errors.append("The previously applied mods have been reverted - "
                          "the game is now back to its original state.")
        return {"ok": False, "conflicts": conflicts, "errors": errors, "journal": None}
    journal = install.apply(payloads, [m.id for m in mods])
    return {"ok": True, "conflicts": conflicts, "errors": [], "journal": journal}


# -- TNC cheat/QoL tweaks the retail console gate rejects --------------------
#
# The New Colossus console and +launch-arguments share one gate that only 304
# of its ~3250 cvars pass (re_probes/cvartable/cvars_tnc_v2.json); everything
# else -- cheat-flagged ones the loader's own tweak list offers included --
# comes back "Unknown command" there, proven from the game's own log
# (TODO.md, 2026-10-01). A cfile the engine execs itself during init
# (cfile:default.cfg via its own 'resourceExec default.cfg -s') runs at a
# lower restriction level and is not filtered by that gate
# (re_probes/agent_reports/r4_consolecmds.md); God/Notarget/infinite ammo
# confirmed working that way from a loader-replaced cfile (mods/sandbox_tools).
# with_tnc_tweaks() below appends the rejected cvars as plain lines to that
# file's *retail* text and hands the result back as one more mod, so it goes
# through apply_mods/build_payloads -- and is journaled and reverted -- like
# any other mod asset, with no special-casing in tncpatch.py.

TNC_DEFAULT_CFG = ("cfile", "default.cfg")


class _TweaksMod:
    """The one in-memory 'mod' build_payloads needs for the tweaks cfile.

    Built fresh from the current tweak settings on every apply/launch --
    there is no mods/ folder for it, because writing one to disk and reading
    it back would only risk serving a stale copy. Duck-types exactly what
    build_payloads/apply_mods ask of a Mod: id, spiel, assets, content_for().
    """

    spiel = "tnc"

    def __init__(self, cfile_settings, data):
        # The id carries a hash of the cvar/value pairs, not of `data`: a
        # cfile is AES-encrypted with a fresh random salt+IV on every call
        # (tnccrypt.cfile_encrypt), so `data` differs between two calls even
        # for the identical tweak set, and hashing it would make the "is the
        # game's mod set == the enabled mod set" check every caller already
        # uses to decide whether to re-apply (cli.cmd_play, gui._mods_in_sync)
        # never report "in sync" -- a full re-patch on every single Start.
        # Hashing the plaintext settings instead gives the same id for the
        # same tweak set and a different one when a value changes, with no
        # extra bookkeeping anywhere.
        key = repr(sorted(cfile_settings.items()))
        self.id = "wolfsdk.tnc_tweaks.%s" % hashlib.sha1(key.encode()).hexdigest()[:12]
        self.name = "WolfSDK tweaks (default.cfg)"
        self.assets = {"%s:%s" % TNC_DEFAULT_CFG: {"file": True}}
        self.keys = sorted(cfile_settings)  # which cvars, for messages only
        self._data = data

    def content_for(self, _key):
        return self._data


def _cfile_conflict(mods):
    """The mod among `mods` that already replaces cfile:default.cfg, if any."""
    for m in mods:
        for key in m.assets:
            if _asset_id(m, key) == TNC_DEFAULT_CFG:
                return m
    return None


_TWEAKS_BLOCK_START = ("\n// --- WolfSDK: Cheats & QoL "
                       "(cvars the retail console/+arg gate rejects) ---\n")
_TWEAKS_BLOCK_END = "\n// --- end WolfSDK tweaks ---\n"


def _strip_tweaks_block(text):
    """`text` with every previously-appended WolfSDK tweaks block removed.

    with_tnc_tweaks() is called before apply_mods' own revert-if-modified
    (cli.cmd_play, cli.cmd_apply, gui._mods_in_sync, gui._apply_worker all
    read the install, then revert later), so this function can see its own
    earlier output here -- the install still carrying the block a previous
    apply wrote. Stripping it first, however many times it was stacked,
    before appending the current one is what makes repeated applies
    (clicking Start again, changing a tweak's value and applying again)
    converge on "retail text + current tweaks" instead of growing a new
    block on top of the old one every time.
    """
    while True:
        i = text.find(_TWEAKS_BLOCK_START)
        if i == -1:
            return text
        j = text.find(_TWEAKS_BLOCK_END, i)
        if j == -1:
            return text
        text = text[:i] + text[j + len(_TWEAKS_BLOCK_END):]


def _tnc_tweaks_cfile(install, cfile_settings):
    """The retail cfile:default.cfg, re-wrapped with `cfile_settings` appended.

    Reads whatever cfile:default.cfg currently is -- pristine on a first
    apply, or still carrying a previous call's own block when this runs
    before apply_mods' revert (see _strip_tweaks_block) -- strips any
    WolfSDK block already there, then appends the current one. Every retail
    line, the key binds included, is never touched by either step, so there
    is nothing here that can lose or reorder a retail line.
    """
    type_, name = TNC_DEFAULT_CFG
    hits = install.find(type_, name)
    if not hits:
        raise TncPatchError("%s:%s not found in this installation" % TNC_DEFAULT_CFG)
    entry = hits[0][1]
    inner, _note = tncview.unwrap(entry, install.read(type_, name))
    got = tncview.as_text(inner)
    if got is None:
        raise TncPatchError("%s:%s is not text" % TNC_DEFAULT_CFG)
    text, encoding, tail = got
    text = _strip_tweaks_block(text)
    block = (_TWEAKS_BLOCK_START
             + cvars.build_cfg(cfile_settings, header=False).rstrip("\n")
             + _TWEAKS_BLOCK_END)
    new_data = tncview.from_text(text + block, encoding, tail)
    return tncview.wrap(entry, new_data)


def with_tnc_tweaks(install, mods, settings):
    """(mods', arg_settings, refusal) -- how to deliver one New Colossus
    cvar/value set, given the `mods` already enabled for this launch/apply.

    The New Order is untouched by this function (it returns `mods, settings,
    None` unchanged for any non-TNC installation): its cvars all go out as
    +args, routed against its registry by cvars.build_args/tno_route
    (cheat-flagged ones additionally as +toggle).

    For The New Colossus: splits `settings` with cvars.split_tnc. Cvars the
    retail gate accepts come back unchanged in arg_settings, to be passed as
    +args as before. The rest, if any, becomes one more mod in the returned
    list, carrying cfile:default.cfg with those lines appended to its retail
    text (see _tnc_tweaks_cfile). When nothing needs that route,
    `cfile_settings` is empty and `mods` comes back unchanged -- no asset is
    even read, let alone written, which is what keeps an all-exposed tweak
    set from ever touching an archive.

    Refuses (mods unchanged, arg_settings still split out) instead of
    silently layering when an enabled mod already replaces that same file:
    "highest priority wins" would then quietly drop either the mod's lines
    or the tweaks', and that is not a call this function gets to make.
    """
    if not isinstance(install, TncInstallation):
        return mods, settings, None
    arg_settings, cfile_settings = cvars.split_tnc(settings)
    if not cfile_settings:
        return mods, arg_settings, None
    clash = _cfile_conflict(mods)
    if clash:
        return mods, arg_settings, (
            "Mod '%s' already replaces cfile:default.cfg. The enabled Cheats & QoL tweaks "
            "that need the same file to work (%s) were left out of this launch - disable "
            "'%s' or those tweaks, then apply again."
            % (clash.name, ", ".join(sorted(cfile_settings)), clash.name))
    try:
        data = _tnc_tweaks_cfile(install, cfile_settings)
    except (TncPatchError, tnccrypt.TncCryptError) as exc:
        return mods, arg_settings, (
            "Could not prepare the Cheats & QoL tweaks that need cfile:default.cfg: %s" % exc)
    return mods + [_TweaksMod(cfile_settings, data)], arg_settings, None
