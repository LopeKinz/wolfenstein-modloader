"""A custom Wolfenstein II map: install and remove dlc/dlc_0.

The engine walks dlc/* in name order and mounts the first folder whose
base/packagemapspec.json lists a map (re_probes/agent_reports/dlc4_override.md,
seen in the game on 2026-09-24). dlc_0 sorts before the retail dlc_2, so a
dlc_0 that claims game/dlc/c02/c2v1 serves its own chunk_22.resources in place
of the retail one. Everything else the map needs -- sound banks, virtual
texture pages, intro video, chunk_22.texdb, textures.cache -- is dlc_2's own
file under a second name: a hardlink, so it costs no space.

A hardlink is one file with two names: opening a dlc_0 side file for writing
writes dlc_2's copy too. So nothing here opens an existing file for writing.
New files are created, the chunk is copied to a temp name and moved over the
old name (os.replace swaps the directory entry, the old content stays as it
is), and removing only unlinks names.

What is ours is written to dlc_0/wolfsdk_karte.json before the first file is
created, with size and sha256 of every file the loader writes. A dlc_0 made by
hand (the PowerShell route; dlcinfo.json may be missing) is adopted: its files
are recorded as found, its chunk and JSONs with size and sha256. An empty
leftover dlc_0 is simply used. Uninstall removes a file only while it still
recognises it -- its recorded size+sha256, or provably dlc_2's (the same file,
or byte-identical). Anything else is kept and named, and then nothing is
removed.

While Wolfenstein II mods are applied, tncpatch may hold dlc_0's
textures.cache as a relinked copy and journals it. Then only the chunk swap is
allowed; creating or removing files waits until the mods are reset.
"""

import contextlib
import filecmp
import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path

from . import idcl, mod as modlib
from .mod import ModError
from .patch import _sha256, _write_json
from .tncpatch import BACKUP_DIR, JOURNAL_NAME

MAP = "game/dlc/c02/c2v1"
ALIAS = MAP + "_custom"
TARGET, SOURCE = "dlc_0", "dlc_2"
CHUNK = "base/chunk_22.resources"
TEXDB = "base/chunk_22.texdb"
SPEC = "base/packagemapspec.json"
INFO = "dlcinfo.json"
MARKER = "wolfsdk_karte.json"
TMP = ".wolfsdk_neu"
# dlc_2 carries three maps; the other two bring their own videos, pages, caches.
OTHER_MAPS = re.compile(r"c2v[23]")
BUILDGAME = "generated/buildgame/"
# Every map loads maps/<name>/textures.cache from its DLC folder; an extra map
# in the container gets c2v1's under its own name (one more hardlink).
TEXCACHE = "base/maps/%s/textures.cache"

NO_DLC = ("The DLC \"The Freedom Chronicles\" (episode 2, dlc/dlc_2 with the map c2v1) "
          "is not installed. Custom maps replace that map and need it.")
MODS_FIRST = ("Mods are applied right now. While they are, the loader may only swap "
              "chunk_22.resources in dlc_0, not create or delete files. "
              "Please revert them first (\"Revert\" in the loader, or: python -m wolfsdk revert), "
              "then install or remove the map, "
              "and apply the mods again afterwards.")


def _dirs(game):
    dlc = Path(game.root) / "dlc"
    return dlc / TARGET, dlc / SOURCE


def _claims(folder):
    """Map names in the folder's packagemapspec.json; empty when unreadable."""
    try:
        return {m["name"] for m in json.loads((folder / SPEC).read_text("utf-8"))["maps"]}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def _dlc_ok(src):
    return MAP in _claims(src) and (src / CHUNK).is_file() and (src / TEXDB).is_file()


def _is_link(path):
    """Symlink or junction. A junction into dlc_2 would make every unlink here
    a delete there, so such a folder is never touched."""
    st = os.lstat(path)
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _kind(dst):
    """'fehlt', 'loader' (our marker), 'handgemacht' (claims c2v1, no marker),
    'leer' (no file at all, e.g. left by a removal that could not rmdir), 'fremd'."""
    if not os.path.lexists(dst):
        return "fehlt"
    if _is_link(dst) or not dst.is_dir():
        return "fremd"
    if (dst / MARKER).is_file():
        return "loader"
    if MAP in _claims(dst):       # dlcinfo.json may be missing: install adds it
        return "handgemacht"
    try:
        return "fremd" if _files(dst) else "leer"
    except ModError:              # a linked subfolder
        return "fremd"


