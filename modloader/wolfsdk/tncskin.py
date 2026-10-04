"""Recolour a Wolfenstein II image in place: same format, size and mips.

Every mip of a BC1 image is decoded block by block, run through a colour
transform and encoded again (wolfsdk/bcenc.py). The result is two things a mod
carries as `file` assets, and tncpatch writes them where the originals sit:

  * the .bimage payload -- header and mip table unchanged except for the
    stored_size of each streamed mip, the inline mips (<= 32 px) re-encoded
    in place, so the payload keeps its length byte for byte;
  * one block per streamed mip, under the image's own texdb key
    ((hash_0x60 & 0x0FFF_FFFF_FFFF_FFFF) << 4 | mip_base - level). hash_0x60
    is not touched, so the key stays and the new block goes into the old
    block's place.

Why it fits: same format and size means the raw BC data of every mip is
exactly as long as before, so an inline mip overwrites its own bytes. A
streamed mip is Kraken at level Optimal1 -- recompressing the retail raw data
reproduces the retail block size byte for byte -- stepping up to Optimal3/4,
and for a mip that still misses, blend(): retail index bits with mapped
endpoints where that costs least, full re-encodes where it fits. The new
stream has to come out no longer than the old block, or recolour() refuses.
The engine reads stored_size bytes (texdbkey.md, .text 0x7A1C7B), so a
shorter block plus zero fill is read exactly; the texdb table and every
offset stay.

The gold transform works in the stored (sRGB-encoded) space the decoder
hands out: luma -> gold of the same luma, so dark stays dark and every
scratch keeps its contrast; `gain` > 1 brightens it (the specular map, which
is what makes metal look like gold in id Tech 6's spec/gloss model), capped
where the brightest channel reaches 255 so the hue never shifts; strongly red
pixels (blood on the *_blood variants) keep their colour.

A skin from the user's own PNG (replace(), build_custom_mod()) is the other
way round: every mip is made from the picture at full quality -- mip_chain()
at each mip's size, area-averaged below the source size -- and packed at
whatever length that takes. The retail blocks of the nearly black pistol
pack to 15-36 % and are far too small for that, so tncpatch appends such a
block to the .texdb and repoints its row (see tncpatch's module head); the
new inline mips make the .bimage pack worse, which costs patch_2.resources
(39 MB) a rebuild.

    python -m wolfsdk.tncskin           # builds mods/tnc_skin_gold_pistol
    python -m wolfsdk.tncskin --bild x.png   # builds mods/tnc_skin_x
"""

import functools
import json
import re
import struct
import sys
import unicodedata
import zlib
from array import array
from pathlib import Path

from . import bcenc, oodle, tncimage, tncview
from .tncimage import BIM_HEADER, BIM_MIP, CODEC_OODLE, CODEC_RAW, TncImageError
from .tncpatch import TEXDB, Installation

GOLD = (247, 219, 148)   # textures/system/specular/gold, the retail gold (probe_skins)
# OodleLZ Optimal1 reproduces the retail block sizes exactly; Optimal3/4 are
# tried only when a mip does not fit that way (same Kraken stream format).
LEVELS = (5, 7, 8)


def gold(gain=1.0, keep_red=True, tint=GOLD):
    """A colour transform: RGBA bytes (any multiple of 4) -> RGBA bytes."""
    lum_t = 0.2126 * tint[0] + 0.7152 * tint[1] + 0.0722 * tint[2]
    cap = lum_t * 255 / max(tint)
    table = {}

    def pixel(p):
        r, g, b, a = p & 255, (p >> 8) & 255, (p >> 16) & 255, p >> 24
        s = min((0.2126 * r + 0.7152 * g + 0.0722 * b) * gain, cap) / lum_t
        out = [c * s for c in tint]
        if keep_red and r:
            # blood is >= 0.6 red, the orange accents stay below 0.45
            w = min(1.0, max(0.0, ((r - max(g, b)) / r - 0.45) / 0.25))
            out = [o + w * (c - o) for o, c in zip(out, (r, g, b))]
        r, g, b = (min(255, int(o + 0.5)) for o in out)
        return r | g << 8 | b << 16 | a << 24

    def fn(rgba):
        px = array("I", bytes(rgba))
        for p in set(px).difference(table):
            table[p] = pixel(p)
        return array("I", map(table.__getitem__, px)).tobytes()

    return fn


