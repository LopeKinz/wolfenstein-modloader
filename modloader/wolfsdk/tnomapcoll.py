"""Collision of a Wolfenstein: The New Order map -> collision.json, cached.

    collision(game_root, map_id, cache=True) -> Path of collision.json

Same output as wolfsdk.mapcoll (Wolfenstein II), so the viewer needs only a
game switch: hulls + base64 position/index streams on a grid (meta.encoding),
metres, +Z up, kinds world/static/dynamic/clip/trigger/volume. map_id is an
id of tnomap.maps() (e.g. game/wolf/c01/c01p1); mount, entity order and the
entity matrix are tnomap's, so hull `ent` indexes scene.json entities.

Asset type `cm` (id Tech 5), measured on the pristine archives by
tools/probe_tno_cm.py (all 8661 payloads parse and re-write byte-exact):

  .bcm  BIG endian   'BCM9' u32 version u32 stamp u32 crc f32 bbox[6]
        version 0:   u32 contents u32 flags u32 nNodes, nNodes x {i32 axis
                     f32 dist i32 c0 i32 c1} (kd tree, child < 0 = leaf -(c+1)),
                     u32 nLeaves, nLeaves x {u32 size u32 32 f32 bbox[6] BLOCK},
                     'BCM9' [stream areas 'BCM9']
          flags 0          model / entity clip, 1 leaf, block inline
          flags 0x1000000  world, blocks inline
          flags 0x1000001  world, blocks in <name>.tbcm (little endian,
                           '9MCB' u32 0 u32 crc u32 stamp BLOCK.. '9MCB')
        version 1:   md6 (skinned) collision: spheres bound to skeleton
                     joints -- not placed here (no skeleton decoded)
  BLOCK  0x70-byte head (u32 size x2, f32 bbox[6], u32 0, 9 x {count,
         offset}, u32 junk), then nodes, refs, materials {u32 contents
         u32 flags u32 surfType u8[4]}, polygons {i16 bounds[6] u8 material
         u8 nEdges u16 firstEdge}, edgerefs (u16: bit15 = edge reversed,
         bits 0-13 edge), edges {u16 v0 v1}, vertices {f32 xyz 0}, brushes,
         planes. A polygon is the closed loop of its edge refs (23 096 862
         retail polygons, every loop closes; 208 have < 3 edges and are
         dropped); it is fan-triangulated. Retail has no brushes.

Contents bits: the EXE's own constant table (CONTENTS_SOLID 1, OPAQUE 2,
WATER 4, PLAYERCLIP 8, MONSTERCLIP 16, VEHICLECLIP 32, MOVEABLECLIP 64,
SHOTCLIP 128, IKCLIP 256, ... TRIGGER 16384, AAS_OBSTACLE 1<<27); retail
trigger/volume clips carry 0x4000, aas/obstacle 0x8000000, as the table says.
World polygons: nothing that blocks (no bit of 0x1F9) -> volume; no SOLID /
OPAQUE / SHOTCLIP, or material flag 0x100 / 0x4000 -> clip (invisible player
/ monster / ik clip); the rest -> world. tools/verify_tnomapcoll.py measures
on every map how many of their vertices lie on tnomap's _world render mesh
(same grid, +-1 cell): world 70..99.8 %, median 97 % (c12p1 70 %: separately
authored solid collision); clip 0..47 %, median 15 %; chance ~5 %.

Not placed (counted per map in meta.coverage): md6 v1 sphere collision,
procedural clip shapes, maps/<id>/_fracture/part_* (debris that exists only
after something breaks; every other map-local cm is placed).

Grid: the finest of 0.005 m * 2^k that holds every hull wider than 254 steps
in 16 bit. Kilometre clip / volume boxes force 0.08 m on c01p1, c01p4,
c03p1, c06p1, c12p1, c12p2: welding there drops sliver triangles (c01p1
world 90 638 -> 44 598) but keeps every solid kind's area (99.7..105.5 %).

Entities (file order, tnomap/mapgeo.parse_entities): clipModelInfo.type
NONE / BOX / BOUNDINGBOX / CYLINDER / CONE -> no cm (procedural shapes, as in
mapcoll); maps/<id>/_combo/megabreakable_<entity>_base (987 breakables);
else clipModelName, else (clipModelName absent) renderModelInfo.model, each
a cm entry of that exact name in the map's chunk, then chunk0. An entity
cm is in the entity's frame: world = mapgeo.affine(edit) (renderModelInfo
scale included, like tnomap's render instances; TNO cm names carry no
$scale variant) with the translation in metres. Except (header bbox):
  - authored in world coordinates (tnomap.world_space: far from its own
    origin, already around the entity origin; c16p1 mover_panel, c01p2
    ..._mg1_broken): identity, not moved a second time;
  - an idBinaryModel (its render is merged into the world in world
    coordinates) moved off the origin whose cm is off_origin but not
    around it: c01p2 ..._mg2_broken reuses mg1's world-space
    _convert.lwo 139 m away; no placement fits, not placed (counted).
Merged idBinaryModel entities are otherwise placed with their own cm like
any other entity. Checked against tnomap: entity hull bbox inside the
bbox of its placed model or merged _world group (+10 %); rotation
transposed / translation doubled fail (tools/verify_tnomapcoll.py).

Units: 1 unit = 0.01905 m (tnomap.M_PER_UNIT, EXE constants DOOR_HEIGHT_*).

Cache: %LOCALAPPDATA%/wolfsdk/mapcache/tno/<map id>/collision.json (+
collision.key: VERSION, size + mtime of every archive pair). Read-only on
the game; reads base/_wolfsdk_backup/ (originals) where both files exist.
A damaged payload (also a broken compressed archive entry: zlib.error,
ResourceError) costs only its own hull(s): the reason (German) lands in
meta.coverage; only an unreadable .entities fails the map.
"""

