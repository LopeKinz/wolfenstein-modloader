package wolfsdk

import (
	"bytes"
	"encoding/binary"
	"fmt"
)

// BIM format codes (SPEC §8).
const (
	FormatRGBA8 = 3
	FormatA8    = 5
	FormatBC1   = 10
	FormatBC3   = 11
)

var bimMagic = []byte{0x09, 'M', 'I', 'B'}

const bimDataStart = 8 + 13*4

// Mip is one mip level of a BIM image.
type Mip struct {
	Width  uint32
	Height uint32
	Data   []byte
}

// Bim is a parsed BIM image.
type Bim struct {
	Hash   uint32
	Header [13]uint32
	Mips   []Mip
}

// TextureType returns h[0].
func (b *Bim) TextureType() uint32 { return b.Header[0] }

// Width returns h[1].
func (b *Bim) Width() uint32 { return b.Header[1] }

// Height returns h[2].
func (b *Bim) Height() uint32 { return b.Header[2] }

// Depth returns h[3].
func (b *Bim) Depth() uint32 { return b.Header[3] }

// MipCount returns h[4].
func (b *Bim) MipCount() uint32 { return b.Header[4] }

// Format returns the format code h[6].
func (b *Bim) Format() uint32 { return b.Header[6] }

// FormatName returns "RGBA8", "A8", "BC1", "BC3" or "unknown(N)".
func (b *Bim) FormatName() string {
	switch b.Format() {
	case FormatRGBA8:
		return "RGBA8"
	case FormatA8:
		return "A8"
	case FormatBC1:
		return "BC1"
	case FormatBC3:
		return "BC3"
	}
	return fmt.Sprintf("unknown(%d)", b.Format())
}

// BaseWidth returns h[11].
func (b *Bim) BaseWidth() uint32 { return b.Header[11] }

// BaseHeight returns h[12].
func (b *Bim) BaseHeight() uint32 { return b.Header[12] }

// ParseBim parses a BIM image. Reading stops early (without error) when the
// data runs out; zero mips is an error.
func ParseBim(data []byte) (*Bim, error) {
	if len(data) < bimDataStart || !bytes.Equal(data[4:8], bimMagic) {
		return nil, fmt.Errorf("%w: BIM: bad magic", ErrFormat)
	}
	b := &Bim{Hash: binary.LittleEndian.Uint32(data)}
	for i := range b.Header {
		b.Header[i] = binary.BigEndian.Uint32(data[8+4*i:])
	}
	pos := bimDataStart
	for i := uint32(0); i < b.Header[4]; i++ {
		if pos+12 > len(data) {
			break
		}
		w := binary.BigEndian.Uint32(data[pos:])
		h := binary.BigEndian.Uint32(data[pos+4:])
		size := int(binary.BigEndian.Uint32(data[pos+8:]))
		pos += 12
		if size > len(data)-pos {
			break
		}
		b.Mips = append(b.Mips, Mip{w, h, data[pos : pos+size]})
		pos += size
	}
	if len(b.Mips) == 0 {
		return nil, fmt.Errorf("%w: BIM: no mip levels", ErrFormat)
	}
	return b, nil
}

// ExpectedMipSize returns the byte size of a w×h mip in format fmtCode.
func ExpectedMipSize(fmtCode uint32, w, h int) (int, error) {
	bw, bh := (w+3)/4, (h+3)/4
	switch fmtCode {
	case FormatRGBA8:
		return w * h * 4, nil
	case FormatA8:
		return w * h, nil
	case FormatBC1:
		return bw * bh * 8, nil
	case FormatBC3:
		return bw * bh * 16, nil
	}
	return 0, fmt.Errorf("%w: BIM: unsupported format code %d", ErrFormat, fmtCode)
}

func rgb565(c uint16) [4]int {
	r, g, b := int(c>>11)&31, int(c>>5)&63, int(c)&31
	return [4]int{r<<3 | r>>2, g<<2 | g>>4, b<<3 | b>>2, 255}
}

func colorPalette(block []byte, fourColor bool) ([4][4]int, uint32) {
	c0 := binary.LittleEndian.Uint16(block)
	c1 := binary.LittleEndian.Uint16(block[2:])
	p0, p1 := rgb565(c0), rgb565(c1)
	var p2, p3 [4]int
	if c0 > c1 || fourColor {
		for i := 0; i < 3; i++ {
			p2[i] = (2*p0[i] + p1[i]) / 3
			p3[i] = (p0[i] + 2*p1[i]) / 3
		}
		p2[3], p3[3] = 255, 255
	} else {
		for i := 0; i < 3; i++ {
			p2[i] = (p0[i] + p1[i]) / 2
		}
		p2[3] = 255
	}
	return [4][4]int{p0, p1, p2, p3}, binary.LittleEndian.Uint32(block[4:])
}

