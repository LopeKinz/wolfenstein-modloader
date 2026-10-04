"""TNC md6mesh (`baseModel`): skinned models (weapons, figures, vehicles) in bind pose.

    md = parse(payload)              dict; MapGeoError unless the payload ends on its last byte
    surface_arrays(mesh, lod=0)      (pos, nrm, uv, idx) of one mesh LOD, or None when absent
    fix_normals(pos, nrm, idx)       zero the stored normals the triangles contradict
    pack([(pos, nrm, uv, idx), ...]) WMD1 bytes, the Studio's models/mesh.bin
    python -m wolfsdk.md6 [TNC]      self-check; with an install also every baseModel in it

The layout is re_probes/assetviewer3d/probe_md6mesh.py's (measured on all 458
baseModel entries of TNC, controls there): little endian, no padding,
str = u32 length + bytes, nothing compressed inside the payload.

  str skeleton ; f32[6] box0 ; u8 flag ; u16 N ; u16[N] joint remap
  f32[6] bounds (== union of the LOD 0 bounds) ; f32[3] ; u32 num_meshes
  mesh: str name ; str material ; u8 1 ; u32 kind (1 skinned, 2 hair cards,
        0 no tangents) ; u32 joint_first ; u32 joint_count ; u32 hash
        3 x LOD slot: u32 absent ; if 0: u32 nv ; u32 nt ; f32[6] bounds ;
                      nv x 48-byte vertex ; u16[3 nt] (clockwise) ; f32[2] uv density
        morphs: u32 n ; n x {str ; u32} ; u32 n ; n x 8 B ; u32 n ; n x 16 B
  u32 n ; n x {str material ; u32 mesh ; u32 lo ; u32 hi}
  u32 n ; n x {str joint ; u32 4 ; f32[17]}

Vertex (48 B): 0x00 f32[3] position (model space, bind pose, metres, z up),
0x14 u8[4] normal (c-128)/127 (80 80 80 = none), 0x18 u8[4] tangent,
0x1C u8[4] joint slots, 0x20 f32[2] uv to render (0x0C holds a copy in kind
0/1 and hair data in kind 2, so 0x0C is not the uv).

WMD1 (little endian, every part 4-byte aligned): b"WMD1", u32 surface count,
per surface u32 vertex count, u32 index count, f32 position[3 nv],
f32 normal[3 nv] (0 where unknown), f32 uv[2 nv], u32 index[ni]. Indices are
counter-clockwise (three.js front faces): the stored clockwise order is
flipped here, as mapgeo.model_surfaces does for static models.

Stored normals are not always the geometry's: in some models whole joints'
normals are turned by 90 or 180 degrees (kampfdrohne_gun, cutting_torch_01,
wheelchair01_fps, chair_terminal_sub, ...), ~3 % of all LOD 0 vertices of
kind 1 are more than 60 degrees off their triangles, and hard edges whose
shared vertices carry one face's normal leave the other face lit sideways.
fix_normals() zeroes those (0 = unknown: the viewer computes them from the
triangles). Hair cards
(kind 2) are left alone: their normals are bent on purpose (63 % of
grace_hair_col's would go).
"""

import math
import struct
import sys
from array import array

from .mapgeo import MapGeoError, _Reader

STRIDE = 48
_NRM = [(c - 128) / 127 for c in range(256)]


def _f(r, n):
    return list(struct.unpack("<%df" % n, r.take(4 * n)))


def parse(data):
    """md6mesh payload -> {skeleton, joints, bounds, meshes: [{name, material,
    kind, lods: [None | {nv, nt, bounds, verts, idx}]}], mats, zones}."""
    r = _Reader(data)
    md = {"skeleton": r.str(), "box0": _f(r, 6), "flag": r.take(1)[0]}
    n = struct.unpack("<H", r.take(2))[0]
    md["joints"] = struct.unpack("<%dH" % n, r.take(2 * n))
    md["bounds"], md["unk"] = _f(r, 6), _f(r, 3)
    if not all(map(math.isfinite, md["bounds"])):
        raise MapGeoError("md6mesh: header bounds not finite")
    md["meshes"] = []
    for _ in range(r.u32()):
        m = {"name": r.str(), "material": r.str()}
        r.take(1)
        m["kind"], m["joint_first"], m["joint_count"], m["hash"] = struct.unpack("<4I", r.take(16))
        m["lods"] = []
        for _ in range(3):
            if r.u32():
                m["lods"].append(None)
                continue
            nv, nt = r.u32(), r.u32()
            m["lods"].append({"nv": nv, "nt": nt, "bounds": _f(r, 6), "verts": r.take(STRIDE * nv),
                              "idx": r.take(6 * nt), "density": _f(r, 2)})
        m["morphs"] = [(r.str(), r.u32()) for _ in range(r.u32())]
        r.take(8 * r.u32())
        r.take(16 * r.u32())
        md["meshes"].append(m)
    md["mats"] = [(r.str(), r.u32(), r.u32(), r.u32()) for _ in range(r.u32())]
    md["zones"] = [(r.str(), r.u32(), _f(r, 17)) for _ in range(r.u32())]
    if r.o != len(data):
        raise MapGeoError("md6mesh: %d bytes after the expected end" % (len(data) - r.o))
    return md