import base64
import hashlib
import json
import math
import os
import struct
import sys
import tempfile
import threading
import zlib
from array import array
from collections import Counter
from pathlib import Path

from . import mapgeo, tnomap
from .mapcoll import KINDS, SOLID, STEP, CmError, _box, _Grid, _inside
from .resources import ResourceError
from .tnomap import off_origin, world_space

VERSION = 2
M = tnomap.M_PER_UNIT
LIMIT = 200000.0          # units (3.8 km); retail vertices reach ~150 000

SOLID_BIT, OPAQUE, SHOTCLIP, TRIGGER = 0x1, 0x2, 0x80, 0x4000
BLOCKING = 0x1F9          # SOLID + every *CLIP bit: something collides with it
VISIBLE = SOLID_BIT | OPAQUE | SHOTCLIP
TOOLCLIP_FLAGS = 0x4100
NO_CM_TYPES = ("CLIPMODEL_NONE", "CLIPMODEL_BOX", "CLIPMODEL_BOUNDINGBOX",
               "CLIPMODEL_CYLINDER", "CLIPMODEL_CONE")


# --------------------------------------------------------------------------
# cm payload (parse + checks; the writer lives in tools/probe_tno_cm.py)
# --------------------------------------------------------------------------

ARRAYS = (  # name, element format, element size, alignment
    ("nodes", "ifHHHBB", 16, 16),
    ("refs", "H", 2, 2),
    ("materials", "III4s", 16, 16),
    ("polygons", "6hBBH", 16, 16),
    ("edgerefs", "H", 2, 2),
    ("edges", "HH", 4, 4),
    ("vertices", "fff4x", 16, 16),
    ("brushes", "6hBBH", 16, 16),
    ("planes", "ffff", 16, 16),
)
BLOCK_HDR = 0x70
MAGIC_BE, MAGIC_LE = b"BCM9", b"9MCB"
F_EXTERNAL, F_WORLD = 0x1, 0x1000000
V1_HDR = 0x40


def _align(x, a):
    return (x + a - 1) // a * a


def _ceil4(x):
    return (x + 3) // 4 * 4


def parse_block(buf, off, E):
    """One BLOCK at buf[off:], endianness E ('>' / '<') -> (dict, size).
    Keeps the alignment gaps and the junk word so the probe can re-write it."""
    size, size2, *rest = struct.unpack_from(E + "2I6fI18II", buf, off)
    bbox, zero, pairs, junk = rest[:6], rest[6], rest[7:25], rest[25]
    if size != size2 or zero != 0:
        raise CmError("Block-Kopf: Größe %d/%d, Null-Feld %d" % (size, size2, zero))
    if off + size > len(buf):
        raise CmError("Block reicht über das Dateiende hinaus")
    blk = {"bbox": bbox, "junk": junk, "gaps": []}
    pos = BLOCK_HDR
    for i, (name, fmt, esz, al) in enumerate(ARRAYS):
        count, aoff = pairs[2 * i], pairs[2 * i + 1]
        want = _align(pos, al)
        if aoff != want:
            raise CmError("%s bei 0x%x, erwartet 0x%x" % (name, aoff, want))
        blk["gaps"].append(bytes(buf[off + pos:off + aoff]))
        raw = buf[off + aoff:off + aoff + count * esz]
        if len(raw) != count * esz:
            raise CmError("%s abgeschnitten" % name)
        if name == "vertices" and any(raw[k + 12:k + 16] != b"\0\0\0\0" for k in range(0, len(raw), 16)):
            raise CmError("Vertex-w ist nicht 0")
        items = list(struct.iter_unpack(E + fmt, raw))
        blk[name] = [t[0] for t in items] if len(fmt) == 1 else items
        pos = aoff + count * esz
    if pos != size:
        raise CmError("Block endet bei 0x%x, Größe 0x%x" % (pos, size))
    _split_edgerefs(blk)
    _check_block(blk)
    return blk, size


