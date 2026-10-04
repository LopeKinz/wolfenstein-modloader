"""Header reader for id Tech 5 BIM images.

Layout, reverse-engineered from the shipped `image` assets:

    u32  hash / id          (little endian, purpose unconfirmed)
    4    magic 'BIM\\x09'    (stored as 09 4D 49 42, i.e. LE u32 0x42494D09)
    BE u32 x 13             header fields, see FIELDS below
    then, per mip level:
        BE u32 width, BE u32 height, BE u32 byte_count, then byte_count bytes

Only the header is modelled. Decoding the pixels means implementing the
block-compression formats the game uses, and *writing* them back would only
matter for assets that are actually read from here -- which, for weapon and
character surfaces, they are not (see docs/limits.md).
"""

import struct

MAGIC = b"\x09MIB"  # 'BIM' + version 9, byte-reversed on disk

# Field names are inferred from how their values track the payload, and the
# ones still marked unknown genuinely are.
FIELDS = (
    "texture_type", "width", "height", "depth", "mip_count",
    "unknown_5", "format", "unknown_7", "unknown_8", "unknown_9",
    "unknown_10", "base_width", "base_height",
)

# format code -> (label, block_bytes or None, bytes_per_pixel or None).
# Derived by dividing each mip's byte count by its pixel count across all 2566
# shipped images; every one of them lands exactly on one of these.
FORMATS = {
    3: ("RGBA8", None, 4),
    5: ("A8 / distance field", None, 1),
    10: ("BC1/DXT1", 8, None),
    11: ("BC3/DXT5", 16, None),
}


class BimError(Exception):
    pass


class Bim:
    def __init__(self, header, mips, hash_):
        self.header = header
        self.mips = mips  # [(width, height, size, data_offset)]
        self.hash = hash_

    def __getattr__(self, name):
        if name in FIELDS:
            return self.header[FIELDS.index(name)]
        raise AttributeError(name)

    @property
    def format_code(self):
        return self.header[FIELDS.index("format")]

    @property
    def format_name(self):
        return FORMATS.get(self.format_code, ("unknown",))[0]

    def describe(self):
        return "%dx%d  %s  %d mip(s)  type=%d" % (
            self.width, self.height, self.format_name, len(self.mips), self.texture_type,
        )


def parse(data):
    """Parse a BIM header and its mip table. Pixel data is left in place."""
    if len(data) < 60:
        raise BimError("too short for a BIM header (%d B)" % len(data))
    if data[4:8] != MAGIC:
        raise BimError("not a BIM (magic %r)" % data[4:8])
    (hash_,) = struct.unpack_from("<I", data, 0)
    header = struct.unpack_from(">13I", data, 8)
    pos = 8 + 13 * 4
    mips = []
    while pos + 12 <= len(data):
        width, height, size = struct.unpack_from(">3I", data, pos)
        pos += 12
        if size == 0 or pos + size > len(data) or width == 0 or height == 0:
            break
        mips.append((width, height, size, pos))
        pos += size
    if not mips:
        raise BimError("no mip level found")
    return Bim(header, mips, hash_)


def expected_size(width, height, format_code):
    """Byte count a mip of this size should occupy, or None if unknown."""
    info = FORMATS.get(format_code)
    if not info:
        return None
    _label, block_bytes, bytes_per_pixel = info
    if block_bytes is not None:
        return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * block_bytes
    return width * height * bytes_per_pixel
