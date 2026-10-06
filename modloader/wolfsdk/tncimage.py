"""Wolfenstein II (TNC) images: .bimage header, .texdb lookup, pixels to PNG.

An `image` entry of The New Colossus is a BIM version 14 (`BIM\\x0e`): a 0x32
byte header, then one 32-byte record per mip level and cube face

    u32 level, u32 face, u32 width, u32 height,
    u32 raw_size, u32 codec, u32 stored_size, u32 offset

codec 0 = stored raw, 1 = one Oodle stream, 2 = tile chain. The header's
level count (0x18) doubles as the 4-bit base of the texdb slot; the first
`streamed_count` levels (0x2E) live in a .texdb under

    key = ((entry.hash_0x60 & 0x0FFF_FFFF_FFFF_FFFF) << 4) | (mip_base - level)

searched across *every* mounted .texdb, the rest sit in the .bimage at
`offset`. The read length is the record's stored_size, not anything in the
.texdb. Source: re_probes/agent_reports/texdbkey.md, tools/probe_matpipe.py.

Decoded here, pure Python: BC1, BC3, BC4, BC5, BC7 (and their _SRGB twins),
R8 and RGBA8 -- every format retail ships except BC6H, which only the HDR
light-probe cube maps use (as do the older BIM versions 10 and 13). Tile
chains (codec 2, the >= 512 px mips of most BC4/BC5 maps) are not
understood yet; the preview then drops to the next mip that is not one.

Written here: a BC5 normal map from the user's picture (normal_map()), every
mip as plain blocks under codec 1 -- see the section at the end.
"""

import functools
import math
import struct
from array import array
from bisect import bisect_left, bisect_right
from pathlib import Path

from . import image, oodle, tilechain

BIM_MAGIC = b"BIM\x0e"
BIM_HEADER = 0x32
BIM_MIP = 32
TEXDB_MAGIC = bytes.fromhex("4fa5c2292ef3c761")
TEXDB_TABLE = 0x20
ID_MASK = (1 << 60) - 1  # the engine's `shl r14, 4` drops the top nibble
CODEC_RAW, CODEC_OODLE, CODEC_TILED = 0, 1, 2
CODEC_V16 = {3: CODEC_OODLE, 4: CODEC_TILED}   # Youngblood (BIM 16) renumbered them; same layout (r14_youngblood.md)

# The engine's own format names (.text 0x784A70, see tools/probe_matpipe.py).
FORMATS = {
    3: "RGBA8", 4: "ARGB8", 5: "A", 6: "LA", 7: "RG8", 8: "L", 9: "I",
    10: "BC1", 11: "BC3", 12: "D24", 13: "D24S8", 14: "X32F", 15: "Y16F_X16F",
    19: "R8", 20: "R11FG11FB10F", 22: "BC6H", 23: "BC7", 24: "BC4", 25: "BC5",
    31: "D16", 32: "RGBA8_SRGB", 33: "BC1_SRGB", 34: "BC3_SRGB", 35: "BC7_SRGB",
}


class TncImageError(Exception):
    pass


# -- .texdb ---------------------------------------------------------------