def _split_edgerefs(blk):
    """edgerefs = used part + pad of copies of the last used value.
    Pad length rule: count = max(firstEdge + ceil4(nEdges + 1))."""
    polys, er = blk["polygons"], blk["edgerefs"]
    used = 0
    for p in polys:
        if p[8] != used:
            raise CmError("Kantenlisten der Polygone nicht lückenlos")
        used += p[7]
    want = max((p[8] + _ceil4(p[7] + 1) for p in polys), default=0)
    if len(er) != want or any(v != er[used - 1] for v in er[used:]):
        raise CmError("Füllregel der Kantenverweise verletzt (%d statt %d)" % (len(er), want))
    blk["edgerefs"] = er[:used]


def _check_block(blk):
    nV, nE, nM = len(blk["vertices"]), len(blk["edges"]), len(blk["materials"])
    nP, nB, nPl, nN = len(blk["polygons"]), len(blk["brushes"]), len(blk["planes"]), len(blk["nodes"])
    refs = blk["refs"]
    if any(a >= nV or b >= nV for a, b in blk["edges"]):
        raise CmError("Kante zeigt auf fehlenden Vertex")
    if any((v & 0x3FFF) >= nE for v in blk["edgerefs"]):
        raise CmError("Kantenverweis außerhalb der Kanten")
    if any(p[6] >= nM for p in blk["polygons"]) or any(b[6] >= nM for b in blk["brushes"]):
        raise CmError("Materialindex außerhalb der Materialien")
    used = 0
    for b in blk["brushes"]:
        if b[8] != used:
            raise CmError("Ebenen der Brushes nicht lückenlos")
        used += b[7]
    if used != nPl:
        raise CmError("%d Ebenen, Brushes nutzen %d" % (nPl, used))
    used = 0
    nodes = blk["nodes"]
    for i, (axis, dist, c0, c1, first, np_, nb) in enumerate(nodes):
        if first != used:
            raise CmError("Knotenverweise nicht lückenlos")
        # the u8 polygon count wraps at 256 (1 retail node, c01p2 tbcm block
        # 1367, has 275); the span is still tiled, slots 256.. are garbage
        span = (nodes[i + 1][4] if i + 1 < len(nodes) else len(refs)) - first
        if (span - nb) & 255 != np_ or span < nb:
            raise CmError("Knotenzähler passen nicht zu den Verweisen")
        if any(refs[first + k] >= nP for k in range(min(span - nb, 256))) or \
           any(refs[first + span - nb + k] >= nB for k in range(nb)):
            raise CmError("Knotenverweis außerhalb")
        if axis == -1:
            if c0 or c1:
                raise CmError("Blattknoten mit Kindern")
        elif not (0 <= axis <= 2 and c0 < nN and c1 < nN):
            raise CmError("ungültiger Knoten")
        used += span
    if used != len(refs):
        raise CmError("%d Verweise, Knoten nutzen %d" % (len(refs), used))


def parse_bcm(buf, blocks=True):
    """.bcm -> dict. Every leaf records `off` (its block's offset in buf, None
    for an external .tbcm block); blocks=False leaves `block` None."""
    if buf[:4] != MAGIC_BE:
        raise CmError("keine BCM9-Kennung (%r)" % bytes(buf[:4]))
    ver, stamp, crc = struct.unpack_from(">3I", buf, 4)
    if ver == 1:
        return parse_v1(buf)
    if ver != 0:
        raise CmError("unbekannte cm-Version %d" % ver)
    bbox = struct.unpack_from(">6f", buf, 16)
    contents, flags, nn = struct.unpack_from(">3I", buf, 40)
    if flags not in (0, F_WORLD, F_WORLD | F_EXTERNAL):
        raise CmError("unbekannte cm-Flags 0x%x" % flags)
    o = 52
    if o + 16 * nn > len(buf):
        raise CmError("Baum reicht über das Dateiende hinaus")
    nodes = list(struct.iter_unpack(">ifii", buf[o:o + 16 * nn]))
    o += 16 * nn
    nl, = struct.unpack_from(">I", buf, o)
    o += 4
    leaves = []
    for _ in range(nl):
        size, hl, *lb = struct.unpack_from(">2I6f", buf, o)
        if hl != 32:
            raise CmError("Blattkopf %d statt 32 Byte" % hl)
        o += 32
        blk, off = None, None
        if not flags & F_EXTERNAL:
            off = o
            if o + size > len(buf):
                raise CmError("Blatt reicht über das Dateiende hinaus")
            if blocks:
                blk, got = parse_block(buf, o, ">")
                if got != size:
                    raise CmError("Blattgröße passt nicht zum Block")
            o += size
        leaves.append({"size": size, "bbox": tuple(lb), "block": blk, "off": off})
    _check_tree(nodes, nl)
    if buf[o:o + 4] != MAGIC_BE:
        raise CmError("kein BCM9 nach den Blättern")
    o += 4
    areas = None
    if flags & F_EXTERNAL:
        areas, o = parse_areas(buf, o)
        if buf[o:o + 4] != MAGIC_BE:
            raise CmError("kein BCM9 nach den Stream-Bereichen")
        o += 4
    if o != len(buf):
        raise CmError("%d überzählige Byte am Ende" % (len(buf) - o))
    cm = {"version": 0, "stamp": stamp, "crc": crc, "bbox": bbox, "contents": contents,
          "flags": flags, "nodes": nodes, "leaves": leaves, "areas": areas}
    if blocks and not flags & F_EXTERNAL:
        got = union_contents(lf["block"] for lf in leaves)
        if got != contents:
            raise CmError("contents 0x%x != ODER der Materialien 0x%x" % (contents, got))
    return cm


