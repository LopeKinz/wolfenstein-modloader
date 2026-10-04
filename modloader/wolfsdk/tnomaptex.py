"""Wolfenstein: The New Order textures for the 3D map viewer, in the contract
of wolfsdk/maptex.py (WTX1 blocks + PNG), for wolfsdk/tnomap.py:

    albedo_for_material(root, map_id, name)  -> texture id | None
    texture_info(root, tid)                  -> {id, name, format, w, h, srgb}
    texture_block(root, tid, max_side=1024)  -> WTX1 bytes (one mip)
    texture_png(root, tid, max_side=512)     -> PNG of the same mip
    classify(root, map_id, name)             -> (kind, image name | None)

`root` is the game directory, `map_id` a map as wolfsdk/tnomap.py names it
(game/wolf/c01/c01p1). Measured in tools/probe_tno_tex.py, proven by
tools/verify_tnomaptex.py.

Archives: every chunk pair of base/master.index, from base/_wolfsdk_backup/
when both files are there (the user's mods patch the install; the viewer
shows the original game). A map mounts its own chunk (the one holding
maps/<id>.entities) and chunk0; materials are looked up there only, like
tnomap does, images there first and then in every other chunk.

Texture id: sha1("tno-image:" + lower-case image entry name), 16 hex digits,
over all 2272 image names (unique, checked when the index is built). 212
names sit in two chunks, always with the same bytes (verify), so the id needs
no map and /api/tex can serve it without one.

Albedo (classify), first hit, placeholders `_white`, `_black` ... skipped:
  mega    landDefinitionFile: the unique megatexture (virtualtextures/
          <map>.pages), 54-99 % of a map's drawn triangles. Not decodable, so
          None and the viewer shades it flat -- measured, not assumed
          (probe_tno_tex.py mega): a per-map atlas needs the coarse levels
          (root .. 16x16 pages); there the colour plane (block 0, JPEG-XR-like
          YUV_444) walks bit-exact on 170 of 5665 pages (3.0 %, 0-20 % per
          map) against 98.7 % for the one-channel block 2, and no decoder
          here turns block 0 into pixels (docs/vt-planes.md 10.6).
  vmtr    virtualMapping: a virtual material in chunkN_vmtr.pages, same
          codec. Albedo only when its diffusemap also ships as an `image`
          (hair, foliage, constantcolor) -> "bim", else None: 1535 of 1595
          diffusemaps ship nowhere, not even under another prefix.
  sky     stageprogram sky*: spare1map, transmap.
  plain   transmap, sparediffusemap, texturemap, diffusemap, editormap
          (eyes: sparediffusemap is the iris, texturemap a lighting LUT).
No colour is invented for mega/vmtr (together 99.3-100 % of the drawn
triangles): their fallback is the viewer's flat shading. Per-map counts:
tools/verify_tnomaptex.py -> re_probes/tno_maps/tnotex/coverage.json.
Image names are the material's value verbatim ("uncompressed textures/..",
"linear ycocgdxt5 models/..", "constantcolor( 0.5, 0.5, 0.5, 1)"); the
compiler named the images that way, with its own spacing: 68 values match
only with blanks ignored ("constantColor ( 0 , 0 , 1 , 1 )").

BIM v9 (not wolfsdk/bim.py, which reads the first mip record's level and
face as header fields and so finds mip 0 only): u32 hash, 09 'MIB', 11 BE
u32 (type, w, h, depth, mips, ?, format, ...), then per mip BE u32 level,
face, w, h, size + data; type 2 = cube (6 faces per level), format 3 RGBA8,
5 A8, 10 BC1, 11 BC3. All 2566 retail images parse and rebuild byte-exact.

WTX1 as maptex: the largest face-0 mip <= max_side is served raw when it is
BC1/BC3 with edges a multiple of 4, or RGBA8. Everything else is decoded
(wolfsdk/tncimage.py's decoders) and served as RGBA8: A8, the 14 YCoCg-DXT5
skies (R=Co, G=Cg, B=(scale-1)*8, A=Y; van Waveren/Castano 2007), small
unaligned mips, and single-mip images larger than max_side (every n-th
pixel). Albedo is served as sRGB. Decoded pixels and PNGs are cached under
%LOCALAPPDATA%\\wolfsdk\\mapcache\\tno\\tex\\, keyed by id, a CRC of the
image bytes, the size and CACHE_VERSION. Read-only on the game.
"""

