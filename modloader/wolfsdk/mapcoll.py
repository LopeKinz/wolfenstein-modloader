"""Collision of any Wolfenstein II map -> collision.json, cached.

    collision(game_root, map_id, cache=True) -> Path of collision.json

Ported from tools/viewer_collision.py (c2v1 only, checked by two verifiers)
and generalized over mapcatalog's mount set. The output keeps that file's
encoding (meta.encoding): hulls + base64 position/index streams on a grid.

A cm payload is  [u32 n][n * 28-byte records][u32 blob_size][Havok TAG0]
(re_probes/cmfmt). The tagfile is read through its own type table (TNA1
names + TBDY members/offsets), and three hknp shape classes become triangles:

  hknpConvexPolytopeShape   vertices + faces (fan-triangulated)
  hknpCompressedMeshShape   hkcdStaticMeshTree: 11/11/10-bit packed section
                            vertices, 21/21/22-bit shared vertices over the
                            tree domain, 4-index primitives (tri if i2 == i3)
  hknpCompoundShape         instances: hkTransform (column-major) * (scale . p)

Every cm is MODEL space except maps/<id>/_combo/world.hkshape (world space,
named by no entity). An entity places its cm with
world = spawnPosition + x*mat[0] + y*mat[1] + z*mat[2]; the cm variant is
picked by its $scale= name part, so entity scale is not applied again. An
.idasset (vertex-painted static) goes through its modelAsset's
collisionSetup.sourceModel first.

Cache: %LOCALAPPDATA%/wolfsdk/mapcache/<map id>/collision.json, keyed by
size + mtime of every archive the map mounts, so a game update rebuilds it.
Read-only on the game install. stdlib + wolfsdk (idcl, oodle, tnccrypt).
"""

import base64
import hashlib
import json
import math
import os
import re
import struct
import sys
import tempfile
import threading
from array import array
from collections import Counter
from pathlib import Path

from . import mapcatalog, oodle, tnccrypt

VERSION = 1             # bump when the output changes; invalidates every cache
STEP = 0.02             # grid in meters; doubled for maps wider than 65535 steps
KINDS = ["world", "static", "dynamic", "clip", "trigger", "volume"]
SOLID = ("world", "static", "dynamic", "clip")
EMPTY = "leere cm-Nutzlast (usize 0)"
LIMIT = 100000          # meters; retail maps span a few km


class CmError(Exception):
    pass


# --------------------------------------------------------------------------
# Havok TAG0 tagfile, typed by its own TNA1/TBDY tables
# --------------------------------------------------------------------------

def _packed(b, i):
    x = b[i]
    if x < 0x80:
        return x, i + 1
    if x < 0xC0:
        return ((x & 0x3F) << 8) | b[i + 1], i + 2
    if x < 0xE0:
        return ((x & 0x1F) << 16) | (b[i + 1] << 8) | b[i + 2], i + 3
    if x < 0xE8:
        return ((x & 0x07) << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3], i + 4
    raise CmError("gepackte Zahl mit Praefix %02x" % x)


def _sections(buf, off, end, out):
    while off < end:
        hdr = struct.unpack_from(">I", buf, off)[0]
        size = hdr & 0x3FFFFFFF
        if size < 8 or off + size > end:
            raise CmError("Sektion ueberlaeuft bei %d" % off)
        tag = bytes(buf[off + 4:off + 8]).decode("latin1")
        if hdr & 0x40000000:
            out[tag] = buf[off + 8:off + size]
        else:
            _sections(buf, off + 8, off + size, out)
        off += size