def union_contents(blocks):
    c = 0
    for b in blocks:
        for m in b["materials"]:
            c |= m[0]
    return c


def _check_tree(nodes, nl):
    """Top-level kd tree: child >= 0 node, < 0 leaf -(c+1); every leaf once."""
    seen = Counter()
    for axis, dist, c0, c1 in nodes:
        if not 0 <= axis <= 2:
            raise CmError("Baumachse %d" % axis)
        for c in (c0, c1):
            if c >= 0:
                if c >= len(nodes):
                    raise CmError("Baumkind außerhalb")
            else:
                seen[-c - 1] += 1
    if seen != (Counter(range(nl)) if nodes else Counter()):
        raise CmError("Baum verweist nicht genau einmal auf jedes Blatt")


def parse_areas(buf, o):
    """Collision stream areas (entities misc_collision_stream_area_*)."""
    outer, size, n, nrefs, nname = struct.unpack_from(">5I", buf, o)
    if outer != size:
        raise CmError("Größe der Stream-Bereiche")
    base = o + 4
    p = base + 16
    areas = []
    for _ in range(n):
        f = struct.unpack_from(">I12f2I", buf, p)
        areas.append({"name_off": f[0], "origin": f[1:4], "axis": f[4:13], "count": f[13], "first": f[14]})
        p += 60
    refs = struct.unpack_from(">%dH" % nrefs, buf, p)
    p += 2 * nrefs
    names = bytes(buf[p:p + nname])
    p += nname
    if p - base != size:
        raise CmError("Länge der Stream-Bereiche %d != %d" % (p - base, size))
    first = 0
    for a in areas:
        if a["first"] != first:
            raise CmError("Verweise der Stream-Bereiche nicht lückenlos")
        first += a["count"]
        a["leaves"] = refs[a["first"]:a["first"] + a["count"]]
        end = names.index(b"\0", a["name_off"])
        a["name"] = names[a["name_off"]:end].decode("ascii")
    if first != nrefs:
        raise CmError("Verweise der Stream-Bereiche")
    return {"areas": areas, "names": names}, p


def tbcm_blocks(buf, world):
    """[(offset, size)] of every leaf block of a .tbcm; checks head and tail
    against its world.bcm."""
    if buf[:4] != MAGIC_LE or buf[-4:] != MAGIC_LE:
        raise CmError("keine 9MCB-Kennung in der .tbcm")
    zero, crc, stamp = struct.unpack_from("<3I", buf, 4)
    if zero != 0 or (stamp, crc) != (world["stamp"], world["crc"]):
        raise CmError(".tbcm gehört nicht zu dieser world.bcm")
    out, o = [], 16
    for lf in world["leaves"]:
        out.append((o, lf["size"]))
        o += lf["size"]
    if o + 4 != len(buf):
        raise CmError(".tbcm-Länge %d != %d" % (len(buf), o + 4))
    return out


def _pad_last(seq, n):
    return list(seq) + [seq[-1]] * (n - len(seq)) if seq else []


def parse_v1(buf):
    """md6 collision: spheres bound to joints, packed at odd offsets.
      0x00 'BCM9' u32 1 u32 h0 u32 h1 f32 bbox[6] u32 contents u8[3] 0
      0x2f u32 size   0x33 block:
        u32 size u32 <uninitialised> f32 bbox[6] u32 contents u16 nJoints u16 nSpheres
        u16 off[6] u8[12] 0     (offsets relative to the block)
        u8 joint[n4] | f32 x[n4] | f32 y[n4] | f32 z[n4] | f32 radius[n4] | u8 surfType[n4]
        n4 = ceil4(nSpheres), padding = copies of the last sphere's value
      'BCM9'"""
    ver, h0, h1 = struct.unpack_from(">3I", buf, 4)
    bbox = struct.unpack_from(">6f", buf, 16)
    contents, = struct.unpack_from(">I", buf, 40)
    if buf[44:47] != b"\0\0\0":
        raise CmError("v1-Kopfbytes")
    lsize, size, junk = struct.unpack_from(">3I", buf, 47)
    b = 51
    bbox2 = struct.unpack_from(">6f", buf, b + 8)
    cont2, nj, ns = struct.unpack_from(">IHH", buf, b + 32)
    offs = struct.unpack_from(">6H", buf, b + 40)
    if lsize != size or buf[b + 52:b + 64] != bytes(12):
        raise CmError("v1-Blockkopf")
    n4 = _ceil4(ns)
    want = [V1_HDR, _align(V1_HDR + n4, 16)]
    for _ in range(4):
        want.append(want[-1] + 4 * n4)
    if list(offs) != want[:6] or size != want[5] + n4:
        raise CmError("v1-Layout")
    cols = [list(buf[b + V1_HDR:b + V1_HDR + n4])]
    cols += [list(struct.unpack_from(">%df" % n4, buf, b + offs[k])) for k in range(1, 5)]
    cols.append(list(buf[b + offs[5]:b + offs[5] + n4]))
    for c in cols:
        if c != _pad_last(c[:ns], n4):
            raise CmError("v1-Füllregel")
    if any(j >= nj for j in cols[0]):
        raise CmError("v1-Gelenkindex")
    if buf[b + size:] != MAGIC_BE:
        raise CmError("v1-Ende")
    return {"version": 1, "h": (h0, h1), "bbox": bbox, "contents": contents, "junk": junk,
            "bbox2": bbox2, "contents2": cont2, "joints": nj,
            "spheres": [tuple(c[i] for c in cols) for i in range(ns)],
            "gap": bytes(buf[b + V1_HDR + n4:b + offs[1]])}


