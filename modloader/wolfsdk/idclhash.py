"""The three hashes an IDCL container carries, as the engine computes them.

  murmur64b      entry +0x50, MurmurHash64B over the decompressed payload,
                 seed 0xDEADBEEF (.text 0x12BB970)
  farmhash64     header +0x20, FarmHash Fingerprint64 over the metadata
                 (.text 0x1363B80)
  header_hash    header +0x08, farmhash64(header[0x10:0x78]) through the kMul
                 finalizer, mixed with the version (.text 0xACBA6B..0xACBAAE)
  metadata_size  how many bytes after the header the +0x20 hash covers
                 (.text 0xACBB0E..0xACBB47)

This is the one implementation. The derivations and the checks against the
retail archives live in tools/probe_datahash.py and tools/probe_idclhash.py,
which import from here; wolfsdk itself must not import from tools/, because
the release package ships without it.

    python -m wolfsdk.idclhash     # self-test against the published vector
"""

import struct

MASK64 = (1 << 64) - 1

# -- entry +0x50: MurmurHash64B -------------------------------------------------

DATA_SEED = 0xDEADBEEF
M = 0x5BD1E995
U32 = 0xFFFFFFFF


def murmur64b(data, seed=DATA_SEED):
    """.text 0x12BB970, instruction for instruction (MurmurHash64B)."""
    n = len(data)
    h1 = (seed ^ n) & U32
    h2 = 0
    i = 0
    while n >= 8:
        k1 = int.from_bytes(data[i:i + 4], "little")
        k1 = (k1 * M) & U32
        k1 ^= k1 >> 24
        k1 = (k1 * M) & U32
        h1 = ((h1 * M) & U32) ^ k1
        k2 = int.from_bytes(data[i + 4:i + 8], "little")
        k2 = (k2 * M) & U32
        k2 ^= k2 >> 24
        k2 = (k2 * M) & U32
        h2 = ((h2 * M) & U32) ^ k2
        i += 8
        n -= 8
    if n >= 4:
        k1 = int.from_bytes(data[i:i + 4], "little")
        k1 = (k1 * M) & U32
        k1 ^= k1 >> 24
        k1 = (k1 * M) & U32
        h1 = ((h1 * M) & U32) ^ k1
        i += 4
        n -= 4
    if n == 3:
        h2 ^= data[i + 2] << 16
    if n >= 2:
        h2 ^= data[i + 1] << 8
    if n >= 1:
        h2 = (((h2 ^ data[i]) & U32) * M) & U32
    h1 = (((h1 ^ (h2 >> 18)) & U32) * M) & U32
    h2 = (((h2 ^ (h1 >> 22)) & U32) * M) & U32
    h1 = (((h1 ^ (h2 >> 17)) & U32) * M) & U32
    h2 = (((h2 ^ (h1 >> 19)) & U32) * M) & U32
    return (h1 << 32) | h2


# -- header +0x20: FarmHash Fingerprint64 ---------------------------------------

K0 = 0xC3A5C85C97CB3127
K1 = 0xB492B66FBE98F273
K2 = 0x9AE16A3B2F90404F


def _u64(value):
    return value & MASK64


def _fetch64(data, pos=0):
    return struct.unpack_from("<Q", data, pos)[0]


def _fetch32(data, pos=0):
    return struct.unpack_from("<I", data, pos)[0]


def _rotate(value, shift):
    if shift == 0:
        return value
    return _u64((value >> shift) | (value << (64 - shift)))


def _shift_mix(value):
    return value ^ (value >> 47)


def _hash_len16(u, v, mul=K2):
    a = _u64((u ^ v) * mul)
    a ^= a >> 47
    b = _u64((v ^ a) * mul)
    b ^= b >> 47
    return _u64(b * mul)


def _hash_len_0_to_16(data):
    length = len(data)
    if length >= 8:
        mul = _u64(K2 + length * 2)
        a = _u64(_fetch64(data) + K2)
        b = _fetch64(data, length - 8)
        c = _u64(_rotate(b, 37) * mul + a)
        d = _u64((_rotate(a, 25) + b) * mul)
        return _hash_len16(c, d, mul)
    if length >= 4:
        mul = _u64(K2 + length * 2)
        a = _fetch32(data)
        return _hash_len16(_u64(length + (a << 3)), _fetch32(data, length - 4), mul)
    if length:
        a = data[0]
        b = data[length >> 1]
        c = data[length - 1]
        y = a + (b << 8)
        z = length + (c << 2)
        return _u64(_shift_mix(_u64(y * K2) ^ _u64(z * K0)) * K2)
    return K2


def _hash_len_17_to_32(data):
    length = len(data)
    mul = _u64(K2 + length * 2)
    a = _u64(_fetch64(data) * K1)
    b = _fetch64(data, 8)
    c = _u64(_fetch64(data, length - 8) * mul)
    d = _u64(_fetch64(data, length - 16) * K2)
    return _hash_len16(
        _u64(_rotate(_u64(a + b), 43) + _rotate(c, 30) + d),
        _u64(a + _rotate(_u64(b + K2), 18) + c), mul)