def _foreign(dst):
    """Why dlc_0 is not the loader's, and exactly what the user can do about it."""
    if _is_link(dst):
        why, how = "is a link (junction/symlink)", (
            "remove just the link (Command Prompt: rmdir \"%s\" - "
            "this does not delete its target)" % dst)
    else:
        how = ("delete the folder\n  %s\nor move it out of the dlc folder "
               "(renaming is not enough, the game reads every folder in dlc)" % dst)
        try:
            files = _files(dst) if dst.is_dir() else None
        except ModError:
            files = None
        if not dst.is_dir():
            why = "is a file, not a folder"
        elif (dst / SPEC).exists():
            why = "has a base/packagemapspec.json that does not claim the map c2v1"
        elif files is None:
            why = "contains a link (junction/symlink)"
        else:
            why = ("contains %d file(s) (%s%s), but no base/packagemapspec.json for c2v1"
                   % (len(files), ", ".join(files[:3]), ", …" if len(files) > 3 else ""))
    return ("dlc/dlc_0 %s. It does not belong to a custom map, so the loader leaves "
            "it alone - nothing was changed.\nIf it can go: %s." % (why, how))


def _files(folder):
    """Every file under folder, as '/'-paths. Refuses linked subfolders."""
    out = []
    for dirpath, _sub, names in os.walk(folder):
        if _is_link(dirpath):
            raise ModError("%s is a link (junction/symlink). The loader leaves "
                           "the folder alone - nothing was changed." % dirpath)
        out += [Path(dirpath, n).relative_to(folder).as_posix() for n in names]
    return sorted(out)


def side_files(src):
    """dlc_2's files that c2v1 needs beside its chunk and the two JSONs."""
    return [rel for rel in _files(src)
            if rel not in (INFO, SPEC, CHUNK) and not OTHER_MAPS.search(rel)]


