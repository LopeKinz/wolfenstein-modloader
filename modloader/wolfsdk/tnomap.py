"""Wolfenstein: The New Order maps for the 3D viewer, in the contract of the
Wolfenstein II modules (mapcatalog.maps, mapgeo.scene, mapgeo.validate), so
the server needs only a game switch:

    maps(game_root)                       -> [{id, title, group, entities, entities_bytes}]
    scene(game_root, map_id, cache=True)  -> (scene dict, Path of geometry.bin)
    validate(doc, bin_path, deep=False)   -> [German problems], empty = fine
    python -m wolfsdk.tnomap [map id] [--game PFAD] [--neu] [--pruefen]

Measured in tools/probe_tno_maps.py and tools/probe_tno_geo.py, proven by
tools/verify_tnomap.py:

  archives   base/master.index names 14 chunk pairs. Each pair is read from
             base/_wolfsdk_backup/ when both files are there: the user's mods
             patch the install, the viewer shows the original game.
  maps       every file:maps/<id>.entities (25, all campaign). A map mounts
             its own chunk (the one holding the .entities, == mapInfo
             vmtrChunkNumber) and chunk0: its .mapresources name entries of
             exactly those two, and every model it references is in them.
  titles     chapter decls (displayName, CHAPTERTYPE_CAMPAIGN) + mapInfo
             prettyMapName + #str_location_<leaf>_title, english.lang (TNO
             ships no German strings).
  entities   mapgeo.parse_entities / mapgeo.affine (agree with the strict
             line parser of probe_tno_maps on all 69 951 entities); idAI2 is
             enemy or npc by myFaction along the entityDef inherit chain.
  world      maps/<id>/_combo/_world.bmodel, world coordinates: one mesh per
             .proc binaryModels group (0 = world, g = the merged idBinaryModel
             entity of that name, which is then not placed a second time);
             static, or dynamic when hidden at the start (see hidden).
             sky.bmodel likewise, kind sky.
  models     renderModelInfo.model: .lwo / .bmodel / maps/... (+.bmodel) is a
             `model` entry in the entity's frame, except a model authored in
             world coordinates (see world_space: placed with the identity);
             .break names a breakable
             decl whose `model` line is one. md6 (skinned), .dmodel
             (destructibles), model LOD decls, shadow-only models (see
             shadow_only): skipped, counted. Hidden at the start: dynamic;
             a model of sky materials only (skydomes placed as
             idStaticEntity): sky.
  .bmodel    big endian, LE-length strings, 32-byte vertices (f32 pos, f32
             uv, u8 normal/tangent/colour), u16 indices, list (0x1F) or strip
             (0x21F, 0xFFFF restart); triangles wind clockwise against the
             normals, written counter-clockwise (a, c, b).
  units      1 unit = 0.75 inch = 0.01905 m (EXE script constants
             DOOR_HEIGHT_INCHES 96 / DOOR_HEIGHT_UNITS 128). geometry.bin and
             the translations are in metres, +Z up.
  textures   wolfsdk.tnomaptex (albedo_for_material(root, map_id, name),
             texture_info(root, id)), imported lazily: without it every
             albedo is None and the viewer shades flat. Megatexture surfaces
             (mega/megawet/megatrans.decl) get no albedo there (.pages not
             decoded): flat grey. meta.coverage counts triangles as drawn
             (per instance, editor surfaces left out): real BIM albedo on
             0.04..0.7 % of them per map.
  alpha      editor (the viewer skips it): stageprogram editordraw (clip,
             collision, triggers, nodraw), blendnodraw (god-ray volumes),
             additive programmes scaled to <= 10 % (fog sheets, bullet-hole
             rays: all but invisible in the game, 75 % sheets in the viewer)
             and pure light layers (an *add* programme, not environment*,
             no diffusemap: lit windows over the Berlin facades, screen glow;
             the viewer has no additive mode, as 75 % sheets they hid the
             facades of c04p1); glass (environment*add) stays blend; the rest
             by the material text, see alpha_mode.

Cache: %LOCALAPPDATA%\\wolfsdk\\mapcache\\tno\\<map id>\\, keyed by VERSION,
size + mtime of every archive file read and of tnomaptex.py; temp files per
process and thread, the rename waits up to 10 s for a reader of the old file.
Read-only on the game. Errors are mapgeo.MapGeoError, so the server handles both games alike.
"""

import argparse
import functools
import hashlib
import json
import math
import os
import re
import string
import struct
import sys
import threading
import time
from array import array
from collections import Counter, defaultdict
from pathlib import Path

from . import mapgeo
from .mapgeo import MapGeoError, parse_entities
from .resources import Archive, load_master

VERSION = 8
KINDS = mapgeo.KINDS
CATEGORIES = mapgeo.CATEGORIES
ALPHAS = ("opaque", "mask", "blend", "editor")
M_PER_UNIT = 0.01905
FAR_M = 9.5                      # world_space: bbox this far beyond its own size from the model origin
MAGIC, MAGIC_1D = b"\x1eLMB", b"\x1dLMB"
STRIDE = 32
FORMATS = (0x1F, 0x21F)
F_STRIP, RESTART = 0x200, 0xFFFF
STATIC_CLASSES = ("idStaticEntity", "idBinaryModel")
_LOCK = threading.Lock()         # ponytail: one build at a time, per-map locks if the server needs more

