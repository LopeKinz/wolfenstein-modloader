"""BC1, BC4 and BC5 encoders, pure Python, the inverses of tncimage's decoders.

BC1 (and BC1_SRGB, which is the same bits read through a different lookup)
for the colour and specular maps. BC4 (one channel) and BC5 (two BC4 blocks,
X then Y: a normal map) for the maps whose large mips retail ships as tile
chains: a replacement writes every mip as plain blocks under codec 1 instead
(re_probes/agent_reports/r2_tilechain.md §5), see tncimage.normal_map().

BC4 per block: min and max as the endpoints of the 8-value ramp, every value
to its nearest ramp entry -- the decoder's floored palette
(image._bc3_alphas), so a decode/encode round trip measures what the decoder
gives back -- then one least-squares refit of both endpoints to those steps,
kept where it errs less. A flat block is a0 == a1 with every index 0. The
6-value mode (explicit 0 and 255) is never written.

BC1 per block: principal axis of the 16 colours (power iteration on the 3x3
covariance), endpoints at the extreme projections, indices by projection onto
the *quantised* endpoint line, then one least-squares pass that refits both
endpoints to the chosen indices. A block still off by more than RMS 4 then
gets a cluster fit over its distinct colours (see _cluster). map_block() is
the other way to get a block:
keep a BC1 block's indices and send its endpoints through a colour map.
Colours are handled in the stored (sRGB-encoded) space, exactly as the
decoder hands them out, so a decode/encode round trip does not drift. Alpha is not stored: a block with a transparent
pixel is refused rather than silently made opaque.

tools/verify_skin_texture.py measures the BC1 round trip on real game mips
(PSNR >= 38 dB) and checks that a scrambled block layout fails it;
tools/verify_normalmaps.py does the same for BC5.
"""

import struct
from collections import Counter
from itertools import combinations_with_replacement
from operator import add, mul

from .image import _bc3_alphas
from .tncimage import TncImageError, _bc1

_W = (0, 3, 1, 2)          # BC1 index -> weight of c1 in thirds (0, 1, 1/3, 2/3)
_IDX = (0, 2, 3, 1)        # position along c0..c1 in thirds -> BC1 index
# Squared error (16 pixels x 4 channels) above which a block gets the cluster
# fit as well: RMS 4 per value. Most blocks never reach it.
CLUSTER_ABOVE = 256


def _q565(r, g, b):
    r = min(31, max(0, int(r * 31 / 255 + 0.5)))
    g = min(63, max(0, int(g * 63 / 255 + 0.5)))
    b = min(31, max(0, int(b * 31 / 255 + 0.5)))
    return (r << 11) | (g << 5) | b


def _rgb(c):
    r, g, b = c >> 11, (c >> 5) & 63, c & 31
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def _indices(rs, gs, bs, c0, c1):
    """BC1 index bits for the 16 pixels against the 4-colour palette c0 > c1."""
    r0, g0, b0 = _rgb(c0)
    r1, g1, b1 = _rgb(c1)
    dr, dg, db = r1 - r0, g1 - g0, b1 - b0
    dd = dr * dr + dg * dg + db * db
    bits = 0
    for i in range(16):
        t = ((rs[i] - r0) * dr + (gs[i] - g0) * dg + (bs[i] - b0) * db) * 3 / dd
        k = 0 if t < 0.5 else 1 if t < 1.5 else 2 if t < 2.5 else 3
        bits |= _IDX[k] << (2 * i)
    return bits


def _pack(c0, c1, bits):
    if c0 < c1:            # keep the 4-colour mode: swap ends, mirror indices
        c0, c1 = c1, c0
        bits ^= 0x55555555   # 0<->1, 2<->3
    if c0 == c1:           # 3-colour mode would make index 3 transparent
        bits = 0
    return struct.pack("<HHI", c0, c1, bits)


