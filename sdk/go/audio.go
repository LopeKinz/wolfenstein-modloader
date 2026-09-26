package wolfsdk

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"io"
	"strings"
)

const englishPrefix = "sound/vo/english/"

// SampleInfo is one language entry of a bsnf descriptor (SPEC §10.1).
type SampleInfo struct {
	Language       string
	Hash           uint32
	Length         uint32 // Ogg bytes
	Granule        uint32 // PCM samples
	FormatTag      uint16
	Channels       uint16
	SampleRate     uint32
	AvgBytesPerSec uint32
	BlockAlign     uint16
	BitsPerSample  uint16
}

// Duration returns granule / sampleRate in seconds (0 if the rate is 0).
func (s SampleInfo) Duration() float64 {
	if s.SampleRate == 0 {
		return 0
	}
	return float64(s.Granule) / float64(s.SampleRate)
}

func bsnfBlock(data []byte, off int, language string) (SampleInfo, error) {
	if off < 0 || off+42 > len(data) {
		return SampleInfo{}, fmt.Errorf("%w: bsnf: truncated sample block", ErrFormat)
	}
	be, le := binary.BigEndian, binary.LittleEndian
	return SampleInfo{
		Language:       language,
		Hash:           be.Uint32(data[off:]),
		Granule:        be.Uint32(data[off+8:]),
		Length:         be.Uint32(data[off+16:]),
		FormatTag:      le.Uint16(data[off+20:]),
		Channels:       le.Uint16(data[off+22:]),
		SampleRate:     le.Uint32(data[off+24:]),
		AvgBytesPerSec: le.Uint32(data[off+28:]),
		BlockAlign:     le.Uint16(data[off+32:]),
		BitsPerSample:  le.Uint16(data[off+34:]),
	}, nil
}

// ParseBsnf parses a bsnf audio descriptor.
func ParseBsnf(data []byte) ([]SampleInfo, error) {
	if len(data) < 8 || string(data[:4]) != "bsnf" {
		return nil, fmt.Errorf("%w: bsnf: bad magic", ErrFormat)
	}
	count := int64(binary.BigEndian.Uint32(data[4:]))
	if count == 1 {
		s, err := bsnfBlock(data, 0x20, "")
		if err != nil {
			return nil, err
		}
		return []SampleInfo{s}, nil
	}
	var out []SampleInfo
	for i := int64(0); i < count; i++ {
		p := 8 + int(i)*24
		if p+24 > len(data) {
			return nil, fmt.Errorf("%w: bsnf: truncated language table", ErrFormat)
		}
		name := data[p : p+16]
		if j := bytes.IndexByte(name, 0); j >= 0 {
			name = name[:j]
		}
		off := int64(binary.BigEndian.Uint32(data[p+20:]))
		s, err := bsnfBlock(data, int(off), asciiReplace(name))
		if err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, nil
}

// StreamContainerFor returns the streamed container file (in base/) that
// holds the Ogg data of a sample asset (SPEC §10.2).
func StreamContainerFor(assetName string) string {
	if strings.HasPrefix(assetName, englishPrefix) {
		return "english.streamed"
	}
	return "streamed.resources"
}

// OggStreamLength walks Ogg pages in data from offset up to and including the
// EOS page and returns the stream length in bytes.
func OggStreamLength(data []byte, offset int64) (int64, error) {
	return OggStreamLengthReader(bytes.NewReader(data), offset)
}

// OggStreamLengthReader is OggStreamLength on an io.ReaderAt (e.g. an
// *os.File of a streamed container).
func OggStreamLengthReader(r io.ReaderAt, offset int64) (int64, error) {
	read := func(pos int64, n int) []byte {
		buf := make([]byte, n)
		if pos < 0 {
			return nil
		}
		k, _ := r.ReadAt(buf, pos)
		return buf[:k]
	}
	pos := offset
	for {
		head := read(pos, 27)
		if len(head) < 27 || string(head[:4]) != "OggS" {
			return 0, fmt.Errorf("%w: Ogg: no page at offset %d", ErrFormat, pos)
		}
		flags, nseg := head[5], int(head[26])
		table := read(pos+27, nseg)
		if len(table) < nseg {
			return 0, fmt.Errorf("%w: Ogg: truncated segment table", ErrFormat)
		}
		body := 0
		for _, s := range table {
			body += int(s)
		}
		pos += int64(27 + nseg + body)
		if flags&0x04 != 0 {
			return pos - offset, nil
		}
	}
}
