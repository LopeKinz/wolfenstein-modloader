"""The two wrappers Wolfenstein II puts *inside* an IDCL payload: cfile and compfile.

Both sit below the archive's own compression (every retail cfile and
compfile entry has compression 0, so read_raw() is the payload).

cfile (idEncryptedFileResource), solved in re_probes/agent_reports/cfilekey2.md:

  0x00    u32   declen   length before Oodle; <= 0 as i32 (0xFFFFFFFF) means
                         not packed -- the reader tests `jle`, not `== -1`
  0x04    12    salt
  0x10    16    iv
  0x20    n*16  AES-128-CBC ciphertext, PKCS7
  end-32  32    HMAC-SHA256 tag over salt || iv || ciphertext

  digest = SHA256(salt || b"swapTeam\\n\\x00" || resource name)
  AES key = digest[:16], HMAC key = digest

The resource name is the IDCL entry name, so a payload opens only under the
name it is stored as. declen is outside the tag.

compfile, measured over all 168 entries (129 in base/, 12/9/9/9 in
dlc/dlc_1..4/base/) -- and the 3129 extkisclule entries, which carry the
same layer:

  0x00  i64  usize   > 0: a Kraken frame follows; < 0: |usize| stored bytes
                     (17 extkisclule, no compfile)
  0x08  u64  csize   len(payload) - 16, in every one of them
  0x10  ...  Kraken frame or the stored bytes

Kraken at LEVEL_NORMAL re-packs all 168 retail compfiles byte-identically,
which is why compfile_wrap uses exactly that. tools/verify_tnccrypt.py
proves all of the above against the install.

AES goes through bcrypt.dll, the library the game itself calls (ported from
tools/cfile.py's Aes, CBC instead of single ECB blocks); Oodle through
wolfsdk/oodle.py. Both are Windows-only, like the game.
"""

import ctypes
import hashlib
import hmac
import os
import struct

from . import oodle

SECRET = b"swapTeam\n\x00"      # .rdata:0x026097e8, 10 bytes including the NUL
NOT_PACKED = 0xFFFFFFFF
SALT_LEN = 12
IV_LEN = 16
TAG_LEN = 32
HEAD = 4 + SALT_LEN + IV_LEN     # ciphertext starts here
# ponytail: sanity cap against a corrupt usize allocating the machine away;
# the largest retail compfile unpacks to 49 MB.
MAX_USIZE = 1 << 30


class TncCryptError(Exception):
    pass


class _Aes:
    """AES-CBC through bcrypt.dll. Padding is left to the caller, so a bad
    pad shows up as such instead of as an opaque CNG status."""

    def __init__(self):
        try:
            self._b = ctypes.WinDLL("bcrypt.dll")
        except (AttributeError, OSError) as exc:
            raise TncCryptError("AES not available (bcrypt.dll): %s" % exc)
        self._alg = ctypes.c_void_p()
        self._ok(self._b.BCryptOpenAlgorithmProvider(
            ctypes.byref(self._alg), "AES", None, 0), "BCryptOpenAlgorithmProvider")
        mode = "ChainingModeCBC".encode("utf-16-le") + b"\0\0"
        self._ok(self._b.BCryptSetProperty(
            self._alg, "ChainingMode", mode, len(mode), 0), "BCryptSetProperty")

    @staticmethod
    def _ok(status, what):
        if status:
            raise TncCryptError("%s: 0x%08X" % (what, status & 0xFFFFFFFF))

    def run(self, encrypt, key, iv, data):
        h = ctypes.c_void_p()
        self._ok(self._b.BCryptGenerateSymmetricKey(
            self._alg, ctypes.byref(h), None, 0, key, len(key), 0),
            "BCryptGenerateSymmetricKey")
        try:
            fn = self._b.BCryptEncrypt if encrypt else self._b.BCryptDecrypt
            ivbuf = ctypes.create_string_buffer(iv, len(iv))   # CNG overwrites it
            out = ctypes.create_string_buffer(len(data))
            n = ctypes.c_ulong()
            self._ok(fn(h, data, len(data), None, ivbuf, len(iv),
                        out, len(out), ctypes.byref(n), 0), fn.__name__)
            return out.raw[:n.value]
        finally:
            self._b.BCryptDestroyKey(h)