def encode_block(px):
    """8 BC1 bytes for 64 bytes of RGBA (4x4, row-major)."""
    if px[3::4] != b"\xff" * 16:
        raise TncImageError("BC1 block with transparency is not encoded")
    blk, axis = _principal(px)
    if axis is not None:
        err = block_error(px, blk)
        if err > CLUSTER_ABOVE:
            better = _cluster(px, axis)
            if better and better[0] < err:
                return better[1]
    return blk


def _principal(px):
    """(block, axis) from the principal axis and one least-squares refit;
    axis is None for a block with nothing left to improve."""
    rs, gs, bs = px[0::4], px[1::4], px[2::4]
    mr, mg, mb = sum(rs) / 16, sum(gs) / 16, sum(bs) / 16
    crr = sum(map(mul, rs, rs)) / 16 - mr * mr
    cgg = sum(map(mul, gs, gs)) / 16 - mg * mg
    cbb = sum(map(mul, bs, bs)) / 16 - mb * mb
    if crr + cgg + cbb < 0.25:                      # flat block
        c = _q565(mr, mg, mb)
        return struct.pack("<HHI", c, c, 0), None
    crg = sum(map(mul, rs, gs)) / 16 - mr * mg
    crb = sum(map(mul, rs, bs)) / 16 - mr * mb
    cgb = sum(map(mul, gs, bs)) / 16 - mg * mb
    # start from the covariance row of the strongest channel, never orthogonal
    vx, vy, vz = max((crr, crr, crg, crb), (cgg, crg, cgg, cgb), (cbb, crb, cgb, cbb))[1:]
    for _ in range(4):
        vx, vy, vz = (crr * vx + crg * vy + crb * vz,
                      crg * vx + cgg * vy + cgb * vz,
                      crb * vx + cgb * vy + cbb * vz)
        n = max(abs(vx), abs(vy), abs(vz))
        if n == 0:
            c = _q565(mr, mg, mb)
            return struct.pack("<HHI", c, c, 0), None
        vx, vy, vz = vx / n, vy / n, vz / n
    ts = [(r - mr) * vx + (g - mg) * vy + (b - mb) * vz for r, g, b in zip(rs, gs, bs)]
    nn = vx * vx + vy * vy + vz * vz
    lo, hi = min(ts) / nn, max(ts) / nn
    c0 = _q565(mr + vx * hi, mg + vy * hi, mb + vz * hi)
    c1 = _q565(mr + vx * lo, mg + vy * lo, mb + vz * lo)
    axis = (vx, vy, vz)
    if c0 == c1:
        return _pack(c0, c1, 0), axis
    bits = _indices(rs, gs, bs, c0, c1)
    fit = _fit(rs, gs, bs, bits)
    if fit and fit[0] != fit[1]:
        n0, n1 = max(fit), min(fit)
        nbits = _indices(rs, gs, bs, n0, n1)
        if block_error(px, _pack(n0, n1, nbits)) < block_error(px, _pack(c0, c1, bits)):
            return _pack(n0, n1, nbits), axis
    return _pack(c0, c1, bits), axis


def _cluster(px, axis):
    """(error, block) of the best cluster fit, or None.

    The block's distinct colours, sorted along `axis`, go to the four palette
    levels in every order-preserving way; each assignment gets its endpoints
    by least squares. That finds the fit a single axis pass misses when the
    colours are not evenly spaced -- a gain that saturates bends them. At
    most 8 distinct colours (165 assignments); a decoded BC1 block has 4.
    """
    rs, gs, bs = px[0::4], px[1::4], px[2::4]
    cols = Counter(zip(rs, gs, bs))
    if not 2 <= len(cols) <= 8:
        return None
    vx, vy, vz = axis
    uniq = sorted(cols, key=lambda c: c[0] * vx + c[1] * vy + c[2] * vz)
    best = None
    for levels in combinations_with_replacement(range(4), len(uniq)):
        if levels[0] == levels[-1]:
            continue
        a11 = a12 = a22 = 0.0
        x1, x2 = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
        for c, level in zip(uniq, levels):
            w, t = cols[c], level / 3
            a11 += w * (1 - t) * (1 - t)
            a12 += w * (1 - t) * t
            a22 += w * t * t
            for ch in range(3):
                x1[ch] += w * (1 - t) * c[ch]
                x2[ch] += w * t * c[ch]
        det = a11 * a22 - a12 * a12
        n0 = _q565(*[(a22 * x1[ch] - a12 * x2[ch]) / det for ch in range(3)])
        n1 = _q565(*[(a11 * x2[ch] - a12 * x1[ch]) / det for ch in range(3)])
        if n0 == n1:
            continue
        hi, lo = max(n0, n1), min(n0, n1)
        cand = _pack(hi, lo, _indices(rs, gs, bs, hi, lo))
        err = block_error(px, cand)
        if best is None or err < best[0]:
            best = (err, cand)
    return best


