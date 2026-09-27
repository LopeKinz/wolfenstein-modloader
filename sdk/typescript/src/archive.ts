/** Game directory access: find, read, patch in place, rebuild (SPEC §5). */

import fs from "node:fs";
import path from "node:path";
import { toBuffer } from "./bytes.js";
import { ChunkIndex, type Entry } from "./chunkIndex.js";
import { deflateExact, deflateSync, inflate, isSyncFlushed } from "./compression.js";
import { FormatError, NoFitError, WolfSdkError } from "./errors.js";
import { parseMasterIndex, type MasterPair } from "./masterIndex.js";

export const RESOURCES_HEADER = Buffer.concat([Buffer.from([0x03, 0x53, 0x45, 0x52]), Buffer.alloc(12)]);

const utf8Strict = new TextDecoder("utf-8", { fatal: true });

/** `true` if `data` is valid UTF-8 without NUL bytes (the `pad=auto` rule). */
export function looksTextual(data: Uint8Array): boolean {
  if (data.includes(0)) return false;
  try {
    utf8Strict.decode(data);
  } catch {
    return false;
  }
  return true;
}

function readAt(fd: number, position: number, length: number): Buffer {
  const out = Buffer.alloc(length);
  let done = 0;
  while (done < length) {
    const n = fs.readSync(fd, out, done, length - done, position + done);
    if (n === 0) break;
    done += n;
  }
  return done === length ? out : out.subarray(0, done);
}

function writeAt(fd: number, data: Buffer, position: number): void {
  let done = 0;
  while (done < data.length) {
    done += fs.writeSync(fd, data, done, data.length - done, position + done);
  }
}

export class Chunk {
  constructor(
    public indexName: string,
    public resourcesName: string,
    public indexPath: string,
    public resourcesPath: string,
    public index: ChunkIndex,
  ) {}

  get entries(): Entry[] {
    return this.index.entries;
  }

  saveIndex(): void {
    fs.writeFileSync(this.indexPath, this.index.toBytes());
  }
}

export interface Occurrence {
  chunk: Chunk;
  entry: Entry;
}

export type Replacements = Map<number, Uint8Array> | Record<number, Uint8Array>;

/** A game installation (the directory that contains `base/`). */
export class Game {
  readonly root: string;
  readonly base: string;
  readonly pairs: MasterPair[];
  readonly chunks: Chunk[] = [];

  constructor(root: string) {
    this.root = root;
    this.base = path.join(root, "base");
    this.pairs = parseMasterIndex(fs.readFileSync(path.join(this.base, "master.index")));
    for (const [idxName, resName] of this.pairs) {
      const ip = path.join(this.base, idxName);
      const rp = path.join(this.base, resName);
      if (!fs.existsSync(ip) || !fs.existsSync(rp)) continue;
      const ci = new ChunkIndex(fs.readFileSync(ip), fs.statSync(rp).size);
      this.chunks.push(new Chunk(idxName, resName, ip, rp, ci));
    }
  }

  static open(root: string): Game {
    return new Game(root);
  }

  *[Symbol.iterator](): Iterator<Occurrence> {
    for (const chunk of this.chunks) for (const entry of chunk.entries) yield { chunk, entry };
  }

  /** All occurrences of `type:name`, in chunk order. */
  find(type: string, name: string): Occurrence[] {
    const out: Occurrence[] = [];
    for (const chunk of this.chunks) for (const entry of chunk.index.find(type, name)) out.push({ chunk, entry });
    return out;
  }

  /** Distinct asset keys in first-appearance order. */
  keys(): string[] {
    const seen = new Set<string>();
    for (const occ of this) seen.add(occ.entry.key);
    return [...seen];
  }

  // ------------------------------------------------------------- reading

  /** The on-disk slot bytes (`csize` bytes at `offset`). */
  readRaw(occ: Occurrence): Buffer {
    const e = occ.entry;
    const fd = fs.openSync(occ.chunk.resourcesPath, "r");
    let data: Buffer;
    try {
      data = readAt(fd, e.offset, e.csize);
    } finally {
      fs.closeSync(fd);
    }
    if (data.length !== e.csize) throw new FormatError(`${e.key}: slot truncated`);
    return data;
  }

  /** The decoded payload (`usize` bytes). */
  read(occ: Occurrence): Buffer {
    const raw = this.readRaw(occ);
    const e = occ.entry;
    return e.compressed ? inflate(raw, e.usize) : raw;
  }

  /** The payload of the first occurrence, or `null` when the asset does not exist. */
  readAsset(type: string, name: string): Buffer | null {
    const occ = this.find(type, name);
    return occ.length ? this.read(occ[0]!) : null;
  }

  // ------------------------------------------------------------- writing