import hashlib
import os
import re
import struct
import threading
import zlib
from pathlib import Path

from . import image, tncimage
from .resources import Archive, load_master

WTX_HEADER = struct.Struct("<4sHHHHI")
WTX_CODE = {"RGBA8": 0, "BC1": 1, "BC3": 3}
BIM_MAGIC = b"\x09MIB"
BIM_HEAD = 0x34
BIM_FORMATS = {3: "RGBA8", 5: "R8", 10: "BC1", 11: "BC3"}   # R8 = tncimage's name for A8
CACHE_VERSION = 1   # bump when decoding changes, old files are then ignored
PLAIN_KEYS = ("transmap", "sparediffusemap", "texturemap", "diffusemap", "editormap")
SKY_KEYS = ("spare1map", "transmap")
TYPES = ("material", "image", "file")

_ID = re.compile(r"[0-9a-f]{16}\Z")
_states = {}
_lock = threading.Lock()


class TnoTexError(Exception):
    pass


def _squash(name):
    return re.sub(r"\s+", "", name)


def texture_id(image_name):
    return hashlib.sha1(b"tno-image:" + image_name.lower().encode("utf-8")).hexdigest()[:16]


def _pairs(root):
    base = Path(root) / "base"
    backup = base / "_wolfsdk_backup"
    out = []
    for i, r in load_master(base):
        b = (backup / i, backup / r)
        out.append(b if b[0].is_file() and b[1].is_file() else (base / i, base / r))
    return out


class _State:
    """All chunk pairs of one install, indexed for material/image/file."""

    def __init__(self, root):
        self.archives = [Archive(i, r) for i, r in _pairs(root)]
        self.read_lock = threading.Lock()      # Archive.read shares one file handle
        self.by = {}                           # (type, lower name) -> [(archive index, entry)]
        for ai, a in enumerate(self.archives):
            for e in a.entries:
                if e.type in TYPES:
                    self.by.setdefault((e.type, e.name.lower()), []).append((ai, e))
        self.squashed = {}                     # image name without blanks -> lower-case name
        self.ids = {}
        for (t, n), hits in self.by.items():
            if t == "image":
                self.squashed.setdefault(_squash(n), n)
                if texture_id(n) in self.ids:
                    raise TnoTexError("Texture id twice: %s / %s" % (self.ids[texture_id(n)], n))
                self.ids[texture_id(n)] = n
        self.chunk_of = {}
        for (t, n), hits in self.by.items():
            if t == "file" and n.startswith("maps/") and n.endswith(".entities"):
                self.chunk_of.setdefault(n[5:-9], hits[0][0])
        self.decls = {}

    def find(self, type_, name, chunks=None):
        key = name.lower().replace("\\", "/")
        hits = self.by.get((type_, key)) or (type_ == "image" and self.by.get(
            ("image", self.squashed.get(_squash(key))))) or []
        for ai in chunks or ():
            for h in hits:
                if h[0] == ai:
                    return h
        return None if chunks and type_ == "material" else (hits[0] if hits else None)

    def read(self, hit):
        with self.read_lock:
            return self.archives[hit[0]].read(hit[1])


def _st(root):
    key = str(Path(root).resolve()).lower()
    with _lock:
        st = _states.get(key)
        if st is None:
            st = _states[key] = _State(root)
    return st


def _chunks(st, map_id):
    ai = st.chunk_of.get(map_id.lower())
    if ai is None:
        raise KeyError("Unknown map: %s" % map_id)
    return (ai, 0)


