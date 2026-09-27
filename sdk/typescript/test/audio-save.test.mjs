import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { describe, it } from "node:test";
import { FormatError, fixSave, oggStreamLength, parseBsnf, saveChecksum, streamContainerFor, verifySave } from "../dist/index.js";
import { VEC, hex, load, readBytes } from "./helpers.mjs";

// The vectors use the Python field names.
const snake = (s) => ({
  language: s.language,
  hash: s.hash,
  length: s.length,
  granule: s.granule,
  format_tag: s.formatTag,
  channels: s.channels,
  sample_rate: s.sampleRate,
  avg_bytes_per_sec: s.avgBytesPerSec,
  block_align: s.blockAlign,
  bits_per_sample: s.bitsPerSample,
});

describe("audio and save vectors", () => {
  const v = load("audio.json");

  it("bsnf", () => {
    for (const c of v.bsnf) {
      const got = parseBsnf(readBytes("audio/" + c.file));
      assert.deepEqual(got.map(snake), c.samples, c.file);
      for (const s of got) assert.equal(s.duration, s.granule / s.sampleRate);
    }
    assert.throws(() => parseBsnf(Buffer.from("nope1234")), FormatError);
  });

  it("ogg", () => {
    const o = v.ogg;
    const data = readBytes("audio/" + o.file);
    for (const [off, length] of o.streams) {
      assert.equal(oggStreamLength(data, off), length);
      assert.equal(oggStreamLength(new Uint8Array(data), off), length);
    }
    const file = path.join(VEC, "audio", o.file);
    const [off1, len1] = o.streams[1];
    assert.equal(oggStreamLength(file, off1), len1);
    const fd = fs.openSync(file, "r");
    try {
      assert.equal(oggStreamLength(fd, off1), len1);
    } finally {
      fs.closeSync(fd);
    }
    assert.throws(() => oggStreamLength(data, o.badOffset), FormatError);
  });

  it("containers", () => {
    for (const [name, container] of v.containers) assert.equal(streamContainerFor(name), container);
  });

  it("save", () => {
    for (const c of load("save.json")) {
      const p = hex(c.payloadHex);
      assert.equal(saveChecksum(p).toString("hex"), c.checksumHex);
      const good = Buffer.concat([hex(c.checksumHex), p]);
      assert.ok(verifySave(good));
      assert.ok(!(verifySave(Buffer.concat([Buffer.alloc(4), p])) && c.checksumHex !== "00000000"));
      assert.ok(fixSave(Buffer.concat([hex("ffffffff"), p])).equals(good));
    }
    assert.ok(!verifySave(Buffer.alloc(3)));
    assert.throws(() => fixSave(Buffer.alloc(3)), FormatError);
  });
});
