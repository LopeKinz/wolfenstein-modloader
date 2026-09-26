package wolfsdk

import (
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
)

// Node kinds (SPEC §6.2).
const (
	KindString = "string"
	KindWord   = "word"
	KindVector = "vector"
	KindBlock  = "block"
	KindTuple  = "tuple"
	KindFlag   = "flag"
)

const (
	declPunct = "{}()=;,"
	declWS    = " \t\r\n\f\v"
)

var numericRe = regexp.MustCompile(`^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?f?$`)

// IsNumeric reports whether a Decl word is numeric (SPEC §6.1).
func IsNumeric(text string) bool { return numericRe.MatchString(text) }

// Node is one value of a Decl with its source span (byte offsets).
type Node struct {
	Path  []string
	Kind  string
	Start int
	End   int
	Raw   string
}

// PathString returns the dotted string form of the node's path.
func (n Node) PathString() string { return FormatPath(n.Path) }

// PathInfo is one entry of Decl.Paths.
type PathInfo struct {
	Path string
	Kind string
	Raw  string
}

type tokKind int

const (
	tWord tokKind = iota
	tString
	tPunct
	tEOF
)

type token struct {
	kind       tokKind
	text       string
	start, end int
	line       int
}

type declPanic struct{ err error }

func declFail(format string, args ...any) {
	panic(declPanic{fmt.Errorf("%w: "+format, append([]any{ErrDecl}, args...)...)})
}

func isWS(c byte) bool    { return strings.IndexByte(declWS, c) >= 0 }
func isPunct(c byte) bool { return strings.IndexByte(declPunct, c) >= 0 }

func tokenize(text string) []token {
	var toks []token
	i, n, line := 0, len(text), 0
	for i < n {
		c := text[i]
		switch {
		case isWS(c):
			if c == '\n' {
				line++
			}
			i++
		case c == '/' && i+1 < n && text[i+1] == '/':
			j := strings.IndexByte(text[i:], '\n')
			if j < 0 {
				i = n
			} else {
				i += j
			}
		case c == '/' && i+1 < n && text[i+1] == '*':
			end := n
			if j := strings.Index(text[i+2:], "*/"); j >= 0 {
				end = i + 2 + j + 2
			}
			line += strings.Count(text[i:end], "\n")
			i = end
		case c == '"':
			j := i + 1
			for j < n && text[j] != '"' {
				if text[j] == '\\' {
					j += 2
				} else {
					j++
				}
			}
			if j >= n {
				declFail("unterminated string at offset %d", i)
			}
			toks = append(toks, token{tString, text[i : j+1], i, j + 1, line})
			line += strings.Count(text[i:j+1], "\n")
			i = j + 1
		case isPunct(c):
			toks = append(toks, token{tPunct, text[i : i+1], i, i + 1, line})
			i++
		default:
			j := i
			for j < n {
				d := text[j]
				if isWS(d) || isPunct(d) || d == '"' {
					break
				}
				if d == '/' && j+1 < n && (text[j+1] == '/' || text[j+1] == '*') {
					break
				}
				j++
			}
			toks = append(toks, token{tWord, text[i:j], i, j, line})
			i = j
		}
	}
	return append(toks, token{tEOF, "", n, n, line})
}

func unquote(raw string) string {
	body := raw[1 : len(raw)-1]
	var sb strings.Builder
	for i := 0; i < len(body); i++ {
		if body[i] == '\\' && i+1 < len(body) {
			i++
		}
		sb.WriteByte(body[i])
	}
	return sb.String()
}