_AES = None
_OO = None


def _aes():
    global _AES
    if _AES is None:
        _AES = _Aes()
    return _AES


def _oodle():
    global _OO
    if _OO is None:
        try:
            _OO = oodle.load()
        except oodle.OodleError as exc:
            raise TncCryptError("Oodle not available: %s" % exc)
    return _OO


def _decompress(data, size):
    if not 0 < size <= MAX_USIZE:
        raise TncCryptError("implausible length %d" % size)
    try:
        # fuzz-safe: archives may come from mods, and every frame here is Kraken
        return _oodle().decompress(data, size, fuzz_safe=True)
    except oodle.OodleError as exc:
        raise TncCryptError(str(exc))


def _digest(salt, name):
    if isinstance(name, str):
        name = name.encode("utf-8")
    return hashlib.sha256(salt + SECRET + name).digest()


def cfile_decrypt(raw, resource_name):
    """cfile payload -> plaintext. The tag is checked before anything is decrypted."""
    raw = bytes(raw)
    body = len(raw) - HEAD - TAG_LEN
    if body < 16 or body % 16:
        raise TncCryptError("no cfile payload (%d bytes)" % len(raw))
    salt, iv, tag = raw[4:16], raw[16:HEAD], raw[-TAG_LEN:]
    d = _digest(salt, resource_name)
    if not hmac.compare_digest(hmac.new(d, raw[4:-TAG_LEN], hashlib.sha256).digest(), tag):
        raise TncCryptError("HMAC tag wrong: wrong resource name or damaged file")
    plain = _aes().run(False, d[:16], iv, raw[HEAD:-TAG_LEN])
    pad = plain[-1]
    if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad:
        raise TncCryptError("no valid PKCS7 padding")
    plain = plain[:-pad]
    (declen,) = struct.unpack_from("<i", raw)
    if declen <= 0:
        return plain
    return _decompress(plain, declen)


def cfile_encrypt(plain, resource_name, salt=None, iv=None):
    """plaintext -> cfile payload, unpacked (declen 0xFFFFFFFF), bound to
    `resource_name`. Fresh random salt and iv unless given."""
    salt = os.urandom(SALT_LEN) if salt is None else bytes(salt)
    iv = os.urandom(IV_LEN) if iv is None else bytes(iv)
    if len(salt) != SALT_LEN or len(iv) != IV_LEN:
        raise TncCryptError("salt must be %d and IV %d bytes long" % (SALT_LEN, IV_LEN))
    plain = bytes(plain)
    pad = 16 - len(plain) % 16
    d = _digest(salt, resource_name)
    ct = _aes().run(True, d[:16], iv, plain + bytes([pad]) * pad)
    tag = hmac.new(d, salt + iv + ct, hashlib.sha256).digest()
    return struct.pack("<I", NOT_PACKED) + salt + iv + ct + tag


def compfile_unwrap(raw):
    """compfile payload -> the bytes inside."""
    raw = bytes(raw)
    if len(raw) < 16:
        raise TncCryptError("no compfile payload (%d bytes)" % len(raw))
    usize, csize = struct.unpack_from("<qQ", raw)
    if csize != len(raw) - 16:
        raise TncCryptError("compfile header says %d bytes, there are %d" % (csize, len(raw) - 16))
    if usize < 0:
        if -usize != csize:
            raise TncCryptError("uncompressed compfile: length %d instead of %d" % (-usize, csize))
        return raw[16:]
    return _decompress(raw[16:], usize)


def compfile_wrap(plain):
    """Inverse of compfile_unwrap: Kraken, LEVEL_NORMAL, as retail."""
    plain = bytes(plain)
    if not plain:
        raise TncCryptError("empty compfile payload")
    try:
        oo = _oodle()
    except TncCryptError as exc:
        raise TncCryptError("compressor not available") from exc
    try:
        packed = oo.compress(plain, oodle.KRAKEN, oodle.LEVEL_NORMAL)
    except oodle.OodleError as exc:
        raise TncCryptError(str(exc))
    out = struct.pack("<QQ", len(plain), len(packed)) + packed
    if compfile_unwrap(out) != plain:      # never hand out what does not read back
        raise TncCryptError("compfile round trip failed")
    return out
