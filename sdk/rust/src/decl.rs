//! Decl parser and span-preserving editor (SPEC §6).
//!
//! Every value remembers its source span; editing replaces exactly that span
//! and re-parses. Parsing and rendering without edits returns the input
//! unchanged. Spans are byte offsets into the UTF-8 source; every delimiter is
//! ASCII, so slicing at them is always valid.

use std::collections::HashMap;
use std::fmt;
use std::str::FromStr;

use crate::error::{Error, Result};

/// Kind of a Decl node.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Kind {
    /// A quoted string.
    String,
    /// A single bare word.
    Word,
    /// Several words on one line (`key 1 2 3`).
    Vector,
    /// A `{ … }` block.
    Block,
    /// An opaque `( … )` tuple.
    Tuple,
    /// A key without a value.
    Flag,
}

impl Kind {
    /// The kind name used by the spec and the test vectors.
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::String => "string",
            Kind::Word => "word",
            Kind::Vector => "vector",
            Kind::Block => "block",
            Kind::Tuple => "tuple",
            Kind::Flag => "flag",
        }
    }
}

impl fmt::Display for Kind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for Kind {
    type Err = Error;

    fn from_str(s: &str) -> Result<Kind> {
        Ok(match s {
            "string" => Kind::String,
            "word" => Kind::Word,
            "vector" => Kind::Vector,
            "block" => Kind::Block,
            "tuple" => Kind::Tuple,
            "flag" => Kind::Flag,
            _ => return Err(Error::Decl(format!("unknown node kind {s:?}"))),
        })
    }
}

/// A value that can be written with [`Decl::set`].
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    /// Text; quoted for string nodes, inserted verbatim otherwise.
    Str(String),
    /// A number, written as the shortest round-trip decimal.
    Num(f64),
    /// `true` / `false`.
    Bool(bool),
}

impl From<&str> for Value {
    fn from(v: &str) -> Self {
        Value::Str(v.to_string())
    }
}
impl From<String> for Value {
    fn from(v: String) -> Self {
        Value::Str(v)
    }
}
impl From<f64> for Value {
    fn from(v: f64) -> Self {
        Value::Num(v)
    }
}
impl From<f32> for Value {
    fn from(v: f32) -> Self {
        Value::Num(v as f64)
    }
}
impl From<i32> for Value {
    fn from(v: i32) -> Self {
        Value::Num(v as f64)
    }
}
impl From<i64> for Value {
    fn from(v: i64) -> Self {
        Value::Num(v as f64)
    }
}
impl From<u32> for Value {
    fn from(v: u32) -> Self {
        Value::Num(v as f64)
    }
}
impl From<bool> for Value {
    fn from(v: bool) -> Self {
        Value::Bool(v)
    }
}

/// A node of a parsed Decl with its source span.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Node {
    /// Keys from the root.
    pub path: Vec<String>,
    /// Node kind.
    pub kind: Kind,
    /// Byte offset of the value span.
    pub start: usize,
    /// Byte offset one past the value span.
    pub end: usize,
    /// Exact source text of the value.
    pub raw: String,
}

impl Node {
    /// The path in its string form (see [`format_path`]).
    pub fn path_str(&self) -> String {
        format_path(&self.path)
    }
}

// ------------------------------------------------------------------ tokens

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TokKind {
    Word,
    Str,
    Punct,
    Eof,
}

#[derive(Debug, Clone, Copy)]
struct Token {
    kind: TokKind,
    start: usize,
    end: usize,
    line: usize,
}

fn is_ws(c: u8) -> bool {
    matches!(c, b' ' | b'\t' | b'\r' | b'\n' | 0x0C | 0x0B)
}

fn is_punct(c: u8) -> bool {
    matches!(c, b'{' | b'}' | b'(' | b')' | b'=' | b';' | b',')
}

fn count_nl(b: &[u8]) -> usize {
    b.iter().filter(|&&c| c == b'\n').count()
}

fn find_from(hay: &[u8], from: usize, needle: &[u8]) -> Option<usize> {
    if from > hay.len() {
        return None;
    }
    hay[from..]
        .windows(needle.len())
        .position(|w| w == needle)
        .map(|p| p + from)
}

