"""Codec-2 "tile chain" decoder (BC4 / BC5 maps, mips >= 512 px): WBP wavelet pages.
(re_probes/agent_reports/r12_tilechain.md; maptex._level decodes streamed codec-2 mips with it)

PROVEN source of the format (all three are the engine's own code):
  * GPU: renderProgResource gpuuploadtranscodewbpsynth / ...wbpblockbc4 / ...wbpblockbc5
    (GLSL text: re_probes/tilechain3/shaders/): 1-level inverse CDF 9/7 + BC4/BC5 encode.
  * CPU: rva 0x79bfc9 (tile reader) -> 0x115b6c0 (level-2 inverse, writes the bordered LL1 band as
    8 bit bytes into the head of the plane) ; rva 0x7a5a47 (page upload, size formula,
    dispatch synth then block).

A tile (512 x 512 for BC4, 512 x 256 for BC5; header u8[8] = fmt, 1, 2,2,3, 4,4,5) is, per channel
plane (BC5: X plane then Y plane), 8 bit coefficients, no entropy coding beyond the Oodle layer:

  level 2, four bands (LL2, HL2, LH2, HH2), each (w/4+6) x (h/4+6) bytes, 3 texel border
  level 1, three bands (HL1, LH1, HH1),      each (w/2+4) x (h/2+4) bytes, 2 texel border
  plane size = 4*(w/4+6)*(h/4+6) + 3*(w/2+4)*(h/2+4)   (BC5 512x256: 140480, BC4 512x512: 274624)

LL is unsigned (b/255); details are signed, 0x7F = 0: (b*2/254 - 1) * scale, scale = header bytes
(level 2: 2,2,3 for HL,LH,HH; level 1: 4,4,5). Decoding = level-2 inverse (-> LL1 bytes) then
level-1 inverse (-> pixels 0..1 -> *255), exactly the engine order. The GPU then encodes BC4/BC5
blocks from the pixels; encode_bc4_block() repeats that shader so the result matches the engine.

Pure python (no numpy on this PC).
"""
import math
import operator
import struct

H_TAPS = (0.0, -0.091271763114, -0.057543526229, 0.591271763114, 1.11508705,
          0.591271763114, -0.057543526229, -0.091271763114, 0.0)
G_TAPS = (0.026748757411, 0.016864118443, -0.078223266529, -0.266864118443, 0.602949018236,
          -0.266864118443, -0.078223266529, 0.016864118443, 0.026748757411)

ORDER_L2 = ("LL", "HL", "LH", "HH")
ORDER_L1 = ("HL", "LH", "HH")
HEADER = 24

# the shipped GLSL reads LH[y+1] where the textbook filter wants LH[y+2] for one tap
# (weight 0.027); the GPU does that. Kept switchable, default = GPU behaviour.
SHADER_QUIRK = True