# First match wins over the entity's `inherit` (decl path) and `class`;
# idAI2 goes by faction. Measured on all 69 951 entities (probe_tno_maps).
WEAPON = ("prop/weapon",)
ITEM_PREFIX = ("prop/ammo", "prop/health", "prop/armor", "prop/secret")
ITEM_CLASS = ("idProp_AIArmor", "idProp_CombineItem", "idProp_Secret", "idProp_HealthPickup",
              "idRandomLoot")
DOOR_CLASS = ("idProp_Static_Door", "idSoundDoor")
LIGHT_CLASS = ("idLight", "idLensFlare", "idGodRays")
TRIGGER_CLASS = ("idSoundTrigger", "idPerceptionVolume")
SPAWN_CLASS = ("idPlayerStart", "idScenePoint_Spawn")
LOGIC_PREFIX = ("idTarget", "idInfo", "idScenePoint", "idSplinePath", "idAI", "idAAS")
LOGIC_CLASS = ("idCustomDeath_Node", "idDynamicJob", "idEncounterGroupMgr", "idWorldspawn",
               "idAutomapObject", "idInteractiveMarker", "idInputListener", "idAlarm",
               "idCollisionStreamArea", "idWorldClipBounds", "mgTimelineController")


def category(inherit, cls, faction=None):
    if inherit.startswith(WEAPON):
        return "weapon"
    if inherit.startswith(ITEM_PREFIX) or cls in ITEM_CLASS:
        return "item"
    if cls == "idAI2":
        return "npc" if faction and not faction.startswith("faction/auth") else "enemy"
    if cls == "idAnimatedSimpleCharacter":
        return "npc"
    if cls.startswith("idDoor") or cls in DOOR_CLASS or inherit.startswith("func/door"):
        return "door"
    if cls in LIGHT_CLASS:
        return "light"
    if cls.startswith(("idTrigger", "idVolume")) or cls in TRIGGER_CLASS:
        return "trigger"
    if cls in SPAWN_CLASS:
        return "spawn"
    if cls.startswith("idSound"):
        return "audio"
    if cls.startswith(LOGIC_PREFIX) or cls in LOGIC_CLASS:
        return "logic"
    return "other"


def alpha_mode(text):
    """editor / mask / blend / opaque from a TNO material decl (a heuristic
    over its text, like mapgeo.alpha_mode): editordraw, blendnodraw, faint
    additive and pure light layers are editor (see the module doc);
    megatexture land materials are
    opaque, their ...Trans variant blend; a cover map or foliage sort map is
    a cut-out; a blend program, or a stage programme with no diffuse map
    whose name says it adds or blends (glow, glass, particles), blends."""
    low = text.lower()
    if re.search(r"^stageprogram\s+(editordraw|blendnodraw)\s*$", low, re.M):
        return "editor"
    faint = re.search(r"^transmap\s+scale\(\s*[^,]+,\s*[-\d.]+\s*,\s*[-\d.]+\s*,\s*[-\d.]+\s*,\s*([-\d.]+)\s*\)",
                      low, re.M)
    if faint and float(faint.group(1)) <= 0.1 and re.search(r"^stageprogram\s+\w*add", low, re.M):
        return "editor"            # additive at <= 10 %: fog sheets, bullet-hole rays; a 75 % sheet here
    if re.search(r"^landdefinitionfile\s", low, re.M):
        return "blend" if re.search(r"^stageprogram\s+\w*trans\s*$", low, re.M) else "opaque"
    prog = re.search(r"^stageprogram\s+(\w+)", low, re.M)
    if prog and not re.search(r"^diffusemap\s", low, re.M) and re.fullmatch(r"(?!environment)\w*add\w*",
                                                                             prog.group(1)):
        return "editor"            # pure light layer (basicadd, backgroundadd ...): the viewer cannot add light
    if re.search(r"^(covermap|transsortmap)\s", low, re.M):
        return "mask"
    if re.search(r"^blendprogram\s|^materialcoverage\s+coveragetranslucent", low, re.M):
        return "blend"
    if prog and not re.search(r"^diffusemap\s", low, re.M) and re.search(
            r"add|blend|glass|particle|flare|screen|distort|filter|mul2|gui|laser|glow|ribbon|beam|water",
            prog.group(1)):
        return "blend"
    return "opaque"


# --------------------------------------------------------------------------
# archives
# --------------------------------------------------------------------------

def _pairs(game_root):
    """[(index, resources)] of master.index, each from the backup when both
    of its files are there (the originals), else from base/."""
    base = Path(game_root) / "base"
    backup = base / "_wolfsdk_backup"
    out = []
    for i, r in load_master(base):
        b = (backup / i, backup / r)
        out.append(b if b[0].is_file() and b[1].is_file() else (base / i, base / r))
    return out


