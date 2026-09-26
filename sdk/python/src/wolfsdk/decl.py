"""Decl parser and span-preserving editor (SPEC §6).

Every value remembers its source span; editing replaces exactly that span and
re-parses. Parsing and rendering without edits returns the input unchanged.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterator, List, Optional, Tuple, Union

from .errors import DeclError

_PUNCT = "{}()=;,"
_WS = " \t\r\n\f\v"
_NUMERIC = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?f?$")

Value = Union[str, int, float, bool]


@dataclass
class Token:
    kind: str  # "word", "string", "punct", "eof"
    text: str
    start: int
    end: int
    line: int


@dataclass
class Node:
    path: Tuple[str, ...]
    kind: str  # string, word, vector, block, tuple, flag
    start: int
    end: int
    raw: str

    @property
    def path_str(self) -> str:
        return format_path(self.path)


def is_numeric(text: str) -> bool:
    return bool(_NUMERIC.match(text))


def tokenize(text: str) -> List[Token]:
    tokens: List[Token] = []
    i, n, line = 0, len(text), 0
    while i < n:
        c = text[i]
        if c in _WS:
            if c == "\n":
                line += 1
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            end = n if j < 0 else j + 2
            line += text.count("\n", i, end)
            i = end
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise DeclError(f"unterminated string at offset {i}")
            tokens.append(Token("string", text[i : j + 1], i, j + 1, line))
            line += text.count("\n", i, j + 1)
            i = j + 1
            continue
        if c in _PUNCT:
            tokens.append(Token("punct", c, i, i + 1, line))
            i += 1
            continue
        j = i
        while j < n:
            d = text[j]
            if d in _WS or d in _PUNCT or d == '"':
                break
            if d == "/" and j + 1 < n and text[j + 1] in "/*":
                break
            j += 1
        tokens.append(Token("word", text[i:j], i, j, line))
        i = j
    tokens.append(Token("eof", "", n, n, line))
    return tokens


def unquote(raw: str) -> str:
    body = raw[1:-1]
    out, i = [], 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            out.append(body[i + 1])
            i += 2
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------------- paths

def _needs_quote(seg: str) -> bool:
    return any(c in seg for c in '."\\') or any(c in _WS for c in seg) or seg == ""


def format_path(path: Tuple[str, ...]) -> str:
    return ".".join(quote(s) if _needs_quote(s) else s for s in path)


def parse_path(path: str) -> Tuple[str, ...]:
    segs: List[str] = []
    i, n = 0, len(path)
    while True:
        if i < n and path[i] == '"':
            j, buf = i + 1, []
            while j < n and path[j] != '"':
                if path[j] == "\\" and j + 1 < n:
                    buf.append(path[j + 1])
                    j += 2
                else:
                    buf.append(path[j])
                    j += 1
            if j >= n:
                raise DeclError(f"unterminated quote in path {path!r}")
            segs.append("".join(buf))
            i = j + 1
            if i < n and path[i] != ".":
                raise DeclError(f"bad path {path!r}")
        else:
            j = path.find(".", i)
            j = n if j < 0 else j
            seg = path[i:j]
            if not seg:
                raise DeclError(f"empty segment in path {path!r}")
            segs.append(seg)
            i = j
        if i >= n:
            break
        i += 1  # skip '.'
        if i >= n:
            raise DeclError(f"empty segment in path {path!r}")
    return tuple(segs)


# -------------------------------------------------------------------- parser

class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.toks = tokenize(text)
        self.i = 0
        self.nodes: List[Node] = []

    def peek(self, k: int = 0) -> Token:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self) -> Token:
        t = self.toks[self.i]
        if t.kind != "eof":
            self.i += 1
        return t

    def is_p(self, t: Token, chars: str) -> bool:
        return t.kind == "punct" and t.text in chars

    def run(self) -> List[Node]:
        scope = _Scope()
        first = self.peek()
        if self.is_p(first, "{"):
            self.next()
            self.items(scope, (), top=False)
        self.items(scope, (), top=True)
        return self.nodes

    def add(self, scope: "_Scope", parent, key, kind, start, end) -> Tuple[str, ...]:
        path = parent + (scope.unique(key),)
        self.nodes.append(Node(path, kind, start, end, self.text[start:end]))
        return path

    def items(self, scope: "_Scope", parent: Tuple[str, ...], top: bool) -> None:
        while True:
            t = self.peek()
            if t.kind == "eof":
                if not top:
                    raise DeclError("unexpected end of input: missing '}'")
                return
            if self.is_p(t, ";,=)"):
                self.next()
                continue
            if self.is_p(t, "}"):
                self.next()
                if top:
                    continue
                return
            if self.is_p(t, "{"):
                self.block(scope, parent, scope.positional())
                continue
            if self.is_p(t, "("):
                self.next()
                end = self.tuple_end()
                self.add(scope, parent, scope.positional(), "tuple", t.start, end)
                continue
            self.item(scope, parent, t)
            if self.is_p(self.peek(), ";"):
                self.next()

    def block(self, scope, parent, key) -> None:
        open_tok = self.next()
        idx = len(self.nodes)
        path = self.add(scope, parent, key, "block", open_tok.start, open_tok.end)
        self.items(_Scope(), path, top=False)
        end = self.toks[self.i - 1].end
        node = self.nodes[idx]
        node.end = end
        node.raw = self.text[node.start : end]

    def tuple_end(self) -> int:
        depth = 1
        while True:
            t = self.next()
            if t.kind == "eof":
                raise DeclError("unexpected end of input: missing ')'")
            if self.is_p(t, "("):
                depth += 1
            elif self.is_p(t, ")"):
                depth -= 1
                if depth == 0:
                    return t.end

    def key_text(self, t: Token) -> str:
        return unquote(t.text) if t.kind == "string" else t.text

    def item(self, scope, parent, t: Token) -> None:
        self.next()
        n = self.peek()
        if n.kind == "eof" or self.is_p(n, ",}"):
            self.add(scope, parent, scope.positional(), t.kind, t.start, t.end)
            return
        key = self.key_text(t)
        if self.is_p(n, "="):
            self.next()
            self.value(scope, parent, key)
            return
        if self.is_p(n, "{"):
            self.block(scope, parent, key)
            return
        if self.is_p(n, "("):
            self.next()
            end = self.tuple_end()
            self.add(scope, parent, key, "tuple", n.start, end)
            return
        if n.kind in ("word", "string") and n.line == t.line:
            after = self.peek(1)
            qualifier = n.kind == "string" or not is_numeric(n.text)
            if qualifier and self.is_p(after, "{"):
                self.next()
                self.block(scope, parent, key + ":" + self.key_text(n))
                return
            self.next()
            kind, end, last = n.kind, n.end, n
            while True:
                v = self.peek()
                if (
                    v.kind == "word"
                    and is_numeric(v.text)
                    and v.line == last.line
                    and not self.is_p(self.peek(1), "=")
                ):
                    self.next()
                    kind, end, last = "vector", v.end, v
                else:
                    break
            self.add(scope, parent, key, kind, n.start, end)
            return
        self.add(scope, parent, key, "flag", t.start, t.end)

    def value(self, scope, parent, key) -> None:
        v = self.peek()
        if self.is_p(v, "{"):
            self.block(scope, parent, key)
        elif self.is_p(v, "("):
            self.next()
            end = self.tuple_end()
            self.add(scope, parent, key, "tuple", v.start, end)
        elif v.kind in ("word", "string"):
            self.next()
            self.add(scope, parent, key, v.kind, v.start, v.end)
        else:
            raise DeclError(f"missing value for {key!r} at offset {v.start}")


class _Scope:
    def __init__(self) -> None:
        self.seen: dict = {}
        self.pos = 0

    def positional(self) -> str:
        k = f"[{self.pos}]"
        self.pos += 1
        return k

    def unique(self, key: str) -> str:
        c = self.seen.get(key, 0)
        self.seen[key] = c + 1
        return key if c == 0 else f"{key}#{c}"


# ---------------------------------------------------------------- formatting

def shortest_number(v: float) -> str:
    """Shortest round-trip decimal without exponent; integers without ``.0``."""
    if v == 0:
        return "0"
    s = format(Decimal(repr(float(v))), "f")
    if s.endswith(".0"):
        s = s[:-2]
    return s


def format_number(value: float, raw: Optional[str]) -> str:
    if not math.isfinite(value):
        raise DeclError(f"cannot write non-finite number {value!r}")
    s = shortest_number(value)
    if not raw or not is_numeric(raw):
        return s
    suffix = "f" if raw.endswith("f") else ""
    mant = re.split(r"[eE]", raw.rstrip("f"))[0]
    if "." in mant:
        digits = len(mant.split(".", 1)[1])
        if "." not in s:
            s += "."
        frac = len(s.split(".", 1)[1])
        if frac < digits:
            s += "0" * (digits - frac)
    return s + suffix


def format_value(node: Node, value: Value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return format_number(float(value), node.raw if node.kind == "word" else None)
    if isinstance(value, str):
        if node.kind == "string":
            return quote(value)
        return value
    raise DeclError(f"unsupported value type {type(value).__name__}")


# ---------------------------------------------------------------------- Decl

class Decl:
    """A parsed Decl with span-preserving edits."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._nodes = _Parser(text).run()
        self._index = {n.path: n for n in self._nodes}

    @classmethod
    def parse(cls, text: str) -> "Decl":
        return cls(text)

    @property
    def text(self) -> str:
        return self._text

    def render(self) -> str:
        return self._text

    def nodes(self) -> List[Node]:
        return list(self._nodes)

    def paths(self) -> List[Tuple[str, str, str]]:
        return [(n.path_str, n.kind, n.raw) for n in self._nodes if n.kind != "block"]

    def __iter__(self) -> Iterator[Tuple[str, str, str]]:
        return iter(self.paths())

    def node(self, path: str) -> Node:
        key = parse_path(path)
        try:
            return self._index[key]
        except KeyError:
            raise DeclError(f"unknown path {path!r}") from None

    def has(self, path: str) -> bool:
        try:
            return parse_path(path) in self._index
        except DeclError:
            return False

    def raw(self, path: str) -> str:
        return self.node(path).raw

    def get(self, path: str) -> str:
        n = self.node(path)
        return unquote(n.raw) if n.kind == "string" else n.raw

    def set_raw(self, path: str, text: str) -> "Decl":
        n = self.node(path)
        self._reparse(self._text[: n.start] + text + self._text[n.end :])
        return self

    def set(self, path: str, value: Value) -> "Decl":
        n = self.node(path)
        if n.kind == "flag":
            raise DeclError(f"{path!r} is a flag without value; use set_raw")
        return self.set_raw(path, format_value(n, value))

    def _reparse(self, text: str) -> None:
        self._text = text
        self._nodes = _Parser(text).run()
        self._index = {n.path: n for n in self._nodes}


def parse(text: str) -> Decl:
    return Decl(text)