def _fit(rs, gs, bs, bits):
    """Least-squares endpoints (index 0, index 1) for fixed 4-colour indices."""
    ws = [_W[(bits >> 2 * i) & 3] / 3 for i in range(16)]
    a11 = sum((1 - w) * (1 - w) for w in ws)
    a12 = sum((1 - w) * w for w in ws)
    a22 = sum(w * w for w in ws)
    det = a11 * a22 - a12 * a12
    if abs(det) < 1e-6:
        return None
    e0, e1 = [], []
    for ch in (rs, gs, bs):
        x1 = sum((1 - w) * v for w, v in zip(ws, ch))
        x2 = sum(w * v for w, v in zip(ws, ch))
        e0.append((a22 * x1 - a12 * x2) / det)
        e1.append((a11 * x2 - a12 * x1) / det)
    return _q565(*e0), _q565(*e1)


def map_block(blk, fn):
    """The BC1 block `blk` with both endpoints sent through the colour map `fn`.

    `fn` takes and returns RGBA bytes. The index bits are kept, so the result
    is exact for a linear map up to 565 rounding, and a stream of such blocks
    repeats wherever the original repeats -- it compresses like the original.
    A 3-colour block (index 3 = transparent, not an interpolant) is encoded
    from its pixels instead.
    """
    c0, c1, bits = struct.unpack("<HHI", blk)
    if c0 <= c1:
        return encode_block(fn(_bc1(blk)))
    p = fn(bytes(_rgb(c0)) + b"\xff" + bytes(_rgb(c1)) + b"\xff")
    return _pack(_q565(*p[0:3]), _q565(*p[4:7]), bits)


def block_error(px, blk):
    """Squared RGBA error of a BC1 block against 64 bytes of RGBA."""
    return sum((a - b) ** 2 for a, b in zip(px, _bc1(blk)))


# -- BC4 / BC5 -------------------------------------------------------------------

_STEP = (1, 7, 6, 5, 4, 3, 2, 0)   # ramp step from a1 (0) up to a0 (7) -> BC4 index


def _bc4_steps(vs, hi, lo):
    """(ramp steps, squared error) of 16 values against the 8-value ramp hi > lo."""
    pal = _bc3_alphas(hi, lo)
    ramp = [pal[i] for i in _STEP]
    d, half = hi - lo, (hi - lo) // 2
    steps, err = [], 0
    for v in vs:
        s = ((v - lo) * 7 + half) // d
        s = 0 if s < 0 else 7 if s > 7 else s
        e = ramp[s] - v
        # the ramp is floored: the nearest entry can be the neighbour of the rounded one
        if e > 0 and s and v - ramp[s - 1] < e:
            s -= 1
            e = ramp[s] - v
        elif e < 0 and s < 7 and ramp[s + 1] - v < -e:
            s += 1
            e = ramp[s] - v
        steps.append(s)
        err += e * e
    return steps, err


