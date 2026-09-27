package wolfsdk

import (
	"bytes"
	"encoding/binary"
	"fmt"
)

const (
	entriesStart = 0x2C
	maxString    = 1024
)

// Entry is one resource entry of a chunk index (SPEC §3).
type Entry struct {
	Index        int    // ordinal within the chunk
	Type         string // resource type, e.g. "weapon"
	Name         string // resource name
	Path         string // source path
	Offset       uint32 // offset into chunkN.resources
	USize        uint32 // uncompressed size
	CSize        uint32 // size on disk
	Start        int    // byte position of the entry in the index
	TripleOffset int    // byte position of the offset/usize/csize triple
	Trailer      []byte // bytes after the triple, preserved verbatim
}

// Key returns "type:name".
func (e *Entry) Key() string { return e.Type + ":" + e.Name }

// Compressed reports whether the payload is raw DEFLATE (csize != usize).
func (e *Entry) Compressed() bool { return e.CSize != e.USize }

// StreamOffset returns the offset of the Ogg stream in the streamed
// container for sample entries (SPEC §3.3); ok is false when the trailer is
// shorter than 24 bytes.
func (e *Entry) StreamOffset() (offset uint32, ok bool) {
	if len(e.Trailer) < 24 {
		return 0, false
	}
	return binary.BigEndian.Uint32(e.Trailer[20:]), true
}

// ChunkIndex is a parsed chunk index backed by its original bytes. It is
// never re-serialised: SetTriple overwrites 12 bytes in place.
type ChunkIndex struct {
	buf []byte
	// ResourcesSize is the size of the matching .resources file, or -1 if
	// unknown.
	ResourcesSize int64
	CounterA      uint32
	CounterB      uint32
	Entries       []*Entry
}

func stringAt(buf []byte, pos int) (string, int, bool) {
	if pos+4 > len(buf) {
		return "", 0, false
	}
	n := int(binary.LittleEndian.Uint32(buf[pos:]))
	if n < 1 || n > maxString || pos+4+n > len(buf) {
		return "", 0, false
	}
	raw := buf[pos+4 : pos+4+n]
	for _, b := range raw {
		if b < 0x20 || b > 0x7E {
			return "", 0, false
		}
	}
	return string(raw), pos + 4 + n, true
}

// ParseChunkIndex parses a chunk index. resourcesSize is the size of the
// .resources file, or -1 if unknown (SPEC §3.1).
func ParseChunkIndex(data []byte, resourcesSize int64) (*ChunkIndex, error) {
	if len(data) < 4 || !bytes.Equal(data[:4], Magic) {
		return nil, fmt.Errorf("%w: chunk index: bad magic", ErrFormat)
	}
	if len(data) < entriesStart {
		return nil, fmt.Errorf("%w: chunk index: truncated header", ErrFormat)
	}
	ci := &ChunkIndex{
		buf:           append([]byte{}, data...),
		ResourcesSize: resourcesSize,
		CounterA:      binary.BigEndian.Uint32(data[0x20:]),
		CounterB:      binary.BigEndian.Uint32(data[0x24:]),
	}
	ci.scan()
	return ci, nil
}

func (ci *ChunkIndex) plausible(pos int) (e *Entry, ok bool) {
	buf := ci.buf
	var strs [3]string
	p := pos
	for i := range strs {
		s, np, ok := stringAt(buf, p)
		if !ok {
			return nil, false
		}
		strs[i], p = s, np
	}
	if p+12 > len(buf) {
		return nil, false
	}
	offset := binary.BigEndian.Uint32(buf[p:])
	usize := binary.BigEndian.Uint32(buf[p+4:])
	csize := binary.BigEndian.Uint32(buf[p+8:])
	if !(csize > 0 && csize <= usize) {
		return nil, false
	}
	if ci.ResourcesSize >= 0 {
		if int64(offset)+int64(csize) > ci.ResourcesSize {
			return nil, false
		}
	} else if offset < 16 {
		return nil, false
	}
	return &Entry{Type: strs[0], Name: strs[1], Path: strs[2], Offset: offset,
		USize: usize, CSize: csize, Start: pos, TripleOffset: p}, true
}

func (ci *ChunkIndex) scan() {
	pos := entriesStart
	var found []*Entry
	for pos < len(ci.buf) {
		e, ok := ci.plausible(pos)
		if !ok {
			pos++
			continue
		}
		e.Index = len(found)
		found = append(found, e)
		pos = e.TripleOffset + 12
	}
	for i, e := range found {
		end := len(ci.buf)
		if i+1 < len(found) {
			end = found[i+1].Start
		}
		e.Trailer = append([]byte{}, ci.buf[e.TripleOffset+12:end]...)
	}
	ci.Entries = found
}

// Find returns all entries with the given type and name.
func (ci *ChunkIndex) Find(typ, name string) []*Entry {
	var out []*Entry
	for _, e := range ci.Entries {
		if e.Type == typ && e.Name == name {
			out = append(out, e)
		}
	}
	return out
}

// SetTriple overwrites the entry's offset/usize/csize triple in the buffer
// and in e (SPEC §3.2).
func (ci *ChunkIndex) SetTriple(e *Entry, offset, usize, csize uint32) {
	binary.BigEndian.PutUint32(ci.buf[e.TripleOffset:], offset)
	binary.BigEndian.PutUint32(ci.buf[e.TripleOffset+4:], usize)
	binary.BigEndian.PutUint32(ci.buf[e.TripleOffset+8:], csize)
	e.Offset, e.USize, e.CSize = offset, usize, csize
}

// Bytes returns a copy of the (possibly patched) index file contents.
func (ci *ChunkIndex) Bytes() []byte { return append([]byte{}, ci.buf...) }

// IndexEntry describes one entry for BuildChunkIndex.
type IndexEntry struct {
	Type, Name, Path     string
	Offset, USize, CSize uint32
	Trailer              []byte
}

// BuildChunkIndex builds a chunk index from scratch (tests and synthetic
// archives only; the game's own files are never re-serialised). counterB < 0
// means len(entries).
func BuildChunkIndex(entries []IndexEntry, counterA uint32, counterB int64) []byte {
	var body []byte
	for _, e := range entries {
		for _, s := range []string{e.Type, e.Name, e.Path} {
			body = binary.LittleEndian.AppendUint32(body, uint32(len(s)))
			body = append(body, s...)
		}
		body = binary.BigEndian.AppendUint32(body, e.Offset)
		body = binary.BigEndian.AppendUint32(body, e.USize)
		body = binary.BigEndian.AppendUint32(body, e.CSize)
		body = append(body, e.Trailer...)
	}
	if counterB < 0 {
		counterB = int64(len(entries))
	}
	total := entriesStart + len(body)
	out := append([]byte{}, Magic...)
	out = binary.BigEndian.AppendUint32(out, uint32(total-32))
	out = append(out, make([]byte, 24)...)
	out = binary.BigEndian.AppendUint32(out, counterA)
	out = binary.BigEndian.AppendUint32(out, uint32(counterB))
	out = binary.BigEndian.AppendUint32(out, 0)
	return append(out, body...)
}
