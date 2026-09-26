package wolfsdk

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"unicode/utf8"
)

var resourcesHeader = append(append([]byte{}, Magic...), make([]byte, 12)...)

// LooksTextual reports whether data is valid UTF-8 without NUL bytes; it
// decides PadAuto in WriteInPlace.
func LooksTextual(data []byte) bool {
	return bytes.IndexByte(data, 0) < 0 && utf8.Valid(data)
}

// Chunk is one index/resources pair of a game.
type Chunk struct {
	IndexName     string
	ResourcesName string
	IndexPath     string
	ResourcesPath string
	Index         *ChunkIndex
}

// Entries returns the entries of the chunk index.
func (c *Chunk) Entries() []*Entry { return c.Index.Entries }

// SaveIndex writes the (patched) index buffer back to IndexPath.
func (c *Chunk) SaveIndex() error {
	return os.WriteFile(c.IndexPath, c.Index.Bytes(), 0o644)
}

// Occurrence is one location of an asset: a chunk and an entry in it.
type Occurrence struct {
	Chunk *Chunk
	Entry *Entry
}

// Game is a game installation (the directory that contains base/).
type Game struct {
	Root   string
	Base   string
	Pairs  []Pair
	Chunks []*Chunk
}

// OpenGame opens the game directory root (SPEC §5). Pairs whose index or
// resources file is missing are skipped.
func OpenGame(root string) (*Game, error) {
	g := &Game{Root: root, Base: filepath.Join(root, "base")}
	data, err := os.ReadFile(filepath.Join(g.Base, "master.index"))
	if err != nil {
		return nil, err
	}
	if g.Pairs, err = ParseMasterIndex(data); err != nil {
		return nil, err
	}
	for _, p := range g.Pairs {
		ip := filepath.Join(g.Base, p.Index)
		rp := filepath.Join(g.Base, p.Resources)
		if _, err := os.Stat(ip); err != nil {
			continue
		}
		st, err := os.Stat(rp)
		if err != nil {
			continue
		}
		idx, err := os.ReadFile(ip)
		if err != nil {
			return nil, err
		}
		ci, err := ParseChunkIndex(idx, st.Size())
		if err != nil {
			return nil, fmt.Errorf("%s: %w", p.Index, err)
		}
		g.Chunks = append(g.Chunks, &Chunk{p.Index, p.Resources, ip, rp, ci})
	}
	return g, nil
}

// Occurrences returns every (chunk, entry) pair of the game in order.
func (g *Game) Occurrences() []Occurrence {
	var out []Occurrence
	for _, c := range g.Chunks {
		for _, e := range c.Entries() {
			out = append(out, Occurrence{c, e})
		}
	}
	return out
}

// Keys returns the distinct asset keys in order of first appearance.
func (g *Game) Keys() []string {
	seen := map[string]bool{}
	var out []string
	for _, o := range g.Occurrences() {
		if k := o.Entry.Key(); !seen[k] {
			seen[k] = true
			out = append(out, k)
		}
	}
	return out
}

// Find returns every occurrence of type:name, in chunk order.
func (g *Game) Find(typ, name string) []Occurrence {
	var out []Occurrence
	for _, c := range g.Chunks {
		for _, e := range c.Index.Find(typ, name) {
			out = append(out, Occurrence{c, e})
		}
	}
	return out
}

// ReadRaw returns the on-disk slot bytes of occ.
func (g *Game) ReadRaw(occ Occurrence) ([]byte, error) {
	e := occ.Entry
	f, err := os.Open(occ.Chunk.ResourcesPath)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	buf := make([]byte, e.CSize)
	n, err := f.ReadAt(buf, int64(e.Offset))
	if n != len(buf) {
		if err != nil && err != io.EOF {
			return nil, err
		}
		return nil, fmt.Errorf("%w: %s: slot truncated", ErrFormat, e.Key())
	}
	return buf, nil
}

