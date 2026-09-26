/** Raw DEFLATE with sync flush and exact-size packing (SPEC §4). */

import zlib from "node:zlib";
import { toBuffer } from "./bytes.js";
import { FormatError } from "./errors.js";

export const SYNC_MARKER = Buffer.from([0x00, 0x00, 0xff, 0xff]);
const EMPTY_STORED = Buffer.from([0x00, 0x00, 0x00, 0xff, 0xff]);
const MAX_STORED = 0xffff;

/** Inflate a raw DEFLATE stream (no final block required) to exactly `usize` bytes. */
export function inflate(stream: Uint8Array, usize: number): Buffer {
  let out: Buffer;
  try {
    out = zlib.inflateRawSync(toBuffer(stream), { finishFlush: zlib.constants.Z_SYNC_FLUSH });
  } catch (e) {
    throw new FormatError(`inflate failed: ${(e as Error).message}`);
  }
  // Like zlib's max_length: extra output beyond `usize` is ignored
  // (a `usize` of 0 means "unbounded", as in Python's decompressobj).
  if (usize > 0 && out.length > usize) out = out.subarray(0, usize);
  if (out.length !== usize) {
    throw new FormatError(`inflate produced ${out.length} bytes, expected ${usize}`);
  }
  return out;
}

/** Compress `data` to a raw DEFLATE stream ending in a sync flush (`00 00 FF FF`). */
export function deflateSync(data: Uint8Array): Buffer {
  return zlib.deflateRawSync(toBuffer(data), { level: 9, finishFlush: zlib.constants.Z_SYNC_FLUSH });
}

export function isSyncFlushed(stream: Uint8Array): boolean {
  const n = stream.length;
  return (
    n >= 4 && stream[n - 4] === 0x00 && stream[n - 3] === 0x00 && stream[n - 2] === 0xff && stream[n - 1] === 0xff
  );
}

function storedBlock(chunk: Buffer): Buffer {
  const n = chunk.length;
  const head = Buffer.from([0, n & 0xff, n >> 8, ~n & 0xff, (~n >> 8) & 0xff]);
  return Buffer.concat([head, chunk]);
}

export interface ExactStream {
  /** Exactly `csize` bytes. */
  stream: Buffer;
  /** What `stream` inflates to (the new `usize` content). */
  payload: Buffer;
}

/**
 * Produce a stream of exactly `csize` bytes or `null` (SPEC §4.1). With `pad`
 * a Decl line comment is appended to fill the slot (text assets only).
 */
export function deflateExact(data: Uint8Array, csize: number, pad: boolean): ExactStream | null {
  const d = toBuffer(data);
  const s = deflateSync(d);
  if (s.length === csize) return { stream: s, payload: Buffer.from(d) };
  if (s.length > csize || !pad) return null;
  const r = csize - s.length;
  if (r >= 13) {
    const n = Math.ceil((r - 5) / (MAX_STORED + 5));
    const total = r - 5 - 5 * n;
    const padding = Buffer.concat([Buffer.from("\n//", "latin1"), Buffer.alloc(total - 3, 0x20)]);
    const parts: Buffer[] = [s];
    for (let i = 0; i < n; i++) {
      parts.push(storedBlock(padding.subarray(i * MAX_STORED, (i + 1) * MAX_STORED)));
    }
    parts.push(EMPTY_STORED);
    return { stream: Buffer.concat(parts), payload: Buffer.concat([d, padding]) };
  }
  for (let k = 3; k < 67; k++) {
    const padding = Buffer.concat([Buffer.from("\n//", "latin1"), Buffer.alloc(k - 3, 0x20)]);
    const payload = Buffer.concat([d, padding]);
    const s2 = deflateSync(payload);
    if (s2.length === csize) return { stream: s2, payload };
  }
  return null;
}