def plane_size(w, h):
    return 4 * (w // 4 + 6) * (h // 4 + 6) + 3 * (w // 2 + 4) * (h // 2 + 4)


def _band(buf, off, pitch, rows, kind, scale):
    out = []
    if kind == "LL":
        k = 1.0 / 255.0
        for r in range(rows):
            o = off + r * pitch
            out.append([v * k for v in buf[o:o + pitch]])
    else:
        k = 2.0 / 254.0
        for r in range(rows):
            o = off + r * pitch
            out.append([(v * k - 1.0) * scale for v in buf[o:o + pitch]])
    return out


def _vsynth(lo, hi, m0, m1, ro, quirk):
    """vertical synthesis; rows indexed i = m + ro; returns rows 2m, 2m+1 for m in [m0, m1)."""
    h = H_TAPS
    h2, h4, h6, h1, h3, h5, h7 = h[2], h[4], h[6], h[1], h[3], h[5], h[7]
    g0, g1, g2, g3, g4, g5, g6, g7, g8 = G_TAPS
    out = []
    for m in range(m0, m1):
        i = m + ro
        L1, L2, L3, L4 = lo[i - 1], lo[i], lo[i + 1], lo[i + 2]
        D0, D1, D2, D3, D4 = hi[i - 2], hi[i - 1], hi[i], hi[i + 1], hi[i + 2]
        out.append([b * g1 + c * h2 + d * g3 + e * h4 + f * g5 + p * h6 + q * g7
                    for b, c, d, e, f, p, q in zip(D0, L1, D1, L2, D2, L3, D3)])
        D4s = D3 if quirk else D4
        out.append([a * g0 + b * h1 + c * g2 + d * h3 + e * g4 + f * h5 + p * g6 + q * h7 + r * g8
                    for a, b, c, d, e, f, p, q, r in zip(D0, L1, D1, L2, D2, L3, D3, L4, D4s)])
    return out


def _hsynth(lo, hi, n0, n1, co):
    """horizontal synthesis, samples 2n, 2n+1 for n in [n0, n1); row index j = n + co."""
    h = H_TAPS
    h2, h4, h6, h1, h3, h5, h7 = h[2], h[4], h[6], h[1], h[3], h[5], h[7]
    g0, g1, g2, g3, g4, g5, g6, g7, g8 = G_TAPS
    a, b = n0 + co, n1 + co
    res = []
    for L, D in zip(lo, hi):
        Lm1, L0, Lp1, Lp2 = L[a - 1:b - 1], L[a:b], L[a + 1:b + 1], L[a + 2:b + 2]
        Dm2, Dm1, D0, Dp1, Dp2 = D[a - 2:b - 2], D[a - 1:b - 1], D[a:b], D[a + 1:b + 1], D[a + 2:b + 2]
        row = [0.0] * (2 * (n1 - n0))
        row[0::2] = [dm2 * g1 + lm1 * h2 + dm1 * g3 + l0 * h4 + d0 * g5 + lp1 * h6 + dp1 * g7
                     for dm2, lm1, dm1, l0, d0, lp1, dp1 in zip(Dm2, Lm1, Dm1, L0, D0, Lp1, Dp1)]
        row[1::2] = [dm2 * g0 + lm1 * h1 + dm1 * g2 + l0 * h3 + d0 * g4 + lp1 * h5 + dp1 * g6 + lp2 * h7 + dp2 * g8
                     for dm2, lm1, dm1, l0, d0, lp1, dp1, lp2, dp2 in zip(Dm2, Lm1, Dm1, L0, D0, Lp1, Dp1, Lp2, Dp2)]
        res.append(row)
    return res


def _synth2d(LL, LH, HL, HH, ro, co, m0, m1, n0, n1, quirk=False):
    Lb = _vsynth(LL, LH, m0, m1, ro, quirk)
    Hb = _vsynth(HL, HH, m0, m1, ro, False)   # the slip is only in the LL/LH half of the shader
    return _hsynth(Lb, Hb, n0, n1, co)


def _q8(rows):
    """float 0..1 rows -> bytes rows, saturate(v)*255+0.5 (shader / CPU rounding)"""
    return [bytes([0 if v <= 0.0 else 255 if v >= 1.0 else int(v * 255.0 + 0.5) for v in r]) for r in rows]


def level1_ll(buf, off, w, h, scale2=(2.0, 2.0, 3.0), quantize=True):
    """Level-2 inverse: the bordered LL1 band, (h/2+4) rows x (w/2+4) columns. Bytes when
    `quantize` (what the engine stores), else float 0..1."""
    w2, h2 = w // 4, h // 4
    p2, r2 = w2 + 6, h2 + 6
    sc = {"HL": scale2[0], "LH": scale2[1], "HH": scale2[2], "LL": 1.0}
    b2 = {n: _band(buf, off + i * p2 * r2, p2, r2, n, sc[n]) for i, n in enumerate(ORDER_L2)}
    ll = _synth2d(b2["LL"], b2["LH"], b2["HL"], b2["HH"], 3, 3, -1, h2 + 1, -1, w2 + 1)
    if len(ll) != h // 2 + 4 or len(ll[0]) != w // 2 + 4:
        raise ValueError("level-2 inverse gave %dx%d, expected %dx%d" % (len(ll[0]), len(ll), w // 2 + 4, h // 2 + 4))
    return _q8(ll) if quantize else ll


def decode_plane(buf, off, w, h, scale2=(2.0, 2.0, 3.0), scale1=(4.0, 4.0, 5.0), quantize_ll1=True):
    """One channel plane of a w x h tile -> h bytes rows of w (0..255)."""
    w2, h2 = w // 4, h // 4
    p2, r2 = w2 + 6, h2 + 6
    w1, h1 = w // 2, h // 2
    p1, r1 = w1 + 4, h1 + 4
    ll1 = level1_ll(buf, off, w, h, scale2, quantize_ll1)
    if quantize_ll1:
        k = 1.0 / 255.0
        ll1 = [[v * k for v in r] for r in ll1]
    off1 = off + 4 * p2 * r2
    sc = {"HL": scale1[0], "LH": scale1[1], "HH": scale1[2]}
    b1 = {"LL": ll1}
    for i, n in enumerate(ORDER_L1):
        b1[n] = _band(buf, off1 + i * p1 * r1, p1, r1, n, sc[n])
    img = _synth2d(b1["LL"], b1["LH"], b1["HL"], b1["HH"], 2, 2, 0, h1, 0, w1, SHADER_QUIRK)
    return _q8(img)


def decode_tile(raw, w, h, channels):
    """decompressed tile bytes -> list of `channels` planes (each a list of h bytes rows)"""
    ps = plane_size(w, h)
    if len(raw) < ps * channels:
        raise ValueError("tile has %d bytes, %d planes of %dx%d need %d" % (len(raw), channels, w, h, ps * channels))
    return [decode_plane(raw, c * ps, w, h) for c in range(channels)]


# -- BC4 / BC5 encoder = the shader's ComputeDXTAlpha (min/max, 1/32 inset) ---------------------

def encode_bc4_block(v):
    """16 pixel values (row-major 4x4, 0..255) -> 8 byte BC4 block, as gpuuploadtranscodewbpblock*"""
    lo, hi = min(v), max(v)
    inset = (hi - lo) >> 5
    lo = lo + inset if lo + inset <= 255 else 255
    hi = hi - inset if hi >= inset else 0
    ab = [(13 * hi + 1 * lo + 7) // 14, (11 * hi + 3 * lo + 7) // 14, (9 * hi + 5 * lo + 7) // 14,
          (7 * hi + 7 * lo + 7) // 14, (5 * hi + 9 * lo + 7) // 14, (3 * hi + 11 * lo + 7) // 14,
          (1 * hi + 13 * lo + 7) // 14]
    bits = 0
    for i, a in enumerate(v):
        idx = (8 - sum(1 for t in ab if a >= t)) & 7
        idx ^= 1 if idx < 2 else 0
        bits |= idx << (3 * i)
    return bytes([hi, lo]) + bits.to_bytes(6, "little")


def encode_plane_bc4(rows):
    """list of h rows (bytes, w) -> BC4 blocks, row-major block raster"""
    h, w = len(rows), len(rows[0])
    out = bytearray()
    for by in range(0, h, 4):
        r = rows[by:by + 4]
        for bx in range(0, w, 4):
            out += encode_bc4_block([r[y][bx + x] for y in range(4) for x in range(4)])
    return bytes(out)


def interleave_bc5(b0, b1):
    out = bytearray()
    for i in range(0, len(b0), 8):
        out += b0[i:i + 8] + b1[i:i + 8]
    return bytes(out)


# -- mip assembly -------------------------------------------------------------------------------

def parse_chain(block):
    """tile chain container -> list of dicts (comp, raw, row, x, w, h, unk8, body)"""
    pos, tiles = 0, []
    while pos + HEADER <= len(block):
        comp, raw, where, w, h = struct.unpack_from("<IIIHH", block, pos)
        body = block[pos + HEADER:pos + HEADER + comp]
        if len(body) != comp:
            raise ValueError("tile chain cut off at %d" % pos)
        tiles.append({"comp": comp, "raw": raw, "row": where >> 24, "x": where & 0xFFFFFF, "w": w, "h": h,
                      "unk8": block[pos + 16:pos + 24], "body": body})
        pos += HEADER + comp
    if pos != len(block):
        raise ValueError("%d stray bytes after the tile chain" % (len(block) - pos))
    return tiles


def _planes(block, oodle, width, height, fmt):
    channels = {"BC4": 1, "BC5": 2}[fmt]
    planes = [[bytearray(width) for _ in range(height)] for _ in range(channels)]
    for t in parse_chain(block):
        dec = oodle.decompress(t["body"], t["raw"])
        tp = decode_tile(dec, t["w"], t["h"], channels)
        y0 = t["row"] * 256   # row counts 256 texel rows (BC4 tiles are 512 tall: rows 0,2,4..)
        for c in range(channels):
            for y, r in enumerate(tp[c]):
                planes[c][y0 + y][t["x"]:t["x"] + t["w"]] = r
    return [[bytes(r) for r in p] for p in planes]


def decode_mip(block, oodle, width, height, fmt):
    """tile chain bytes of one mip -> (planes, bc) ; planes = [rows of bytes] per channel (height x width),
    bc = BC4 / BC5 block bytes of the whole mip. `oodle` needs .decompress(body, raw_size)."""
    planes = _planes(block, oodle, width, height, fmt)
    channels = len(planes)
    bc = encode_plane_bc4(planes[0]) if channels == 1 else interleave_bc5(
        encode_plane_bc4(planes[0]), encode_plane_bc4(planes[1]))
    return planes, bc


_Z = None   # 65536 bytes: the rebuilt z of a BC5 normal by x * 256 + y (as tncimage._bc5)


def decode_mip_pixels(mip, block, oodle, fmt):
    """The mip as RGBA in tncimage.decode's layout (BC4: v v v 255; BC5: x y z 255, z rebuilt), straight from
    the wavelet pixels: no BC4/BC5 encode + decode round trip (the GPU's), for previews (pbrmat, ~3x faster)."""
    global _Z
    w, h = mip["width"], mip["height"]
    planes = _planes(block, oodle, w, h, fmt)
    out = bytearray(b"\xff" * (4 * w * h))
    x = b"".join(planes[0])
    if fmt == "BC4":
        out[0::4] = out[1::4] = out[2::4] = x
        return bytes(out)
    if _Z is None:
        _Z = bytes(int(127.5 + 127.5 * math.sqrt(max(0.0, 1 - (i // 256 / 127.5 - 1) ** 2 - (i % 256 / 127.5 - 1) ** 2)))
                   for i in range(65536))
    y = b"".join(planes[1])
    out[0::4], out[1::4] = x, y
    out[2::4] = bytes(map(_Z.__getitem__, map(operator.add, map(_X256.__getitem__, x), y)))
    return bytes(out)


_X256 = [i * 256 for i in range(256)]


def decode_mip_record(mip, block, oodle, fmt):
    """The call the project wants: `mip` = a parse_bimage mip record (codec 2), `block` = the raw tile chain
    bytes of that mip (TexdbSet.lookup), fmt 'BC4' | 'BC5' -> the mip's BC block bytes (len == mip['raw_size'])."""
    bc = decode_mip(block, oodle, mip["width"], mip["height"], fmt)[1]
    if len(bc) != mip["raw_size"]:
        raise ValueError("decoded %d bytes, mip record expects %d" % (len(bc), mip["raw_size"]))
    return bc
