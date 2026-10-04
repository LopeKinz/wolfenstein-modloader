"""glTF 2.0 (.gltf + .bin, or .glb) -> a neutral in-memory scene in TNC's axes and units.

Stdlib only. Reads meshes (TRIANGLES primitives: POSITION, NORMAL, TEXCOORD_0,
indices), the node hierarchy (matrix or translation/rotation/scale) and
materials (name, baseColorFactor, baseColorTexture -> image reference). Every
mesh comes out in WORLD space, already converted to the game's convention.

Axes and units, derived from retail data:
  * TNC is +Z up, 1 unit = 1 metre. re_probes/kitmap/kit/kit.json measured
    every kit piece's LOD 0 bounds against the size in its file name
    (man_wall_400x400 is 4.0 m, floor_400x400cm is 4.0 m, ...), and the retail
    crate metal_crate_02_70cm_100cm is 0.692 m tall (r4_modelimport.md).
    tools/verify_gltfimport.py re-counts the kit.json name/size agreement.
  * glTF is +Y up, metres, right-handed. game = (x, -z, y), scale 1: a +90
    degree turn about X (determinant +1, so triangle winding is kept). It is
    the exact inverse of wolfsdk.modelexport.y_up, so a model the Studio
    exported comes back at the coordinates it left.
  * UVs are kept as stored: the Studio's GLB export writes the game's uv0
    unflipped and r4 re-imported it bit-identically.

Refused with a GltfError, never silently misread: skins, morph targets, Draco
(and every other extension a file marks as required), sparse accessors,
non-triangle primitives, out-of-range accessors and indices.

    scene = load("level.glb")
    for m in scene.meshes: m.node, m.positions, m.indices, scene.materials[m.material]
"""

from __future__ import annotations

import base64
import json
import math
import struct
import sys
import urllib.parse
from array import array
from dataclasses import dataclass, field
from pathlib import Path


class GltfError(ValueError):
    pass


# componentType -> (array/struct code, divisor when normalized)
COMPONENT = {5120: ("b", 127.0), 5121: ("B", 255.0), 5122: ("h", 32767.0),
             5123: ("H", 65535.0), 5125: ("I", None), 5126: ("f", None)}
NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
# glTF (x, y, z) -> game (x, -z, y), column-major like every glTF matrix
TO_GAME = (1, 0, 0, 0, 0, 0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1)
IDENTITY = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)


@dataclass
class Texture:
    image: int                # index into the file's images
    name: str
    uri: str | None           # file name relative to the .gltf, or a data: URI
    mime: str | None
    buffer_view: int | None   # embedded image (GLB)


@dataclass
class Material:
    name: str
    base_color: tuple = (1.0, 1.0, 1.0, 1.0)
    texture: Texture | None = None    # pbrMetallicRoughness.baseColorTexture


@dataclass
class Mesh:
    """One primitive, world space, game axes, metres."""
    node: str                 # unique per node; a node's primitives share it
    material: int | None      # index into Scene.materials
    positions: array          # f32 x y z
    normals: array | None     # f32 unit vectors, None when the file has none
    uvs: array | None         # f32 u v (TEXCOORD_0), None when the file has none
    indices: array            # u32, counter-clockwise front faces (kept through mirrored nodes)
    origin: tuple             # the node's world origin, game axes: the natural pivot

    @property
    def num_verts(self):
        return len(self.positions) // 3


@dataclass
class Scene:
    meshes: list = field(default_factory=list)
    materials: list = field(default_factory=list)

    def bounds(self):
        """(min xyz, max xyz) over every mesh, game axes."""
        lo, hi = [math.inf] * 3, [-math.inf] * 3
        for m in self.meshes:
            for i in range(3):
                c = m.positions[i::3]
                lo[i], hi[i] = min(lo[i], min(c)), max(hi[i], max(c))
        return tuple(lo), tuple(hi)


# -- file framing -------------------------------------------------------------