def _marker(dst):
    try:
        return json.loads((dst / MARKER).read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ModError("%s is damaged (%s) - nothing was changed." % (dst / MARKER, exc))


def _rec(path=None, data=None):
    """Size and sha256: what uninstall later recognises a file by."""
    if data is None:
        return {"bytes": path.stat().st_size, "sha256": _sha256(path)}
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _same(path, rec):
    size = path.stat().st_size
    return size == rec.get("bytes", size) and _sha256(path) == rec["sha256"]


def _known(dst, src, rel, marker):
    """True while dlc_0/rel is still what the loader put or found there."""
    records = marker.get("files") or {}
    recs = [r for r in (records.get(rel), marker.get("chunk") if rel == CHUNK else None) if r]
    if any(_same(dst / rel, r) for r in recs):
        return True
    link = (marker.get("links") or {}).get(rel)      # dlc_2's file under an extra map's name
    if link and (src / link).is_file() and os.path.samefile(dst / rel, src / link):
        return True
    if (src / rel).is_file() and (os.path.samefile(dst / rel, src / rel)
                                  or filecmp.cmp(dst / rel, src / rel, shallow=False)):
        return True     # dlc_2's file, or a copy of it: removing it loses nothing
    # A layout JSON from a marker written before files were recorded.
    return rel in (INFO, SPEC) and rel not in records


HELD = (5, 32, 33)      # access denied, sharing violation, lock violation


def _reason(exc):
    """An OSError in plain words; a file held open gets who to close."""
    if not (isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in HELD):
        return str(exc)
    name = exc.filename2 or exc.filename
    return ("Windows denies access to %s - usually because the file is open right now. "
            "Please quit the game and close the Studio (including a console window running "
            "\"wolfsdk studio\"). If that does not help, restart the loader once."
            % (Path(name).name if name else "a file"))


def _prune(dst):
    """Remove the empty folders at and below dst; full ones stay."""
    for dirpath, _sub, _names in os.walk(dst, topdown=False):
        with contextlib.suppress(OSError):
            os.rmdir(dirpath)


def _mods_applied(game):
    return (Path(game.base) / BACKUP_DIR / JOURNAL_NAME).is_file()


def _check_title(game):
    if game.title.key != "tnc":
        raise ModError("Custom maps are only available for Wolfenstein II: The New Colossus.")


def check_container(path):
    """Refuse anything that is not an IDCL archive carrying the map c2v1.

    Returns the maps it carries beside c2v1 (e.g. game/wolfsdk/box from
    tools/build_boxmap.py): every map has a generated/buildgame/<name>.mapresources.
    """
    path = Path(path)
    try:
        with idcl.Archive(path) as archive:
            names = [e.name for e in archive.entries]
    except Exception as exc:  # noqa: BLE001 - any file the user picks; unreadable = not a container
        raise ModError("%s is not a map container (%s)." % (path.name, exc))
    if not any(n.startswith("maps/" + MAP + ".") or n.startswith("maps/" + MAP + "/")
               for n in names):
        raise ModError("%s does not contain the map c2v1 - this is not a container "
                       "for a custom map." % path.name)
    maps = [n[len(BUILDGAME):-len(".mapresources")] for n in names
            if n.startswith(BUILDGAME) and n.endswith(".mapresources")]
    return sorted(m for m in maps if m not in (MAP, ALIAS) and not OTHER_MAPS.search(m))


def status(game, sha=False):
    """What dlc_0 holds. Reads only; sha=True also hashes the installed chunk."""
    dst, src = _dirs(game)
    kind = _kind(dst)
    marker = _marker(dst) if kind == "loader" else None
    chunk = dst / CHUNK
    dlc = _dlc_ok(src)
    missing = []
    if dlc and kind in ("loader", "handgemacht"):
        missing = [rel for rel in side_files(src) + [INFO, SPEC] if not (dst / rel).exists()]
    return {
        "dlc": dlc,
        "ordner": kind,
        "beansprucht": MAP in _claims(dst) if kind != "fehlt" else False,
        "fehlend": missing,
        "chunk_bytes": chunk.stat().st_size if kind != "fehlt" and chunk.is_file() else None,
        "chunk_sha256": _sha256(chunk) if sha and kind != "fehlt" and chunk.is_file() else None,
        "installiert": (marker or {}).get("chunk"),
        "mods_aktiv": _mods_applied(game),
    }


def describe(st):
    """status() as lines for the CLI and the loader window."""
    lines = ["DLC The Freedom Chronicles (map c2v1): %s"
             % ("installed" if st["dlc"] else "MISSING - custom maps need it")]
    lines.append({
        "fehlt": "Custom map: none installed (c2v1 is the original map).",
        "loader": "Custom map: installed by the loader.",
        "handgemacht": "Custom map: set up by hand. The first install through the loader "
                       "takes the folder over.",
        "leer": "Custom map: none installed (dlc/dlc_0 is an empty leftover, "
                "\"Install map\" will use it).",
        "fremd": "dlc/dlc_0 does not belong to a custom map - the loader leaves it alone.",
    }[st["ordner"]])
    done = st["installiert"]
    if done:
        lines.append("Source: %s" % done.get("quelle"))
        lines.append("sha256 recorded by the loader: %s" % done.get("sha256"))
    if st["chunk_bytes"] is not None:
        lines.append("chunk_22.resources: %s bytes%s" % (
            "{:,}".format(st["chunk_bytes"]),
            ", sha256 %s" % st["chunk_sha256"] if st["chunk_sha256"] else ""))
        if st["chunk_sha256"] and done and done.get("sha256") != st["chunk_sha256"]:
            lines.append("WARNING: chunk_22.resources was changed after it was installed.")
    if st["fehlend"]:
        lines.append("Missing files: %d (%s)" % (len(st["fehlend"]), ", ".join(st["fehlend"])))
    if st["ordner"] in ("loader", "handgemacht") and not st["beansprucht"]:
        lines.append("WARNING: packagemapspec.json is missing - the map is not active.")
    if st["mods_aktiv"]:
        lines.append("Mods are applied: only the map container can be swapped.")
    return lines


def _link_or_copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        # FAT/exFAT or another volume: a real copy, under a temp name so a
        # broken-off copy never sits under the live name.
        tmp = dst.with_name(dst.name + TMP)
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)


