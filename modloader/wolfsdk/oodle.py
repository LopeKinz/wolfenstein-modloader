"""ctypes binding for the Oodle library the game ships (oo2core_5_win64.dll).

The New Colossus stores its texture blobs Oodle-compressed and no free
encoder for Kraken exists, so the only way to read *and* write them is to
call RAD's own DLL. It is loaded from its absolute path inside the game
folder: copying it into this project would be redistributing a licensed
library.

The signatures are Oodle 2.5.5 -- Oodle_CheckVersion reports 0x2e050530 on
the shipped DLL (46, major 5, minor 5, sizeof(OodleLZ_SeekTable) = 0x30) --
and each was confirmed against the DLL, because two of them differ from what
the public 2.9 headers describe:

  * OodleLZ_GetCompressedBufferSizeNeeded takes only the raw size here. The
    2.9 form (compressor, rawSize) does not fail loudly, it returns a
    constant 282 for every input, which would under-allocate the output
    buffer for anything larger than a few hundred bytes.
  * OodleLZ_GetChunkCompressor takes (chunkPtr, OO_BOOL* pIndependent) and
    has no compBufAvail parameter. Calling the documented 3-argument form
    faults, because the second argument is dereferenced as the out-pointer.

The other entry points take more arguments than we care about. Windows x64
has a single calling convention and the caller cleans up, so spelling out
the full list with the header's default values is both safe and explicit.
"""

import ctypes
from pathlib import Path

DLL_NAME = "oo2core_5_win64.dll"

# OodleLZ_Compressor. Only the ones the game and this project use are named;
# the numbering is RAD's and is stable across 2.x.
LZNA = 7
KRAKEN = 8
MERMAID = 9
SELKIE = 11
LEVIATHAN = 13
NONE = 3

# OodleLZ_CompressionLevel. 4 = Normal is the sane default; the Optimal
# levels cost orders of magnitude more time for a few percent of size.
LEVEL_FAST = 3
LEVEL_NORMAL = 4
LEVEL_OPTIMAL = 5

_SINTa = ctypes.c_ssize_t
_VP = ctypes.c_void_p


class OodleError(Exception):
    pass


