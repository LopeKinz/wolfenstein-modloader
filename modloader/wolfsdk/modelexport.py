"""Export a Studio model with its colour maps: glTF 2.0 (GLB, or .gltf in a ZIP) and OBJ + MTL + PNG.

    name, data, mime = export(studio, game, model_id, fmt="glb", surfaces=None, skin=None, max_tex=FULL_TEX)
    python -m wolfsdk.modelexport <game> <model id or search> [--format glb|gltf|obj] [--out DIR]
                                  [--all-surfaces] [--max-tex N]

Nothing is decoded here: the geometry is LOD 0 of the Studio's models/mesh.bin
(Studio.model_mesh: md6.surface_arrays or mapgeo.model_surfaces, md6.fix_normals),
the colour maps are its models/albedo.png (Studio.model_albedo) of each surface's
albedo URL, so a file holds what the viewer shows. Bind pose only: skeleton,
skin weights, morphs and animations are not exported.

surfaces: indices into info.surfaces; None = the ones the Studio shows at start
(md6Def meshKits, studio._kits). Surfaces without triangles are left out.
skin: (material, PNG bytes) replaces that material's colour map on every
exported surface with it, as the viewer's 'Eigener Skin' does.

glb   one file: JSON + BIN chunk, the PNGs embedded as buffer views
gltf  ZIP: <name>.gltf, <name>.bin, textures/*.png (textures editable)
obj   ZIP: <name>.obj, <name>.mtl, textures/*.png (one `o` + `usemtl` per surface)

Per surface one node with one mesh of one primitive (POSITION, NORMAL,
TEXCOORD_0 float32, u32 indices, counter-clockwise as the Studio sends them),
under a root node named after the model; one material per game material
(baseColorTexture, double sided as in the viewer; untextured: the viewer's grey).

Axes: the game is +Z up, metres; glTF (and OBJ as Blender reads it) is +Y up.
Every position and normal is turned by

    [x']   [1  0  0] [x]
    [y'] = [0  0  1] [y]      game (x, y, z) -> (x, z, -y), determinant +1: a rotation,
    [z']   [0 -1  0] [z]      so the winding stays; the viewer's world group turns the same way

Blender turns glTF's Y-up back to Z-up on import: there the model lies in game
coordinates again. Metres stay metres.

UV: the game's v runs top-down, as glTF's (origin top left): written as it is.
OBJ's v runs bottom-up: v' = 1 - v.

Normals: the Studio's where it sends one (|n|^2 > 0.01, as the viewer),
normalised; where it sends 0 (none stored, or contradicted by the triangles)
the area-weighted normal of the vertex's triangles (the viewer's
computeVertexNormals); a vertex without triangle area gets +Y (up).

Errors are the Studio's (German): BadRequest, Missing, Unsupported.
"""

import argparse
import io
import json
import math
import os
import re
import struct
import sys
import time
import zipfile
from array import array
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import studio

FORMATS = {"glb": ("model/gltf-binary", ".glb"), "gltf": ("application/zip", "_gltf.zip"),
           "obj": ("application/zip", "_obj.zip")}
PNG_SIG = b"\x89PNG\r\n\x1a\n"
MAX_SKIN = 64 << 20                                  # bytes of an uploaded skin PNG
OUT = Path.home() / "Downloads" / "WolfSDK-Export"   # the CLI's default folder
GREY = [round(((c / 255 + 0.055) / 1.055) ** 2.4, 4) for c in (0x8A, 0x90, 0x99)] + [1.0]   # viewer grey, linear
NOTE = "bind pose without skeleton, weights and animations"


def _wmd1(data):
    """[(pos, nrm, uv, idx)] of the Studio's WMD1 (md6.pack)."""
    n, = struct.unpack_from("<I", data, 4)
    o, out = 8, []
    for _ in range(n):
        nv, ni = struct.unpack_from("<II", data, o)
        o += 8
        part = []
        for code, count in (("f", 3 * nv), ("f", 3 * nv), ("f", 2 * nv), ("I", ni)):
            part.append(array(code, data[o:o + 4 * count]))
            o += 4 * count
        out.append(part)
    return out


