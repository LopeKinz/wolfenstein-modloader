/**
 * Decl parser and span-preserving editor (SPEC §6).
 *
 * Every value remembers its source span; editing replaces exactly that span
 * and re-parses. Parsing and rendering without edits returns the input
 * unchanged.
 */

import { DeclError } from "./errors.js";

const PUNCT = "{}()=;,";
const WS = " \t\r\n\f\v";
const NUMERIC = /^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?f?$/;

/** A value accepted by {@link Decl.set}. */
export type Value = string | number | boolean;

export type NodeKind = "string" | "word" | "vector" | "block" | "tuple" | "flag";

interface Token {
  kind: "word" | "string" | "punct" | "eof";
  text: string;
  start: number;
  end: number;
  line: number;
}

/** A parsed value with its source span. */
export class Node {
  constructor(
    public path: string[],
    public kind: NodeKind,
    public start: number,
    public end: number,
    public raw: string,
  ) {}

  /** The path in its string form (`edit.damageParms.maxDamage`). */
  get pathStr(): string {
    return formatPath(this.path);
  }
}

/** `[path, kind, raw]` as returned by {@link Decl.paths}. */
export type PathTuple = [string, NodeKind, string];

export function isNumeric(text: string): boolean {
  return NUMERIC.test(text);
}

function countNewlines(text: string, from: number, to: number): number {
  let c = 0;
  for (let i = from; i < to; i++) if (text.charCodeAt(i) === 10) c++;
  return c;
}

export function tokenize(text: string): Token[] {
  const tokens: Token[] = [];
  const n = text.length;
  let i = 0;
  let line = 0;
  while (i < n) {
    const c = text[i]!;
    if (WS.includes(c)) {
      if (c === "\n") line++;
      i++;
      continue;
    }
    if (c === "/" && i + 1 < n && text[i + 1] === "/") {
      const j = text.indexOf("\n", i);
      i = j < 0 ? n : j;
      continue;
    }
    if (c === "/" && i + 1 < n && text[i + 1] === "*") {
      const j = text.indexOf("*/", i + 2);
      const end = j < 0 ? n : j + 2;
      line += countNewlines(text, i, end);
      i = end;
      continue;
    }
    if (c === '"') {
      let j = i + 1;
      while (j < n && text[j] !== '"') j += text[j] === "\\" ? 2 : 1;
      if (j >= n) throw new DeclError(`unterminated string at offset ${i}`);
      tokens.push({ kind: "string", text: text.slice(i, j + 1), start: i, end: j + 1, line });
      line += countNewlines(text, i, j + 1);
      i = j + 1;
      continue;
    }
    if (PUNCT.includes(c)) {
      tokens.push({ kind: "punct", text: c, start: i, end: i + 1, line });
      i++;
      continue;
    }
    let j = i;
    while (j < n) {
      const d = text[j]!;
      if (WS.includes(d) || PUNCT.includes(d) || d === '"') break;
      if (d === "/" && j + 1 < n && (text[j + 1] === "/" || text[j + 1] === "*")) break;
      j++;
    }
    tokens.push({ kind: "word", text: text.slice(i, j), start: i, end: j, line });
    i = j;
  }
  tokens.push({ kind: "eof", text: "", start: n, end: n, line });
  return tokens;
}

/** Remove the quotes of a string token and resolve `\x` escapes. */
export function unquote(raw: string): string {
  const body = raw.slice(1, -1);
  let out = "";
  let i = 0;
  while (i < body.length) {
    if (body[i] === "\\" && i + 1 < body.length) {
      out += body[i + 1];
      i += 2;
    } else {
      out += body[i];
      i += 1;
    }
  }
  return out;
}

export function quote(value: string): string {
  return '"' + value.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
}

// ----------------------------------------------------------------- paths

function needsQuote(seg: string): boolean {
  if (seg === "") return true;
  for (const c of seg) if ('."\\'.includes(c) || WS.includes(c)) return true;
  return false;
}

export function formatPath(path: ReadonlyArray<string>): string {
  return path.map((s) => (needsQuote(s) ? quote(s) : s)).join(".");
}

export function parsePath(path: string): string[] {
  const segs: string[] = [];
  const n = path.length;
  let i = 0;
  for (;;) {
    if (i < n && path[i] === '"') {
      let j = i + 1;
      let buf = "";
      while (j < n && path[j] !== '"') {
        if (path[j] === "\\" && j + 1 < n) {
          buf += path[j + 1];
          j += 2;
        } else {
          buf += path[j];
          j += 1;
        }
      }
      if (j >= n) throw new DeclError(`unterminated quote in path ${JSON.stringify(path)}`);
      segs.push(buf);
      i = j + 1;
      if (i < n && path[i] !== ".") throw new DeclError(`bad path ${JSON.stringify(path)}`);
    } else {
      let j = path.indexOf(".", i);
      if (j < 0) j = n;
      const seg = path.slice(i, j);
      if (!seg) throw new DeclError(`empty segment in path ${JSON.stringify(path)}`);
      segs.push(seg);
      i = j;
    }
    if (i >= n) break;
    i += 1; // skip '.'
    if (i >= n) throw new DeclError(`empty segment in path ${JSON.stringify(path)}`);
  }
  return segs;
}

