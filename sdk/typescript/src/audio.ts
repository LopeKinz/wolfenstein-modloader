/** Audio descriptors and streamed containers (SPEC §10). */

import fs from "node:fs";
import { asciiReplace, toBuffer } from "./bytes.js";
import { FormatError } from "./errors.js";

export const ENGLISH_PREFIX = "sound/vo/english/";

export class SampleInfo {
  constructor(
    public language: string,
    public hash: number,
    /** Ogg stream length in bytes. */
    public length: number,
    /** PCM samples. */
    public granule: number,
    public formatTag: number,
    public channels: number,
    public sampleRate: number,
    public avgBytesPerSec: number,
    public blockAlign: number,
    public bitsPerSample: number,
  ) {}

  /** Duration in seconds (`granule / sampleRate`). */
  get duration(): number {
    return this.sampleRate ? this.granule / this.sampleRate : 0;
  }
}

function block(data: Buffer, off: number, language: string): SampleInfo {
  if (off + 42 > data.length) throw new FormatError("bsnf: truncated sample block");
  return new SampleInfo(
    language,
    data.readUInt32BE(off),
    data.readUInt32BE(off + 16),
    data.readUInt32BE(off + 8),
    data.readUInt16LE(off + 20),
    data.readUInt16LE(off + 22),
    data.readUInt32LE(off + 24),
    data.readUInt32LE(off + 28),
    data.readUInt16LE(off + 32),
    data.readUInt16LE(off + 34),
  );
}

export function parseBsnf(data: Uint8Array): SampleInfo[] {
  const buf = toBuffer(data);
  if (buf.length < 8 || buf.toString("latin1", 0, 4) !== "bsnf") throw new FormatError("bsnf: bad magic");
  const count = buf.readUInt32BE(4);
  if (count === 1) return [block(buf, 0x20, "")];
  const out: SampleInfo[] = [];
  for (let i = 0; i < count; i++) {
    const p = 8 + i * 24;
    if (p + 24 > buf.length) throw new FormatError("bsnf: truncated language table");
    const raw = buf.subarray(p, p + 16);
    const nul = raw.indexOf(0);
    const name = asciiReplace(nul < 0 ? raw : raw.subarray(0, nul));
    out.push(block(buf, buf.readUInt32BE(p + 20), name));
  }
  return out;
}

/** `"english.streamed"` for `sound/vo/english/…`, else `"streamed.resources"`. */
export function streamContainerFor(assetName: string): string {
  return assetName.startsWith(ENGLISH_PREFIX) ? "english.streamed" : "streamed.resources";
}

/** Anything that can read `n` bytes at `pos` (fewer at end of data). */
export interface RandomAccessReader {
  read(pos: number, n: number): Uint8Array;
}

/** Bytes, an open file descriptor, a file path, or a reader. */
export type OggSource = Uint8Array | number | string | RandomAccessReader;

function fdReader(fd: number): RandomAccessReader {
  return {
    read(pos, n) {
      const out = Buffer.alloc(n);
      let done = 0;
      while (done < n) {
        const r = fs.readSync(fd, out, done, n - done, pos + done);
        if (r === 0) break;
        done += r;
      }
      return out.subarray(0, done);
    },
  };
}

/** Walk Ogg pages from `offset` up to and including the EOS page; returns the byte length. */
export function oggStreamLength(src: OggSource, offset: number): number {
  if (typeof src === "string") {
    const fd = fs.openSync(src, "r");
    try {
      return walk(fdReader(fd), offset);
    } finally {
      fs.closeSync(fd);
    }
  }
  if (typeof src === "number") return walk(fdReader(src), offset);
  if (src instanceof Uint8Array) {
    const buf = toBuffer(src);
    return walk({ read: (pos, n) => buf.subarray(pos, pos + n) }, offset);
  }
  return walk(src, offset);
}

function walk(r: RandomAccessReader, offset: number): number {
  let pos = offset;
  for (;;) {
    const head = r.read(pos, 27);
    if (head.length < 27 || head[0] !== 0x4f || head[1] !== 0x67 || head[2] !== 0x67 || head[3] !== 0x53) {
      throw new FormatError(`Ogg: no page at offset ${pos}`);
    }
    const flags = head[5]!;
    const nseg = head[26]!;
    const table = r.read(pos + 27, nseg);
    if (table.length < nseg) throw new FormatError("Ogg: truncated segment table");
    let body = 0;
    for (let i = 0; i < nseg; i++) body += table[i]!;
    pos += 27 + nseg + body;
    if (flags & 0x04) return pos - offset;
  }
}