_BAD = (struct.error, IndexError, ValueError, KeyError, TypeError, OverflowError,
        MemoryError, UnicodeError, RecursionError, zlib.error, ResourceError)


def _guard(fn, *args):
    """fn(*args); anything a damaged payload can raise becomes a readable CmError."""
    try:
        return fn(*args)
    except CmError:
        raise
    except _BAD as ex:
        raise CmError("cm beschädigt (%s: %s)" % (type(ex).__name__, ex)) from None


# --------------------------------------------------------------------------
# block -> triangles per kind
# --------------------------------------------------------------------------

def world_kind(contents, flags):
    if not contents & BLOCKING:
        return "volume"
    if not contents & VISIBLE or flags & TOOLCLIP_FLAGS:
        return "clip"
    return "world"


def block_tris(blk, kind_of=None):
    """{kind or None: flat triangle list (vertex indices of the block)}.
    kind_of(contents, flags) splits by material (world), None keeps one list.
    Raises CmError when an edge loop does not close (damage)."""
    V, E, ER, mats = blk["vertices"], blk["edges"], blk["edgerefs"], blk["materials"]
    if any(not all(-LIMIT < c < LIMIT for c in v) for v in V):
        raise CmError("Vertex nicht endlich oder weiter als %d Einheiten weg" % LIMIT)
    kinds = [kind_of(m[0], m[1]) if kind_of else None for m in mats]
    out = {}
    for p in blk["polygons"]:
        n, first = p[7], p[8]
        if n < 3:
            continue
        ring, prev_end = [], None
        for r in ER[first:first + n]:
            a, b = E[r & 0x3FFF]
            if r & 0x8000:
                a, b = b, a
            if prev_end is not None and a != prev_end:
                raise CmError("Kantenschleife eines Polygons schließt nicht")
            ring.append(a)
            prev_end = b
        if prev_end != ring[0]:
            raise CmError("Kantenschleife eines Polygons schließt nicht")
        t = out.setdefault(kinds[p[6]], array("I"))
        for k in range(1, n - 1):
            t.extend((ring[0], ring[k], ring[k + 1]))
    return out


# --------------------------------------------------------------------------
# entities -> cm
# --------------------------------------------------------------------------

def entity_kind(inherit, cls, contents):
    """mapcoll's name rules, then the cm's own contents: nothing blocking ->
    volume (stream areas, aas obstacles, water), only clip bits -> clip."""
    s = (inherit + " " + cls).lower()
    if "trigger" in s:
        return "trigger"
    if "volume" in s or not contents & BLOCKING:
        return "volume"
    if not contents & VISIBLE:
        return "clip"
    if cls in tnomap.STATIC_CLASSES or inherit.startswith("func/static"):
        return "static"
    return "dynamic"


def entity_cm(st, chunks, combo, name, edit):
    """(cm entry name, how) or (None, German reason)."""
    cmi = mapgeo._d(edit.get("clipModelInfo"))
    typ = str(cmi.get("type", ""))
    if typ in NO_CM_TYPES:
        return None, "%s (prozedurale Form, kein cm)" % typ if typ != "CLIPMODEL_NONE" \
            else "CLIPMODEL_NONE (keine Kollision)"
    brk = "%smegabreakable_%s_base" % (combo, name.lower())
    if st.find(chunks, "cm", brk):
        return brk, "megabreakable"
    if any(float(mapgeo._d(cmi.get("offset")).get(c, 0) or 0) for c in "xyz"):
        return None, "clipModelInfo.offset gesetzt (Bedeutung ungeprüft)"
    clip = cmi.get("clipModelName")
    how = "clipModelName"
    if clip is None:
        clip, how = mapgeo._d(edit.get("renderModelInfo")).get("model"), "Rendermodell"
    if not isinstance(clip, str) or not clip or clip == "NULL":
        return None, "kein Clipmodell"
    clip = clip.lower().replace("\\", "/")
    if not st.find(chunks, "cm", clip):
        return None, "%s ohne cm-Eintrag (.%s)" % (how, clip.rsplit("/", 1)[-1].rpartition(".")[2] or "-")
    return clip, how