def surface_arrays(mesh, lod=0):
    """(pos f32[3nv], nrm f32[3nv], uv f32[2nv], idx u32[3nt] counter-clockwise)
    of LOD `lod`, None when the slot is absent. MapGeoError on an index past
    the vertices or a position/uv that is not finite."""
    lo = mesh["lods"][lod]
    if lo is None:
        return None
    nv, v = lo["nv"], lo["verts"]
    f = array("f", v)
    pos, nrm, uv = array("f", bytes(12 * nv)), array("f", bytes(12 * nv)), array("f", bytes(8 * nv))
    pos[0::3], pos[1::3], pos[2::3] = f[0::12], f[1::12], f[2::12]
    uv[0::2], uv[1::2] = f[8::12], f[9::12]
    for k in range(3):
        nrm[k::3] = array("f", map(_NRM.__getitem__, v[0x14 + k::STRIDE]))
    idx = array("I", array("H", lo["idx"]))
    if idx and max(idx) >= nv:
        raise MapGeoError("md6mesh %s: index %d >= %d vertices" % (mesh["name"], max(idx), nv))
    if not (all(map(math.isfinite, pos)) and all(map(math.isfinite, uv))):
        raise MapGeoError("md6mesh %s: position or UV not finite" % mesh["name"])
    idx[1::3], idx[2::3] = idx[2::3], idx[1::3]   # clockwise -> counter-clockwise
    return pos, nrm, uv, idx


def fix_normals(pos, nrm, idx, min_cos=0.5):
    """Zero (in place) the stored normals the triangles contradict: a vertex's
    normal more than 60 degrees (cos < min_cos) off the area-weighted normal
    of its counter-clockwise triangles, then the three of every triangle
    that still faces against the sum of its vertices' normals (a hard edge
    whose shared vertices carry the other face's normal). A vertex without
    a triangle normal keeps its own. -> count zeroed."""
    nv = len(pos) // 3
    acc, faces = [0.0] * (3 * nv), []
    p, ix = pos.tolist(), idx.tolist()
    for t in range(0, len(ix), 3):
        a, b, c = 3 * ix[t], 3 * ix[t + 1], 3 * ix[t + 2]
        ax, ay, az = p[a], p[a + 1], p[a + 2]
        ux, uy, uz = p[b] - ax, p[b + 1] - ay, p[b + 2] - az
        vx, vy, vz = p[c] - ax, p[c + 1] - ay, p[c + 2] - az
        x, y, z = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        faces.append((a, b, c, x, y, z))
        for v in (a, b, c):
            acc[v] += x
            acc[v + 1] += y
            acc[v + 2] += z
    n, out = nrm.tolist(), 0
    for v in range(0, 3 * nv, 3):
        cx, cy, cz, nx, ny, nz = acc[v], acc[v + 1], acc[v + 2], n[v], n[v + 1], n[v + 2]
        l2 = (cx * cx + cy * cy + cz * cz) * (nx * nx + ny * ny + nz * nz)
        if l2 > 0 and cx * nx + cy * ny + cz * nz < min_cos * math.sqrt(l2):
            n[v] = n[v + 1] = n[v + 2] = 0.0
            out += 1
    for a, b, c, x, y, z in faces:
        sx, sy, sz = n[a] + n[b] + n[c], n[a + 1] + n[b + 1] + n[c + 1], n[a + 2] + n[b + 2] + n[c + 2]
        if (x or y or z) and (sx or sy or sz) and x * sx + y * sy + z * sz <= 0:
            for v in (a, b, c):
                out += bool(n[v] or n[v + 1] or n[v + 2])
                n[v] = n[v + 1] = n[v + 2] = 0.0
    nrm[:] = array("f", n)
    return out


