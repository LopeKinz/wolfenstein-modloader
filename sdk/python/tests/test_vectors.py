"""Run the shared test vectors (spec/vectors) against the Python binding."""

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
VEC = os.environ.get("WOLFSDK_VECTORS", os.path.join(HERE, "..", "..", "..", "spec", "vectors"))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import wolfsdk as w  # noqa: E402
from wolfsdk.decl import Node, format_value  # noqa: E402


def load(rel):
    with open(os.path.join(VEC, rel), encoding="utf-8") as f:
        return json.load(f)


def read_bytes(rel):
    with open(os.path.join(VEC, rel), "rb") as f:
        return f.read()


def read_text(rel):
    return read_bytes(rel).decode("utf-8")


def sha(b):
    return hashlib.sha256(b).hexdigest()


class DeclVectors(unittest.TestCase):
    def test_parse(self):
        v = load("decl.json")
        for case in v["parse"]:
            text = read_text("decl/" + case["file"])
            d = w.Decl(text)
            self.assertEqual(d.text, text)
            self.assertEqual([list(p) for p in d.paths()], case["paths"], case["file"])
            self.assertEqual([n.path_str for n in d.nodes() if n.kind == "block"], case["blocks"])

    def test_edits(self):
        for case in load("decl.json")["edits"]:
            d = w.Decl(read_text("decl/" + case["file"]))
            for p, val in case["set"]:
                d.set(p, val)
            self.assertEqual(d.text, read_text("decl/" + case["expect"]), case)

    def test_errors(self):
        v = load("decl.json")
        for t in v["parseErrors"]:
            with self.assertRaises(w.DeclError, msg=t):
                w.Decl(t)
        for c in v["setErrors"]:
            with self.assertRaises(w.DeclError):
                w.Decl(read_text("decl/" + c["file"])).set(c["path"], c["value"])
        for p in v["pathErrors"]:
            with self.assertRaises(w.DeclError):
                w.parse_path(p)

    def test_paths(self):
        for s, segs in load("decl.json")["paths"]:
            self.assertEqual(list(w.parse_path(s)), segs)
            self.assertEqual(w.format_path(tuple(segs)), s)

    def test_format(self):
        for c in load("format.json"):
            node = Node((), c["kind"], 0, len(c["raw"]), c["raw"])
            self.assertEqual(format_value(node, c["value"]), c["expected"], c)


class ImageVectors(unittest.TestCase):
    def test_decode(self):
        v = load("bim.json")
        for c in v["decode"]:
            b = w.parse_bim(read_bytes("bim/" + c["file"]))
            self.assertEqual(b.hash, c["hash"])
            self.assertEqual(b.header, c["header"])
            self.assertEqual([[m.width, m.height, len(m.data)] for m in b.mips], c["mips"])
            width, height, rgba = w.decode(b)
            self.assertEqual((width, height), (c["width"], c["height"]))
            self.assertEqual(sha(rgba), c["rgbaSha256"], c["file"])
        for f in v["badMagic"]:
            with self.assertRaises(w.FormatError):
                w.parse_bim(read_bytes("bim/" + f))

    def test_encode(self):
        e = load("bim.json")["encode"]
        tmpl = w.parse_bim(read_bytes("bim/" + e["template"]))
        out = w.encode_rgba8(tmpl, e["width"], e["height"], bytes.fromhex(e["rgbaHex"]))
        self.assertEqual(out, read_bytes("bim/" + e["expect"]))

    def test_png(self):
        for c in load("bim.json")["png"]:
            width, height, rgba = w.decode_png(read_bytes("png/" + c["file"]))
            self.assertEqual((width, height), (c["width"], c["height"]))
            self.assertEqual(sha(rgba), c["rgbaSha256"], c["file"])
            again = w.decode_png(w.encode_png(width, height, rgba))
            self.assertEqual(again, (width, height, rgba))


class AudioSaveVectors(unittest.TestCase):
    def test_bsnf(self):
        v = load("audio.json")
        for c in v["bsnf"]:
            got = [s.__dict__ for s in w.parse_bsnf(read_bytes("audio/" + c["file"]))]
            self.assertEqual(got, c["samples"])

    def test_ogg(self):
        v = load("audio.json")["ogg"]
        data = read_bytes("audio/" + v["file"])
        for off, length in v["streams"]:
            self.assertEqual(w.ogg_stream_length(data, off), length)
        with open(os.path.join(VEC, "audio", v["file"]), "rb") as f:
            self.assertEqual(w.ogg_stream_length(f, v["streams"][1][0]), v["streams"][1][1])
        with self.assertRaises(w.FormatError):
            w.ogg_stream_length(data, v["badOffset"])

    def test_containers(self):
        for name, container in load("audio.json")["containers"]:
            self.assertEqual(w.stream_container_for(name), container)

    def test_save(self):
        for c in load("save.json"):
            p = bytes.fromhex(c["payloadHex"])
            self.assertEqual(w.save_checksum(p).hex(), c["checksumHex"])
            good = bytes.fromhex(c["checksumHex"]) + p
            self.assertTrue(w.verify_save(good))
            self.assertFalse(w.verify_save(b"\x00\x00\x00\x00" + p) and c["checksumHex"] != "00000000")
            self.assertEqual(w.fix_save(b"\xff\xff\xff\xff" + p), good)