func alphaPalette(block []byte) ([8]int, uint64) {
	a0, a1 := int(block[0]), int(block[1])
	pal := [8]int{a0, a1}
	if a0 > a1 {
		for i := 1; i <= 6; i++ {
			pal[1+i] = ((7-i)*a0 + i*a1) / 7
		}
	} else {
		for i := 1; i <= 4; i++ {
			pal[1+i] = ((5-i)*a0 + i*a1) / 5
		}
		pal[6], pal[7] = 0, 255
	}
	var bits uint64
	for i := 7; i >= 2; i-- {
		bits = bits<<8 | uint64(block[i])
	}
	return pal, bits
}

func decodeBlocks(data []byte, w, h int, bc3 bool) []byte {
	out := make([]byte, w*h*4)
	bw, bh := (w+3)/4, (h+3)/4
	size := 8
	if bc3 {
		size = 16
	}
	for by := 0; by < bh; by++ {
		for bx := 0; bx < bw; bx++ {
			off := (by*bw + bx) * size
			blk := data[off : off+size]
			var apal [8]int
			var abits uint64
			var pal [4][4]int
			var idx uint32
			if bc3 {
				apal, abits = alphaPalette(blk[:8])
				pal, idx = colorPalette(blk[8:], true)
			} else {
				pal, idx = colorPalette(blk, false)
			}
			for t := 0; t < 16; t++ {
				x, y := bx*4+(t&3), by*4+(t>>2)
				if x >= w || y >= h {
					continue
				}
				px := pal[(idx>>(2*t))&3]
				if bc3 {
					px[3] = apal[(abits>>(3*t))&7]
				}
				o := (y*w + x) * 4
				out[o], out[o+1], out[o+2], out[o+3] = byte(px[0]), byte(px[1]), byte(px[2]), byte(px[3])
			}
		}
	}
	return out
}

// DecodeBim decodes a mip level to row-major RGBA8 (SPEC §8).
func DecodeBim(b *Bim, mip int) (width, height int, rgba []byte, err error) {
	if mip < 0 || mip >= len(b.Mips) {
		return 0, 0, nil, fmt.Errorf("%w: BIM: no mip %d", ErrFormat, mip)
	}
	m := b.Mips[mip]
	w, h, f := int(m.Width), int(m.Height), b.Format()
	need, err := ExpectedMipSize(f, w, h)
	if err != nil {
		return 0, 0, nil, err
	}
	if len(m.Data) < need {
		return 0, 0, nil, fmt.Errorf("%w: BIM: mip %d has %d bytes, needs %d", ErrFormat, mip, len(m.Data), need)
	}
	switch f {
	case FormatRGBA8:
		return w, h, append([]byte{}, m.Data[:need]...), nil
	case FormatA8:
		out := make([]byte, w*h*4)
		for i, v := range m.Data[:need] {
			out[i*4], out[i*4+1], out[i*4+2], out[i*4+3] = v, v, v, 255
		}
		return w, h, out, nil
	}
	return w, h, decodeBlocks(m.Data, w, h, f == FormatBC3), nil
}

// EncodeRGBA8 builds an RGBA8 BIM with one mip; hash and header are copied
// from template.
func EncodeRGBA8(template *Bim, width, height int, rgba []byte) ([]byte, error) {
	if len(rgba) != width*height*4 {
		return nil, fmt.Errorf("%w: RGBA buffer size does not match dimensions", ErrFormat)
	}
	header := template.Header
	header[1], header[11] = uint32(width), uint32(width)
	header[2], header[12] = uint32(height), uint32(height)
	header[4] = 1
	header[6] = FormatRGBA8
	return BuildBim(template.Hash, header, []Mip{{uint32(width), uint32(height), rgba}}), nil
}

// BuildBim serialises a BIM from raw parts (tests and synthetic data).
func BuildBim(hash uint32, header [13]uint32, mips []Mip) []byte {
	out := binary.LittleEndian.AppendUint32(nil, hash)
	out = append(out, bimMagic...)
	for _, h := range header {
		out = binary.BigEndian.AppendUint32(out, h)
	}
	for _, m := range mips {
		out = binary.BigEndian.AppendUint32(out, m.Width)
		out = binary.BigEndian.AppendUint32(out, m.Height)
		out = binary.BigEndian.AppendUint32(out, uint32(len(m.Data)))
		out = append(out, m.Data...)
	}
	return out
}