  /**
   * Overwrite the entry's slot without moving anything (SPEC §5.1).
   * `pad` defaults to {@link looksTextual}. Returns the previous slot bytes.
   */
  writeInPlace(occ: Occurrence, data: Uint8Array, pad?: boolean): Buffer {
    const e = occ.entry;
    const buf = toBuffer(data);
    const original = this.readRaw(occ);
    let stream: Buffer;
    let usize: number;
    if (!e.compressed) {
      if (buf.length !== e.csize) {
        throw new NoFitError(`${e.key}: stored entry needs exactly ${e.csize} bytes, got ${buf.length}`);
      }
      stream = buf;
      usize = e.usize;
    } else {
      const packed = deflateExact(buf, e.csize, pad ?? looksTextual(buf));
      if (packed === null) throw new NoFitError(`${e.key}: does not fit into its ${e.csize}-byte slot`);
      stream = packed.stream;
      usize = packed.payload.length;
    }
    const fd = fs.openSync(occ.chunk.resourcesPath, "r+");
    try {
      writeAt(fd, stream, e.offset);
    } finally {
      fs.closeSync(fd);
    }
    if (usize !== e.usize) {
      occ.chunk.index.setTriple(e, e.offset, usize, e.csize);
      occ.chunk.saveIndex();
    }
    return original;
  }

  /** Write `data` into every occurrence. Returns `[occurrence, originalSlotBytes]` pairs. */
  writeAsset(type: string, name: string, data: Uint8Array): Array<[Occurrence, Buffer]> {
    const occs = this.find(type, name);
    if (!occs.length) throw new WolfSdkError(`asset not found: ${type}:${name}`);
    return occs.map((o) => [o, this.writeInPlace(o, data)] as [Occurrence, Buffer]);
  }

  /**
   * Rewrite `chunk.resources` in original offset order (SPEC §5.2).
   * `replacements` maps entry indexes to new payloads. Writes to `dest`
   * (default: the chunk's own resources file) and persists the index.
   */
  rebuild(chunk: Chunk, replacements: Replacements, dest?: string): void {
    const repl = replacements instanceof Map ? replacements : new Map(Object.entries(replacements).map(([k, v]) => [Number(k), v]));
    const target = dest ?? chunk.resourcesPath;
    const tmp = target + ".tmp";
    const slots = new Map<string, { offset: number; csize: number; group: Entry[] }>();
    for (const e of chunk.entries) {
      const k = `${e.offset}:${e.csize}`;
      const slot = slots.get(k);
      if (slot) slot.group.push(e);
      else slots.set(k, { offset: e.offset, csize: e.csize, group: [e] });
    }
    const ordered = [...slots.values()].sort((a, b) => a.offset - b.offset || a.csize - b.csize);
    const updates: Array<[Entry, number, number, number]> = [];
    const src = fs.openSync(chunk.resourcesPath, "r");
    let out: number | null = null;
    try {
      out = fs.openSync(tmp, "w");
      writeAt(out, RESOURCES_HEADER, 0);
      let cursor = RESOURCES_HEADER.length;
      for (const { offset, csize, group } of ordered) {
        const r = group.find((e) => repl.has(e.index));
        let payload: Buffer;
        let usize: number;
        if (r === undefined) {
          payload = readAt(src, offset, csize);
          usize = group[0]!.usize;
        } else {
          const data = toBuffer(repl.get(r.index)!);
          const s = deflateSync(data);
          payload = s.length < data.length ? s : data;
          usize = data.length;
        }
        for (const e of group) updates.push([e, cursor, usize, payload.length]);
        writeAt(out, payload, cursor);
        cursor += payload.length;
        const pad = (16 - (cursor % 16)) % 16;
        if (pad) writeAt(out, Buffer.alloc(pad), cursor);
        cursor += pad;
      }
    } finally {
      fs.closeSync(src);
      if (out !== null) fs.closeSync(out);
    }
    fs.renameSync(tmp, target);
    for (const [e, off, usize, csize] of updates) chunk.index.setTriple(e, off, usize, csize);
    chunk.index.resourcesSize = fs.statSync(target).size;
    chunk.saveIndex();
  }
}

/** Return a list of invariant violations (empty = valid), SPEC §5.2. */
export function validateChunk(chunk: Chunk, checkStreams = true): string[] {
  const problems: string[] = [];
  const size = fs.statSync(chunk.resourcesPath).size;
  let last = -1;
  const seen = new Set<string>();
  const sorted = [...chunk.entries].sort((a, b) => a.offset - b.offset || a.index - b.index);
  const fd = fs.openSync(chunk.resourcesPath, "r");
  try {
    for (const e of sorted) {
      const k = `${e.offset}:${e.csize}`;
      if (seen.has(k)) continue;
      seen.add(k);
      if (e.offset <= last) problems.push(`${e.key}: offset ${e.offset} not ascending`);
      last = e.offset;
      if (e.offset % 16) problems.push(`${e.key}: offset ${e.offset} not 16-byte aligned`);
      if (e.offset + e.csize > size) {
        problems.push(`${e.key}: slot exceeds resources file`);
        continue;
      }
      if (checkStreams && e.compressed && !isSyncFlushed(readAt(fd, e.offset, e.csize))) {
        problems.push(`${e.key}: stream not sync-flushed`);
      }
    }
  } finally {
    fs.closeSync(fd);
  }
  return problems;
}