def _key(game_root):
    h = hashlib.sha1(b"tnomap %d\n" % VERSION)
    tex = _tex()
    files = [p for pair in _pairs(game_root) for p in pair]
    files += [Path(tex.__file__)] if tex and getattr(tex, "__file__", None) else []
    for p in files:
        st = p.stat()
        h.update(("%s|%d|%d\n" % (p, st.st_size, st.st_mtime_ns)).encode("utf-8"))
    return h.hexdigest()[:16]


class _Store:
    """Every chunk pair, opened. lookup order per map: its own chunk, chunk0."""

    def __init__(self, game_root):
        self.root = Path(game_root)
        self.archives = []
        try:
            for i, r in _pairs(game_root):
                self.archives.append(Archive(i, r))
        except Exception:
            self.close()
            raise
        self.index = {}
        for ai, a in enumerate(self.archives):
            for e in a.entries:
                self.index.setdefault((ai, e.type, e.name.lower()), e)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        for a in self.archives:
            a.close()

    def find(self, chunks, type_, name):
        for ai in chunks:
            e = self.index.get((ai, type_, name.lower().replace("\\", "/")))
            if e is not None:
                return ai, e
        return None

    def read(self, hit):
        return self.archives[hit[0]].read(hit[1])

    def text(self, chunks, type_, name):
        hit = self.find(chunks, type_, name)
        return self.read(hit).decode("utf-8", "replace") if hit else ""

    def entities(self):
        """map id -> (chunk index, entry) of every maps/**.entities."""
        out = {}
        for (ai, t, n), e in sorted(self.index.items(), key=lambda kv: kv[0][0]):
            if t == "file" and n.startswith("maps/") and n.endswith(".entities"):
                out.setdefault(n[5:-9], (ai, e))
        return out


def _pretty(s):
    return string.capwords(s.lower()) if s.isupper() else s


def _titles(st, ids):
    lang = {k.lower(): v for k, v in re.findall(r'^\s*"(#[^"]+)"\s+"(.*)"\s*$', st.text(
        [0], "file", "strings/english.lang").lstrip("﻿"), re.M)}
    chapters = defaultdict(list)          # map id -> [(sortId, chapter name)]
    for (ai, t, _n), e in st.index.items():
        if ai == 0 and t == "chapter":
            text = st.read((ai, e)).decode("utf-8", "replace")
            sort, name = re.search(r"sortId = (\d+)", text), re.search(r'displayName = "([^"]*)"', text)
            if sort and name and '"CHAPTERTYPE_CAMPAIGN"' in text:
                for mid in set(re.findall(r'\bmap = "([^"]*)"', text)):
                    chapters[mid].append((int(sort.group(1)), lang.get(name.group(1).lower(), "")))
    out = {}
    for mid in ids:
        leaf = mid.rsplit("/", 1)[-1]
        pretty = re.search(r'prettyMapName = "([^"]*)"', st.text([0], "mapInfo", mid))
        parts = [" / ".join(_pretty(n) for _, n in sorted(chapters[mid]) if n),
                 lang.get(pretty.group(1).lower(), "") if pretty else "",
                 _pretty(lang.get("#str_location_%s_title" % leaf, ""))]
        parts = [p for p in parts if p]
        out[mid] = "%s (%s)" % (" – ".join(parts), leaf) if parts else leaf
    return out


def maps(game_root):
    """[{id, title, group, entities: {archive, entry}, entities_bytes}], by id.
    entities_bytes is the unpacked .entities size."""
    with _Store(game_root) as st:
        ents = st.entities()
        titles = _titles(st, ents)
        return [{"id": mid, "title": titles[mid],
                 "group": "Campaign" if mid.startswith("game/wolf/") else "Other",
                 "entities": {"archive": str(st.archives[ai].resources_path), "entry": e.name},
                 "entities_bytes": e.usize}
                for mid, (ai, e) in sorted(ents.items())]


# --------------------------------------------------------------------------
# .bmodel
# --------------------------------------------------------------------------