class Oodle:
    """A loaded oo2core DLL. Cheap to hold, so load() keeps one per path."""

    def __init__(self, dll_path):
        self.path = Path(dll_path)
        if not self.path.is_file():
            raise OodleError(
                "Oodle DLL not found: %s\n"
                "It sits next to the Wolfenstein II game EXE and is "
                "only read, never copied." % self.path
            )
        try:
            lib = ctypes.CDLL(str(self.path))
        except OSError as exc:
            raise OodleError("Oodle DLL could not be loaded (%s): %s" % (self.path, exc))
        self._lib = lib

        self._compress = lib.OodleLZ_Compress
        self._compress.restype = _SINTa
        self._compress.argtypes = [
            ctypes.c_int, _VP, _SINTa, _VP, ctypes.c_int,
            _VP, _VP, _VP, _VP, _SINTa,  # options, dict, lrm, scratch, scratchSize
        ]

        self._decompress = lib.OodleLZ_Decompress
        self._decompress.restype = _SINTa
        self._decompress.argtypes = [
            _VP, _SINTa, _VP, _SINTa,
            ctypes.c_int, ctypes.c_int, ctypes.c_int,  # fuzzSafe, checkCRC, verbosity
            _VP, _SINTa, _VP, _VP, _VP, _SINTa, ctypes.c_int,
        ]

        self._bound = lib.OodleLZ_GetCompressedBufferSizeNeeded
        self._bound.restype = _SINTa
        self._bound.argtypes = [_SINTa]

        self._decode_size = lib.OodleLZ_GetDecodeBufferSize
        self._decode_size.restype = _SINTa
        self._decode_size.argtypes = [_SINTa, ctypes.c_int]

        self._chunk_compressor = lib.OodleLZ_GetChunkCompressor
        self._chunk_compressor.restype = ctypes.c_int
        self._chunk_compressor.argtypes = [_VP, ctypes.POINTER(ctypes.c_int)]

        self._name = lib.OodleLZ_Compressor_GetName
        self._name.restype = ctypes.c_char_p
        self._name.argtypes = [ctypes.c_int]

        self._check_version = lib.Oodle_CheckVersion
        self._check_version.restype = ctypes.c_int
        self._check_version.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]

    # -- introspection -----------------------------------------------------

    @property
    def version(self):
        """(major, minor) of the loaded library, straight from the DLL."""
        raw = ctypes.c_uint32(0)
        self._check_version(0, ctypes.byref(raw))
        return (raw.value >> 16) & 0xFF, (raw.value >> 8) & 0xFF

    def compressor_name(self, compressor):
        return self._name(compressor).decode("ascii")

    def chunk_compressor(self, data):
        """Which compressor produced `data`, as (id, name, independent).

        This is how a payload is identified without decoding it -- but the
        call only reads the two header bytes, so it invents a plausible
        answer for non-Oodle input too. A result is a hint until some
        decompress confirms it.
        """
        if len(data) < 2:
            raise OodleError("not enough data for an Oodle block header")
        head = bytes(data[:16])
        buf = ctypes.create_string_buffer(head, len(head))
        independent = ctypes.c_int(0)
        cid = self._chunk_compressor(ctypes.cast(buf, _VP), ctypes.byref(independent))
        return cid, self.compressor_name(cid), bool(independent.value)

    # -- the actual work ---------------------------------------------------

    def compress(self, data, compressor=KRAKEN, level=LEVEL_NORMAL):
        """Compress `data`. Empty input yields empty output, as the DLL does."""
        raw_len = len(data)
        if raw_len == 0:
            return b""
        src = ctypes.create_string_buffer(bytes(data), raw_len)
        dst = ctypes.create_string_buffer(self._bound(raw_len))
        got = self._compress(
            compressor, ctypes.cast(src, _VP), raw_len, ctypes.cast(dst, _VP),
            level, None, None, None, None, 0,
        )
        if got <= 0:
            raise OodleError(
                "OodleLZ_Compress failed (returned %d, compressor %s, "
                "level %d, %d bytes)"
                % (got, self.compressor_name(compressor), level, raw_len)
            )
        return dst.raw[:got]

    def decompress(self, data, raw_len, fuzz_safe=False):
        """Decompress `data` to exactly `raw_len` bytes.

        The output buffer is over-allocated to OodleLZ_GetDecodeBufferSize.
        That is not politeness: with fuzzSafe=No the older codecs write past
        the end of the raw data while decoding, and Oodle documents the pad
        as mandatory.

        fuzzSafe stays off by default because in 2.5 only the new codecs
        (Kraken/Mermaid/Selkie/Leviathan) are fuzz-safe. Asking for it on an
        LZNA stream makes OodleLZ_Decompress return -1 on perfectly valid
        data, which reads exactly like corruption. Switch it on for untrusted
        input, and accept that LZNA then cannot be read at all.
        """
        if raw_len == 0:
            return b""
        if not data:
            raise OodleError("no compressed data (%d bytes expected)" % raw_len)
        src = ctypes.create_string_buffer(bytes(data), len(data))
        padded = max(self._decode_size(raw_len, 0), raw_len)
        dst = ctypes.create_string_buffer(padded)
        got = self._decompress(
            ctypes.cast(src, _VP), len(data), ctypes.cast(dst, _VP), raw_len,
            1 if fuzz_safe else 0, 0, 0,
            None, 0, None, None, None, 0, 0,
        )
        if got != raw_len:
            raise OodleError(
                "OodleLZ_Decompress failed (returned %d, expected %d, "
                "%d compressed bytes, fuzzSafe=%s)"
                % (got, raw_len, len(data), fuzz_safe)
            )
        return dst.raw[:raw_len]

    def compressed_bound(self, raw_len):
        """Worst-case output size for `raw_len` bytes, per the DLL."""
        return self._bound(raw_len)

    def raw_length(self, data, limit=1 << 26, step=4):
        """Recover the uncompressed length of a stream that does not state it.

        Needed because .texdb stores no length field. There is no arithmetic
        shortcut: a decode succeeds only for the exact raw length, or for a
        prefix that ends on a 256 KB chunk boundary (and, for a block Oodle
        stored uncompressed, for any prefix at all). So the whole chunks are
        counted first and the remainder is then searched downwards -- the
        largest length that decodes is the true one, because every smaller
        hit is one of those prefixes.

        This is brute force, roughly 64k decode attempts for a small
        payload, so source and destination buffers are allocated once and
        reused: rebuilding them per attempt costs more than the decoding.

        Fuzz-safe decoding is forced on, unlike everywhere else: during the
        search a *wrong* length is the normal case, and only the fuzz-safe
        decoders reject one instead of writing past the buffer. The price is
        that this cannot measure an LZNA stream -- no .texdb payload is one.

        `step` assumes the length is a multiple of 4, which holds for every
        block-compressed texture payload. Returns None if nothing decodes.
        """
        if not data:
            return None
        src = ctypes.create_string_buffer(bytes(data), len(data))
        src_ptr, src_len = ctypes.cast(src, _VP), len(data)
        dst = ctypes.create_string_buffer(max(self._decode_size(limit, 0), limit))
        dst_ptr = ctypes.cast(dst, _VP)

        def decodes(raw):
            return raw == self._decompress(
                src_ptr, src_len, dst_ptr, raw, 1, 0, 0,
                None, 0, None, None, None, 0, 0,
            )

        quantum = 1 << 18
        full = 0
        while (full + 1) * quantum <= limit and decodes((full + 1) * quantum):
            full += 1
        for rest in range(quantum - step, 0, -step):
            if decodes(full * quantum + rest):
                return full * quantum + rest
        return full * quantum or None


_cached = {}


def find_dll(game_root=None):
    """Absolute path of the shipped DLL, from `game_root` or the TNC install."""
    if game_root is None:
        from wolfsdk.game import Game, GameError  # local: game.py knows nothing of us

        try:
            game_root = Game.find(title="tnc").root
        except GameError as exc:
            raise OodleError("Wolfenstein II not found: %s" % exc)
    return Path(game_root) / DLL_NAME


def load(game_root=None):
    """Load (once per path) and return an Oodle handle."""
    path = find_dll(game_root)
    key = str(path).lower()
    if key not in _cached:
        _cached[key] = Oodle(path)
    return _cached[key]