def _put_chunk(src, dst):
    """Copy src to a temp name, move it over dst. Returns its sha256.

    Never opens dst: were dst a hardlink to dlc_2's chunk, writing it would
    write dlc_2's. os.replace only takes the name away from the old file.
    """
    if dst.exists() and os.path.samefile(src, dst):
        return _sha256(dst)
    tmp = dst.with_name(dst.name + TMP)
    tmp.unlink(missing_ok=True)
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(tmp, "xb") as fo:
            for block in iter(lambda: fi.read(1 << 20), b""):
                h.update(block)
                fo.write(block)
        os.replace(tmp, dst)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return h.hexdigest()


def install(game, chunk_path):
    """Install chunk_path as the custom c2v1. Returns a summary for the user."""
    _check_title(game)
    chunk_path = Path(chunk_path)
    dst, src = _dirs(game)
    modlib.refuse_while_running(game)
    if not _dlc_ok(src):
        raise ModError(NO_DLC)
    extras = check_container(chunk_path)
    kind = _kind(dst)
    if kind == "fremd":
        raise ModError(_foreign(dst) + " Then click \"Install map\" again.")
    present = _files(dst) if kind != "fehlt" else []
    if (dst / SPEC).exists() and MAP not in _claims(dst):
        raise ModError("dlc_0/base/packagemapspec.json does not claim c2v1. Nothing was changed.")
    marker = _marker(dst) if kind == "loader" else {"created": [], "adopted": present}
    records = marker.setdefault("files", {})
    links = {TEXCACHE % name: TEXCACHE % MAP for name in extras}
    todo = [rel for rel in side_files(src) + list(links) + [INFO, SPEC] if not (dst / rel).exists()]
    if todo and _mods_applied(game):
        raise ModError(MODS_FIRST)
    info = (json.dumps({"dlcid": 4}, indent=3) + "\n").encode()
    spec = json.loads((src / SPEC).read_text("utf-8"))
    entry = next(m for m in spec["maps"] if m["name"] == MAP)
    spec["maps"] = [dict(entry, name=ALIAS), entry] + [dict(entry, name=name) for name in extras]
    spec = (json.dumps(spec, indent=3) + "\n").encode()
    # Our own map list from an earlier install (or one taken over before files were
    # recorded, which uninstall counts as ours too) follows the new container's maps;
    # one edited by hand stays as it is.
    respec = (SPEC not in todo and _known(dst, src, SPEC, marker)
              and set(_claims(dst)) != {ALIAS, MAP, *extras})
    unlisted = [n for n in extras if SPEC not in todo and not respec and n not in _claims(dst)]
    for rel, data in ((INFO, info), (SPEC, spec)):
        if rel in todo or rel == SPEC and respec:
            records[rel] = _rec(data=data)

    try:
        if kind == "handgemacht":
            # Found files known by content from now on: a swap that breaks off
            # below leaves a dlc_0 "Karte entfernen" can still remove, and a
            # JSON edited later is kept, not deleted.
            for rel in (CHUNK, INFO, SPEC):
                if rel in present and not (rel == SPEC and respec):   # a rewritten spec has its record
                    records[rel] = _rec(dst / rel)
        # The record comes first: whatever breaks after this, uninstall knows.
        marker["created"] = sorted(set(marker.get("created", [])) | set(todo))
        dst.mkdir(parents=True, exist_ok=True)
        _write_json(dst / MARKER, marker)
        linked = copied = 0
        for rel in todo:
            if rel in (INFO, SPEC):
                continue
            origin = links.get(rel, rel)
            _link_or_copy(src / origin, dst / rel)
            if os.path.samefile(src / origin, dst / rel):
                linked += 1
                if rel in links:
                    marker.setdefault("links", {})[rel] = origin
            else:
                copied += 1
                records[rel] = _rec(dst / rel)
        if INFO in todo:
            (dst / INFO).write_bytes(info)
        (dst / "base").mkdir(exist_ok=True)
        digest = _put_chunk(chunk_path, dst / CHUNK)
        if SPEC in todo or respec:
            _write_json(dst / MARKER, marker)  # the links and the spec record before the spec itself
            (dst / SPEC).write_bytes(spec)   # last: from here on the engine serves c2v1 from dlc_0
        records.pop(CHUNK, None)             # the found chunk is gone; ours is "chunk"
        marker["chunk"] = {"sha256": digest, "bytes": (dst / CHUNK).stat().st_size,
                           "quelle": str(chunk_path)}
        _write_json(dst / MARKER, marker)
    except OSError as exc:
        raise ModError("Installation aborted: %s.\nNothing is lost: click \"Install "
                       "map\" once more when that is fixed - the loader picks up "
                       "where it stopped." % _reason(exc))

    lines = ["Map installed: %s" % chunk_path.name, "sha256 %s" % digest]
    if kind == "handgemacht":
        lines.append("The loader has taken over the dlc_0 folder that was set up by hand.")
    if linked or copied:
        lines.append("%d file(s) linked from dlc_2%s." % (
            linked, ", %d copied (a hardlink was not possible)" % copied if copied else ""))
    lines.append("In the game: load The Freedom Chronicles, episode 2 (c2v1).")
    if extras:
        lines.append("Also in this container: %s (load it from the dev menu)." % ", ".join(extras))
    if unlisted:
        lines.append("dlc_0/base/packagemapspec.json was edited by hand, so %s was not added "
                     "to it - the game will not find it." % ", ".join(unlisted))
    return "\n".join(lines)


