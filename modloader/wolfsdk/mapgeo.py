"""Wolfenstein II map geometry for the 3D viewer: scene.json + geometry.bin.

    scene(game_root, map_id, cache=True) -> (scene dict, Path of geometry.bin)
    python -m wolfsdk.mapgeo <map id> [--game PFAD] [--neu] [--pruefen]

A map is its .entities (first hit in the mount of wolfsdk/mapcatalog.py) plus
every static model an entity places. Ported from the reference tools, which
stay the proof (tools/verify_mapgeo.py):

  entities   tools/viewer_collision.parse_entities, plus `key = ! {` blocks
             (44 in 3 retail maps); categories as tools/viewer_entities.RULES
  placement  tools/viewer_meshes.affine: world = t + x*A0 + y*A1 + z*A2 with
             A_i = renderModelInfo.scale_i * spawnOrientation.mat[i]
             (omitted rows/components: identity; bound entities are drawn at
             their spawnPosition)
  models     tools/probe_meshfmt.parse_model, LOD 0 of every surface; the
             entry is <lwo>[$static_instance=1]$lodgroup=<g> (resource_name),
             an .idasset goes through its modelAsset: paintSource ->
             <lwo>...$lodgroup=vpaintgrpoverride, else sourceModel
  materials  albedo via maptex.albedo_for_material (loosealbedo first,
             agent_reports/matpipe.md); alpha mode from the material text

scene.json (the data contract with wolfsdk/mapview):
  meta       {id, title, units, up, bbox, bbox_core, counts, coverage}
  materials  [{name, albedo: texture id | None, alpha: opaque|mask|blend}]
  meshes     [{name: model entry, surfaces: [{material, vcount, icount,
               pos_off, uv_off, idx_off}]}]
  instances  [{mesh, ent, kind: static|dynamic|sky, m: 3x4 row-major}]
  entities   [{name, cat, cls, def, x, y, z}], file order (= `ent`)
  textures   [{id, name, format, w, h, srgb}]: wolfsdk/maptex.py's records,
             id = the image entry's +0x60 as 16 hex (what /api/tex takes)

geometry.bin, little endian, per surface at its offsets: positions f32[3n]
(model space, meters), uv0 f32[2n], indices u32, counter-clockwise (the
source winds clockwise against its normals). An entity's customMaterial
replaces every surface's material (idTech customShader semantics, not
runtime-proven): that is a second mesh entry sharing the first one's bytes.

Cache: %LOCALAPPDATA%\\wolfsdk\\mapcache\\<map id>\\, keyed by VERSION and
size + mtime of every mounted archive and .texdb. Read-only on the game.
"""

import argparse
import hashlib
import json
import math
import os
import re
import struct
import sys
import threading
import time
from array import array
from collections import Counter
from pathlib import Path

from . import mapcatalog, maptex, oodle, tnccrypt, tncimage

VERSION = 4
KINDS = ("static", "dynamic", "sky")
CATEGORIES = ("enemy", "npc", "item", "weapon", "light", "trigger", "spawn",
              "door", "audio", "logic", "other")
# First match wins: (field, prefix, category) over the entityDef's `inherit`
# (decl path) and `class` (C++ class). tools/viewer_entities.RULES.
RULES = (
    ("inherit", "prop/pickup/weapons", "weapon"), ("inherit", "prop/pickup/", "item"),
    ("inherit", "ai/civilians", "npc"), ("inherit", "ai/resistance", "npc"),
    ("class", "idAI", "enemy"), ("inherit", "prop/doors", "door"),
    ("class", "idLight", "light"), ("class", "idLensFlare", "light"),
    ("class", "idTrigger", "trigger"), ("class", "idVolume", "trigger"),
    ("class", "idAutomapVolume", "trigger"), ("class", "idEnvArea", "trigger"),
    ("class", "idPlayerStart", "spawn"), ("class", "idScenePoint_Spawn", "spawn"),
    ("class", "idTarget_AISpawn", "spawn"), ("class", "idSound", "audio"),
    ("class", "idEnvironmentSpeaker", "audio"), ("inherit", "sound_position", "audio"),
    ("class", "idScenePoint", "logic"), ("class", "idTarget", "logic"),
    ("class", "idAlarm", "logic"), ("class", "idKiscule", "logic"),
    ("class", "idEncounterGroupMgr", "logic"), ("class", "idWorldMarker", "logic"),
    ("class", "idSplinePath", "logic"), ("class", "idWorldspawn", "logic"),
    ("inherit", "info/null", "logic"),
)
VERTEX_FORMAT = 0x0001801F      # the only one retail ships: 48 bytes
STRIDE = 48
BMLR = 0x724C4D42
WHITE_COVER = "textures/system/constant_color/white"
_LOCK = threading.Lock()        # ponytail: one build at a time, per-map locks if the server needs more