// Read returns the decoded payload of occ (inflated if compressed).
func (g *Game) Read(occ Occurrence) ([]byte, error) {
	raw, err := g.ReadRaw(occ)
	if err != nil {
		return nil, err
	}
	if !occ.Entry.Compressed() {
		return raw, nil
	}
	return Inflate(raw, int(occ.Entry.USize))
}

// ReadAsset reads the first occurrence of type:name; ok is false if the
// asset does not exist.
func (g *Game) ReadAsset(typ, name string) (data []byte, ok bool, err error) {
	occ := g.Find(typ, name)
	if len(occ) == 0 {
		return nil, false, nil
	}
	data, err = g.Read(occ[0])
	return data, err == nil, err
}

// PadMode selects whether WriteInPlace may pad text payloads with a Decl
// comment.
type PadMode int

const (
	// PadAuto pads when the data is valid UTF-8 without NUL bytes.
	PadAuto PadMode = iota
	// PadOn always allows padding.
	PadOn
	// PadOff never pads.
	PadOff
)

// WriteInPlace overwrites the slot of occ without moving anything (SPEC
// §5.1) and returns the previous slot bytes. It fails with ErrNoFit when the
// data does not fit.
func (g *Game) WriteInPlace(occ Occurrence, data []byte, pad PadMode) ([]byte, error) {
	e := occ.Entry
	original, err := g.ReadRaw(occ)
	if err != nil {
		return nil, err
	}
	var stream []byte
	usize := e.USize
	if !e.Compressed() {
		if len(data) != int(e.CSize) {
			return nil, fmt.Errorf("%w: %s: stored entry needs exactly %d bytes, got %d", ErrNoFit, e.Key(), e.CSize, len(data))
		}
		stream = data
	} else {
		p := pad == PadOn || (pad == PadAuto && LooksTextual(data))
		s, payload, ok := DeflateExact(data, int(e.CSize), p)
		if !ok {
			return nil, fmt.Errorf("%w: %s: does not fit into its %d-byte slot", ErrNoFit, e.Key(), e.CSize)
		}
		stream, usize = s, uint32(len(payload))
	}
	f, err := os.OpenFile(occ.Chunk.ResourcesPath, os.O_WRONLY, 0)
	if err != nil {
		return nil, err
	}
	if _, err := f.WriteAt(stream, int64(e.Offset)); err != nil {
		f.Close()
		return nil, err
	}
	if err := f.Close(); err != nil {
		return nil, err
	}
	if usize != e.USize {
		occ.Chunk.Index.SetTriple(e, e.Offset, usize, e.CSize)
		if err := occ.Chunk.SaveIndex(); err != nil {
			return nil, err
		}
	}
	return original, nil
}

// WriteRecord is one slot overwritten by WriteAsset and its previous bytes.
type WriteRecord struct {
	Occurrence Occurrence
	Original   []byte
}

// WriteAsset writes data into every occurrence of type:name (PadAuto). It
// fails with ErrNotFound if the asset does not exist.
func (g *Game) WriteAsset(typ, name string, data []byte) ([]WriteRecord, error) {
	occs := g.Find(typ, name)
	if len(occs) == 0 {
		return nil, fmt.Errorf("%w: %s:%s", ErrNotFound, typ, name)
	}
	var out []WriteRecord
	for _, o := range occs {
		orig, err := g.WriteInPlace(o, data, PadAuto)
		if err != nil {
			return out, err
		}
		out = append(out, WriteRecord{o, orig})
	}
	return out, nil
}

type slotKey struct{ offset, csize uint32 }