def load(path):
    """A .gltf (external or data: URI buffers) or .glb file -> Scene."""
    path = Path(path)
    data = path.read_bytes()
    if data[:4] == b"glTF":
        doc, blob = _glb(data)
    else:
        try:
            doc = json.loads(data.decode("utf-8-sig"))
        except ValueError as exc:
            raise GltfError("%s is neither a GLB nor glTF JSON (%s)" % (path.name, exc)) from None
        blob = None
    return read(doc, blob, path.parent)


def _glb(data):
    if len(data) < 20:
        raise GltfError("GLB is truncated (%d bytes)" % len(data))
    _magic, version, length = struct.unpack_from("<4sII", data)
    if version != 2:
        raise GltfError("GLB container version %d, only 2 is supported" % version)
    if length > len(data):
        raise GltfError("GLB header says %d bytes, file has %d" % (length, len(data)))
    o, doc, blob = 12, None, None
    while o + 8 <= length:
        size, kind = struct.unpack_from("<I4s", data, o)
        o += 8
        if o + size > length:
            raise GltfError("GLB chunk %r runs past the end of the file" % kind)
        if kind == b"JSON" and doc is None:
            doc = json.loads(data[o:o + size].decode("utf-8"))
        elif kind == b"BIN\0" and blob is None:
            blob = data[o:o + size]
        o += size
    if doc is None:
        raise GltfError("GLB has no JSON chunk")
    return doc, blob


# -- matrices (column-major, 16 floats) --------------------------------------

def _mul(a, b):
    return tuple(sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4)) for c in range(4) for r in range(4))


def _local(node):
    if "matrix" in node:
        m = node["matrix"]
        if len(m) != 16:
            raise GltfError("node %r: matrix has %d numbers, not 16" % (node.get("name"), len(m)))
        return tuple(float(v) for v in m)
    tx, ty, tz = node.get("translation", (0.0, 0.0, 0.0))
    x, y, z, w = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    sx, sy, sz = node.get("scale", (1.0, 1.0, 1.0))
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return (
        (1 - 2 * (y * y + z * z)) * sx, (2 * (x * y + z * w)) * sx, (2 * (x * z - y * w)) * sx, 0.0,
        (2 * (x * y - z * w)) * sy, (1 - 2 * (x * x + z * z)) * sy, (2 * (y * z + x * w)) * sy, 0.0,
        (2 * (x * z + y * w)) * sz, (2 * (y * z - x * w)) * sz, (1 - 2 * (x * x + y * y)) * sz, 0.0,
        float(tx), float(ty), float(tz), 1.0)


def _normal_matrix(m):
    """Cofactor matrix of the upper 3x3 (== det * inverse-transpose), rows, and det."""
    a = [[m[c * 4 + r] for c in range(3)] for r in range(3)]
    cof = [[a[(r + 1) % 3][(c + 1) % 3] * a[(r + 2) % 3][(c + 2) % 3]
            - a[(r + 1) % 3][(c + 2) % 3] * a[(r + 2) % 3][(c + 1) % 3] for c in range(3)] for r in range(3)]
    det = sum(a[0][c] * cof[0][c] for c in range(3))
    return cof, det


# -- the reader ---------------------------------------------------------------