class MapGeoError(Exception):
    pass


def cache_dir(map_id):
    """%LOCALAPPDATA%\\wolfsdk\\mapcache\\<map id>. The id comes from a
    packagemapspec (a user DLC may write anything there), so it must stay a
    plain relative path."""
    parts = map_id.split("/")
    if any(p in ("", ".", "..") or not re.fullmatch(r"[\w.-]+", p) for p in parts):
        raise KeyError("Invalid map id: %r" % map_id)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base, "wolfsdk", "mapcache", *parts)


# --------------------------------------------------------------------------
# .entities
# --------------------------------------------------------------------------

_TOK = re.compile(r'"[^"]*"|[{}=;]|[^\s{}=;"]+')


def parse_entities(text):
    """[(name, entityDef dict)] from idTech 6 .entities text, file order."""
    tok = _TOK.findall(text)

    def block(i):
        out = {}
        while tok[i] != "}":
            k = tok[i]
            i += 1
            if tok[i] == "=":
                i += 1
                if tok[i] == "!":                   # `renderModelInfo = ! {` (c3v2 & co.)
                    i += 1
                if tok[i] == "{":
                    v, i = block(i + 1)
                else:
                    v, i = tok[i].strip('"'), i + 1
                    if tok[i] == ";":
                        i += 1
            elif tok[i] == "{":
                v, i = block(i + 1)
            elif tok[i + 1] == "{" and tok[i] not in "{};":   # entityDef NAME { .. }
                k, (v, i) = k + " " + tok[i], block(i + 2)
            else:                                   # bare item: layers { "a" "b" }
                v = True
            out[k] = v
        return out, i + 1

    ents = []
    try:
        i = tok.index("entity")
        while i < len(tok):
            if tok[i] != "entity" or tok[i + 1] != "{":
                raise MapGeoError("unerwartetes Token %r" % tok[i])
            e, i = block(i + 2)
            (k, v), = [(k, v) for k, v in e.items() if k.startswith("entityDef ")]
            ents.append((k[10:], v))
    except (IndexError, ValueError) as exc:
        raise MapGeoError(".entities nach %d Entities nicht lesbar (%s)" % (len(ents), exc))
    return ents


def category(inherit, cls):
    for field, prefix, cat in RULES:
        if (inherit if field == "inherit" else cls).startswith(prefix):
            return cat
    return "other"


def _d(x):
    return x if isinstance(x, dict) else {}


def _vec(b, default):
    b = _d(b)
    return tuple(float(b.get(k, d)) for k, d in zip("xyz", default))


def affine(edit):
    """[A0x,A1x,A2x,tx, A0y,A1y,A2y,ty, A0z,A1z,A2z,tz]: world = t + x*A0 +
    y*A1 + z*A2, A_i = scale_i * mat[i] (tools/viewer_meshes.affine 'local')."""
    t = _vec(edit.get("spawnPosition"), (0, 0, 0))
    mat = _d(_d(edit.get("spawnOrientation")).get("mat"))
    s = _vec(_d(edit.get("renderModelInfo")).get("scale"), (1, 1, 1))
    a = [[c * s[r] for c in _vec(mat.get("mat[%d]" % r), [float(c == r) for c in range(3)])]
         for r in range(3)]
    out = [a[0][0], a[1][0], a[2][0], t[0], a[0][1], a[1][1], a[2][1], t[1],
           a[0][2], a[1][2], a[2][2], t[2]]
    if not all(map(math.isfinite, out)):
        raise ValueError("nicht endlich")
    return out