// Rebuild rewrites the chunk's .resources in original offset order (SPEC
// §5.2). replacements maps entry indexes to new uncompressed data. dest is
// the output file ("" = replace the chunk's resources file). All index
// triples are updated and the index file is saved.
func (g *Game) Rebuild(chunk *Chunk, replacements map[int][]byte, dest string) error {
	if dest == "" {
		dest = chunk.ResourcesPath
	}
	tmp := dest + ".tmp"
	slots := map[slotKey][]*Entry{}
	var keys []slotKey
	for _, e := range chunk.Entries() {
		k := slotKey{e.Offset, e.CSize}
		if _, ok := slots[k]; !ok {
			keys = append(keys, k)
		}
		slots[k] = append(slots[k], e)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].offset != keys[j].offset {
			return keys[i].offset < keys[j].offset
		}
		return keys[i].csize < keys[j].csize
	})
	src, err := os.Open(chunk.ResourcesPath)
	if err != nil {
		return err
	}
	defer src.Close()
	out, err := os.Create(tmp)
	if err != nil {
		return err
	}
	type update struct {
		e                    *Entry
		offset, usize, csize uint32
	}
	var updates []update
	fail := func(err error) error {
		out.Close()
		os.Remove(tmp)
		return err
	}
	if _, err := out.Write(resourcesHeader); err != nil {
		return fail(err)
	}
	cursor := int64(len(resourcesHeader))
	for _, k := range keys {
		group := slots[k]
		var repl []byte
		found := false
		for _, e := range group {
			if r, ok := replacements[e.Index]; ok {
				repl, found = r, true
				break
			}
		}
		var payload []byte
		var usize uint32
		if !found {
			payload = make([]byte, k.csize)
			if n, err := src.ReadAt(payload, int64(k.offset)); n != len(payload) {
				if err == nil || err == io.EOF {
					err = fmt.Errorf("%w: %s: slot truncated", ErrFormat, group[0].Key())
				}
				return fail(err)
			}
			usize = group[0].USize
		} else {
			s := DeflateSync(repl)
			if len(s) < len(repl) {
				payload = s
			} else {
				payload = repl
			}
			usize = uint32(len(repl))
		}
		for _, e := range group {
			updates = append(updates, update{e, uint32(cursor), usize, uint32(len(payload))})
		}
		if _, err := out.Write(payload); err != nil {
			return fail(err)
		}
		cursor += int64(len(payload))
		pad := (16 - cursor%16) % 16
		if _, err := out.Write(make([]byte, pad)); err != nil {
			return fail(err)
		}
		cursor += pad
	}
	if err := out.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	src.Close()
	if err := os.Rename(tmp, dest); err != nil {
		return err
	}
	for _, u := range updates {
		chunk.Index.SetTriple(u.e, u.offset, u.usize, u.csize)
	}
	chunk.Index.ResourcesSize = cursor
	return chunk.SaveIndex()
}

// ValidateChunk returns the invariant violations of a chunk (empty = valid):
// non-ascending or unaligned offsets, slots past the end of the resources
// file, and compressed payloads that are not sync-flushed.
func ValidateChunk(chunk *Chunk) ([]string, error) {
	problems := []string{}
	f, err := os.Open(chunk.ResourcesPath)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return nil, err
	}
	size := st.Size()
	entries := append([]*Entry{}, chunk.Entries()...)
	sort.SliceStable(entries, func(i, j int) bool {
		if entries[i].Offset != entries[j].Offset {
			return entries[i].Offset < entries[j].Offset
		}
		return entries[i].Index < entries[j].Index
	})
	last := int64(-1)
	seen := map[slotKey]bool{}
	for _, e := range entries {
		k := slotKey{e.Offset, e.CSize}
		if seen[k] {
			continue
		}
		seen[k] = true
		off := int64(e.Offset)
		if off <= last {
			problems = append(problems, fmt.Sprintf("%s: offset %d not ascending", e.Key(), e.Offset))
		}
		last = off
		if off%16 != 0 {
			problems = append(problems, fmt.Sprintf("%s: offset %d not 16-byte aligned", e.Key(), e.Offset))
		}
		if off+int64(e.CSize) > size {
			problems = append(problems, fmt.Sprintf("%s: slot exceeds resources file", e.Key()))
			continue
		}
		if e.Compressed() {
			buf := make([]byte, e.CSize)
			if _, err := f.ReadAt(buf, off); err != nil && err != io.EOF {
				return nil, err
			}
			if !IsSyncFlushed(buf) {
				problems = append(problems, fmt.Sprintf("%s: stream not sync-flushed", e.Key()))
			}
		}
	}
	return problems, nil
}