function pathKey(path: ReadonlyArray<string>): string {
  return JSON.stringify(path);
}

// ---------------------------------------------------------------- parser

class Scope {
  private seen = new Map<string, number>();
  private pos = 0;

  positional(): string {
    return `[${this.pos++}]`;
  }

  unique(key: string): string {
    const c = this.seen.get(key) ?? 0;
    this.seen.set(key, c + 1);
    return c === 0 ? key : `${key}#${c}`;
  }
}

class Parser {
  private toks: Token[];
  private i = 0;
  readonly nodes: Node[] = [];

  constructor(private text: string) {
    this.toks = tokenize(text);
  }

  private peek(k = 0): Token {
    return this.toks[Math.min(this.i + k, this.toks.length - 1)]!;
  }

  private next(): Token {
    const t = this.toks[this.i]!;
    if (t.kind !== "eof") this.i++;
    return t;
  }

  private isP(t: Token, chars: string): boolean {
    return t.kind === "punct" && chars.includes(t.text);
  }

  run(): Node[] {
    const scope = new Scope();
    if (this.isP(this.peek(), "{")) {
      this.next();
      this.items(scope, [], false);
    }
    this.items(scope, [], true);
    return this.nodes;
  }

  private add(scope: Scope, parent: string[], key: string, kind: NodeKind, start: number, end: number): string[] {
    const path = [...parent, scope.unique(key)];
    this.nodes.push(new Node(path, kind, start, end, this.text.slice(start, end)));
    return path;
  }

  private items(scope: Scope, parent: string[], top: boolean): void {
    for (;;) {
      const t = this.peek();
      if (t.kind === "eof") {
        if (!top) throw new DeclError("unexpected end of input: missing '}'");
        return;
      }
      if (this.isP(t, ";,=)")) {
        this.next();
        continue;
      }
      if (this.isP(t, "}")) {
        this.next();
        if (top) continue;
        return;
      }
      if (this.isP(t, "{")) {
        this.block(scope, parent, scope.positional());
        continue;
      }
      if (this.isP(t, "(")) {
        this.next();
        const end = this.tupleEnd();
        this.add(scope, parent, scope.positional(), "tuple", t.start, end);
        continue;
      }
      this.item(scope, parent, t);
      if (this.isP(this.peek(), ";")) this.next();
    }
  }

  private block(scope: Scope, parent: string[], key: string): void {
    const open = this.next();
    const idx = this.nodes.length;
    const path = this.add(scope, parent, key, "block", open.start, open.end);
    this.items(new Scope(), path, false);
    const end = this.toks[this.i - 1]!.end;
    const node = this.nodes[idx]!;
    node.end = end;
    node.raw = this.text.slice(node.start, end);
  }

  private tupleEnd(): number {
    let depth = 1;
    for (;;) {
      const t = this.next();
      if (t.kind === "eof") throw new DeclError("unexpected end of input: missing ')'");
      if (this.isP(t, "(")) depth++;
      else if (this.isP(t, ")")) {
        depth--;
        if (depth === 0) return t.end;
      }
    }
  }

  private keyText(t: Token): string {
    return t.kind === "string" ? unquote(t.text) : t.text;
  }

  private item(scope: Scope, parent: string[], t: Token): void {
    this.next();
    const n = this.peek();
    if (n.kind === "eof" || this.isP(n, ",}")) {
      this.add(scope, parent, scope.positional(), t.kind as NodeKind, t.start, t.end);
      return;
    }
    const key = this.keyText(t);
    if (this.isP(n, "=")) {
      this.next();
      this.value(scope, parent, key);
      return;
    }
    if (this.isP(n, "{")) {
      this.block(scope, parent, key);
      return;
    }
    if (this.isP(n, "(")) {
      this.next();
      const end = this.tupleEnd();
      this.add(scope, parent, key, "tuple", n.start, end);
      return;
    }
    if ((n.kind === "word" || n.kind === "string") && n.line === t.line) {
      const after = this.peek(1);
      const qualifier = n.kind === "string" || !isNumeric(n.text);
      if (qualifier && this.isP(after, "{")) {
        this.next();
        this.block(scope, parent, key + ":" + this.keyText(n));
        return;
      }
      this.next();
      let kind: NodeKind = n.kind;
      let end = n.end;
      let last = n;
      for (;;) {
        const v = this.peek();
        if (v.kind === "word" && isNumeric(v.text) && v.line === last.line && !this.isP(this.peek(1), "=")) {
          this.next();
          kind = "vector";
          end = v.end;
          last = v;
        } else break;
      }
      this.add(scope, parent, key, kind, n.start, end);
      return;
    }
    this.add(scope, parent, key, "flag", t.start, t.end);
  }

