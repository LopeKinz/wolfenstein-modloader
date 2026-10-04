"""Decoding BIM pixel data and writing PNG, using only the standard library.

Supports the four formats the game actually ships (see bim.py): BC1/DXT1,
BC3/DXT5, RGBA8 and single-channel A8.

Two decode paths:

    decode()          full resolution, one output pixel per source pixel
    decode_preview()  one output pixel per 4x4 block for the compressed
                      formats, i.e. a quarter-size thumbnail for ~1/16th of
                      the work

The preview path matters: this is pure Python, and a 2048x2048 texture is
four million pixels. Thumbnails stay interactive; full decode is reserved for
export, where waiting a moment is fine.
"""

import struct
import zlib

from .bim import FIELDS, FORMATS, MAGIC, parse as parse_bim

BC1 = 10
BC3 = 11
RGBA8 = 3
A8 = 5


class ImageError(Exception):
    pass


def _rgb565(value):
    r = (value >> 11) & 0x1F
    g = (value >> 5) & 0x3F
    b = value & 0x1F
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def _bc1_palette(c0, c1):
    r0, g0, b0 = _rgb565(c0)
    r1, g1, b1 = _rgb565(c1)
    if c0 > c1:
        return (
            (r0, g0, b0, 255),
            (r1, g1, b1, 255),
            ((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3, 255),
            ((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3, 255),
        )
    return (
        (r0, g0, b0, 255),
        (r1, g1, b1, 255),
        ((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, 255),
        (0, 0, 0, 0),
    )


def _bc3_alphas(a0, a1):
    if a0 > a1:
        return [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
    return [a0, a1] + [((5 - i) * a0 + i * a1) // 5 for i in range(1, 5)] + [0, 255]


def decode(data, offset, width, height, format_code):
    """Return (width, height, RGBA bytes) at full resolution."""
    if format_code == RGBA8:
        end = offset + width * height * 4
        return width, height, bytearray(data[offset:end])
    if format_code == A8:
        out = bytearray(width * height * 4)
        for i in range(width * height):
            value = data[offset + i]
            out[i * 4 : i * 4 + 4] = bytes((value, value, value, 255))
        return width, height, out
    if format_code not in (BC1, BC3):
        raise ImageError("format %r is not decoded" % format_code)

    out = bytearray(width * height * 4)
    blocks_x = max(1, (width + 3) // 4)
    blocks_y = max(1, (height + 3) // 4)
    stride = width * 4
    block_bytes = 8 if format_code == BC1 else 16
    pos = offset

    for by in range(blocks_y):
        for bx in range(blocks_x):
            if format_code == BC3:
                a0, a1 = data[pos], data[pos + 1]
                alphas = _bc3_alphas(a0, a1)
                abits = int.from_bytes(data[pos + 2 : pos + 8], "little")
                c0, c1, bits = struct.unpack_from("<HHI", data, pos + 8)
                palette = _bc1_palette(max(c0, c1), min(c0, c1))
            else:
                alphas = None
                c0, c1, bits = struct.unpack_from("<HHI", data, pos)
                palette = _bc1_palette(c0, c1)
            pos += block_bytes

            for py in range(4):
                y = by * 4 + py
                if y >= height:
                    break
                row = y * stride
                for px in range(4):
                    x = bx * 4 + px
                    if x >= width:
                        break
                    index = py * 4 + px
                    r, g, b, a = palette[(bits >> (2 * index)) & 3]
                    if alphas is not None:
                        a = alphas[(abits >> (3 * index)) & 7]
                    at = row + x * 4
                    out[at] = r
                    out[at + 1] = g
                    out[at + 2] = b
                    out[at + 3] = a
    return width, height, out


def decode_preview(data, offset, width, height, format_code):
    """Quarter-size decode: one pixel per compressed block."""
    if format_code not in (BC1, BC3):
        return decode(data, offset, width, height, format_code)

    blocks_x = max(1, (width + 3) // 4)
    blocks_y = max(1, (height + 3) // 4)
    out = bytearray(blocks_x * blocks_y * 4)
    block_bytes = 8 if format_code == BC1 else 16
    pos = offset

    for by in range(blocks_y):
        row = by * blocks_x * 4
        for bx in range(blocks_x):
            if format_code == BC3:
                a0, a1 = data[pos], data[pos + 1]
                c0, c1 = struct.unpack_from("<HH", data, pos + 8)
                alpha = max(a0, a1)
                palette = _bc1_palette(max(c0, c1), min(c0, c1))
            else:
                c0, c1 = struct.unpack_from("<HH", data, pos)
                alpha = 255
                palette = _bc1_palette(c0, c1)
            pos += block_bytes
            # Average the two endpoints: a fair stand-in for the whole block.
            r = (palette[0][0] + palette[1][0]) // 2
            g = (palette[0][1] + palette[1][1]) // 2
            b = (palette[0][2] + palette[1][2]) // 2
            at = row + bx * 4
            out[at] = r
            out[at + 1] = g
            out[at + 2] = b
            out[at + 3] = alpha if palette[3][3] else 255
    return blocks_x, blocks_y, out


def to_png(width, height, rgba):
    """Encode RGBA bytes as a PNG file."""
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # filter type 0 (None)
        raw += rgba[y * stride : (y + 1) * stride]

    def chunk(tag, payload):
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def from_png(png):
    """Decode a PNG into (width, height, RGBA bytes).

    Handles the subset PNG encoders actually produce for 8-bit images:
    colour types 6 (RGBA), 2 (RGB), 0 (grey) and 4 (grey+alpha), all five
    filter types, non-interlaced.
    """
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ImageError("not a PNG file")
    pos, idat, width = 8, bytearray(), None
    while pos + 8 <= len(png):
        length, tag = struct.unpack_from(">I4s", png, pos)
        body = png[pos + 8 : pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, color, _c, _f, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8:
                raise ImageError("only 8 bits per channel are supported (got %d)" % depth)
            if interlace:
                raise ImageError("interlaced PNG is not supported")
            channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
            if channels is None:
                raise ImageError("color type %d is not supported" % color)
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + length
    if width is None:
        raise ImageError("IHDR missing")

    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(width * height * 4)
    prev = bytearray(stride)
    at = 0
    for y in range(height):
        filt = raw[at]
        line = bytearray(raw[at + 1 : at + 1 + stride])
        at += 1 + stride
        _unfilter(filt, line, prev, channels)
        for x in range(width):
            px = line[x * channels : (x + 1) * channels]
            if channels == 4:
                r, g, b, a = px
            elif channels == 3:
                r, g, b, a = px[0], px[1], px[2], 255
            elif channels == 2:
                r = g = b = px[0]
                a = px[1]
            else:
                r = g = b = px[0]
                a = 255
            o = (y * width + x) * 4
            out[o], out[o + 1], out[o + 2], out[o + 3] = r, g, b, a
        prev = line
    return width, height, out


def _unfilter(filt, line, prev, bpp):
    if filt == 0:
        return
    for i in range(len(line)):
        a = line[i - bpp] if i >= bpp else 0
        b = prev[i]
        if filt == 1:
            line[i] = (line[i] + a) & 0xFF
        elif filt == 2:
            line[i] = (line[i] + b) & 0xFF
        elif filt == 3:
            line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
        elif filt == 4:
            c = prev[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line[i] = (line[i] + pred) & 0xFF
        else:
            raise ImageError("unknown PNG filter %d" % filt)


def to_bim(width, height, rgba, template=None):
    """Build a BIM file holding one uncompressed RGBA8 mip.

    Format code 3 stores raw RGBA, so no block compressor is needed -- the
    reason texture replacement works at all without implementing BC1/BC3
    encoding. The header's unknown fields are copied from `template` (the BIM
    being replaced) so anything the engine reads but we have not decoded keeps
    its original value.
    """
    if len(rgba) != width * height * 4:
        raise ImageError("pixel data does not match %dx%d" % (width, height))

    if template is None:
        raise ImageError(
            "to_bim needs a template: several header fields are not "
            "decoded and must be taken over from the replaced image")

    # Copy the original header wholesale and change only what must change.
    # Writing our own values here cost a full debugging round: setting
    # mip_count to 0 made the engine read zero mip levels and log
    #   idFile_RingBuffer ... skipping <n> bytes of stream data
    # while discarding the entire payload. The shipped files carry mip_count
    # 5, 7 or 8 even though only mip 0 is stored inline, and base_width /
    # base_height are always 0 -- so those fields do not mean what their
    # names suggest and are left exactly as found.
    header = list(template.header)
    hash_ = template.hash
    header[FIELDS.index("width")] = width
    header[FIELDS.index("height")] = height
    header[FIELDS.index("format")] = RGBA8

    out = bytearray()
    out += struct.pack("<I", hash_)
    out += MAGIC
    out += struct.pack(">13I", *header)
    out += struct.pack(">3I", width, height, len(rgba))
    out += bytes(rgba)
    return bytes(out)


def png_to_bim(png, template=None):
    width, height, rgba = from_png(png)
    return to_bim(width, height, rgba, template)


def bim_to_png(bim, data, mip=0, preview=False):
    """Render one mip of a parsed BIM as PNG bytes."""
    if mip >= len(bim.mips):
        raise ImageError("mip level %d does not exist" % mip)
    width, height, _size, offset = bim.mips[mip]
    code = bim.format_code
    if code not in FORMATS:
        raise ImageError("unknown format code %d" % code)
    decoder = decode_preview if preview else decode
    out_w, out_h, rgba = decoder(data, offset, width, height, code)
    return to_png(out_w, out_h, rgba)