def _normals(pos, nrm, idx):
    """Unit normals, game axes (see the module docstring)."""
    nv = len(pos) // 3
    n = nrm.tolist()
    need = [n[v] * n[v] + n[v + 1] * n[v + 1] + n[v + 2] * n[v + 2] <= 0.01 for v in range(0, 3 * nv, 3)]
    if any(need):
        p, ix, acc = pos.tolist(), idx.tolist(), [0.0] * (3 * nv)
        for t in range(0, len(ix), 3):
            a, b, c = 3 * ix[t], 3 * ix[t + 1], 3 * ix[t + 2]
            ux, uy, uz = p[b] - p[a], p[b + 1] - p[a + 1], p[b + 2] - p[a + 2]
            vx, vy, vz = p[c] - p[a], p[c + 1] - p[a + 1], p[c + 2] - p[a + 2]
            x, y, z = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            for v in (a, b, c):
                acc[v] += x
                acc[v + 1] += y
                acc[v + 2] += z
        for v, miss in enumerate(need):
            if miss:
                n[3 * v:3 * v + 3] = acc[3 * v:3 * v + 3]
    out = []
    for v in range(0, 3 * nv, 3):
        x, y, z = n[v], n[v + 1], n[v + 2]
        ln = math.sqrt(x * x + y * y + z * z)
        out += (x / ln, y / ln, z / ln) if ln > 1e-30 else (0.0, 0.0, 1.0)
    return array("f", out)


def y_up(a):
    """game (x, y, z) -> glTF (x, z, -y) of a flat f32[3n]."""
    out = array("f", a)
    out[1::3] = a[2::3]
    out[2::3] = array("f", [-v for v in a[1::3]])
    return out


def _token(text):
    """A name OBJ/MTL can carry: no whitespace."""
    return re.sub(r"\s+", "_", text.strip()) or "_"