fn tokenize(text: &str) -> Result<Vec<Token>> {
    let b = text.as_bytes();
    let n = b.len();
    let mut tokens = Vec::new();
    let (mut i, mut line) = (0usize, 0usize);
    while i < n {
        let c = b[i];
        if is_ws(c) {
            if c == b'\n' {
                line += 1;
            }
            i += 1;
            continue;
        }
        if c == b'/' && i + 1 < n && b[i + 1] == b'/' {
            i = find_from(b, i, b"\n").unwrap_or(n);
            continue;
        }
        if c == b'/' && i + 1 < n && b[i + 1] == b'*' {
            let end = find_from(b, i + 2, b"*/").map_or(n, |j| j + 2);
            line += count_nl(&b[i..end]);
            i = end;
            continue;
        }
        if c == b'"' {
            let mut j = i + 1;
            while j < n && b[j] != b'"' {
                j += if b[j] == b'\\' { 2 } else { 1 };
            }
            if j >= n {
                return Err(Error::Decl(format!("unterminated string at offset {i}")));
            }
            tokens.push(Token {
                kind: TokKind::Str,
                start: i,
                end: j + 1,
                line,
            });
            line += count_nl(&b[i..j + 1]);
            i = j + 1;
            continue;
        }
        if is_punct(c) {
            tokens.push(Token {
                kind: TokKind::Punct,
                start: i,
                end: i + 1,
                line,
            });
            i += 1;
            continue;
        }
        let mut j = i;
        while j < n {
            let d = b[j];
            if is_ws(d) || is_punct(d) || d == b'"' {
                break;
            }
            if d == b'/' && j + 1 < n && (b[j + 1] == b'/' || b[j + 1] == b'*') {
                break;
            }
            j += 1;
        }
        tokens.push(Token {
            kind: TokKind::Word,
            start: i,
            end: j,
            line,
        });
        i = j;
    }
    tokens.push(Token {
        kind: TokKind::Eof,
        start: n,
        end: n,
        line,
    });
    Ok(tokens)
}

/// `true` iff `text` matches `^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?f?$`.
pub fn is_numeric(text: &str) -> bool {
    let b = text.as_bytes();
    let mut i = 0;
    let digits = |i: &mut usize| {
        let s = *i;
        while *i < b.len() && b[*i].is_ascii_digit() {
            *i += 1;
        }
        *i - s
    };
    if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
        i += 1;
    }
    if digits(&mut i) > 0 {
        if i < b.len() && b[i] == b'.' {
            i += 1;
            digits(&mut i);
        }
    } else {
        if i >= b.len() || b[i] != b'.' {
            return false;
        }
        i += 1;
        if digits(&mut i) == 0 {
            return false;
        }
    }
    if i < b.len() && (b[i] == b'e' || b[i] == b'E') {
        i += 1;
        if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
            i += 1;
        }
        if digits(&mut i) == 0 {
            return false;
        }
    }
    if i < b.len() && b[i] == b'f' {
        i += 1;
    }
    i == b.len()
}

/// Remove the surrounding quotes of a string token and resolve `\x` escapes.
pub fn unquote(raw: &str) -> String {
    let body: Vec<char> = raw.chars().collect();
    let body = if body.len() >= 2 {
        &body[1..body.len() - 1]
    } else {
        &body[..0]
    };
    let mut out = String::new();
    let mut i = 0;
    while i < body.len() {
        if body[i] == '\\' && i + 1 < body.len() {
            out.push(body[i + 1]);
            i += 2;
        } else {
            out.push(body[i]);
            i += 1;
        }
    }
    out
}

/// Quote `value` as a Decl string (`\` and `"` escaped).
pub fn quote(value: &str) -> String {
    format!("\"{}\"", value.replace('\\', "\\\\").replace('"', "\\\""))
}

// ------------------------------------------------------------------- paths

fn needs_quote(seg: &str) -> bool {
    seg.is_empty()
        || seg
            .bytes()
            .any(|c| c == b'.' || c == b'"' || c == b'\\' || is_ws(c))
}