class CompressionVectors(unittest.TestCase):
    def test_deflate_exact(self):
        v = load("compression.json")
        for c in v["cases"]:
            data = read_bytes(v["files"][c["data"]])
            s = w.deflate_sync(data)
            self.assertTrue(w.is_sync_flushed(s))
            self.assertEqual(w.inflate(s, len(data)), data)
            csize = len(s) + c["delta"]
            r = w.deflate_exact(data, csize, c["pad"])
            if c["mustFit"]:
                self.assertIsNotNone(r, c)
            if c["delta"] < 0 or (c["delta"] > 0 and not c["pad"]):
                self.assertIsNone(r, c)
            if r is not None:
                stream, payload = r
                self.assertEqual(len(stream), csize)
                self.assertTrue(w.is_sync_flushed(stream))
                self.assertTrue(payload.startswith(data))
                self.assertRegex(payload[len(data):].decode(), r"^(\n// *)?$")
                self.assertEqual(w.inflate(stream, len(payload)), payload)

    def test_inflate_short(self):
        with self.assertRaises(w.FormatError):
            w.inflate(w.deflate_sync(b"abc"), 10)


class ArchiveVectors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "game")
        shutil.copytree(os.path.join(VEC, "game"), self.root)
        self.v = load("archive.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_master_index(self):
        data = read_bytes("game/base/master.index")
        pairs = w.parse_master_index(data)
        self.assertEqual([list(p) for p in pairs], self.v["pairs"])
        self.assertEqual(w.build_master_index(pairs), data)
        with self.assertRaises(w.FormatError):
            w.parse_master_index(b"XXXX" + data[4:])
        with self.assertRaises(w.FormatError):
            w.parse_master_index(data[:20])

    def test_entries(self):
        g = w.Game(self.root)
        self.assertEqual(len(g.chunks), len(self.v["chunks"]))
        for c, exp in zip(g.chunks, self.v["chunks"]):
            self.assertEqual(c.index_name, exp["index"])
            self.assertEqual((c.index.counter_a, c.index.counter_b), (exp["counterA"], exp["counterB"]))
            self.assertEqual(len(c.entries), len(exp["entries"]))
            for e, x in zip(c.entries, exp["entries"]):
                got = {"index": e.index, "type": e.type, "name": e.name, "path": e.path,
                       "offset": e.offset, "usize": e.usize, "csize": e.csize, "start": e.start,
                       "tripleOffset": e.triple_offset, "trailerHex": e.trailer.hex(),
                       "streamOffset": e.stream_offset, "compressed": e.compressed,
                       "payloadSha256": sha(g.read(w.Occurrence(c, e)))}
                self.assertEqual(got, x)
            # index without resources size must give the same entries
            self.assertEqual(len(w.ChunkIndex(c.index.to_bytes())), len(exp["entries"]))
            self.assertEqual(w.validate_chunk(c), [])
        for key, chunks in self.v["find"].items():
            t, n = key.split(":", 1)
            self.assertEqual([o.chunk.index_name for o in g.find(t, n)], chunks)

    def test_write_in_place(self):
        for case in self.v["writeInPlace"]:
            g = w.Game(self.root)
            t, n = case["asset"].split(":", 1)
            data = case["text"].encode()
            sizes = {c.resources_name: os.path.getsize(c.resources_path) for c in g.chunks}
            before = [(o.chunk.index_name, o.entry.offset, o.entry.csize) for o in g.find(t, n)]
            if case["expect"] == "nofit":
                with self.assertRaises(w.NoFitError):
                    g.write_asset(t, n, data)
                continue
            g.write_asset(t, n, data)
            g2 = w.Game(self.root)
            after = [(o.chunk.index_name, o.entry.offset, o.entry.csize) for o in g2.find(t, n)]
            self.assertEqual(before, after)
            for o in g2.find(t, n):
                payload = g2.read(o)
                self.assertTrue(payload.startswith(data))
                self.assertRegex(payload[len(data):].decode(), r"^(\n// *)?$")
            for c in g2.chunks:
                self.assertEqual(os.path.getsize(c.resources_path), sizes[c.resources_name])
                self.assertEqual(w.validate_chunk(c), [])

    def test_rebuild(self):
        r = self.v["rebuild"]
        g = w.Game(self.root)
        chunk = next(c for c in g.chunks if c.index_name == r["chunk"])
        t, n = r["asset"].split(":", 1)
        target = chunk.index.find(t, n)[0]
        before = {e.index: g.read(w.Occurrence(chunk, e)) for e in chunk.entries}
        g.rebuild(chunk, {target.index: r["text"].encode()})
        g2 = w.Game(self.root)
        chunk2 = next(c for c in g2.chunks if c.index_name == r["chunk"])
        self.assertEqual(w.validate_chunk(chunk2), [])
        for e in chunk2.entries:
            want = r["text"].encode() if e.index == target.index else before[e.index]
            self.assertEqual(g2.read(w.Occurrence(chunk2, e)), want)


class ModVectors(unittest.TestCase):
    def test_apply(self):
        v = load("mod.json")
        base = {k: read_text("mod/" + p) for k, p in v["base"].items()}
        mods = [w.Mod.load(os.path.join(VEC, "mod", n)) for n in v["order"]]
        res = w.apply_mods(mods, lambda t, n: base.get(f"{t}:{n}"))
        self.assertEqual(list(res.results), v["resultOrder"])
        for k, p in v["results"].items():
            self.assertEqual(res.results[k], read_text("mod/" + p), k)
        self.assertEqual([c.to_dict() for c in res.conflicts], v["conflicts"])
        self.assertEqual([{"asset": e.asset, "mod": e.mod} for e in res.errors], v["errors"])

    def test_invalid(self):
        for _, text in load("mod.json")["invalid"]:
            with self.assertRaises(w.ModError, msg=text):
                w.Mod.loads(text)


class PatcherRoundTrip(unittest.TestCase):
    """apply → revert restores every file byte for byte (Python-only feature)."""

    def test_apply_revert(self):
        from wolfsdk import patcher
        tmp = tempfile.mkdtemp()
        try:
            root = os.path.join(tmp, "game")
            shutil.copytree(os.path.join(VEC, "game"), root)
            snap = {f: read_bytes(os.path.join("game", "base", f)) for f in os.listdir(os.path.join(root, "base"))}
            mod = w.Mod.from_dict({"name": "t", "assets": {
                "damage:damage/tungsten/mg60": {"set": {"edit.damageParms.maxDamage": 99}},
                "weapon:weapon/shotgun_base": {"set": {"edit.handsModelMD6": "weapons/a_much_longer_model_name_" + "x" * 300 + ".md6"}},
            }})
            rep = patcher.apply(w.Game(root), [mod])
            self.assertEqual(rep.plan.errors, [])
            self.assertIn("chunk0.resources", rep.rebuilt)
            g = w.Game(root)
            self.assertIn(b"maxDamage = 99", g.read_asset("damage", "damage/tungsten/mg60"))
            self.assertIn(b"a_much_longer", g.read_asset("weapon", "weapon/shotgun_base"))
            for c in g.chunks:
                self.assertEqual(w.validate_chunk(c), [])
            # a second session on top of the rebuilt archive
            mod2 = w.Mod.from_dict({"name": "u", "assets": {
                "damage:damage/tungsten/mg60": {"set": {"edit.damageParms.minDamage": 1}}}})
            patcher.apply(w.Game(root), [mod2])
            self.assertGreater(patcher.revert(root), 0)
            now = {f: read_bytes_abs(os.path.join(root, "base", f)) for f in os.listdir(os.path.join(root, "base"))}
            self.assertEqual(now, snap)
        finally:
            shutil.rmtree(tmp)


def read_bytes_abs(p):
    with open(p, "rb") as f:
        return f.read()


class CliSmoke(unittest.TestCase):
    def test_commands(self):
        import contextlib
        import io
        from wolfsdk.cli import main
        root = os.path.join(VEC, "game")
        for argv in (["-g", root, "info"], ["-g", root, "list", "--type", "weapon"],
                     ["-g", root, "paths", "damage:damage/tungsten/mg60"],
                     ["-g", root, "image-info", "image:ui/test_bc1"],
                     ["-g", root, "audio-info", "sample:sound/test/imp_01.wav"],
                     ["-g", root, "validate"],
                     ["-g", root, "plan", os.path.join(VEC, "mod", "low")]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(main(argv), 0, argv)
            self.assertTrue(buf.getvalue(), argv)
        self.assertTrue(re.search(r"maxDamage\s+17\.500000", self._run(["-g", root, "paths", "damage:damage/tungsten/mg60"])))

    def _run(self, argv):
        import contextlib
        import io
        from wolfsdk.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(argv)
        return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