def uninstall(game):
    """Remove what the loader put into dlc_0, all or nothing. Returns a summary."""
    _check_title(game)
    dst, src = _dirs(game)
    modlib.refuse_while_running(game)
    kind = _kind(dst)
    if kind == "fehlt":
        return "No custom map installed - nothing to do."
    if kind == "fremd":
        raise ModError(_foreign(dst))
    if kind == "handgemacht":
        raise ModError("dlc/dlc_0 was not created by the loader - nothing was deleted. "
                       "One \"Install map\" lets the loader take it over; "
                       "after that it can remove it too.")
    if _mods_applied(game):
        raise ModError(MODS_FIRST)
    if kind == "leer":
        _prune(dst)
        if dst.exists():
            return "dlc/dlc_0 is empty but could not be deleted: %s" % dst
        return "dlc/dlc_0 was an empty leftover without files and has been removed."
    marker = _marker(dst)
    ours = set(marker.get("created", [])) | set(marker.get("adopted", [])) | {CHUNK}
    try:
        files = _files(dst)
        keep = [rel for rel in files
                if not (rel == MARKER or rel.endswith((TMP, MARKER + ".tmp"))
                        or rel in ours and _known(dst, src, rel, marker))]
    except OSError as exc:
        raise ModError("Check aborted: %s. Nothing was deleted." % _reason(exc))
    if keep:
        raise ModError("dlc/dlc_0 holds files the loader does not (or no longer) recognise "
                       "as its own:\n  %s\nNothing was deleted. Back these files up yourself and "
                       "move them out of dlc_0, then click \"Remove map\" again. (A "
                       "chunk_22.resources swapped by hand is recognised again after "
                       "\"Install map\".)" % "\n  ".join(keep))

    # packagemapspec first: a removal that breaks off never leaves a dlc_0
    # that claims c2v1 without its files. The record goes last, so running
    # uninstall again picks up where it stopped.
    order = [SPEC] + [r for r in files if r not in (SPEC, INFO, MARKER)] + [INFO, MARKER]
    try:
        for rel in order:
            (dst / rel).unlink(missing_ok=True)   # only this name goes, dlc_2 keeps its file
    except OSError as exc:
        raise ModError("Removal interrupted: %s. The map is already switched off; "
                       "please click \"Remove map\" once more." % _reason(exc))
    _prune(dst)
    if dst.exists():
        return "Map removed; dlc/dlc_0 could not be deleted completely: %s" % dst
    return "Custom map removed. c2v1 loads the original map from the DLC again."
