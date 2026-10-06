"""A game material as metal/rough PBR channels (PNG), for the Studio's model viewer and model export.

    build(mount, material, cap=1024) -> {"slots": {slot: png}, "alpha": "mask" | "mask2" | None,
                                          "roughness": float | None, "emissive": bool,
                                          "specular": factor (hair, F0 / 0.04), "clearcoat": (factor, roughness) (eyes)}

Slots, each only when it carries information:
  mr      G = roughness = (1 - 0.8 gloss)^2 (loosesmoothness, the BC4 "_pm" map; the game's GGX), B = metal from spec
  normal  loosenormal (BC5, Z rebuilt by tncimage), green flipped: the game's maps are DirectX, three.js/glTF OpenGL
  base    the albedo, metal pixels tinted by the spec colour, alpha from loosecover or the hair strand mask
  occ     hair ambient occlusion (spare1map)
  mask    hair strand coverage (sparediffusemap2) for the SECOND uv set (md6.uv2): alpha "mask2"
The game uses a specular-colour workflow; metal/rough is an approximation (re_probes/agent_reports/r11_pbrexport.md).
Skin, hair and eye programs as their shader text reads them: re_probes/agent_reports/r12_skinmat.md (no SSS in glTF).
Normal and gloss maps come at up to DETAIL_MAX px; their larger mips are tile chains (codec 2, wolfsdk/tilechain.py).
"""
from . import image, maptex, tncimage

VERSION = 5                       # part of the cache name: bump when the mapping changes
LIN = [((i / 255 + 0.055) / 1.055) ** 2.4 if i > 10 else i / 255 / 12.92 for i in range(256)]
METAL_LO, METAL_HI = 0.12, 0.40   # linear spec luminance; dielectrics sit near 0.04
MR_MAX = 512
DETAIL_MAX = 1024                 # normal + gloss: their >= 512 px mips are tile chains (maptex/tilechain, 2-4 s once)
ROUGH = bytes(max(10, round(255 * (1 - 0.8 * i / 255) ** 2)) for i in range(256))   # specBRDF: alpha = (1-.8s)^4 = roughness^2
FLIP = bytes(range(255, -1, -1))  # 255 - v, as bytes.translate tables: per-pixel work in C


def _decl(mount, name):
    hit = mount.find("material", name)
    text = mount.read(*hit).decode("utf-8", "replace") if hit else ""
    return {k: v.strip().strip('"') for k, v in (line.strip().split("\t", 1) for line in text.splitlines() if "\t" in line)}


def _image(mount, name, cap):
    """(w, h, rgba) of image `name` at its largest servable mip <= cap, or None."""
    hit = mount.find("image", name) if name else None
    if hit is None:
        return None
    try:
        return maptex.mip_rgba(mount, "%016x" % hit[1].hash_0x60, cap)
    except Exception:  # noqa: BLE001 -- a channel that cannot be decoded is left out, the albedo still shows
        return None


def _vec(d, key, n=4):
    """"{ a, b, c, d }" decl value -> list of floats, zeros when absent."""
    try:
        v = [float(x) for x in d[key].strip("{} ").split(",")]
    except (KeyError, ValueError):
        v = []
    return (v + [0.0] * n)[:n]


def _flat(img):
    w, h, px = img
    return px == bytes(px[:4]) * (w * h)


