#!/usr/bin/env python3
"""Generate the shared test vectors (SPEC §12).

Run from anywhere:  python spec/vectors/generate.py
Set WOLFSDK_VECTORS_OUT to write somewhere else (CI smoke test).

Uses the Python reference binding in sdk/python/src. Everything is
deterministic; re-running must not change any committed file.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "sdk", "python", "src"))
OUT = os.environ.get("WOLFSDK_VECTORS_OUT") or HERE

from wolfsdk import (  # noqa: E402
    Decl, DeclError, Game, Mod, apply_mods, build_bim, build_chunk_index,
    build_master_index, decode, deflate_exact, deflate_sync, format_path,
    parse_bim, parse_bsnf, parse_path, save_checksum,
)
from wolfsdk.decl import format_value, Node  # noqa: E402


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def write(rel: str, data) -> None:
    p = os.path.join(OUT, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
    kw = {} if mode == "wb" else {"encoding": "utf-8", "newline": ""}
    with open(p, mode, **kw) as f:
        f.write(data)


def dump(rel: str, obj) -> None:
    write(rel, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


class Lcg:
    def __init__(self, seed: int) -> None:
        self.x = seed

    def byte(self) -> int:
        self.x = (self.x * 1103515245 + 12345) & 0x7FFFFFFF
        return (self.x >> 16) & 0xFF

    def bytes(self, n: int) -> bytes:
        return bytes(self.byte() for _ in range(n))


# ------------------------------------------------------------------ decls

SHOTGUN = """{
\tinherit = "weapon/base";
\tedit = {
\t\tisDualWieldable = true;
\t\tammoPerShot = 1;
\t\thandsFovScale = 0.90;
\t\thandsModelMD6 = "weapons/shotgun.md6";
\t\tinitialAmmoDecl = "ammo/shotgun";
\t\tfiringIntervals = {
\t\t\tnum = 3;
\t\t\tfiringIntervals[0] = 200;
\t\t\tfiringIntervals[1] = 200;
\t\t\tfiringIntervals[2] = 200;
\t\t}
\t\tvalidAmmoClips = {
\t\t\tnum = 1;
\t\t\titem[0] = {
\t\t\t\tclipSize = 20;
\t\t\t}
\t\t}
\t}
}
"""

MG60 = """{
\tedit = {
\t\tdamageParms = {
\t\t\tminDamage = 12.5;
\t\t\tmaxDamage = 17.500000;
\t\t\tragdollImpulseMag = 500;
\t\t\tgoreTypes = "GORETYPE_SMALL_WOUND GORETYPE_BIG_WOUND";
\t\t\tdeclGoreBehavior = "standard/heavy";
\t\t}
\t}
}
"""

HANDGUN = """{
\tdiffusemap\t"models/weapons/Handgun/Handgun"
\tspecularmap\t"models/weapons/Handgun/Handgun_s"
\tvirtualmapping { 0.015625, 0.015625, 0.078125, 0.031250 }
\tusevirtualmapping 1
}
"""

MATERIAL = """// material with the no-'=' dialect
{
\tsurfacetype 5.000000
\ttwoSided
\tsssblurscale 2.5 1.0 0.6 0.1
\tvirtualmapping { 0.015625, 0.015625, 0.078125, 0.031250 }
\toffset ( 0 0 0 )
\tprop "_info" {
\t\ttag "muzzle" { trans ( 0 0 0 ) rot ( 1 0 0 ( 0 1 0 ) ) }
\t\ttag "eject" { trans ( 1 2.5 -3 ) }
\t}
\t/* block
\t   comment */ bumpmap "models/x_local"
\tpowermip -1
\tscale 1.0f
}
"""

TABLE = "{ spline clamp min 0.5 max 0.5 left 0 right 1 {0:0, 1:1} }"

SKINS = """{
\twhite {
\t\t"models/weapons/handgun/handgun_hg" "models/weapons/handgun/handgun_white_hg"
\t\t"models/weapons/handgun/handgun_silencer" "models/weapons/handgun/handgun_silencer_white"
\t}
}
"""

MD6 = """init {
\tinherit "characters/standard/base.md6"
\tmultimesh { base "md6/models/soldier.md6mesh" }
}
joints {
\tjoint "head" { parent "neck" }
\tjoint "head" { parent "spine" }
}
"""

DUPLICATES = """{
\tedit = {
\t\tdup = 1; dup = 2; dup = 3;
\t\tpath = "C:\\\\games\\\\\\"quoted\\"";
\t\tlist = { "a", "b", c }
\t\tempty = { }
\t\t;; , ; flag2 ;
\t}
}
trailing = 7;
"""

DECLS = {
    "shotgun.decl": SHOTGUN,
    "mg60.decl": MG60,
    "handgun.decl": HANDGUN,
    "material.decl": MATERIAL,
    "table.decl": TABLE,
    "skins.decl": SKINS,
    "md6.decl": MD6,
    "duplicates.decl": DUPLICATES,
    "crlf.decl": SHOTGUN.replace("\n", "\r\n"),
}

EDITS = [
    ("shotgun.decl", [["edit.firingIntervals.firingIntervals[0]", 120],
                      ["edit.firingIntervals.firingIntervals[2]", 120],
                      ["edit.validAmmoClips.item[0].clipSize", 40],
                      ["edit.isDualWieldable", False],
                      ["edit.handsFovScale", 1],
                      ["inherit", "weapon/other"]]),
    ("mg60.decl", [["edit.damageParms.minDamage", 22],
                   ["edit.damageParms.maxDamage", 30.25],
                   ["edit.damageParms.ragdollImpulseMag", 1200],
                   ["edit.damageParms.goreTypes", 'GORETYPE_NONE "x"']]),
    ("handgun.decl", [["diffusemap", "models/weapons/Handgun/Handgun_white_hg"],
                      ["virtualmapping", "{ 0.015625, 0.015625, 0.000000, 0.062500 }"]]),
    ("handgun.decl", [["virtualmapping.[3]", 0.0625], ["usevirtualmapping", 0]]),
    ("material.decl", [["surfacetype", 7], ["sssblurscale", "1 1 1 1"],
                       ["prop:_info.tag:eject.trans", "( 0 0 0 )"], ["scale", 2],
                       ["powermip", -2.5], ["bumpmap", "models/y"]]),
    ("table.decl", [["min", 0], ["max", 0]]),
    ("skins.decl", [['white."models/weapons/handgun/handgun_hg"', "models/other"]]),
    ("md6.decl", [["init.multimesh.base", "md6/models/other.md6mesh"],
                  ["joints.joint:head#1.parent", "pelvis"]]),
    ("duplicates.decl", [["edit.dup#2", 30], ["edit.path", "D:\\x"], ["trailing", 0.0000001]]),
    ("crlf.decl", [["edit.validAmmoClips.item[0].clipSize", 99]]),
]

DECL_ERRORS = [
    "{ a = 1;",
    "{ a = \"unterminated }",
    "{ a = ; }",
    "{ a = }",
    "{ a = ( 1 2 }",
]

PATH_CASES = [
    ["edit.damageParms.maxDamage", ["edit", "damageParms", "maxDamage"]],
    ["edit.validAmmoClips.item[0].clipSize", ["edit", "validAmmoClips", "item[0]", "clipSize"]],
    ["props.prop:_info.tag:muzzle.trans", ["props", "prop:_info", "tag:muzzle", "trans"]],
    ["virtualmapping.[2]", ["virtualmapping", "[2]"]],
    ['white."models/a.b"', ["white", "models/a.b"]],
    ['"a b"."c\\"d".e', ["a b", 'c"d', "e"]],
    ['"x\\\\y"', ["x\\y"]],
]

PATH_ERRORS = ["", "a..b", ".a", "a.", '"open']

FORMAT_CASES = [
    # raw, kind, value, expected
    ["17.500000", "word", 22, "22.000000"],
    ["17.500000", "word", 0.0625, "0.062500"],
    ["17.500000", "word", 0.1234567, "0.1234567"],
    ["25", "word", 30, "30"],
    ["25", "word", 30.5, "30.5"],
    ["25.", "word", 3, "3."],
    ["1.0f", "word", 2, "2.0f"],
    ["5f", "word", 2.5, "2.5f"],
    ["1.5e3", "word", 2, "2.0"],
    ["0.1", "word", 0, "0.0"],
    ["0.1", "word", -0.0, "0.0"],
    ["x", "word", 0.0000001, "0.0000001"],
    ["x", "word", 1e21, "1000000000000000000000"],
    ["x", "word", -3, "-3"],
    ["x", "word", 123456789.125, "123456789.125"],
    ["x", "word", 0.1 + 0.2, "0.30000000000000004"],
    ["true", "word", True, "true"],
    ["1", "word", False, "false"],
    ['"a"', "string", "b", '"b"'],
    ['"a"', "string", 'q"\\', '"q\\"\\\\"'],
    ["word", "word", "other", "other"],
    ["{ 1 }", "block", "{ 2 }", "{ 2 }"],
    ["( 1 )", "tuple", "( 2 )", "( 2 )"],
    ["1 2 3", "vector", 4, "4"],
    ['"a"', "string", 5, "5"],
]


def gen_decl() -> None:
    cases = []
    for fname, text in DECLS.items():
        write(f"decl/{fname}", text)
        d = Decl(text)
        assert d.text == text
        cases.append({"file": fname, "paths": [list(p) for p in d.paths()],
                      "blocks": [n.path_str for n in d.nodes() if n.kind == "block"]})
    edits = []
    for i, (fname, sets) in enumerate(EDITS):
        d = Decl(DECLS[fname])
        for p, v in sets:
            d.set(p, v)
        out = f"edit{i}.out"
        write(f"decl/{out}", d.text)
        edits.append({"file": fname, "set": sets, "expect": out})
    errors = []
    for t in DECL_ERRORS:
        try:
            Decl(t)
        except DeclError:
            errors.append(t)
        else:
            raise AssertionError(f"expected DeclError for {t!r}")
    for p in PATH_ERRORS:
        try:
            parse_path(p)
        except DeclError:
            pass
        else:
            raise AssertionError(f"expected DeclError for path {p!r}")
    for s, segs in PATH_CASES:
        assert list(parse_path(s)) == segs, (s, parse_path(s))
        assert format_path(tuple(segs)) == s, (segs, format_path(tuple(segs)))
    set_errors = [
        {"file": "shotgun.decl", "path": "edit.nope", "value": 1},
        {"file": "material.decl", "path": "twoSided", "value": 1},
    ]
    for c in set_errors:
        try:
            Decl(DECLS[c["file"]]).set(c["path"], c["value"])
        except DeclError:
            pass
        else:
            raise AssertionError(c)
    fmt = []
    for raw, kind, value, expected in FORMAT_CASES:
        got = format_value(Node((), kind, 0, len(raw), raw), value)
        assert got == expected, (raw, value, got, expected)
        fmt.append({"raw": raw, "kind": kind, "value": value, "expected": expected})
    dump("decl.json", {"parse": cases, "edits": edits, "parseErrors": errors,
                       "setErrors": set_errors, "paths": PATH_CASES, "pathErrors": PATH_ERRORS})
    dump("format.json", fmt)


# ------------------------------------------------------------------ images

def gen_bim_files():
    rng = Lcg(2026)
    out = {}
    hdr = lambda w, h, mips, fmt: [0, w, h, 1, mips, 0, fmt, 0, 0, 0, 0, w, h]

    rgba = rng.bytes(4 * 2 * 4)
    out["rgba8.bim"] = build_bim(0x11223344, hdr(4, 2, 1, 3), [(4, 2, rgba)])

    a8 = rng.bytes(5 * 3)
    out["a8.bim"] = build_bim(0x55, hdr(5, 3, 1, 5), [(5, 3, a8)])

    # BC1 8x8 + 4x4 mip, including 3-colour blocks (c0 <= c1)
    blocks = bytearray()
    for i in range(4):
        c0, c1 = struct.unpack("<HH", rng.bytes(4))
        if i % 2:
            c0, c1 = min(c0, c1), max(c0, c1)  # 3-colour mode
        else:
            c0, c1 = max(c0, c1), min(c0, c1)
        blocks += struct.pack("<HH", c0, c1) + rng.bytes(4)
    equal = struct.pack("<HH", 0x1234, 0x1234) + bytes([0xFF, 0xFF, 0xFF, 0xFF])
    out["bc1.bim"] = build_bim(7, hdr(8, 8, 2, 10), [(8, 8, bytes(blocks)), (4, 4, equal)])

    # BC3 6x5 (partial blocks) — both alpha modes
    blocks = bytearray()
    for i in range(4):
        a = rng.bytes(2)
        a0, a1 = (max(a), min(a)) if i % 2 == 0 else (min(a), max(a))
        c0, c1 = struct.unpack("<HH", rng.bytes(4))
        c0, c1 = min(c0, c1), max(c0, c1)  # BC3 colour is always 4-colour
        blocks += bytes([a0, a1]) + rng.bytes(6) + struct.pack("<HH", c0, c1) + rng.bytes(4)
    out["bc3.bim"] = build_bim(9, hdr(6, 5, 1, 11), [(6, 5, bytes(blocks))])

    # declares 3 mips but only 1 present → reading stops early
    out["short.bim"] = build_bim(1, hdr(1, 1, 3, 3), [(1, 1, b"\x01\x02\x03\x04")])
    return out


def _png_filtered(w, h, pixels: bytes, channels: int, ctype: int) -> bytes:
    """PNG writer that cycles through all five filter types (for decode tests)."""
    from wolfsdk.png import SIGNATURE, _chunk, _paeth
    stride = w * channels
    raw = bytearray()
    prev = bytes(stride)
    for y in range(h):
        line = pixels[y * stride:(y + 1) * stride]
        f = y % 5
        enc = bytearray()
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            pred = [0, a, b, (a + b) >> 1, _paeth(a, b, c)][f]
            enc.append((line[i] - pred) & 0xFF)
        raw += bytes([f]) + enc
        prev = line
    comp = zlib.compress(bytes(raw), 9)
    half = len(comp) // 2
    ihdr = struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)
    return (SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"tEXt", b"k\x00v") +
            _chunk(b"IDAT", comp[:half]) + _chunk(b"IDAT", comp[half:]) + _chunk(b"IEND", b""))


def gen_images() -> dict:
    from wolfsdk.png import decode_png
    files = gen_bim_files()
    cases = []
    for name, data in files.items():
        write(f"bim/{name}", data)
        b = parse_bim(data)
        w, h, rgba = decode(b)
        cases.append({"file": name, "hash": b.hash, "header": b.header,
                      "mips": [[m.width, m.height, len(m.data)] for m in b.mips],
                      "width": w, "height": h, "rgbaSha256": sha(rgba), "rgbaHex": rgba.hex() if len(rgba) <= 64 else None})
    write("bim/bad_magic.bim", b"\x00" * 8 + b"\x00" * 52)
    tmpl = parse_bim(files["bc1.bim"])
    enc = __import__("wolfsdk").encode_rgba8(tmpl, 2, 1, bytes(range(8)))
    write("bim/encoded.bim", enc)
    rng = Lcg(7)
    pngs = []
    for name, (w, h, ch, ct) in {"rgba.png": (7, 6, 4, 6), "rgb.png": (5, 5, 3, 2),
                                  "grey.png": (3, 5, 1, 0), "greyalpha.png": (4, 5, 2, 4)}.items():
        px = rng.bytes(w * h * ch)
        data = _png_filtered(w, h, px, ch, ct)
        write(f"png/{name}", data)
        dw, dh, rgba = decode_png(data)
        pngs.append({"file": name, "width": dw, "height": dh, "rgbaSha256": sha(rgba)})
    dump("bim.json", {"decode": cases, "badMagic": ["bad_magic.bim"],
                      "encode": {"template": "bc1.bim", "width": 2, "height": 1,
                                 "rgbaHex": bytes(range(8)).hex(), "expect": "encoded.bim"},
                      "png": pngs})
    return files


# ------------------------------------------------------------------ audio

def ogg_page(flags: int, body: bytes, seq: int) -> bytes:
    segs = []
    n = len(body)
    while n >= 255:
        segs.append(255)
        n -= 255
    segs.append(n)
    head = b"OggS" + bytes([0, flags]) + struct.pack("<qIII", seq * 100, 0x1234, seq, 0)
    return head + bytes([len(segs)]) + bytes(segs) + body


def ogg_stream(rng: Lcg, pages: int, size: int) -> bytes:
    out = b""
    for i in range(pages):
        flags = (0x02 if i == 0 else 0) | (0x04 if i == pages - 1 else 0)
        out += ogg_page(flags, rng.bytes(size + i * 37), i)
    return out


def bsnf_block(h: int, length: int, granule: int, channels: int) -> bytes:
    return (struct.pack(">IIIII", h, 0, granule, 0, length) +
            struct.pack("<HHIIHHHI", 0x674F, channels, 48000, 96000 * channels, 2 * channels, 16, 4, granule))


def bsnf_mono(h, length, granule, channels) -> bytes:
    return b"bsnf" + struct.pack(">I", 1) + b"\x00" * 16 + struct.pack(">II", length, 32) + bsnf_block(h, length, granule, channels)


def bsnf_multi(entries) -> bytes:
    head = b"bsnf" + struct.pack(">I", len(entries))
    table, blocks = b"", b""
    base = 8 + 24 * len(entries)
    for i, (lang, h, length, granule, ch) in enumerate(entries):
        table += lang.encode().ljust(16, b"\x00") + struct.pack(">II", length, base + 42 * i)
        blocks += bsnf_block(h, length, granule, ch)
    return head + table + blocks


def gen_audio():
    rng = Lcg(99)
    s1 = ogg_stream(rng, 3, 300)
    s2 = ogg_stream(rng, 1, 50)
    container = bytearray(s1)
    container += b"\x00" * ((-len(container)) % 16384)
    off2 = len(container)
    container += s2
    container += b"\x00" * ((-len(container)) % 16384)
    eng1 = ogg_stream(rng, 2, 700)
    english = eng1 + b"\x00" * ((-len(eng1)) % 16384)

    mono = bsnf_mono(0xCAFEBABE, len(s2), 38822, 2)
    multi = bsnf_multi([("english", 1, len(eng1), 48000, 1), ("french", 2, 1000, 24000, 1),
                        ("italian", 3, 2000, 12000, 2), ("spanish", 4, 3000, 96000, 2)])
    assert len(mono) == 74 and len(multi) == 272
    write("audio/mono.bsnf", mono)
    write("audio/multi.bsnf", multi)
    write("audio/streamed.bin", bytes(container))
    dump("audio.json", {
        "bsnf": [{"file": "mono.bsnf", "samples": [s.__dict__ for s in parse_bsnf(mono)]},
                 {"file": "multi.bsnf", "samples": [s.__dict__ for s in parse_bsnf(multi)]}],
        "ogg": {"file": "streamed.bin", "streams": [[0, len(s1)], [off2, len(s2)]], "badOffset": 5},
        "containers": [["sound/vo/english/ai/x.wav", "english.streamed"],
                       ["sound/projectile/imp_01.wav", "streamed.resources"],
                       ["sound/vo/french/x.wav", "streamed.resources"]],
    })
    return container, off2, mono, english, multi


# ------------------------------------------------------------------ archive

def trailer(seq: int, stream_offset=None) -> bytes:
    if stream_offset is None:
        return b"\x00" * 9 + struct.pack(">I", seq)
    return b"\x00" * 20 + struct.pack(">I", stream_offset) + b"\x00" * 12 + struct.pack(">I", seq)


def build_resources(items):
    """items: list of (payload_bytes, stored?, slack). Returns (blob, triples)."""
    blob = bytearray(b"\x03SER" + b"\x00" * 12)
    triples = []
    for data, stored, slack in items:
        if stored:
            stream, payload = data, data
        else:
            s = deflate_sync(data)
            stream, payload = deflate_exact(data, len(s) + slack, True) if slack else (s, data)
        triples.append((len(blob), len(payload), len(stream)))
        blob += stream
        blob += b"\x00" * ((-len(blob)) % 16)
    return bytes(blob), triples


def gen_archive(images: dict, audio) -> None:
    container, off2, mono, english, multi = audio
    gdir = os.path.join(OUT, "game")
    if os.path.isdir(gdir):
        shutil.rmtree(gdir)
    chunks = {
        "chunk0": [
            ("weapon", "weapon/shotgun_base", "generated/decls/weapon/weapon/shotgun_base.decl", SHOTGUN.encode(), False, 0, None),
            ("damage", "damage/tungsten/mg60", "generated/decls/damage/damage/tungsten/mg60.decl", MG60.encode(), False, 300, None),
            ("material", "models/weapons/handgun/handgun_hg", "generated/decls/material/models/weapons/handgun/handgun_hg.decl", HANDGUN.encode(), True, 0, None),
            ("image", "ui/test_rgba8", "generated/image/ui/test_rgba8.bimage", images["rgba8.bim"], False, 0, None),
            ("sample", "sound/test/imp_01.wav", "generated/sound/test/imp_01_vorbis.bsnd", mono, True, 0, off2),
        ],
        "chunk1": [
            ("image", "ui/test_bc1", "generated/image/ui/test_bc1.bimage", images["bc1.bim"], False, 0, None),
            ("weapon", "weapon/shotgun_base", "generated/decls/weapon/weapon/shotgun_base.decl", SHOTGUN.encode(), False, 0, None),
            ("sample", "sound/vo/english/test/line_01.wav", "generated/sound/vo/english/test/line_01_vorbis.bsnd", multi, True, 0, 0),
            ("image", "ui/test_bc3", "generated/image/ui/test_bc3.bimage", images["bc3.bim"], False, 0, None),
            ("skins", "weapons/handgun", "generated/decls/skins/weapons/handgun.decl", SKINS.encode(), False, 40, None),
        ],
    }
    pairs = [(f"{c}.index", f"{c}.resources") for c in chunks] + [("chunk9.index", "chunk9.resources")]
    write("game/base/master.index", build_master_index(pairs))
    expected = {"pairs": [list(p) for p in pairs], "chunks": []}
    seq = 0
    for cname, items in chunks.items():
        blob, triples = build_resources([(d, st, sl) for _, _, _, d, st, sl, _ in items])
        entries = []
        for (typ, name, path, data, st, sl, so), (off, us, cs) in zip(items, triples):
            seq += 1
            entries.append((typ, name, path, off, us, cs, trailer(seq, so)))
        idx = build_chunk_index(entries, counter_a=len(entries) - 1)
        write(f"game/base/{cname}.index", idx)
        write(f"game/base/{cname}.resources", blob)
    write("game/base/streamed.resources", container)
    write("game/base/english.streamed", english)

    g = Game(gdir)
    for c in g.chunks:
        ents = []
        for e in c.entries:
            payload = g.read(__import__("wolfsdk").Occurrence(c, e))
            ents.append({"index": e.index, "type": e.type, "name": e.name, "path": e.path,
                         "offset": e.offset, "usize": e.usize, "csize": e.csize, "start": e.start,
                         "tripleOffset": e.triple_offset, "trailerHex": e.trailer.hex(),
                         "streamOffset": e.stream_offset, "compressed": e.compressed,
                         "payloadSha256": sha(payload)})
        expected["chunks"].append({"index": c.index_name, "resources": c.resources_name,
                                   "counterA": c.index.counter_a, "counterB": c.index.counter_b,
                                   "resourcesSize": os.path.getsize(c.resources_path),
                                   "entries": ents})
    expected["find"] = {"weapon:weapon/shotgun_base": ["chunk0.index", "chunk1.index"],
                        "damage:damage/tungsten/mg60": ["chunk0.index"],
                        "weapon:nope": []}
    # write scenarios (performed on a temporary copy by each binding)
    new_mg60 = Decl(MG60).set("edit.damageParms.maxDamage", 30).text
    expected["writeInPlace"] = [
        {"asset": "damage:damage/tungsten/mg60", "text": new_mg60, "expect": "fits"},
        {"asset": "material:models/weapons/handgun/handgun_hg",
         "text": HANDGUN.replace("Handgun_s", "Handgun_x"), "expect": "fits"},
        {"asset": "material:models/weapons/handgun/handgun_hg", "text": HANDGUN + " ", "expect": "nofit"},
        {"asset": "weapon:weapon/shotgun_base", "text": SHOTGUN + "// much longer\n" * 40, "expect": "nofit"},
    ]
    expected["rebuild"] = {"chunk": "chunk0.index", "asset": "weapon:weapon/shotgun_base",
                           "text": SHOTGUN + "// much longer\n" * 40}
    dump("archive.json", expected)


# ------------------------------------------------------------------ mods

def gen_mods() -> None:
    base = {"weapon:weapon/shotgun_base": SHOTGUN, "damage:damage/tungsten/mg60": MG60,
            "material:models/weapons/handgun/handgun_hg": HANDGUN}
    mods = {
        "low": {"name": "low", "priority": 1, "game": "tno", "assets": {
            "weapon:weapon/shotgun_base": {"set": {"edit.validAmmoClips.item[0].clipSize": 30,
                                                   "edit.ammoPerShot": 2}},
            "damage:damage/tungsten/mg60": {"set": {"edit.damageParms.maxDamage": 25}},
            "material:models/weapons/handgun/handgun_hg": {"set": {"usevirtualmapping": 0}}}},
        "high": {"name": "high", "priority": 5, "assets": {
            "weapon:weapon/shotgun_base": {"set": {"edit.validAmmoClips.item[0].clipSize": 50}},
            "material:models/weapons/handgun/handgun_hg": {"file": "handgun_white.decl"}}},
        "legacy": {"name": "legacy", "spiel": "tno",
                   "damage:damage/tungsten/mg60": {"file": "mg60.decl",
                                                   "set": {"edit.damageParms.minDamage": 1}},
                   "weapon:weapon/missing": {"set": {"edit.x": 1}},
                   "weapon:weapon/shotgun_base": {"set": {"edit.unknown": 1}}},
        "mid": {"name": "mid", "priority": 3, "assets": {
            "material:models/weapons/handgun/handgun_hg": {"file": "handgun_blue.decl"}}},
    }
    files = {
        "high/handgun_white.decl": HANDGUN.replace("Handgun\"", "Handgun_white_hg\""),
        "mid/handgun_blue.decl": HANDGUN.replace("Handgun\"", "Handgun_blue\""),
        "legacy/mg60.decl": MG60.replace("500", "900"),
    }
    for k, v in base.items():
        write(f"mod/base/{k.replace(':', '__').replace('/', '_')}.decl", v)
    for name, m in mods.items():
        dump(f"mod/{name}/mod.json", m)
    for rel, text in files.items():
        write(f"mod/{rel}", text)
    order = ["low", "high", "legacy", "mid"]
    loaded = [Mod.load(os.path.join(OUT, "mod", n)) for n in order]
    res = apply_mods(loaded, lambda t, n: base.get(f"{t}:{n}"))
    expected_files = {}
    for k, text in res.results.items():
        fn = f"expected/{k.replace(':', '__').replace('/', '_')}.decl"
        write(f"mod/{fn}", text)
        expected_files[k] = fn
    invalid = [
        ["not an object", "[]"],
        ["bad priority", '{"priority": "high", "assets": {}}'],
        ["bad game", '{"game": "doom", "assets": {}}'],
        ["no change", '{"assets": {"a:b": {}}}'],
        ["bad key", '{"assets": {"nocolon": {"set": {}}}}'],
        ["bad value", '{"assets": {"a:b": {"set": {"x": [1]}}}}'],
        ["null value", '{"assets": {"a:b": {"set": {"x": null}}}}'],
        ["bad json", "{"],
    ]
    for _, t in invalid:
        try:
            Mod.loads(t)
        except Exception as e:  # noqa: BLE001
            assert type(e).__name__ == "ModError", (t, e)
        else:
            raise AssertionError(t)
    dump("mod.json", {
        "base": {k: f"base/{k.replace(':', '__').replace('/', '_')}.decl" for k in base},
        "order": order,
        "sorted": [m.name for m in sorted(loaded, key=lambda m: m.priority)],
        "results": expected_files,
        "resultOrder": list(res.results),
        "conflicts": [c.to_dict() for c in res.conflicts],
        "errors": [{"asset": e.asset, "mod": e.mod} for e in res.errors],
        "invalid": invalid,
    })


# ------------------------------------------------------------------ misc

def gen_save() -> None:
    rng = Lcg(5)
    payloads = [b"", b"a", bytes(range(256)), b"\x05\x00MD&\x00\x00\x00\x01\x00" + b"2018" * 10, rng.bytes(1000)]
    dump("save.json", [{"payloadHex": p.hex(), "checksumHex": save_checksum(p).hex()} for p in payloads])


def gen_compression() -> None:
    rng = Lcg(11)
    texts = {
        "decl": MG60.encode(),
        "random": rng.bytes(3000),
        "large": (SHOTGUN * 50).encode(),
    }
    cases = []
    for name, data in texts.items():
        for delta, pad in ((0, False), (0, True), (-1, True), (13, True), (200, True),
                           (200, False), (70000, True), (140000, True)):
            cases.append({"data": name, "delta": delta, "pad": pad,
                          "mustFit": delta == 0 or (pad and delta >= 13)})
    for n, d in texts.items():
        write(f"compression/{n}.bin", d)
    dump("compression.json", {"files": {n: f"compression/{n}.bin" for n in texts}, "cases": cases})


def main() -> None:
    gen_decl()
    images = gen_images()
    audio = gen_audio()
    gen_archive(images, audio)
    gen_mods()
    gen_save()
    gen_compression()
    print("vectors written to", OUT)


if __name__ == "__main__":
    main()
