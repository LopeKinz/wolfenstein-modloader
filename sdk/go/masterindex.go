package wolfsdk

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"strings"
)

// Magic is the 4-byte signature of master.index, chunk indexes and
// .resources files.
var Magic = []byte{0x03, 'S', 'E', 'R'}

// Pair is one index/resources file pair listed in base/master.index.
type Pair struct {
	Index     string
	Resources string
}

// asciiReplace decodes b as ASCII, replacing bytes >= 0x80 with U+FFFD
// (like Python's decode("ascii", "replace")).
func asciiReplace(b []byte) string {
	var sb strings.Builder
	for _, c := range b {
		if c >= 0x80 {
			sb.WriteRune('�')
		} else {
			sb.WriteByte(c)
		}
	}
	return sb.String()
}

// ParseMasterIndex parses base/master.index (SPEC §2).
func ParseMasterIndex(data []byte) ([]Pair, error) {
	if len(data) < 4 || !bytes.Equal(data[:4], Magic) {
		return nil, fmt.Errorf("%w: master.index: bad magic", ErrFormat)
	}
	if len(data) < 8 {
		return nil, fmt.Errorf("%w: master.index: truncated header", ErrFormat)
	}
	count := uint64(binary.BigEndian.Uint32(data[4:]))
	pos := 8
	var names []string
	for i := uint64(0); i < count*2; i++ {
		if pos+4 > len(data) {
			return nil, fmt.Errorf("%w: master.index: truncated", ErrFormat)
		}
		length := int(binary.LittleEndian.Uint32(data[pos:]))
		pos += 4
		if length > len(data)-pos {
			return nil, fmt.Errorf("%w: master.index: truncated name", ErrFormat)
		}
		raw := data[pos : pos+length]
		if j := bytes.IndexByte(raw, 0); j >= 0 {
			raw = raw[:j]
		}
		names = append(names, asciiReplace(raw))
		pos += length
	}
	pairs := make([]Pair, 0, len(names)/2)
	for i := 0; i+1 < len(names); i += 2 {
		pairs = append(pairs, Pair{names[i], names[i+1]})
	}
	return pairs, nil
}

// BuildMasterIndex serialises pairs; the inverse of ParseMasterIndex.
func BuildMasterIndex(pairs []Pair) []byte {
	out := append([]byte{}, Magic...)
	out = binary.BigEndian.AppendUint32(out, uint32(len(pairs)))
	for _, p := range pairs {
		for _, name := range []string{p.Index, p.Resources} {
			out = binary.LittleEndian.AppendUint32(out, uint32(len(name)+1))
			out = append(out, name...)
			out = append(out, 0)
		}
	}
	return out
}
