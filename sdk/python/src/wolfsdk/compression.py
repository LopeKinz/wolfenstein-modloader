"""Raw DEFLATE with sync flush and exact-size packing (SPEC §4)."""

from __future__ import annotations

import zlib
from typing import Optional, Tuple

from .errors import FormatError

SYNC_MARKER = b"\x00\x00\xff\xff"
_EMPTY_STORED = b"\x00" + SYNC_MARKER
_MAX_STORED = 0xFFFF


def inflate(stream: bytes, usize: int) -> bytes:
    """Inflate a raw DEFLATE stream to exactly ``usize`` bytes."""
    d = zlib.decompressobj(-15)
    try:
        out = d.decompress(stream, usize)
    except zlib.error as e:
        raise FormatError(f"inflate failed: {e}") from None
    if len(out) != usize:
        raise FormatError(f"inflate produced {len(out)} bytes, expected {usize}")
    return out


def deflate_sync(data: bytes) -> bytes:
    """Compress ``data`` to a raw DEFLATE stream ending in a sync flush."""
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    return co.compress(data) + co.flush(zlib.Z_SYNC_FLUSH)


def is_sync_flushed(stream: bytes) -> bool:
    return stream[-4:] == SYNC_MARKER


def _stored_block(chunk: bytes) -> bytes:
    n = len(chunk)
    return bytes([0, n & 0xFF, n >> 8, ~n & 0xFF, (~n >> 8) & 0xFF]) + chunk


def deflate_exact(data: bytes, csize: int, pad: bool) -> Optional[Tuple[bytes, bytes]]:
    """Return ``(stream, payload)`` with ``len(stream) == csize`` or ``None``.

    ``payload`` is what the stream inflates to. With ``pad`` a Decl line
    comment is appended to fill the slot (text assets only).
    """
    s = deflate_sync(data)
    if len(s) == csize:
        return s, data
    if len(s) > csize or not pad:
        return None
    r = csize - len(s)
    if r >= 13:
        n = -(-(r - 5) // (_MAX_STORED + 5))
        total = r - 5 - 5 * n
        padding = b"\n//" + b" " * (total - 3)
        out = bytearray(s)
        for i in range(n):
            out += _stored_block(padding[i * _MAX_STORED : (i + 1) * _MAX_STORED])
        out += _EMPTY_STORED
        return bytes(out), data + padding
    for k in range(3, 67):
        padding = b"\n//" + b" " * (k - 3)
        s2 = deflate_sync(data + padding)
        if len(s2) == csize:
            return s2, data + padding
    return None
