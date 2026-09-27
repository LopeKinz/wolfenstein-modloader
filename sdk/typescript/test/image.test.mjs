import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { FormatError, decode, decodeBim, decodePng, encodePng, encodeRgba8, parseBim } from "../dist/index.js";
import { hex, load, readBytes, sha } from "./helpers.mjs";

describe("image vectors", () => {
  const v = load("bim.json");

  it("decode", () => {
    for (const c of v.decode) {
      const b = parseBim(readBytes("bim/" + c.file));
      assert.equal(b.hash, c.hash);
      assert.deepEqual(b.header, c.header);
      assert.deepEqual(b.mips.map((m) => [m.width, m.height, m.data.length]), c.mips);
      const { width, height, rgba } = decodeBim(b);
      assert.deepEqual([width, height], [c.width, c.height]);
      assert.equal(sha(rgba), c.rgbaSha256, c.file);
      assert.equal(sha(decode(b).rgba), c.rgbaSha256);
    }
    for (const f of v.badMagic) assert.throws(() => parseBim(readBytes("bim/" + f)), FormatError);
  });

  it("encode", () => {
    const e = v.encode;
    const tmpl = parseBim(readBytes("bim/" + e.template));
    const out = encodeRgba8(tmpl, e.width, e.height, hex(e.rgbaHex));
    assert.ok(out.equals(readBytes("bim/" + e.expect)));
  });

  it("png", () => {
    for (const c of v.png) {
      const { width, height, rgba } = decodePng(readBytes("png/" + c.file));
      assert.deepEqual([width, height], [c.width, c.height]);
      assert.equal(sha(rgba), c.rgbaSha256, c.file);
      const again = decodePng(encodePng(width, height, rgba));
      assert.equal(again.width, width);
      assert.equal(again.height, height);
      assert.ok(again.rgba.equals(rgba));
    }
    assert.throws(() => decodePng(Buffer.from("not a png")), FormatError);
  });
});
