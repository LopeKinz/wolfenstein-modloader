import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, it } from "node:test";
import {
  ChunkIndex,
  FormatError,
  Game,
  NoFitError,
  buildMasterIndex,
  parseMasterIndex,
  validateChunk,
} from "../dist/index.js";
import { PAD_RE, VEC, load, readBytes, sha } from "./helpers.mjs";

describe("archive vectors", () => {
  const v = load("archive.json");
  let tmp;
  let root;

  beforeEach(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), "wolfsdk-"));
    root = path.join(tmp, "game");
    fs.cpSync(path.join(VEC, "game"), root, { recursive: true });
  });

  afterEach(() => {
    fs.rmSync(tmp, { recursive: true, force: true });
  });

  it("master index", () => {
    const data = readBytes("game/base/master.index");
    const pairs = parseMasterIndex(data);
    assert.deepEqual(pairs, v.pairs);
    assert.ok(buildMasterIndex(pairs).equals(data));
    assert.throws(() => parseMasterIndex(Buffer.concat([Buffer.from("XXXX"), data.subarray(4)])), FormatError);
    assert.throws(() => parseMasterIndex(data.subarray(0, 20)), FormatError);
  });

  it("entries", () => {
    const g = Game.open(root);
    assert.equal(g.chunks.length, v.chunks.length);
    g.chunks.forEach((c, ci) => {
      const exp = v.chunks[ci];
      assert.equal(c.indexName, exp.index);
      assert.deepEqual([c.index.counterA, c.index.counterB], [exp.counterA, exp.counterB]);
      assert.equal(c.entries.length, exp.entries.length);
      c.entries.forEach((e, i) => {
        const got = {
          index: e.index,
          type: e.type,
          name: e.name,
          path: e.path,
          offset: e.offset,
          usize: e.usize,
          csize: e.csize,
          start: e.start,
          tripleOffset: e.tripleOffset,
          trailerHex: e.trailer.toString("hex"),
          streamOffset: e.streamOffset,
          compressed: e.compressed,
          payloadSha256: sha(g.read({ chunk: c, entry: e })),
        };
        assert.deepEqual(got, exp.entries[i]);
      });
      // index without resources size must give the same entries
      assert.equal(new ChunkIndex(c.index.toBytes()).length, exp.entries.length);
      assert.ok(c.index.toBytes().equals(fs.readFileSync(c.indexPath)));
      assert.deepEqual(validateChunk(c), []);
    });
    for (const [key, chunks] of Object.entries(v.find)) {
      const i = key.indexOf(":");
      assert.deepEqual(
        g.find(key.slice(0, i), key.slice(i + 1)).map((o) => o.chunk.indexName),
        chunks,
      );
    }
    assert.equal(g.readAsset("weapon", "nope"), null);
  });

  it("writeInPlace", () => {
    for (const c of v.writeInPlace) {
      const g = Game.open(root);
      const i = c.asset.indexOf(":");
      const [t, n] = [c.asset.slice(0, i), c.asset.slice(i + 1)];
      const data = Buffer.from(c.text, "utf-8");
      const sizes = Object.fromEntries(g.chunks.map((ch) => [ch.resourcesName, fs.statSync(ch.resourcesPath).size]));
      const before = g.find(t, n).map((o) => [o.chunk.indexName, o.entry.offset, o.entry.csize]);
      if (c.expect === "nofit") {
        assert.throws(() => g.writeAsset(t, n, data), NoFitError);
        continue;
      }
      g.writeAsset(t, n, data);
      const g2 = Game.open(root);
      const after = g2.find(t, n).map((o) => [o.chunk.indexName, o.entry.offset, o.entry.csize]);
      assert.deepEqual(after, before);
      for (const o of g2.find(t, n)) {
        const payload = g2.read(o);
        assert.ok(payload.subarray(0, data.length).equals(data));
        assert.match(payload.subarray(data.length).toString("utf-8"), PAD_RE);
      }
      for (const ch of g2.chunks) {
        assert.equal(fs.statSync(ch.resourcesPath).size, sizes[ch.resourcesName]);
        assert.deepEqual(validateChunk(ch), []);
      }
    }
  });

  it("rebuild", () => {
    const r = v.rebuild;
    const g = Game.open(root);
    const chunk = g.chunks.find((c) => c.indexName === r.chunk);
    const i = r.asset.indexOf(":");
    const target = chunk.index.find(r.asset.slice(0, i), r.asset.slice(i + 1))[0];
    const before = new Map(chunk.entries.map((e) => [e.index, g.read({ chunk, entry: e })]));
    const text = Buffer.from(r.text, "utf-8");
    g.rebuild(chunk, new Map([[target.index, text]]));
    const g2 = Game.open(root);
    const chunk2 = g2.chunks.find((c) => c.indexName === r.chunk);
    assert.deepEqual(validateChunk(chunk2), []);
    for (const e of chunk2.entries) {
      const want = e.index === target.index ? text : before.get(e.index);
      assert.ok(g2.read({ chunk: chunk2, entry: e }).equals(want), e.key);
    }
  });
});
