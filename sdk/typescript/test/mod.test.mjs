import assert from "node:assert/strict";
import path from "node:path";
import { describe, it } from "node:test";
import { Mod, ModError, applyMods } from "../dist/index.js";
import { VEC, load, readText } from "./helpers.mjs";

describe("mod vectors", () => {
  const v = load("mod.json");

  it("apply", () => {
    const base = Object.fromEntries(Object.entries(v.base).map(([k, p]) => [k, readText("mod/" + p)]));
    const mods = v.order.map((n) => Mod.load(path.join(VEC, "mod", n)));
    const res = applyMods(mods, (t, n) => base[`${t}:${n}`]);
    assert.deepEqual([...res.results.keys()], v.resultOrder);
    for (const [k, p] of Object.entries(v.results)) assert.equal(res.results.get(k), readText("mod/" + p), k);
    assert.deepEqual(res.conflicts, v.conflicts);
    assert.deepEqual(res.errors.map((e) => ({ asset: e.asset, mod: e.mod })), v.errors);
  });

  it("load from file path and legacy keys", () => {
    const m = Mod.load(path.join(VEC, "mod", "legacy", "mod.json"));
    assert.equal(m.name, "legacy");
    assert.equal(m.game, "tno");
    assert.deepEqual(m.assets.map((a) => a.key), [
      "damage:damage/tungsten/mg60",
      "weapon:weapon/missing",
      "weapon:weapon/shotgun_base",
    ]);
    assert.equal(m.assets[0].type, "damage");
    assert.equal(m.assets[0].name, "damage/tungsten/mg60");
  });

  it("invalid", () => {
    for (const [label, text] of v.invalid) assert.throws(() => Mod.loads(text), ModError, label);
  });
});
