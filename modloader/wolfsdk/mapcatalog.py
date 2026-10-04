"""Which Wolfenstein II maps exist, and which archives each one loads.

    maps(game_root)          every map named by a packagemapspec.json
    mount(game_root, map_id) the ordered archive set that map loads

The mount set is read from the game's own data, never hardcoded per map
(docs/patchvorrang.md, re_probes/agent_reports/{dlc_primary,map_selection}.md):

  base/packagemapspec.json and dlc/dlc_<n>/base/packagemapspec.json name each
  map with chunkid (primary container), basechunkid (0 = none, 6 =
  chunkbase_6), refchunks (DLC only: extra dependencies) and one patchlevel
  per file. chunkid 0 is `common` = gameresources.

Precedence, first archive wins:

  1. patch layers, highest first. patch_<n>_chunkbase_<b> up to the map's own
     patchlevel, patch_<n> up to base/'s (they patch `common`, which every map
     mounts). Missing layers are skipped, as the engine does -- the retail
     DLCs announce patchlevel 2/3 and ship none. Patches go first because a
     patch copy is newer in 1635 of 1680 collisions and never older, and
     because patches carry newer .entities (c02p0 in patch_1, submarine in
     patch_1..3).
  2. chunk_<chunkid> (the map's folder first, then base/), 3. chunkbase_<b>,
  4. refchunks, 5. gameresources.

.texdb files are the same stems, where present, in the same order.

dlc_1..3 are retail; any other dlc_<n> is the user's own and lands in group
"Custom". A name already defined by an earlier spec keeps its first
definition (which one the engine picks is unmeasured, map_selection.md).

Read-only on the game install. stdlib + wolfsdk (idcl, oodle, tnccrypt).
"""

import hashlib
import json
import re
import string
import struct
import threading
from pathlib import Path

from . import oodle, tnccrypt
from .idcl import Archive

RETAIL_DLC = ("dlc_1", "dlc_2", "dlc_3")
GROUPS = ("Campaign", "DLC", "Other", "Custom")   # Studio-facing, English


def _chunk(folder, base, cid):
    if cid == 0:
        return base / "gameresources.resources"
    own = folder / ("chunk_%d.resources" % cid)
    return own if own.exists() else base / ("chunk_%d.resources" % cid)