def parse_bmodel(data):
    """{names, surfs: [{group, fmt, nv, ni, verts, idx}]}; strict: every byte
    accounted for, else MapGeoError."""
    o = 0

    def take(n):
        nonlocal o
        if n < 0 or o + n > len(data):
            raise MapGeoError("Modell abgeschnitten bei %d (+%d von %d)" % (o, n, len(data)))
        o += n
        return data[o - n:o]

    def u32():
        return struct.unpack(">I", take(4))[0]

    def text():
        n = struct.unpack("<I", take(4))[0]
        if n > 4096:
            raise MapGeoError("Modell: Namenslänge %d" % n)
        return take(n).decode("latin1")

    magic = take(4)
    if magic not in (MAGIC, MAGIC_1D):
        raise MapGeoError("Modell: falsche Marke %s" % magic.hex())
    take(4)
    names = [text() for _ in range(u32())]
    take(12)
    for _ in range(u32()):
        text()
        take(16)
    if magic == MAGIC_1D:
        if o != len(data) or names:
            raise MapGeoError("Modell 0x1D: nur als leerer Kopf bekannt")
        return {"names": [], "surfs": []}
    surfs = []
    for _ in range(u32()):
        _a, group, nv, ni, fmt = struct.unpack(">5I", take(20))
        if fmt not in FORMATS:
            raise MapGeoError("Modell: Vertexformat %#x" % fmt)
        take(40)
        s = {"group": group, "fmt": fmt, "nv": nv, "ni": ni, "verts": take(nv * STRIDE),
             "idx": take(ni * 2)}
        take(28)
        if take(4) != MAGIC:
            raise MapGeoError("Modell: falsche Flächen-Endmarke")
        surfs.append(s)
    if o != len(data) or len(surfs) != len(names):
        raise MapGeoError("Modell: %d Bytes übrig, %d Flächen, %d Namen"
                          % (len(data) - o, len(surfs), len(names)))
    return {"names": [n.lower() for n in names], "surfs": surfs}


def _be(raw, code):
    a = array(code, raw)
    if sys.byteorder == "little":
        a.byteswap()
    return a


def surface_arrays(s, scale=M_PER_UNIT):
    """(positions f32[3n] in metres, uv0 f32[2n], indices u32 CCW) of a surface."""
    nv = s["nv"]
    f = _be(s["verts"], "f")                    # 8 words per vertex; words 5..7 are bytes
    pos, uv = array("f", bytes(12 * nv)), array("f", bytes(8 * nv))
    for k in range(3):
        pos[k::3] = f[k::8]
    uv[0::2], uv[1::2] = f[3::8], f[4::8]
    pos = array("f", map(scale.__mul__, pos))
    ix = _be(s["idx"], "H")
    if s["fmt"] & F_STRIP:
        out, run = array("I"), []
        for i in list(ix) + [RESTART]:
            if i != RESTART:
                run.append(i)
                continue
            for k in range(len(run) - 2):
                a, b, c = (run[k], run[k + 1], run[k + 2]) if k % 2 == 0 else (run[k + 1], run[k], run[k + 2])
                if a != b and b != c and a != c:
                    out.extend((a, c, b))
            run = []
        ix = out
    else:
        ix = array("I", ix[:len(ix) - len(ix) % 3])
        ix[1::3], ix[2::3] = ix[2::3], ix[1::3]  # clockwise -> counter-clockwise
    if ix and max(ix) >= nv:
        raise MapGeoError("Modell: Index %d >= %d Ecken" % (max(ix), nv))
    if not (all(map(math.isfinite, pos)) and all(map(math.isfinite, uv))):
        raise MapGeoError("Modell: Position oder UV nicht endlich")
    return pos, uv, ix


def parse_proc(text):
    """binaryModels names of a .proc, in group order."""
    m = re.search(r"binaryModels \{ /\* numBinaryModels = \*/ (\d+)\s*(.*?)\}", text, re.S)
    if not m:
        raise MapGeoError(".proc ohne binaryModels")
    names = [ln.split()[0] for ln in m.group(2).splitlines() if ln.strip()]
    if len(names) != int(m.group(1)):
        raise MapGeoError(".proc: %s binaryModels angekündigt, %d gefunden" % (m.group(1), len(names)))
    return names


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def _tex():
    """wolfsdk.tnomaptex, or None while it does not exist (albedo None)."""
    try:
        from . import tnomaptex
    except ImportError:
        return None
    return tnomaptex


def _resolve(st, chunks, model):
    """(model entry name, None) or (None, why not)."""
    low = model.lower().replace("\\", "/")
    tail = low.rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1] if "." in tail else ""
    if ext == "break":
        m = re.search(r'^\s*model\s+"?([^\s"]+)', st.text(chunks, "breakable", low[:-6]), re.M)
        if not m:
            return None, "breakable ohne Modell"
        low = m.group(1).lower()
        ext = low.rsplit(".", 1)[-1]
    elif ext == "" and low.startswith("maps/"):
        low, ext = low + ".bmodel", "bmodel"
    if ext in ("lwo", "bmodel"):
        if st.find(chunks, "model", low):
            return low, None
        return None, ("dmodel (zerstörbar)" if st.find(chunks, "discreteAnimation", low)
                      else "kein model-Eintrag im Mount")
    return None, {"md6": "md6 (animiert)", "decl": "md6 (animiert)", "dmodel": "dmodel (zerstörbar)",
                  "breakabledecl": "dmodel (zerstörbar)", "modelloddecl": "LOD-Decl (bmd6)"}.get(
        ext, "Endung .%s" % ext if ext else "ohne Endung")


def hidden(d):
    """Hidden at level start (flags.hide; hide; invisible on 67 idBinaryModels:
    broken walls, rubble, later states, 53 of them shown by an idTarget_Show):
    kind dynamic, the viewer's switchable layer, never static."""
    edit = mapgeo._d(d.get("edit"))
    return "true" in (str(mapgeo._d(edit.get("flags")).get("hide")), str(edit.get("hide")),
                      str(edit.get("invisible")))