func quote(value string) string {
	value = strings.ReplaceAll(value, `\`, `\\`)
	return `"` + strings.ReplaceAll(value, `"`, `\"`) + `"`
}

// ---------------------------------------------------------------- paths

func needsQuote(seg string) bool {
	return seg == "" || strings.ContainsAny(seg, ".\"\\"+declWS)
}

// FormatPath joins path segments with "."; segments containing ".", '"',
// '\' or whitespace are quoted (SPEC §6.3).
func FormatPath(path []string) string {
	parts := make([]string, len(path))
	for i, s := range path {
		if needsQuote(s) {
			parts[i] = quote(s)
		} else {
			parts[i] = s
		}
	}
	return strings.Join(parts, ".")
}

// ParsePath splits a dotted path into segments; the inverse of FormatPath.
func ParsePath(path string) ([]string, error) {
	var segs []string
	i, n := 0, len(path)
	for {
		if i < n && path[i] == '"' {
			j := i + 1
			var sb strings.Builder
			for j < n && path[j] != '"' {
				if path[j] == '\\' && j+1 < n {
					sb.WriteByte(path[j+1])
					j += 2
				} else {
					sb.WriteByte(path[j])
					j++
				}
			}
			if j >= n {
				return nil, fmt.Errorf("%w: unterminated quote in path %q", ErrDecl, path)
			}
			segs = append(segs, sb.String())
			i = j + 1
			if i < n && path[i] != '.' {
				return nil, fmt.Errorf("%w: bad path %q", ErrDecl, path)
			}
		} else {
			j := strings.IndexByte(path[i:], '.')
			if j < 0 {
				j = n
			} else {
				j += i
			}
			seg := path[i:j]
			if seg == "" {
				return nil, fmt.Errorf("%w: empty segment in path %q", ErrDecl, path)
			}
			segs = append(segs, seg)
			i = j
		}
		if i >= n {
			break
		}
		i++ // skip '.'
		if i >= n {
			return nil, fmt.Errorf("%w: empty segment in path %q", ErrDecl, path)
		}
	}
	return segs, nil
}

// --------------------------------------------------------------- parser

type scope struct {
	seen map[string]int
	pos  int
}

func newScope() *scope { return &scope{seen: map[string]int{}} }

func (s *scope) positional() string {
	k := "[" + strconv.Itoa(s.pos) + "]"
	s.pos++
	return k
}

func (s *scope) unique(key string) string {
	c := s.seen[key]
	s.seen[key] = c + 1
	if c == 0 {
		return key
	}
	return key + "#" + strconv.Itoa(c)
}

type parser struct {
	text  string
	toks  []token
	i     int
	nodes []Node
}

func (p *parser) peek(k int) token {
	j := p.i + k
	if j > len(p.toks)-1 {
		j = len(p.toks) - 1
	}
	return p.toks[j]
}

func (p *parser) next() token {
	t := p.toks[p.i]
	if t.kind != tEOF {
		p.i++
	}
	return t
}

func isP(t token, chars string) bool {
	return t.kind == tPunct && strings.Contains(chars, t.text)
}

func (p *parser) run() {
	sc := newScope()
	if isP(p.peek(0), "{") {
		p.next()
		p.items(sc, nil, false)
	}
	p.items(sc, nil, true)
}

func (p *parser) add(sc *scope, parent []string, key, kind string, start, end int) []string {
	path := append(append([]string{}, parent...), sc.unique(key))
	p.nodes = append(p.nodes, Node{path, kind, start, end, p.text[start:end]})
	return path
}

func (p *parser) items(sc *scope, parent []string, top bool) {
	for {
		t := p.peek(0)
		if t.kind == tEOF {
			if !top {
				declFail("unexpected end of input: missing '}'")
			}
			return
		}
		switch {
		case isP(t, ";,=)"):
			p.next()
			continue
		case isP(t, "}"):
			p.next()
			if top {
				continue
			}
			return
		case isP(t, "{"):
			p.block(sc, parent, sc.positional())
			continue
		case isP(t, "("):
			p.next()
			end := p.tupleEnd()
			p.add(sc, parent, sc.positional(), KindTuple, t.start, end)
			continue
		}
		p.item(sc, parent, t)
		if isP(p.peek(0), ";") {
			p.next()
		}
	}
}

func (p *parser) block(sc *scope, parent []string, key string) {
	open := p.next()
	idx := len(p.nodes)
	path := p.add(sc, parent, key, KindBlock, open.start, open.end)
	p.items(newScope(), path, false)
	end := p.toks[p.i-1].end
	p.nodes[idx].End = end
	p.nodes[idx].Raw = p.text[p.nodes[idx].Start:end]
}

func (p *parser) tupleEnd() int {
	depth := 1
	for {
		t := p.next()
		if t.kind == tEOF {
			declFail("unexpected end of input: missing ')'")
		}
		if isP(t, "(") {
			depth++
		} else if isP(t, ")") {
			depth--
			if depth == 0 {
				return t.end
			}
		}
	}
}

func keyText(t token) string {
	if t.kind == tString {
		return unquote(t.text)
	}
	return t.text
}

func kindOf(t token) string {
	if t.kind == tString {
		return KindString
	}
	return KindWord
}

func (p *parser) item(sc *scope, parent []string, t token) {
	p.next()
	n := p.peek(0)
	if n.kind == tEOF || isP(n, ",}") {
		p.add(sc, parent, sc.positional(), kindOf(t), t.start, t.end)
		return
	}
	key := keyText(t)
	if isP(n, "=") {
		p.next()
		p.value(sc, parent, key)
		return
	}
	if isP(n, "{") {
		p.block(sc, parent, key)
		return
	}
	if isP(n, "(") {
		p.next()
		end := p.tupleEnd()
		p.add(sc, parent, key, KindTuple, n.start, end)
		return
	}
	if (n.kind == tWord || n.kind == tString) && n.line == t.line {
		after := p.peek(1)
		qualifier := n.kind == tString || !IsNumeric(n.text)
		if qualifier && isP(after, "{") {
			p.next()
			p.block(sc, parent, key+":"+keyText(n))
			return
		}
		p.next()
		kind, end, last := kindOf(n), n.end, n
		for {
			v := p.peek(0)
			if v.kind == tWord && IsNumeric(v.text) && v.line == last.line && !isP(p.peek(1), "=") {
				p.next()
				kind, end, last = KindVector, v.end, v
			} else {
				break
			}
		}
		p.add(sc, parent, key, kind, n.start, end)
		return
	}
	p.add(sc, parent, key, KindFlag, t.start, t.end)
}

func (p *parser) value(sc *scope, parent []string, key string) {
	v := p.peek(0)
	switch {
	case isP(v, "{"):
		p.block(sc, parent, key)
	case isP(v, "("):
		p.next()
		end := p.tupleEnd()
		p.add(sc, parent, key, KindTuple, v.start, end)
	case v.kind == tWord || v.kind == tString:
		p.next()
		p.add(sc, parent, key, kindOf(v), v.start, v.end)
	default:
		declFail("missing value for %q at offset %d", key, v.start)
	}
}

func parseNodes(text string) (nodes []Node, err error) {
	defer func() {
		if r := recover(); r != nil {
			dp, ok := r.(declPanic)
			if !ok {
				panic(r)
			}
			nodes, err = nil, dp.err
		}
	}()
	p := &parser{text: text, toks: tokenize(text)}
	p.run()
	return p.nodes, nil
}

// ----------------------------------------------------------- formatting

func shortestNumber(v float64) string {
	if v == 0 {
		return "0"
	}
	return strconv.FormatFloat(v, 'f', -1, 64)
}

func formatNumber(value float64, raw string, useRaw bool) (string, error) {
	if math.IsNaN(value) || math.IsInf(value, 0) {
		return "", fmt.Errorf("%w: cannot write non-finite number %v", ErrDecl, value)
	}
	s := shortestNumber(value)
	if !useRaw || raw == "" || !IsNumeric(raw) {
		return s, nil
	}
	suffix := ""
	if strings.HasSuffix(raw, "f") {
		suffix = "f"
	}
	mant := strings.TrimRight(raw, "f")
	if j := strings.IndexAny(mant, "eE"); j >= 0 {
		mant = mant[:j]
	}
	if dot := strings.IndexByte(mant, '.'); dot >= 0 {
		digits := len(mant) - dot - 1
		if !strings.Contains(s, ".") {
			s += "."
		}
		frac := len(s) - strings.IndexByte(s, '.') - 1
		if frac < digits {
			s += strings.Repeat("0", digits-frac)
		}
	}
	return s + suffix, nil
}

func toFloat(value any) (float64, bool) {
	switch v := value.(type) {
	case float64:
		return v, true
	case float32:
		return float64(v), true
	case int:
		return float64(v), true
	case int8:
		return float64(v), true
	case int16:
		return float64(v), true
	case int32:
		return float64(v), true
	case int64:
		return float64(v), true
	case uint:
		return float64(v), true
	case uint8:
		return float64(v), true
	case uint16:
		return float64(v), true
	case uint32:
		return float64(v), true
	case uint64:
		return float64(v), true
	case json.Number:
		f, err := strconv.ParseFloat(string(v), 64)
		return f, err == nil
	}
	return 0, false
}

// FormatValue renders value (string, bool or any Go number) for insertion
// into node's span (SPEC §6.5).
func FormatValue(node Node, value any) (string, error) {
	switch v := value.(type) {
	case bool:
		if v {
			return "true", nil
		}
		return "false", nil
	case string:
		if node.Kind == KindString {
			return quote(v), nil
		}
		return v, nil
	}
	if f, ok := toFloat(value); ok {
		return formatNumber(f, node.Raw, node.Kind == KindWord)
	}
	return "", fmt.Errorf("%w: unsupported value type %T", ErrDecl, value)
}

// ----------------------------------------------------------------- Decl

// Decl is a parsed Decl with span-preserving edits (SPEC §6).
type Decl struct {
	text  string
	nodes []Node
	index map[string]int
}

// ParseDecl parses a Decl. Rendering without edits returns text unchanged.
func ParseDecl(text string) (*Decl, error) {
	d := &Decl{}
	if err := d.reparse(text); err != nil {
		return nil, err
	}
	return d, nil
}

func (d *Decl) reparse(text string) error {
	nodes, err := parseNodes(text)
	if err != nil {
		return err
	}
	d.text, d.nodes = text, nodes
	d.index = make(map[string]int, len(nodes))
	for i, n := range nodes {
		d.index[FormatPath(n.Path)] = i
	}
	return nil
}

// Text returns the current source.
func (d *Decl) Text() string { return d.text }

// Nodes returns every node including blocks, in source order.
func (d *Decl) Nodes() []Node { return append([]Node{}, d.nodes...) }

// Paths returns (path, kind, raw) of every non-block node in source order.
func (d *Decl) Paths() []PathInfo {
	var out []PathInfo
	for _, n := range d.nodes {
		if n.Kind != KindBlock {
			out = append(out, PathInfo{n.PathString(), n.Kind, n.Raw})
		}
	}
	return out
}

// Node returns the node at path.
func (d *Decl) Node(path string) (Node, error) {
	segs, err := ParsePath(path)
	if err != nil {
		return Node{}, err
	}
	i, ok := d.index[FormatPath(segs)]
	if !ok {
		return Node{}, fmt.Errorf("%w: unknown path %q", ErrDecl, path)
	}
	return d.nodes[i], nil
}

// Has reports whether path exists (false for malformed paths).
func (d *Decl) Has(path string) bool {
	_, err := d.Node(path)
	return err == nil
}

// Raw returns the exact source text of the value at path.
func (d *Decl) Raw(path string) (string, error) {
	n, err := d.Node(path)
	return n.Raw, err
}

// Get returns the raw text of the value at path; strings are unquoted.
func (d *Decl) Get(path string) (string, error) {
	n, err := d.Node(path)
	if err != nil {
		return "", err
	}
	if n.Kind == KindString {
		return unquote(n.Raw), nil
	}
	return n.Raw, nil
}

// SetRaw replaces the span of path with text verbatim and re-parses.
func (d *Decl) SetRaw(path, text string) error {
	n, err := d.Node(path)
	if err != nil {
		return err
	}
	return d.reparse(d.text[:n.Start] + text + d.text[n.End:])
}

// Set formats value (string, bool or number) for the node at path and
// splices it into the source (SPEC §6.4). Flags cannot be set; use SetRaw.
func (d *Decl) Set(path string, value any) error {
	n, err := d.Node(path)
	if err != nil {
		return err
	}
	if n.Kind == KindFlag {
		return fmt.Errorf("%w: %q is a flag without value; use SetRaw", ErrDecl, path)
	}
	s, err := FormatValue(n, value)
	if err != nil {
		return err
	}
	return d.SetRaw(path, s)
}