/// Join keys with `.`, quoting keys that contain `.`, `"`, `\` or whitespace.
pub fn format_path<S: AsRef<str>>(path: &[S]) -> String {
    path.iter()
        .map(|s| {
            let s = s.as_ref();
            if needs_quote(s) {
                quote(s)
            } else {
                s.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join(".")
}

/// Split a path string into keys (inverse of [`format_path`]).
pub fn parse_path(path: &str) -> Result<Vec<String>> {
    let chars: Vec<char> = path.chars().collect();
    let n = chars.len();
    let mut segs = Vec::new();
    let mut i = 0;
    loop {
        if i < n && chars[i] == '"' {
            let mut j = i + 1;
            let mut buf = String::new();
            while j < n && chars[j] != '"' {
                if chars[j] == '\\' && j + 1 < n {
                    buf.push(chars[j + 1]);
                    j += 2;
                } else {
                    buf.push(chars[j]);
                    j += 1;
                }
            }
            if j >= n {
                return Err(Error::Decl(format!("unterminated quote in path {path:?}")));
            }
            segs.push(buf);
            i = j + 1;
            if i < n && chars[i] != '.' {
                return Err(Error::Decl(format!("bad path {path:?}")));
            }
        } else {
            let j = (i..n).find(|&k| chars[k] == '.').unwrap_or(n);
            if j == i {
                return Err(Error::Decl(format!("empty segment in path {path:?}")));
            }
            segs.push(chars[i..j].iter().collect());
            i = j;
        }
        if i >= n {
            break;
        }
        i += 1; // skip '.'
        if i >= n {
            return Err(Error::Decl(format!("empty segment in path {path:?}")));
        }
    }
    Ok(segs)
}

// ------------------------------------------------------------------ parser

#[derive(Default)]
struct Scope {
    seen: HashMap<String, usize>,
    pos: usize,
}

impl Scope {
    fn positional(&mut self) -> String {
        let k = format!("[{}]", self.pos);
        self.pos += 1;
        k
    }

    fn unique(&mut self, key: String) -> String {
        let c = self.seen.entry(key.clone()).or_insert(0);
        let out = if *c == 0 { key } else { format!("{key}#{c}") };
        *c += 1;
        out
    }
}

struct Parser<'a> {
    text: &'a str,
    toks: Vec<Token>,
    i: usize,
    nodes: Vec<Node>,
}

impl<'a> Parser<'a> {
    fn new(text: &'a str) -> Result<Self> {
        Ok(Parser {
            text,
            toks: tokenize(text)?,
            i: 0,
            nodes: Vec::new(),
        })
    }

    fn peek(&self, k: usize) -> Token {
        self.toks[(self.i + k).min(self.toks.len() - 1)]
    }

    fn next(&mut self) -> Token {
        let t = self.toks[self.i];
        if t.kind != TokKind::Eof {
            self.i += 1;
        }
        t
    }

    fn tok_text(&self, t: Token) -> &'a str {
        &self.text[t.start..t.end]
    }

    fn is_p(&self, t: Token, chars: &str) -> bool {
        t.kind == TokKind::Punct && chars.as_bytes().contains(&self.text.as_bytes()[t.start])
    }

    fn run(mut self) -> Result<Vec<Node>> {
        let mut scope = Scope::default();
        let first = self.peek(0);
        if self.is_p(first, "{") {
            self.next();
            self.items(&mut scope, &[], false)?;
        }
        self.items(&mut scope, &[], true)?;
        Ok(self.nodes)
    }

    fn add(
        &mut self,
        scope: &mut Scope,
        parent: &[String],
        key: String,
        kind: Kind,
        start: usize,
        end: usize,
    ) -> Vec<String> {
        let mut path = parent.to_vec();
        path.push(scope.unique(key));
        self.nodes.push(Node {
            path: path.clone(),
            kind,
            start,
            end,
            raw: self.text[start..end].to_string(),
        });
        path
    }

    fn items(&mut self, scope: &mut Scope, parent: &[String], top: bool) -> Result<()> {
        loop {
            let t = self.peek(0);
            if t.kind == TokKind::Eof {
                if !top {
                    return Err(Error::Decl("unexpected end of input: missing '}'".into()));
                }
                return Ok(());
            }
            if self.is_p(t, ";,=)") {
                self.next();
                continue;
            }
            if self.is_p(t, "}") {
                self.next();
                if top {
                    continue;
                }
                return Ok(());
            }
            if self.is_p(t, "{") {
                let key = scope.positional();
                self.block(scope, parent, key)?;
                continue;
            }
            if self.is_p(t, "(") {
                self.next();
                let end = self.tuple_end()?;
                let key = scope.positional();
                self.add(scope, parent, key, Kind::Tuple, t.start, end);
                continue;
            }
            self.item(scope, parent, t)?;
            if self.is_p(self.peek(0), ";") {
                self.next();
            }
        }
    }

    fn block(&mut self, scope: &mut Scope, parent: &[String], key: String) -> Result<()> {
        let open = self.next();
        let idx = self.nodes.len();
        let path = self.add(scope, parent, key, Kind::Block, open.start, open.end);
        self.items(&mut Scope::default(), &path, false)?;
        let end = self.toks[self.i - 1].end;
        let node = &mut self.nodes[idx];
        node.end = end;
        node.raw = self.text[node.start..end].to_string();
        Ok(())
    }

    fn tuple_end(&mut self) -> Result<usize> {
        let mut depth = 1usize;
        loop {
            let t = self.next();
            if t.kind == TokKind::Eof {
                return Err(Error::Decl("unexpected end of input: missing ')'".into()));
            }
            if self.is_p(t, "(") {
                depth += 1;
            } else if self.is_p(t, ")") {
                depth -= 1;
                if depth == 0 {
                    return Ok(t.end);
                }
            }
        }
    }

    fn key_text(&self, t: Token) -> String {
        if t.kind == TokKind::Str {
            unquote(self.tok_text(t))
        } else {
            self.tok_text(t).to_string()
        }
    }

    fn item(&mut self, scope: &mut Scope, parent: &[String], t: Token) -> Result<()> {
        self.next();
        let n = self.peek(0);
        if n.kind == TokKind::Eof || self.is_p(n, ",}") {
            let kind = if t.kind == TokKind::Str {
                Kind::String
            } else {
                Kind::Word
            };
            let key = scope.positional();
            self.add(scope, parent, key, kind, t.start, t.end);
            return Ok(());
        }
        let key = self.key_text(t);
        if self.is_p(n, "=") {
            self.next();
            return self.value(scope, parent, key);
        }
        if self.is_p(n, "{") {
            return self.block(scope, parent, key);
        }
        if self.is_p(n, "(") {
            self.next();
            let end = self.tuple_end()?;
            self.add(scope, parent, key, Kind::Tuple, n.start, end);
            return Ok(());
        }
        if matches!(n.kind, TokKind::Word | TokKind::Str) && n.line == t.line {
            let after = self.peek(1);
            let qualifier = n.kind == TokKind::Str || !is_numeric(self.tok_text(n));
            if qualifier && self.is_p(after, "{") {
                self.next();
                let q = format!("{key}:{}", self.key_text(n));
                return self.block(scope, parent, q);
            }
            self.next();
            let mut kind = if n.kind == TokKind::Str {
                Kind::String
            } else {
                Kind::Word
            };
            let (mut end, mut last) = (n.end, n);
            loop {
                let v = self.peek(0);
                if v.kind == TokKind::Word
                    && is_numeric(self.tok_text(v))
                    && v.line == last.line
                    && !self.is_p(self.peek(1), "=")
                {
                    self.next();
                    kind = Kind::Vector;
                    end = v.end;
                    last = v;
                } else {
                    break;
                }
            }
            self.add(scope, parent, key, kind, n.start, end);
            return Ok(());
        }
        self.add(scope, parent, key, Kind::Flag, t.start, t.end);
        Ok(())
    }

    fn value(&mut self, scope: &mut Scope, parent: &[String], key: String) -> Result<()> {
        let v = self.peek(0);
        if self.is_p(v, "{") {
            self.block(scope, parent, key)
        } else if self.is_p(v, "(") {
            self.next();
            let end = self.tuple_end()?;
            self.add(scope, parent, key, Kind::Tuple, v.start, end);
            Ok(())
        } else if matches!(v.kind, TokKind::Word | TokKind::Str) {
            self.next();
            let kind = if v.kind == TokKind::Str {
                Kind::String
            } else {
                Kind::Word
            };
            self.add(scope, parent, key, kind, v.start, v.end);
            Ok(())
        } else {
            Err(Error::Decl(format!(
                "missing value for {key:?} at offset {}",
                v.start
            )))
        }
    }
}

// -------------------------------------------------------------- formatting

/// Shortest round-trip decimal without exponent; integers without `.0`;
/// `-0` is `0`.
pub fn shortest_number(v: f64) -> String {
    if v == 0.0 {
        return "0".to_string();
    }
    // Rust's Display for f64 is the shortest round-trip representation and
    // never uses an exponent.
    format!("{v}")
}

/// Format a number, keeping the fraction width / `f` suffix of a numeric
/// `raw` (SPEC §6.5).
pub fn format_number(value: f64, raw: Option<&str>) -> Result<String> {
    if !value.is_finite() {
        return Err(Error::Decl(format!(
            "cannot write non-finite number {value}"
        )));
    }
    let mut s = shortest_number(value);
    let raw = match raw {
        Some(r) if !r.is_empty() && is_numeric(r) => r,
        _ => return Ok(s),
    };
    let suffix = if raw.ends_with('f') { "f" } else { "" };
    let stripped = raw.trim_end_matches('f');
    let mant = stripped.split(['e', 'E']).next().unwrap_or("");
    if let Some((_, frac)) = mant.split_once('.') {
        let digits = frac.len();
        if !s.contains('.') {
            s.push('.');
        }
        let have = s.split_once('.').map_or(0, |(_, f)| f.len());
        for _ in have..digits {
            s.push('0');
        }
    }
    s.push_str(suffix);
    Ok(s)
}

/// The text [`Decl::set`] splices into `node`'s span for `value` (SPEC §6.5).
pub fn format_value(node: &Node, value: &Value) -> Result<String> {
    match value {
        Value::Bool(b) => Ok(if *b { "true" } else { "false" }.to_string()),
        Value::Num(v) => format_number(
            *v,
            if node.kind == Kind::Word {
                Some(node.raw.as_str())
            } else {
                None
            },
        ),
        Value::Str(s) => Ok(if node.kind == Kind::String {
            quote(s)
        } else {
            s.clone()
        }),
    }
}

// -------------------------------------------------------------------- Decl

/// A parsed Decl with span-preserving edits.
#[derive(Debug, Clone)]
pub struct Decl {
    text: String,
    nodes: Vec<Node>,
    index: HashMap<Vec<String>, usize>,
}

impl Decl {
    /// Parse a Decl.
    pub fn parse(text: &str) -> Result<Decl> {
        let nodes = Parser::new(text)?.run()?;
        let mut index = HashMap::new();
        for (i, n) in nodes.iter().enumerate() {
            index.insert(n.path.clone(), i);
        }
        Ok(Decl {
            text: text.to_string(),
            nodes,
            index,
        })
    }

    /// The current source text.
    pub fn text(&self) -> &str {
        &self.text
    }

    /// Every node including blocks, in source order.
    pub fn nodes(&self) -> &[Node] {
        &self.nodes
    }

    /// `(path, kind, raw)` for every non-block node, in source order.
    pub fn paths(&self) -> Vec<(String, Kind, String)> {
        self.nodes
            .iter()
            .filter(|n| n.kind != Kind::Block)
            .map(|n| (n.path_str(), n.kind, n.raw.clone()))
            .collect()
    }

    /// The node at `path`.
    pub fn node(&self, path: &str) -> Result<&Node> {
        let key = parse_path(path)?;
        self.index
            .get(&key)
            .map(|&i| &self.nodes[i])
            .ok_or_else(|| Error::Decl(format!("unknown path {path:?}")))
    }

    /// `true` if `path` exists (malformed paths are simply absent).
    pub fn has(&self, path: &str) -> bool {
        parse_path(path).is_ok_and(|k| self.index.contains_key(&k))
    }

    /// Exact source text of the value at `path`.
    pub fn raw(&self, path: &str) -> Result<&str> {
        Ok(&self.node(path)?.raw)
    }

    /// The value at `path`; strings are unquoted and unescaped.
    pub fn get(&self, path: &str) -> Result<String> {
        let n = self.node(path)?;
        Ok(if n.kind == Kind::String {
            unquote(&n.raw)
        } else {
            n.raw.clone()
        })
    }

    /// Replace the span at `path` with `text` verbatim, then re-parse.
    ///
    /// On error the Decl is left unchanged.
    pub fn set_raw(&mut self, path: &str, text: &str) -> Result<&mut Self> {
        let n = self.node(path)?;
        let new_text = format!("{}{}{}", &self.text[..n.start], text, &self.text[n.end..]);
        *self = Decl::parse(&new_text)?;
        Ok(self)
    }

    /// Write `value` at `path` (SPEC §6.4/§6.5).
    pub fn set<V: Into<Value>>(&mut self, path: &str, value: V) -> Result<&mut Self> {
        let value = value.into();
        let n = self.node(path)?;
        if n.kind == Kind::Flag {
            return Err(Error::Decl(format!(
                "{path:?} is a flag without value; use set_raw"
            )));
        }
        let text = format_value(n, &value)?;
        self.set_raw(path, &text)
    }
}

impl fmt::Display for Decl {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.text)
    }
}

impl FromStr for Decl {
    type Err = Error;

    fn from_str(s: &str) -> Result<Decl> {
        Decl::parse(s)
    }
}