def shadow_only(d):
    """hide + staticShadowsFromDynamicModel: a model that only casts the baked
    shadows (32 light/shadowcaster, 5 func/dynamic; no script shows them),
    e.g. the 952 m² card of c03p2. Never drawn in the game: not placed."""
    edit = mapgeo._d(d.get("edit"))
    rmi = mapgeo._d(edit.get("renderModelInfo"))
    return str(edit.get("hide")) == "true" and str(rmi.get("staticShadowsFromDynamicModel")) == "true"


def off_origin(lo, hi):
    """The model's own bbox (metres) lies farther from its origin than its
    diagonal + FAR_M: authored around some other point."""
    return math.hypot(*((a + b) / 2 for a, b in zip(lo, hi))) > math.dist(lo, hi) + FAR_M


def world_space(lo, hi, t):
    """A model (or cm) authored in world coordinates although an entity at
    t (metres) places it: off_origin, and its own bbox already holds t
    (+-1.2 m). The entity matrix would move it a second time; tnomap and
    tnomapcoll place it with the identity. All 25 maps: 256 off_origin
    models at a moved entity, 4 hold it: c16p1 gp_func_movers_mover_panel
    (model + cm, 165 m off otherwise), c01p2 ..._mg1_broken (cm, 181 m),
    c01p3 wc_crashed_plane_tail1 (hidden; lands on wc_crashed_plane_tail)."""
    return off_origin(lo, hi) and all(a - 1.2 <= x <= b + 1.2 for a, b, x in zip(lo, hi, t))


def _faction(st, chunks, name, cache):
    key = name.lower()
    if key not in cache:
        cache[key] = None                          # also stops inherit loops
        text = st.text(chunks, "entityDef", key)
        m = re.search(r'myFaction = "([^"]*)"', text)
        i = re.search(r'inherit = "([^"]*)"', text)
        cache[key] = m.group(1) if m else _faction(st, chunks, i.group(1), cache) if i else None
    return cache[key]


def _r(x, nd):
    x = round(x, nd)
    return int(x) if x == int(x) else x


def build(game_root, map_id, out_dir):
    """Write out_dir/scene.json + geometry.bin (atomically) and return the scene."""
    t0 = time.time()
    tex = _tex()
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = "%d.%d.tmp" % (os.getpid(), threading.get_ident())   # other processes may build the same map
    tmp_bin, tmp_json = out_dir / ("geometry.bin." + tag), out_dir / ("scene.json." + tag)
    try:
        return _build(game_root, map_id, out_dir, tmp_bin, tmp_json, t0, tex)
    finally:
        for p in (tmp_bin, tmp_json):
            try:
                p.unlink()
            except OSError:
                pass


def _replace(src, dst):
    """os.replace, retried for ~10 s: on Windows a reader that has the old
    file open (the server streaming geometry.bin) blocks the rename."""
    for i in range(40):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if i == 39:
                raise MapGeoError("%s ist in Benutzung und kann nicht ersetzt werden" % dst.name)
            time.sleep(0.25)