# --------------------------------------------------------------------------
# model names (tools/probe_meshfmt.resource_name, verify_mapcatalog.static_asset)
# --------------------------------------------------------------------------

def lod_groups(mount):
    return {e.name: [p.lower().rstrip("/") for p in re.findall(
        r'item\[\d+\]\s*=\s*"([^"]*)"', mount.read(a, e).decode("latin1"))]
        for a, e in mount.entries("lodGroup")}


def resource_name(model, groups, static_instance=True):
    """The `model` entry the engine asks for when an entity says model = `model`."""
    path = model.lower().replace("\\", "/")
    best, length = "default", -1
    for g, paths in groups.items():
        for p in paths:
            if g != "vpaintgrpoverride" and (path == p or path.startswith(p + "/")) and len(p) > length:
                best, length = g, len(p)
    return path + ("$static_instance=1" if static_instance else "") + "$lodgroup=" + best


def _asset_models(mount, model, groups, si):
    """Candidate `model` entries of a static .idasset (vertex-painted or not)."""
    model = model.lower()
    ma = mount.find("modelAsset", model) or mount.find("modelAsset", model[:-len("idasset")] + "decl")
    if not ma:
        return []
    t = mount.read(*ma).decode("latin1")
    if "idRenderModelStatic" not in t:
        return []
    paint = re.search(r'paintSource\s*=\s*"([^"]+)"', t)
    if paint:
        return [paint.group(1).lower() + ("$static_instance=1" if s else "")
                + "$lodgroup=vpaintgrpoverride" for s in (si, not si)]
    src = re.search(r'sourceModel\s*=\s*"([^"]+\.lwo)"', t)
    return [resource_name(src.group(1), groups, s) for s in (si, not si)] if src else []


def _resolve(mount, model, si, groups):
    """(model entry name, None) or (None, why not)."""
    ext = model.rsplit(".", 1)[-1].lower() if "." in model[-12:] else ""
    if ext == "lwo":
        names = [resource_name(model, groups, s) for s in (si, not si)]
    elif ext == "idasset":
        names = _asset_models(mount, model, groups, si)
        if not names:
            return None, "idasset ohne statisches Modell"
    elif ext == "md6":
        return None, "md6 (animiert)"
    else:
        return None, ".%s" % ext if ext else "ohne Endung"
    for n in names:
        if mount.find("model", n):
            return n, None
    return None, "kein model-Eintrag im Mount"


# --------------------------------------------------------------------------
# model payload -> LOD 0 surfaces (tools/probe_meshfmt.parse_model)
# --------------------------------------------------------------------------

class _Reader:
    def __init__(self, data):
        self.d, self.o = data, 0

    def take(self, n):
        if n < 0 or self.o + n > len(self.d):
            raise MapGeoError("Modell abgeschnitten bei %d (+%d von %d)" % (self.o, n, len(self.d)))
        self.o += n
        return self.d[self.o - n:self.o]

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def i32(self):
        return struct.unpack("<i", self.take(4))[0]

    def str(self):
        return self.take(self.u32()).decode("latin1")