def _bc4_refit(vs, steps):
    """Least-squares endpoints (hi, lo) for fixed ramp steps, or None."""
    a11 = a12 = a22 = x1 = x2 = 0.0
    for v, s in zip(vs, steps):
        t = s / 7
        a11 += (1 - t) * (1 - t)
        a12 += (1 - t) * t
        a22 += t * t
        x1 += (1 - t) * v
        x2 += t * v
    det = a11 * a22 - a12 * a12
    if det < 1e-6:
        return None
    lo = min(255, max(0, int((a22 * x1 - a12 * x2) / det + 0.5)))
    hi = min(255, max(0, int((a11 * x2 - a12 * x1) / det + 0.5)))
    return (hi, lo) if hi > lo else None


def bc4_block(vs):
    """8 BC4 bytes for 16 values (bytes, 4x4 row-major)."""
    hi, lo = max(vs), min(vs)
    if hi == lo:
        return bytes((hi, lo, 0, 0, 0, 0, 0, 0))
    steps, err = _bc4_steps(vs, hi, lo)
    # ponytail: up to 3 refits land ~18 % SSE above a brute-force endpoint search
    # (~0.7 dB); a +-1 endpoint search halves that gap at twice the time.
    for _ in range(3):
        fit = _bc4_refit(vs, steps) if err else None
        if not fit or fit == (hi, lo):
            break
        steps2, err2 = _bc4_steps(vs, *fit)
        if err2 >= err:
            break
        (hi, lo), steps, err = fit, steps2, err2
    bits = 0
    for i, s in enumerate(steps):
        bits |= _STEP[s] << 3 * i
    return bytes((hi, lo)) + bits.to_bytes(6, "little")


def _bc4_blocks(plane, width, height):
    """[8-byte BC4 block] of a width x height one-byte plane, row-major, edges repeated."""
    plane = bytes(plane)
    out, cache = [], {}
    for by in range(0, height, 4):
        rows = [plane[min(by + r, height - 1) * width:(min(by + r, height - 1) + 1) * width] for r in range(4)]
        for bx in range(0, width, 4):
            if bx + 4 <= width:
                px = rows[0][bx:bx + 4] + rows[1][bx:bx + 4] + rows[2][bx:bx + 4] + rows[3][bx:bx + 4]
            else:
                px = bytes(row[min(x, width - 1)] for row in rows for x in range(bx, bx + 4))
            blk = cache.get(px)
            if blk is None:
                blk = cache[px] = bc4_block(px)
            out.append(blk)
    return out


def encode_bc5(xs, ys, width, height):
    """BC5 bytes from two width x height planes: X (red) and Y (green) of a normal map."""
    return b"".join(map(add, _bc4_blocks(xs, width, height), _bc4_blocks(ys, width, height)))


def encode(fmt, rgba, width, height):
    """BC1, BC4 (red) or BC5 (red = X, green = Y) bytes for a width x height RGBA
    image (edges padded by repetition) -- what tncimage.decode() reads back."""
    base = fmt.replace("_SRGB", "")
    if base not in ("BC1", "BC4", "BC5"):
        raise TncImageError("%s is not encoded (BC1, BC4 and BC5 only)" % fmt)
    rgba = bytes(rgba)
    if len(rgba) != 4 * width * height:
        raise TncImageError("RGBA has %d bytes, %dx%d needs %d"
                            % (len(rgba), width, height, 4 * width * height))
    if base == "BC4":
        return b"".join(_bc4_blocks(rgba[0::4], width, height))
    if base == "BC5":
        return encode_bc5(rgba[0::4], rgba[1::4], width, height)
    out = bytearray()
    cache = {}
    stride = 4 * width
    for by in range(0, height, 4):
        rows = [rgba[min(by + r, height - 1) * stride:(min(by + r, height - 1) + 1) * stride]
                for r in range(4)]
        for bx in range(0, width, 4):
            if bx + 4 <= width:
                px = b"".join(row[4 * bx:4 * bx + 16] for row in rows)
            else:
                px = b"".join(row[4 * min(x, width - 1):4 * min(x, width - 1) + 4]
                              for row in rows for x in range(bx, bx + 4))
            blk = cache.get(px)
            if blk is None:
                blk = cache[px] = encode_block(px)
            out += blk
    return bytes(out)
