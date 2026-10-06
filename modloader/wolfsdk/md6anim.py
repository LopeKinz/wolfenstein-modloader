"""Wolfenstein II .md6skl skeletons and .md6anim clips, decoded for the Studio's model viewer (read only).

Ported from the research decoders, which reproduce the engine's poses (re_probes/agent_reports/r11_humananim.md,
r11_animframes.md, r13_animimport.md): re_probes/humananim/decode2.py (clip layout), re_probes/md6anim/
{anim_header,decode_frames,quatcompress,md6skl}.py and the pose maths of re_probes/animexport/export_anim.py.
Game axes (Z up), metres; quaternions are (x, y, z, w).

  skeleton(data) -> {names, parents, rot: [bind local quat], pos: [bind local vec3]}
  clip(data)     -> {skeleton, fps, frames, R: {joint: [quat]}, T: {joint: [vec3]}}   (raw per-frame channels)
  pose(skel, cl) -> {joint: (quats or None, positions or None)}: local transforms per frame,
                    rotation = D * bind, translation = bind + dT (the S and U channels are not used)
"""
import math
import struct

K1, K0 = math.sqrt(2) / 32767.0, 1 / math.sqrt(2)   # quaternion word scale, rva 0x2999e60 / 0x299a23c
TAG = b"_FRAMESET_"


class AnimError(ValueError):
    pass


# -- skeleton -----------------------------------------------------------------------------------