def model_surfaces(data):
    """[(material, positions f32[3n], uv0 f32[2n], indices u32 CCW)] of LOD 0,
    one per surface that has triangles, in file order."""
    r = _Reader(data)
    r.take(12)
    for _ in range(max(r.i32(), 0)):                 # material list: str + 3 u32
        r.str()
        r.take(12)
    lods, nsurf = r.i32(), r.i32()
    if not 1 <= lods <= 3 or nsurf < 0:
        raise MapGeoError("Modell: %d LODs, %d Flächen" % (lods, nsurf))
    r.take(12)
    out = []
    for _ in range(nsurf):
        material = r.str()
        r.take(4)
        nx = r.u32()
        if nx > 7:
            raise MapGeoError("Modell: %d Zusatznamen" % nx)
        for _ in range(nx):
            r.str()
        r.take(8)
        for li in range(lods):
            if r.u32():                             # absent
                if li == 0:
                    raise MapGeoError("Modell ohne LOD 0")
                continue
            for _ in range(max(r.i32(), 0)):
                r.str()
            nv, ni, fmt = r.i32(), r.i32(), r.u32()
            if nv < 0 or ni < 0 or ni % 3 or fmt != VERTEX_FORMAT:
                raise MapGeoError("Modell: %d Ecken, %d Indizes, Format %08x" % (nv, ni, fmt))
            r.take(40)                              # quant f32[10]
            verts, idx = r.take(nv * STRIDE), r.take(ni * 2)
            r.take(36)                              # bounds f32[6], u32, density u32[2]
            if r.u32() != BMLR:
                raise MapGeoError("Modell: falsche BMLr-Marke")
            if li or not ni:
                continue
            f = array("f", verts)
            pos, uv = array("f", bytes(12 * nv)), array("f", bytes(8 * nv))
            pos[0::3], pos[1::3], pos[2::3] = f[0::12], f[1::12], f[2::12]
            uv[0::2], uv[1::2] = f[3::12], f[4::12]
            if not (all(map(math.isfinite, pos)) and all(map(math.isfinite, uv))):
                raise MapGeoError("Modell: Position oder UV nicht endlich")
            ix = array("I", array("H", idx))
            if max(ix) >= nv:
                raise MapGeoError("Modell: Index %d >= %d Ecken" % (max(ix), nv))
            ix[1::3], ix[2::3] = ix[2::3], ix[1::3]   # clockwise -> counter-clockwise
            out.append((material.lower(), pos, uv, ix))
    return out


# --------------------------------------------------------------------------
# materials
# --------------------------------------------------------------------------

def _line(text, key):
    m = re.search(r"^%s\t(.+?)\s*$" % key, text, re.M)
    return m.group(1).strip('"') if m else None


def alpha_mode(text):
    """editor: stageprogram editordraw (clip, collision, triggers -- the game
    never draws them); blend: blendprogram, decals, and stage programs (gui,
    particles, glass) of non-loose materials -- a loose one with e.g.
    outsidedisintegrate is still opaque; mask: a loose material whose cover
    map is its own (foliage, grates); else opaque. A heuristic over the
    material text."""
    if re.search(r"^stageprogram	editordraw\s*$", text, re.M):
        return "editor"
    if (re.search(r"^(blendprogram|decaldiffusemap)\t", text, re.M)
            or (not _line(text, "loosealbedo") and re.search(r"^stageprogram\t", text, re.M))):
        return "blend"
    cover = _line(text, "loosecover")
    return "mask" if cover and not cover.startswith(WHITE_COVER) else "opaque"


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def _key(paths):
    h = hashlib.sha1(b"mapgeo %d\n" % VERSION)
    for p in paths:
        for q in (Path(p), Path(p).with_suffix(".texdb")):
            if q.exists():
                st = q.stat()
                h.update(("%s|%d|%d\n" % (q, st.st_size, st.st_mtime_ns)).encode("utf-8"))
    return h.hexdigest()[:16]


def _box(box, m):
    """World AABB of a model-space AABB under the 3x4 matrix m (8 corners)."""
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    for x in (box[0][0], box[1][0]):
        for y in (box[0][1], box[1][1]):
            for z in (box[0][2], box[1][2]):
                for j in range(3):
                    w = m[4 * j] * x + m[4 * j + 1] * y + m[4 * j + 2] * z + m[4 * j + 3]
                    lo[j], hi[j] = min(lo[j], w), max(hi[j], w)
    return lo, hi


def _r(x, nd):
    x = round(x, nd)
    return int(x) if x == int(x) else x


