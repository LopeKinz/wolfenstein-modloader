/** BIM images: header, decode to RGBA, RGBA8 encode (SPEC §8). */

import { toBuffer } from "./bytes.js";
import { FormatError } from "./errors.js";

export const MAGIC = Buffer.from([0x09, 0x4d, 0x49, 0x42]);
export const HEADER_FIELDS = 13;
export const DATA_START = 8 + HEADER_FIELDS * 4;

export const FORMAT_RGBA8 = 3;
export const FORMAT_A8 = 5;
export const FORMAT_BC1 = 10;
export const FORMAT_BC3 = 11;
export const FORMAT_NAMES: Readonly<Record<number, string>> = {
  [FORMAT_RGBA8]: "RGBA8",
  [FORMAT_A8]: "A8",
  [FORMAT_BC1]: "BC1",
  [FORMAT_BC3]: "BC3",
};

export interface Mip {
  width: number;
  height: number;
  data: Buffer;
}

/** An RGBA8 image, row-major. */
export interface Image {
  width: number;
  height: number;
  rgba: Buffer;
}

export class Bim {
  constructor(
    public hash: number,
    /** The 13 BE u32 header fields `h[0..12]`. */
    public header: number[],
    public mips: Mip[],
  ) {}

  get textureType(): number {
    return this.header[0]!;
  }
  get width(): number {
    return this.header[1]!;
  }
  get height(): number {
    return this.header[2]!;
  }
  get depth(): number {
    return this.header[3]!;
  }
  get mipCount(): number {
    return this.header[4]!;
  }
  get format(): number {
    return this.header[6]!;
  }
  get formatName(): string {
    return FORMAT_NAMES[this.format] ?? `unknown(${this.format})`;
  }
  get baseWidth(): number {
    return this.header[11]!;
  }
  get baseHeight(): number {
    return this.header[12]!;
  }
}

function writeHeader(hash: number, header: ReadonlyArray<number>): Buffer {
  const out = Buffer.alloc(DATA_START);
  out.writeUInt32LE(hash >>> 0, 0);
  MAGIC.copy(out, 4);
  for (let i = 0; i < HEADER_FIELDS; i++) out.writeUInt32BE(header[i]! >>> 0, 8 + i * 4);
  return out;
}

function mipHeader(w: number, h: number, size: number): Buffer {
  const out = Buffer.alloc(12);
  out.writeUInt32BE(w, 0);
  out.writeUInt32BE(h, 4);
  out.writeUInt32BE(size, 8);
  return out;
}

export function parseBim(data: Uint8Array): Bim {
  const buf = toBuffer(data);
  if (buf.length < DATA_START || !buf.subarray(4, 8).equals(MAGIC)) throw new FormatError("BIM: bad magic");
  const hash = buf.readUInt32LE(0);
  const header: number[] = [];
  for (let i = 0; i < HEADER_FIELDS; i++) header.push(buf.readUInt32BE(8 + i * 4));
  const mips: Mip[] = [];
  let pos = DATA_START;
  for (let i = 0; i < header[4]!; i++) {
    if (pos + 12 > buf.length) break;
    const w = buf.readUInt32BE(pos);
    const h = buf.readUInt32BE(pos + 4);
    const size = buf.readUInt32BE(pos + 8);
    pos += 12;
    if (pos + size > buf.length) break;
    mips.push({ width: w, height: h, data: Buffer.from(buf.subarray(pos, pos + size)) });
    pos += size;
  }
  if (!mips.length) throw new FormatError("BIM: no mip levels");
  return new Bim(hash, header, mips);
}

export function expectedSize(fmt: number, w: number, h: number): number {
  const bw = Math.ceil(w / 4);
  const bh = Math.ceil(h / 4);
  switch (fmt) {
    case FORMAT_RGBA8:
      return w * h * 4;
    case FORMAT_A8:
      return w * h;
    case FORMAT_BC1:
      return bw * bh * 8;
    case FORMAT_BC3:
      return bw * bh * 16;
    default:
      throw new FormatError(`BIM: unsupported format code ${fmt}`);
  }
}

type RGBA = [number, number, number, number];

function rgb565(c: number): [number, number, number] {
  const r = (c >> 11) & 31;
  const g = (c >> 5) & 63;
  const b = c & 31;
  return [(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)];
}