def _plan(game_root):
    """map id -> (group, [archive paths in precedence order])."""
    root = Path(game_root)
    base = root / "base"
    spec = json.loads((base / "packagemapspec.json").read_text(encoding="utf-8"))
    base_level = spec.get("patchinfo", {}).get("patchlevel", 0)
    specs = [(base, "base", spec)]
    dlcs = sorted((root / "dlc").glob("dlc_*/base/packagemapspec.json"),
                  key=lambda p: int(p.parent.parent.name[4:]) if p.parent.parent.name[4:].isdigit()
                  else 1 << 30)
    for p in dlcs:
        try:
            specs.append((p.parent, p.parent.parent.name, json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            if p.parent.parent.name in RETAIL_DLC:
                raise
            # a broken user-made spec must not hide the other maps
    out = {}
    for folder, owner, sp in specs:
        user = owner != "base" and owner not in RETAIL_DLC
        try:
            level = sp.get("patchinfo", {}).get("patchlevel", 0)
            entries = list(sp.get("maps", []))
        except (AttributeError, TypeError):
            if not user:
                raise
            continue
        for m in entries:
            try:
                name = m["name"]
                if m.get("isbasechunk") or name in out or "/" not in name:
                    continue
                b = m.get("basechunkid", 0)
                paths = []
                for n in range(max(level, base_level), 0, -1):
                    if b and n <= level:
                        paths.append(base / ("patch_%d_chunkbase_%d.resources" % (n, b)))
                    if n <= base_level:
                        paths.append(base / ("patch_%d.resources" % n))
                paths.append(_chunk(folder, base, m.get("chunkid", 0)))
                if b:
                    paths.append(base / ("chunkbase_%d.resources" % b))
                paths += [_chunk(folder, base, r) for r in m.get("refchunks", [])]
            except (AttributeError, KeyError, TypeError, ValueError):
                if not user:
                    raise
                continue            # malformed user-made entry
            paths.append(base / "gameresources.resources")
            seen = set()
            paths = [p for p in paths if p.exists() and not (p in seen or seen.add(p))]
            if user:
                group = "Custom"
            elif name.startswith("game/dlc/"):
                group = "DLC"
            elif name.startswith("game/wolf/") and "/bonusmaps/" not in name:
                group = "Campaign"
            else:
                group = "Other"
            out[name] = (group, paths)
    return out


class Mount:
    """An ordered archive set. find() answers like the engine's first-wins lookup.

    Read payloads through read(): it is thread-safe, while an Archive shares
    one file handle (seek + read) between callers."""

    def __init__(self, paths, game_root=None):
        self.game_root = game_root
        self.archives = []
        try:
            for p in paths:
                self.archives.append(Archive(p))
        except Exception:
            self.close()
            raise
        self.texdbs = [a.path.with_suffix(".texdb") for a in self.archives
                       if a.path.with_suffix(".texdb").exists()]
        self._index = None
        self._lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        for a in self.archives:
            a.close()

    def _idx(self):
        if self._index is None:
            self._index = {}
            for a in self.archives:
                for e in a.entries:
                    self._index.setdefault((e.type, e.name), (a, e))
        return self._index

    def find(self, type_, name):
        """(archive, entry) of the first archive holding type_:name, or None.
        Retail names are all lower case (218167/218167), so the query is folded."""
        return self._idx().get((type_, name.lower().replace("\\", "/")))

    def entries(self, type_):
        """Every (archive, entry) of this type the set resolves to, one per name."""
        return [hit for (t, _), hit in self._idx().items() if t == type_]

    def read(self, archive, entry):
        """The entry's payload, Kraken undone (mode 4: 12-byte prefix first)."""
        with self._lock:
            raw = archive.read_raw(entry)
        if entry.compression == 0:
            return raw
        oo = oodle.load(self.game_root)
        return oo.decompress(raw[12:] if entry.compression == 4 else raw, entry.usize)

    @property
    def key(self):
        """Changes whenever any mounted file changes size or mtime (cache key)."""
        h = hashlib.sha1()
        for p in [a.path for a in self.archives] + self.texdbs:
            st = p.stat()
            h.update(("%s|%d|%d\n" % (p, st.st_size, st.st_mtime_ns)).encode("utf-8"))
        return h.hexdigest()[:16]


def mount(game_root, map_id):
    plan = _plan(game_root)
    if map_id not in plan:
        raise KeyError("Unknown map: %s" % map_id)
    return Mount(plan[map_id][1], game_root)


# --------------------------------------------------------------------------
# titles: chapter (chapter <- chaptervariation.map) + location string
# --------------------------------------------------------------------------

def _q(key, text):
    return re.findall(r'\b%s\s*=\s*"([^"]*)"' % key, text)


def _pretty(s):
    return string.capwords(s.lower()) if s.isupper() else s


def _titles(game_root, ids):
    base = Path(game_root) / "base"
    level = json.loads((base / "packagemapspec.json").read_text(encoding="utf-8")) \
        .get("patchinfo", {}).get("patchlevel", 0)
    common = [base / ("patch_%d.resources" % n) for n in range(level, 0, -1)]
    common = [p for p in common if p.exists()] + [base / "gameresources.resources"]
    with Mount(common, game_root) as m:
        def text(hit):
            return m.read(*hit).decode("utf-8", "replace") if hit else ""
        lang = {}
        hit = m.find("cfile", "strings/english.lang")
        if hit:
            try:
                t = tnccrypt.cfile_decrypt(m.read(*hit), hit[1].name).decode("utf-8-sig", "replace")
                lang = {k.lower(): v for k, v in re.findall(r'^\s*"(#[^"]+)"\s+"([^"]*)"', t, re.M)}
            except (tnccrypt.TncCryptError, oodle.OodleError):
                pass            # titles fall back to the map code
        chapter_of = {}        # chaptervariation -> chapter displayName key
        for hit in m.entries("chapter"):
            t = text(hit)
            for v in re.findall(r'item\[\d+\]\s*=\s*"([^"]*)"', t):
                chapter_of.setdefault(v, (_q("displayName", t) or [""])[0])
        chapter = {}           # map -> (campaign variation first, chapter key)
        for hit in m.entries("chaptervariation"):
            name = hit[1].name
            for mp in _q("map", text(hit)):
                cand = ("campaign" not in name, name)
                if name in chapter_of and cand < chapter.get(mp, (True, "￿", ""))[:2]:
                    chapter[mp] = cand + (chapter_of[name],)
        out = {}
        for mid in ids:
            leaf = mid.rsplit("/", 1)[-1]
            chap = lang.get(chapter.get(mid, (0, 0, ""))[2].lower(), "")
            loc = ""
            for key in (["#str_location_%s_name" % leaf]
                        + _q("headingText", text(m.find("tutorialEvent", "location/" + leaf)))
                        + _q("locationName", text(m.find("extralocation", "dlc/" + leaf)))):
                loc = lang.get(key.lower(), "")
                if loc:
                    break
            parts = [_pretty(s) for s in (chap, loc) if s]
            out[mid] = "%s (%s)" % (" – ".join(parts), leaf) if parts else leaf
    return out


def maps(game_root):
    """[{id, title, group, entities: {archive, entry} | None, entities_bytes}],
    sorted by group (Campaign, DLC, Other, Custom), then id.

    entities is None when no mounted archive holds maps/<id>.entities (the
    installed dlc_4 is a copy of dlc_2's chunk_22 without c2v1_custom's);
    the title then says so. entities_bytes is the unpacked text size."""
    plan = _plan(game_root)
    titles = _titles(game_root, plan)
    opened, out = {}, []
    try:
        for mid, (group, paths) in plan.items():
            name = "maps/%s.entities" % mid
            src, size = None, 0
            for p in paths:
                if p not in opened:
                    opened[p] = Archive(p)
                hits = opened[p].find("compfile", name)
                if hits:
                    with open(p, "rb") as f:
                        f.seek(hits[0].offset)
                        size = abs(struct.unpack("<q", f.read(8))[0])
                    src = {"archive": str(p), "entry": name}
                    break
            title = titles[mid] if src else "%s (no .entities in the container)" % mid.rsplit("/", 1)[-1]
            out.append({"id": mid, "title": title, "group": group,
                        "entities": src, "entities_bytes": size})
    finally:
        for a in opened.values():
            a.close()
    out.sort(key=lambda m: (GROUPS.index(m["group"]), m["id"]))
    return out