def decl_fields(text):
    """First value of every `key value` line of a material decl, keys lower-case,
    quotes and // comments removed."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z_][\w.]*)\s+(.+?)\s*(?://.*)?$", line)
        if m:
            out.setdefault(m.group(1).lower(), m.group(2).strip().strip('"').strip())
    return out


def classify(root, map_id, material_name):
    """(kind, image entry name or None); kind is bim, mega, vmtr, none or
    missing (no decl in the map's mount). See the module doc."""
    st = _st(root)
    chunks = _chunks(st, map_id)
    hit = st.find("material", material_name, chunks)
    if hit is None:
        return "missing", None
    f = st.decls.get(id(hit[1]))
    if f is None:
        f = st.decls[id(hit[1])] = decl_fields(st.read(hit).decode("latin-1"))
    if "landdefinitionfile" in f:
        return "mega", None
    vmtr = "virtualmapping" in f or "usevirtualmapping" in f
    keys = ("diffusemap",) if vmtr else SKY_KEYS if f.get("stageprogram", "").lower().startswith("sky") \
        else PLAIN_KEYS
    for k in keys:
        v = f.get(k, "")
        img = st.find("image", v, chunks) if v and not v.startswith("_") else None
        if img is not None:
            return "bim", img[1].name
    return ("vmtr" if vmtr else "none"), None


def albedo_for_material(root, map_id, material_name):
    """Texture id of the material's colour map, or None (megatexture, virtual
    material without a shipped image, no colour slot, no decl)."""
    kind, name = classify(root, map_id, material_name)
    return texture_id(name) if kind == "bim" else None


# --------------------------------------------------------------------------
# BIM v9
# --------------------------------------------------------------------------

def mip_size(w, h, fmt):
    if fmt in ("BC1", "BC3"):
        return ((w + 3) // 4) * ((h + 3) // 4) * (8 if fmt == "BC1" else 16)
    return w * h * (4 if fmt == "RGBA8" else 1)


def parse_bim(data):
    """{type, fmt, w, h, mips: [(level, face, w, h, offset, size)]}. Strict:
    records level-major, sizes as the format demands, every byte used."""
    if len(data) < BIM_HEAD or data[4:8] != BIM_MAGIC:
        raise TnoTexError("not a BIM v9")
    typ, w, h, _depth, nmips, _f5, code = struct.unpack_from(">7I", data, 8)
    fmt = BIM_FORMATS.get(code)
    if fmt is None:
        raise TnoTexError("BIM format %d unknown" % code)
    faces = 6 if typ == 2 else 1
    pos, mips = BIM_HEAD, []
    while pos < len(data):
        if pos + 20 > len(data):
            raise TnoTexError("mip header cut off at %d" % pos)
        level, face, mw, mh, size = struct.unpack_from(">5I", data, pos)
        k = len(mips)
        if ((level, face) != (k // faces, k % faces) or (mw, mh) != (max(1, w >> level), max(1, h >> level))
                or size != mip_size(mw, mh, fmt) or pos + 20 + size > len(data)):
            raise TnoTexError("Mip %d does not fit (level %d, %dx%d, %d bytes)" % (k, level, mw, mh, size))
        mips.append((level, face, mw, mh, pos + 20, size))
        pos += 20 + size
    if len(mips) != nmips * faces:
        raise TnoTexError("%d mips instead of %d x %d" % (len(mips), nmips, faces))
    return {"type": typ, "fmt": fmt, "w": w, "h": h, "mips": mips}


def is_ycocg(image_name):
    return any(t.startswith("ycocg") for t in image_name.lower().split(" ")[:-1])


def ycocg_to_rgb(rgba):
    """YCoCg-DXT5 pixels (R=Co, G=Cg, B=(scale-1)*8, A=Y) -> RGB, alpha 255."""
    out = bytearray(rgba)
    for i in range(0, len(out), 4):
        s = (out[i + 2] >> 3) + 1
        co, cg, y = (out[i] - 128) / s, (out[i + 1] - 128) / s, out[i + 3]
        out[i] = min(255, max(0, round(y + co - cg)))
        out[i + 1] = min(255, max(0, round(y + cg)))
        out[i + 2] = min(255, max(0, round(y - co - cg)))
        out[i + 3] = 255
    return out


# --------------------------------------------------------------------------
# served textures
# --------------------------------------------------------------------------

def _image(root, tid):
    st = _st(root)
    name = st.ids.get(tid) if isinstance(tid, str) and _ID.match(tid) else None
    if name is None:
        raise KeyError("Unknown texture: %r" % (tid,))
    data = st.read(st.find("image", name))
    return name, data, parse_bim(data)


def _cached(name, make):
    """Bytes of the cache file `name`, made (and stored) on a miss."""
    path = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "wolfsdk" / "mapcache" / "tno" / "tex" / name
    try:
        return path.read_bytes()
    except OSError:
        pass
    data = make()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".%d.tmp" % threading.get_ident())
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        pass   # a read-only or full cache only costs speed
    return data


def texture_info(root, tid):
    """{id, name, format, w, h, srgb} of the full image; format is what
    texture_block serves for a mip that fits (A8 and YCoCg: RGBA8)."""
    name, _data, bim = _image(root, tid)
    fmt = "RGBA8" if bim["fmt"] == "R8" or is_ycocg(name) else bim["fmt"]
    return {"id": tid, "name": name, "format": fmt, "w": bim["w"], "h": bim["h"], "srgb": True}


def _mip(root, tid, max_side):
    """(served format, w, h, raw) of the mip texture_block serves."""
    name, data, bim = _image(root, tid)
    fmt, ycocg = bim["fmt"], is_ycocg(name)
    chain = [m for m in bim["mips"] if m[1] == 0]
    fit = next((m for m in chain if max(m[2], m[3]) <= max_side), None)
    if fit and not ycocg and (fmt == "RGBA8" or fmt in ("BC1", "BC3") and fit[2] % 4 == 0 == fit[3] % 4):
        return fmt, fit[2], fit[3], data[fit[4]:fit[4] + fit[5]]
    m = fit or chain[-1]
    step = max(1, -(-max(m[2], m[3]) // max(1, max_side)))

    def make():
        _w, _h, rgba = tncimage.decode(fmt, data[m[4]:m[4] + m[5]], m[2], m[3], step)
        return bytes(ycocg_to_rgb(rgba) if ycocg else rgba)

    rgba = _cached("%s_%08x_%d_v%d.rgba" % (tid, zlib.crc32(data), max_side, CACHE_VERSION), make)
    return "RGBA8", len(range(0, m[2], step)), len(range(0, m[3], step)), rgba


def texture_block(root, tid, max_side=1024):
    """WTX1 header + one mip. KeyError for an unknown id, TnoTexError for a
    broken image."""
    fmt, w, h, raw = _mip(root, tid, max_side)
    return WTX_HEADER.pack(b"WTX1", WTX_CODE[fmt], 1, w, h, len(raw)) + bytes(raw)


def wtx_png(block):
    """PNG of a WTX1 block (tncimage's decoders)."""
    magic, code, _flags, w, h, n = WTX_HEADER.unpack_from(block)
    fmt = {v: k for k, v in WTX_CODE.items()}.get(code)
    if magic != b"WTX1" or fmt is None or len(block) != WTX_HEADER.size + n:
        raise TnoTexError("not a valid WTX1 block")
    w, h, rgba = tncimage.decode(fmt, block[WTX_HEADER.size:], w, h)
    return image.to_png(w, h, rgba)


def texture_png(root, tid, max_side=512):
    """PNG of the mip texture_block(root, tid, max_side) serves, cached."""
    _name, data, _bim = _image(root, tid)
    return _cached("%s_%08x_%d_v%d.png" % (tid, zlib.crc32(data), max_side, CACHE_VERSION),
                   lambda: wtx_png(texture_block(root, tid, max_side)))
