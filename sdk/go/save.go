package wolfsdk

import (
	"bytes"
	"crypto/md5"
	"encoding/binary"
	"fmt"
)

// SaveChecksum returns the 4-byte MD5_BlockChecksum of payload (SPEC §11):
// BE u32 of the XOR of the four LE u32 words of MD5(payload).
func SaveChecksum(payload []byte) []byte {
	d := md5.Sum(payload)
	le := binary.LittleEndian
	x := le.Uint32(d[0:]) ^ le.Uint32(d[4:]) ^ le.Uint32(d[8:]) ^ le.Uint32(d[12:])
	return binary.BigEndian.AppendUint32(nil, x)
}

// VerifySave reports whether file[0:4] == SaveChecksum(file[4:]).
func VerifySave(file []byte) bool {
	return len(file) >= 4 && bytes.Equal(file[:4], SaveChecksum(file[4:]))
}

// FixSave returns file with a recomputed checksum header.
func FixSave(file []byte) ([]byte, error) {
	if len(file) < 4 {
		return nil, fmt.Errorf("%w: save file shorter than its 4-byte header", ErrFormat)
	}
	return append(SaveChecksum(file[4:]), file[4:]...), nil
}
