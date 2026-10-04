"""Textures for the 3D map viewer: material -> albedo image -> one mip.

    albedo_for_material(mount, name)         -> texture id | None
    texture_info(mount, tid)                 -> {id, name, format, w, h, srgb}
    texture_block(mount, tid, max_side=1024) -> WTX1 bytes (raw BC blocks)
    texture_png(mount, tid, max_side=512)    -> PNG of the same mip

`mount` is a wolfsdk.mapcatalog.Mount. A texture id is the image entry's
+0x60 field as 16 lower-case hex digits. That field is the engine's texture
identity: the .texdb key is built from it, and across every retail archive
(30 700 values over 38 959 image/decalatlas entries) one value never stands
for two different payloads -- identical images share it, so the id is
content-addressed, stable and URL-safe.

Albedo is the colour map the viewer shows, first hit of:
  loosealbedo      standard PBR materials (7 938 of 11 469 retail)
  sparediffusemap  glass
  transmap         programme materials: emissive, sky, clouds, blinking lights
  texturemap       stageprogram textureonly (flat colours)
  decaldiffusemap  decals; names a `decalatlas` entry, not an `image`
Materials with none of these get None: editordraw (clip, triggers -- the
game does not draw them) and watervolume (no texture at all).

WTX1 (little endian): b"WTX1", u16 format (1 BC1, 3 BC3, 4 BC4, 5 BC5, 7 BC7,
0 RGBA8), u16 flags (bit0 sRGB), u16 width, u16 height, u32 length, then
one mip of raw block data. The mip is the largest one <= max_side whose edges
are multiples of 4 (WebGL uploads it as level 0) and that the mounted .texdb
files actually hold; top mips the build never shipped and tile chains
(codec 2, not understood) are skipped the way tncimage.to_png does.
Single-mip images have no such mip (3000x3000 screens, 2048x682 graphics,
an 8192x514 sky horizon): their mip is decoded in Python, every n-th pixel
kept to fit max_side, and served as RGBA8 -- the only case that does pixel
work, so it is cached like the PNGs.

BIM parsing, .texdb lookup and the pure-Python BC decoders are
wolfsdk/tncimage.py's. PNGs are cached under
%LOCALAPPDATA%\\wolfsdk\\mapcache\\tex\\, keyed by id + data hash, so a
changed image gets a new file. BC blocks are not cached: they are a read and
a Kraken pass (Oodle DLL), no pixel work in Python.
"""

import os
import re
import struct
import threading
import weakref
from pathlib import Path

from . import image, oodle, tncimage
from .tncimage import TncImageError

WTX_MAGIC = b"WTX1"
WTX_HEADER = struct.Struct("<4sHHHHI")
WTX_FORMATS = {"RGBA8": 0, "BC1": 1, "BC3": 3, "BC4": 4, "BC5": 5, "BC7": 7}
BLOCK_BYTES = {"BC1": 8, "BC4": 8, "BC3": 16, "BC5": 16, "BC7": 16}
ALBEDO_KEYS = (("loosealbedo", "image"), ("sparediffusemap", "image"), ("transmap", "image"),
               ("texturemap", "image"), ("decaldiffusemap", "decalatlas"),
               ("watersurfacealbedo", "image"))
CACHE = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "wolfsdk" / "mapcache" / "tex"
CACHE_VERSION = 1   # bump when decoding changes, old files are then ignored

_ID = re.compile(r"[0-9a-f]{16}\Z")
_state = weakref.WeakKeyDictionary()   # Mount -> (id -> (archive, entry), TexdbSet)
_lock = threading.Lock()


def _st(mount):
    with _lock:
        st = _state.get(mount)
        if st is None:
            ids = {}
            for t in ("image", "decalatlas"):
                for a, e in mount.entries(t):
                    ids.setdefault("%016x" % e.hash_0x60, (a, e))
            texdbs = tncimage.TexdbSet(mount.texdbs)
            texdbs.root = mount.game_root
            st = _state[mount] = (ids, texdbs)
    return st


def _hit(mount, texture_id):
    hit = _st(mount)[0].get(texture_id) if isinstance(texture_id, str) and _ID.match(texture_id) else None
    if hit is None:
        raise KeyError("Unknown texture: %r" % (texture_id,))
    return hit


def _cached(name, make):
    """Bytes of CACHE/name, made (and stored) on a miss."""
    path = CACHE / name
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


def albedo_for_material(mount, material_name):
    """Texture id of the material's colour map, or None (no material, no
    colour slot, or the image is in no mounted archive)."""
    hit = mount.find("material", material_name)
    if hit is None:
        return None
    slots = dict(line.split("\t", 1) for line in
                 mount.read(*hit).decode("utf-8", "replace").splitlines() if "\t" in line)
    for key, type_ in ALBEDO_KEYS:
        img = mount.find(type_, slots.get(key, "").strip().strip('"'))
        if img is not None:
            return "%016x" % img[1].hash_0x60
    return None


def _format(bim):
    fmt = bim["format"]
    base = fmt.replace("_SRGB", "")
    if base not in WTX_FORMATS and base != "R8":
        raise TncImageError("%dx%d %s: this format is not served"
                            % (bim["width"], bim["height"], fmt))
    return base, fmt.endswith("_SRGB")


