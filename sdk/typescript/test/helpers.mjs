import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const VEC = process.env.WOLFSDK_VECTORS ?? path.resolve(HERE, "..", "..", "..", "spec", "vectors");

export const load = (rel) => JSON.parse(fs.readFileSync(path.join(VEC, rel), "utf-8"));
export const readBytes = (rel) => fs.readFileSync(path.join(VEC, rel));
export const readText = (rel) => fs.readFileSync(path.join(VEC, rel), "utf-8");
export const sha = (b) => crypto.createHash("sha256").update(b).digest("hex");
export const hex = (h) => Buffer.from(h, "hex");
/** The padding a padded write may append: nothing or a Decl line comment. */
export const PAD_RE = /^(\n\/\/ *)?$/;
