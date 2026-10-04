"""Parser and surgical editor for id Tech 5 decl text.

The game ships two dialects in the same container and both appear inside a
single file tree:

    weapon / entityDef / damage      key = value;   and   key = { ... }
    material / md6Def / skins        key value      and   key { ... }

plus qualified blocks such as `prop "_info" { ... }` and indexed members such
as `item[0] = { ... }`.

Rather than parse into a tree and write it back out -- which would silently
reformat the file and drop anything the grammar failed to model -- we record
the byte span of every value and edit by slicing. An edited decl differs from
the original only inside the spans that were actually changed, so comments,
whitespace, ordering and unknown syntax all survive untouched.

Paths are dotted:

    edit.damageParms.minDamage
    edit.validAmmoClips.item[0].clipSize
    props.prop:_info.tag:muzzle.trans

Repeated keys at the same level get a `#N` suffix after the first.
"""

import re

_TOKEN = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<line_comment>//[^\n]*)
    | (?P<block_comment>/\*.*?\*/)
    | (?P<string>"(?:[^"\\]|\\.)*")
    | (?P<punct>[{}()=;,])
    | (?P<word>[^\s{}()=;,"]+)
    """,
    re.VERBOSE | re.DOTALL,
)

SCALAR = "scalar"
BLOCK = "block"
TUPLE = "tuple"


class DeclError(Exception):
    pass


class Token:
    __slots__ = ("kind", "text", "start", "end")

    def __init__(self, kind, text, start, end):
        self.kind = kind
        self.text = text
        self.start = start
        self.end = end

    def __repr__(self):
        return "<%s %r>" % (self.kind, self.text)


def tokenize(text):
    """Split decl text into significant tokens, dropping whitespace/comments."""
    out = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m:
            raise DeclError("unparsable character at offset %d: %r" % (pos, text[pos]))
        pos = m.end()
        kind = m.lastgroup
        if kind in ("ws", "line_comment", "block_comment"):
            continue
        out.append(Token(kind, m.group(), m.start(), m.end()))
    return out


class Value:
    """A parsed value and the exact span it occupies in the source text."""

    __slots__ = ("kind", "start", "end", "text")

    def __init__(self, kind, start, end, text):
        self.kind = kind
        self.start = start
        self.end = end
        self.text = text

    def __repr__(self):
        return "<Value %s %r>" % (self.kind, self.text[:40])


def unquote(s):
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return s


class Decl:
    """A decl document addressable by dotted path."""

    def __init__(self, text):
        self.text = text
        self.values = {}  # path -> Value
        self._parse()

    # -- parsing -----------------------------------------------------------

    def _parse(self):
        tokens = tokenize(self.text)
        if not tokens:
            return
        start = 1 if tokens[0].text == "{" else 0
        end = len(tokens) - 1 if start == 1 and tokens[-1].text == "}" else len(tokens)
        self._parse_items(tokens, start, end, prefix="")

    def _parse_items(self, tokens, i, end, prefix):
        seen = {}
        while i < end:
            tok = tokens[i]
            if tok.text in (";", ","):
                i += 1
                continue
            if tok.text == "}":
                break
            if tok.kind not in ("word", "string"):
                i += 1
                continue

            key = unquote(tok.text)
            i += 1
            # Indexed members appear both as `item[0]` and as `item[ 0 ]`.
            # Glue the pieces back together so one path form addresses both.
            if key.endswith("["):
                while i < end and not key.endswith("]"):
                    key += unquote(tokens[i].text)
                    i += 1
            if i < end and tokens[i].text == "=":
                i += 1
            if i >= end:
                break

            # `prop "_info" { ... }` -- a string between key and block is part
            # of the key, not a value of its own.
            if (
                tokens[i].kind in ("word", "string")
                and i + 1 < end
                and tokens[i + 1].text == "{"
            ):
                key = "%s:%s" % (key, unquote(tokens[i].text))
                i += 1

            count = seen.get(key, 0)
            seen[key] = count + 1
            path = key if count == 0 else "%s#%d" % (key, count)
            full = "%s.%s" % (prefix, path) if prefix else path

            i = self._parse_value(tokens, i, end, full)
        return i

    def _parse_value(self, tokens, i, end, full):
        tok = tokens[i]
        if tok.text == "{":
            depth, j = 1, i + 1
            while j < end and depth:
                if tokens[j].text == "{":
                    depth += 1
                elif tokens[j].text == "}":
                    depth -= 1
                j += 1
            inner_end = j - 1
            self.values[full] = Value(
                BLOCK, tok.start, tokens[j - 1].end, self.text[tok.start : tokens[j - 1].end]
            )
            self._parse_items(tokens, i + 1, inner_end, full)
            return j
        if tok.text == "(":
            j = i + 1
            while j < end and tokens[j].text != ")":
                j += 1
            j = min(j, end - 1)
            self.values[full] = Value(
                TUPLE, tok.start, tokens[j].end, self.text[tok.start : tokens[j].end]
            )
            return j + 1
        # Scalar: a single string or word.
        self.values[full] = Value(SCALAR, tok.start, tok.end, tok.text)
        return i + 1

    # -- reading -----------------------------------------------------------

    def get(self, path, default=None):
        v = self.values.get(path)
        return default if v is None else unquote(v.text)

    def paths(self, kind=None):
        return [p for p, v in self.values.items() if kind is None or v.kind == kind]

    def __contains__(self, path):
        return path in self.values

    # -- editing -----------------------------------------------------------

    def set(self, path, value):
        """Queue a replacement. Returns self so calls can chain."""
        if path not in self.values:
            raise DeclError("no such path: %s" % path)
        self._edits = getattr(self, "_edits", [])
        self._edits.append((path, _render(value, self.values[path])))
        return self

    def render(self):
        """Apply queued edits and return the new text."""
        edits = getattr(self, "_edits", [])
        if not edits:
            return self.text
        spans = []
        for path, new_text in edits:
            v = self.values[path]
            spans.append((v.start, v.end, new_text))
        spans.sort(key=lambda s: s[0])
        for a, b in zip(spans, spans[1:]):
            if a[1] > b[0]:
                raise DeclError("overlapping edits at offset %d" % b[0])
        out = []
        pos = 0
        for start, end, new_text in spans:
            out.append(self.text[pos:start])
            out.append(new_text)
            pos = end
        out.append(self.text[pos:])
        return "".join(out)


def _render(value, old):
    """Format a Python value the way the surrounding decl writes that type."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if old.kind == SCALAR and old.text.startswith('"'):
        return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')
    return text


def apply_patch(text, patch):
    """Apply {dotted_path: value} to decl text, returning the new text."""
    decl = Decl(text)
    missing = [p for p in patch if p not in decl]
    if missing:
        raise DeclError("unknown path(s): %s" % ", ".join(sorted(missing)))
    for path, value in patch.items():
        decl.set(path, value)
    return decl.render()