class _Reader:
    def __init__(self, doc, glb_bin, base_dir):
        self.doc = doc
        ver = str(doc.get("asset", {}).get("version", ""))
        if not ver.startswith("2."):
            raise GltfError("glTF version %r, only 2.x is supported" % ver)
        required = doc.get("extensionsRequired") or []
        if required:
            hint = " (Draco: export without mesh compression)" if "KHR_draco_mesh_compression" in required else ""
            raise GltfError("required extension(s) not supported: %s%s" % (", ".join(required), hint))
        if doc.get("skins"):
            raise GltfError("skins are not supported: static geometry only "
                            "(apply or remove the armature before exporting)")
        self.buffers = []
        for i, b in enumerate(doc.get("buffers", [])):
            uri = b.get("uri")
            if uri is None:
                if i != 0 or glb_bin is None:
                    raise GltfError("buffer %d has no uri and there is no GLB BIN chunk" % i)
                data = glb_bin
            elif uri.startswith("data:"):
                data = base64.b64decode(uri.split(",", 1)[1])
            else:
                if base_dir is None:
                    raise GltfError("buffer %d refers to the file %r but no folder was given" % (i, uri))
                try:
                    data = (Path(base_dir) / urllib.parse.unquote(uri)).read_bytes()
                except OSError as exc:
                    raise GltfError("buffer %d: cannot read %r (%s)" % (i, uri, exc)) from None
            if len(data) < b.get("byteLength", 0):
                raise GltfError("buffer %d holds %d bytes, byteLength says %d" % (i, len(data), b["byteLength"]))
            self.buffers.append(data)

    def accessor(self, i, types, components, what):
        """Flat array of accessor `i`; normalized integers come back as floats."""
        try:
            acc = self.doc["accessors"][i]
        except (IndexError, KeyError, TypeError):
            raise GltfError("%s: accessor %r does not exist" % (what, i)) from None
        if "sparse" in acc:
            raise GltfError("%s: sparse accessors are not supported" % what)
        if acc.get("type") not in types or acc.get("componentType") not in components:
            raise GltfError("%s: accessor %d is %s/%s, want %s of %s" % (
                what, i, acc.get("type"), acc.get("componentType"), "/".join(types), components))
        code, norm = COMPONENT[acc["componentType"]]
        n, count = NCOMP[acc["type"]], acc["count"]
        if "bufferView" not in acc:                    # spec: no view = all zeros
            return array("f" if code == "f" or acc.get("normalized") else code, bytes(4 * n * count))[:n * count]
        bv = self.doc["bufferViews"][acc["bufferView"]]
        buf = self.buffers[bv["buffer"]]
        elem = struct.calcsize("<" + code) * n
        stride = bv.get("byteStride") or elem
        start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        end = start + stride * (count - 1) + elem if count else start
        if end > bv.get("byteOffset", 0) + bv["byteLength"] or end > len(buf):
            raise GltfError("%s: accessor %d reads past its buffer view (%d > %d)" % (
                what, i, end, min(bv.get("byteOffset", 0) + bv["byteLength"], len(buf))))
        raw = buf[start:end] if stride == elem else b"".join(
            buf[start + k * stride:start + k * stride + elem] for k in range(count))
        out = array(code, raw)
        if sys.byteorder == "big":
            out.byteswap()
        if acc.get("normalized") and norm:
            return array("f", (max(v / norm, -1.0) for v in out))
        return out

    def materials(self):
        out = []
        textures, images = self.doc.get("textures", []), self.doc.get("images", [])
        for k, m in enumerate(self.doc.get("materials", [])):
            pbr = m.get("pbrMetallicRoughness", {})
            tex = None
            ref = pbr.get("baseColorTexture")
            if ref is not None:
                try:
                    src = textures[ref["index"]]["source"]
                    img = images[src]
                except (IndexError, KeyError, TypeError):
                    raise GltfError("material %r: baseColorTexture does not lead to an image"
                                    % m.get("name", k)) from None
                tex = Texture(src, img.get("name", ""), img.get("uri"), img.get("mimeType"), img.get("bufferView"))
            out.append(Material(m.get("name") or "material_%d" % k,
                                tuple(float(v) for v in pbr.get("baseColorFactor", (1.0, 1.0, 1.0, 1.0))), tex))
        return out

    def roots(self):
        nodes = self.doc.get("nodes", [])
        scenes = self.doc.get("scenes")
        if scenes:
            return list(scenes[self.doc.get("scene", 0)].get("nodes", []))
        children = {c for n in nodes for c in n.get("children", [])}
        return [i for i in range(len(nodes)) if i not in children]

    def scene(self):
        doc, out = self.doc, Scene(materials=self.materials())
        nodes, seen, labels = doc.get("nodes", []), set(), set()
        stack = [(i, IDENTITY) for i in reversed(self.roots())]
        while stack:
            i, parent = stack.pop()
            if i in seen:
                raise GltfError("node %d is reached twice (a cycle or a shared child)" % i)
            seen.add(i)
            node = nodes[i]
            if "skin" in node:
                raise GltfError("node %r is skinned: skins are not supported" % node.get("name", i))
            if node.get("weights"):
                raise GltfError("node %r has morph weights: morph targets are not supported" % node.get("name", i))
            world = _mul(parent, _local(node))
            stack += [(c, world) for c in reversed(node.get("children", []))]
            if "mesh" in node:
                label = node.get("name") or "node_%d" % i
                while label in labels:
                    label += "_%d" % i
                labels.add(label)
                out.meshes += self.mesh(node["mesh"], label, world)
        return out

    def mesh(self, k, label, world):
        mesh = self.doc["meshes"][k]
        what = "mesh %r (node %r)" % (mesh.get("name", k), label)
        if mesh.get("weights"):
            raise GltfError("%s: morph targets are not supported" % what)
        g = _mul(TO_GAME, world)
        cof, det = _normal_matrix(g)
        if abs(det) < 1e-12:
            raise GltfError("%s: the node transform squashes the mesh flat (scale 0?)" % what)
        sign = 1.0 if det > 0 else -1.0
        origin = (g[12], g[13], g[14])
        out = []
        for p, prim in enumerate(mesh.get("primitives", [])):
            w = "%s primitive %d" % (what, p)
            if "KHR_draco_mesh_compression" in prim.get("extensions", {}):
                raise GltfError("%s: Draco-compressed (export without mesh compression)" % w)
            if prim.get("targets"):
                raise GltfError("%s: morph targets are not supported" % w)
            if prim.get("mode", 4) != 4:
                raise GltfError("%s: mode %d, only TRIANGLES (4) is supported" % (w, prim["mode"]))
            attrs = prim.get("attributes", {})
            if "POSITION" not in attrs:
                raise GltfError("%s: no POSITION" % w)
            src = self.accessor(attrs["POSITION"], ("VEC3",), (5126,), w + " POSITION")
            nv = len(src) // 3
            pos = array("f", bytes(4 * len(src)))
            for v in range(nv):
                x, y, z = src[3 * v:3 * v + 3]
                for r in range(3):
                    pos[3 * v + r] = g[r] * x + g[4 + r] * y + g[8 + r] * z + g[12 + r]
            nrm = None
            if "NORMAL" in attrs:
                src = self.accessor(attrs["NORMAL"], ("VEC3",), (5126,), w + " NORMAL")
                if len(src) != 3 * nv:
                    raise GltfError("%s: %d normals for %d positions" % (w, len(src) // 3, nv))
                nrm = array("f", bytes(4 * len(src)))
                for v in range(nv):
                    n = src[3 * v:3 * v + 3]
                    t = [sign * sum(cof[r][c] * n[c] for c in range(3)) for r in range(3)]
                    ln = math.sqrt(sum(c * c for c in t)) or 1.0
                    nrm[3 * v:3 * v + 3] = array("f", (c / ln for c in t))
            uv = None
            if "TEXCOORD_0" in attrs:
                uv = self.accessor(attrs["TEXCOORD_0"], ("VEC2",), (5126, 5121, 5123), w + " TEXCOORD_0")
                if uv.typecode != "f":
                    raise GltfError("%s: TEXCOORD_0 integers must be normalized" % w)
                if len(uv) != 2 * nv:
                    raise GltfError("%s: %d UVs for %d positions" % (w, len(uv) // 2, nv))
            if "indices" in prim:
                idx = array("I", self.accessor(prim["indices"], ("SCALAR",), (5121, 5123, 5125), w + " indices"))
            else:
                idx = array("I", range(nv))
            if len(idx) % 3:
                raise GltfError("%s: %d indices is not a whole number of triangles" % (w, len(idx)))
            if idx and max(idx) >= nv:
                raise GltfError("%s: index %d, but only %d vertices" % (w, max(idx), nv))
            if det < 0:                       # mirrored node: keep the front faces in front
                idx[1::3], idx[2::3] = idx[2::3], idx[1::3]
            out.append(Mesh(label, prim.get("material"), pos, nrm, uv, idx, origin))
        return out


def read(doc, glb_bin=None, base_dir=None):
    """A parsed glTF JSON dict (+ the GLB BIN chunk, + the folder external
    buffers live in) -> Scene."""
    return _Reader(doc, glb_bin, base_dir).scene()
