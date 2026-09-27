package wolfsdk

import (
	"bytes"
	"compress/flate"
	"fmt"
	"io"
	"strings"
)

var syncMarker = []byte{0x00, 0x00, 0xFF, 0xFF}

const maxStored = 0xFFFF

// Inflate inflates a raw DEFLATE stream to exactly usize bytes. The stream
// need not contain a final block (SPEC §4).
func Inflate(stream []byte, usize int) ([]byte, error) {
	r := flate.NewReader(bytes.NewReader(stream))
	defer r.Close()
	if usize <= 0 {
		out, err := io.ReadAll(r)
		if len(out) != 0 {
			return nil, fmt.Errorf("%w: inflate produced %d bytes, expected 0", ErrFormat, len(out))
		}
		if err != nil && err != io.ErrUnexpectedEOF {
			return nil, fmt.Errorf("%w: inflate failed: %v", ErrFormat, err)
		}
		return []byte{}, nil
	}
	out := make([]byte, usize)
	n, err := io.ReadFull(r, out)
	if err != nil {
		if err == io.EOF || err == io.ErrUnexpectedEOF {
			return nil, fmt.Errorf("%w: inflate produced %d bytes, expected %d", ErrFormat, n, usize)
		}
		return nil, fmt.Errorf("%w: inflate failed: %v", ErrFormat, err)
	}
	return out, nil
}

// DeflateSync compresses data (level 9) to a raw DEFLATE stream that ends in
// a sync flush (00 00 FF FF) and has no final block.
func DeflateSync(data []byte) []byte {
	var buf bytes.Buffer
	w, err := flate.NewWriter(&buf, flate.BestCompression)
	if err != nil {
		panic(err) // unreachable: level is valid
	}
	// Writes to a bytes.Buffer cannot fail. Flush emits the sync marker; the
	// writer is deliberately not closed (that would add a final block).
	_, _ = w.Write(data)
	_ = w.Flush()
	return buf.Bytes()
}

// IsSyncFlushed reports whether stream ends with 00 00 FF FF.
func IsSyncFlushed(stream []byte) bool {
	return bytes.HasSuffix(stream, syncMarker)
}

func storedBlock(out, chunk []byte) []byte {
	n := len(chunk)
	out = append(out, 0, byte(n), byte(n>>8), byte(^n), byte(^n>>8))
	return append(out, chunk...)
}

// DeflateExact produces a sync-flushed stream of exactly csize bytes
// (SPEC §4.1). payload is what the stream inflates to. With pad, a Decl line
// comment is appended to fill the slot (text assets only). ok is false when
// no such stream can be produced.
func DeflateExact(data []byte, csize int, pad bool) (stream, payload []byte, ok bool) {
	s := DeflateSync(data)
	if len(s) == csize {
		return s, data, true
	}
	if len(s) > csize || !pad {
		return nil, nil, false
	}
	r := csize - len(s)
	if r >= 13 {
		n := (r - 5 + maxStored + 5 - 1) / (maxStored + 5)
		total := r - 5 - 5*n
		padding := []byte("\n//" + strings.Repeat(" ", total-3))
		out := append([]byte{}, s...)
		for i := 0; i < n; i++ {
			lo := i * maxStored
			hi := lo + maxStored
			if hi > len(padding) {
				hi = len(padding)
			}
			if lo > hi {
				lo = hi
			}
			out = storedBlock(out, padding[lo:hi])
		}
		out = append(out, 0)
		out = append(out, syncMarker...)
		return out, concat(data, padding), true
	}
	for k := 3; k <= 66; k++ {
		padded := concat(data, []byte("\n//"+strings.Repeat(" ", k-3)))
		s2 := DeflateSync(padded)
		if len(s2) == csize {
			return s2, padded, true
		}
	}
	return nil, nil, false
}

func concat(a, b []byte) []byte {
	out := make([]byte, 0, len(a)+len(b))
	return append(append(out, a...), b...)
}
