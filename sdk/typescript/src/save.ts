/** Save-game `MD5_BlockChecksum` header (SPEC §11). */

import crypto from "node:crypto";
import { equalBytes, toBuffer } from "./bytes.js";
import { FormatError } from "./errors.js";

/** `BE u32(w0 ^ w1 ^ w2 ^ w3)` over the LE u32 words of `MD5(payload)`. */
export function saveChecksum(payload: Uint8Array): Buffer {
  const d = crypto.createHash("md5").update(payload).digest();
  const x = (d.readUInt32LE(0) ^ d.readUInt32LE(4) ^ d.readUInt32LE(8) ^ d.readUInt32LE(12)) >>> 0;
  const out = Buffer.alloc(4);
  out.writeUInt32BE(x, 0);
  return out;
}

/** `true` if the first four bytes are the checksum of the rest. */
export function verifySave(data: Uint8Array): boolean {
  return data.length >= 4 && equalBytes(data.subarray(0, 4), saveChecksum(data.subarray(4)));
}

/** Return `data` with a corrected checksum header. */
export function fixSave(data: Uint8Array): Buffer {
  if (data.length < 4) throw new FormatError("save file shorter than its 4-byte header");
  const payload = toBuffer(data).subarray(4);
  return Buffer.concat([saveChecksum(payload), payload]);
}
