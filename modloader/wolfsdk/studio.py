"""The Studio's assets: catalogs and playable content of both games.

    st = Studio({"tnc": root, "tno": root})   a missing root is looked up (config, Steam)
    st.games()                                 which game has which section
    st.listing(game, kind, q, group, lang, offset, limit, facets)
    st.info(game, kind, id)
    st.frames(game, id, start, count, width, seek) -> FrameStream (WVF1, see below)
    st.video_png(game, id, n, width)           one frame as PNG
    st.video_wav(game, id, track_ids)          audio track(s) as WAV
    st.sound(game, id)                         (mime, bytes) as wwise/tnoaudio decode it
    st.texture_png(game, id, max_side)         PNG
    st.text(game, id)                          str
    st.text_file(game, id)                     the text's bytes as a file (TNC: decrypted/unpacked)
    st.export_file(game, kind, id)             (file path, bytes or Path): one item in its natural format
    st.export_rows(game, kind, q, group, lang) the hits a ZIP export takes, or TooLarge (413)
    st.model_mesh(game, id, lod)               WMD1 bytes (wolfsdk/md6.py)
    st.model_albedo(game, texture id, max)     PNG of a model surface's colour map
    st.model_uv2(game, id, surface)            f32[2nv]: a hair surface's second uv set (md6.uv2)
    st.model_pbr(game, material)               {alpha, roughness, emissive, maps: {slot: URL}} (TNC, pbrmat)
    st.model_pbr_png(game, material, slot)     PNG of one of those maps

kind is videos | sounds | textures | texts | models, game is tnc | tno. Ids are catalog
keys, never paths: a request can only name what a catalog lists, so nothing
outside the installs is reachable. Read-only on both installs.

Everything decoding goes through the verified modules: bink (videos),
wwise/tnoaudio (sounds), tncimage/tncview/tnccrypt/oodle (TNC textures and
texts), resources/bim/image (TNO textures and texts, as the tkinter browser
reads them).

WVF1, the frame stream of frames(): a 32-byte header, little endian

    4s magic "WVF1"  u16 width  u16 height  u32 first frame  u32 frame count
    u32 frames in the video  u32 fps numerator  u32 fps denominator
    u32 requested start

then `count` frames of width*height*4 bytes RGBA (alpha 255), frame `first`
first. Nearest-neighbour downscaled by an integer step so the width is at
most the requested one (bink.Video.frame(n, step), bytes slicing).

Seeking: a Bink frame depends on every frame back to the previous keyframe,
and keyframes are sparse (median gap 1 200-1 600 frames, up to 14 700).
seek=exact decodes from the keyframe (measured 520-980 frames/s at
1920 wide), seek=key starts at the keyframe itself (instant), seek=auto is exact
while that costs at most AUTO_EXACT frames and snaps to the keyframe
otherwise. The header says where the stream really starts.

Poster: many cutscenes open on black and some keep their only keyframe
there, so the poster is the frame nearest a third in whose picture has
contrast (luma spread >= POSTER_SPREAD): the keyframes first (instant), then
one frame a second from the start (decoded on, POSTER_SCAN frames at most,
up to ~1.5 s once per video). A video flat throughout gets its least flat frame.

Models (TNC only; TNO answers Unsupported): one row per model path under
models/, the first archive's entry of each (variants that differ only in
their $-suffix are left out). md6mesh through wolfsdk/md6.py (skinned, bind
pose, LOD 0-2), .lwo through mapgeo.model_surfaces (static, LOD 0; its
normals are read at 0x14 of the vertex block that function took the
positions from). Normals the triangles contradict go out as 0 (md6.fix_normals).
A figure's variant meshes (gore, gear, spare heads) come with visible: false
as its md6Def's meshKits hide them at start (see _kits). A material's colour
map comes from maptex over one Mount of every archive (first wins); its PNGs
are served at models/albedo.png, the URL carrying the Mount's key (v=): a
skin changes pixels but neither the texture id nor the image's data hash,
so maptex's own id + data hash cache name would serve the old picture.
The catalog is rebuilt when an archive or a .texdb next to one changes, and
info and mesh.bin always come from the same decode. Rows get tris/surfaces
once the model was opened (null before: counting them all would decode 1.3 GB).

Errors are German: Missing (404), BadRequest (400), Unsupported (422: the
asset exists but cannot be shown, e.g. a BC6H texture or a binary "text").
"""

import hashlib
import json
import math
import os
import re
import struct
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from array import array
from collections import Counter, OrderedDict
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from . import (bink, cutsceneaudio, decl, idcl, image, kiscule, mapcatalog, mapgeo, maptex, md6, md6anim, oodle, pbrmat, tnccrypt,
               ybmesh,
               tncimage, tncview, tnoaudio, wwise)
from .bim import BimError, parse as parse_bim
from .resources import ResourceError, load_master, Archive as TnoArchive

KINDS = ("videos", "sounds", "textures", "texts")
MODELS = "models"         # not in KINDS: KINDS is what every section of both games has
SCRIPTS = "scripts"       # likewise (TNC only): the Kiscule level scripts, edited as node graphs
ALBEDO_MAX = 1024         # side of the colour maps info() links to
MODEL_KEEP = 3            # decoded models held for info + mesh.bin (the largest payload is ~25 MB)
MODEL_GROUPS = (("models/weapons/", "Weapons"), ("models/characters/player/", "Player"),
                ("models/characters/", "Characters and enemies"), ("models/robots/", "Robots"),
                ("models/animals/", "Animals"), ("models/vehicles/", "Vehicles"), ("models/", "Props"))
NAMES = {"tnc": "Wolfenstein II: The New Colossus", "tno": "Wolfenstein: The New Order",
         "yb": "Wolfenstein: Youngblood"}
IDT6 = ("tnc", "yb")      # idTech 6 archives (idcl): Youngblood is read with the Wolfenstein II readers, read only
AUTO_EXACT = 1500         # frames an automatic seek may decode (measured 520-770 fps: ~2-3 s)
POSTER_SPREAD = 20        # luma standard deviation below which a still reads as flat (black, white, a fade)
POSTER_SCAN = 1200        # frames a poster search may decode past the keyframes (~1.5-2 s)
MAX_LIMIT = 1000
CACHE_BYTES = 768 << 20   # decoded sounds, WAVs and PNGs kept for Range requests
NO_TRACK = ("This video has no audio track: cutscene audio lives in the game's "
            "sound banks (Wwise), not in the video.")

# Types whose payload is binary (surveyed on both installs: 0 of 12 samples
# text); every other type is a decl or text file. A text row that turns out
# binary anyway answers Unsupported.
BINARY = {
    "tnc": {"anim", "animEvent", "baseModel", "cgr", "cm", "decalatlas", "destructionmodeldata",
            "extkisclule", "fga", "font", "geistanim", "geistanimpara", "geistidentity",
            "glassmodel", "hkcloth", "hknavmesh", "image", "json", "lightatlas",
            "mannequinmodel", "model", "modelstream", "prtmeshdist", "pvs", "renderProgResource",
            "skeleton", "staticParticleModel", "staticShadowGeom", "staticStreamTree",
            "transsortatlas", "voicetrack"},
    "tno": {"aas", "anim", "atlas", "baseModel", "cg", "cm", "cuttableResource",
            "discreteAnimation", "foliageModel", "font", "glassModel", "image",
            "jointconversion", "model", "morphVertices", "prtMeshDist", "renderProg", "sample",
            "skeleton", "staticParticleModel", "video", "voicetrack"},
}
BINARY["yb"] = BINARY["tnc"] | {"extkiscule", "rs_emb_sfile", "umbratome", "hknavvolume", "havokcompendium"}
# ponytail: TNC's list plus the binary types new in Youngblood that r14_youngblood names; any other answers Unsupported when opened
SCRIPT_TYPE = {"tnc": kiscule.TYPE, "yb": "extkiscule"}   # Youngblood fixed the type name's typo
TEXT_FILES = (".lang", ".txt", ".cfg", ".json", ".def", ".decl")  # of the mixed type 'file'


# Material maps are pure-Python decodes (~1.5 s a material, cold): a few worker processes build them in
# parallel, one Mount each (ponytail: ~150 MB per worker; more workers when a model has many materials).
PBR_WORKERS = max(1, min(4, (os.cpu_count() or 2) - 2))
_worker_mount = None


def _pbr_init(paths, root):
    global _worker_mount
    _worker_mount = mapcatalog.Mount(paths, root)


def _pbr_job(material):
    return pbrmat.build(_worker_mount, material)