class TexdbSet:
    """Every mounted .texdb, searched the way the engine does: all of them.

    A file's key table is read on first use and kept as two compact arrays;
    lookups are a binary search over the sorted keys. Block lengths are not
    stored anywhere -- a block ends where the next one (in offset order)
    begins, so the sorted offsets are built only for a file that gets a hit.
    Keys repeat, within a file and across files, but every copy carries the
    same bytes (tools/verify_tncimage.py checks), so the first hit wins. A
    broken .texdb only matters for a key no intact file has.
    """

    def __init__(self, texdb_paths):
        self.paths = [Path(p) for p in texdb_paths]
        self.root = None  # game folder, for the Oodle DLL; set by for_game()
        self._tables = {}

    @classmethod
    def for_game(cls, game_root):
        root = Path(game_root)
        found = cls(sorted(root.glob("base/*.texdb")) + sorted(root.glob("dlc/*/base/*.texdb")))
        found.root = root
        return found

    def _table(self, path):
        table = self._tables.get(path)
        if table is None:
            try:
                size = path.stat().st_size
                with open(path, "rb") as fh:
                    head = fh.read(TEXDB_TABLE)
                    if len(head) < TEXDB_TABLE or head[:8] != TEXDB_MAGIC:
                        raise TncImageError("%s is not a TexDB (magic %s)" % (path.name, head[:8].hex()))
                    count = struct.unpack_from("<Q", head, 0x18)[0]
                    if TEXDB_TABLE + 16 * count > size:
                        raise TncImageError("%s: table with %d rows does not fit in %d bytes"
                                            % (path.name, count, size))
                    rows = array("Q")
                    rows.frombytes(fh.read(16 * count))
                table = [rows[0::2], rows[1::2], None, size]
            except OSError as exc:
                table = TncImageError("%s: %s" % (path.name, exc.strerror or exc))
            except TncImageError as exc:
                table = exc
            self._tables[path] = table
        return table

    def rows(self, key):
        """[(path, start, end)] of every row carrying `key`, in every file.

        For a writer, which has to reach every copy; a broken file raises
        instead of being skipped, because a copy it hides stays unwritten.
        """
        out = []
        for path in self.paths:
            table = self._table(path)
            if isinstance(table, TncImageError):
                raise table
            keys, offsets = table[0], table[1]
            i = bisect_left(keys, key)
            while i < len(keys) and keys[i] == key:
                if table[2] is None:
                    table[2] = array("Q", sorted(offsets))
                j = bisect_right(table[2], offsets[i])
                out.append((path, offsets[i], table[2][j] if j < len(table[2]) else table[3]))
                i += 1
        return out

    def lookup(self, key, size=None):
        """The stored bytes of `key`, or None if no .texdb has it.

        With `size` -- the mip's stored_size, which is all the engine reads --
        a longer block comes back cut to `size`. A block ends where the next
        offset in the table begins, which overshoots when a neighbour is not
        listed (pistole_61_pm, r11_pbrexport.md), and tncpatch leaves zero fill
        behind a shorter replacement.
        """
        broken = None
        for path in self.paths:
            table = self._table(path)
            if isinstance(table, TncImageError):
                broken = broken or table
                continue
            keys, offsets = table[0], table[1]
            i = bisect_left(keys, key)
            if i == len(keys) or keys[i] != key:
                continue
            if table[2] is None:
                table[2] = array("Q", sorted(offsets))
            start = offsets[i]
            ends = table[2]
            j = bisect_right(ends, start)
            end = ends[j] if j < len(ends) else table[3]
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    block = fh.read(end - start)
            except OSError as exc:
                raise TncImageError("%s: %s" % (path.name, exc.strerror or exc))
            if size is not None and len(block) > size:
                block = block[:size]
            return block
        if broken:
            raise broken
        return None


def texdb_key(hash_0x60, mip_base, level):
    slot = mip_base - level
    if not 0 <= slot <= 15:
        raise TncImageError("mip %d lies outside the 4-bit slot (base %d)" % (level, mip_base))
    return ((hash_0x60 & ID_MASK) << 4) | slot


# -- .bimage ----------------------------------------------------------------