def _mm(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _tr(a):
    return [[a[j][i] for j in range(3)] for i in range(3)]


def _mv(a, v):
    return [sum(a[i][k] * v[k] for k in range(3)) for i in range(3)]


def _quat_mat(q):
    x, y, z, w = q
    n2 = x * x + y * y + z * z + w * w
    s = 2.0 / n2 if n2 > 1e-12 else 0.0
    xx, yy, zz, xy, xz, yz, wx, wy, wz = x * x * s, y * y * s, z * z * s, x * y * s, x * z * s, y * z * s, w * x * s, w * y * s, w * z * s
    return [[1 - (yy + zz), xy - wz, xz + wy], [xy + wz, 1 - (xx + zz), yz - wx], [xz - wy, yz + wx, 1 - (xx + yy)]]


def _mat_quat(m):
    t = m[0][0] + m[1][1] + m[2][2]
    if t > 0:
        s = math.sqrt(t + 1) * 2
        q = ((m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s, s / 4)
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1 + m[0][0] - m[1][1] - m[2][2]) * 2
        q = (s / 4, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s, (m[2][1] - m[1][2]) / s)
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1 + m[1][1] - m[0][0] - m[2][2]) * 2
        q = ((m[0][1] + m[1][0]) / s, s / 4, (m[1][2] + m[2][1]) / s, (m[0][2] - m[2][0]) / s)
    else:
        s = math.sqrt(1 + m[2][2] - m[0][0] - m[1][1]) * 2
        q = ((m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, s / 4, (m[1][0] - m[0][1]) / s)
    n = math.sqrt(sum(c * c for c in q))
    return tuple(c / n for c in q)


def skeleton(data):
    """md6skl: names, parents (-1 root, always before the child), bind pose as local quaternion + position.
    The table at h2 holds inverse-bind rows (T_i, R_i); the true row translation is (t1_i, t2_i, t0_{i+1})."""
    if len(data) < 0x20:
        raise AnimError("skeleton too small")
    a, _b, h2, h3 = struct.unpack_from("<4I", data, 0)
    n = struct.unpack_from("<H", data, 0x10)[0]
    padded = -(-n // 8) * 8
    if not (0 < n <= 4096 and h2 < h3 < a < len(data)) or h3 - h2 != 48 * padded or a - h3 != 32 * padded:
        raise AnimError("skeleton header does not fit %d joints" % n)
    p, names = a + 4, []
    while p + 4 <= len(data) and len(names) < n:
        k = struct.unpack_from("<I", data, p)[0]
        names.append(data[p + 4:p + 4 + k].decode("ascii", "replace"))
        p += 4 + k
    words = struct.unpack_from("<%dh" % (h2 // 2), data, 0)
    par = next((list(words[s:s + n]) for s in range(len(words) - n + 1)
                if words[s] == -1 and all(words[s + i] < i for i in range(1, n))), None)
    if par is None or len(names) < n:
        raise AnimError("skeleton parents or names not found")
    rows = [struct.unpack_from("<12f", data, h2 + 48 * i) for i in range(n)]
    nxt = struct.unpack_from("<f", data, h2 + 48 * n)[0]
    rw, pw = [], []
    for i, r in enumerate(rows):
        t = (r[4], r[8], rows[i + 1][0] if i + 1 < n else nxt)
        w = _tr([[r[1], r[2], r[3]], [r[5], r[6], r[7]], [r[9], r[10], r[11]]])
        rw.append(w)
        pw.append([-c for c in _mv(w, t)])
    rl = [rw[i] if par[i] < 0 else _mm(_tr(rw[par[i]]), rw[i]) for i in range(n)]
    tl = [pw[i] if par[i] < 0 else _mv(_tr(rw[par[i]]), [x - y for x, y in zip(pw[i], pw[par[i]])]) for i in range(n)]
    return {"names": names[:n], "parents": par, "rot": [_mat_quat(m) for m in rl], "pos": tl, "_rl": rl}


# -- clip -------------------------------------------------------------------------------------------

def _deq(w0, w1, w2):
    sel = ((w1 >> 15) & 1) << 1 | ((w0 >> 15) & 1)
    a, b, c = (w0 & 0x7fff) * K1 - K0, (w1 & 0x7fff) * K1 - K0, (w2 & 0x7fff) * K1 - K0
    s = 1.0 - a * a - b * b - c * c
    if s < 0:
        raise AnimError("undecodable quaternion words")
    arr = (a, b, c, math.sqrt(s))
    return tuple(arr[(sel + k) & 3] for k in range(4))


def _bits(b, n):
    return [(b[i >> 3] >> (7 - (i & 7))) & 1 for i in range(n)]


def _rle_u8(b, o):
    n = b[o]
    o += 1
    out = []
    while len(out) < n:
        c = b[o]
        o += 1
        if c & 0x80:
            out += [None] * (c & 0x7f)
            continue
        out += list(range(b[o], b[o] + c))
        o += 1
    if len(out) != n:
        raise AnimError("joint list overrun")
    return out, o


def _vi(b, i):
    v = s = 0
    while True:
        c = b[i]
        i += 1
        v |= (c & 0x7f) << s
        s += 7
        if not c & 0x80:
            return v, i


def _rle_var(b, o):
    """skeletons with >= 128 joints: varint count, then (varint run, varint first) pairs."""
    n, o = _vi(b, o)
    out = []
    while len(out) < n:
        c, o = _vi(b, o)
        f, o = _vi(b, o)
        if c == 0:
            raise AnimError("empty run")
        out += list(range(f, f + c))
    if len(out) != n:
        raise AnimError("joint list overrun")
    return out, o


def _rle(b, o, want_end):
    for fn in (_rle_u8, _rle_var):
        try:
            got = fn(b, o)
        except (AnimError, IndexError):
            continue
        if want_end is None or got[1] == want_end:
            return got
    raise AnimError("joint list does not end where the next starts")


def skeleton_name(data):
    """The .md6skl an .md6anim is made for (the header's first string)."""
    n = struct.unpack_from("<I", data, 0)[0] if len(data) >= 4 else 0
    if not 0 < n <= 256 or 4 + n > len(data):
        raise AnimError("no skeleton name")
    return data[4:4 + n].decode("ascii", "replace")


def _header_end(data):
    n = struct.unpack_from("<I", data, 0)[0]
    p = 4 + n
    n2 = struct.unpack_from("<I", data, p)[0]
    return p + 4 + n2 + 24 if n2 else p + 4 + 24      # skeleton, optional reference anim, 12 x s16


def clip(data):
    p = _header_end(data)
    _a, _flags, frames, fps, nfs = struct.unpack_from("<5H", data, p + 0x48)
    base = p + 0xc4
    if struct.unpack_from("<H", data, base)[0] != 1:
        raise AnimError("animation map count is not 1")
    off = struct.unpack_from("<9H", data, base + 4)
    lists = []
    for k in range(8):
        end = base + off[k + 1] if k < 7 else (base + off[8] if off[8] else None)
        lists.append(_rle(data, base + off[k], end)[0])
    cR, cS, cT, cU, aR, aS, aT, aU = lists
    v = struct.unpack_from("<14H", data, p + 0x48)
    start = p + 0x34
    const = {"R": [_deq(*struct.unpack_from("<3H", data, start + v[7] + 6 * i)) for i in range(len(cR))],
             "T": [struct.unpack_from("<3f", data, start + v[9] + 12 * i) for i in range(len(cT))]}
    sets, at = [], data.find(TAG)
    while at >= 0:
        s = at + 10 - 0x30
        u = struct.unpack_from("<17H", data, s)
        f0, n = struct.unpack_from("<2H", data, s + 34)
        mb = (n + 7) // 8
        keys = {}
        for k, idx, lst, size in (("R", 0, aR, 6), ("T", 2, aT, 12)):
            masks = [_bits(data[s + u[8 + idx] + i * mb: s + u[8 + idx] + (i + 1) * mb], n) for i in range(len(lst))]
            q, out = s + u[4 + idx], []
            for c in range(len(lst)):
                rd = (lambda o: _deq(*struct.unpack_from("<3H", data, o))) if k == "R" else (lambda o: struct.unpack_from("<3f", data, o))
                d = {0: rd(s + u[idx] + size * c)}
                for i in range(1, n):
                    if masks[c][i]:
                        d[i] = rd(q)
                        q += size
                out.append(d)
            keys[k] = out          # each channel has its own first/key/mask offsets: S and U need not be read
        sets.append({"f0": f0, "n": n, "keys": keys})
        at = data.find(TAG, at + 10)
    if len(sets) != nfs or sum(st["n"] for st in sets) != frames:
        raise AnimError("framesets do not tile %d frames" % frames)
    out = {"skeleton": skeleton_name(data), "fps": fps, "frames": frames, "R": {}, "T": {}}
    for k, cl, al in (("R", cR, aR), ("T", cT, aT)):
        for j, val in zip(cl, const[k]):
            if j is not None:
                out[k][j] = [val] * frames
        for c, j in enumerate(al):
            if j is None:
                continue
            track = out[k].setdefault(j, [])
            for si, st in enumerate(sets):
                nxt = sets[si + 1]["keys"][k][c][0] if si + 1 < len(sets) else None
                ks = st["keys"][k][c]
                for i in range(st["n"]):
                    track.append(_sample(ks, i, _nlerp if k == "R" else _lerp, (st["n"], nxt) if nxt is not None else None))
    return out


def _lerp(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def _nlerp(a, b, t):
    if sum(x * y for x, y in zip(a, b)) < 0:
        b = tuple(-x for x in b)
    q = tuple(x + (y - x) * t for x, y in zip(a, b))
    n = math.sqrt(sum(x * x for x in q))
    return tuple(x / n for x in q)


def _sample(keys, i, mix, end):
    """keys {frame in set: value}, linear in between; after the last key it runs to the next set's first value."""
    if i in keys:
        return keys[i]
    lo = max(k for k in keys if k < i)
    hi = min((k for k in keys if k > i), default=None)
    if hi is None:
        return keys[lo] if end is None else mix(keys[lo], end[1], (i - lo) / (end[0] - lo))
    return mix(keys[lo], keys[hi], (i - lo) / (hi - lo))


def pose(skel, cl):
    """{joint: ([quat per frame] or None, [vec3 per frame] or None)} in local joint space, game axes.
    Joints the clip leaves alone keep the bind pose (not listed). Quaternions stay in one hemisphere per track."""
    out = {}
    for j in set(cl["R"]) | set(cl["T"]):
        if j >= len(skel["names"]):
            continue
        qs = ts = None
        if j in cl["R"]:
            qs, prev = [], None
            for d in cl["R"][j]:
                q = _mat_quat(_mm(_quat_mat(d), skel["_rl"][j]))
                if prev is not None and sum(a * b for a, b in zip(q, prev)) < 0:
                    q = tuple(-c for c in q)
                qs.append(q)
                prev = q
        if j in cl["T"]:
            ts = [tuple(a + b for a, b in zip(skel["pos"][j], d)) for d in cl["T"][j]]
        out[j] = (qs, ts)
    return out