def _xform(m, verts):
    """Flat local xyz (units) -> flat world xyz (metres) under tnomap's matrix
    (translation already in metres)."""
    out = array("d", bytes(8 * len(verts)))
    x, y, z = verts[0::3], verts[1::3], verts[2::3]
    for j in range(3):
        a, b, c, t = m[4 * j] * M, m[4 * j + 1] * M, m[4 * j + 2] * M, m[4 * j + 3]
        out[j::3] = array("d", (a * p + b * q + c * r + t for p, q, r in zip(x, y, z)))
    return out


def _det3(m):
    return (m[0] * (m[5] * m[10] - m[6] * m[9]) - m[1] * (m[4] * m[10] - m[6] * m[8])
            + m[2] * (m[4] * m[9] - m[5] * m[8]))


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

STEPS = [0.005 * 2 ** k for k in range(12)]


def grid(boxes):
    """(step, origin) for mapcoll's grid encoding. 8-bit hulls carry their own
    base, so only hulls wider than 254 steps (stored as absolute 16-bit grid
    values) must fit into 65 535 steps from the origin: the finest step of
    STEPS that allows it (19 of 25 maps get 0.005..0.04 m, see the module
    docstring for the 0.08 m ones)."""
    boxes = [b for b in boxes if b]
    if not boxes:
        return STEP, [0.0, 0.0, 0.0]
    for step in STEPS:
        wide = [b for b in boxes if max(b[1][j] - b[0][j] for j in range(3)) / step > 254]
        if not wide:                    # every hull is 8-bit: any origin works
            return step, [math.floor(min(b[0][j] for b in boxes) / step) * step for j in range(3)]
        lo = [min(b[0][j] for b in wide) for j in range(3)]
        hi = [max(b[1][j] for b in wide) for j in range(3)]
        if max(hi[j] - lo[j] for j in range(3)) / step <= 65000:
            return step, [math.floor(lo[j] / step) * step for j in range(3)]
    raise CmError("Karte zu groß für das 16-Bit-Raster")


def _world_blocks(st, chunks, combo):
    """(world header, [(leaf index, buf, offset, endianness)]) or raises CmError."""
    wname = combo + "world"
    ai = next((a for a in chunks if st.find([a], "cm", wname)), None)
    if ai is None:
        raise CmError("kein world-cm im Container")
    files = {e.path.rsplit(".", 1)[-1].lower(): e for e in st.archives[ai].entries
             if e.type == "cm" and e.name.lower() == wname}
    if "bcm" not in files:
        raise CmError("world.bcm fehlt")
    bcm = _guard(st.archives[ai].read, files["bcm"])
    w = _guard(parse_bcm, bcm, False)
    if w["version"] != 0 or not w["flags"] & F_WORLD:
        raise CmError("world.bcm ist kein Welt-cm (Flags 0x%x)" % w.get("flags", 0))
    if not w["flags"] & F_EXTERNAL:
        return w, [(i, bcm, lf["off"], ">") for i, lf in enumerate(w["leaves"])]
    if "tbcm" not in files:
        raise CmError("world.tbcm fehlt")
    buf = _guard(st.archives[ai].read, files["tbcm"])
    return w, [(i, buf, o, "<") for i, (o, _) in enumerate(_guard(tbcm_blocks, buf, w))]