def texture_info(mount, texture_id):
    """{id, name, format, w, h, srgb} of the full image (for scene.json).
    The format is the stored one; texture_block may serve RGBA8 instead
    (R8, single-mip images over max_side)."""
    a, e = _hit(mount, texture_id)
    bim = tncimage.parse_bimage(mount.read(a, e))
    base, srgb = _format(bim)
    return {"id": texture_id, "name": e.name, "format": base,
            "w": bim["width"], "h": bim["height"], "srgb": srgb}


def _level(mount, texdbs, entry, data, bim, m):
    """Unpacked bytes of one mip record, or None if no mounted .texdb holds it.
    Raises when a block's size disagrees with the record."""
    if m["streamed"]:
        key = tncimage.texdb_key(entry.hash_0x60, bim["mip_base"], m["level"])
        block = texdbs.lookup(key, m["stored_size"])
        if block is None:
            return None
        where = "TexDB block %016x" % key
    else:
        block = data[m["offset"]:m["offset"] + m["stored_size"]]
        where = "embedded mip"
    if len(block) != m["stored_size"]:
        raise TncImageError("%s has %d bytes, mip %d expects %d"
                            % (where, len(block), m["level"], m["stored_size"]))
    if m["codec"] == tncimage.CODEC_OODLE:
        try:
            block = oodle.load(mount.game_root).decompress(block, m["raw_size"], fuzz_safe=True)
        except oodle.OodleError as exc:
            raise TncImageError("Mip %d cannot be unpacked: %s" % (m["level"], exc))
    if len(block) != m["raw_size"]:
        raise TncImageError("Mip %d unpacks to %d bytes, expected %d"
                            % (m["level"], len(block), m["raw_size"]))
    return block


def _mip(mount, texture_id, max_side):
    """(served format, srgb, w, h, raw) of the mip texture_block serves."""
    a, e = _hit(mount, texture_id)
    data = mount.read(a, e)
    bim = tncimage.parse_bimage(data)
    base, srgb = _format(bim)
    chain = [m for m in bim["mips"] if m["face"] == 0 and m["codec"] != tncimage.CODEC_TILED]
    texdbs = _st(mount)[1]

    def unpacked(m):
        raw = _level(mount, texdbs, e, data, bim, m)
        w, h = m["width"], m["height"]
        size = (((w + 3) // 4) * ((h + 3) // 4) * BLOCK_BYTES[base] if base in BLOCK_BYTES
                else w * h * (1 if base == "R8" else 4))
        if raw is not None and len(raw) != size:
            raise TncImageError("Mip %d %dx%d %s has %d bytes, expected %d"
                                % (m["level"], w, h, base, len(raw), size))
        return raw

    for m in chain:
        w, h = m["width"], m["height"]
        if max(w, h) > max_side or base in BLOCK_BYTES and (w % 4 or h % 4):
            continue
        raw = unpacked(m)
        if raw is None:
            continue
        if base == "R8":
            base, raw = "RGBA8", tncimage.decode("R8", raw, w, h)[2]
        return base, srgb, w, h, bytes(raw)
    # nothing servable as blocks: the largest mip there is, every step-th pixel, RGBA8
    for m in chain:
        raw = unpacked(m)
        if raw is None:
            continue
        w, h = m["width"], m["height"]
        step = max(1, -(-max(w, h) // max(1, max_side)))
        rgba = _cached("%s_%016x_%d_v%d.rgba" % (texture_id, e.data_hash, max_side, CACHE_VERSION),
                       lambda: bytes(tncimage.decode(base, raw, w, h, step)[2]))
        return "RGBA8", srgb, len(range(0, w, step)), len(range(0, h, step)), rgba
    raise TncImageError("%s: no mip level in the mounted archives" % e.name)


def texture_block(mount, texture_id, max_side=1024):
    """WTX1 header + one mip of raw block data. KeyError for an unknown id,
    TncImageError when the image has no servable mip."""
    base, srgb, w, h, raw = _mip(mount, texture_id, max_side)
    return WTX_HEADER.pack(WTX_MAGIC, WTX_FORMATS[base], int(srgb), w, h, len(raw)) + raw


def wtx_png(block):
    """PNG of a WTX1 block, decoded in pure Python (tncimage's decoders)."""
    magic, code, _flags, w, h, n = WTX_HEADER.unpack_from(block)
    fmt = {v: k for k, v in WTX_FORMATS.items()}.get(code)
    if magic != WTX_MAGIC or fmt is None or len(block) != WTX_HEADER.size + n:
        raise TncImageError("not a valid WTX1 block")
    w, h, rgba = tncimage.decode(fmt, block[WTX_HEADER.size:], w, h)
    return image.to_png(w, h, rgba)


def texture_png(mount, texture_id, max_side=512):
    """PNG of the mip texture_block(mount, texture_id, max_side) serves, cached."""
    _a, e = _hit(mount, texture_id)
    return _cached("%s_%016x_%d_v%d.png" % (texture_id, e.data_hash, max_side, CACHE_VERSION),
                   lambda: wtx_png(texture_block(mount, texture_id, max_side)))