def transcode(fmt, raw, fn):
    """BC1 blocks -> decoded, transformed, encoded again; same length."""
    if fmt.replace("_SRGB", "") != "BC1":
        raise TncImageError("%s is not recolored (BC1 only)" % fmt)
    out = bytearray(len(raw))
    done = {}
    for i in range(0, len(raw), 8):
        blk = raw[i:i + 8]
        new = done.get(blk)
        if new is None:
            new = done[blk] = bcenc.encode_block(fn(tncimage._bc1(blk)))
        out[i:i + 8] = new
    return bytes(out)


def blend(raw, fn, room, oo):
    """(stream, share of mapped blocks) for a mip whose full re-encode misses `room`.

    Every block starts as bcenc.map_block -- retail indices, mapped endpoints,
    compresses like retail -- and the blocks where that errs most against
    the transformed pixels are re-encoded in full, as many as still fit
    (binary search on the count). Blocks, not pixels: the size of a Kraken
    stream is not additive, so the count is measured, not computed.
    """
    n = len(raw) // 8
    full, mapped, gain, seen = [], [], [], {}
    for i in range(n):
        blk = raw[8 * i:8 * i + 8]
        if blk not in seen:
            px = fn(tncimage._bc1(blk))
            a, b = bcenc.encode_block(px), bcenc.map_block(blk, fn)
            seen[blk] = (a, b, bcenc.block_error(px, b) - bcenc.block_error(px, a))
        a, b, g = seen[blk]
        full.append(a)
        mapped.append(b)
        gain.append(g)
    order = sorted(range(n), key=gain.__getitem__, reverse=True)
    best, lo, hi = None, 0, n
    while lo <= hi:
        k = (lo + hi) // 2
        use = set(order[:k])
        packed = _squeeze(b"".join(full[i] if i in use else mapped[i] for i in range(n)), room, oo)
        if packed is None:
            hi = k - 1
        else:
            best, lo = (packed, 1 - k / n), k + 1
    return best


def _squeeze(out, room, oo):
    """Kraken at the lowest level whose stream fits `room`, or None."""
    for level in LEVELS:
        packed = oo.compress(out, oodle.KRAKEN, level)
        if len(packed) <= room:
            return packed
    return None


def mip_blocks(entry, data, texdbs):
    """[(mip, stored bytes, texdb key or None)] for every mip of face 0."""
    bim = tncimage.parse_bimage(data)
    if bim["faces"] != 1:
        raise TncImageError("cube maps are not recolored")
    out = []
    for m in bim["mips"]:
        if m["streamed"]:
            key = tncimage.texdb_key(entry.hash_0x60, bim["mip_base"], m["level"])
            block = texdbs.lookup(key)
            if block is None or len(block) != m["stored_size"]:
                raise TncImageError("mip %d: TexDB block %016x is missing or not in its "
                                    "original state" % (m["level"], key))
        else:
            key = None
            block = data[m["offset"]:m["offset"] + m["stored_size"]]
        out.append((m, bytes(block), key))
    return bim, out


def recolour(entry, data, texdbs, fn, oo, notes=None):
    """(new .bimage payload, {texdb key: new block}) for one image entry.

    `data` is the entry's decompressed payload, `texdbs` a tncimage.TexdbSet
    over the pristine files. Raises before returning anything that would not
    fit its place. `notes` collects the mips that needed blend() and how much
    of each was mapped rather than re-encoded.
    """
    bim, mips = mip_blocks(entry, data, texdbs)
    fmt = bim["format"]
    new = bytearray(data)
    blocks = {}
    for i, (m, block, key) in enumerate(mips):
        if m["codec"] == CODEC_RAW:
            raw = block
        elif m["codec"] == CODEC_OODLE:
            raw = oo.decompress(block, m["raw_size"], fuzz_safe=True)
        else:
            raise TncImageError("mip %d is a tile chain (codec %d)" % (m["level"], m["codec"]))
        out = transcode(fmt, raw, fn)
        if m["codec"] == CODEC_OODLE:
            packed = _squeeze(out, len(block), oo)
            if packed is None:
                packed, share = blend(raw, fn, len(block), oo) or (None, 1)
                if notes is not None:
                    notes.append("mip %d %d%% mapped" % (m["level"], round(100 * share)))
            if packed is None:
                raise TncImageError("mip %d does not fit into %d bytes in any version" % (m["level"], len(block)))
            out = packed
        if len(out) > len(block) or (key is None and len(out) != len(block)):
            raise TncImageError("mip %d: %d bytes do not fit into the space of %d"
                                % (m["level"], len(out), len(block)))
        if key is None:
            new[m["offset"]:m["offset"] + len(out)] = out
        else:
            blocks[key] = out
            struct.pack_into("<I", new, BIM_HEADER + BIM_MIP * i + 24, len(out))
    return bytes(new), blocks