def _build(game_root, map_id, out_dir, tmp_bin, tmp_json, t0, tex):
    with _Store(game_root) as st:
        hit = st.entities().get(map_id)
        if hit is None:
            raise KeyError("Unknown map: %s" % map_id)
        chunks = (hit[0], 0)
        leaf = map_id.rsplit("/", 1)[-1]
        combo = "maps/%s/_combo/" % map_id
        try:
            ents = parse_entities(st.read(hit).decode("utf-8", "surrogateescape"))
        except RecursionError:
            raise MapGeoError("%s: .entities zu tief verschachtelt" % map_id)
        by_name = {}
        for ei, (name, _d) in enumerate(ents):
            by_name.setdefault(name, ei)
        world_ent = by_name.get("world", 0)

        # entities -> markers + placed models
        entities, placed, skipped, fcache, spots = [], [], Counter(), {}, []
        mtx_of, total = [], 0
        for ei, (name, d) in enumerate(ents):
            edit = mapgeo._d(d.get("edit"))
            inherit, cls = str(d.get("inherit", "")), str(d.get("class", ""))
            try:
                mtx = mapgeo.affine(edit)
                for j in (3, 7, 11):
                    mtx[j] *= M_PER_UNIT
            except (TypeError, ValueError):
                mtx = None
                skipped["Platzierung nicht lesbar"] += 1
            mtx_of.append(mtx)
            if mtx and isinstance(edit.get("spawnPosition"), dict):
                spots.append(mtx[3::4])
            faction = _faction(st, chunks, inherit, fcache) if cls == "idAI2" else None
            entities.append({"name": name, "cat": category(inherit, cls, faction), "cls": cls, "def": inherit,
                             "x": _r(mtx[3], 3) if mtx else 0, "y": _r(mtx[7], 3) if mtx else 0,
                             "z": _r(mtx[11], 3) if mtx else 0})

        # the compiled world, one mesh per binaryModels group, plus the sky
        groups = parse_proc(st.text(chunks, "file", combo + leaf + ".proc"))
        world = st.find(chunks, "model", combo + "_world.bmodel")
        if world is None:
            raise MapGeoError("%s: kein _world.bmodel im Container" % map_id)
        wm = parse_bmodel(st.read(world))
        per = defaultdict(list)
        for i, s in enumerate(wm["surfs"]):
            if s["group"] >= len(groups):
                raise MapGeoError("%s: Flächengruppe %d, .proc hat %d" % (map_id, s["group"], len(groups)))
            per[s["group"]].append(i)
        merged = {groups[g] for g in per}
        parts = []                                 # (mesh name, [(material, surface)], ent, kind, matrix)
        ident = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0]
        for g in sorted(per):
            ei = world_ent if g == 0 else by_name.get(groups[g], world_ent)
            d = ents[ei][1] if ei < len(ents) else {}
            if g and shadow_only(d):
                skipped["Schattenwerfer, nie sichtbar"] += 1
                continue
            kind = "static" if g == 0 or (d.get("class") == "idBinaryModel" and not hidden(d)) else "dynamic"
            parts.append(("_world/" + groups[g], [(wm["names"][i], wm["surfs"][i]) for i in per[g]],
                          ei, kind, ident))
        sky = st.find(chunks, "model", combo + "sky.bmodel")
        if sky:
            sm = parse_bmodel(st.read(sky))
            if sm["surfs"]:
                parts.append(("_sky", list(zip(sm["names"], sm["surfs"])), world_ent, "sky", ident))
        world_surfaces = len(wm["surfs"])

        names = {}
        for ei, (name, d) in enumerate(ents):
            model = mapgeo._d(mapgeo._d(d.get("edit")).get("renderModelInfo")).get("model")
            if not isinstance(model, str) or not model or model == "NULL":
                continue
            total += 1
            if name in merged:
                skipped["im _world.bmodel enthalten"] += 1
                continue
            if mtx_of[ei] is None:
                continue
            if shadow_only(d):
                skipped["Schattenwerfer, nie sichtbar"] += 1
                continue
            if model.lower() not in names:
                names[model.lower()] = _resolve(st, chunks, model)
            rn, why = names[model.lower()]
            if rn is None:
                # idStaticEntity models ship no entry of their own: the compiler
                # baked them into _world.bmodel (verify_tnomap: their materials
                # and positions are there)
                baked = why == "kein model-Eintrag im Mount" and d.get("class") == "idStaticEntity"
                skipped["idStaticEntity, in _world.bmodel verbacken" if baked else why] += 1
                continue
            static = d.get("class") in STATIC_CLASSES and not hidden(d)
            kind = "sky" if "/skies/" in rn else "static" if static else "dynamic"
            placed.append((rn, ei, kind, mtx_of[ei]))

        # geometry: each mesh once, in order of first use
        meshes, mesh_of, mats, mat_of, instances = [], {}, [], {}, []
        nsurf = nvert = ntri = world_space_n = 0
        box = {}                                   # mesh index -> model-space AABB

        sky_of = {}

        def sky_mat(name):
            if name not in sky_of:
                sky_of[name] = bool(re.search(r"^\s*stageprogram\s+sky", st.text(chunks, "material", name),
                                              re.M | re.I))
            return sky_of[name]

        def mat(name):
            if name not in mat_of:
                mat_of[name] = len(mats)
                mats.append(name)
            return mat_of[name]

        with open(tmp_bin, "wb") as f:
            def add_mesh(name, surfs):
                nonlocal nsurf, nvert, ntri
                recs, lo, hi = [], [math.inf] * 3, [-math.inf] * 3
                for mname, s in surfs:
                    pos, uv, ix = surface_arrays(s)
                    if not ix:
                        continue
                    off = f.tell()
                    f.write(pos.tobytes())
                    f.write(uv.tobytes())
                    f.write(ix.tobytes())
                    mi = mat(mname)
                    recs.append({"material": mi, "vcount": len(pos) // 3, "icount": len(ix), "pos_off": off,
                                 "uv_off": off + 4 * len(pos), "idx_off": off + 4 * (len(pos) + len(uv))})
                    for j in range(3):
                        lo[j], hi[j] = min(lo[j], min(pos[j::3])), max(hi[j], max(pos[j::3]))
                    nsurf, nvert, ntri = nsurf + 1, nvert + len(pos) // 3, ntri + len(ix) // 3
                if not recs:
                    return None
                meshes.append({"name": name, "surfaces": recs})
                box[len(meshes) - 1] = (lo, hi)
                return len(meshes) - 1

            for name, surfs, ei, kind, m in parts:
                mi = add_mesh(name, surfs)
                if mi is not None:
                    instances.append({"mesh": mi, "ent": ei, "kind": kind, "m": m})
            for rn, ei, kind, m in placed:
                if rn not in mesh_of:
                    try:
                        mm = parse_bmodel(st.read(st.find(chunks, "model", rn)))
                        got = add_mesh(rn, list(zip(mm["names"], mm["surfs"])))
                        mesh_of[rn] = "Modell ohne Dreiecke" if got is None else got
                    except MapGeoError:
                        mesh_of[rn] = "Modell nicht lesbar"
                if isinstance(mesh_of[rn], str):
                    skipped[mesh_of[rn]] += 1
                else:
                    if all(sky_mat(mats[x["material"]]) for x in meshes[mesh_of[rn]]["surfaces"]):
                        kind = "sky"               # a sky dome placed as idStaticEntity (c04p2_3)
                    if world_space(*box[mesh_of[rn]], m[3::4]):
                        m = ident                  # already where the entity stands
                        world_space_n += 1
                    instances.append({"mesh": mesh_of[rn], "ent": ei, "kind": kind,
                                      "m": [_r(x, 4 if i % 4 == 3 else 6) for i, x in enumerate(m)]})
            size = f.tell()

        # materials: alpha from the decl text, albedo from wolfsdk.tnomaptex
        materials, textures, tex_of, tex_errors, land = [], [], {}, Counter(), []
        for name in mats:
            text = st.text(chunks, "material", name)
            if re.search(r"^landdefinitionfile\s", text, re.M | re.I):
                land.append(len(materials))        # megatexture: mega.decl, megatrans.decl, megawet.decl
            tid = None
            if tex is not None:
                try:
                    tid = tex.albedo_for_material(game_root, map_id, name)
                except Exception as exc:  # noqa: BLE001 -- a texture must never cost the geometry
                    tex_errors[type(exc).__name__] += 1
            if tid and tid not in tex_of:
                try:
                    tex_of[tid] = tex.texture_info(game_root, tid)
                    textures.append(tex_of[tid])
                except Exception as exc:  # noqa: BLE001
                    tex_of[tid] = None
                    tex_errors[type(exc).__name__] += 1
            materials.append({"name": name, "albedo": tid if tid and tex_of[tid] else None,
                              "alpha": alpha_mode(text) if text else "opaque"})
        missing_decl = sum(1 for name in mats if not st.find(chunks, "material", name))
        title = _titles(st, [map_id])[map_id]

    # bounds: every non-sky instance. core: 5th..95th percentile of the entities
    # that have a spawnPosition -- the playable level; the world's backdrop
    # (London skyline, the sea around the compound) spans kilometres, and
    # 1..99 still lets a few far cinematic entities stretch it
    boxes = [mapgeo._box(box[i["mesh"]], i["m"]) for i in instances if i["kind"] != "sky"]
    pts = spots if len(spots) >= 20 else [[(b[0][j] + b[1][j]) / 2 for j in range(3)] for b in boxes]
    if not boxes:
        pts = pts or [[0, 0, 0]]
        boxes = [(p, p) for p in pts]
    bbox = {"min": [_r(min(b[0][j] for b in boxes), 2) for j in range(3)],
            "max": [_r(max(b[1][j] for b in boxes), 2) for j in range(3)]}
    ax = [sorted(p[j] for p in pts) for j in range(3)]
    k = len(pts) // 20
    core = {"min": [_r(a[k], 2) for a in ax], "max": [_r(a[-k - 1], 2) for a in ax]}

    by_kind = Counter(i["kind"] for i in instances)
    drawn = sum(sum(s["icount"] for s in meshes[i["mesh"]]["surfaces"]) for i in instances) // 3
    textured = [i for i, x in enumerate(materials) if x["albedo"]]
    tri_of = Counter()                             # material -> triangles drawn (per instance, not editor)
    for i in instances:
        for s in meshes[i["mesh"]]["surfaces"]:
            if materials[s["material"]]["alpha"] != "editor":
                tri_of[s["material"]] += s["icount"] // 3
    doc = {
        "meta": {
            "id": map_id, "game": "tno",
            "title": title,
            "units": "m", "up": "z", "bbox": bbox, "bbox_core": core,
            "unit_scale": {"meters_per_unit": M_PER_UNIT,
                           "source": "EXE-Konstanten DOOR_HEIGHT_INCHES 96 / DOOR_HEIGHT_UNITS 128 "
                                     "(1 Einheit = 0,75 Zoll)"},
            "counts": {"entities": len(entities), "instances": len(instances),
                       "instances_by_kind": {k: by_kind[k] for k in KINDS},
                       "meshes": len(meshes), "models": sum(not isinstance(v, str) for v in mesh_of.values()),
                       "surfaces": nsurf, "vertices": nvert, "triangles": ntri,
                       "triangles_drawn": drawn, "materials": len(materials),
                       "textures": len(textures), "geometry_bytes": size,
                       "world_surfaces": world_surfaces,
                       "skipped": dict(skipped.most_common()), "seconds": round(time.time() - t0, 1)},
            "coverage": {"models_resolved": sum(1 for i in instances
                                                if not meshes[i["mesh"]]["name"].startswith(("_world/", "_sky"))),
                         "models_total": total,
                         "models_merged": skipped["im _world.bmodel enthalten"],
                         "models_world_space": world_space_n,
                         "materials_textured": len(textured), "materials_total": len(materials),
                         "materials_without_decl": missing_decl,
                         "triangles_visible": sum(tri_of.values()),
                         "triangles_textured": sum(tri_of[i] for i in textured),
                         "triangles_megatexture": sum(tri_of[i] for i in land),
                         "triangles_megatexture_textured": sum(tri_of[i] for i in land if materials[i]["albedo"]),
                         "textures_source": "wolfsdk.tnomaptex" if tex else "keins (wolfsdk.tnomaptex fehlt)",
                         "texture_errors": dict(tex_errors)},
        },
        "materials": materials, "meshes": meshes, "instances": instances,
        "entities": entities, "textures": textures,
    }
    tmp_json.write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    _replace(tmp_bin, out_dir / "geometry.bin")
    _replace(tmp_json, out_dir / "scene.json")
    return doc


