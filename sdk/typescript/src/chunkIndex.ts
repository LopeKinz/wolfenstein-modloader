/**
 * `base/chunkN.index` (SPEC §3).
 *
 * The index is never re-serialised: the original buffer is kept and only the
 * 12-byte offset/usize/csize triple of an entry is overwritten.
 */

import { asciiBytes, toBuffer } from "./bytes.js";
import { FormatError } from "./errors.js";

export const MAGIC = Buffer.from([0x03, 0x53, 0x45, 0x52]);
export const ENTRIES_START = 0x2c;
const MAX_STRING = 1024;

export class Entry {
  constructor(
    public index: number,
    public type: string,
    public name: string,
    public path: string,
    public offset: number,
    public usize: number,
    public csize: number,
    /** Byte position of the entry in the index. */
    public start: number,
    /** Byte position of the `offset` field. */
    public tripleOffset: number,
    public trailer: Buffer = Buffer.alloc(0),
  ) {}

  /** `"type:name"`. */
  get key(): string {
    return `${this.type}:${this.name}`;
  }

  get compressed(): boolean {
    return this.csize !== this.usize;
  }

  /** Offset into the streamed audio container (`sample` entries), SPEC §3.3. */
  get streamOffset(): number | null {
    if (this.trailer.length < 24) return null;
    return this.trailer.readUInt32BE(20);
  }
}

function stringAt(buf: Buffer, pos: number): [string, number] | null {
  if (pos + 4 > buf.length) return null;
  const n = buf.readUInt32LE(pos);
  if (n < 1 || n > MAX_STRING || pos + 4 + n > buf.length) return null;
  for (let i = pos + 4; i < pos + 4 + n; i++) {
    const b = buf[i]!;
    if (b < 0x20 || b > 0x7e) return null;
  }
  return [buf.toString("latin1", pos + 4, pos + 4 + n), pos + 4 + n];
}

interface Plausible {
  strings: [string, string, string];
  triple: number;
  offset: number;
  usize: number;
  csize: number;
}

function plausible(buf: Buffer, pos: number, resourcesSize: number | null): Plausible | null {
  const strings: string[] = [];
  let p = pos;
  for (let i = 0; i < 3; i++) {
    const r = stringAt(buf, p);
    if (r === null) return null;
    strings.push(r[0]);
    p = r[1];
  }
  if (p + 12 > buf.length) return null;
  const offset = buf.readUInt32BE(p);
  const usize = buf.readUInt32BE(p + 4);
  const csize = buf.readUInt32BE(p + 8);
  if (!(csize > 0 && csize <= usize)) return null;
  if (resourcesSize !== null) {
    if (offset + csize > resourcesSize) return null;
  } else if (offset < 16) {
    return null;
  }
  return { strings: strings as [string, string, string], triple: p, offset, usize, csize };
}

/** A parsed chunk index backed by its original bytes. */
export class ChunkIndex {
  readonly buffer: Buffer;
  resourcesSize: number | null;
  readonly counterA: number;
  readonly counterB: number;
  readonly entries: Entry[] = [];

  constructor(data: Uint8Array, resourcesSize: number | null = null) {
    const src = toBuffer(data);
    if (!src.subarray(0, 4).equals(MAGIC)) throw new FormatError("chunk index: bad magic");
    if (src.length < ENTRIES_START) throw new FormatError("chunk index: truncated header");
    this.buffer = Buffer.from(src); // private copy
    this.resourcesSize = resourcesSize;
    this.counterA = this.buffer.readUInt32BE(0x20);
    this.counterB = this.buffer.readUInt32BE(0x24);
    this.scan();
  }

  static parse(data: Uint8Array, resourcesSize: number | null = null): ChunkIndex {
    return new ChunkIndex(data, resourcesSize);
  }

  private scan(): void {
    const buf = this.buffer;
    const found: Plausible[] = [];
    const starts: number[] = [];
    let pos = ENTRIES_START;
    while (pos < buf.length) {
      const r = plausible(buf, pos, this.resourcesSize);
      if (r === null) {
        pos += 1;
        continue;
      }
      found.push(r);
      starts.push(pos);
      pos = r.triple + 12;
    }
    found.forEach((f, i) => {
      const end = i + 1 < found.length ? starts[i + 1]! : buf.length;
      const trailer = Buffer.from(buf.subarray(f.triple + 12, end));
      const [type, name, path] = f.strings;
      this.entries.push(new Entry(i, type, name, path, f.offset, f.usize, f.csize, starts[i]!, f.triple, trailer));
    });
  }

  get length(): number {
    return this.entries.length;
  }

  [Symbol.iterator](): Iterator<Entry> {
    return this.entries[Symbol.iterator]();
  }

  find(type: string, name: string): Entry[] {
    return this.entries.filter((e) => e.type === type && e.name === name);
  }

  byKey(): Map<string, Entry[]> {
    const out = new Map<string, Entry[]>();
    for (const e of this.entries) {
      const list = out.get(e.key);
      if (list) list.push(e);
      else out.set(e.key, [e]);
    }
    return out;
  }

  /** Overwrite the 12-byte triple of `entry` in the buffer (SPEC §3.2). */
  setTriple(entry: Entry, offset: number, usize: number, csize: number): void {
    this.buffer.writeUInt32BE(offset, entry.tripleOffset);
    this.buffer.writeUInt32BE(usize, entry.tripleOffset + 4);
    this.buffer.writeUInt32BE(csize, entry.tripleOffset + 8);
    entry.offset = offset;
    entry.usize = usize;
    entry.csize = csize;
  }

  toBytes(): Buffer {
    return Buffer.from(this.buffer);
  }
}

/** `[type, name, path, offset, usize, csize, trailer]` */
export type ChunkIndexEntrySpec = [string, string, string, number, number, number, Uint8Array];

/**
 * Build a chunk index from entry tuples. Used for tests and synthetic
 * archives; the game's own files are never re-serialised.
 */
export function buildChunkIndex(
  entries: ReadonlyArray<ChunkIndexEntrySpec>,
  counterA = 0,
  counterB: number | null = null,
): Buffer {
  const body: Buffer[] = [];
  for (const [type, name, path, offset, usize, csize, trailer] of entries) {
    for (const s of [type, name, path]) {
      const raw = asciiBytes(s);
      const len = Buffer.alloc(4);
      len.writeUInt32LE(raw.length, 0);
      body.push(len, raw);
    }
    const tri = Buffer.alloc(12);
    tri.writeUInt32BE(offset, 0);
    tri.writeUInt32BE(usize, 4);
    tri.writeUInt32BE(csize, 8);
    body.push(tri, toBuffer(trailer));
  }
  const bodyBuf = Buffer.concat(body);
  const cb = counterB ?? entries.length;
  const head = Buffer.alloc(ENTRIES_START);
  MAGIC.copy(head, 0);
  head.writeUInt32BE(ENTRIES_START + bodyBuf.length - 32, 4);
  head.writeUInt32BE(counterA, 0x20);
  head.writeUInt32BE(cb, 0x24);
  return Buffer.concat([head, bodyBuf]);
}