def _channel(img, w, h, ch):
    """Channel `ch` of img, nearest-sampled to w x h."""
    sw, sh, px = img
    src = px[ch::4]
    if (sw, sh) == (w, h):
        return bytearray(src)
    xs = [x * sw // w for x in range(w)]
    out = bytearray(w * h)
    for y in range(h):
        row = src[(y * sh // h) * sw:(y * sh // h + 1) * sw]
        out[y * w:(y + 1) * w] = bytes(row[x] for x in xs)
    return out


def _metal(spec, w, h):
    """Metal 0..255 per pixel from the spec colour's linear luminance (smoothstep METAL_LO..METAL_HI)."""
    r, g, b = (_channel(spec, w, h, c) for c in range(3))
    out = bytearray(w * h)
    for i in range(w * h):
        t = (0.2126 * LIN[r[i]] + 0.7152 * LIN[g[i]] + 0.0722 * LIN[b[i]] - METAL_LO) / (METAL_HI - METAL_LO)
        out[i] = 0 if t <= 0 else 255 if t >= 1 else int(t * t * (3 - 2 * t) * 255)
    return out


def build(mount, material, cap=1024):
    d = _decl(mount, material)
    prog = d.get("ambientprogram", "")
    hair, eye = prog == "hairnew", prog == "eye"
    out = {"slots": {}, "alpha": None, "roughness": None, "emissive": "emissive" in prog}
    if hair:                          # hairnew: F0 = hairparms.x (0.002, not 0.04), aniso lobes m = .6/.5 -> broad streak
        out["specular"] = min(1.0, _vec(d, "hairparms")[0] / 0.04)
        out["roughness"] = 0.75
    if eye:                           # two lobes: cornea F0 .05 smoothness .7 (clearcoat), iris smoothness .35
        ep = _vec(d, "eyeparms")
        out["roughness"] = (1 - 0.8 * ep[3]) ** 2
        out["clearcoat"] = (1.0, (1 - 0.8 * ep[1]) ** 2)
    gloss = None if hair or eye else _image(mount, d.get("loosesmoothness"), DETAIL_MAX)
    spec = _image(mount, d.get("loosespec"), MR_MAX)
    if spec and (_flat(spec) or d.get("packedspec", "0") not in ("0", "0.000000")):
        spec = None                   # flat: no metal; packedspec (skin): R = SSS thickness, G = F0 (grey), B unused -> dielectric
    if gloss or spec:
        w, h = (gloss or spec)[:2]
        rough = _channel(gloss, w, h, 0).translate(ROUGH) if gloss else bytearray([128]) * (w * h)
        metal = _metal(spec, w, h) if spec else bytearray(w * h)
        if any(metal) or (gloss and not _flat(gloss)):
            px = bytearray(b"\xff" * (4 * w * h))
            px[1::4], px[2::4] = rough, metal
            out["slots"]["mr"] = image.to_png(w, h, px)
        else:
            out["roughness"] = rough[0] / 255
    alb = _image(mount, d.get("sparediffusemap" if hair or eye else "loosealbedo"), cap)
    if eye and alb:                   # iris: albedo -> luma * eyeirisalbedo.rgb, weight sat(eyeirisalbedo.a * map.a * 8) (map.a = iris mask/height)
        w, h, px = alb
        px = bytearray(px)
        ia = _vec(d, "eyeirisalbedo")
        for i in range(w * h):
            t = min(1.0, ia[3] * px[4 * i + 3] / 255 * 8)
            if t:
                lin = [LIN[px[4 * i + c]] for c in range(3)]
                y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
                for c in range(3):
                    v = lin[c] * (1 - t) + y * ia[c] * t
                    px[4 * i + c] = round(255 * (v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055))
            px[4 * i + 3] = 255
        alb = (w, h, bytes(px))
    mask = _image(mount, d.get("sparediffusemap2"), cap) if hair else _image(mount, d.get("loosecover"), cap)
    if mask and _flat(mask):
        mask = None
    if hair and mask:                 # the strand mask lies on the second uv set (md6.uv2): its own map, not in the base alpha
        # hairprez: clip(mask - bayer*hairparms.w - 1/255) -> coverage = sat(mask / hairparms.w)
        w, h, px = mask
        k = max(_vec(d, "hairparms")[3], 1e-3)
        cov = px[0::4].translate(bytes(min(255, int(v / k)) for v in range(256)))
        px = bytearray(b"\xff" * (4 * w * h))
        px[0::4] = px[1::4] = px[2::4] = px[3::4] = cov
        out["slots"]["mask"] = image.to_png(w, h, px)
        out["alpha"] = "mask2"
        mask = None
    if alb and (mask or eye or "mr" in out["slots"] and spec):
        w, h, px = alb
        px = bytearray(px)
        if spec:                      # metal pixels take the spec colour as base colour (spec workflow -> metal/rough)
            met = _metal(spec, w, h)
            sr, sg, sb = (_channel(spec, w, h, c) for c in range(3))
            for i in range(w * h):
                t = met[i]
                if t:
                    j = 4 * i
                    px[j] = (px[j] * (255 - t) + sr[i] * t) // 255
                    px[j + 1] = (px[j + 1] * (255 - t) + sg[i] * t) // 255
                    px[j + 2] = (px[j + 2] * (255 - t) + sb[i] * t) // 255
        if mask:
            px[3::4] = _channel(mask, w, h, 0)
            out["alpha"] = "mask"
        out["slots"]["base"] = image.to_png(w, h, px)
    nrm = _image(mount, d.get("sparebumpmap" if eye else "loosenormal"), DETAIL_MAX)
    if nrm and not _flat(nrm):
        w, h, px = nrm
        px = bytearray(px)
        px[1::4] = px[1::4].translate(FLIP)
        out["slots"]["normal"] = image.to_png(w, h, px)
    occ = _image(mount, d.get("spare1map"), cap) if hair else None
    if occ:                           # hairnew: colour *= sat(mix(1, ao.r^2, hairparms.z)) -> baked, strength 1
        w, h, px = occ
        z = min(1.0, max(0.0, _vec(d, "hairparms")[2]))
        px = bytearray(px)
        px[0::4] = px[1::4] = px[2::4] = px[0::4].translate(bytes(round(255 * (1 - z + z * (i / 255) ** 2)) for i in range(256)))
        out["slots"]["occ"] = image.to_png(w, h, px)
    return out