def validate(doc, bin_path, deep=False):
    """mapgeo.validate (every range inside geometry.bin, every index in
    range; deep: indices < vcount, finite floats) plus the TNO specifics:
    alpha modes, categories, metres/+Z, finite entity positions."""
    bad = mapgeo.validate(doc, bin_path, deep)
    meta = doc.get("meta", {})
    if meta.get("units") != "m" or meta.get("up") != "z":
        bad.append("Einheiten %r/%r statt m/z" % (meta.get("units"), meta.get("up")))
    bad += ["Material %s: Alpha %r" % (m["name"], m["alpha"]) for m in doc["materials"]
            if m["alpha"] not in ALPHAS]
    bad += ["Entität %s: Kategorie %r oder Position nicht endlich" % (e["name"], e["cat"])
            for e in doc["entities"] if e["cat"] not in CATEGORIES
            or not all(isinstance(e[k], (int, float)) and math.isfinite(e[k]) for k in "xyz")]
    return bad


@functools.lru_cache(maxsize=4)
def _ids(game_root):
    return frozenset(m["id"] for m in maps(game_root))


def cache_dir(map_id):
    """%LOCALAPPDATA%\\wolfsdk\\mapcache\\tno\\<map id> (KeyError for an unsafe id)."""
    return mapgeo.cache_dir("tno/" + map_id)


