"""Wolfenstein: Youngblood md6mesh (`baseModel`), read only, for the Studio's model viewer.

Ported from re_probes/r15_ybgaps/ybmesh.py (471/471 retail files rebuild byte-exact there,
re_probes/agent_reports/r15_ybgaps.md) with the skin layout of re_probes/agent_reports/r16_ybmodels.md.

  md = parse(payload, sfile=None, oo=None)   like md6.parse; sfile = the rs_emb_sfile payload named
                                             stream_name(model id), needed for LODs streamed out of the file
  surface_arrays(mesh, lod=0)                (pos, nrm, uv, idx) as md6.surface_arrays, or None
  vertex_skin(mesh, lod=0)                   (slot bytes u8[4 nv], weights f32[4 nv]) of LOD lod, or None

Layout differences to Wolfenstein II (md6.py): a leading u8 flag (1: u32 streamed-LOD count, a u8 `streamed`
per LOD slot and a stream index at the end); per LOD a u8 vertex format and a quantisation block (u8[4],
f32[3] scale, f32[3] offset, f32[4] uv scale/offset) before the vertices. Format 1 = 40-byte vertex:
0x00 i16[3] position (value / 32768 * scale + offset), 0x0C u8[4] normal, 0x10 u8[4] tangent,
0x14 u8[4] joint slots, 0x18 f32[2] uv. Format 0 = Wolfenstein II's 48-byte vertex (hair cards).
Weights as in Wolfenstein II: w1 = (tangent.b3 & 127)/254, w2 = (normal.b3 >> 4)/45, w3 = (normal.b3 & 15)/60.
"""
import math
import struct
from array import array

from .md6 import MapGeoError, _NRM

STRIDES = {0: 48, 1: 40}


class _R:
    def __init__(self, d):
        self.d, self.o = d, 0

    def take(self, n):
        if n < 0 or self.o + n > len(self.d):
            raise MapGeoError("Youngblood md6mesh ends early (offset %d, %d more bytes)" % (self.o, n))
        self.o += n
        return self.d[self.o - n:self.o]

    def u8(self):
        return self.take(1)[0]

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def f(self, n):
        return list(struct.unpack("<%df" % n, self.take(4 * n)))

    def str(self):
        n = self.u32()
        if n > 4096:
            raise MapGeoError("Youngblood md6mesh: string of %d bytes" % n)
        return self.take(n).decode("latin1")


def stream_name(model_id):
    """The rs_emb_sfile entry that holds a model's streamed LODs."""
    return "basemodel#%s#0" % model_id


def _lod_body(r, lod):
    lod["nv"], lod["nt"] = r.u32(), r.u32()
    lod["bounds"] = r.f(6)
    r.take(4)
    lod["scale"], lod["off"], lod["uvq"] = r.f(3), r.f(3), r.f(4)
    stride = STRIDES.get(lod["fmt"])
    if stride is None:
        raise MapGeoError("Youngblood md6mesh: vertex format %d" % lod["fmt"])
    lod["stride"] = stride
    lod["verts"] = r.take(stride * lod["nv"])
    lod["idx"] = r.take(6 * lod["nt"])
    r.take(8)                                     # f32[2] uv density