def pack(surfaces):
    """WMD1 bytes of [(pos, nrm, uv, idx)] (arrays 'f', 'f', 'f', 'I')."""
    if sys.byteorder != "little":
        raise MapGeoError("WMD1 wird nur auf Little-Endian-Rechnern geschrieben")
    out = [b"WMD1", struct.pack("<I", len(surfaces))]
    for pos, nrm, uv, idx in surfaces:
        out += [struct.pack("<II", len(pos) // 3, len(idx)), pos.tobytes(), nrm.tobytes(),
                uv.tobytes(), idx.tobytes()]
    return b"".join(out)


# -- self-check ------------------------------------------------------------------

def _s(text):
    b = text.encode("latin1")
    return struct.pack("<I", len(b)) + b


def _sample(nv=3, idx=(0, 1, 2)):
    """A one-mesh payload: LOD 0 with nv vertices, LOD 1/2 absent, 1 morph."""
    verts = b""
    for i in range(nv):
        verts += struct.pack("<3f2f", i, 2 * i, 3 * i, 9, 9)          # pos, 0x0C decoy
        verts += bytes((255, 128, 1, 0)) + bytes((128,) * 4) + bytes(4)   # normal, tangent, joints
        verts += struct.pack("<2f", 0.25 * i, 1 - 0.25 * i) + bytes(8)   # uv at 0x20
    lod = (struct.pack("<3I", 0, nv, len(idx) // 3) + struct.pack("<6f", 0, 0, 0, nv - 1, 2 * nv - 2, 3 * nv - 3)
           + verts + struct.pack("<%dH" % len(idx), *idx) + struct.pack("<2f", 1e30, 1e30))
    mesh = (_s("m0") + _s("mat/a") + b"\1" + struct.pack("<4I", 1, 0, 1, 7) + lod + struct.pack("<3I", 1, 1, 1)
            + _s("smile") + struct.pack("<I", 0) + struct.pack("<I", 1) + bytes(8) + struct.pack("<I", 1) + bytes(16))
    return (_s("a.md6skl") + struct.pack("<6f", *[0.0] * 6) + b"\0" + struct.pack("<H4H", 4, 0, 1, 2, 3)
            + struct.pack("<6f", 0, 0, 0, 2, 4, 6) + struct.pack("<3f", 0, 0, 0) + struct.pack("<I", 1) + mesh
            + struct.pack("<I", 1) + _s("mat/a") + struct.pack("<3I", 0, 0, 2)
            + struct.pack("<I", 1) + _s("head") + struct.pack("<I", 4) + struct.pack("<17f", *[0.0] * 17))


def _fails(fn, *args):
    try:
        fn(*args)
    except MapGeoError:
        return True
    return False


def selftest():
    data = _sample()
    md = parse(data)
    m = md["meshes"][0]
    assert md["skeleton"] == "a.md6skl" and md["joints"] == (0, 1, 2, 3) and len(md["meshes"]) == 1
    assert m["name"] == "m0" and m["material"] == "mat/a" and m["lods"][1:] == [None, None]
    assert m["morphs"] == [("smile", 0)] and md["mats"] == [("mat/a", 0, 0, 2)] and md["zones"][0][0] == "head"
    pos, nrm, uv, idx = surface_arrays(m)
    assert list(pos) == [0, 0, 0, 1, 2, 3, 2, 4, 6], list(pos)
    assert list(uv) == [0, 1, 0.25, 0.75, 0.5, 0.5], "uv must come from 0x20, not 0x0C"
    assert list(nrm[:3]) == [1.0, 0.0, -1.0], list(nrm[:3])
    assert list(idx) == [0, 2, 1], "clockwise must turn counter-clockwise"
    assert surface_arrays(m, 1) is None
    wmd = pack([(pos, nrm, uv, idx)])
    assert wmd[:4] == b"WMD1" and struct.unpack_from("<3I", wmd, 4) == (1, 3, 3)
    assert len(wmd) == 16 + 4 * (9 + 9 + 6 + 3) and len(wmd) % 4 == 0
    assert struct.unpack_from("<3f", wmd, 16 + 12) == (1, 2, 3)
    # controls: every damage must raise MapGeoError, never anything else
    assert _fails(parse, data[:-1]), "short payload parsed"
    assert _fails(parse, data + b"\0"), "trailing byte parsed"
    assert _fails(parse, b""), "empty payload parsed"
    huge = bytearray(data)
    at = data.index(b"mat/a") + 5 + 1 + 16 + 4          # nv of LOD 0
    huge[at:at + 4] = struct.pack("<I", 0xFFFFFFFF)
    assert _fails(parse, bytes(huge)), "nv 2^32-1 parsed"
    assert _fails(surface_arrays, parse(_sample(idx=(0, 1, 3)))["meshes"][0]), "index >= nv accepted"
    nan = bytearray(data)
    at = data.index(struct.pack("<6f", 0, 0, 0, 2, 4, 6))
    nan[at + 12:at + 16] = struct.pack("<f", float("nan"))
    assert _fails(parse, bytes(nan)), "NaN in the header bounds parsed"
    # normals: a quad in z=0 (counter-clockwise from +z); stored +z, turned 90 deg (+x), 180 deg (-z), none
    qp = array("f", [0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0])
    qn = array("f", [0, 0, 1, 1, 0, 0, 0, 0, -1, 0, 0, 0])
    qi = array("I", [0, 1, 2, 0, 2, 3])
    assert fix_normals(qp, qn, qi) == 2 and list(qn) == [0, 0, 1] + [0] * 9, list(qn)
    assert fix_normals(qp, qn, qi) == 0, "fix_normals must be idempotent"
    qn = array("f", [0, 0, 1] * 4)
    assert fix_normals(qp, qn, array("I", [0, 2, 1, 0, 3, 2])) == 4, "clockwise triangles against +z: all off"
    # a hard edge on shared vertices: an L of two quads (floor z=0, wall x=1), every normal +z;
    # each vertex is within 60 degrees of its triangles' mean, but the wall faces -x
    lp = array("f", [0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0.2, 1, 1, 0.2])
    ln = array("f", [0, 0, 1] * 6)
    li = array("I", [0, 1, 2, 0, 2, 3, 1, 4, 5, 1, 5, 2])
    assert fix_normals(lp, ln, li) == 4 and list(ln[:3]) == [0, 0, 1] and not any(ln[3:9]) and not any(ln[12:]), list(ln)
    print("md6 Selbsttest: ok")


def corpus(game_root):
    """Every baseModel entry of every archive of the install (not first-wins):
    parse, per-LOD bounds == min/max of the served positions, header bounds ==
    union of LOD 0, the material list names its mesh; controls: 4 bytes cut
    or 1 byte appended must fail in every file. -> (clean, total, causes, controls)."""
    from collections import Counter
    from pathlib import Path

    from .mapcatalog import Mount
    root = Path(game_root)
    paths = sorted(root.glob("base/*.resources")) + sorted(root.glob("dlc/*/base/*.resources"))
    clean, causes, controls, total = 0, Counter(), 0, 0
    with Mount(paths, root) as mount:
        for a in mount.archives:
            for e in a.entries:
                if e.type != "baseModel":
                    continue
                total += 1
                data = mount.read(a, e)
                controls += (not _fails(parse, data[:-4])) + (not _fails(parse, data + b"\0"))
                try:
                    md = parse(data)
                    b0 = []
                    for mi, m in enumerate(md["meshes"]):
                        for li in range(3):
                            got = surface_arrays(m, li)
                            if got is None:
                                continue
                            p = got[0]
                            box = [min(p[0::3]), min(p[1::3]), min(p[2::3]), max(p[0::3]), max(p[1::3]), max(p[2::3])]
                            if box != m["lods"][li]["bounds"]:
                                raise MapGeoError("LOD-Grenzen != min/max")
                            if li == 0:
                                b0.append(box)
                    union = [min(b[j] for b in b0) for j in range(3)] + [max(b[j + 3] for b in b0) for j in range(3)]
                    if b0 and union != md["bounds"]:
                        raise MapGeoError("Kopf-Grenzen != Vereinigung LOD 0")
                    if any(mi >= len(md["meshes"]) or md["meshes"][mi]["material"] != name
                           for name, mi, _lo, _hi in md["mats"]):
                        raise MapGeoError("Materialliste nennt falsches Mesh")
                    clean += 1
                except MapGeoError as exc:
                    causes[str(exc).split(":")[0]] += 1
                    print("  FEHLER %s (%s): %s" % (e.name, a.path.name, exc))
    return clean, total, causes, controls


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    selftest()
    root = argv[0] if argv else None
    if root is None:
        try:
            from .game import Game
            root = next((g.root for g in Game.find_all() if g.title.key == "tnc"), None)
        except Exception:  # noqa: BLE001 -- no install: the synthetic check is all there is
            root = None
    if root is None:
        print("Kein Wolfenstein II gefunden: nur der Selbsttest lief.")
        return 0
    clean, total, causes, controls = corpus(root)
    print("baseModel-Einträge: %d/%d dekodiert und geprüft, Ursachen der Fehler: %s"
          % (clean, total, dict(causes) or "keine"))
    print("Gegenprobe (4 Bytes weg / 1 Byte dran): %d von %d Fällen fälschlich gelesen" % (controls, 2 * total))
    return 0 if clean == total and total and not controls else 1


if __name__ == "__main__":
    sys.exit(main())