def _md6_skin(m):
    """(slot bytes u8[4 nv], weights f32[4 nv]) of a Wolfenstein II mesh's LOD 0: the skinvmtr formula on kind 1
    (w1 = (tangent.b3 & 127)/254, w2 = (normal.b3 >> 4)/45, w3 = (normal.b3 & 15)/60, w0 = rest, r13_skinwrite.md);
    hair cards and meshes without tangents follow slot 0."""
    lo = m["lods"][0]
    if lo is None:
        return None
    v, nv = lo["verts"], lo["nv"]
    slots, weights = bytearray(4 * nv), array("f", bytes(16 * nv))
    for k in range(4):
        slots[k::4] = v[0x1C + k::48]
    if m["kind"] == 1:
        for i in range(nv):
            n3, t3 = v[48 * i + 0x17], v[48 * i + 0x1B]
            w1, w2, w3 = (t3 & 127) / 254, (n3 >> 4) / 45, (n3 & 15) / 60
            weights[4 * i:4 * i + 4] = array("f", (max(0.0, 1 - w1 - w2 - w3), w1, w2, w3))
    else:
        weights[0::4] = array("f", [1.0]) * nv
    return bytes(slots), weights


class StudioError(Exception):
    status = 500


class Missing(StudioError, LookupError):
    status = 404


class BadRequest(StudioError, ValueError):
    status = 400


class Unsupported(StudioError):
    status = 422


class TooLarge(StudioError):
    status = 413


class Conflict(StudioError):
    status = 409


# A ZIP of every hit: (items, bytes by the rows' sizes) at most, None = no limit.
# Textures are decoded at full size on the way (4096 px: up to ~7 s and ~9 MB each).
ZIP_LIMITS = {"textures": (50, None), "texts": (5000, None), "sounds": (500, 1 << 30), "videos": (None, 2 << 30)}


# -- small helpers ---------------------------------------------------------------

class _Cache:
    """Bytes by key, least recently used out past `budget`; one make() per key
    at a time (the <audio> element and Web Audio ask for the same sound)."""

    def __init__(self, budget):
        self.budget, self.used = budget, 0
        self._d, self._busy = OrderedDict(), {}
        self._lock = threading.Lock()

    def get(self, key, make):
        while True:
            with self._lock:
                if key in self._d:
                    self._d.move_to_end(key)
                    return self._d[key]
                ev = self._busy.get(key)
                if ev is None:
                    ev = self._busy[key] = threading.Event()
                    break
            ev.wait()            # someone else makes it; then look again
        try:
            value = make()
        finally:
            with self._lock:
                del self._busy[key]
            ev.set()
        size = len(value[1]) if isinstance(value, tuple) else len(value)
        with self._lock:
            if size <= self.budget // 2:
                self._d[key] = value
                self.used += size
                while self.used > self.budget:
                    _k, old = self._d.popitem(last=False)
                    self.used -= len(old[1]) if isinstance(old, tuple) else len(old)
        return value


def _stamp(paths):
    out = []
    for p in paths:
        try:
            st = p.stat()
            out.append((str(p), st.st_size, st.st_mtime_ns))
        except OSError:
            out.append((str(p), -1, -1))
    return tuple(out)


def _split(path):
    """(folder, leaf) of a slash path."""
    folder, _, leaf = path.rpartition("/")
    return folder, leaf


def _tex_path(name):
    """The file path inside an image name: TNC 'a/b.tga$mtlkind=albedo',
    TNO ' mipbias(-1) uncompressed a/b.tga' or 'shrink( a/b.tga, 512)'."""
    name = name.split("$", 1)[0]
    parts = [p.strip("(),") for p in name.split() if "/" in p]
    return parts[0] if len(parts) == 1 else name.strip()


def _group_key(group):
    """Folders by name; the pseudo folders '(no folder)', '(main folder)' last."""
    return group.startswith("("), group.lower()


def _luma_spread(rgba):
    """Standard deviation of the luma of RGBA bytes."""
    lum = [(2 * r + 5 * g + b) >> 3 for r, g, b in zip(rgba[0::4], rgba[1::4], rgba[2::4])]
    m = sum(lum) / len(lum)
    return max(0.0, sum(v * v for v in lum) / len(lum) - m * m) ** 0.5


class _Catalog:
    """Rows sorted by (group, name), each with id/name/group/lang, searchable."""

    def __init__(self, rows, extra=None):
        rows.sort(key=lambda r: (_group_key(r["group"]), r["name"].lower(), r["id"]))
        self.rows = rows
        self.by_id = {r["id"]: r for r in rows}
        self.hay = [("%s %s %s" % (r["id"], r["name"], r["group"])).lower() for r in rows]
        self.extra = extra or {}     # id -> private data (paths, entries), never sent


# -- the studio ------------------------------------------------------------------

