package wolfsdk

import (
	"bytes"
	"compress/zlib"
	"encoding/binary"
	"fmt"
	"hash/crc32"
	"io"
)

var pngSignature = []byte{0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'}

func pngChunk(out []byte, kind string, data []byte) []byte {
	out = binary.BigEndian.AppendUint32(out, uint32(len(data)))
	start := len(out)
	out = append(out, kind...)
	out = append(out, data...)
	return binary.BigEndian.AppendUint32(out, crc32.ChecksumIEEE(out[start:]))
}

// EncodePNG encodes 8-bit RGBA pixels as a non-interlaced PNG (SPEC §9).
func EncodePNG(width, height int, rgba []byte) ([]byte, error) {
	if width < 0 || height < 0 || len(rgba) != width*height*4 {
		return nil, fmt.Errorf("%w: RGBA buffer size does not match dimensions", ErrFormat)
	}
	stride := width * 4
	raw := make([]byte, 0, height*(stride+1))
	for y := 0; y < height; y++ {
		raw = append(raw, 0)
		raw = append(raw, rgba[y*stride:(y+1)*stride]...)
	}
	var z bytes.Buffer
	zw, _ := zlib.NewWriterLevel(&z, zlib.BestCompression)
	_, _ = zw.Write(raw)
	_ = zw.Close()
	ihdr := binary.BigEndian.AppendUint32(nil, uint32(width))
	ihdr = binary.BigEndian.AppendUint32(ihdr, uint32(height))
	ihdr = append(ihdr, 8, 6, 0, 0, 0)
	out := append([]byte{}, pngSignature...)
	out = pngChunk(out, "IHDR", ihdr)
	out = pngChunk(out, "IDAT", z.Bytes())
	return pngChunk(out, "IEND", nil), nil
}

func paeth(a, b, c int) int {
	p := a + b - c
	pa, pb, pc := abs(p-a), abs(p-b), abs(p-c)
	if pa <= pb && pa <= pc {
		return a
	}
	if pb <= pc {
		return b
	}
	return c
}

func abs(x int) int {
	if x < 0 {
		return -x
	}
	return x
}

// DecodePNG decodes an 8-bit, non-interlaced PNG of colour type 0, 2, 4 or 6
// to RGBA (SPEC §9). Chunk CRCs are not verified.
func DecodePNG(data []byte) (width, height int, rgba []byte, err error) {
	if len(data) < 8 || !bytes.Equal(data[:8], pngSignature) {
		return 0, 0, nil, fmt.Errorf("%w: PNG: bad signature", ErrFormat)
	}
	pos := 8
	var idat, ihdr []byte
	for pos+8 <= len(data) {
		n := int(binary.BigEndian.Uint32(data[pos:]))
		kind := string(data[pos+4 : pos+8])
		end := pos + 8 + n
		if n > len(data)-pos-8 {
			end = len(data)
		}
		body := data[pos+8 : end]
		pos = end + 4
		if kind == "IHDR" {
			ihdr = body
		} else if kind == "IDAT" {
			idat = append(idat, body...)
		} else if kind == "IEND" {
			break
		}
	}
	if ihdr == nil {
		return 0, 0, nil, fmt.Errorf("%w: PNG: missing IHDR", ErrFormat)
	}
	if len(ihdr) != 13 {
		return 0, 0, nil, fmt.Errorf("%w: PNG: bad IHDR", ErrFormat)
	}
	w, h := int(binary.BigEndian.Uint32(ihdr)), int(binary.BigEndian.Uint32(ihdr[4:]))
	depth, ctype, interlace := ihdr[8], ihdr[9], ihdr[12]
	channels := map[byte]int{0: 1, 2: 3, 4: 2, 6: 4}[ctype]
	if depth != 8 || channels == 0 || interlace != 0 {
		return 0, 0, nil, fmt.Errorf("%w: PNG: unsupported (depth %d, colour type %d, interlace %d)", ErrFormat, depth, ctype, interlace)
	}
	zr, err := zlib.NewReader(bytes.NewReader(idat))
	if err != nil {
		return 0, 0, nil, fmt.Errorf("%w: PNG: %v", ErrFormat, err)
	}
	raw, err := io.ReadAll(zr)
	if err != nil {
		return 0, 0, nil, fmt.Errorf("%w: PNG: %v", ErrFormat, err)
	}
	bpp, stride := channels, w*channels
	if len(raw) < h*(stride+1) {
		return 0, 0, nil, fmt.Errorf("%w: PNG: image data truncated", ErrFormat)
	}
	prev := make([]byte, stride)
	pixels := make([]byte, 0, h*stride)
	for y := 0; y < h; y++ {
		f := raw[y*(stride+1)]
		line := append([]byte{}, raw[y*(stride+1)+1:(y+1)*(stride+1)]...)
		for i := 0; i < stride; i++ {
			var a, c int
			if i >= bpp {
				a, c = int(line[i-bpp]), int(prev[i-bpp])
			}
			b := int(prev[i])
			switch f {
			case 0:
			case 1:
				line[i] += byte(a)
			case 2:
				line[i] += byte(b)
			case 3:
				line[i] += byte((a + b) >> 1)
			case 4:
				line[i] += byte(paeth(a, b, c))
			default:
				return 0, 0, nil, fmt.Errorf("%w: PNG: bad filter %d", ErrFormat, f)
			}
		}
		pixels = append(pixels, line...)
		prev = line
	}
	if channels == 4 {
		return w, h, pixels, nil
	}
	out := make([]byte, w*h*4)
	for i := 0; i < w*h; i++ {
		px := pixels[i*channels : (i+1)*channels]
		o := out[i*4 : i*4+4]
		switch channels {
		case 3:
			o[0], o[1], o[2], o[3] = px[0], px[1], px[2], 255
		case 1:
			o[0], o[1], o[2], o[3] = px[0], px[0], px[0], 255
		default:
			o[0], o[1], o[2], o[3] = px[0], px[0], px[0], px[1]
		}
	}
	return w, h, out, nil
}
