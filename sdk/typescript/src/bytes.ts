/** Internal byte helpers (not part of the public API). */

export function toBuffer(data: Uint8Array): Buffer {
  return Buffer.isBuffer(data) ? data : Buffer.from(data.buffer, data.byteOffset, data.byteLength);
}

/** Decode ASCII the way Python's ``decode("ascii", "replace")`` does. */
export function asciiReplace(data: Uint8Array): string {
  let s = "";
  for (const b of data) s += b < 0x80 ? String.fromCharCode(b) : "�";
  return s;
}

export function asciiBytes(s: string): Buffer {
  for (let i = 0; i < s.length; i++) {
    if (s.charCodeAt(i) > 0x7f) throw new RangeError(`non-ASCII character in ${JSON.stringify(s)}`);
  }
  return Buffer.from(s, "latin1");
}

export function equalBytes(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}