def build(game_root, map_id, out_dir):
    """Write out_dir/scene.json + geometry.bin (atomically) and return the scene."""
    t0 = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_bin, tmp_json = out_dir / "geometry.bin.tmp", out_dir / "scene.json.tmp"
    with mapcatalog.mount(game_root, map_id) as m:
        hit = m.find("compfile", "maps/%s.entities" % map_id)
        if not hit:
            raise MapGeoError("%s: keine .entities im Container" % map_id)
        try:
            text = tnccrypt.compfile_unwrap(m.read(*hit)).decode("utf-8", "surrogateescape")
        except (tnccrypt.TncCryptError, oodle.OodleError) as exc:
            raise MapGeoError("%s: .entities nicht entpackbar (%s)" % (map_id, exc))
        try:
            ents = parse_entities(text)
        except RecursionError:
            raise MapGeoError("%s: .entities zu tief verschachtelt" % map_id)
        groups = lod_groups(m)

        entities, placed, skipped, names = [], [], Counter(), {}
        total = 0
        for ei, (name, d) in enumerate(ents):
            edit = _d(d.get("edit"))
            inherit, cls = str(d.get("inherit", "")), str(d.get("class", ""))
            try:
                mtx = affine(edit)
            except (TypeError, ValueError):
                mtx = None
                skipped["Platzierung nicht lesbar"] += 1
            entities.append({"name": name, "cat": category(inherit, cls), "cls": cls, "def": inherit,
                             "x": _r(mtx[3], 3) if mtx else 0, "y": _r(mtx[7], 3) if mtx else 0,
                             "z": _r(mtx[11], 3) if mtx else 0})
            rmi = _d(edit.get("renderModelInfo"))
            model = rmi.get("model")
            if not isinstance(model, str) or not model or model == "NULL":
                continue
            total += 1
            if mtx is None:
                continue
            si = cls == "idStaticInstanceEntity"
            k = (model.lower(), si)
            if k not in names:
                names[k] = _resolve(m, model, si, groups)
            rn, why = names[k]
            if rn is None:
                skipped[why] += 1
                continue
            cm = rmi.get("customMaterial")
            cm = cm.lower() if isinstance(cm, str) and cm and cm != "NULL" else None
            kind = "sky" if model.lower().startswith("models/skies/") else "static" if si else "dynamic"
            placed.append((rn, cm, ei, kind, mtx))

        # geometry: each model once, in order of first use
        geo, meshes, mesh_of, mats, mat_of, instances = {}, [], {}, [], {}, []
        nsurf = nvert = ntri = 0

        def mat(name):
            if name not in mat_of:
                mat_of[name] = len(mats)
                mats.append(name)
            return mat_of[name]

        with open(tmp_bin, "wb") as f:
            for rn, cm, ei, kind, mtx in placed:
                if rn not in geo:
                    try:
                        surfs = model_surfaces(m.read(*m.find("model", rn)))
                    except (MapGeoError, oodle.OodleError):
                        geo[rn] = None
                    else:
                        recs, lo, hi = [], [math.inf] * 3, [-math.inf] * 3
                        for name, pos, uv, ix in surfs:
                            off = f.tell()
                            f.write(pos.tobytes())
                            f.write(uv.tobytes())
                            f.write(ix.tobytes())
                            recs.append((name, len(pos) // 3, len(ix), off, off + 4 * len(pos),
                                         off + 4 * (len(pos) + len(uv))))
                            for j in range(3):
                                lo[j], hi[j] = min(lo[j], min(pos[j::3])), max(hi[j], max(pos[j::3]))
                            nsurf, nvert, ntri = nsurf + 1, nvert + len(pos) // 3, ntri + len(ix) // 3
                        geo[rn] = (recs, (lo, hi) if recs else None)
                if geo[rn] is None:
                    skipped["Modell nicht lesbar"] += 1
                    continue
                if not geo[rn][0]:
                    skipped["Modell ohne Dreiecke"] += 1
                    continue
                if (rn, cm) not in mesh_of:
                    mesh_of[(rn, cm)] = len(meshes)
                    meshes.append({"name": rn, "surfaces": [
                        {"material": mat(cm or name), "vcount": v, "icount": n,
                         "pos_off": po, "uv_off": uo, "idx_off": io}
                        for name, v, n, po, uo, io in geo[rn][0]]})
                instances.append({"mesh": mesh_of[(rn, cm)], "ent": ei, "kind": kind,
                                  "m": [_r(x, 4 if i % 4 == 3 else 6) for i, x in enumerate(mtx)]})
            size = f.tell()

        # materials -> albedo texture, with maptex's ids so /api/tex/<id> serves them
        materials, textures, tex_of = [], [], {}
        for name in mats:
            hit = m.find("material", name)
            try:
                text = m.read(*hit).decode("latin1") if hit else ""
                tid = maptex.albedo_for_material(m, name)
            except (oodle.OodleError, ValueError):
                hit, text, tid = None, "", None
            if tid and tid not in tex_of:
                try:
                    tex_of[tid] = maptex.texture_info(m, tid)
                    textures.append(tex_of[tid])
                except (KeyError, tncimage.TncImageError, oodle.OodleError, struct.error):
                    tex_of[tid] = None              # e.g. BC6H: maptex does not serve it
            materials.append({"name": name, "albedo": tid if tid and tex_of[tid] else None,
                              "alpha": alpha_mode(text) if hit else "opaque"})

    # bounds: all non-sky instances; core = 1st..99th percentile of their centres
    boxes = [_box(geo[meshes[i["mesh"]]["name"]][1], i["m"]) for i in instances if i["kind"] != "sky"]
    if boxes:
        bbox = {"min": [_r(min(b[0][j] for b in boxes), 2) for j in range(3)],
                "max": [_r(max(b[1][j] for b in boxes), 2) for j in range(3)]}
        cs = [sorted((b[0][j] + b[1][j]) / 2 for b in boxes) for j in range(3)]
        k = len(boxes) // 100
        core = {"min": [_r(c[k], 2) for c in cs], "max": [_r(c[-k - 1], 2) for c in cs]}
    else:
        pts = [(e["x"], e["y"], e["z"]) for e in entities] or [(0, 0, 0)]
        bbox = core = {"min": [min(p[j] for p in pts) for j in range(3)],
                       "max": [max(p[j] for p in pts) for j in range(3)]}
    by_kind = Counter(i["kind"] for i in instances)
    drawn = sum(sum(s["icount"] for s in meshes[i["mesh"]]["surfaces"]) for i in instances) // 3
    doc = {
        "meta": {
            "id": map_id,
            "title": mapcatalog._titles(game_root, [map_id])[map_id],
            "units": "m", "up": "z", "bbox": bbox, "bbox_core": core,
            "counts": {"entities": len(entities), "instances": len(instances),
                       "instances_by_kind": {k: by_kind[k] for k in KINDS},
                       "meshes": len(meshes), "models": sum(1 for g in geo.values() if g and g[0]),
                       "surfaces": nsurf, "vertices": nvert, "triangles": ntri,
                       "triangles_drawn": drawn, "materials": len(materials),
                       "textures": len(textures), "geometry_bytes": size,
                       "skipped": dict(skipped.most_common()), "seconds": round(time.time() - t0, 1)},
            "coverage": {"models_resolved": len(instances), "models_total": total,
                         "materials_textured": sum(1 for x in materials if x["albedo"]),
                         "materials_total": len(materials)},
        },
        "materials": materials, "meshes": meshes, "instances": instances,
        "entities": entities, "textures": textures,
    }
    tmp_json.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp_bin, out_dir / "geometry.bin")
    os.replace(tmp_json, out_dir / "scene.json")
    return doc


