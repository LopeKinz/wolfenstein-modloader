"""Save-game ``MD5_BlockChecksum`` header (SPEC §11)."""

from __future__ import annotations

import hashlib
import struct


def save_checksum(payload: bytes) -> bytes:
    w = struct.unpack("<4I", hashlib.md5(payload).digest())
    return struct.pack(">I", w[0] ^ w[1] ^ w[2] ^ w[3])


def verify_save(data: bytes) -> bool:
    return len(data) >= 4 and data[:4] == save_checksum(data[4:])


def fix_save(data: bytes) -> bytes:
    if len(data) < 4:
        raise ValueError("save file shorter than its 4-byte header")
    return save_checksum(data[4:]) + data[4:]