def parse_bimage(data):
    """Header and mip table of a TNC .bimage. Pixel data is left in place."""
    if data[:3] != b"BIM":
        raise TncImageError("not a BIM (magic %r)" % bytes(data[:4]))
    if data[:4] not in (BIM_MAGIC, b"BIM\x10"):
        raise TncImageError("BIM version %d is not read (only 14 and 16)" % data[3])
    if len(data) < BIM_HEADER:
        raise TncImageError("BIM header cut off (%d bytes)" % len(data))
    faces = 6 if struct.unpack_from("<I", data, 0x04)[0] == 2 else 1
    width, height, _depth, mip_base = struct.unpack_from("<4I", data, 0x0C)
    code = struct.unpack_from("<I", data, 0x1D)[0]  # unaligned on purpose
    streamed = struct.unpack_from("<H", data, 0x2E)[0]
    count = mip_base * faces  # 0x18 is the level count as well as the slot base
    if not count or BIM_HEADER + BIM_MIP * count > len(data):
        raise TncImageError("header lists %d mip records, there is room for %d"
                            % (count, (len(data) - BIM_HEADER) // BIM_MIP))
    mips = []
    for i in range(count):
        level, face, w, h, raw, codec, stored, off = struct.unpack_from(
            "<8I", data, BIM_HEADER + BIM_MIP * i)
        if data[3] == 16:
            codec = CODEC_V16.get(codec, codec)
        if (level, face) != divmod(i, faces) or not w or not h:
            raise TncImageError("mip record %d is level %d/face %d, %dx%d" % (i, level, face, w, h))
        mips.append({"level": level, "face": face, "width": w, "height": h,
                     "raw_size": raw, "codec": codec, "stored_size": stored,
                     "offset": off, "streamed": level < streamed})
    return {"width": width, "height": height, "format": FORMATS.get(code, "Format %d" % code),
            "format_code": code, "mips": mips, "inline": streamed == 0,
            "mip_base": mip_base, "faces": faces, "streamed_count": streamed}


# -- block decoders: 4x4 block bytes -> 64 bytes RGBA, row-major -------------

def _bc1(blk, four=False):
    # the colour half is the last 8 bytes: all of a BC1 block, the back of BC3
    c0, c1, bits = struct.unpack_from("<HHI", blk, len(blk) - 8)
    r0, g0, b0 = image._rgb565(c0)
    r1, g1, b1 = image._rgb565(c1)
    if c0 > c1 or four:  # a BC3 colour block is always the 4-colour kind
        pal = (bytes((r0, g0, b0, 255)), bytes((r1, g1, b1, 255)),
               bytes(((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3, 255)),
               bytes(((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3, 255)))
    else:
        pal = (bytes((r0, g0, b0, 255)), bytes((r1, g1, b1, 255)),
               bytes(((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, 255)), b"\0\0\0\0")
    return b"".join([pal[(bits >> 2 * i) & 3] for i in range(16)])


def _bc4_values(blk, p=0):
    pal = image._bc3_alphas(blk[p], blk[p + 1])
    bits = int.from_bytes(blk[p + 2:p + 8], "little")
    return bytes([pal[(bits >> 3 * i) & 7] for i in range(16)])


def _bc3(blk):
    out = bytearray(_bc1(blk, four=True))
    out[3::4] = _bc4_values(blk)
    return out


def _bc4(blk):
    v = _bc4_values(blk)
    out = bytearray(b"\xff" * 64)
    out[0::4] = out[1::4] = out[2::4] = v
    return out


def _bc5(blk):
    """Tangent-space normal: X and Y are stored, Z is rebuilt for display."""
    xs, ys = _bc4_values(blk, 0), _bc4_values(blk, 8)
    out = bytearray(b"\xff" * 64)
    out[0::4] = xs
    out[1::4] = ys
    zs = []
    for x, y in zip(xs, ys):
        nx, ny = x / 127.5 - 1, y / 127.5 - 1
        zs.append(int(127.5 + 127.5 * math.sqrt(max(0.0, 1 - nx * nx - ny * ny))))
    out[2::4] = bytes(zs)
    return out


# BC7 ("BPTC", Khronos Data Format 1.3 / D3D11). Two-subset partitions as bit
# masks (bit i = subset of pixel i), three-subset partitions as digit strings.
# Retail uses modes 1, 3, 4, 5 and 6 only (all 266 BC7 images); 0, 2 and 7
# follow the spec but have no game data to be checked against.
_P2 = (0xCCCC, 0x8888, 0xEEEE, 0xECC8, 0xC880, 0xFEEC, 0xFEC8, 0xEC80,
       0xC800, 0xFFEC, 0xFE80, 0xE800, 0xFFE8, 0xFF00, 0xFFF0, 0xF000,
       0xF710, 0x008E, 0x7100, 0x08CE, 0x008C, 0x7310, 0x3100, 0x8CCE,
       0x088C, 0x3110, 0x6666, 0x366C, 0x17E8, 0x0FF0, 0x718E, 0x399C,
       0xAAAA, 0xF0F0, 0x5A5A, 0x33CC, 0x3C3C, 0x55AA, 0x9696, 0xA55A,
       0x73CE, 0x13C8, 0x324C, 0x3BDC, 0x6996, 0xC33C, 0x9966, 0x0660,
       0x0272, 0x04E4, 0x4E40, 0x2720, 0xC936, 0x936C, 0x39C6, 0x639C,
       0x9336, 0x9CC6, 0x817E, 0xE718, 0xCCF0, 0x0FCC, 0x7744, 0xEE22)
_P3 = tuple(tuple(int(c) for c in s) for s in (
    "0011001102212222 0001001122112221 0000200122112211 0222002200110111 "
    "0000000011221122 0011001100220022 0022002211111111 0011001122112211 "
    "0000000011112222 0000111111112222 0000111122222222 0012001200120012 "
    "0112011201120112 0122012201220122 0011011211221222 0011200122002220 "
    "0001001101121122 0111001120012200 0000112211221122 0022002200221111 "
    "0111011102220222 0001000122212221 0000001101220122 0000110022102210 "
    "0122012200110000 0012001211222222 0110122112210110 0000011012211221 "
    "0022110211020022 0110011020022222 0011012201220011 0000200022112221 "
    "0000000211221222 0222002200120011 0011001200220222 0120012001200120 "
    "0000111122220000 0120120120120120 0120201212010120 0011220011220011 "
    "0011112222000011 0101010122222222 0000000021212121 0022112200221122 "
    "0022001100220011 0220122102201221 0101222222220101 0000212121212121 "
    "0101010101012222 0222011102220111 0002111200021112 0000211221122112 "
    "0222011101110222 0002111211120002 0110011001102222 0000000021122112 "
    "0110011022222222 0022001100110022 0022112211220022 0000000000002112 "
    "0002000100020001 0222122202221222 0101222222222222 0111201122012220").split())
_A2 = (15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15,
       15, 2, 8, 2, 2, 8, 8, 15, 2, 8, 2, 2, 8, 8, 2, 2,
       15, 15, 6, 8, 2, 8, 15, 15, 2, 8, 2, 2, 2, 15, 15, 6,
       6, 2, 6, 8, 15, 15, 2, 2, 15, 15, 15, 15, 15, 2, 2, 15)
_A3A = (3, 3, 15, 15, 8, 3, 15, 15, 8, 8, 6, 6, 6, 5, 3, 3,
        3, 3, 8, 15, 3, 3, 6, 10, 5, 8, 8, 6, 8, 5, 15, 15,
        8, 15, 3, 5, 6, 10, 8, 15, 15, 3, 15, 5, 15, 15, 15, 15,
        3, 15, 5, 5, 5, 8, 5, 10, 5, 10, 8, 13, 15, 12, 3, 3)
_A3B = (15, 8, 8, 3, 15, 15, 3, 8, 15, 15, 15, 15, 15, 15, 15, 8,
        15, 8, 15, 3, 15, 8, 15, 8, 3, 15, 6, 10, 15, 15, 10, 8,
        15, 3, 15, 10, 10, 8, 9, 10, 6, 15, 8, 15, 3, 6, 6, 8,
        15, 3, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 3, 15, 15, 8)
_WEIGHTS = {2: (0, 21, 43, 64), 3: (0, 9, 18, 27, 37, 46, 55, 64),
            4: (0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64)}
# subsets, partition bits, rotation bits, index-select bits, colour bits,
# alpha bits, p-bit per endpoint, p-bit per subset, index bits, 2nd index bits
_MODES = ((3, 4, 0, 0, 4, 0, 1, 0, 3, 0), (2, 6, 0, 0, 6, 0, 0, 1, 3, 0),
          (3, 6, 0, 0, 5, 0, 0, 0, 2, 0), (2, 6, 0, 0, 7, 0, 1, 0, 2, 0),
          (1, 0, 2, 1, 5, 6, 0, 0, 2, 3), (1, 0, 2, 0, 7, 8, 0, 0, 2, 2),
          (1, 0, 0, 0, 7, 7, 1, 0, 4, 0), (2, 6, 0, 0, 5, 5, 1, 0, 2, 0))


def _bc7(blk):
    b0 = blk[0]
    if b0 == 0:
        return bytes(64)  # mode 8 is reserved: transparent black
    mode = (b0 & -b0).bit_length() - 1
    ns, pb, rb, isb, cb, ab, epb, spb, ib, ib2 = _MODES[mode]
    v = int.from_bytes(blk, "little") >> (mode + 1)
    part = v & ((1 << pb) - 1)
    v >>= pb
    rot = v & ((1 << rb) - 1)
    v >>= rb
    sel = v & ((1 << isb) - 1)
    v >>= isb
    n = 2 * ns
    widths = (cb, cb, cb, ab) if ab else (cb, cb, cb)
    ep = [[0, 0, 0, 255] for _ in range(n)]
    for c, bits in enumerate(widths):
        mask = (1 << bits) - 1
        for e in ep:
            e[c] = v & mask
            v >>= bits
    if epb:
        pbits = [(v >> i) & 1 for i in range(n)]
        v >>= n
    elif spb:  # one bit per subset, shared by both of its endpoints
        pbits = [(v >> (i >> 1)) & 1 for i in range(n)]
        v >>= ns
    else:
        pbits = None
    for i, e in enumerate(ep):
        for c, bits in enumerate(widths):
            x = e[c]
            if pbits:
                x, bits = (x << 1) | pbits[i], bits + 1
            x <<= 8 - bits
            e[c] = x | (x >> bits)
    if ns == 1:
        sub, anchors = (0,) * 16, (0,)
    elif ns == 2:
        sub, anchors = [(_P2[part] >> i) & 1 for i in range(16)], (0, _A2[part])
    else:
        sub, anchors = _P3[part], (0, _A3A[part], _A3B[part])
    i1 = []
    for i in range(16):
        bits = ib - (i in anchors)  # an anchor index drops its top bit
        i1.append(v & ((1 << bits) - 1))
        v >>= bits
    if not ib2:
        w = _WEIGHTS[ib]
        pal = [[bytes([((64 - x) * e0[c] + x * e1[c] + 32) >> 6 for c in range(4)]) for x in w]
               for e0, e1 in zip(ep[0::2], ep[1::2])]
        return b"".join([pal[sub[i]][i1[i]] for i in range(16)])
    # Modes 4 and 5: one subset, a second index set for alpha (or, with the
    # selector bit, for colour), and a rotation that swaps alpha into a colour.
    i2 = []
    for i in range(16):
        bits = ib2 - (i == 0)
        i2.append(v & ((1 << bits) - 1))
        v >>= bits
    wc, wa = _WEIGHTS[ib], _WEIGHTS[ib2]
    if sel:
        i1, i2, wc, wa = i2, i1, wa, wc
    e0, e1 = ep
    out = bytearray(64)
    for c in range(4):
        idx, w = (i2, wa) if c == 3 else (i1, wc)
        pal = [((64 - x) * e0[c] + x * e1[c] + 32) >> 6 for x in w]
        out[c::4] = bytes([pal[k] for k in idx])
    if rot:
        out[rot - 1::4], out[3::4] = out[3::4], out[rot - 1::4]
    return out


# base format -> (block decoder, bytes per 4x4 block) or (None, bytes per pixel)
_DECODERS = {"BC1": (_bc1, 8), "BC3": (_bc3, 16), "BC4": (_bc4, 8),
             "BC5": (_bc5, 16), "BC7": (_bc7, 16), "R8": (None, 1), "RGBA8": (None, 4)}


def _sample(row, w, step):
    """Every `step`-th RGBA pixel of the first `w` in `row`."""
    if step == 1:
        return row[:4 * w]
    return array("I", bytes(row[:4 * w]))[::step].tobytes()


def decode(fmt, raw, width, height, step=1):
    """(w, h, RGBA) of one mip, keeping every `step`-th pixel and row.

    Only the blocks a kept pixel falls into are decoded, and identical blocks
    (flat areas are full of them) are decoded once.
    """
    fn, size = _DECODERS[fmt.replace("_SRGB", "")]
    raw = bytes(raw)
    out = bytearray()
    ys = range(0, height, step)
    if fn is None:
        if len(raw) != width * height * size:
            raise TncImageError("mip has %d bytes, %dx%d %s needs %d"
                                % (len(raw), width, height, fmt, width * height * size))
        for y in ys:
            line = raw[y * width * size:(y + 1) * width * size]
            if size == 1:
                row = bytearray(b"\xff" * 4 * width)
                row[0::4] = row[1::4] = row[2::4] = line
                line = row
            out += _sample(line, width, step)
        return len(range(0, width, step)), len(ys), out

    bw, bh = (width + 3) // 4, (height + 3) // 4
    if len(raw) != bw * bh * size:
        raise TncImageError("mip has %d bytes, %dx%d %s needs %d"
                            % (len(raw), width, height, fmt, bw * bh * size))
    cols = sorted({x >> 2 for x in range(0, width, step)})
    cache = {}
    band_row, band = -1, None
    for y in ys:
        by = y >> 2
        if by != band_row:
            band = [bytearray(16 * bw) for _ in range(4)]
            base = by * bw * size
            for bx in cols:
                blk = raw[base + bx * size:base + (bx + 1) * size]
                px = cache.get(blk)
                if px is None:
                    px = cache[blk] = fn(blk)
                o = 16 * bx
                for r in range(4):
                    band[r][o:o + 16] = px[16 * r:16 * r + 16]
            band_row = by
        out += _sample(band[y & 3], width, step)
    return len(range(0, width, step)), len(ys), out


# -- the preview ------------------------------------------------------------

TILED_MAX = 1024   # ponytail: pure-Python wavelet decode, ~1 s at 512 px, ~4 s at 1024; bigger tile chains are skipped


def _unpack(block, mip, root, fmt=None):
    if mip["codec"] == CODEC_RAW:
        return block
    if mip["codec"] == CODEC_TILED:
        try:
            return tilechain.decode_mip_record(mip, block, oodle.load(root), fmt.replace("_SRGB", ""))
        except (oodle.OodleError, ValueError, struct.error) as exc:
            raise TncImageError("mip %d: tile chain cannot be decoded: %s" % (mip["level"], exc))
    if mip["codec"] != CODEC_OODLE:
        raise TncImageError("mip %d: unknown codec %d" % (mip["level"], mip["codec"]))
    try:
        # fuzz-safe: a user's DLC may carry a broken block, and texture
        # payloads are Kraken, which decodes fuzz-safe (LZNA would not)
        return oodle.load(root).decompress(block, mip["raw_size"], fuzz_safe=True)
    except oodle.OodleError as exc:
        raise TncImageError("mip %d cannot be unpacked: %s" % (mip["level"], exc))


def to_png(entry, data, texdbs, max_side=512):
    """(PNG bytes, one-line info) for an `image` entry's decompressed payload.

    Takes the smallest mip that still covers `max_side` and walks down from
    there past mips it cannot use (tile chains over TILED_MAX, top levels the build did not
    ship, blocks no .texdb has -- the engine does the same), saying so in the
    info line. The result is at most `max_side` on its longer side.
    """
    bim = parse_bimage(data)
    fmt = bim["format"]
    if fmt.replace("_SRGB", "") not in _DECODERS:
        raise TncImageError("%dx%d %s: this format is not decoded"
                            % (bim["width"], bim["height"], fmt))
    chain = [m for m in bim["mips"] if m["face"] == 0]
    start = 0
    for i, m in enumerate(chain):
        if max(m["width"], m["height"]) >= max_side:
            start = i
    root = texdbs.root if texdbs is not None else None
    notes, missing = [], None
    for m in chain[start:]:
        if m["streamed"]:
            if m["codec"] == CODEC_TILED and (fmt.replace("_SRGB", "") not in ("BC4", "BC5")
                                              or max(m["width"], m["height"]) > TILED_MAX):
                if "tile chain skipped" not in notes:
                    notes.append("tile chain skipped")
                continue
            if texdbs is None:
                if "without TexDB" not in notes:
                    notes.append("without TexDB")
                continue
            key = texdb_key(entry.hash_0x60, bim["mip_base"], m["level"])
            block = texdbs.lookup(key, m["stored_size"])
            if block is None:
                if not missing:
                    missing = (key, m["level"])
                    notes.append("TexDB without block %016x (mip %d)" % missing)
                continue
            if len(block) != m["stored_size"]:
                raise TncImageError("TexDB block %016x has %d bytes, mip %d expects %d"
                                    % (key, len(block), m["level"], m["stored_size"]))
            source = "TexDB, tile chain" if m["codec"] == CODEC_TILED else "TexDB"
        else:
            block = data[m["offset"]:m["offset"] + m["stored_size"]]
            if len(block) != m["stored_size"]:
                raise TncImageError("embedded mip %d cut off (%d of %d bytes)"
                                    % (m["level"], len(block), m["stored_size"]))
            source = "embedded"
        side = max(m["width"], m["height"])
        step = max(1, -(-side // max_side))
        w, h, rgba = decode(fmt, _unpack(block, m, root, fmt), m["width"], m["height"], step)
        if bim["faces"] > 1:
            notes.append("cube map, face 1/%d" % bim["faces"])
        if step > 1:
            notes.append("downscaled 1:%d" % step)
        info = "%dx%d  %s  %d mips  preview: mip %d %dx%d (%s)" % (
            bim["width"], bim["height"], fmt, len(chain), m["level"], w, h, source)
        if notes:
            info += "  - " + ", ".join(notes)
        return image.to_png(w, h, rgba), info
    if missing:
        raise TncImageError("TexDB has no block %016x (mip %d) - is the "
                            "matching .texdb missing?" % missing)
    raise TncImageError("%dx%d %s: no displayable mip level (%s)"
                        % (bim["width"], bim["height"], fmt, ", ".join(notes) or "no TexDB"))


# -- a normal map from the user's picture ---------------------------------------
#
# Retail ships the >= 512 px mips of most BC4/BC5 maps as tile chains (codec
# 2), whose inner layout is not decoded. It does not have to be: the engine
# takes codec 1 -- one Kraken stream of plain BC blocks -- at any size, and
# retail mixes both within one image (re_probes/agent_reports/r2_tilechain.md
# section 5). So every mip is written as plain BC5 under codec 1: the record's
# codec goes 2 -> 1, raw_size stays (for a codec-2 mip it already is the plain
# BC size, ceil(w/4) * ceil(h/4) * 16), stored_size becomes the new block's
# length. Header and every other field stay. Which mips are written is
# `keep`, as for tncskin.replace_any(): the inline ones, and the streamed ones
# whose key some base .texdb or map textures.cache carries
# (tncskin.check_image(..., normal=True)); tncpatch writes every row of such a
# key, the caches included, and appends a block longer than its old place.
#
# Green: DirectX (+Y points down the image). Measured on retail, not assumed:
# a normal map baked from a height field is curl-free, and of the two pairings
# of X with Y only one is -- 60 of 60 retail BC5 maps are curl-free as
# X right / Y down (tools/verify_normalmaps.py checks a sample). An
# OpenGL-style picture (Blender, Unity, "OpenGL" exports) gets `opengl=True`:
# its green is flipped.

_UNIT = [b / 127.5 - 1 for b in range(256)]     # byte -> -1..1, symmetric: 255 - b is -v


def _q(v):
    """-1..1 -> byte, the inverse of _UNIT (a byte that went through _UNIT comes back)."""
    return min(255, max(0, int(v * 127.5 + 128)))


@functools.lru_cache(maxsize=1)
def _z_table():
    """Z of the unit normal for every (X byte << 8 | Y byte)."""
    return [math.sqrt(max(0.0, 1 - _UNIT[i >> 8] ** 2 - _UNIT[i & 255] ** 2)) for i in range(65536)]


def _halve_xy(w, h, xs, ys, W, H):
    """X and Y planes w x h -> W x H (each side halved or kept): every pixel the
    mean of its 2 x 2 (or 2 x 1) unit vectors, made unit length again."""
    fx, fy = w // W, h // H
    unit, ztab = _UNIT, _z_table()
    ox, oy = bytearray(W * H), bytearray(W * H)
    o = 0
    for y in range(H):
        rows = [(xs[(y * fy + r) * w:(y * fy + r + 1) * w], ys[(y * fy + r) * w:(y * fy + r + 1) * w])
                for r in range(fy)]
        for x in range(W):
            sx = sy = sz = 0.0
            for rx, ry in rows:
                for c in range(x * fx, x * fx + fx):
                    a, b = rx[c], ry[c]
                    sx += unit[a]
                    sy += unit[b]
                    sz += ztab[a << 8 | b]
            n = math.sqrt(sx * sx + sy * sy + sz * sz) or 1.0
            ox[o], oy[o] = _q(sx / n), _q(sy / n)
            o += 1
    return bytes(ox), bytes(oy)


def normal_mips(src, sizes, opengl=False):
    """[(X plane, Y plane)] of every mip in `sizes` ((w, h), ...), from
    `src` = (w, h, RGBA) at sizes[0].

    The top mip: R, G, B read as a vector (byte / 127.5 - 1; green negated for
    an OpenGL-style picture) and made unit length -- with B where it points
    out of the surface (B > 127), else from R and G alone, cut back to the
    unit circle (a two-channel export with B = 0). Every smaller mip is the
    renormalised mean of the one above it. A picture already of unit normals
    keeps its R and G bytes.
    """
    w, h, rgba = src
    if (w, h) != tuple(sizes[0]):
        raise TncImageError("picture %d x %d, mip 0 is %d x %d" % ((w, h) + tuple(sizes[0])))
    px = array("I", bytes(rgba))
    table = {}
    for p in set(px):
        r, g, b = p & 255, (p >> 8) & 255, (p >> 16) & 255
        if opengl:
            g = 255 - g
        x, y, z = _UNIT[r], _UNIT[g], _UNIT[b]
        n = math.sqrt(x * x + y * y + z * z) if b > 127 else max(1.0, math.sqrt(x * x + y * y))
        table[p] = _q(x / n) | _q(y / n) << 8
    xy = array("H", map(table.__getitem__, px)).tobytes()   # little endian: X, Y, X, Y ...
    out = [(xy[0::2], xy[1::2])]
    for (pw, ph), (mw, mh) in zip(sizes, sizes[1:]):
        if pw not in (mw, 2 * mw) or ph not in (mh, 2 * mh):
            raise TncImageError("mip %d x %d does not halve %d x %d" % (mw, mh, pw, ph))
        out.append(_halve_xy(pw, ph, *out[-1], mw, mh))
    return out


def normal_map(entry, data, src, oo, keep, opengl=False):
    """(new .bimage payload, {texdb key: block}) for the BC5 normal map whose
    archive entry is `entry` and decompressed payload `data`, every written mip
    made from `src` = (w, h, RGBA) -- the user's PNG at exactly the texture's
    size, both sides powers of two. `keep`: the texdb keys to write (see the
    section head); a streamed mip whose key is not in it keeps its record and
    gets no block. `opengl`: the picture's green points up. TncImageError
    (English, for the Studio) when the picture or the image does not fit."""
    from . import bcenc     # bcenc imports this module
    bim = parse_bimage(data)
    if bim["faces"] != 1 or bim["format"] != "BC5":
        raise TncImageError("%s with %d face(s): only single BC5 normal maps are written this way."
                            % (bim["format"], bim["faces"]))
    w, h = src[0], src[1]
    if not w or not h or w & (w - 1) or h & (h - 1):
        raise TncImageError("The normal map PNG is %d x %d; both sides must be powers of two "
                            "(such as 1024 x 1024)." % (w, h))
    if (w, h) != (bim["width"], bim["height"]):
        raise TncImageError("The normal map PNG is %d x %d, this texture is %d x %d. Normal maps "
                            "are not rescaled: save it at exactly that size."
                            % (w, h, bim["width"], bim["height"]))
    mips = bim["mips"]
    planes = normal_mips(src, [(m["width"], m["height"]) for m in mips], opengl)
    new, blocks = bytearray(data), {}
    for i, (m, (xs, ys)) in enumerate(zip(mips, planes)):
        key = texdb_key(entry.hash_0x60, bim["mip_base"], m["level"]) if m["streamed"] else None
        if key is not None and key not in keep:
            continue
        raw = bcenc.encode_bc5(xs, ys, m["width"], m["height"])
        if len(raw) != m["raw_size"]:
            raise TncImageError("mip %d: %d bytes of BC5 instead of %d" % (m["level"], len(raw), m["raw_size"]))
        if key is None:
            if m["codec"] != CODEC_RAW or len(raw) != m["stored_size"]:
                raise TncImageError("mip %d: inline with codec %d and %d bytes, not written"
                                    % (m["level"], m["codec"], m["stored_size"]))
            new[m["offset"]:m["offset"] + len(raw)] = raw
        else:
            blocks[key] = oo.compress(raw, oodle.KRAKEN, oodle.LEVEL_OPTIMAL)
            struct.pack_into("<II", new, BIM_HEADER + BIM_MIP * i + 20, CODEC_OODLE, len(blocks[key]))
    return bytes(new), blocks