def validate(doc, bin_path, deep=False):
    """Problems (German, empty = fine) of a scene against its geometry.bin.
    Always: every surface range lies inside the file and the file ends where
    the last one does; every index into materials/meshes/entities/textures
    is in range. deep: also reads the file (indices < vcount, finite floats)."""
    bad = []
    try:
        size = os.path.getsize(bin_path)
        data = Path(bin_path).read_bytes() if deep else None
    except OSError as exc:
        return ["geometry.bin fehlt: %s" % exc]
    nmat, nmesh, nent = len(doc["materials"]), len(doc["meshes"]), len(doc["entities"])
    tex = {t["id"] for t in doc["textures"]}
    end = 0
    for mi, mesh in enumerate(doc["meshes"]):
        for si, s in enumerate(mesh["surfaces"]):
            where = "Mesh %d Fläche %d" % (mi, si)
            v, n = s["vcount"], s["icount"]
            if not 0 <= s["material"] < nmat or v <= 0 or n <= 0 or n % 3:
                bad.append("%s: Material %s, %s Ecken, %s Indizes" % (where, s["material"], v, n))
                continue
            spans = ((s["pos_off"], 12 * v), (s["uv_off"], 8 * v), (s["idx_off"], 4 * n))
            end = max([end] + [o + ln for o, ln in spans])
            if any(o < 0 or o % 4 or o + ln > size for o, ln in spans):
                bad.append("%s: Bereich außerhalb von geometry.bin (%d Bytes)" % (where, size))
                continue
            if deep:
                ix = array("I", data[s["idx_off"]:s["idx_off"] + 4 * n])
                fl = array("f", data[s["pos_off"]:s["pos_off"] + 20 * v])
                if max(ix) >= v:
                    bad.append("%s: Index %d >= %d Ecken" % (where, max(ix), v))
                if s["uv_off"] != s["pos_off"] + 12 * v or not all(map(math.isfinite, fl)):
                    bad.append("%s: Positionen/UV nicht endlich oder nicht hintereinander" % where)
    if end != size:
        bad.append("geometry.bin hat %d Bytes, scene.json beschreibt %d" % (size, end))
    for k, i in enumerate(doc["instances"]):
        if (not 0 <= i["mesh"] < nmesh or not 0 <= i["ent"] < nent or i["kind"] not in KINDS
                or len(i["m"]) != 12 or not all(map(math.isfinite, i["m"]))):
            bad.append("Instanz %d: %s" % (k, i))
    bad += ["Material %s: Textur %s fehlt" % (x["name"], x["albedo"])
            for x in doc["materials"] if x["albedo"] is not None and x["albedo"] not in tex]
    return bad