class Studio:
    def __init__(self, roots=None):
        self._given = {k: Path(v) for k, v in (roots or {}).items() if v}
        self._roots = None
        self._cats, self._stamps = {}, {}
        self._build_locks = {}
        self._lock = threading.Lock()
        self._read_lock = threading.Lock()   # ponytail: one lock for all archive reads, per-archive if it ever matters
        self._held = {}                      # game -> (stamp, [idcl.Archive] or [resources.Archive])
        self._texdbs = {}                    # idTech 6 game -> TexdbSet
        self.cache = _Cache(CACHE_BYTES)
        self._posters = {}                   # (game, video id) -> frame
        self._models = OrderedDict()         # model id -> (catalog, info, surfaces, {lod: WMD1}), the last MODEL_KEEP
        self._skels = {}                     # (catalog token, md6skl name) -> md6anim.skeleton()
        self._pool, self._pool_key = None, None   # material-map workers, made for one archive set

    # -- games ------------------------------------------------------------------

    def roots(self):
        if self._roots is None:
            found = {}
            if any(k not in self._given for k in NAMES):
                from .game import YOUNGBLOOD, Game, find_root
                try:
                    found = {g.title.key: g.root for g in Game.find_all()}
                    if "yb" not in self._given:
                        found["yb"] = find_root(YOUNGBLOOD)
                except Exception:  # noqa: BLE001 -- a broken config must not hide the given root
                    found = {}
            self._roots = {k: self._given.get(k) or found.get(k) for k in NAMES}
        return self._roots

    def root(self, game):
        if game not in NAMES:
            raise Missing("Unknown game: %s (tnc, tno or yb)" % game)
        r = self.roots()[game]
        if r is None or not r.is_dir():
            raise Missing("%s was not found." % NAMES[game])
        return r

    def games(self):
        out = []
        for key, name in NAMES.items():
            r = self.roots()[key]
            ok = r is not None and r.is_dir()
            full = ok and key != "yb"           # Youngblood so far: texts, scripts, textures, models and videos
            out.append({"id": key, "name": name, "available": ok, "root": str(r) if ok else None,
                        "sections": {"maps": full, "videos": ok, "sounds": full,
                                     "textures": ok, "texts": ok, "models": ok and key in IDT6, "scripts": ok and key in IDT6}})
        return out

    def close(self):
        for _stamp_, arcs in self._held.values():
            for a in arcs:
                a.close()
        self._held = {}
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = self._pool_key = None
        for g in IDT6:
            cat = self._cats.pop((g, MODELS), None)
            if cat is not None:
                cat.mount.close()

    # -- catalogs ---------------------------------------------------------------

    def catalog(self, game, kind):
        root = self.root(game)
        if kind not in KINDS + (MODELS, SCRIPTS):
            raise Missing("Unknown asset kind: %s (%s)" % (kind, ", ".join(KINDS + (MODELS, SCRIPTS))))
        key = (game, kind)
        with self._lock:
            lock = self._build_locks.setdefault(key, threading.Lock())
        with lock:                     # one build per catalog; other catalogs go on
            stamp = self._catalog_stamp(game, kind, root)
            if self._stamps.get(key) != stamp or key not in self._cats:
                make = getattr(self, "_%s_%s" % (kind, game), None)
                if make is None:
                    raise Unsupported("%s: %s are not supported yet." % (NAMES[game], kind))
                self._cats[key] = make(root)
                self._stamps[key] = stamp
            return self._cats[key]

    def _catalog_stamp(self, game, kind, root):
        if kind == "videos":
            return None                       # a glob per request costs more than it saves
        if kind == "sounds":
            return None                       # wwise/tnoaudio keep their own index per process
        stamp, paths = self._archive_paths(game, root)
        if kind == MODELS and game in IDT6:   # a skin changes blocks in the .texdb, not only the archive
            return stamp + _stamp([p.with_suffix(".texdb") for p in paths])
        return stamp

    def _archive_paths(self, game, root):
        base = root / "base"
        if game in IDT6:
            # patches first (newest copy wins), then base/, then the DLC folders
            patches = sorted(base.glob("patch_*.resources"),
                             key=lambda p: -int(re.match(r"patch_(\d+)", p.name).group(1)))
            rest = [p for p in sorted(base.glob("*.resources")) if p not in patches]
            paths = patches + rest + sorted(root.glob("dlc/*/base/*.resources"))
            return _stamp(paths), paths
        pairs = load_master(base)
        paths = [base / "master.index"] + [base / n for pair in pairs for n in pair]
        return _stamp(paths), pairs

    def _archives(self, game, root):
        stamp, paths = self._archive_paths(game, root)
        with self._lock:
            held = self._held.get(game)
            if held and held[0] == stamp:
                return held[1]
        if game in IDT6:
            arcs = [idcl.Archive(p) for p in paths]
        else:
            base = root / "base"
            arcs = [TnoArchive(base / i, base / r) for i, r in paths]
        with self._lock:
            old = self._held.get(game)
            self._held[game] = (stamp, arcs)
            if game in IDT6:
                self._texdbs[game] = tncimage.TexdbSet.for_game(root)
        for a in (old or (None, []))[1]:
            a.close()                  # an archive a running read holds reopens nothing: reads fail loudly
        return arcs

    # videos

    def _videos(self, root, dirs):
        rows, extra = [], {}
        for d in dirs:
            for p in sorted(d.rglob("*")):
                if p.suffix.lower() not in (".bk2", ".bik") or not p.is_file():
                    continue
                vid = p.relative_to(root).as_posix()
                try:
                    h = bink.read_header(p)
                except (bink.BinkError, OSError) as exc:
                    h = {"error": str(exc)}
                rel = p.relative_to(d).parent.as_posix()
                dlc = p.relative_to(root).parts[1] if p.relative_to(root).parts[0] == "dlc" else ""
                group = "/".join(x for x in (dlc, "" if rel == "." else rel) if x) or "(main folder)"
                row = {"id": vid, "name": p.stem, "group": group, "lang": None, "size": p.stat().st_size}
                if "error" in h:
                    row["error"] = h["error"]
                else:
                    fps = h["fps_num"] / h["fps_den"]
                    row.update(width=h["width"], height=h["height"], fps=round(fps, 3),
                               frames=h["frames"], duration=round(h["frames"] / fps, 3),
                               tracks=len(h["tracks"]), has_audio=bool(h["tracks"]))
                rows.append(row)
                extra[vid] = (p, h)
        return _Catalog(rows, extra)

    def _videos_tnc(self, root):
        return self._videos(root, [root / "base" / "bink"] + sorted(root.glob("dlc/*/base/bink")))

    def _videos_yb(self, root):
        return self._videos(root, [root / "base" / "bink"])

    def _videos_tno(self, root):
        return self._videos(root, [root / "base" / "bink"])

    # sounds

    def _sounds_tnc(self, root):
        rows, every = [], wwise.sounds(root)
        for r in every:
            if r["codec"] not in ("vorbis", "pcm"):
                continue     # 'other' = plugin media (reverb impulse responses), not sounds
            bank = PurePosixPath(r["bank"]).stem if r["bank"] else "Streams without bank"
            rows.append({"id": r["id"], "name": str(r["media_id"]), "group": "%s/%s" % (r["scope"], bank),
                         "lang": None if r["lang"] == "sfx" else r["lang"], "codec": r["codec"],
                         "channels": r["channels"], "sample_rate": r["sample_rate"],
                         "duration": round(r["duration"], 3), "size": r["size"],
                         "channel_order": r["channel_order"]})
        return _Catalog(rows, {r["id"]: r for r in every})

    def _sounds_tno(self, root):
        rows, extra = [], {}
        for r in tnoaudio.sounds(root):
            extra[r["id"]] = r
            if r["alias_of"] is not None:
                continue     # the same stream as its sample; reachable by id, not listed
            folder, leaf = _split(r["id"])
            rows.append({"id": r["id"], "name": leaf[:-4] if leaf.endswith(".wav") else leaf,
                         "group": folder, "lang": r["lang"] or None, "codec": "vorbis",
                         "channels": r["channels"], "sample_rate": r["sample_rate"],
                         "duration": round(r["duration"], 3), "size": r["size"], "channel_order": None})
        return _Catalog(rows, extra)

    # textures and texts

    def _entries(self, game, root, want):
        """(archive, entry) per name for the entries want(type, name) accepts, first archive wins."""
        seen, out = set(), []
        for a in self._archives(game, root):
            for e in a.entries:
                if want(e.type, e.name) and (e.type, e.name) not in seen:
                    seen.add((e.type, e.name))
                    out.append((a, e))
        return out

    def _where(self, game, root, a):
        p = a.path if game in IDT6 else a.resources_path
        return p.relative_to(root).as_posix()

    def _textures(self, game, root):
        rows, extra = [], {}
        for a, e in self._entries(game, root, lambda t, n: t == "image"):
            folder, leaf = _split(_tex_path(e.name))
            rows.append({"id": e.name, "name": leaf or e.name, "group": folder or "(no folder)",
                         "lang": None, "size": e.usize})
            extra[e.name] = (a, e)
        return _Catalog(rows, extra)

    def _textures_tnc(self, root):
        return self._textures("tnc", root)

    def _textures_yb(self, root):
        return self._textures("yb", root)

    def _textures_tno(self, root):
        return self._textures("tno", root)

    def _texts(self, game, root):
        binary = BINARY[game]

        def want(t, n):
            if t == "file":
                return n.lower().endswith(TEXT_FILES)
            return t not in binary

        rows, extra = [], {}
        for a, e in self._entries(game, root, want):
            tid = "%s:%s" % (e.type, e.name)
            m = re.fullmatch(r"strings/(\w+)\.(?:lang|json)", e.name)   # .json: Youngblood's plain strings
            rows.append({"id": tid, "name": e.name, "group": "strings" if m else e.type,
                         "lang": m.group(1) if m else None, "size": e.usize, "type": e.type})
            extra[tid] = (a, e)
        return _Catalog(rows, extra)

    def _texts_tnc(self, root):
        return self._texts("tnc", root)

    def _texts_tno(self, root):
        return self._texts("tno", root)

    def _texts_yb(self, root):
        return self._texts("yb", root)

    # scripts

    def _scripts_tnc(self, root):
        return self._scripts("tnc", root)

    def _scripts_yb(self, root):
        return self._scripts("yb", root)

    def _scripts(self, game, root):
        rows, extra = [], {}
        for a, e in self._entries(game, root, lambda t, n: t == SCRIPT_TYPE[game]):
            m = re.fullmatch(r"(?:generated/decls/)?maps/game/(.*)/kiscules/([^/]+)\.decl", e.name)
            rows.append({"id": e.name, "name": m.group(2) if m else e.name, "group": m.group(1) if m else "shared",
                         "lang": None, "size": e.usize, "nodes": None, "edges": None, "missions": None,
                         "editable": game == "tnc" and "dlc" not in a.path.relative_to(root).parts})
            extra[e.name] = (a, e)
        return _Catalog(rows, extra)

    def _scripts_tno(self, root):
        raise Unsupported("Scripts are not supported for The New Order: its levels are scripted in another format.")

    def script_graph(self, game, sid):
        """(Doc, Graph) of one script, from its payload as the loader will read it."""
        cat = self.catalog(game, SCRIPTS)
        if sid not in cat.extra:
            raise Missing("Not found: script %s" % sid)
        a, e = cat.extra[sid]
        try:
            doc = kiscule.Doc(kiscule.unpack(self._read(game, a, e), oodle.load(self.root(game))))
            return doc, kiscule.Graph(doc, sid)
        except (kiscule.KisculeError, oodle.OodleError, ValueError) as exc:
            raise Unsupported("%s is not readable as a script: %s" % (e.name, exc))

    def _script_counts(self, game, rows):
        """Fill nodes/edges/missions of the rows a page shows (all 3010 scripts would take a minute)."""
        for r in rows:
            if r["nodes"] is None:
                try:
                    r["nodes"], r["edges"], r["missions"] = kiscule.summary(self.script_graph(game, r["id"])[1])
                except Unsupported:
                    r["nodes"] = r["edges"] = r["missions"] = 0
                    r["editable"] = False

    def _info_scripts(self, game, cat, row, sid):
        a, _e = cat.extra[sid]
        out = {k: v for k, v in row.items() if k not in ("nodes", "edges", "missions")}
        out.update(kiscule.view(self.script_graph(game, sid)[1]), map=row["group"],
                   archive=self._where(game, self.root(game), a),
                   reason=None if row["editable"] else "This script ships only in a DLC archive; "
                   "the loader writes base/ only, so it cannot be changed here.")
        return out

    # models

    def _models_tnc(self, root):
        return self._models_of("tnc", root)

    def _models_yb(self, root):
        return self._models_of("yb", root)

    def _models_of(self, game, root):
        paths = self._archive_paths(game, root)[1]
        mount = mapcatalog.Mount(paths, root)
        mount.find("", "")              # the name index is lazy and not thread-safe: build it now
        rows, extra, seen = [], {}, set()
        kinds = (("baseModel", ".md6mesh"), ("model", ".lwo")) if game == "tnc" else (("baseModel", ".md6mesh"),)
        for type_, ext in kinds:              # Youngblood's static .lwo models are another format: not listed yet
            for a, e in mount.entries(type_):
                path = e.name.split("$", 1)[0]
                if not path.endswith(ext) or not path.startswith("models/") or path in seen:
                    continue
                seen.add(path)
                pfx, cat = next(g for g in MODEL_GROUPS if path.startswith(g[0]))
                folder, leaf = _split(path[len(pfx):])
                rows.append({"id": e.name, "name": leaf[:-len(ext)], "group": "/".join(x for x in (cat, folder) if x),
                             "lang": None, "format": ext[1:], "size": e.usize, "tris": None, "surfaces": None})
                extra[e.name] = (a, e)
        out = _Catalog(rows, extra)
        out.mount, out.token, out.defs, out.anims = mount, mount.key, None, None
        out.paths, out.root, out.game = paths, root, game   # paths, root: what a material-map worker mounts
        old = self._cats.get((game, MODELS))
        with self._lock:
            self._models.clear()
        if old is not None:
            old.mount.close()           # a read still running on it fails loudly, as in _archives
        return out

    def _models_tno(self, root):
        raise Unsupported("Models from The New Order are not supported yet: their md6mesh "
                          "files have a format of their own, and the colour maps live in the virtual "
                          "texture pages, which are not decoded yet.")

    def _decode_model(self, cat, row, data):
        """(surface table [(name, material, verts, tris)], surfaces, bounds, lods, joints)."""
        if row["format"] == "md6mesh":
            md = self._md6_of(cat, row["id"], data)
            surfs = md["meshes"]
            lods = 1 + max((i for m in surfs for i, lo in enumerate(m["lods"]) if lo), default=0)
            sk = cat.mount.find("skeleton", md["skeleton"])
            joints = struct.unpack_from("<H", cat.mount.read(*sk), 0x10)[0] if sk else len(md["joints"])
            table = [(m["name"], m["material"], (m["lods"][0] or {}).get("nv", 0), (m["lods"][0] or {}).get("nt", 0))
                     for m in surfs]
            return table, surfs, md["bounds"], lods, joints
        found = mapgeo.model_surfaces(data)
        surfs = [(p, n, uv, ix) for (_m, p, uv, ix), n in zip(found, _lwo_normals(data, found))]
        for p, n, _uv, ix in surfs:
            md6.fix_normals(p, n, ix)
        table = [(_split(m)[1], m, len(p) // 3, len(ix) // 3) for m, p, _uv, ix in found]
        bounds = ([min(min(s[0][k::3]) for s in surfs) for k in range(3)]
                  + [max(max(s[0][k::3]) for s in surfs) for k in range(3)]) if surfs else [0.0] * 6
        return table, surfs, bounds, 1, 0

    def _model(self, game, mid):
        """(info, surfaces, {lod: WMD1}) of one model, decoded once per catalog
        (the last MODEL_KEEP are kept): mesh.bin is packed from the very decode
        info describes, so a payload changed on disk changes both at once."""
        cat = self.catalog(game, MODELS)
        if mid not in cat.extra:
            raise Missing("Not found: model %s" % mid)
        with self._lock:
            hit = self._models.get(mid)
            if hit is not None and hit[0] is cat:
                self._models.move_to_end(mid)
                return hit[1:]
        a, e = cat.extra[mid]
        row = cat.by_id[mid]
        try:
            table, surfs, bounds, lods, joints = self._decode_model(cat, row, cat.mount.read(a, e))
        except (OSError, idcl.IdclError, oodle.OodleError) as exc:
            raise Unsupported("%s is not readable: %s" % (e.name, exc))
        except (mapgeo.MapGeoError, struct.error, ValueError, OverflowError, MemoryError) as exc:
            raise Unsupported("Model %s cannot be read: %s" % (row["name"], exc))
        if not all(map(math.isfinite, bounds)):
            raise Unsupported("Model %s cannot be read: its bounds are not finite numbers." % row["name"])
        dname, hidden = self._kits(cat, mid) if row["format"] == "md6mesh" else (None, set())
        if all(name.lower() in hidden for name, _m, _nv, _nt in table):
            hidden = set()              # a def that would hide everything is not this mesh's
        sizes, out = {}, []
        for i, (name, mat, nv, nt) in enumerate(table):
            s = {"index": i, "name": name, "material": mat, "verts": nv, "tris": nt, "albedo": None, "albedo_size": None}
            if name.lower() in hidden:
                s["visible"] = False
            tid = maptex.albedo_for_material(cat.mount, mat)
            if tid is not None and tid not in sizes:
                try:        # the mip albedo.png will serve: its header only, no pixel work
                    sizes[tid] = list(maptex.WTX_HEADER.unpack_from(maptex.texture_block(cat.mount, tid, ALBEDO_MAX))[3:5])
                except Exception as exc:  # noqa: BLE001 -- as texture_png: malformed data escapes as anything
                    sizes[tid] = "Texture cannot be shown: %s" % exc
            if isinstance(sizes.get(tid), list):
                s.update(albedo="/api/%s/models/albedo.png?id=%s&max=%d&v=%s" % (game, tid, ALBEDO_MAX, cat.token),
                         albedo_size=sizes[tid])
            elif tid is not None:
                s["albedo_error"] = sizes[tid]
            if game == "tnc" and mat:
                s["pbr"] = "/api/%s/models/pbr.json?id=%s&v=%s" % (game, quote(mat, safe=""), cat.token)
            if game == "tnc" and row["format"] == "md6mesh" and surfs[i]["kind"] == 2:   # hair: the strand mask's uv set
                s["uv2"] = "/api/%s/models/uv2.bin?id=%s&surface=%d&v=%s" % (game, quote(mid, safe=""), i, cat.token)
            out.append(s)
        info = dict(row, archive=self._where(game, self.root(game), a), bounds={"min": bounds[:3], "max": bounds[3:]},
                    lods=lods, joints=joints, surfaces=out, tris=sum(s["tris"] for s in out),
                    md6def=dname if hidden else None)
        row["tris"], row["surfaces"] = info["tris"], len(out)
        meshes = {}
        with self._lock:
            self._models[mid] = (cat, info, surfs, meshes)
            while len(self._models) > MODEL_KEEP:
                self._models.popitem(last=False)
        return info, surfs, meshes

    def _kits(self, cat, mid):
        """(md6Def name, lower-case mesh names hidden at start) of an md6mesh.

        The md6Def whose init names this mesh (the one named after it, else the
        first by name; 21 meshes have several) -> the nearest meshKits in its
        inherit chain -> per group the kit its defaultMeshKits names (the
        child's wins), else the group's first kit (no_gear, no_gore by
        convention); every mesh another kit of the group names is hidden.
        Material swaps (`skins` decls, picked by the defaultSkin of 130 md6Defs
        or by an entity) are not applied: surfaces show the
        md6mesh's own materials."""
        if cat.defs is None:              # ponytail: read on the first model (1 495 decls, ~1 s), not locked
            decls, by_mesh = {}, {}
            for a, e in cat.mount.entries("md6Def"):
                try:
                    d = decls[e.name] = decl.Decl(cat.mount.read(a, e).decode("utf-8", "replace"))
                except (OSError, ValueError, idcl.IdclError, oodle.OodleError, decl.DeclError):
                    continue
                if d.get("init.mesh"):
                    by_mesh.setdefault(d.get("init.mesh").lower(), []).append(e.name)
            cat.defs = decls, by_mesh
        decls, by_mesh = cat.defs
        path = mid.split("$", 1)[0]
        names = sorted(by_mesh.get(path, ()))
        if not names:
            return None, set()
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        pick = next((n for n in names if n.rsplit("/", 1)[-1].rsplit(".", 1)[0] == stem), names[0])
        chain, n = [], pick
        while n in decls and n not in chain:
            chain.append(n)
            n = decls[n].get("init.inherit")
        kits = next((decls[n] for n in chain if "meshKits" in decls[n]), None)
        if kits is None:
            return pick, set()
        default = {}
        for n in chain:
            for p in decls[n].paths():
                if p.startswith("defaultMeshKits."):
                    default.setdefault(p[len("defaultMeshKits."):].lower(), decls[n].get(p).lower())
        hidden, paths = set(), kits.paths()
        for g in (p for p in paths if p.startswith("meshKits.") and p.count(".") == 1):
            group = [(p[len(g) + 1:].lower(), kits.get(p).lower().split()) for p in paths if p.startswith(g + ".")]
            if group:
                shown = dict(group).get(default.get(g[len("meshKits."):].split(":")[0].lower()), group[0][1])
                hidden |= {m for _k, ms in group for m in ms} - set(shown)
        return pick, hidden

    def _info_models(self, game, cat, row, mid):
        return self._model(game, mid)[0]

    def model_mesh(self, game, mid, lod=0):
        """WMD1 (wolfsdk/md6.py) of LOD `lod`, surfaces in info order; a surface
        without that LOD has 0 vertices and 0 indices."""
        info, surfs, meshes = self._model(game, mid)
        if not 0 <= lod < info["lods"]:
            raise BadRequest("This model has only LOD 0." if info["lods"] == 1
                             else "lod must be between 0 and %d." % (info["lods"] - 1))
        if lod in meshes:
            return meshes[lod]
        if info["format"] != "md6mesh":
            got = md6.pack(surfs)
        else:
            empty = (array("f"), array("f"), array("f"), array("I"))
            try:
                reader = ybmesh if game == "yb" else md6
                parts = [reader.surface_arrays(m, lod) or empty for m in surfs]
                for m, (p, n, _uv, ix) in zip(surfs, parts):
                    if m["kind"] != 2:          # hair cards: bent on purpose (md6.py)
                        md6.fix_normals(p, n, ix)
                got = md6.pack(parts)
            except (mapgeo.MapGeoError, ValueError, MemoryError) as exc:
                raise Unsupported("Model %s cannot be read: %s" % (info["name"], exc))
        meshes[lod] = got               # ponytail: two first requests for one LOD may both pack it
        return got

    def model_albedo(self, game, tid, max_side=ALBEDO_MAX):
        """PNG of texture id `tid`: the largest mip <= max_side, cached on disk
        under the id and the Mount's key (not maptex's id + data hash: see above)."""
        cat = self.catalog(game, MODELS)
        if not 16 <= max_side <= 4096:
            raise BadRequest("max must be between 16 and 4096.")
        side = 1 << (max_side.bit_length() - 1)
        try:
            maptex._hit(cat.mount, tid)   # KeyError before the cache file is looked at
            return maptex._cached("model_%s_%s_%d_v%d.png" % (tid, cat.token, side, maptex.CACHE_VERSION),
                                  lambda: maptex.wtx_png(maptex.texture_block(cat.mount, tid, side)))
        except KeyError:
            raise Missing("Not found: texture %s" % tid)
        except Exception as exc:  # noqa: BLE001 -- as texture_png: malformed data escapes as anything
            raise Unsupported("Texture cannot be shown: %s" % exc)

    # -- animations (Wolfenstein II md6mesh + md6skl + md6anim, wolfsdk/md6anim.py) ----------

    def _rig(self, game, mid):
        """(catalog, md6 parse, skeleton) of a skinned Wolfenstein II or Youngblood model."""
        info = self._model(game, mid)[0]
        if game not in IDT6 or info["format"] != "md6mesh":
            raise Unsupported("Animations are shown for md6mesh models of Wolfenstein II and Youngblood only.")
        cat = self.catalog(game, MODELS)
        md = self._md6_of(cat, mid)
        key = (cat.token, md["skeleton"])
        skel = self._skels.get(key)
        if skel is None:
            hit = cat.mount.find("skeleton", md["skeleton"])
            if hit is None:
                raise Unsupported("Model %s names skeleton %s, which is in no archive." % (info["name"], md["skeleton"]))
            try:
                skel = self._skels[key] = md6anim.skeleton(cat.mount.read(*hit))
            except (md6anim.AnimError, struct.error, IndexError) as exc:
                raise Unsupported("Skeleton %s cannot be read: %s" % (md["skeleton"], exc))
        return cat, md, skel

    def _md6_of(self, cat, mid, data=None):
        """md6.parse, or for Youngblood ybmesh.parse with its streamed LODs (the rs_emb_sfile entry)."""
        data = cat.mount.read(*cat.extra[mid]) if data is None else data
        if cat.game != "yb":
            return md6.parse(data)
        sf = cat.mount.find("rs_emb_sfile", ybmesh.stream_name(mid))
        return ybmesh.parse(data, cat.mount.read(*sf) if sf else None, oodle.load(cat.root))

    def _anim_index(self, cat):
        """{skeleton name: [anim names]} over every md6anim of the mount, read once and cached on disk."""
        def make():
            out = {}
            for a, e in cat.mount.entries("anim"):
                try:
                    name = md6anim.skeleton_name(cat.mount.read(a, e))
                except (OSError, ValueError, idcl.IdclError, oodle.OodleError, struct.error):
                    continue
                lst = out.setdefault(name.lower(), [])
                if e.name not in lst:
                    lst.append(e.name)
            return json.dumps(out).encode()
        if cat.anims is None:
            cat.anims = json.loads(maptex._cached("anims_%s_v1.json" % cat.token, make))
        return cat.anims

    def model_anims(self, game, mid):
        """{skeleton, joints, anims: [{id, name, group}]}: the clips made for this model's skeleton."""
        cat, md, skel = self._rig(game, mid)
        rows = []
        for n in sorted(self._anim_index(cat).get(md["skeleton"].lower(), ())):
            folder, leaf = _split(n)
            rows.append({"id": n, "name": leaf[:-8] if leaf.endswith(".md6anim") else leaf, "group": folder})
        return {"skeleton": md["skeleton"], "joints": len(skel["names"]), "anims": rows}

    def model_skeleton(self, game, mid):
        """{skeleton, names, parents, rot, pos}: the bind pose as local quaternion + position, game axes."""
        _cat, md, skel = self._rig(game, mid)
        r6 = lambda v: [round(c, 6) for c in v]  # noqa: E731
        return {"skeleton": md["skeleton"], "names": skel["names"], "parents": skel["parents"],
                "rot": [r6(q) for q in skel["rot"]], "pos": [r6(p) for p in skel["pos"]]}

    def model_skin(self, game, mid):
        """b"WSK1", u32 surface count, per surface (info order, LOD 0): u32 nv, u16 joint[4 nv], f32 weight[4 nv]
        (_md6_skin, ybmesh.vertex_skin: the same weight formula in both games)."""
        cat, md, skel = self._rig(game, mid)
        n = len(skel["names"])
        inv = [0] * len(md["joints"])
        for i, j in enumerate(md["joints"]):
            if j < len(inv):
                inv[j] = i
        out = bytearray(b"WSK1" + struct.pack("<I", len(md["meshes"])))
        for m in md["meshes"]:
            slots, weights = (ybmesh.vertex_skin(m, 0) if cat.game == "yb" else _md6_skin(m)) or (b"", array("f"))
            first = m["joint_first"]
            joints = array("H", (inv[first + s] if first + s < len(inv) and inv[first + s] < n else 0 for s in slots))
            out += struct.pack("<I", len(slots) // 4) + joints.tobytes() + weights.tobytes()   # stays 4-aligned
        return bytes(out)

    def model_anim(self, game, mid, anim):
        """{name, fps, frames, tracks: [{joint, rot?, pos?}]}: per-frame local transforms (flat lists), game axes;
        a track constant over the clip has one value. Joints the clip leaves alone keep the bind pose."""
        cat, md, skel = self._rig(game, mid)
        if anim not in self._anim_index(cat).get(md["skeleton"].lower(), ()):
            raise Missing("%s is not an animation for skeleton %s." % (anim, md["skeleton"]))
        hit = cat.mount.find("anim", anim)

        def make():
            try:
                cl = md6anim.clip(cat.mount.read(*hit))
            except (md6anim.AnimError, struct.error, IndexError, ZeroDivisionError) as exc:
                raise Unsupported("Animation %s cannot be decoded: %s" % (anim, exc))
            tracks = []
            for j, (qs, ts) in sorted(md6anim.pose(skel, cl).items()):
                t = {"joint": j}
                for key, vals in (("rot", qs), ("pos", ts)):
                    if vals:
                        same = all(max(abs(a - b) for a, b in zip(v, vals[0])) < 1e-6 for v in vals)
                        t[key] = [round(c, 6) for v in (vals[:1] if same else vals) for c in v]
                tracks.append(t)
            return json.dumps({"name": _split(anim)[1][:-8], "fps": cl["fps"], "frames": cl["frames"],
                               "tracks": tracks}).encode()
        return self.cache.get(("anim", cat.token, anim), make)

    def model_uv2(self, game, mid, index):
        """f32[2nv] little endian: LOD 0's second uv set of hair surface `index` (md6.uv2)."""
        info, surfs, _meshes = self._model(game, mid)
        if not 0 <= index < len(surfs) or info["format"] != "md6mesh":
            raise Missing("Model %s has no surface %d." % (info["name"], index))
        got = md6.uv2(surfs[index])
        if got is None:
            raise Missing("Surface %d of %s has no second uv set." % (index, info["name"]))
        return got.tobytes()

    def model_pbr(self, game, material):
        """{alpha, roughness, emissive, maps: {slot: URL}} of a TNC material (pbrmat), built once
        and cached on disk under the material and the Mount's key like model_albedo."""
        cat, stem = self._pbr_stem(game, material)
        info = json.loads(maptex._cached(stem + ".json", lambda: self._pbr_build(cat, material, stem)))
        info["maps"] = {s: "/api/%s/models/pbr.png?id=%s&slot=%s&v=%s" % (game, quote(material, safe=""), s, cat.token)
                        for s in info.pop("slots")}
        return info

    def model_pbr_png(self, game, material, slot):
        cat, stem = self._pbr_stem(game, material)
        if slot not in json.loads(maptex._cached(stem + ".json", lambda: self._pbr_build(cat, material, stem)))["slots"]:
            raise Missing("Material %s has no %s map." % (material, slot))
        return maptex._cached("%s_%s.png" % (stem, slot), lambda: pbrmat.build(cat.mount, material)["slots"][slot])

    def _pbr_stem(self, game, material):
        if game != "tnc":
            raise Unsupported("Material maps are read for Wolfenstein II only.")
        cat = self.catalog(game, MODELS)
        if not material or cat.mount.find("material", material) is None:
            raise Missing("Not found: material %s" % material)
        key = hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]
        return cat, "pbr_%s_%s_v%d" % (key, cat.token, pbrmat.VERSION)

    def _pbr_built(self, cat, material):
        """pbrmat.build in the worker pool (made for this archive set); here when the pool cannot run."""
        with self._lock:
            if self._pool_key != cat.token:
                if self._pool is not None:
                    self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = ProcessPoolExecutor(PBR_WORKERS, initializer=_pbr_init, initargs=(cat.paths, cat.root))
                self._pool_key = cat.token
            pool = self._pool
        try:
            return pool.submit(_pbr_job, material).result()
        except (BrokenProcessPool, OSError, RuntimeError):
            return pbrmat.build(cat.mount, material)

    def _pbr_build(self, cat, material, stem):
        """The JSON cache entry; writes the slot PNGs beside it on the way."""
        try:
            built = self._pbr_built(cat, material)
        except Exception as exc:  # noqa: BLE001 -- as texture_png: malformed data escapes as anything
            raise Unsupported("Material %s cannot be read: %s" % (material, exc))
        for slot, png in built["slots"].items():
            maptex._cached("%s_%s.png" % (stem, slot), lambda png=png: png)
        built["slots"] = sorted(built["slots"])
        return json.dumps(built).encode("utf-8")

    # -- listing and info ------------------------------------------------------------

    def listing(self, game, kind, q="", group=None, lang=None, offset=0, limit=100, facets=False, find=None):
        """One page of the hits. find=<id> adds "index": its place in all the
        hits (-1 if it is not among them), so a page can show a linked item."""
        searched = self.hits(game, kind, q)
        if not 1 <= limit <= MAX_LIMIT:
            raise BadRequest("limit must be between 1 and %d." % MAX_LIMIT)
        if offset < 0:
            raise BadRequest("offset must not be negative.")
        out = {"game": game, "kind": kind, "q": q, "group": group, "lang": lang}
        if facets:
            out["groups"] = sorted(Counter(r["group"] for r in searched).items(), key=lambda x: _group_key(x[0]))
            out["langs"] = sorted(Counter(r["lang"] or "" for r in searched).items())
        hits = self.hits(game, kind, q, group, lang, searched)
        out.update(total=len(hits), offset=offset, limit=limit, items=hits[offset:offset + limit])
        if kind == SCRIPTS:
            self._script_counts(game, out["items"])
        if find is not None:
            out["index"] = next((i for i, r in enumerate(hits) if r["id"] == find), -1)
        return out

    def hits(self, game, kind, q="", group=None, lang=None, searched=None):
        """The rows listing() pages through: every search term matches, then group/lang."""
        if searched is None:
            cat = self.catalog(game, kind)
            terms = q.lower().split()
            searched = [r for r, h in zip(cat.rows, cat.hay) if all(t in h for t in terms)] if terms else cat.rows
        hits = searched
        if group is not None:
            hits = [r for r in hits if r["group"] == group or r["group"].startswith(group + "/")]
        if lang is not None:
            hits = [r for r in hits if (r["lang"] or "") == lang]
        return hits

    def info(self, game, kind, item_id):
        """Everything known about one item. A TNO alias or a TNC plugin medium
        is not listed but has an info (row None)."""
        cat = self.catalog(game, kind)
        if item_id not in cat.extra:
            raise Missing("Not found: %s %s" % (_KIND_NAME[kind], item_id))
        return getattr(self, "_info_" + kind)(game, cat, cat.by_id.get(item_id), item_id)

    def _info_videos(self, game, cat, row, vid):
        path, h = cat.extra[vid]
        out = dict(row)
        if "error" in h:
            return out
        tracks = []
        mono = [t for t in h["tracks"] if t["channels"] == 1]
        for t in h["tracks"]:
            if t["channels"] == 2:
                label = "Stereo, track id %d" % t["id"]
            elif len(mono) > 1:
                label = "Mono (voice?), track id %d, language unknown" % t["id"]
            else:
                label = "Mono, track id %d" % t["id"]
            tracks.append({"id": t["id"], "index": t["index"], "channels": t["channels"],
                           "rate": t["rate"], "label": label})
        keys = h["keyframe_list"]
        out.update(fps_num=h["fps_num"], fps_den=h["fps_den"], keyframes=keys,
                   tracks=tracks, default_tracks=self._default_tracks(h),
                   audio_note=None if tracks else NO_TRACK, poster=self.poster(game, vid),
                   auto_exact=AUTO_EXACT, revision=h["revision"])
        if game == "tnc" and not tracks:          # cutscene audio: layers from the Wwise banks (cutsceneaudio)
            try:
                out["bank_audio"] = cutsceneaudio.bank_audio(self.root(game), PurePosixPath(vid).stem)
            except (OSError, ValueError, IndexError, struct.error, idcl.IdclError, wwise.WwiseError) as exc:
                out["bank_audio"] = {"reason": "Sound banks not readable: %s" % exc}
        return out

    def _info_sounds(self, game, cat, row, sid):
        r = cat.extra[sid]
        out = dict(row) if row else {"id": sid, "listed": False}
        out.update({k: v for k, v in r.items() if k not in ("id",)})
        if r["codec"] not in ("vorbis", "pcm"):
            out["playable"] = False
            out["note"] = "Plugin medium (e.g. a reverb impulse response), not a playable sound."
        else:
            out["playable"] = True
            out["mime"] = "audio/ogg" if r["codec"] == "vorbis" else "audio/wav"
        if game == "tno":
            out["variants"] = sorted(k for k, v in cat.extra.items() if v.get("alias_of") == sid)
        return out

    def _info_textures(self, game, cat, row, tid):
        a, e = cat.extra[tid]
        out = dict(row, archive=self._where(game, self.root(game), a))
        data = self._read(game, a, e)
        try:
            if game in IDT6:
                bim = tncimage.parse_bimage(data)
                out.update(width=bim["width"], height=bim["height"], format=bim["format"],
                           mips=len([m for m in bim["mips"] if m["face"] == 0]), faces=bim["faces"],
                           tex_id="%016x" % e.hash_0x60)
            else:
                bim = parse_bim(data)
                out.update(width=bim.width, height=bim.height, format=bim.format_name,
                           mips=len(bim.mips), faces=1)
        except (tncimage.TncImageError, BimError, struct.error, IndexError) as exc:
            out["error"] = _english(str(exc))
        return out

    def _info_texts(self, game, cat, row, tid):
        a, e = cat.extra[tid]
        return dict(row, archive=self._where(game, self.root(game), a))

    # -- reading ---------------------------------------------------------------------

    def _read(self, game, a, e):
        """The entry's payload (archive compression undone)."""
        try:
            if game in IDT6:
                with self._read_lock:
                    raw = a.read_raw(e)
                return tncview.payload(_Bytes(raw), e, oodle.load(self.root(game)))
            with self._read_lock:
                return a.read(e)
        except (OSError, ValueError, idcl.IdclError, ResourceError, oodle.OodleError) as exc:
            raise Unsupported("%s is not readable: %s" % (e.name, exc))

    # -- videos ----------------------------------------------------------------------

    @staticmethod
    def _default_tracks(h):
        """Stereo beds plus the first mono (voice) track, by track id."""
        mono = [t["id"] for t in h["tracks"] if t["channels"] == 1]
        return [t["id"] for t in h["tracks"] if t["channels"] == 2] + mono[:1]

    def poster(self, game, vid):
        """The still shown before playing (see the module docstring)."""
        if (game, vid) in self._posters:
            return self._posters[game, vid]
        path, h = self._video(game, vid)
        third = h["frames"] // 3
        near = lambda n: abs(n - third)  # noqa: E731
        try:
            with bink.open(path) as v:
                step = max(1, v.width // 64)
                seen = []
                for k in sorted(h["keyframe_list"] or [0], key=near):
                    seen.append((_luma_spread(v.frame(k, step)), k))
                    if seen[-1][0] >= POSTER_SPREAD:
                        break
                else:        # no keyframe will do: one frame a second from the start, up to a third in
                    for n in range(0, min(h["frames"], POSTER_SCAN), max(1, round(v.fps))):
                        seen.append((_luma_spread(v.frame(n, step)), n))
                        if n >= third and any(s >= POSTER_SPREAD for s, _n in seen):
                            break
        except bink.BinkError as exc:
            raise Unsupported(str(exc))
        good = [n for s, n in seen if s >= POSTER_SPREAD]
        pick = min(good, key=near) if good else max(seen)[1]   # else the least flat
        self._posters[game, vid] = pick
        return pick

    def _video(self, game, vid):
        cat = self.catalog(game, "videos")
        hit = cat.extra.get(vid)
        if hit is None:
            raise Missing("Not found: video %s" % vid)
        path, h = hit
        if "error" in h:
            raise Unsupported(h["error"])
        return path, h

    @staticmethod
    def _step(h, width):
        if width < 16:
            raise BadRequest("w must be at least 16.")
        return max(1, -(-h["width"] // width))

    def frames(self, game, vid, start=0, count=None, width=960, seek="auto"):
        path, h = self._video(game, vid)
        if seek not in ("auto", "exact", "key"):
            raise BadRequest("seek must be auto, exact or key.")
        if not 0 <= start < h["frames"]:
            raise BadRequest("start must be between 0 and %d." % (h["frames"] - 1))
        if count is not None and count < 1:
            raise BadRequest("count must be at least 1.")
        step = self._step(h, width)
        key = max(k for k in h["keyframe_list"] + [0] if k <= start)
        first = key if seek == "key" or (seek == "auto" and start - key > AUTO_EXACT) else start
        n = h["frames"] - first if count is None else min(count, h["frames"] - first)
        return FrameStream(path, h, first, n, step, start)

    def video_png(self, game, vid, n=None, width=480):
        path, h = self._video(game, vid)
        n = self.poster(game, vid) if n is None else n
        if not 0 <= n < h["frames"]:
            raise BadRequest("n must be between 0 and %d." % (h["frames"] - 1))
        step = self._step(h, width)

        def make():
            try:
                with bink.open(path) as v:
                    w, hh = v.scaled_size(step)
                    return image.to_png(w, hh, v.frame(n, step))
            except bink.BinkError as exc:
                raise Unsupported(str(exc))
        return self.cache.get(("png", game, vid, n, step), make)

    def video_wav(self, game, vid, track_ids=None):
        path, h = self._video(game, vid)
        if not h["tracks"]:
            raise Missing(NO_TRACK)
        by_id = {t["id"]: t["index"] for t in h["tracks"]}
        if track_ids is None:
            track_ids = self._default_tracks(h)
        bad = [t for t in track_ids if t not in by_id]
        if bad or not track_ids:
            raise Missing("This video has no track id %s (present: %s)."
                          % (", ".join(map(str, bad)) or "-", ", ".join(map(str, sorted(by_id)))))
        picks = sorted({by_id[t] for t in track_ids})

        def make():
            try:
                with bink.open(path) as v:
                    return v.audio(picks)
            except bink.BinkError as exc:
                raise Unsupported(str(exc))
        return self.cache.get(("wav", game, vid, tuple(picks)), make)

    # -- sounds, textures, texts -------------------------------------------------------

    def sound(self, game, sid):
        cat = self.catalog(game, "sounds")
        r = cat.extra.get(sid)
        if r is None:
            raise Missing("Not found: sound %s" % sid)
        if r["codec"] not in ("vorbis", "pcm"):
            raise Unsupported("Plugin medium (e.g. a reverb impulse response), not a playable sound.")
        root = self.root(game)

        def make():
            try:
                return (wwise if game == "tnc" else tnoaudio).decode(root, sid)
            except (wwise.WwiseError, ValueError, OSError) as exc:
                raise Unsupported("Sound %s cannot be decoded: %s" % (sid, exc))
        return self.cache.get(("snd", game, sid), make)

    def texture_png(self, game, tid, max_side=512, full=False):
        """PNG of the largest mip <= max_side; full: the largest mip there is (an export)."""
        cat = self.catalog(game, "textures")
        hit = cat.extra.get(tid)
        if hit is None:
            raise Missing("Not found: texture %s" % tid)
        if full:
            max_side = 1 << 16          # beyond every mip: the top one (8192 px skies included)
        elif not 16 <= max_side <= 4096:
            raise BadRequest("max must be between 16 and 4096.")
        a, e = hit

        def make():
            data = self._read(game, a, e)
            try:
                if game in IDT6:
                    return tncimage.to_png(e, data, self._texdbs[game], max_side)[0]
                bim = parse_bim(data)
                mip = next((i for i, m in enumerate(bim.mips) if max(m[0], m[1]) <= max_side),
                           len(bim.mips) - 1)
                return image.bim_to_png(bim, data, mip=mip)
            except Exception as exc:  # noqa: BLE001 -- see tncview.present: malformed data escapes as anything
                why = _english(str(exc)) if isinstance(exc, (tncimage.TncImageError, image.ImageError, BimError)) \
                    else "Image data damaged (%s: %s)" % (type(exc).__name__, exc)
                raise Unsupported("Texture cannot be shown: %s" % why)
        return self.cache.get(("tex", game, tid, max_side), make)

    def text(self, game, tid):
        cat = self.catalog(game, "texts")
        hit = cat.extra.get(tid)
        if hit is None:
            raise Missing("Not found: text %s" % tid)
        a, e = hit
        data = self._read(game, a, e)
        if game in IDT6:
            try:
                inner = self._unwrap(game, e, data)
            except tnccrypt.TncCryptError as exc:
                raise Unsupported("%s cannot be opened: %s" % (e.name, exc))
            got = tncview.as_text(inner)
            if got is None:
                raise Unsupported("%s is not text (binary data)." % e.name)
            return got[0]
        if not _looks_textual(data) or b"\0" in data.rstrip(b"\0"):
            raise Unsupported("%s is not text (binary data)." % e.name)
        return data.rstrip(b"\0").decode("utf-8", "replace")

    def _unwrap(self, game, e, data):
        """tncview.unwrap, plus Youngblood's `lang` strings: u32 usize, u32 csize, then an Oodle frame."""
        if e.type == "lang" and len(data) >= 8:
            try:
                return oodle.load(self.root(game)).decompress(data[8:], struct.unpack_from("<I", data)[0])
            except oodle.OodleError as exc:
                raise Unsupported("%s cannot be unpacked: %s" % (e.name, exc))
        return tncview.unwrap(e, data)[0]

    def text_file(self, game, tid):
        """The text entry's bytes as its file: TNC decrypted/unpacked (cfile, compfile), TNO as stored."""
        cat = self.catalog(game, "texts")
        hit = cat.extra.get(tid)
        if hit is None:
            raise Missing("Not found: text %s" % tid)
        a, e = hit
        data = self._read(game, a, e)
        if game in IDT6:
            try:
                return self._unwrap(game, e, data)
            except tnccrypt.TncCryptError as exc:
                raise Unsupported("%s cannot be opened: %s" % (e.name, exc))
        return data

    # -- exports ---------------------------------------------------------------------

    def export_file(self, game, kind, item):
        """(relative file path, bytes or the file's Path) of one item in its natural format:
        a texture as PNG at full size, a text as its file, a sound as decoded (Ogg or WAV),
        a video as its .bk2/.bik. The path is what a ZIP of hits files it under."""
        if kind == "textures":
            return _export_name(_tex_path(item), ".png", item), self.texture_png(game, item, full=True)
        if kind == "texts":
            type_, _, name = item.partition(":")
            data = self.text_file(game, item)
            return _export_name(type_ + "/" + name, "" if "." in name.rsplit("/", 1)[-1] else ".decl"), data
        if kind == "sounds":
            mime, data = self.sound(game, item)
            return _export_name(re.sub(r"\.wav\Z", "", item), ".ogg" if mime == "audio/ogg" else ".wav"), data
        if kind == "videos":
            path, _h = self._video(game, item)
            return _export_name(item, ""), path
        raise Unsupported("Models are exported one at a time: Models tab, Export.")

    def export_rows(self, game, kind, q="", group=None, lang=None):
        """The hits a ZIP export takes; TooLarge past ZIP_LIMITS, before anything is sent."""
        if kind not in ZIP_LIMITS:
            raise Unsupported("Models are exported one at a time (Models tab, Export), not as a ZIP.")
        rows = self.hits(game, kind, q, group, lang)
        most, most_bytes = ZIP_LIMITS[kind]
        size = sum(r.get("size") or 0 for r in rows)
        if not rows:
            raise BadRequest("Nothing to export: the search has no hits.")
        if most is not None and len(rows) > most:
            raise TooLarge("Too many hits for one ZIP: %d %s, at most %d. Narrow the search or pick a folder."
                           % (len(rows), kind, most))
        if most_bytes is not None and size > most_bytes:
            raise TooLarge("Too large for one ZIP: %.1f GB of %s, at most %d GB. Narrow the search or pick a folder."
                           % (size / (1 << 30), kind, most_bytes >> 30))
        return rows


def _export_name(path, ext, item=None):
    """A relative file path from an id: no drive, no '..', no character Windows refuses.
    A texture (item given) drops its .tga and carries its $mtlkind: one file per image."""
    parts = [re.sub(r'[<>:"|?*\x00-\x1f$]', "_", p).strip(" .") for p in path.replace("\\", "/").split("/")]
    parts = [p for p in parts if p] or ["export"]
    if item is not None:
        kind = re.search(r"\$mtlkind=(\w+)", item)
        parts[-1] = re.sub(r"\.(tga|png|dds|bimage)\Z", "", parts[-1], flags=re.I) + ("_" + kind.group(1) if kind else "")
    return "/".join(parts) + ext


def _looks_textual(data):
    """UTF-8 with over 90 % printable ASCII in the first 2 KB."""
    sample = data[:2048]
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return bool(sample) and sum(1 for b in sample if 0x20 <= b < 0x7F or b in (9, 10, 13)) / len(sample) > 0.9


def _lwo_normals(data, found):
    """Stored normals (f32[3n], 0 where not found) of mapgeo.model_surfaces'
    surfaces, which does not return them: a surface's vertex block (48 bytes
    a vertex: position, uv, normal u8[4] at 0x14 as md6) is where its first
    vertex's position + uv lie, checked for every vertex, searched on from
    the previous block (file order)."""
    out, at = [], 0
    for _m, pos, uv, _ix in found:
        nv = len(pos) // 3
        nrm = array("f", bytes(12 * nv))
        head = pos[:3].tobytes() + uv[:2].tobytes()
        o = data.find(head, at) if nv else -1
        while o >= 0:
            f = array("f", data[o:o + 48 * nv]) if len(data) >= o + 48 * nv else array("f")
            if (len(f) == 12 * nv and f[0::12] == pos[0::3] and f[1::12] == pos[1::3] and f[2::12] == pos[2::3]
                    and f[3::12] == uv[0::2] and f[4::12] == uv[1::2]):
                for k in range(3):
                    nrm[k::3] = array("f", map(md6._NRM.__getitem__, data[o + 0x14 + k:o + 48 * nv:48]))
                at = o + 48 * nv
                break
            o = data.find(head, o + 1)
        out.append(nrm)
    return out


# ponytail: tncimage/image/bim still raise German (their own verifiers match it); the
# Studio shows these reachable ones in English. Drop when those modules speak English.
_LIB_EN = ((r"dieses Format wird nicht dekodiert", "this format is not decoded"),
           (r"keine darstellbare Mip-Ebene", "no displayable mip level"),
           (r"TexDB hat keinen Block (\w+) \(Mip (\d+)\) - fehlt die passende \.texdb\?",
            r"TexDB has no block \1 (mip \2) - is the matching .texdb missing?"),
           (r"Format (.+) wird nicht dekodiert", r"format \1 is not decoded"),
           (r"Mip-Ebene (\d+) existiert nicht", r"mip level \1 does not exist"),
           (r"unbekannter Formatcode", "unknown format code"), (r"keine Mip-Ebene gefunden", "no mip level found"),
           (r"kein BIM", "not a BIM"), (r"zu kurz fuer einen BIM-Header", "too short for a BIM header"),
           # tncimage: parse_bimage / decode / TexDB (BIM v10 HDR probes hit the first one)
           (r"BIM-Version (\d+) wird nicht gelesen \(nur 14\)", r"BIM version \1 is not read (only 14)"),
           (r"BIM-Kopf abgeschnitten \((\d+) Bytes\)", r"BIM header truncated (\1 bytes)"),
           (r"Kopf nennt (\d+) Mip-Sätze, Platz ist für (\d+)", r"header names \1 mip records, room for \2"),
           (r"Mip-Satz (\d+) ist Ebene (\d+)/Seite (\d+)", r"mip record \1 is level \2/face \3"),
           (r"Mip (\d+) liegt außerhalb des 4-Bit-Slots \(Basis (\d+)\)", r"mip \1 lies outside the 4-bit slot (base \2)"),
           (r"Mip hat (\d+) Bytes, (\S+) (.+) braucht (\d+)", r"mip has \1 bytes, \2 \3 needs \4"),
           (r"Mip (\d+): unbekannter Codec (\d+)", r"mip \1: unknown codec \2"),
           (r"Mip (\d+) lässt sich nicht entpacken", r"mip \1 cannot be unpacked"),
           (r"TexDB-Block (\w+) hat (\d+) Bytes, Mip (\d+) erwartet (\d+)", r"TexDB block \1 has \2 bytes, mip \3 expects \4"),
           (r"eingebettete Mip (\d+) abgeschnitten \((\d+) von (\d+) Bytes\)", r"embedded mip \1 truncated (\2 of \3 bytes)"),
           (r"(\S+) ist keine TexDB", r"\1 is not a TexDB"),
           (r"Tabelle mit (\d+) Zeilen passt nicht in (\d+) Bytes", r"table of \1 rows does not fit in \2 bytes"),
           # tnccrypt: undecryptable or damaged archive entries
           (r"HMAC-Tag falsch: falscher Resource-Name oder beschaedigte Datei",
            "HMAC tag wrong: wrong resource name or damaged file"),
           (r"kein gueltiges PKCS7-Padding", "no valid PKCS7 padding"), (r"unplausible Laenge", "implausible length"),
           (r"keine (cfile|compfile)-Nutzlast \((\d+) Byte\)", r"no \1 payload (\2 bytes)"),
           (r"compfile-Kopf sagt (\d+) Byte, da sind (\d+)", r"compfile header says \1 bytes, there are \2"),
           (r"leere compfile-Nutzlast", "empty compfile payload"),
           (r"(AES|Oodle) nicht verfuegbar", r"\1 not available"), (r"Kompressor nicht verfuegbar", "compressor not available"),
           # mapgeo / tnomap: map containers (a user map without .entities, a locked cache file)
           (r"keine \.entities im Container", "no .entities in the container"),
           (r"kein _world\.bmodel im Container", "no _world.bmodel in the container"),
           (r"\.entities nicht entpackbar", ".entities cannot be unpacked"),
           (r"\.entities zu tief verschachtelt", ".entities nested too deeply"),
           (r"\.entities nach (\d+) Entities nicht lesbar", r".entities unreadable after \1 entities"),
           (r"(\S+) ist in Benutzung und kann nicht ersetzt werden", r"\1 is in use and cannot be replaced"),
           (r"keine TexDB", "no TexDB"))   # after "... ist keine TexDB"


def _english(text):
    for de, en in _LIB_EN:
        text = re.sub(de, en, text)
    return text


_KIND_NAME = {"videos": "video", "sounds": "sound", "textures": "texture", "texts": "text", "models": "model",
              "scripts": "script"}


class _Bytes:
    """tncview.payload() reads through read_raw(); this hands it bytes already read."""

    def __init__(self, raw):
        self._raw = raw

    def read_raw(self, _entry):
        return self._raw


class FrameStream:
    """A WVF1 response: header, then `count` frames. Close it (or exhaust it)."""

    MAGIC = b"WVF1"
    HEADER = struct.Struct("<4sHHIIIIII")

    def __init__(self, path, h, first, count, step, requested):
        self.first, self.count, self.step = first, count, step
        try:
            self._video = bink.open(path)   # before any byte is sent: errors still get a status
        except bink.BinkError as exc:
            raise Unsupported(str(exc))
        self.width, self.height = self._video.scaled_size(step)
        self.frame_bytes = self.width * self.height * 4
        self.header = self.HEADER.pack(self.MAGIC, self.width, self.height, first, count,
                                       h["frames"], h["fps_num"], h["fps_den"], requested)
        self.length = len(self.header) + count * self.frame_bytes

    def __iter__(self):
        for n in range(self.first, self.first + self.count):
            if self._video is None:
                return
            yield self._video.frame(n, self.step)
        self.close()

    def close(self):
        if self._video is not None:
            self._video.close()
            self._video = None


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")
