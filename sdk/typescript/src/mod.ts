/** `mod.json` loading, layering and conflict detection (SPEC §7). */

import fs from "node:fs";
import path from "node:path";
import { Decl, type Value } from "./decl.js";
import { DeclError, ModError } from "./errors.js";

export type GameId = "tno" | "tnc";

/** The changes one mod makes to one asset. */
export class AssetChange {
  constructor(
    /** `"type:name"`. */
    public key: string,
    /** Decl path → value, in file order. */
    public set: Array<[string, Value]> = [],
    /** Replacement file, relative to the mod directory. */
    public file: string | null = null,
  ) {}

  get type(): string {
    return this.key.slice(0, this.key.indexOf(":"));
  }

  get name(): string {
    return this.key.slice(this.key.indexOf(":") + 1);
  }
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v) && !(v instanceof Map);
}

function entriesOf(v: Record<string, unknown> | Map<string, unknown>): Array<[string, unknown]> {
  return v instanceof Map ? [...v.entries()] : Object.entries(v);
}

function has(obj: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

export class Mod {
  constructor(
    public name: string,
    public priority = 0,
    public game: GameId | null = null,
    public version: string | null = null,
    public description: string | null = null,
    public assets: AssetChange[] = [],
    /** Directory the mod was loaded from (resolves `file` paths). */
    public root: string | null = null,
  ) {}

  /**
   * Build a mod from a parsed `mod.json` object. `assets` and `set` may also
   * be `Map`s, which preserve the order of integer-like keys.
   */
  static fromObject(data: unknown, defaultName = "mod", root: string | null = null): Mod {
    if (!isPlainObject(data)) throw new ModError("mod.json must contain a JSON object");
    const prio = has(data, "priority") ? data.priority : 0;
    if (typeof prio !== "number" || !Number.isInteger(prio)) throw new ModError("priority must be an integer");
    const game = has(data, "game") ? data.game : data.spiel;
    if (game !== undefined && game !== null && game !== "tno" && game !== "tnc") {
      throw new ModError(`unknown game ${JSON.stringify(game)} (expected 'tno' or 'tnc')`);
    }
    let rawAssets: Array<[string, unknown]>;
    if (has(data, "assets")) {
      const a = data.assets;
      if (!(isPlainObject(a) || a instanceof Map)) throw new ModError("assets must be an object");
      rawAssets = entriesOf(a as Record<string, unknown> | Map<string, unknown>);
    } else {
      rawAssets = Object.entries(data).filter(([k]) => k.includes(":"));
    }
    const assets: AssetChange[] = [];
    for (const [key, change] of rawAssets) {
      if (!key.includes(":") || key.startsWith(":") || key.endsWith(":")) {
        throw new ModError(`asset key ${JSON.stringify(key)} must be 'type:name'`);
      }
      if (!isPlainObject(change)) throw new ModError(`${key}: change must be an object`);
      const sets = change.set ?? null;
      const file = change.file ?? null;
      if (sets === null && file === null) throw new ModError(`${key}: needs 'set' or 'file'`);
      if (sets !== null && !(isPlainObject(sets) || sets instanceof Map)) {
        throw new ModError(`${key}: 'set' must be an object`);
      }
      if (file !== null && typeof file !== "string") throw new ModError(`${key}: 'file' must be a string`);
      const pairs: Array<[string, Value]> = [];
      if (sets !== null) {
        for (const [p, value] of entriesOf(sets as Record<string, unknown> | Map<string, unknown>)) {
          if (typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean") {
            throw new ModError(`${key}: value of ${JSON.stringify(p)} must be string, number or bool`);
          }
          pairs.push([p, value]);
        }
      }
      assets.push(new AssetChange(key, pairs, file as string | null));
    }
    const name = typeof data.name === "string" && data.name ? data.name : defaultName;
    const str = (v: unknown): string | null => (typeof v === "string" ? v : null);
    return new Mod(name, prio, (game ?? null) as GameId | null, str(data.version), str(data.description), assets, root);
  }

  /** Parse `mod.json` text. */
  static loads(text: string, defaultName = "mod", root: string | null = null): Mod {
    let data: unknown;
    try {
      data = JSON.parse(text);
    } catch (e) {
      throw new ModError(`invalid JSON: ${(e as Error).message}`);
    }
    return Mod.fromObject(data, defaultName, root);
  }

  /** Load `mod.json`, or a directory containing one. */
  static load(p: string): Mod {
    let file = p;
    if (fs.existsSync(file) && fs.statSync(file).isDirectory()) file = path.join(file, "mod.json");
    const root = path.dirname(path.resolve(file));
    return Mod.loads(fs.readFileSync(file, "utf-8"), path.basename(root), root);
  }

  /** Read a file relative to the mod directory. */
  readFile(rel: string): string {
    if (this.root === null) {
      throw new ModError(`${this.name}: cannot resolve file ${JSON.stringify(rel)} without a mod directory`);
    }
    return fs.readFileSync(path.join(this.root, rel), "utf-8");
  }
}

export type ConflictKind = "path" | "file" | "file-over-set";

export interface Conflict {
  kind: ConflictKind;
  asset: string;
  path: string | null;
  mods: string[];
  winner: string;
}

export interface ModErrorEntry {
  asset: string;
  mod: string;
  message: string;
}

export interface ApplyResult {
  /** Asset key → new Decl text, in first-appearance order. */
  results: Map<string, string>;
  conflicts: Conflict[];
  errors: ModErrorEntry[];
}

/** Returns the asset text, or `null`/`undefined` when it does not exist. */
export type ReadAsset = (type: string, name: string) => string | Uint8Array | null | undefined;
export type ReadFile = (mod: Mod, rel: string) => string | Uint8Array;

const utf8 = new TextDecoder("utf-8");
const asText = (v: string | Uint8Array): string => (typeof v === "string" ? v : utf8.decode(v));

/** Sort by priority ascending; ties keep input order. */
export function sortMods(mods: ReadonlyArray<Mod>): Mod[] {
  return [...mods].sort((a, b) => a.priority - b.priority);
}

function isReadError(e: unknown): boolean {
  return e instanceof ModError || (e instanceof Error && typeof (e as NodeJS.ErrnoException).code === "string");
}

/** Layer `mods` over the assets returned by `readAsset` (SPEC §7.1). */
export function applyMods(mods: ReadonlyArray<Mod>, readAsset: ReadAsset, readFile?: ReadFile): ApplyResult {
  const rf: ReadFile = readFile ?? ((m, rel) => m.readFile(rel));
  const perAsset = new Map<string, Array<[Mod, AssetChange]>>();
  for (const m of sortMods(mods)) {
    for (const ch of m.assets) {
      const list = perAsset.get(ch.key);
      if (list) list.push([m, ch]);
      else perAsset.set(ch.key, [[m, ch]]);
    }
  }

  const results = new Map<string, string>();
  const conflicts: Conflict[] = [];
  const errors: ModErrorEntry[] = [];

  for (const [key, changes] of perAsset) {
    // conflicts
    const fileMods = changes.filter(([, ch]) => ch.file !== null).map(([m]) => m.name);
    if (fileMods.length > 1) {
      conflicts.push({ kind: "file", asset: key, path: null, mods: fileMods, winner: fileMods[fileMods.length - 1]! });
    }
    const pathMods = new Map<string, string[]>();
    for (const [m, ch] of changes) {
      for (const [p] of ch.set) {
        let list = pathMods.get(p);
        if (!list) pathMods.set(p, (list = []));
        if (!list.includes(m.name)) list.push(m.name);
      }
    }
    for (const [p, names] of pathMods) {
      if (names.length > 1) conflicts.push({ kind: "path", asset: key, path: p, mods: names, winner: names[names.length - 1]! });
    }
    changes.forEach(([m, ch], i) => {
      if (ch.file === null) return;
      const setters = changes
        .slice(0, i)
        .filter(([pm, pch]) => pch.set.length > 0 && pm !== m)
        .map(([pm]) => pm.name);
      if (setters.length) {
        conflicts.push({ kind: "file-over-set", asset: key, path: null, mods: [...setters, m.name], winner: m.name });
      }
    });

    const colon = key.indexOf(":");
    const found = readAsset(key.slice(0, colon), key.slice(colon + 1));
    if (found === null || found === undefined) {
      errors.push({ asset: key, mod: changes[0]![0].name, message: "asset not found" });
      continue;
    }
    let text = asText(found);
    for (const [m, ch] of changes) {
      if (ch.file !== null) {
        try {
          text = asText(rf(m, ch.file));
        } catch (e) {
          if (!isReadError(e)) throw e;
          errors.push({ asset: key, mod: m.name, message: `cannot read ${ch.file}: ${(e as Error).message}` });
          continue;
        }
      }
      if (!ch.set.length) continue;
      let decl: Decl;
      try {
        decl = new Decl(text);
      } catch (e) {
        if (!(e instanceof DeclError)) throw e;
        errors.push({ asset: key, mod: m.name, message: e.message });
        continue;
      }
      for (const [p, v] of ch.set) {
        try {
          decl.set(p, v);
        } catch (e) {
          if (!(e instanceof DeclError)) throw e;
          errors.push({ asset: key, mod: m.name, message: e.message });
        }
      }
      text = decl.text;
    }
    results.set(key, text);
  }
  return { results, conflicts, errors };
}