def _hash_len_33_to_64(data):
    length = len(data)
    mul = _u64(K2 + length * 2)
    a = _u64(_fetch64(data) * K2)
    b = _fetch64(data, 8)
    c = _u64(_fetch64(data, length - 8) * mul)
    d = _u64(_fetch64(data, length - 16) * K2)
    y = _u64(_rotate(_u64(a + b), 43) + _rotate(c, 30) + d)
    z = _hash_len16(y, _u64(a + _rotate(_u64(b + K2), 18) + c), mul)
    e = _u64(_fetch64(data, 16) * mul)
    f = _fetch64(data, 24)
    g = _u64((y + _fetch64(data, length - 32)) * mul)
    h = _u64((z + _fetch64(data, length - 24)) * mul)
    return _hash_len16(
        _u64(_rotate(_u64(e + f), 43) + _rotate(g, 30) + h),
        _u64(e + _rotate(_u64(f + a), 18) + g), mul)


def _weak_hash_len32_with_seeds(data, pos, a, b):
    w = _fetch64(data, pos)
    x = _fetch64(data, pos + 8)
    y = _fetch64(data, pos + 16)
    z = _fetch64(data, pos + 24)
    a = _u64(a + w)
    b = _rotate(_u64(b + a + z), 21)
    c = a
    a = _u64(a + x + y)
    b = _u64(b + _rotate(a, 44))
    return _u64(a + z), _u64(b + c)


def farmhash64(data):
    """FarmHash Fingerprint64 (farmhashna::Hash64), returned as an unsigned u64."""
    length = len(data)
    if length <= 16:
        return _hash_len_0_to_16(data)
    if length <= 32:
        return _hash_len_17_to_32(data)
    if length <= 64:
        return _hash_len_33_to_64(data)

    seed = 81
    x = seed
    y = _u64(seed * K1 + 113)
    z = _u64(_shift_mix(_u64(y * K2 + 113)) * K2)
    v0 = v1 = w0 = w1 = 0
    x = _u64(x * K2 + _fetch64(data))
    end = (length - 1) & ~63
    last64 = end + ((length - 1) & 63) - 63
    pos = 0
    while pos != end:
        x = _u64(_rotate(_u64(x + y + v0 + _fetch64(data, pos + 8)), 37) * K1)
        y = _u64(_rotate(_u64(y + v1 + _fetch64(data, pos + 48)), 42) * K1)
        x ^= w1
        y = _u64(y + v0 + _fetch64(data, pos + 40))
        z = _u64(_rotate(_u64(z + w0), 33) * K1)
        v0, v1 = _weak_hash_len32_with_seeds(data, pos, _u64(v1 * K1), _u64(x + w0))
        w0, w1 = _weak_hash_len32_with_seeds(data, pos + 32, _u64(z + w1), _u64(y + _fetch64(data, pos + 16)))
        x, z = z, x
        pos += 64

    mul = _u64(K1 + ((z & 0xFF) << 1))
    pos = last64
    w0 = _u64(w0 + ((length - 1) & 63))
    v0 = _u64(v0 + w0)
    w0 = _u64(w0 + v0)
    x = _u64(_rotate(_u64(x + y + v0 + _fetch64(data, pos + 8)), 37) * mul)
    y = _u64(_rotate(_u64(y + v1 + _fetch64(data, pos + 48)), 42) * mul)
    x ^= _u64(w1 * 9)
    y = _u64(y + v0 * 9 + _fetch64(data, pos + 40))
    z = _u64(_rotate(_u64(z + w0), 33) * mul)
    v0, v1 = _weak_hash_len32_with_seeds(data, pos, _u64(v1 * mul), _u64(x + w0))
    w0, w1 = _weak_hash_len32_with_seeds(data, pos + 32, _u64(z + w1), _u64(y + _fetch64(data, pos + 16)))
    x, z = z, x
    return _hash_len16(
        _u64(_hash_len16(v0, w0, mul) + _u64(_shift_mix(y) * K0) + z),
        _u64(_hash_len16(v1, w1, mul) + x), mul)


# -- header +0x08 and the +0x20 span --------------------------------------------

KMUL = 0x9DDFEA08EB382D69


def finalize(h, version):
    """The kMul finalizer of .text 0xACBA70..0xACBAAE, applied to a FarmHash."""
    a = ((version ^ h) * KMUL) & MASK64
    a ^= a >> 47
    b = ((a ^ h) * KMUL) & MASK64
    b ^= b >> 44
    b = (b * KMUL) & MASK64
    b ^= b >> 41
    return (b * KMUL) & MASK64


def header_hash(header, version):
    """Header +0x08. header+0x20 lies inside the hashed span, so set it first."""
    return finalize(farmhash64(header[0x10:0x78]), version)


def metadata_size(header):
    """Re-implementation of .text 0xACBB0E..0xACBB47."""
    (entries, deps, dep_idx, str_idx, u38, u3c,
     strings_size, u44) = struct.unpack_from("<8I", header, 0x28)
    n = 9 * entries + 2 * deps
    n += u38
    n = u3c + 2 * n
    n += str_idx
    n = dep_idx + 2 * n
    n += 1
    n = u44 + 4 * n
    return n + strings_size


if __name__ == "__main__":
    # Published FarmHash Fingerprint64 vector; the retail-archive checks are
    # tools/probe_idclhash.py and tools/probe_datahash.py.
    assert farmhash64(b"hello world") == 6381520714923946011, "farmhash64 drifted"
    print("farmhash64 self-test ok")
