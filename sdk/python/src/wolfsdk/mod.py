"""``mod.json`` loading, layering and conflict detection (SPEC §7)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple, Union

from .decl import Decl
from .errors import DeclError, ModError

Value = Union[str, int, float, bool]


@dataclass
class AssetChange:
    key: str
    set: List[Tuple[str, Value]] = field(default_factory=list)
    file: Optional[str] = None

    @property
    def type(self) -> str:
        return self.key.split(":", 1)[0]

    @property
    def name(self) -> str:
        return self.key.split(":", 1)[1]


@dataclass
class Mod:
    name: str
    priority: int = 0
    game: Optional[str] = None
    version: Optional[str] = None
    description: Optional[str] = None
    assets: List[AssetChange] = field(default_factory=list)
    root: Optional[str] = None  # directory the mod was loaded from

    @classmethod
    def from_dict(cls, data: dict, default_name: str = "mod", root: Optional[str] = None) -> "Mod":
        if not isinstance(data, dict):
            raise ModError("mod.json must contain a JSON object")
        prio = data.get("priority", 0)
        if isinstance(prio, bool) or not isinstance(prio, (int, float)) or int(prio) != prio:
            raise ModError("priority must be an integer")
        game = data.get("game", data.get("spiel"))
        if game is not None and game not in ("tno", "tnc"):
            raise ModError(f"unknown game {game!r} (expected 'tno' or 'tnc')")
        if "assets" in data:
            raw_assets = data["assets"]
            if not isinstance(raw_assets, dict):
                raise ModError("assets must be an object")
        else:
            raw_assets = {k: v for k, v in data.items() if ":" in k}
        assets = []
        for key, change in raw_assets.items():
            if ":" not in key or key.startswith(":") or key.endswith(":"):
                raise ModError(f"asset key {key!r} must be 'type:name'")
            if not isinstance(change, dict):
                raise ModError(f"{key}: change must be an object")
            sets = change.get("set")
            file = change.get("file")
            if sets is None and file is None:
                raise ModError(f"{key}: needs 'set' or 'file'")
            if sets is not None and not isinstance(sets, dict):
                raise ModError(f"{key}: 'set' must be an object")
            if file is not None and not isinstance(file, str):
                raise ModError(f"{key}: 'file' must be a string")
            pairs = []
            for path, value in (sets or {}).items():
                if value is None or isinstance(value, (list, dict)):
                    raise ModError(f"{key}: value of {path!r} must be string, number or bool")
                pairs.append((path, value))
            assets.append(AssetChange(key, pairs, file))
        name = data.get("name") or default_name
        return cls(name, int(prio), game, data.get("version"), data.get("description"), assets, root)

    @classmethod
    def loads(cls, text: str, default_name: str = "mod", root: Optional[str] = None) -> "Mod":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ModError(f"invalid JSON: {e}") from None
        return cls.from_dict(data, default_name, root)

    @classmethod
    def load(cls, path: str) -> "Mod":
        """Load ``mod.json`` (or a directory containing one)."""
        if os.path.isdir(path):
            path = os.path.join(path, "mod.json")
        root = os.path.dirname(os.path.abspath(path))
        with open(path, encoding="utf-8") as f:
            return cls.loads(f.read(), os.path.basename(root), root)

    def read_file(self, rel: str) -> str:
        if self.root is None:
            raise ModError(f"{self.name}: cannot resolve file {rel!r} without a mod directory")
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            return f.read()


@dataclass
class Conflict:
    kind: str  # "path", "file", "file-over-set"
    asset: str
    path: Optional[str]
    mods: List[str]
    winner: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "asset": self.asset, "path": self.path, "mods": self.mods, "winner": self.winner}


@dataclass
class ModErrorEntry:
    asset: str
    mod: str
    message: str

    def to_dict(self) -> dict:
        return {"asset": self.asset, "mod": self.mod, "message": self.message}


@dataclass
class ApplyResult:
    results: Dict[str, str]
    conflicts: List[Conflict]
    errors: List[ModErrorEntry]


def sort_mods(mods: List[Mod]) -> List[Mod]:
    return sorted(mods, key=lambda m: m.priority)  # stable


def apply_mods(
    mods: List[Mod],
    read_asset: Callable[[str, str], Optional[str]],
    read_file: Optional[Callable[[Mod, str], str]] = None,
) -> ApplyResult:
    read_file = read_file or (lambda m, rel: m.read_file(rel))
    ordered = sort_mods(mods)
    per_asset: Dict[str, List[Tuple[Mod, AssetChange]]] = {}
    for m in ordered:
        for ch in m.assets:
            per_asset.setdefault(ch.key, []).append((m, ch))

    results: Dict[str, str] = {}
    conflicts: List[Conflict] = []
    errors: List[ModErrorEntry] = []

    for key, changes in per_asset.items():
        # conflicts
        file_mods = [m.name for m, ch in changes if ch.file is not None]
        if len(file_mods) > 1:
            conflicts.append(Conflict("file", key, None, file_mods, file_mods[-1]))
        path_mods: Dict[str, List[str]] = {}
        for m, ch in changes:
            for p, _ in ch.set:
                lst = path_mods.setdefault(p, [])
                if m.name not in lst:
                    lst.append(m.name)
        for p, names in path_mods.items():
            if len(names) > 1:
                conflicts.append(Conflict("path", key, p, names, names[-1]))
        for i, (m, ch) in enumerate(changes):
            if ch.file is None:
                continue
            setters = [pm.name for pm, pch in changes[:i] if pch.set and pm is not m]
            if setters:
                conflicts.append(Conflict("file-over-set", key, None, setters + [m.name], m.name))

        typ, name = key.split(":", 1)
        text = read_asset(typ, name)
        if text is None:
            errors.append(ModErrorEntry(key, changes[0][0].name, "asset not found"))
            continue
        for m, ch in changes:
            if ch.file is not None:
                try:
                    text = read_file(m, ch.file)
                except (OSError, ModError) as e:
                    errors.append(ModErrorEntry(key, m.name, f"cannot read {ch.file}: {e}"))
                    continue
            if not ch.set:
                continue
            try:
                decl = Decl(text)
            except DeclError as e:
                errors.append(ModErrorEntry(key, m.name, str(e)))
                continue
            for p, v in ch.set:
                try:
                    decl.set(p, v)
                except DeclError as e:
                    errors.append(ModErrorEntry(key, m.name, str(e)))
            text = decl.text
        results[key] = text
    return ApplyResult(results, conflicts, errors)