class Tag:
    """One cm payload: types, items and DATA, with lazy pointer/array access."""

    def __init__(self, payload):
        n = struct.unpack_from("<I", payload, 0)[0]
        p = 4 + 28 * n
        if n < 1 or p + 4 > len(payload):         # retail: up to 5926 records
            raise CmError("unplausible Record-Anzahl %d" % n)
        blob = struct.unpack_from("<I", payload, p)[0]
        if p + 4 + blob != len(payload) or payload[p + 8:p + 12] != b"TAG0":
            raise CmError("blob_size oder TAG0-Kennung passt nicht")
        s = {}
        _sections(memoryview(payload), p + 4, len(payload), s)
        missing = {"DATA", "TSTR", "FSTR", "TNA1", "TBDY", "ITEM"} - set(s)
        if missing:
            raise CmError("TAG0 ohne Sektion %s" % ", ".join(sorted(missing)))
        self.data = bytes(s["DATA"])
        tstr = bytes(s["TSTR"]).split(b"\0")
        fstr = bytes(s["FSTR"]).split(b"\0")
        b = s["TNA1"]
        nt, i = _packed(b, 0)
        self.T = [dict(name="void", parent=0, fmt=0, sub=0, size=0, members=())]
        for _ in range(1, nt):
            ni, i = _packed(b, i)
            na, i = _packed(b, i)
            for _ in range(2 * na):
                _, i = _packed(b, i)
            self.T.append(dict(name=tstr[ni].decode("latin1"), parent=0, fmt=None,
                               sub=0, size=None, members=[]))
        b, i = s["TBDY"], 0
        while i < len(b):
            t, i = _packed(b, i)
            if t == 0:
                continue
            d = self.T[t]
            d["parent"], i = _packed(b, i)
            opt, i = _packed(b, i)
            if opt & 1:
                d["fmt"], i = _packed(b, i)
            if opt & 2:
                d["sub"], i = _packed(b, i)
            if opt & 4:
                _, i = _packed(b, i)
            if opt & 8:
                d["size"], i = _packed(b, i)
                _, i = _packed(b, i)
            if opt & 0x10:
                _, i = _packed(b, i)
            if opt & 0x20:
                nm, i = _packed(b, i)
                for _ in range(nm):
                    fn, i = _packed(b, i)
                    _, i = _packed(b, i)
                    fo, i = _packed(b, i)
                    ft, i = _packed(b, i)
                    d["members"].append((fstr[fn].decode("latin1"), fo, ft))
            if opt & 0x40:
                k, i = _packed(b, i)
                for _ in range(2 * k):
                    _, i = _packed(b, i)
            if opt & 0x80:
                _, i = _packed(b, i)
            if opt >= 0x100:
                raise CmError("TBDY-Optionsbits %x" % opt)
        it = bytes(s["ITEM"])
        self.items = [struct.unpack_from("<III", it, k) for k in range(0, len(it) - 11, 12)]

    # -- types (a corrupt parent chain must not loop forever) ------------------
    def _base(self, t):
        """Follow alias typedefs (no own format) to the type that has one."""
        for _ in self.T:
            if self.T[t]["fmt"] is not None:
                return t
            t = self.T[t]["parent"]
        raise CmError("Typkette ohne Ende")

    def size(self, t):
        return self.T[self._base(t)]["size"]

    def name(self, item):
        return self.T[self.items[item][0] & 0xFFFFFF]["name"]

    def _members(self, t):
        out = []
        for _ in self.T:
            if not t:
                return out
            out.extend(self.T[t]["members"])
            t = self.T[t]["parent"]
        raise CmError("Elternkette ohne Ende")

    # -- values ---------------------------------------------------------------
    def read(self, t, off):
        t = self._base(t)
        f = self.T[t]["fmt"]
        k = f & 0xFF
        d = self.data
        if k == 0x07:
            return {n: self.read(ft, off + fo) for n, fo, ft in self._members(t)}
        if k in (0x06, 0x08):          # pointer / array: item index, resolved lazily
            return struct.unpack_from("<I", d, off)[0]
        if k == 0x28:                  # fixed array T[N], element type = TBDY sub
            st = self.T[t]["sub"]
            return [self.read(st, off + j * self.size(st)) for j in range(f >> 8)]
        if k == 0x04:
            code = {8: "b", 16: "h", 32: "i", 64: "q"}[f >> 10]
            if not f & 0x200:
                code = code.upper()
            return struct.unpack_from("<" + code, d, off)[0]
        if k == 0x05:
            return struct.unpack_from("<f", d, off)[0] if self.size(t) == 4 else \
                struct.unpack_from("<e", d, off)[0]
        if k == 0x02:
            return d[off] != 0
        return None

    def obj(self, item):
        t, off, _ = self.items[item]
        return self.read(t & 0xFFFFFF, off)

    def _span(self, item):
        t, off, cnt = self.items[item]
        t &= 0xFFFFFF
        sz = self.size(t)
        if cnt > len(self.data) or off + cnt * (sz or 1) > len(self.data):
            raise CmError("Array %d reicht ueber DATA hinaus" % item)
        return t, off, cnt, sz

    def raw(self, item, code):
        """Array item as a flat stdlib array of `code` (e.g. 'f', 'I', 'H', 'Q')."""
        a = array(code)
        if item == 0:
            return a
        _, off, cnt, sz = self._span(item)
        a.frombytes(self.data[off:off + cnt * sz // a.itemsize * a.itemsize])
        return a

    def arr(self, item):
        if item == 0:
            return []
        t, off, cnt, sz = self._span(item)
        return [self.read(t, off + j * sz) for j in range(cnt)]


# --------------------------------------------------------------------------
# hknp shapes -> triangles (shape space)
# --------------------------------------------------------------------------

def shape_hulls(tag, item, depth=0):
    """[(class, verts[(x,y,z)], tris[(a,b,c)])] for the shape object `item`."""
    cls = tag.name(item)
    o = tag.obj(item)
    if cls == "hknpConvexPolytopeShape":
        v = tag.raw(o["vertices"], "f")
        verts = [(v[i], v[i + 1], v[i + 2]) for i in range(0, len(v) - 3, 4)]
        idx = tag.raw(o["indices"], "B")
        tris = []
        for f in tag.arr(o["faces"]):
            ring = idx[f["firstIndex"]:f["firstIndex"] + f["numIndices"]]
            tris += [(ring[0], ring[k], ring[k + 1]) for k in range(1, len(ring) - 1)]
        if any(i >= len(verts) for t in tris for i in t):
            raise CmError("Flaechenindex ausserhalb der Vertices")
        return [(cls, verts, tris)]
    if cls == "hknpCompressedMeshShape":
        return [(cls,) + compressed_mesh(tag, tag.obj(o["data"])["meshTree"])]
    if cls in ("hknpCompoundShape", "hknpStaticCompoundShape", "hknpDynamicCompoundShape"):
        if depth > 8:
            raise CmError("Compound zu tief verschachtelt")
        out = []
        for inst in tag.arr(o["instances"]["elements"]):
            if inst["isEmpty"] or not inst["shape"]:
                continue
            m = inst["transform"]          # 3 rotation columns + translation, w ignored
            sx, sy, sz = inst["scale"][:3]
            for c, verts, tris in shape_hulls(tag, inst["shape"], depth + 1):
                vv = []
                for x, y, z in verts:
                    x, y, z = x * sx, y * sy, z * sz
                    vv.append((m[0] * x + m[4] * y + m[8] * z + m[12],
                               m[1] * x + m[5] * y + m[9] * z + m[13],
                               m[2] * x + m[6] * y + m[10] * z + m[14]))
                if sx * sy * sz < 0:       # mirrored instance: keep faces outward
                    tris = [(a, c_, b) for a, b, c_ in tris]
                out.append((c, vv, tris))
        return out
    raise CmError("nicht unterstuetzte Shape-Klasse %s" % cls)


def compressed_mesh(tag, mt):
    """hkcdStaticMeshTree -> (verts, tris). Raises CmError if any vertex
    decodes outside its declared AABB (section domain / tree domain)."""
    dmin, dmax = mt["domain"]["min"][:3], mt["domain"]["max"][:3]
    ext = [dmax[k] - dmin[k] for k in range(3)]
    packed = tag.raw(mt["packedVertices"], "I")
    shared = tag.raw(mt["sharedVertices"], "Q")
    svi = tag.raw(mt["sharedVerticesIndex"], "H")
    prims = tag.raw(mt["primitives"], "B")
    sv = [(dmin[0] + (q & 0x1FFFFF) * ext[0] / 0x1FFFFF,
           dmin[1] + ((q >> 21) & 0x1FFFFF) * ext[1] / 0x1FFFFF,
           dmin[2] + (q >> 42) * ext[2] / 0x3FFFFF) for q in shared]
    tol = 1e-3 + 1e-4 * max(ext)
    for p in sv:
        if any(p[k] < dmin[k] - tol or p[k] > dmax[k] + tol for k in range(3)):
            raise CmError("geteilter Vertex ausserhalb der Baum-Domain")
    verts, tris = [], []
    for s in tag.arr(mt["sections"]):
        cp = s["codecParms"]
        smin, smax = s["domain"]["min"], s["domain"]["max"]
        base = len(verts)
        npv = s["numPackedVertices"]
        f0 = s["firstPackedVertex"]
        for q in packed[f0:f0 + npv]:
            p = (cp[0] + (q & 0x7FF) * cp[3],
                 cp[1] + ((q >> 11) & 0x7FF) * cp[4],
                 cp[2] + (q >> 22) * cp[5])
            if any(p[k] < smin[k] - tol or p[k] > smax[k] + tol for k in range(3)):
                raise CmError("gepackter Vertex ausserhalb der Sektions-Domain")
            verts.append(p)
        # SharedVertices.data = offset into sharedVerticesIndex << 8 | first
        # shared local index (== numPackedVertices); count = numSharedIndices
        s_off = s["sharedVertices"]["data"] >> 8
        if s["sharedVertices"]["data"] & 0xFF != npv:
            raise CmError("Basis der geteilten Vertices != numPackedVertices")
        local = list(range(base, base + npv))
        for j in range(s["numSharedIndices"]):
            local.append(len(verts))
            verts.append(sv[svi[s_off + j]])
        p_off = s["primitives"]["data"] >> 8
        for j in range(s["primitives"]["data"] & 0xFF):
            a, b, c, d = prims[4 * (p_off + j):4 * (p_off + j) + 4]
            if (a, b, c, d) == (0xDE, 0xAD, 0xDE, 0xAD):    # removed primitive
                continue
            tris.append((local[a], local[b], local[c]))
            if c != d:
                tris.append((local[a], local[c], local[d]))
    return verts, tris


def decode(payload):
    """cm payload -> [(class, verts, tris)] in shape space. A damaged or
    foreign payload raises CmError with a readable reason, never anything else."""
    try:
        hulls = shape_hulls(Tag(payload), 1)
    except CmError:
        raise
    except (struct.error, KeyError, IndexError, ValueError, TypeError, OverflowError,
            ZeroDivisionError, RecursionError, UnicodeError, MemoryError) as ex:
        raise CmError("TAG0 beschaedigt (%s: %s)" % (type(ex).__name__, ex)) from None
    for _, v, t in hulls:           # NaN/inf or absurd floats would wreck the grid
        if not all(-LIMIT < c < LIMIT for p in v for c in p):
            raise CmError("Vertex nicht endlich oder weiter als %d m weg" % LIMIT)
        if any(i >= len(v) for tri in t for i in tri):
            raise CmError("Dreieck zeigt auf fehlenden Vertex")
    return hulls


# --------------------------------------------------------------------------
# .entities -> placements
# --------------------------------------------------------------------------

_TOK = re.compile(r'"[^"]*"|[{}=;]|[^\s{}=;"]+')
_BANG = re.compile(r"=\s*!\s*\{")      # `renderModelInfo = ! {` (c3v2 & co.)


def parse_entities(text):
    """[(name, def-block dict)] from idTech 6 .entities text, file order."""
    tok = _TOK.findall(_BANG.sub("= {", text))

    def block(i):
        out = {}
        while tok[i] != "}":
            k = tok[i]
            i += 1
            if tok[i] == "=":
                if tok[i + 1] == "{":
                    v, i = block(i + 2)
                else:
                    v, i = tok[i + 1].strip('"'), i + 2
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
                raise CmError(".entities: unerwartetes Token %r" % tok[i])
            e, i = block(i + 2)
            (k, v), = [(k, v) for k, v in e.items() if k.startswith("entityDef ")]
            ents.append((k[10:], v))
    except (IndexError, ValueError, RecursionError) as ex:
        raise CmError(".entities nicht lesbar (Entity %d: %s)" % (len(ents), ex)) from None
    return ents


def _vec(b, default):
    b = b if isinstance(b, dict) else {}
    return tuple(float(b.get(k, d)) for k, d in zip("xyz", default))


def placement(edit):
    """(origin, rows) with idTech delta defaults (identity / zero)."""
    org = _vec(edit.get("spawnPosition"), (0, 0, 0))
    mat = (edit.get("spawnOrientation") or {}).get("mat") or {}
    rows = [_vec(mat.get("mat[%d]" % r), [1.0 if c == r else 0.0 for c in range(3)])
            for r in range(3)]
    return org, rows


def kind_of(inherit, cls):
    s = (inherit + " " + cls).lower()
    if "trigger" in s:
        return "trigger"
    if "volume" in s or "soundportal" in s or "soundclip" in s:
        return "volume"
    if inherit.startswith("func/collision_only"):
        return "clip"
    if inherit.startswith("func/static"):
        return "static"
    return "dynamic"


NO_CM_TYPES = ("CLIPMODEL_NONE", "CLIPMODEL_BOX", "CLIPMODEL_CYLINDER")


def entity_clip(edit):
    """(clip model name, how) or (None, reason)."""
    cmi = edit.get("clipModelInfo") or {}
    rmi = edit.get("renderModelInfo") or {}
    typ = cmi.get("type", "CLIPMODEL_AUTO")
    if typ in NO_CM_TYPES:
        return None, typ + (" (no collision)" if typ == "CLIPMODEL_NONE"
                            else " (procedural, no cm resource)")
    clip = cmi.get("clipModelName")
    if clip and clip != "NULL":
        return clip, "clipModelName"
    model = rmi.get("model")
    if clip is None and model and model != "NULL":
        return model, "render model"
    return None, "no clip model"


# --------------------------------------------------------------------------
# cm lookup over a mount
# --------------------------------------------------------------------------

FILTER_BITS = {"playerClip": 0x08, "monsterClip": 0x10, "moveableClip": 0x40, "shotClip": 0x80}
_COLSETUP = re.compile(r'collisionSetup\s*=\s*\{[^}]*?sourceModel\s*=\s*"([^"]+)"', re.S)


def _ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name[-12:] else "none"


class CmIndex:
    """base model name -> [(params, archive, entry)] over a mapcatalog.Mount,
    first archive wins for identical full names."""

    def __init__(self, m):
        self.m, self.by_base, self._assets = m, {}, {}
        for a, e in m.entries("cm"):
            base, *rest = e.name.lower().split("$")
            params = dict(r.split("=", 1) if "=" in r else (r, "") for r in rest)
            self.by_base.setdefault(base, []).append((params, a, e))

    def resolve(self, clip, scale, material, contents):
        """-> (archive, entry, note) or (None, None, reason).

        Model cm names are  <model>$scale=%e,%e,%e$filterclip=<hex>
        $overridematerial=<decl>$simplify=20$filteroccluders=true  with each
        part present only when it differs from the default. filterclip is the
        OR of the contentsFilter flags set to false (c2v1: playerClip 0x08,
        monsterClip 0x10, moveableClip 0x40, shotClip 0x80)."""
        clip = clip.lower()
        if clip.startswith("maps/"):
            c = self.by_base.get(clip + ".hkshape") or self.by_base.get(clip)
            return (c[0][1], c[0][2], "exact") if c else (None, None, "no cm entry")
        cands = self.by_base.get(clip)
        if not cands:
            return None, None, "no cm entry"
        want = None if scale == (1.0, 1.0, 1.0) else ",".join("%e" % s for s in scale)
        c = [x for x in cands if x[0].get("scale") == want]
        if not c:
            return None, None, "no cm with this $scale"
        mat = (material or "").lower() or None
        c = [x for x in c if x[0].get("overridematerial") == mat] or c
        bits = [FILTER_BITS.get(k) for k, v in (contents or {}).items() if v == "false"]
        if None not in bits:
            fc = sum(bits)
            f = [x for x in c if x[0].get("filterclip") == ("%x" % fc if fc else None)]
            if f:
                return f[0][1], f[0][2], "exact"
        return c[0][1], c[0][2], "filterclip guessed"

    def asset_source(self, name):
        """collisionSetup.sourceModel of an .idasset's modelAsset, or None."""
        if name not in self._assets:
            hit = (self.m.find("modelAsset", name)
                   or self.m.find("modelAsset", name[:-len("idasset")] + "decl"))
            col = hit and _COLSETUP.search(self.m.read(*hit).decode("latin1"))
            self._assets[name] = col.group(1) if col else None
        return self._assets[name]

    def entity_cm(self, edit):
        """(archive, entry, note) of the cm this entity collides with, or
        (None, None, reason)."""
        clip, how = entity_clip(edit)
        if clip is None:
            return None, None, how
        if _ext(clip) == "idasset" and self.asset_source(clip):
            clip, how = self.asset_source(clip), how + " -> collisionSetup"
        rmi = edit.get("renderModelInfo") or {}
        cmi = edit.get("clipModelInfo") or {}
        a, e, note = self.resolve(clip, _vec(rmi.get("scale"), (1, 1, 1)),
                                  rmi.get("customMaterial"), cmi.get("contentsFilter"))
        if a is None:
            return None, None, "%s (.%s via %s)" % (note, _ext(clip), how)
        return a, e, note


# --------------------------------------------------------------------------
# build: every collision hull of one map, streamed into the grid encoding
# --------------------------------------------------------------------------

def _place(v, org, rows):
    if org is None:
        return v
    r0, r1, r2 = rows
    return [(org[0] + x * r0[0] + y * r1[0] + z * r2[0],
             org[1] + x * r0[1] + y * r1[1] + z * r2[1],
             org[2] + x * r0[2] + y * r1[2] + z * r2[2]) for x, y, z in v]


def _det(r0, r1, r2):
    return (r0[0] * (r1[1] * r2[2] - r1[2] * r2[1]) - r0[1] * (r1[0] * r2[2] - r1[2] * r2[0])
            + r0[2] * (r1[0] * r2[1] - r1[1] * r2[0]))


class _Grid:
    """Weld per hull on the grid, drop triangles that collapse, and pack."""

    def __init__(self, origin, step):
        self.origin, self.step = origin, step
        self.p8, self.i8 = bytearray(), bytearray()
        self.p16, self.i16 = array("H"), array("H")
        self.rec, self.nv, self.nt, self.per_kind = [], 0, 0, Counter()

    def add(self, kind, ent, v, t):
        o, s, key = self.origin, self.step, {}
        m = [key.setdefault((round((p[0] - o[0]) / s), round((p[1] - o[1]) / s),
                             round((p[2] - o[2]) / s)), len(key)) for p in v]
        tt = [(m[a], m[b], m[c]) for a, b, c in t if m[a] != m[b] and m[b] != m[c] and m[a] != m[c]]
        if not tt:
            return
        used = sorted({i for tri in tt for i in tri})
        remap = {o: n for n, o in enumerate(used)}
        q = list(key)
        q = [q[o] for o in used]
        tt = [(remap[a], remap[b], remap[c]) for a, b, c in tt]
        if len(q) > 65536:
            raise CmError("Huelle mit %d Vertices muesste geteilt werden" % len(q))
        base = [min(x[i] for x in q) for i in range(3)]
        if all(max(x[i] for x in q) - base[i] <= 255 for i in range(3)):
            bits = 8
            for x in q:
                self.p8 += bytes(x[i] - base[i] for i in range(3))
        else:
            bits, base = 16, [0, 0, 0]
            for x in q:
                self.p16.extend(x)
        (self.i8.extend if len(q) <= 256 else self.i16.extend)(i for tri in tt for i in tri)
        self.rec.append([KINDS.index(kind), ent, len(q), len(tt), bits] + base)
        self.nv += len(q)
        self.nt += len(tt)
        self.per_kind[kind] += len(tt)


def _box(lo, hi):
    return {"min": [round(x, 2) for x in lo], "max": [round(x, 2) for x in hi]} \
        if lo[0] <= hi[0] else None


def _inside(points, lo, hi):
    return sum(1 for p in points if all(lo[i] <= p[i] <= hi[i] for i in range(3)))


def build(m, map_id):
    """collision.json document of `map_id` over the mapcatalog.Mount `m`."""
    name = "maps/%s.entities" % map_id
    hit = m.find("compfile", name)
    if hit is None:
        raise CmError("%s: keine .entities im Container" % map_id)
    try:
        text = tnccrypt.compfile_unwrap(m.read(*hit))
    except (tnccrypt.TncCryptError, oodle.OodleError) as ex:
        raise CmError("%s: .entities nicht entpackbar (%s)" % (map_id, ex)) from None
    ents = parse_entities(text.decode("utf-8", "surrogateescape"))
    idx = CmIndex(m)
    shapes, fails, st = {}, Counter(), Counter()

    def hulls_of(a, e):
        k = (str(a.path), e.index)
        if k not in shapes:
            try:
                shapes[k] = decode(m.read(a, e)) if e.usize else EMPTY
            except (CmError, oodle.OodleError) as ex:
                shapes[k] = str(ex)
            if isinstance(shapes[k], str):
                fails[shapes[k]] += 1
        return shapes[k]

    todo = []              # (kind, entity index, hulls, origin or None, rows)
    positions, explicit = [], []
    wname = "maps/%s/_combo/world.hkshape" % map_id
    world = m.find("cm", wname)
    hs = hulls_of(*world) if world else "missing"
    if isinstance(hs, str):
        st["skip: world.hkshape " + ("empty" if hs == EMPTY else hs)] += 1
    else:
        todo.append(("world", -1, hs, None, None))
        st["placed: world.hkshape (identity)"] += 1
    for ei, (_, d) in enumerate(ents):
        edit = d.get("edit") or {}
        try:
            org, rows = placement(edit)
            a, e, note = idx.entity_cm(edit)
        except ValueError:                  # a number that is none (user-made maps)
            st["skip: unreadable number"] += 1
            continue
        if not all(-LIMIT < c < LIMIT for c in org + rows[0] + rows[1] + rows[2]):
            st["skip: placement not finite or out of range"] += 1
            continue
        positions.append(org)
        if "spawnPosition" in edit:
            explicit.append(org)
        if a is None:
            st["skip: " + note] += 1
            continue
        st["cm from " + a.path.name] += 1
        st["cm match " + note] += 1
        hs = hulls_of(a, e)
        if isinstance(hs, str):
            st["skip: cm payload " + ("empty (usize 0)" if hs == EMPTY else "does not decode")] += 1
            continue
        st["placed: entity"] += 1
        todo.append((kind_of(d.get("inherit", ""), d.get("class", "")), ei, hs, org, rows))

    # pass 1: bounding boxes (the grid origin depends on them)
    inf = float("inf")
    lo, hi, slo, shi = [inf] * 3, [-inf] * 3, [inf] * 3, [-inf] * 3
    for kind, _, hs, org, rows in todo:
        for _, v, _ in hs:
            if not v:
                continue
            w = _place(v, org, rows)
            for i in range(3):
                c = [p[i] for p in w]
                a, b = min(c), max(c)
                lo[i], hi[i] = min(lo[i], a), max(hi[i], b)
                if kind in SOLID:
                    slo[i], shi[i] = min(slo[i], a), max(shi[i], b)
    step = STEP
    if lo[0] > hi[0]:
        origin = [0.0, 0.0, 0.0]
    else:
        while max((hi[i] - lo[i]) / step for i in range(3)) > 65000:
            step *= 2
        origin = [math.floor(lo[i] / step) * step for i in range(3)]

    # pass 2: world space + grid encoding, one hull at a time
    g = _Grid(origin, step)
    for kind, ent, hs, org, rows in todo:
        flip = org is not None and _det(*rows) < 0
        for _, v, t in hs:
            if flip:
                t = [(p, r, q) for p, q, r in t]
            g.add(kind, ent, _place(v, org, rows), t)
    if sys.byteorder != "little":
        g.p16.byteswap()
        g.i16.byteswap()

    decoded = sum(1 for v in shapes.values() if not isinstance(v, str))
    empty = sum(1 for v in shapes.values() if v == EMPTY)
    placed = st["placed: world.hkshape (identity)"] + st["placed: entity"]
    meta = {
        "map": map_id,
        "version": VERSION,
        "source": {
            "entities": "%s:%s" % (hit[0].path.name, name),
            "world_cm": ("%s:%s" % (world[0].path.name, wname)) if world else None,
            "archives": [str(a.path) for a in m.archives],
            "cm_per_archive": {k[8:]: v for k, v in st.items() if k.startswith("cm from ")},
        },
        "coverage": {
            "cm_payloads": {"distinct": len(shapes), "decoded": decoded, "empty": empty,
                            "failed": len(shapes) - decoded - empty},
            "cm_decode_failures": {k: v for k, v in fails.items() if k != EMPTY},
            "instances_placed": placed,
            "instances_resolved": placed + st["skip: cm payload does not decode"]
            + st["skip: cm payload empty (usize 0)"],
            "cm_match": {k[9:]: v for k, v in st.items() if k.startswith("cm match ")},
            "skipped_entities": {k[6:]: v for k, v in sorted(st.items()) if k.startswith("skip: ")},
        },
        "counts": {"hulls": len(g.rec), "vertices": g.nv, "triangles": g.nt,
                   "triangles_per_kind": dict(g.per_kind), "entities_in_file": len(ents)},
        "units": "m",
        "coords": "id Tech: right-handed, +Z up. three.js (Y up): (x, y, z) -> (x, z, -y)",
        "space": "world = spawnPosition + x*mat[0] + y*mat[1] + z*mat[2]; winding flipped when"
                 " det < 0; _combo/world.hkshape is already world space",
        "bbox_all": _box(lo, hi),
        "bbox_solid": dict(_box(slo, shi), kinds=list(SOLID)) if slo[0] <= shi[0] else None,
        "sanity": {
            "entity_positions_in_bbox_all": [_inside(positions, lo, hi), len(positions)],
            "entity_positions_in_bbox_solid": [_inside(positions, slo, shi), len(positions)],
            "explicit_spawnPositions_in_bbox_solid": [_inside(explicit, slo, shi), len(explicit)],
        },
        "kinds": {"world": "_combo/world.hkshape", "static": "inherit func/static*",
                  "dynamic": "any other entity with a cm", "clip": "inherit func/collision_only",
                  "trigger": "'trigger' in inherit/class (not solid)",
                  "volume": "'volume'/soundportal/soundclip in inherit/class (not solid)"},
        "encoding": {
            "grid_origin": origin, "step": step,
            "hull": "[kind, ent, nv, nt, bits, bx, by, bz]; kind indexes `kinds`; ent = 0-based"
                    " entity {} index in file order (same order as viewer/entities.json ents),"
                    " -1 for world.hkshape",
            "positions": "walk hulls in order. bits 8: nv*3 bytes from p8, grid = (bx,by,bz) +"
                         " byte; bits 16: nv*3 Uint16LE from p16, grid = value."
                         " world = grid_origin + grid*step",
            "indices": "nt*3 per hull, local to the hull's own vertices: Uint8 from i8 when"
                       " nv <= 256, else Uint16LE from i16. Winding as Havok stores it (not"
                       " verified per hull); render double-sided",
            "welding": "vertices welded per hull on the grid; triangles that collapse on the"
                       " grid are dropped",
        },
    }

    def b64(b):
        return base64.b64encode(b).decode("ascii")
    return {"meta": meta, "kinds": KINDS, "hulls": g.rec, "p8": b64(g.p8),
            "p16": b64(g.p16.tobytes()), "i8": b64(g.i8), "i16": b64(g.i16.tobytes())}


# --------------------------------------------------------------------------
# public entry point + cache
# --------------------------------------------------------------------------

_LOCK = threading.Lock()   # ponytail: one build at a time; per-map locks if the server needs more


def cache_dir(map_id):
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") \
        / "wolfsdk" / "mapcache"
    d = (root / map_id).resolve()
    if root.resolve() not in d.parents:
        raise KeyError("Invalid map id: %r" % map_id)
    return d


def cache_key(paths, map_id):
    h = hashlib.sha1(("mapcoll %d %s\n" % (VERSION, map_id)).encode("utf-8"))
    for p in paths:
        st = Path(p).stat()
        h.update(("%s|%d|%d\n" % (p, st.st_size, st.st_mtime_ns)).encode("utf-8"))
    return h.hexdigest()[:16]


def collision(game_root, map_id, cache=True):
    """Path of the map's collision.json, built on first use or when the game
    changed. Unknown id -> KeyError; unusable .entities -> CmError."""
    plan = mapcatalog._plan(game_root)
    if map_id not in plan:
        raise KeyError("Unknown map: %s" % map_id)
    paths = plan[map_id][1]
    out = cache_dir(map_id) / "collision.json"
    stamp = out.with_name("collision.key")
    with _LOCK:
        key = cache_key(paths, map_id)
        try:
            if cache and out.exists() and stamp.read_text(encoding="ascii") == key:
                return out
        except OSError:
            pass
        with mapcatalog.Mount(paths, game_root) as m:
            doc = build(m, map_id)
        doc["meta"]["cache_key"] = key
        out.parent.mkdir(parents=True, exist_ok=True)
        stamp.unlink(missing_ok=True)
        fd, tmp = tempfile.mkstemp(dir=out.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, separators=(",", ":"))
            os.replace(tmp, out)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        stamp.write_text(key, encoding="ascii")
    return out