def _stem(text):
    """A file name part: letters, digits, . _ - only."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._") or "modell"


def png_size(png):
    """(width, height) from the IHDR of PNG bytes, or None when they are not a PNG."""
    if len(png) < 24 or png[:8] != PNG_SIG or png[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", png[16:24])
    return (w, h) if w and h else None


# -- gather ----------------------------------------------------------------------

def _gather(st, game, mid, surfaces, skin, max_tex):
    """(info, [(surface info, pos, nrm, uv, idx) y-up], [material], {material: image index or None},
    [(file stem, PNG bytes)])."""
    info = st.info(game, studio.MODELS, mid)
    table = info["surfaces"]
    if surfaces is None:
        surfaces = [s["index"] for s in table if s.get("visible") is not False]
    surfaces = sorted(set(surfaces))
    bad = [i for i in surfaces if not 0 <= i < len(table)]
    if bad:
        raise studio.BadRequest("There is no surface %d: the model has %d (0 to %d)."
                                % (bad[0], len(table), len(table) - 1))
    if not surfaces:
        raise studio.BadRequest("No surface chosen: nothing to export.")
    if skin is not None and (png_size(skin[1]) is None or skin[1][-8:-4] != b"IEND"):
        raise studio.BadRequest("The custom skin is not a complete PNG file.")
    if skin is not None and skin[0] not in {table[i]["material"] for i in surfaces}:
        raise studio.BadRequest("The custom skin belongs to %s, which none of the chosen surfaces use." % skin[0])
    parts = _wmd1(st.model_mesh(game, mid, 0))
    chosen = [i for i in surfaces if len(parts[i][3])]
    if not chosen:
        raise studio.BadRequest("The chosen surfaces have no triangles.")
    surfs, mats, tex_of, images, by_key, stems = [], [], {}, [], {}, set()
    for i in chosen:
        s, (pos, nrm, uv, idx) = table[i], parts[i]
        surfs.append((s, y_up(pos), y_up(_normals(pos, nrm, idx)), uv, idx))
        mat = s["material"]
        if mat in tex_of:
            continue
        mats.append(mat)
        if skin is not None and mat == skin[0]:
            key = "skin"
        else:
            key = parse_qs(urlsplit(s["albedo"]).query)["id"][0] if s["albedo"] else None
        if key is not None and key not in by_key:
            stem = _stem(mat.rsplit("/", 1)[-1] + ("_skin" if key == "skin" else ""))
            while stem in stems:
                stem += "_"
            stems.add(stem)
            by_key[key] = len(images)
            images.append((stem, skin[1] if key == "skin" else st.model_albedo(game, key, max_tex)))
        tex_of[mat] = by_key.get(key)
    return info, surfs, mats, tex_of, images


# -- writers ---------------------------------------------------------------------

def _gltf(name, mid, surfs, mats, tex_of, images, embed):
    """(glTF JSON dict, buffer bytes). embed: the PNGs go into the buffer (GLB), else textures/<stem>.png."""
    blob, views, accs = bytearray(), [], []

    def view(data, target=None):
        blob.extend(bytes(-len(blob) % 4))           # every view starts 4-byte aligned
        v = {"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)}
        if target:
            v["target"] = target
        views.append(v)
        blob.extend(data)
        return len(views) - 1

    def acc(a, kind, ctype, target, bounds=False):
        k = {"SCALAR": 1, "VEC2": 2, "VEC3": 3}[kind]
        d = {"bufferView": view(a.tobytes(), target), "componentType": ctype, "count": len(a) // k, "type": kind}
        if bounds:
            d["min"] = [min(a[j::k]) for j in range(k)]
            d["max"] = [max(a[j::k]) for j in range(k)]
        accs.append(d)
        return len(accs) - 1

    nodes, meshes = [{"name": name, "children": list(range(1, len(surfs) + 1))}], []
    for s, pos, nrm, uv, idx in surfs:
        prim = {"attributes": {"POSITION": acc(pos, "VEC3", 5126, 34962, True),
                               "NORMAL": acc(nrm, "VEC3", 5126, 34962),
                               "TEXCOORD_0": acc(uv, "VEC2", 5126, 34962)},
                "indices": acc(idx, "SCALAR", 5125, 34963), "material": mats.index(s["material"]), "mode": 4}
        meshes.append({"name": s["name"], "primitives": [prim]})
        nodes.append({"name": s["name"], "mesh": len(meshes) - 1})
    materials = []
    for m in mats:
        pbr = {"metallicFactor": 0.0, "roughnessFactor": 0.9}
        if tex_of[m] is None:
            pbr["baseColorFactor"] = GREY
        else:
            pbr["baseColorTexture"] = {"index": tex_of[m]}
        materials.append({"name": m, "pbrMetallicRoughness": pbr, "doubleSided": True})
    doc = {"asset": {"version": "2.0", "generator": "wolfsdk modelexport",
                     "extras": {"model": mid, "axes": "Y up, metres; game (x, y, z) -> (x, z, -y)", "note": NOTE}},
           "scene": 0, "scenes": [{"name": name, "nodes": [0]}], "nodes": nodes, "meshes": meshes,
           "materials": materials}
    if images:
        doc["samplers"] = [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}]
        doc["textures"] = [{"sampler": 0, "source": i} for i in range(len(images))]
        doc["images"] = [{"name": stem, "mimeType": "image/png", "bufferView": view(png)} if embed
                         else {"name": stem, "uri": "textures/%s.png" % stem} for stem, png in images]
    doc.update(accessors=accs, bufferViews=views, buffers=[{"byteLength": len(blob)}])
    if not embed:
        doc["buffers"][0]["uri"] = name + ".bin"
    return doc, bytes(blob)


def _glb(doc, blob):
    j = json.dumps(doc, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    j += b" " * (-len(j) % 4)
    b = blob + bytes(-len(blob) % 4)
    return b"".join((struct.pack("<4sII", b"glTF", 2, 28 + len(j) + len(b)), struct.pack("<I4s", len(j), b"JSON"), j,
                     struct.pack("<I4s", len(b), b"BIN\0"), b))


def _obj(name, mid, surfs, mats, tex_of, images):
    """(obj text, mtl text)."""
    g = lambda v: "%.9g" % v  # noqa: E731 -- 9 digits: every float32 comes back exactly
    out = ["# %s (%s), exported by wolfsdk: %s" % (name, mid, NOTE),
           "# Axes: Y up, metres (game Z up: (x, y, z) -> (x, z, -y)); vt: v = 1 - the game's v",
           "mtllib %s.mtl" % name]
    base = 1
    for s, pos, nrm, uv, idx in surfs:
        out.append("o " + _token(s["name"]))
        out += ["v %s %s %s" % (g(x), g(y), g(z)) for x, y, z in zip(pos[0::3], pos[1::3], pos[2::3])]
        out += ["vt %s %s" % (g(u), g(1 - v)) for u, v in zip(uv[0::2], uv[1::2])]
        out += ["vn %s %s %s" % (g(x), g(y), g(z)) for x, y, z in zip(nrm[0::3], nrm[1::3], nrm[2::3])]
        out.append("usemtl " + _token(s["material"]))
        out += ["f {0}/{0}/{0} {1}/{1}/{1} {2}/{2}/{2}".format(a + base, b + base, c + base)
                for a, b, c in zip(idx[0::3], idx[1::3], idx[2::3])]
        base += len(pos) // 3
    mtl = ["# %s, exported by wolfsdk" % name]
    for m in mats:
        t = tex_of[m]
        mtl += ["", "newmtl " + _token(m), "Ka 0 0 0", "Kd %s" % (" ".join(g(c) for c in GREY[:3]) if t is None else "1 1 1"),
                "Ks 0 0 0", "d 1", "illum 1"]
        if t is not None:
            mtl.append("map_Kd textures/%s.png" % images[t][0])
    return "\n".join(out) + "\n", "\n".join(mtl) + "\n"


def _zip(files):
    buf = io.BytesIO()
    stamp = time.localtime()[:6]
    with zipfile.ZipFile(buf, "w") as z:
        for path, data in files:
            z.writestr(zipfile.ZipInfo(path, stamp), data,
                       zipfile.ZIP_STORED if path.endswith(".png") else zipfile.ZIP_DEFLATED)
    return buf.getvalue()


FULL_TEX = 4096   # exports carry the game's largest mip (first-person weapons are 4096)


def export(st, game, mid, fmt="glb", surfaces=None, skin=None, max_tex=FULL_TEX):
    """(file name, bytes, content type) of model `mid` as `fmt` (see the module docstring)."""
    if fmt not in FORMATS:
        raise studio.BadRequest("format must be glb, gltf or obj.")
    info, surfs, mats, tex_of, images = _gather(st, game, mid, surfaces, skin, max_tex)
    name = _stem(info["name"])
    mime, ext = FORMATS[fmt]
    pngs = [("textures/%s.png" % stem, png) for stem, png in images]
    if fmt == "obj":
        obj, mtl = _obj(name, mid, surfs, mats, tex_of, images)
        data = _zip([(name + ".obj", obj.encode("utf-8")), (name + ".mtl", mtl.encode("utf-8"))] + pngs)
    else:
        doc, blob = _gltf(name, mid, surfs, mats, tex_of, images, embed=fmt == "glb")
        if fmt == "glb":
            data = _glb(doc, blob)
        else:
            text = json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False).encode("utf-8")
            data = _zip([(name + ".gltf", text), (name + ".bin", blob)] + pngs)
    return name + ext, data, mime


# -- command line ----------------------------------------------------------------

def find(st, game, q):
    """Model id of `q`: an id, else the one hit of a search (an exact name wins)."""
    cat = st.catalog(game, studio.MODELS)
    if q in cat.by_id:
        return q
    got = st.listing(game, studio.MODELS, q, limit=studio.MAX_LIMIT)
    hits = got["items"]
    exact = [r for r in hits if r["name"].lower() == q.lower()]
    if len(exact) == 1:
        return exact[0]["id"]
    if got["total"] == 1:
        return hits[0]["id"]
    if not hits:
        raise studio.Missing("No model matches \"%s\"." % q)
    raise studio.BadRequest("\"%s\" matches %d models. Be more specific, or use the full id:\n  %s%s" % (
        q, got["total"], "\n  ".join(r["id"] for r in hits[:20]), "\n  …" if got["total"] > 20 else ""))


def check_out(out, roots):
    """Refuse a target folder inside a game install: exports never go into the game."""
    def norm(p):                      # \\?\ paths, case and trailing dots/spaces must not slip past the guard
        p = os.fspath(p)
        p = p[4:] if p.startswith(("\\\\?\\", "//?/")) else p
        return Path(os.path.normcase(os.path.realpath(p.rstrip(" ."))))
    o = norm(out)
    for r in roots.values():
        if r is not None and (o == norm(r) or norm(r) in o.parents):
            raise studio.BadRequest("Do not export into the game folder (%s). Choose another target folder." % r)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m wolfsdk.modelexport",
                                 description="Export a Studio model with its textures (%s)." % NOTE)
    ap.add_argument("game", choices=("tnc", "tno"), help="tnc = Wolfenstein II (TNO models are not supported yet)")
    ap.add_argument("model", help="model id or search term, e.g. pistole61")
    ap.add_argument("--format", choices=list(FORMATS), default="glb",
                    help="glb: one file; gltf, obj: ZIP with textures as PNG (default glb)")
    ap.add_argument("--out", type=Path, default=OUT, help="target folder (default: %(default)s)")
    ap.add_argument("--all-surfaces", "--alle-flaechen", dest="all_surfaces", action="store_true",
                    help="also the variants the Studio hides at first (gore, gear, spare heads)")
    ap.add_argument("--max-tex", type=int, default=FULL_TEX,
                    help="longest texture side, 16-4096 (default %(default)s = the game's full resolution)")
    for stream in (sys.stdout, sys.stderr):   # as cli.py: cp1252 consoles mangle the German text
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    a = ap.parse_args(argv)
    st = studio.Studio()
    try:
        check_out(a.out, st.roots())
        mid = find(st, a.game, a.model)
        surfaces = range(len(st.info(a.game, studio.MODELS, mid)["surfaces"])) if a.all_surfaces else None
        t = time.time()
        name, data, _mime = export(st, a.game, mid, a.format, surfaces, max_tex=a.max_tex)
        a.out.mkdir(parents=True, exist_ok=True)
        path = a.out / name
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    except studio.StudioError as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("Error while writing: %s" % exc, file=sys.stderr)
        return 1
    finally:
        st.close()
    print("%s -> %s (%.1f MB, %.1f s; %s)" % (mid, path, len(data) / 1048576, time.time() - t, NOTE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