# -- the gold pistol ---------------------------------------------------------

MOD_ID = "wolfsdk.tnc_skin_gold_pistol"
PISTOL = "models/weapons/pistole61/textures/"
ALBEDO_GAIN, SPEC_GAIN = 1.0, 1.8
GOLD_PISTOL = [PISTOL + n + kind
               for n in ("pistole_61", "pistole_61_blood", "silencer_pistole_61",
                         "regular_magazine_pistole_61", "pistole_extendedmag_01",
                         "pistole_61_pickup_01")
               for kind in (".tga$mtlkind=albedo$streamed", "_s.tga$mtlkind=specular$streamed")]


def transform_for(name):
    return gold(SPEC_GAIN if "$mtlkind=specular" in name else ALBEDO_GAIN)


def asset_file(name):
    return "assets/" + name.split("/")[-1].split(".")[0] + ".bimage"


def build_mod(game_root, out_dir, names=GOLD_PISTOL):
    """Write mod.json and the assets for `names` into `out_dir`.

    Reads the installation the way tncpatch does and refuses while an
    applied mod journal names any image it would read: then the bytes on
    disk are not the originals.
    """
    root = Path(game_root)
    inst = Installation(root / "base")
    journal = inst.read_journal() or {}
    touched = [n for n in names if "image:" + n in journal.get("assets", ())]
    if touched or any(a.startswith(TEXDB + ":") for a in journal.get("assets", ())):
        raise TncImageError("The installation currently carries image mods (%s). "
                            "Revert first, then build." % ", ".join(journal.get("mods", ())))
    # tncpatch writes base/ only, so a key some DLC .texdb also carries would
    # stay retail there -- and the engine searches every mounted .texdb.
    texdbs = tncimage.TexdbSet(sorted((root / "base").glob("*.texdb")))
    dlc = tncimage.TexdbSet(sorted(root.glob("dlc/*/base/*.texdb")))
    oo = oodle.load(root)
    out = Path(out_dir)
    (out / "assets").mkdir(parents=True, exist_ok=True)
    (out / "texdb").mkdir(exist_ok=True)
    assets = {}
    try:
        for name in names:
            hits = inst.find("image", name)
            copies = {tncview.payload(a, e, oo) for a, e in hits}
            if len(copies) != 1 or len({e.hash_0x60 for _a, e in hits}) != 1:
                raise TncImageError("%s: the copies are not identical (%d versions)"
                                    % (name, len(copies)))
            entry, data = hits[0][1], copies.pop()
            kept = []
            new, blocks = recolour(entry, data, texdbs, transform_for(name), oo, kept)
            shared = [k for k in blocks if dlc.lookup(k) is not None]
            if shared:
                raise TncImageError("%s: TexDB key also in a DLC TexDB: %s"
                                    % (name, ", ".join("%016x" % k for k in shared)))
            (out / asset_file(name)).write_bytes(new)
            assets["image:" + name] = {"file": asset_file(name)}
            for key, block in blocks.items():
                rel = "texdb/%016x.bin" % key
                (out / rel).write_bytes(block)
                assets["%s:%016x" % (TEXDB, key)] = {"file": rel}
            print("  %s: %d TexDB blocks%s" % (name.split("/")[-1], len(blocks),
                  ", " + ", ".join(kept) if kept else ""), flush=True)
    finally:
        inst.close()
    manifest = {
        "id": MOD_ID,
        "name": "Golden Pistol",
        "version": "1.0.0",
        "author": "wolfsdk",
        "description": ("The standard pistol (campaign and Agent Silent Death) in gold, with real "
                        "textures: albedo and gloss map of the body, blood variant, silencer, both "
                        "magazines and the pickup weapon recolored, every mip level, same format and "
                        "same size. Scratches and details stay, blood stays red. Pistols lying around "
                        "and the ones enemies carry are golden too. Do not enable together with 'Golden "
                        "Pistol (Simple)' - that one swaps the materials and hides these textures. "
                        "Checked structurally, not yet confirmed in game."),
        "priority": 100,
        "spiel": "tnc",
        "assets": assets,
    }
    (out / "mod.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return manifest


# -- a skin from the user's own PNG ------------------------------------------

def read_png(path, alpha=False):
    """(w, h, RGBA bytes) of an 8-bit non-interlaced PNG (grey, RGB, grey+alpha, RGBA).

    `path` may be the PNG's bytes. Alpha is 255 unless `alpha` (BC1 stores none)."""
    data = bytes(path) if isinstance(path, (bytes, bytearray, memoryview)) else Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise TncImageError("%s is not a PNG" % ("The image" if isinstance(path, (bytes, bytearray, memoryview)) else path))
    pos, idat, head = 8, [], None
    while pos + 8 <= len(data):
        n, kind = struct.unpack_from(">I4s", data, pos)
        chunk = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            head = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat.append(chunk)
        elif kind == b"IEND":
            break
    w, h, depth, ctype, _c, _f, interlace = head
    bpp = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
    if depth != 8 or interlace or bpp is None:
        raise TncImageError("Please save the PNG as 8-bit RGB/RGBA without interlacing")
    stride = w * bpp
    # bounded: a small IDAT must not inflate past the picture it claims to be
    raw = zlib.decompressobj().decompress(b"".join(idat), h * (stride + 1))
    prev, rows = bytearray(stride), []
    for y in range(h):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line)
        prev = line
    out = bytearray(w * h * 4)
    out[3::4] = b"\xff" * (w * h)                  # opaque: BC1 albedo carries no alpha
    for y, line in enumerate(rows):
        o = y * w * 4
        for c in range(3):
            out[o + c:o + w * 4:4] = line[(c if bpp >= 3 else 0)::bpp]
        if alpha and bpp in (2, 4):
            out[o + 3:o + w * 4:4] = line[bpp - 1::bpp]
    return w, h, bytes(out)