def parse(data, sfile=None, oo=None):
    r = _R(data)
    flag = r.u8()
    if flag:
        r.u32()
    md = {"skeleton": r.str(), "box0": r.f(6), "flag": r.u8()}
    n = struct.unpack("<H", r.take(2))[0]
    md["joints"] = struct.unpack("<%dH" % n, r.take(2 * n))
    md["bounds"], _unk = r.f(6), r.f(3)
    md["meshes"] = []
    for _ in range(r.u32()):
        m = {"name": r.str(), "material": r.str()}
        r.u8()
        m["kind"], m["joint_first"], m["joint_count"], m["hash"] = struct.unpack("<4I", r.take(16))
        m["lods"] = []
        for _li in range(3):
            if r.u32():
                m["lods"].append(None)
                continue
            lod = {"fmt": r.u8(), "streamed": bool(r.u8()) if flag else False}
            if not lod["streamed"]:
                _lod_body(r, lod)
            m["lods"].append(lod)
        for _k in range(r.u32()):                 # morph names
            r.str()
            r.u32()
        if not flag:
            r.take(8 * r.u32())
            r.take(16 * r.u32())
        r.u8()
        md["meshes"].append(m)
    for _ in range(r.u32()):                      # material ranges
        r.str()
        r.take(12)
    for _ in range(r.u32()):                      # zones
        r.str()
        r.take(4 + 68 + 1)
    rest = data[r.o:]
    if not flag:
        if rest:
            raise MapGeoError("Youngblood md6mesh: %d bytes after the end" % len(rest))
        return md
    if len(rest) != 48 + 24 * len(md["meshes"]):
        raise MapGeoError("Youngblood md6mesh: stream index of %d bytes" % len(rest))
    w = struct.unpack("<%dI" % (len(rest) // 4), rest)
    streams = [w[4 * i:4 * i + 4] for i in range(3)]           # (blob offset, csize, usize, 1) per LOD
    ranges = [w[12 + 6 * i:18 + 6 * i] for i in range(len(md["meshes"]))]
    if sfile is None or oo is None:
        for m in md["meshes"]:
            m["lods"] = [None if lod is None or lod["streamed"] else lod for lod in m["lods"]]
        return md
    if sum(s[1] for s in streams) != len(sfile):
        raise MapGeoError("Youngblood md6mesh: stream file of %d bytes, index says %d" % (len(sfile), sum(s[1] for s in streams)))
    blobs = {li: oo.decompress(sfile[off:off + cs], us) for li, (off, cs, us, _one) in enumerate(streams) if cs}
    for mi, m in enumerate(md["meshes"]):
        for li, lod in enumerate(m["lods"]):
            if lod is not None and lod["streamed"]:
                o, size = ranges[mi][2 * li:2 * li + 2]
                _lod_body(_R(blobs[li][o:o + size]), lod)
    return md


def surface_arrays(mesh, lod=0):
    """(pos f32[3nv], nrm f32[3nv], uv f32[2nv], idx u32[3nt] counter-clockwise), as md6.surface_arrays."""
    lo = mesh["lods"][lod]
    if lo is None or "verts" not in lo:
        return None
    nv, v, st = lo["nv"], lo["verts"], lo["stride"]
    pos, nrm, uv = array("f", bytes(12 * nv)), array("f", bytes(12 * nv)), array("f", bytes(8 * nv))
    if lo["fmt"] == 1:
        h, f = array("h", v), array("f", v)
        for k in range(3):
            s, o = lo["scale"][k] / 32768, lo["off"][k]
            pos[k::3] = array("f", (x * s + o for x in h[k::20]))
            nrm[k::3] = array("f", map(_NRM.__getitem__, v[0x0C + k::40]))
        uv[0::2], uv[1::2] = f[6::10], f[7::10]
    else:
        f = array("f", v)
        pos[0::3], pos[1::3], pos[2::3] = f[0::12], f[1::12], f[2::12]
        uv[0::2], uv[1::2] = f[8::12], f[9::12]
        for k in range(3):
            nrm[k::3] = array("f", map(_NRM.__getitem__, v[0x14 + k::48]))
    idx = array("I", array("H", lo["idx"]))
    if idx and max(idx) >= nv:
        raise MapGeoError("Youngblood md6mesh %s: index %d >= %d vertices" % (mesh["name"], max(idx), nv))
    if not (all(map(math.isfinite, pos)) and all(map(math.isfinite, uv))):
        raise MapGeoError("Youngblood md6mesh %s: position or UV not finite" % mesh["name"])
    idx[1::3], idx[2::3] = idx[2::3], idx[1::3]   # clockwise -> counter-clockwise
    return pos, nrm, uv, idx


def vertex_skin(mesh, lod=0):
    """(slot bytes u8[4 nv], weights f32[4 nv]) of a LOD; format 0 (hair cards) follows slot 0 rigidly."""
    lo = mesh["lods"][lod]
    if lo is None or "verts" not in lo:
        return None
    nv, v, st = lo["nv"], lo["verts"], lo["stride"]
    slots, weights = bytearray(4 * nv), array("f", bytes(16 * nv))
    so = 0x14 if lo["fmt"] == 1 else 0x1C
    for k in range(4):
        slots[k::4] = v[so + k::st]
    if lo["fmt"] == 1:
        for i in range(nv):
            n3, t3 = v[st * i + 0x0F], v[st * i + 0x13]
            w1, w2, w3 = (t3 & 127) / 254, (n3 >> 4) / 45, (n3 & 15) / 60
            weights[4 * i:4 * i + 4] = array("f", (max(0.0, 1 - w1 - w2 - w3), w1, w2, w3))
    else:
        weights[0::4] = array("f", [1.0]) * nv
    return bytes(slots), weights