def build(st, chunks, map_id, ents):
    """collision.json document of `map_id` (entities = tnomap/mapgeo order)."""
    combo = "maps/%s/_combo/" % map_id
    hulls = []                  # (kind, ent, flat world xyz metres, flat tris)
    fails, st_ = Counter(), Counter()
    raw_tris = Counter()

    # world
    world_info = {"leaves": 0, "blocks_ok": 0, "blocks_failed": 0, "polygons_per_kind": Counter()}
    try:
        w, blocks = _world_blocks(st, chunks, combo)
        world_info["leaves"] = len(blocks)
        for i, buf, off, E in blocks:
            try:
                blk, _ = _guard(parse_block, buf, off, E)
                parts = _guard(block_tris, blk, world_kind)
            except CmError as ex:
                world_info["blocks_failed"] += 1
                fails["world-Blatt: %s" % ex] += 1
                continue
            world_info["blocks_ok"] += 1
            kinds = [world_kind(m[0], m[1]) for m in blk["materials"]]
            world_info["polygons_per_kind"].update(kinds[p[6]] for p in blk["polygons"] if p[7] >= 3)
            flat = array("d", (c * M for v in blk["vertices"] for c in v))
            for kind, t in parts.items():
                hulls.append((kind, -1, flat, t))
                raw_tris[kind] += len(t) // 3
        st_["placed: world.bcm (identity)"] += 1
    except CmError as ex:
        st_["skip: world-cm (%s)" % ex] += 1

    # entities
    local, positions, explicit = {}, [], []    # cm name -> [(flat local xyz units, tris)] or str
    for ei, (name, d) in enumerate(ents):
        edit = mapgeo._d(d.get("edit"))
        try:
            m = mapgeo.affine(edit)
        except (TypeError, ValueError):
            st_["skip: Platzierung nicht lesbar"] += 1
            continue
        m[3], m[7], m[11] = m[3] * M, m[7] * M, m[11] * M
        positions.append((m[3], m[7], m[11]))
        if "spawnPosition" in edit:
            explicit.append((m[3], m[7], m[11]))
        cm, how = entity_cm(st, chunks, combo, name, edit)
        if cm is None:
            st_["skip: " + how] += 1
            continue
        st_["cm match " + how] += 1
        if cm not in local:
            try:
                c = _guard(lambda: parse_bcm(st.read(st.find(chunks, "cm", cm))))
                if c["version"] == 1:
                    local[cm] = "md6-Kollision: Kugeln an Skelettgelenken (Skelett nicht dekodiert)"
                else:
                    local[cm] = (c["contents"], [x * M for x in c["bbox"]], [
                        (array("d", (x for v in lf["block"]["vertices"] for x in v)),
                         _guard(block_tris, lf["block"]).get(None, array("I")))
                        for lf in c["leaves"]])
            except CmError as ex:
                local[cm] = "cm beschädigt: %s" % ex
                fails[local[cm]] += 1
        got = local[cm]
        if isinstance(got, str):
            st_["skip: " + got.split(":")[0]] += 1
            continue
        contents, bb, parts = got
        if not all(-LIMIT * M < c < LIMIT * M for c in m[3::4]):
            st_["skip: Platzierung außerhalb"] += 1
            continue
        if world_space(bb[:3], bb[3:], m[3::4]):
            m = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0]
            st_["world_space"] += 1
        elif d.get("class") == "idBinaryModel" and any(m[3::4]) and off_origin(bb[:3], bb[3:]):
            st_["skip: idBinaryModel-cm in Weltkoordinaten an anderer Stelle"] += 1
            continue
        kind = entity_kind(str(d.get("inherit", "")), str(d.get("class", "")), contents)
        flip = _det3(m) < 0
        for v, t in parts:
            if flip:                            # mirrored: (a, b, c) -> (a, c, b)
                t = array("I", (x for a, b, c in zip(t[0::3], t[1::3], t[2::3]) for x in (a, c, b)))
            hulls.append((kind, ei, _xform(m, v), t))
            raw_tris[kind] += len(t) // 3
        st_["placed: entity"] += 1

    # bounds (pass 1) -> grid origin/step; then grid encoding (pass 2)
    inf = float("inf")
    lo, hi, slo, shi = [inf] * 3, [-inf] * 3, [inf] * 3, [-inf] * 3
    boxes = []
    for kind, _, v, t in hulls:
        if not t:
            boxes.append(None)
            continue
        b = [min(v[j::3]) for j in range(3)], [max(v[j::3]) for j in range(3)]
        boxes.append(b)
        for j in range(3):
            lo[j], hi[j] = min(lo[j], b[0][j]), max(hi[j], b[1][j])
            if kind in SOLID:
                slo[j], shi[j] = min(slo[j], b[0][j]), max(shi[j], b[1][j])
    step, origin = grid(boxes)
    g = _Grid(origin, step)
    for (kind, ent, v, t), b in zip(hulls, boxes):
        if b is None:
            continue
        try:
            g.add(kind, ent, list(zip(v[0::3], v[1::3], v[2::3])), list(zip(t[0::3], t[1::3], t[2::3])))
        except (CmError, OverflowError) as ex:     # a hull the 16-bit grid cannot hold
            fails["Hülle nicht kodierbar: %s" % ex] += 1
    if sys.byteorder != "little":
        g.p16.byteswap()
        g.i16.byteswap()

    decoded = sum(1 for x in local.values() if not isinstance(x, str))
    placed = st_["placed: world.bcm (identity)"] + st_["placed: entity"]
    meta = {
        "map": map_id,
        "game": "tno",
        "version": VERSION,
        "source": {
            "entities": "maps/%s.entities" % map_id,
            "world_cm": combo + "world (.bcm/.tbcm)",
            "archives": [str(st.archives[a].resources_path) for a in chunks],
        },
        "coverage": {
            "world": {"leaves": world_info["leaves"], "blocks_decoded": world_info["blocks_ok"],
                      "blocks_failed": world_info["blocks_failed"],
                      "polygons_per_kind": dict(world_info["polygons_per_kind"])},
            "cm_payloads": {"distinct": len(local), "decoded": decoded,
                            "md6_not_placed": sum(1 for x in local.values() if isinstance(x, str)
                                                  and x.startswith("md6")),
                            "failed": sum(1 for x in local.values() if isinstance(x, str)
                                          and x.startswith("cm besch"))},
            "cm_decode_failures": dict(fails),
            "instances_placed": placed,
            "instances_resolved": st_["placed: world.bcm (identity)"]
            + sum(v for k, v in st_.items() if k.startswith("cm match ")),
            "cm_world_space_identity": st_["world_space"],
            "cm_match": {k[9:]: v for k, v in st_.items() if k.startswith("cm match ")},
            "skipped_entities": {k[6:]: v for k, v in sorted(st_.items()) if k.startswith("skip: ")},
        },
        "counts": {"hulls": len(g.rec), "vertices": g.nv, "triangles": g.nt,
                   "triangles_per_kind": dict(g.per_kind),
                   "triangles_before_welding_per_kind": dict(raw_tris),
                   "entities_in_file": len(ents)},
        "units": "m",
        "unit_scale": {"meters_per_unit": M, "source": "tnomap.M_PER_UNIT (EXE DOOR_HEIGHT_INCHES 96 / "
                                                       "DOOR_HEIGHT_UNITS 128)"},
        "coords": "id Tech: right-handed, +Z up. three.js (Y up): (x, y, z) -> (x, z, -y)",
        "space": "world = tnomap/mapgeo.affine(edit) (scale included, translation in m) applied to"
                 " the cm in the entity frame; winding flipped when det < 0; world.bcm and cm"
                 " authored in world coordinates (tnomap.world_space) are placed with the identity",
        "bbox_all": _box(lo, hi),
        "bbox_solid": dict(_box(slo, shi), kinds=list(SOLID)) if slo[0] <= shi[0] else None,
        "sanity": {
            "entity_positions_in_bbox_all": [_inside(positions, lo, hi), len(positions)],
            "entity_positions_in_bbox_solid": [_inside(positions, slo, shi), len(positions)],
            "explicit_spawnPositions_in_bbox_solid": [_inside(explicit, slo, shi), len(explicit)],
        },
        "kinds": {"world": "world.bcm polygons with SOLID/OPAQUE/SHOTCLIP (render-mesh collision)",
                  "static": "entity of class idStaticEntity/idBinaryModel or inherit func/static*",
                  "dynamic": "any other entity with a solid cm",
                  "clip": "world.bcm polygons or entity cm that block without SOLID/OPAQUE/SHOTCLIP"
                          " (player/monster/ik clip) or carry material flag 0x100/0x4000",
                  "trigger": "'trigger' in inherit/class (not solid)",
                  "volume": "'volume' in inherit/class, or contents without any blocking bit"
                            " (stream areas, aas obstacles, water)"},
        "encoding": {
            "grid_origin": origin, "step": step,
            "hull": "[kind, ent, nv, nt, bits, bx, by, bz]; kind indexes `kinds`; ent = 0-based"
                    " entity {} index in file order (same order as scene.json entities),"
                    " -1 for world.bcm (one hull per kd leaf and kind)",
            "positions": "walk hulls in order. bits 8: nv*3 bytes from p8, grid = (bx,by,bz) +"
                         " byte; bits 16: nv*3 Uint16LE from p16, grid = value."
                         " world = grid_origin + grid*step",
            "indices": "nt*3 per hull, local to the hull's own vertices: Uint8 from i8 when"
                       " nv <= 256, else Uint16LE from i16. Polygons fan-triangulated in edge"
                       " loop order; render double-sided",
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


def cache_key(game_root, map_id):
    h = hashlib.sha1(("tnomapcoll %d %s\n" % (VERSION, map_id)).encode("utf-8"))
    for p in (p for pair in tnomap._pairs(game_root) for p in pair):
        s = p.stat()
        h.update(("%s|%d|%d\n" % (p, s.st_size, s.st_mtime_ns)).encode("utf-8"))
    return h.hexdigest()[:16]


def collision(game_root, map_id, cache=True):
    """Path of the map's collision.json, built on first use or when the game
    changed. Unknown id -> KeyError; unusable .entities -> CmError."""
    out = tnomap.cache_dir(map_id) / "collision.json"
    stamp = out.with_name("collision.key")
    with _LOCK:
        key = cache_key(game_root, map_id)
        try:
            if cache and out.exists() and stamp.read_text(encoding="ascii") == key:
                return out
        except OSError:
            pass
        with tnomap._Store(game_root) as st:
            hit = st.entities().get(map_id)
            if hit is None:
                raise KeyError("Unknown map: %s" % map_id)
            try:
                ents = mapgeo.parse_entities(st.read(hit).decode("utf-8", "surrogateescape"))
            except (mapgeo.MapGeoError, RecursionError) as ex:
                raise CmError("%s: .entities nicht lesbar (%s)" % (map_id, ex)) from None
            doc = build(st, (hit[0], 0), map_id, ents)
        doc["meta"]["cache_key"] = key
        out.parent.mkdir(parents=True, exist_ok=True)
        stamp.unlink(missing_ok=True)
        fd, tmp = tempfile.mkstemp(dir=out.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, separators=(",", ":"), ensure_ascii=False)
            os.replace(tmp, out)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        stamp.write_text(key, encoding="ascii")
    return out