  private value(scope: Scope, parent: string[], key: string): void {
    const v = this.peek();
    if (this.isP(v, "{")) this.block(scope, parent, key);
    else if (this.isP(v, "(")) {
      this.next();
      const end = this.tupleEnd();
      this.add(scope, parent, key, "tuple", v.start, end);
    } else if (v.kind === "word" || v.kind === "string") {
      this.next();
      this.add(scope, parent, key, v.kind, v.start, v.end);
    } else {
      throw new DeclError(`missing value for ${JSON.stringify(key)} at offset ${v.start}`);
    }
  }
}

// ------------------------------------------------------------ formatting

/** Expand JS exponent notation (`1e-7`, `1.5e+21`) to a plain decimal string. */
function expandExponent(s: string): string {
  const m = /^(-?)(\d+)(?:\.(\d+))?e([+-]\d+)$/.exec(s);
  if (!m) return s;
  const sign = m[1]!;
  const digits = m[2]! + (m[3] ?? "");
  const point = m[2]!.length + Number(m[4]); // position of the decimal point in `digits`
  let out: string;
  if (point <= 0) out = "0." + "0".repeat(-point) + digits;
  else if (point >= digits.length) out = digits + "0".repeat(point - digits.length);
  else out = digits.slice(0, point) + "." + digits.slice(point);
  return sign + out;
}

/** Shortest round-trip decimal without exponent; integers without `.0`; `-0` → `0`. */
export function shortestNumber(v: number): string {
  if (v === 0) return "0";
  return expandExponent(String(v));
}

export function formatNumber(value: number, raw: string | null): string {
  if (!Number.isFinite(value)) throw new DeclError(`cannot write non-finite number ${value}`);
  let s = shortestNumber(value);
  if (!raw || !isNumeric(raw)) return s;
  const suffix = raw.endsWith("f") ? "f" : "";
  const mant = raw.replace(/f+$/, "").split(/[eE]/)[0]!;
  const dot = mant.indexOf(".");
  if (dot >= 0) {
    const digits = mant.length - dot - 1;
    if (!s.includes(".")) s += ".";
    const frac = s.length - s.indexOf(".") - 1;
    if (frac < digits) s += "0".repeat(digits - frac);
  }
  return s + suffix;
}

/** `format(node, value)` — the text `set` splices into the node's span (SPEC §6.5). */
export function formatValue(node: { kind: string; raw: string }, value: Value): string {
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return formatNumber(value, node.kind === "word" ? node.raw : null);
  if (typeof value === "string") return node.kind === "string" ? quote(value) : value;
  throw new DeclError(`unsupported value type ${typeof value}`);
}

// ------------------------------------------------------------------ Decl

/** A parsed Decl with span-preserving edits. */
export class Decl {
  private _text = "";
  private _nodes: Node[] = [];
  private _index = new Map<string, Node>();

  constructor(text: string) {
    this.reparse(text);
  }

  static parse(text: string): Decl {
    return new Decl(text);
  }

  /** The current source text. */
  get text(): string {
    return this._text;
  }

  render(): string {
    return this._text;
  }

  /** Every node including blocks, in source order. */
  nodes(): Node[] {
    return [...this._nodes];
  }

  /** `[path, kind, raw]` for every non-block node, in source order. */
  paths(): PathTuple[] {
    return this._nodes.filter((n) => n.kind !== "block").map((n) => [n.pathStr, n.kind, n.raw] as PathTuple);
  }

  [Symbol.iterator](): Iterator<PathTuple> {
    return this.paths()[Symbol.iterator]();
  }

  node(path: string): Node {
    const n = this._index.get(pathKey(parsePath(path)));
    if (!n) throw new DeclError(`unknown path ${JSON.stringify(path)}`);
    return n;
  }

  has(path: string): boolean {
    try {
      return this._index.has(pathKey(parsePath(path)));
    } catch (e) {
      if (e instanceof DeclError) return false;
      throw e;
    }
  }

  /** Exact source text of the value. */
  raw(path: string): string {
    return this.node(path).raw;
  }

  /** Raw text, except strings are unquoted and unescaped. */
  get(path: string): string {
    const n = this.node(path);
    return n.kind === "string" ? unquote(n.raw) : n.raw;
  }

  /** Splice `text` verbatim into the value's span. */
  setRaw(path: string, text: string): this {
    const n = this.node(path);
    this.reparse(this._text.slice(0, n.start) + text + this._text.slice(n.end));
    return this;
  }

  /** Replace a value, keeping the original number formatting (SPEC §6.5). */
  set(path: string, value: Value): this {
    const n = this.node(path);
    if (n.kind === "flag") throw new DeclError(`${JSON.stringify(path)} is a flag without value; use setRaw`);
    return this.setRaw(path, formatValue(n, value));
  }

  private reparse(text: string): void {
    const nodes = new Parser(text).run();
    this._text = text;
    this._nodes = nodes;
    this._index = new Map(nodes.map((n) => [pathKey(n.path), n]));
  }

  toString(): string {
    return this._text;
  }
}
