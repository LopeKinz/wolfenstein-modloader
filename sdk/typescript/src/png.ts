/** Minimal RGBA PNG encoder/decoder (SPEC §9). */

import zlib from "node:zlib";
import type { Image } from "./bim.js";
import { toBuffer } from "./bytes.js";
import { FormatError } from "./errors.js";

export const SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(data: Uint8Array): number {
  let c = 0xffffffff;
  for (const b of data) c = CRC_TABLE[(c ^ b) & 0xff]! ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(kind: string, data: Buffer): Buffer {
  const body = Buffer.concat([Buffer.from(kind, "latin1"), data]);
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body), 0);
  return Buffer.concat([len, body, crc]);
}

/** Encode 8-bit RGBA, non-interlaced, filter 0, one IDAT. */
export function encodePng(width: number, height: number, rgba: Uint8Array): Buffer {
  if (rgba.length !== width * height * 4) throw new FormatError("RGBA buffer size does not match dimensions");
  const src = toBuffer(rgba);
  const stride = width * 4;
  const raw = Buffer.alloc(height * (stride + 1));
  for (let y = 0; y < height; y++) src.copy(raw, y * (stride + 1) + 1, y * stride, (y + 1) * stride);
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;
  ihdr[9] = 6;
  return Buffer.concat([
    SIGNATURE,
    chunk("IHDR", ihdr),
    chunk("IDAT", zlib.deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

function paeth(a: number, b: number, c: number): number {
  const p = a + b - c;
  const pa = Math.abs(p - a);
  const pb = Math.abs(p - b);
  const pc = Math.abs(p - c);
  if (pa <= pb && pa <= pc) return a;
  return pb <= pc ? b : c;
}

const CHANNELS: Readonly<Record<number, number>> = { 0: 1, 2: 3, 4: 2, 6: 4 };

/** Decode an 8-bit grey/RGB/grey+alpha/RGBA non-interlaced PNG to RGBA8. */
export function decodePng(data: Uint8Array): Image {
  const buf = toBuffer(data);
  if (!buf.subarray(0, 8).equals(SIGNATURE)) throw new FormatError("PNG: bad signature");
  let pos = 8;
  const idat: Buffer[] = [];
  let ihdr: Buffer | null = null;
  while (pos + 8 <= buf.length) {
    const n = buf.readUInt32BE(pos);
    const kind = buf.toString("latin1", pos + 4, pos + 8);
    const body = buf.subarray(pos + 8, pos + 8 + n);
    pos += 12 + n;
    if (kind === "IHDR") ihdr = body;
    else if (kind === "IDAT") idat.push(body);
    else if (kind === "IEND") break;
  }
  if (ihdr === null) throw new FormatError("PNG: missing IHDR");
  if (ihdr.length !== 13) throw new FormatError("PNG: bad IHDR");
  const w = ihdr.readUInt32BE(0);
  const h = ihdr.readUInt32BE(4);
  const depth = ihdr[8]!;
  const ctype = ihdr[9]!;
  const interlace = ihdr[12]!;
  const channels = CHANNELS[ctype];
  if (depth !== 8 || channels === undefined || interlace !== 0) {
    throw new FormatError(`PNG: unsupported (depth ${depth}, colour type ${ctype}, interlace ${interlace})`);
  }
  let raw: Buffer;
  try {
    raw = zlib.inflateSync(Buffer.concat(idat));
  } catch (e) {
    throw new FormatError(`PNG: ${(e as Error).message}`);
  }
  const bpp = channels;
  const stride = w * channels;
  if (raw.length < h * (stride + 1)) throw new FormatError("PNG: image data truncated");
  let prev = Buffer.alloc(stride);
  const pixels = Buffer.alloc(h * stride);
  for (let y = 0; y < h; y++) {
    const f = raw[y * (stride + 1)]!;
    const line = pixels.subarray(y * stride, (y + 1) * stride);
    raw.copy(line, 0, y * (stride + 1) + 1, (y + 1) * (stride + 1));
    if (f > 4) throw new FormatError(`PNG: bad filter ${f}`);
    if (f !== 0) {
      for (let i = 0; i < stride; i++) {
        const a = i >= bpp ? line[i - bpp]! : 0;
        const b = prev[i]!;
        const c = i >= bpp ? prev[i - bpp]! : 0;
        let add: number;
        if (f === 1) add = a;
        else if (f === 2) add = b;
        else if (f === 3) add = (a + b) >> 1;
        else add = paeth(a, b, c);
        line[i] = (line[i]! + add) & 0xff;
      }
    }
    prev = line;
  }
  if (channels === 4) return { width: w, height: h, rgba: pixels };
  const out = Buffer.alloc(w * h * 4);
  for (let i = 0; i < w * h; i++) {
    const p = i * channels;
    const o = i * 4;
    if (channels === 3) {
      out[o] = pixels[p]!;
      out[o + 1] = pixels[p + 1]!;
      out[o + 2] = pixels[p + 2]!;
      out[o + 3] = 255;
    } else {
      const v = pixels[p]!;
      out[o] = v;
      out[o + 1] = v;
      out[o + 2] = v;
      out[o + 3] = channels === 1 ? 255 : pixels[p + 1]!;
    }
  }
  return { width: w, height: h, rgba: out };
}
