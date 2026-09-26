/** `base/master.index` (SPEC §2). */

import { asciiBytes, asciiReplace, toBuffer } from "./bytes.js";
import { FormatError } from "./errors.js";

export const MAGIC = Buffer.from([0x03, 0x53, 0x45, 0x52]);

/** A `[indexName, resourcesName]` pair. */
export type MasterPair = [string, string];

export function parseMasterIndex(data: Uint8Array): MasterPair[] {
  const buf = toBuffer(data);
  if (!buf.subarray(0, 4).equals(MAGIC)) throw new FormatError("master.index: bad magic");
  if (buf.length < 8) throw new FormatError("master.index: truncated header");
  const count = buf.readUInt32BE(4);
  let pos = 8;
  const names: string[] = [];
  for (let i = 0; i < count * 2; i++) {
    if (pos + 4 > buf.length) throw new FormatError("master.index: truncated");
    const length = buf.readUInt32LE(pos);
    pos += 4;
    if (pos + length > buf.length) throw new FormatError("master.index: truncated name");
    const raw = buf.subarray(pos, pos + length);
    const nul = raw.indexOf(0);
    names.push(asciiReplace(nul < 0 ? raw : raw.subarray(0, nul)));
    pos += length;
  }
  const pairs: MasterPair[] = [];
  for (let i = 0; i < names.length; i += 2) pairs.push([names[i]!, names[i + 1]!]);
  return pairs;
}

export function buildMasterIndex(pairs: ReadonlyArray<readonly [string, string]>): Buffer {
  const parts: Buffer[] = [MAGIC];
  const head = Buffer.alloc(4);
  head.writeUInt32BE(pairs.length, 0);
  parts.push(head);
  for (const pair of pairs) {
    for (const name of pair) {
      const raw = Buffer.concat([asciiBytes(name), Buffer.from([0])]);
      const len = Buffer.alloc(4);
      len.writeUInt32LE(raw.length, 0);
      parts.push(len, raw);
    }
  }
  return Buffer.concat(parts);
}