function colorPalette(block: Buffer, off: number, fourColor: boolean): [RGBA[], number] {
  const c0 = block.readUInt16LE(off);
  const c1 = block.readUInt16LE(off + 2);
  const p0 = rgb565(c0);
  const p1 = rgb565(c1);
  let p2: RGBA;
  let p3: RGBA;
  if (c0 > c1 || fourColor) {
    p2 = [0, 1, 2].map((i) => Math.floor((2 * p0[i]! + p1[i]!) / 3)).concat(255) as RGBA;
    p3 = [0, 1, 2].map((i) => Math.floor((p0[i]! + 2 * p1[i]!) / 3)).concat(255) as RGBA;
  } else {
    p2 = [0, 1, 2].map((i) => Math.floor((p0[i]! + p1[i]!) / 2)).concat(255) as RGBA;
    p3 = [0, 0, 0, 0];
  }
  return [[[...p0, 255], [...p1, 255], p2, p3], block.readUInt32LE(off + 4)];
}

function alphaPalette(block: Buffer, off: number): [number[], number] {
  const a0 = block[off]!;
  const a1 = block[off + 1]!;
  const pal = [a0, a1];
  if (a0 > a1) {
    for (let i = 1; i < 7; i++) pal.push(Math.floor(((7 - i) * a0 + i * a1) / 7));
  } else {
    for (let i = 1; i < 5; i++) pal.push(Math.floor(((5 - i) * a0 + i * a1) / 5));
    pal.push(0, 255);
  }
  return [pal, block.readUIntLE(off + 2, 6)]; // 48-bit index field, exact in a double
}

function decodeBlocks(data: Buffer, w: number, h: number, bc3: boolean): Buffer {
  const out = Buffer.alloc(w * h * 4);
  const bw = Math.ceil(w / 4);
  const bh = Math.ceil(h / 4);
  const size = bc3 ? 16 : 8;
  for (let by = 0; by < bh; by++) {
    for (let bx = 0; bx < bw; bx++) {
      const off = (by * bw + bx) * size;
      let apal: number[] = [];
      let abits = 0;
      let pal: RGBA[];
      let idx: number;
      if (bc3) {
        [apal, abits] = alphaPalette(data, off);
        [pal, idx] = colorPalette(data, off + 8, true);
      } else {
        [pal, idx] = colorPalette(data, off, false);
      }
      for (let t = 0; t < 16; t++) {
        const x = bx * 4 + (t & 3);
        const y = by * 4 + (t >> 2);
        if (x >= w || y >= h) continue;
        const px = pal[(idx >>> (2 * t)) & 3]!;
        const o = (y * w + x) * 4;
        out[o] = px[0];
        out[o + 1] = px[1];
        out[o + 2] = px[2];
        out[o + 3] = bc3 ? apal[Math.floor(abits / 2 ** (3 * t)) % 8]! : px[3];
      }
    }
  }
  return out;
}

/** Decode a mip level to RGBA8 (SPEC §8). */
export function decodeBim(bim: Bim, mip = 0): Image {
  const m = bim.mips[mip];
  if (!m) throw new FormatError(`BIM: no mip ${mip}`);
  const { width: w, height: h } = m;
  const fmt = bim.format;
  const need = expectedSize(fmt, w, h);
  if (m.data.length < need) throw new FormatError(`BIM: mip ${mip} has ${m.data.length} bytes, needs ${need}`);
  if (fmt === FORMAT_RGBA8) return { width: w, height: h, rgba: Buffer.from(m.data.subarray(0, need)) };
  if (fmt === FORMAT_A8) {
    const out = Buffer.alloc(w * h * 4);
    for (let i = 0; i < need; i++) {
      const v = m.data[i]!;
      out[i * 4] = v;
      out[i * 4 + 1] = v;
      out[i * 4 + 2] = v;
      out[i * 4 + 3] = 255;
    }
    return { width: w, height: h, rgba: out };
  }
  return { width: w, height: h, rgba: decodeBlocks(m.data, w, h, fmt === FORMAT_BC3) };
}

/** Alias of {@link decodeBim} (the SPEC name). */
export const decode = decodeBim;

/** Build an RGBA8 BIM with one mip, header copied from `template`. */
export function encodeRgba8(template: Bim, width: number, height: number, rgba: Uint8Array): Buffer {
  if (rgba.length !== width * height * 4) throw new FormatError("RGBA buffer size does not match dimensions");
  const header = [...template.header];
  header[1] = header[11] = width;
  header[2] = header[12] = height;
  header[4] = 1;
  header[6] = FORMAT_RGBA8;
  return Buffer.concat([writeHeader(template.hash, header), mipHeader(width, height, rgba.length), toBuffer(rgba)]);
}

/** Serialise a BIM from raw parts (tests and synthetic data). */
export function buildBim(
  hash: number,
  header: ReadonlyArray<number>,
  mips: ReadonlyArray<readonly [number, number, Uint8Array]>,
): Buffer {
  const parts = [writeHeader(hash, header)];
  for (const [w, h, data] of mips) parts.push(mipHeader(w, h, data.length), toBuffer(data));
  return Buffer.concat(parts);
}