def scene(game_root, map_id, cache=True):
    """(scene dict, Path of geometry.bin) for a map id of maps(). Built once
    and cached; a changed archive (or tnomaptex.py) or a damaged cache
    rebuilds it. KeyError for an unknown map."""
    if map_id not in _ids(str(game_root)):     # before any cache path: no litter, no case aliases
        raise KeyError("Unknown map: %s" % map_id)
    d = cache_dir(map_id)
    key = _key(game_root)
    with _LOCK:
        if cache:
            try:
                if (d / "key").read_text(encoding="ascii") == key:
                    doc = json.loads((d / "scene.json").read_text(encoding="utf-8"))
                    if not validate(doc, d / "geometry.bin"):
                        return doc, d / "geometry.bin"
            except (OSError, ValueError, KeyError, TypeError):
                pass                                # missing or damaged: rebuild
        try:
            (d / "key").unlink()
        except OSError:
            pass
        doc = build(game_root, map_id, d)
        (d / "key").write_text(key, encoding="ascii")
    return doc, d / "geometry.bin"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m wolfsdk.tnomap",
                                 description="TNO-Karte als scene.json + geometry.bin in den Cache schreiben")
    ap.add_argument("map_id", nargs="?", help="z. B. game/wolf/c01/c01p1 (ohne: Kartenliste)")
    ap.add_argument("--game", help="Spielordner (sonst wie der Loader: Konfiguration, Steam)")
    ap.add_argument("--neu", action="store_true", help="Cache ignorieren und neu erzeugen")
    ap.add_argument("--pruefen", action="store_true", help="geometry.bin vollständig prüfen")
    a = ap.parse_args(argv)
    from .game import Game, GameError
    try:
        root = Game.find(a.game, "tno").root
    except GameError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not a.map_id:
        for m in maps(root):
            print("%-32s %8d  %s" % (m["id"], m["entities_bytes"], m["title"]))
        return 0
    t = time.time()
    try:
        doc, path = scene(root, a.map_id, cache=not a.neu)
    except (KeyError, MapGeoError) as exc:
        print("Fehler: %s" % (exc.args[0] if exc.args else exc), file=sys.stderr)
        return 1
    c, cov = doc["meta"]["counts"], doc["meta"]["coverage"]
    print("%s: %d/%d Modelle, %d Flächen, %d Dreiecke, %.1f MB, %.1f s -> %s" % (
        a.map_id, cov["models_resolved"], cov["models_total"], c["surfaces"], c["triangles"],
        path.stat().st_size / 1e6, time.time() - t, path.parent))
    if a.pruefen:
        bad = validate(doc, path, deep=True)
        print("Prüfung: %s" % ("in Ordnung" if not bad else "%d Fehler, z. B. %s" % (len(bad), bad[:3])))
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