def resize(w, h, px, W, H):
    """Bilinear resample of RGBA bytes to W x H (pixel centres aligned)."""
    def taps(n, N):
        out = []
        for i in range(N):
            f = min(max((i + 0.5) * n / N - 0.5, 0.0), n - 1.0)
            i0 = int(f)
            out.append((i0, min(i0 + 1, n - 1), f - i0))
        return out
    xs = [(a * 4, b * 4, t) for a, b, t in taps(w, W)]
    out = bytearray(W * H * 4)
    o = 0
    for y0, y1, u in taps(h, H):
        r0, r1 = px[y0 * w * 4:(y0 + 1) * w * 4], px[y1 * w * 4:(y1 + 1) * w * 4]
        row = [a + (b - a) * u for a, b in zip(r0, r1)]
        for a, b, t in xs:
            out[o] = int(row[a] + (row[b] - row[a]) * t + 0.5)
            out[o + 1] = int(row[a + 1] + (row[b + 1] - row[a + 1]) * t + 0.5)
            out[o + 2] = int(row[a + 2] + (row[b + 2] - row[a + 2]) * t + 0.5)
            out[o + 3] = int(row[a + 3] + (row[b + 3] - row[a + 3]) * t + 0.5)
            o += 4
    return bytes(out)


def halve(w, h, px, W, H):
    """RGBA w x h -> W x H, each side halved or kept: every pixel the mean of its 2 x 2 (or 2 x 1)."""
    fx, fy = w // W, h // H
    n, out = fx * fy, bytearray()
    for y in range(H):
        rows = [px[(y * fy + r) * w * 4:(y * fy + r + 1) * w * 4] for r in range(fy)]
        v = list(map(sum, zip(*rows)))
        if fx == 2:
            v = [v[8 * x + c] + v[8 * x + 4 + c] for x in range(W) for c in range(4)]
        out += bytes((t + n // 2) // n for t in v)
    return bytes(out)


@functools.lru_cache(maxsize=1)
def mip_chain(src, sizes):
    """RGBA of every mip in `sizes` ((w, h), ...), made from `src` = (w, h, RGBA).

    A mip at least as large as the source is resampled from it (bilinear).
    Every smaller one is the exact 2 x 2 mean of the mip above it, so it
    averages its whole area instead of sampling a few pixels: bilinear
    straight from a 1254 px source reads 2 x 2 of ~20 x 20 pixels at 64 px
    and aliases (measured 15-17 dB against an area mean at 4..64 px). The
    first mip below the source is thereby the mean of a bilinear upsample at
    twice its size -- an area average of the source.
    """
    w, h, px = src
    out = []
    for i, (mw, mh) in enumerate(sizes):
        if i and (mw < w or mh < h):
            out.append(halve(*sizes[i - 1], out[-1], mw, mh))
        else:
            # ponytail: mip 0 from a source over 2x its size is bilinear too and
            # aliases; box it down first if anyone paints at 8K+.
            out.append(resize(w, h, px, mw, mh))
    return out


@functools.lru_cache(maxsize=1)
def bc1_chain(src, sizes):
    """BC1 bytes of every mip_chain() level. Cached: body and blood variant share it."""
    return [bcenc.encode("BC1", px, w, h) for (w, h), px in zip(sizes, mip_chain(src, sizes))]


def replace(entry, data, texdbs, src, oo):
    """(new .bimage payload, {texdb key: block}) with every mip made from `src`.

    `src` = (w, h, RGBA). Every mip at full quality: mip_chain() at the mip's
    own size, BC1-encoded. A streamed mip is packed with Kraken (Optimal1)
    at whatever length that takes and its stored_size set to it -- tncpatch
    appends a block longer than its old place to the .texdb and repoints the
    row. An inline mip (raw BC1) replaces its old bytes, same length.
    """
    bim, mips = mip_blocks(entry, data, texdbs)
    if bim["format"].replace("_SRGB", "") != "BC1":
        raise TncImageError("%s: only BC1 is replaced" % bim["format"])
    sizes = tuple((m["width"], m["height"]) for m, _b, _k in mips)
    new, blocks = bytearray(data), {}
    for i, ((m, block, key), raw) in enumerate(zip(mips, bc1_chain(src, sizes))):
        if len(raw) != m["raw_size"]:
            raise TncImageError("mip %d: %d bytes of BC1 instead of %d" % (m["level"], len(raw), m["raw_size"]))
        if m["codec"] == CODEC_RAW:
            out = raw
        elif m["codec"] == CODEC_OODLE:
            out = oo.compress(raw, oodle.KRAKEN, LEVELS[0])
        else:
            raise TncImageError("mip %d is a tile chain (codec %d)" % (m["level"], m["codec"]))
        # Inline: the payload keeps its layout. A streamed block may come out
        # 2 bytes per 256 KB chunk longer than raw (Kraken's stored chunk) --
        # retail ships that in 1459 mips of just four archives.
        if key is None and len(out) != len(block):
            raise TncImageError("mip %d: %d bytes do not fit into %d" % (m["level"], len(out), len(block)))
        if key is None:
            new[m["offset"]:m["offset"] + len(out)] = out
        else:
            blocks[key] = out
            struct.pack_into("<I", new, BIM_HEADER + BIM_MIP * i + 24, len(out))
        print("    mip %d %dx%d: %d bytes (old space %d)" % (m["level"], m["width"], m["height"],
              len(out), len(block)), flush=True)
    return bytes(new), blocks


# -- any BC1 image from a picture (the Studio's 'Replace') ---------------------
#
# replace() above needs the old .texdb blocks (mip_blocks) and so refuses an
# installation whose skin mod is applied, and every image whose top mips the
# build never shipped. replace_any() reads neither: it takes the mip table and
# hash_0x60 of the .bimage and makes every mip it writes from the picture, so a
# modded install gives the same bytes as a pristine one. Which streamed mips it
# writes is `keep`: the keys some base .texdb or map cache carries
# (check_image). A top level no file carries keeps its record and gets no
# block: a texdb asset for it would fail the whole mod ("existiert nicht").
# The refusal texts are English: the Studio shows them.

def replace_any(entry, data, src, oo, keep):
    """(new .bimage payload, {texdb key: block}): the mips of `data` that are
    written (inline ones, streamed ones whose key is in `keep`) made from
    `src` = (w, h, RGBA) as replace() makes them; nothing else changes."""
    bim = tncimage.parse_bimage(data)
    if bim["faces"] != 1 or bim["format"].replace("_SRGB", "") != "BC1":
        raise TncImageError("%s with %d face(s): only single BC1 images are written" % (bim["format"], bim["faces"]))
    todo = []
    for i, m in enumerate(bim["mips"]):
        if m["codec"] not in (CODEC_RAW, CODEC_OODLE):
            raise TncImageError("mip %d is a tile chain (codec %d)" % (m["level"], m["codec"]))
        key = tncimage.texdb_key(entry.hash_0x60, bim["mip_base"], m["level"]) if m["streamed"] else None
        if key is None or key in keep:
            todo.append((i, m, key))
    sizes = tuple((m["width"], m["height"]) for _i, m, _k in todo)
    new, blocks = bytearray(data), {}
    for (i, m, key), raw in zip(todo, bc1_chain(src, sizes)):
        if len(raw) != m["raw_size"]:
            raise TncImageError("mip %d: %d bytes of BC1 instead of %d" % (m["level"], len(raw), m["raw_size"]))
        out = raw if m["codec"] == CODEC_RAW else oo.compress(raw, oodle.KRAKEN, LEVELS[0])
        if key is None:
            if len(out) != m["stored_size"]:
                raise TncImageError("mip %d: %d bytes do not fit its %d" % (m["level"], len(out), m["stored_size"]))
            new[m["offset"]:m["offset"] + len(out)] = out
        else:
            blocks[key] = out
            struct.pack_into("<I", new, BIM_HEADER + BIM_MIP * i + 24, len(out))
    return bytes(new), blocks


def check_image(inst, name, oo, blood=True, normal=False):
    """What replacing image `name` (tncpatch.Installation `inst`, base/) writes,
    or TncImageError saying why it cannot be replaced.

    {"name", "width", "height", "format", "mips": written levels, "keep": set of
    texdb keys, "group": every base name sharing its data (hash_0x60, so its
    texdb keys) -- each gets the new .bimage, "blood": [check_image of each
    *_blood sibling of the same size and format], "blood_skipped": [why not]}.
    `normal`: a BC5 normal map instead of a BC1 texture; its tile-chain mips
    are fine, tncimage.normal_map() writes them as plain blocks. None: whichever
    of the two the image is.
    """
    dlc = tncview.dlc_archives(inst.base)
    try:
        hits = inst.find("image", name)
        if not hits:
            if any(a.find("image", name) for a in dlc):
                raise TncImageError("This texture ships only in a DLC archive; the loader writes base/ only.")
            raise TncImageError("Texture %s is not in base/." % name)
        copies = {tncview.payload(a, e, oo) for a, e in hits}
        if len(copies) != 1 or len({e.hash_0x60 for _a, e in hits}) != 1:
            raise TncImageError("Its %d copies in base/ differ; which one the game loads is unknown." % len(hits))
        entry, data = hits[0][1], copies.pop()
        if data[:3] == b"BIM" and data[3:4] != tncimage.BIM_MAGIC[3:]:
            raise TncImageError("BIM version %d (an HDR light probe or older image): not writable." % data[3])
        bim = tncimage.parse_bimage(data)
        fmt = bim["format"]
        if normal is None:
            normal = fmt == "BC5"
        if bim["faces"] != 1:
            raise TncImageError("A cube map (%d faces, %s): not writable yet." % (bim["faces"], fmt))
        if normal and fmt != "BC5":
            raise TncImageError("%s is not a BC5 normal map." % fmt)
        if not normal and fmt.replace("_SRGB", "") != "BC1":
            why = {"BC6H": "HDR", "BC5": "a normal map", "BC4": "a single-channel map", "BC7": "BC7 (UI atlas or sky)"}
            raise TncImageError("%s is %s: only BC1 textures (colour and specular maps) can be replaced so far."
                                % (fmt, why.get(fmt.replace("_SRGB", ""), "not BC1")))
        if not normal and any(m["codec"] == tncimage.CODEC_TILED for m in bim["mips"]):
            raise TncImageError("Its large mips are a tile chain, a format not decoded yet.")
        hid = entry.hash_0x60 & tncimage.ID_MASK
        if any(e.type == "image" and e.hash_0x60 & tncimage.ID_MASK == hid for a in dlc for e in a.entries):
            raise TncImageError("A DLC texture shares its data; the loader writes base/ only.")
        dlc_db = tncimage.TexdbSet(sorted(inst.base.parent.glob("dlc/*/base/*.texdb")))
        keep, written = set(), []
        for m in bim["mips"]:
            if m["streamed"]:
                key = tncimage.texdb_key(entry.hash_0x60, bim["mip_base"], m["level"])
                if dlc_db.rows(key):
                    raise TncImageError("Mip %d also lies in a DLC .texdb; the loader writes base/ only." % m["level"])
                if inst.texdb_hits("%016x" % key):
                    keep.add(key)
                    written.append(m["level"])
            else:
                written.append(m["level"])
        if not written or written != list(range(written[0], len(bim["mips"]))):
            raise TncImageError("Its shipped mips are not one chain down to the smallest (%s)." % written)
        group = sorted({e.name for a in inst.archives for e in a.entries
                        if e.type == "image" and e.hash_0x60 & tncimage.ID_MASK == hid})
        for other in group:
            if other != name and {tncview.payload(a, e, oo) for a, e in inst.find("image", other)} != {data}:
                raise TncImageError("It shares its streamed mips with %s, whose data differ." % other)
    finally:
        for a in dlc:
            a.close()
    info = {"name": name, "width": bim["width"], "height": bim["height"], "format": fmt, "mips": written,
            "keep": keep, "group": group, "blood": [], "blood_skipped": []}
    if blood:
        for sib in sorted({e.name for a in inst.archives for e in a.entries if e.type == "image"
                           and e.name != name and e.name.replace("_blood", "") == name}):
            try:
                b = check_image(inst, sib, oo, blood=False, normal=normal)
            except TncImageError as exc:
                info["blood_skipped"].append("%s: %s" % (sib, exc))
                continue
            if (b["width"], b["height"], b["format"]) == (bim["width"], bim["height"], fmt):
                info["blood"].append(b)
            else:
                info["blood_skipped"].append("%s: other size or format" % sib)
    return info


def image_assets(inst, info, src, oo, blood=True, opengl=False):
    """{mod asset key: bytes} replacing info's image (check_image), every name of
    its group and, with `blood`, its blood siblings, from `src` = (w, h, RGBA).
    A BC5 normal map goes through tncimage.normal_map(); `opengl` says its
    picture's green points up."""
    out = {}
    for one in [info] + (info["blood"] if blood else []):
        a, e = inst.find("image", one["name"])[0]
        if one["format"] == "BC5":
            new, blocks = tncimage.normal_map(e, tncview.payload(a, e, oo), src, oo, one["keep"], opengl)
        else:
            new, blocks = replace_any(e, tncview.payload(a, e, oo), src, oo, one["keep"])
        for n in one["group"]:
            out["image:" + n] = new
        for k, b in blocks.items():
            out["%s:%016x" % (TEXDB, k)] = b
    return out


CUSTOM = [PISTOL + n + ".tga$mtlkind=albedo$streamed" for n in ("pistole_61", "pistole_61_blood")]


def custom_names(png):
    """(folder, display name) from the picture's file name, so every player's
    skin is its own mod: 'Meine Pistole!.png' -> ('tnc_skin_meine_pistole',
    'Meine Pistole!'). The folder is ASCII [a-z0-9_] only."""
    stem = Path(png).stem.strip()
    plain = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", plain.lower()).strip("_")[:40].rstrip("_") or "eigen"
    return "tnc_skin_" + slug, stem or slug


def build_custom_mod(game_root, png, out_dir, names=CUSTOM, oo=None):
    """Mod folder with `png` as the pistol's albedo (body and its blood variant).

    `game_root` must be pristine where it matters (refused while a journal
    names images); `oo` defaults to the Oodle DLL found there.
    """
    src = read_png(png)
    root = Path(game_root)
    inst = Installation(root / "base")
    journal = inst.read_journal() or {}
    if any(a.startswith(("image:", TEXDB + ":")) for a in journal.get("assets", ())):
        raise TncImageError("The installation currently carries image mods (%s). "
                            "Revert first, then build." % ", ".join(journal.get("mods", ())))
    texdbs = tncimage.TexdbSet(sorted((root / "base").glob("*.texdb")))
    dlc = tncimage.TexdbSet(sorted(root.glob("dlc/*/base/*.texdb")))
    oo = oo or oodle.load(root)
    out = Path(out_dir)
    (out / "assets").mkdir(parents=True, exist_ok=True)
    (out / "texdb").mkdir(exist_ok=True)
    if Path(png).resolve() != (out / "vorlage.png").resolve():
        (out / "vorlage.png").write_bytes(Path(png).read_bytes())
    assets = {}
    try:
        for name in names:
            hits = inst.find("image", name)
            copies = {tncview.payload(a, e, oo) for a, e in hits}
            if len(copies) != 1:
                raise TncImageError("%s: the copies are not identical" % name)
            print("  %s" % name.split("/")[-1], flush=True)
            new, blocks = replace(hits[0][1], copies.pop(), texdbs, src, oo)
            # tncpatch writes base/ only, and the engine searches every mounted .texdb.
            shared = [k for k in blocks if dlc.lookup(k) is not None]
            if shared:
                raise TncImageError("%s: TexDB key also in a DLC TexDB: %s"
                                    % (name, ", ".join("%016x" % k for k in shared)))
            (out / asset_file(name)).write_bytes(new)
            assets["image:" + name] = {"file": asset_file(name)}
            for key, block in blocks.items():
                rel = "texdb/%016x.bin" % key
                (out / rel).write_bytes(block)
                assets["%s:%016x" % (TEXDB, key)] = {"file": rel}
    finally:
        inst.close()
    folder, title = custom_names(png)
    manifest = {
        "id": "local." + folder,
        "name": "Own Pistol: %s" % title,
        "version": "2.0.0",
        "author": "",
        "description": ("The standard pistol with your own skin from vorlage.png (color texture of the "
                        "body and the blood variant), every mip level re-encoded at full resolution, "
                        "nothing downscaled. The loader appends larger blocks to the .texdb; Revert "
                        "restores the files bit for bit. Gloss map, silencer, magazine and the pistol "
                        "lying on the ground stay original. Do not enable together with one of the "
                        "golden pistols. Checked structurally, not yet confirmed in game."),
        "priority": 100,
        "spiel": "tnc",
        "assets": assets,
    }
    (out / "mod.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return manifest


def main(argv=None):
    import argparse
    from .game import Game
    ap = argparse.ArgumentParser(prog="python -m wolfsdk.tncskin")
    ap.add_argument("--bild", help="your own PNG as the pistol skin (otherwise: golden pistol)")
    args = ap.parse_args(argv)
    game = Game.find(title="tnc")
    mods = Path(__file__).resolve().parent.parent / "mods"
    if args.bild:
        out = mods / custom_names(args.bild)[0]
        if out.exists() and not (out / "vorlage.png").is_file():
            raise SystemExit("%s belongs to another mod. Please rename the image." % out)
        print("Building %s from %s" % (out, args.bild))
        manifest = build_custom_mod(game.root, args.bild, out)
    else:
        out = mods / "tnc_skin_gold_pistol"
        print("Building %s from %s" % (out, game.root))
        manifest = build_mod(game.root, out)
    print("%d assets written" % len(manifest["assets"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
