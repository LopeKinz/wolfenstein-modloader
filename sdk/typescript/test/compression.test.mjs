import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { FormatError, deflateExact, deflateSync, inflate, isSyncFlushed } from "../dist/index.js";
import { PAD_RE, load, readBytes } from "./helpers.mjs";

describe("compression vectors", () => {
  it("deflateExact", () => {
    const v = load("compression.json");
    for (const c of v.cases) {
      const data = readBytes(v.files[c.data]);
      const s = deflateSync(data);
      assert.ok(isSyncFlushed(s));
      assert.ok(inflate(s, data.length).equals(data));
      const csize = s.length + c.delta;
      const r = deflateExact(data, csize, c.pad);
      const label = JSON.stringify(c);
      if (c.mustFit) assert.notEqual(r, null, label);
      if (c.delta < 0 || (c.delta > 0 && !c.pad)) assert.equal(r, null, label);
      if (r !== null) {
        assert.equal(r.stream.length, csize, label);
        assert.ok(isSyncFlushed(r.stream), label);
        assert.ok(r.payload.subarray(0, data.length).equals(data), label);
        assert.match(r.payload.subarray(data.length).toString("utf-8"), PAD_RE, label);
        assert.ok(inflate(r.stream, r.payload.length).equals(r.payload), label);
      }
    }
  });

  it("inflate short", () => {
    assert.throws(() => inflate(deflateSync(Buffer.from("abc")), 10), FormatError);
  });

  it("strategy B (small remainder)", () => {
    const data = Buffer.from("{\n\tedit = {\n\t\tammoPerShot = 1;\n\t}\n}\n".repeat(20));
    const base = deflateSync(data).length;
    for (let delta = 1; delta < 13; delta++) {
      const r = deflateExact(data, base + delta, true);
      if (r === null) continue;
      assert.equal(r.stream.length, base + delta);
      assert.ok(inflate(r.stream, r.payload.length).equals(r.payload));
    }
  });
});
