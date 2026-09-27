"""Apply mods to a game with a journal, and revert them (File-Formats §3).

Backups live in ``base/_wolfsdk_backup/``:

* ``*.index``          copies of every index touched (restored last)
* ``*.resources.N``    full copies of archives taken before a rebuild
* ``journal.json``     ordered events: original bytes of every in-place slot
                       write (base64) and every full archive copy

Revert walks the events backwards (slot bytes are written back, archive
copies are restored) and finally restores the index copies. Walking backwards
keeps offsets valid even when a later session patched a rebuilt archive.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .archive import Chunk, Game
from .errors import NoFitError
from .mod import ApplyResult, Mod, apply_mods

BACKUP_DIR = "_wolfsdk_backup"
JOURNAL = "journal.json"


@dataclass
class PatchReport:
    written: List[str] = field(default_factory=list)
    rebuilt: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    plan: Optional[ApplyResult] = None


def _backup_dir(game: Game) -> str:
    return os.path.join(game.base, BACKUP_DIR)


def _load_journal(game: Game) -> dict:
    p = os.path.join(_backup_dir(game), JOURNAL)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "events": [], "indexes": []}


def _save_journal(game: Game, j: dict) -> None:
    with open(os.path.join(_backup_dir(game), JOURNAL), "w", encoding="utf-8") as f:
        json.dump(j, f, indent=1)


def _backup_index(game: Game, j: dict, chunk: Chunk) -> None:
    if chunk.index_name in j["indexes"]:
        return
    shutil.copy2(chunk.index_path, os.path.join(_backup_dir(game), chunk.index_name))
    j["indexes"].append(chunk.index_name)


def plan(game: Game, mods: List[Mod]) -> ApplyResult:
    def read_asset(t: str, n: str) -> Optional[str]:
        data = game.read_asset(t, n)
        return None if data is None else data.decode("utf-8")

    return apply_mods(mods, read_asset)


def apply(game: Game, mods: List[Mod]) -> PatchReport:
    """Apply ``mods`` to ``game``; every change is journaled for :func:`revert`."""
    os.makedirs(_backup_dir(game), exist_ok=True)
    j = _load_journal(game)
    report = PatchReport(plan=plan(game, mods))
    rebuilds: Dict[str, Dict[int, bytes]] = {}
    chunk_by_name = {c.index_name: c for c in game.chunks}
    for key, text in report.plan.results.items():
        typ, name = key.split(":", 1)
        data = text.encode("utf-8")
        if game.read_asset(typ, name) == data:
            report.unchanged.append(key)
            continue
        for occ in game.find(typ, name):
            _backup_index(game, j, occ.chunk)
            try:
                original = game.write_in_place(occ, data, pad=True)
            except NoFitError:
                rebuilds.setdefault(occ.chunk.index_name, {})[occ.entry.index] = data
                continue
            j["events"].append({
                "op": "slot",
                "resources": occ.chunk.resources_name,
                "offset": occ.entry.offset,
                "data": base64.b64encode(original).decode("ascii"),
            })
        report.written.append(key)
        _save_journal(game, j)
    for idx_name, repl in rebuilds.items():
        chunk = chunk_by_name[idx_name]
        copy = f"{chunk.resources_name}.{len(j['events'])}"
        shutil.copy2(chunk.resources_path, os.path.join(_backup_dir(game), copy))
        j["events"].append({"op": "resources", "resources": chunk.resources_name, "copy": copy})
        _save_journal(game, j)
        game.rebuild(chunk, repl)
        report.rebuilt.append(chunk.resources_name)
    _save_journal(game, j)
    return report


def revert(game_root: str) -> int:
    """Undo every journaled change. Returns the number of restored items."""
    bdir = os.path.join(game_root, "base", BACKUP_DIR)
    jp = os.path.join(bdir, JOURNAL)
    if not os.path.exists(jp):
        return 0
    with open(jp, encoding="utf-8") as f:
        j = json.load(f)
    base = os.path.join(game_root, "base")
    count = 0
    for ev in reversed(j.get("events", [])):
        target = os.path.join(base, ev["resources"])
        if ev["op"] == "resources":
            src = os.path.join(bdir, ev["copy"])
            shutil.copy2(src, target)
            os.remove(src)
        else:
            with open(target, "r+b") as f:
                f.seek(ev["offset"])
                f.write(base64.b64decode(ev["data"]))
        count += 1
    for name in j.get("indexes", []):
        shutil.copy2(os.path.join(bdir, name), os.path.join(base, name))
        os.remove(os.path.join(bdir, name))
        count += 1
    os.remove(jp)
    try:
        os.rmdir(bdir)
    except OSError:
        pass
    return count
