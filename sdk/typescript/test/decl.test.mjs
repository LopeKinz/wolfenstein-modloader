import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { Decl, DeclError, formatPath, formatValue, parsePath } from "../dist/index.js";
import { load, readText } from "./helpers.mjs";

describe("decl vectors", () => {
  const v = load("decl.json");

  it("parse", () => {
    for (const c of v.parse) {
      const text = readText("decl/" + c.file);
      const d = new Decl(text);
      assert.equal(d.text, text);
      assert.deepEqual(d.paths(), c.paths, c.file);
      assert.deepEqual(
        d.nodes().filter((n) => n.kind === "block").map((n) => n.pathStr),
        c.blocks,
        c.file,
      );
    }
  });

  it("edits", () => {
    for (const c of v.edits) {
      const d = Decl.parse(readText("decl/" + c.file));
      for (const [p, val] of c.set) d.set(p, val);
      assert.equal(d.text, readText("decl/" + c.expect), c.expect);
    }
  });

  it("errors", () => {
    for (const t of v.parseErrors) assert.throws(() => new Decl(t), DeclError, t);
    for (const c of v.setErrors) {
      assert.throws(() => new Decl(readText("decl/" + c.file)).set(c.path, c.value), DeclError, c.path);
    }
    for (const p of v.pathErrors) assert.throws(() => parsePath(p), DeclError, p);
  });

  it("paths", () => {
    for (const [s, segs] of v.paths) {
      assert.deepEqual(parsePath(s), segs);
      assert.equal(formatPath(segs), s);
    }
  });

  it("format", () => {
    for (const c of load("format.json")) {
      assert.equal(formatValue({ kind: c.kind, raw: c.raw }, c.value), c.expected, JSON.stringify(c));
    }
  });

  it("api basics", () => {
    const d = Decl.parse('{ a = "x\\"y"; b 1 2 3; flag; }');
    assert.equal(d.get("a"), 'x"y');
    assert.equal(d.raw("a"), '"x\\"y"');
    assert.equal(d.raw("b"), "1 2 3");
    assert.ok(d.has("flag"));
    assert.ok(!d.has("nope"));
    assert.ok(!d.has("a..b"));
    assert.throws(() => d.set("flag", 1), DeclError);
    d.setRaw("flag", "flag2");
    assert.ok(d.has("flag2"));
    assert.throws(() => d.set("a", Infinity), DeclError);
  });
});