def scene(game_root, map_id, cache=True):
    """(scene dict, Path of geometry.bin) for a map id of mapcatalog.maps().
    Built once and cached; a game update (any mounted archive or .texdb
    changing size or mtime) or a damaged cache rebuilds it."""
    plan = mapcatalog._plan(game_root)
    if map_id not in plan:
        raise KeyError("Unknown map: %s" % map_id)
    d = cache_dir(map_id)
    key = _key(plan[map_id][1])
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
    ap = argparse.ArgumentParser(prog="python -m wolfsdk.mapgeo",
                                 description="Karte als scene.json + geometry.bin in den Cache schreiben")
    ap.add_argument("map_id", help="z. B. game/dlc/c02/c2v1")
    ap.add_argument("--game", help="Spielordner (sonst wie der Loader: Konfiguration, Steam)")
    ap.add_argument("--neu", action="store_true", help="Cache ignorieren und neu erzeugen")
    ap.add_argument("--pruefen", action="store_true", help="geometry.bin vollständig prüfen")
    a = ap.parse_args(argv)
    from .game import Game, GameError
    try:
        root = Game.find(a.game, "tnc").root
    except GameError as exc:
        print(exc, file=sys.stderr)
        return 2
    t = time.time()
    try:
        doc, path = scene(root, a.map_id, cache=not a.neu)
    except (KeyError, MapGeoError) as exc:
        print("Fehler: %s" % (exc.args[0] if exc.args else exc), file=sys.stderr)
        return 1
    c, cov = doc["meta"]["counts"], doc["meta"]["coverage"]
    print("%s: %d/%d Instanzen, %d Flächen, %d Dreiecke, %.1f MB, %.1f s -> %s" % (
        a.map_id, cov["models_resolved"], cov["models_total"], c["surfaces"], c["triangles"],
        path.stat().st_size / 1e6, time.time() - t, path.parent))
    if a.pruefen:
        bad = validate(doc, path, deep=True)
        print("Prüfung: %s" % ("in Ordnung" if not bad else "%d Fehler, z. B. %s" % (len(bad), bad[:3])))
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
