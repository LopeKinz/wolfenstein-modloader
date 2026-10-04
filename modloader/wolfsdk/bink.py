"""ctypes binding for the Bink 2 DLL both games ship (bink2w64.dll).

Same pattern as oodle.py: RAD's own decoder is called from its absolute path
inside the game folder, never copied. Each install carries its own build --
Wolfenstein II 2.7a (2017), The New Order 1.993d (2013) -- and a video is
decoded with the DLL of the install it lives in.

Nothing here trusts the DLL blindly. `read_header` parses the KB2 header
independently, and every open compares it with what the DLL put into its
HBINK struct. The fields read from HBINK (offsets below) were found by
dumping the struct of both builds next to the file header; they agree for all
209 videos of both games (tools/verify_bink.py). A wrong offset therefore
fails loudly on the first open instead of producing a wrong-sized frame.

KB2 header, little-endian u32 unless noted (as found in both games' files):

    0x00 'KB2' + revision (g, i, j)   0x04 file size - 8
    0x08 frames                        0x0c largest frame in bytes
    0x10 frames (again)                0x14 width        0x18 height
    0x1c fps numerator                 0x20 fps denominator
    0x24 flags                         0x28 audio tracks
    [0x2c u32, only revision >= 'i']
    per track: u32 max decoded bytes, then per track u16 rate + u16 flags
    (0x2000 = stereo), then per track u32 id,
    then frames+1 u32 frame offsets (bit 0 = keyframe), the last == file size.

Audio comes from BinkOpenTrack/BinkGetTrackData: 16-bit PCM per video frame.
The DLL writes up to BINKTRACK.MaxSize bytes into the destination -- more than
BinkGetTrackMaxSize reports; a buffer of the smaller size corrupts the heap.

Paths: BinkOpen takes a char* in the ANSI code page, so a path is encoded
with 'mbcs' (strict), and one the code page cannot spell goes in as its 8.3
short name. Which DLL decodes a video is decided by the two game installs
alone (find_dll): a bink2w64.dll lying anywhere else is never loaded.
"""

import ctypes
import functools
import io
import math
import struct
import wave
from array import array
from pathlib import Path

DLL_NAME = "bink2w64.dll"

# Where the DLL keeps what we cross-check. Same in 1.993d and 2.7a.
HBINK = {"width": 0x00, "height": 0x04, "frames": 0x08,
         "fps_num": 0x14, "fps_den": 0x18, "tracks": 0x38}

SURFACE_RGBX = 4          # byte order R,G,B,x (5/6 would be BGRA/RGBA, but the
COPY_ALL = 0x80000000     # 1.993d build leaves A at 0 for videos without alpha)
STEREO = 0x2000

_V, _U = ctypes.c_void_p, ctypes.c_uint32


class BinkError(Exception):
    pass


def read_header(path):
    """Parse a .bk2/.bik header and validate it against the file on disk.

    Refuses anything the DLL should never see: other magic, a truncated or
    padded file, a frame index that does not start at the end of the header
    or does not end at the end of the file. This guard is load-bearing: the
    2.7a DLL opens a half-truncated file happily and then blocks forever in
    BinkDoFrame waiting for bytes that never come.
    """
    path = Path(path)
    with path.open("rb") as f:
        return _parse_header(f, path.stat().st_size, path.name)


def header_of(data, name="the file"):
    """read_header for a file's bytes (an upload), with the same checks."""
    return _parse_header(io.BytesIO(data), len(data), name)


def _parse_header(f, size, name):
    path = Path(name)
    head = f.read(0x2C)
    if len(head) < 0x2C or head[:3] != b"KB2":
        raise BinkError("%s is not a Bink 2 video (magic %r)" % (path.name, head[:4]))
    (_, stored, frames, _, frames2, width, height,
     fps_num, fps_den, flags, ntracks) = struct.unpack("<4s10I", head)
    if stored + 8 != size:
        raise BinkError(
            "%s is cut off or damaged: the header says %d bytes, "
            "the file has %d" % (path.name, stored + 8, size))
    if not (0 < frames == frames2 < 1 << 24 and ntracks < 256 and fps_num and fps_den):
        raise BinkError("%s: implausible Bink header (%d frames, %d audio tracks)"
                        % (path.name, frames, ntracks))
    extra = 4 if head[3:4] >= b"i" else 0
    body = f.read(extra + 12 * ntracks + 4 * (frames + 1))
    if len(body) < extra + 12 * ntracks + 4 * (frames + 1):
        raise BinkError("%s: frame index incomplete" % path.name)
    p = extra + 4 * ntracks  # skip the per-track max decoded sizes
    fmt = [struct.unpack_from("<HH", body, p + 4 * i) for i in range(ntracks)]
    ids = struct.unpack_from("<%dI" % ntracks, body, p + 4 * ntracks)
    index = struct.unpack_from("<%dI" % (frames + 1), body, p + 8 * ntracks)
    offsets = [o & ~1 for o in index]
    if offsets[0] != 0x2C + len(body) or offsets[-1] != size or any(
            b < a for a, b in zip(offsets, offsets[1:])):
        raise BinkError("%s: frame index does not match the file (start %d instead of %d, end %d instead of %d)"
                        % (path.name, offsets[0], 0x2C + len(body), offsets[-1], size))
    keys = [n for n, o in enumerate(index[:-1]) if o & 1]
    return {
        "revision": head[3:4].decode("latin-1"), "frames": frames,
        "width": width, "height": height, "fps_num": fps_num, "fps_den": fps_den,
        "flags": flags, "keyframes": len(keys), "keyframe_list": keys,
        "tracks": [{"index": i, "id": ids[i], "rate": rate,
                    "channels": 2 if fl & STEREO else 1}
                   for i, (rate, fl) in enumerate(fmt)],
    }


class Bink:
    """One loaded bink2w64.dll. load() keeps one per path."""

    def __init__(self, dll_path):
        self.path = Path(dll_path)
        if not self.path.is_file():
            raise BinkError("Bink DLL not found: %s (it sits next to the game EXE, "
                            "it is only read, not copied)" % self.path)
        try:
            lib = ctypes.CDLL(str(self.path))
        except OSError as exc:
            raise BinkError("Bink DLL cannot be loaded (%s): %s" % (self.path, exc))
        for name, res, args in (
            ("BinkOpen", _V, [ctypes.c_char_p, _U]),
            ("BinkClose", None, [_V]),
            ("BinkGetError", ctypes.c_char_p, []),
            ("BinkDoFrame", ctypes.c_int32, [_V]),
            ("BinkNextFrame", None, [_V]),
            ("BinkGoto", None, [_V, _U, ctypes.c_int32]),
            ("BinkCopyToBuffer", ctypes.c_int32, [_V, _V, ctypes.c_int32, _U, _U, _U, _U]),
            ("BinkOpenTrack", _V, [_V, _U]),
            ("BinkCloseTrack", None, [_V]),
            ("BinkGetTrackData", _U, [_V, _V]),
        ):
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = res, args
        self._lib = lib

    def open(self, path):
        h = self._lib.BinkOpen(ansi_path(path), 0)
        if not h:
            raise BinkError("BinkOpen(%s) failed: %s"
                            % (path, (self._lib.BinkGetError() or b"?").decode("latin-1")))
        return h

    def fields(self, h):
        raw = ctypes.string_at(h, max(HBINK.values()) + 4)
        return {k: struct.unpack_from("<I", raw, o)[0] for k, o in HBINK.items()}


_cached = {}


def ansi_path(path):
    """`path` as the char* BinkOpen reads: the ANSI code page, or the 8.3 name.

    os.fsencode gives UTF-8, which the DLL reads as ANSI -- every non-ASCII
    path then names a file that does not exist.
    """
    text = str(Path(path).resolve())
    try:
        return text.encode("mbcs")  # strict: no best-fit '?' for a missing character
    except UnicodeEncodeError:
        pass
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetShortPathNameW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    k32.GetShortPathNameW.restype = ctypes.c_uint32
    buf = ctypes.create_unicode_buffer(32768)
    n = k32.GetShortPathNameW(text, buf, len(buf))
    try:
        if not 0 < n < len(buf):
            raise UnicodeEncodeError("mbcs", text, 0, len(text), "no short name")
        return buf.value.encode("mbcs")
    except UnicodeEncodeError:
        raise BinkError("The path %s has characters the Bink DLL cannot read "
                        "(ANSI code page) and no 8.3 short name. Copy the video into a "
                        "folder with a simple name." % text)


@functools.lru_cache(maxsize=1)
def game_roots():
    """{'tnc'|'tno': install root} of the installed games (config + Steam)."""
    from wolfsdk.game import Game  # local: game.py knows nothing of us

    return {g.title.key: g.root.resolve() for g in Game.find_all()}


def find_dll(video_path, roots=None):
    """The bink2w64.dll of the game install `video_path` lies in, else
    Wolfenstein II's (else The New Order's).

    Only the install roots are asked -- `roots` ({key: path}, default
    game_roots()) -- never the folders around the video: a bink2w64.dll
    planted beside a downloaded video would otherwise run as code.
    """
    roots = game_roots() if roots is None else {k: Path(r).resolve() for k, r in roots.items()}
    video = Path(video_path).resolve()
    for root in roots.values():
        if root in video.parents and (root / DLL_NAME).is_file():
            return root / DLL_NAME
    for key in ("tnc", "tno"):
        if key in roots and (roots[key] / DLL_NAME).is_file():
            return roots[key] / DLL_NAME
    raise BinkError("no Bink DLL found: neither Wolfenstein II nor The New Order is "
                    "installed (the DLL sits next to the game EXE)")


def load(dll_path):
    key = str(Path(dll_path)).lower()
    if key not in _cached:
        _cached[key] = Bink(dll_path)
    return _cached[key]


_FLIP = bytes(b ^ 0x80 for b in range(256))  # high byte of s <-> high byte of s + 32768
_CHUNK = 1 << 18  # PCM bytes mixed per step (fastest measured of 64 KB..8 MB)


def _wide(pcm):
    """16-bit PCM as one int: sample i as s + 32768 in bits 32i..32i+15."""
    buf = bytearray(2 * len(pcm))
    buf[0::4] = pcm[0::2]
    buf[1::4] = pcm[1::2].translate(_FLIP)
    return int.from_bytes(buf, "little")


def mix_stereo(parts):
    """16-bit PCM parts [(channels, bytes)] mixed to one stereo PCM.

    Byte for byte what audioop (gone in Python 3.13) produced: a mono part
    becomes floor(s * 0.7071) on both sides (-3 dB), the parts are added in
    order and clipped to 16 bit after every addition, and a shorter part is
    silence past its end. tools/verify_py313.py compares both on real videos.

    The clipped addition runs on all samples of a chunk at once, as one big
    int with a 32-bit field per sample (C speed; per sample in Python the
    longest TNO cutscene, 9 min with 5 tracks, took 29 s instead of ~4).
    """
    # floor(s * 0.7071) for every 16-bit s; a negative s indexes from the end
    gain = [math.floor(s * 0.7071) for s in range(32768)] + [math.floor(s * 0.7071) for s in range(-32768, 0)]
    stereo = []
    for ch, pcm in parts:
        if ch == 1:
            pcm = array("h", map(gain.__getitem__, array("h", pcm))).tobytes()
            both = bytearray(2 * len(pcm))
            both[0::4] = both[2::4] = pcm[0::2]
            both[1::4] = both[3::4] = pcm[1::2]
            pcm = both
        stereo.append(pcm)
    n = max(map(len, stereo))
    out = bytearray(n)
    for at in range(0, n, _CHUNK):
        k = min(_CHUNK, n - at)
        one = int.from_bytes(b"\1\0\0\0" * (k // 2), "little")  # 1 in every sample's field
        acc = _wide(stereo[0][at:at + k].ljust(k, b"\0"))
        for pcm in stereo[1:]:
            # a field holds a + b + 98304 < 2**18: nothing carries into the next sample
            w = acc + _wide(pcm[at:at + k].ljust(k, b"\0")) + (one << 15)
            over = (w >> 17) & one              # a + b > 32767
            keep = (w >> 16) & one & ~over      # a + b fits; else (not over) it is below -32768
            acc = (w & keep * 0xFFFF) | over * 0xFFFF  # a + b + 32768 clipped to 0..65535
        buf = acc.to_bytes(2 * k, "little")
        out[at:at + k:2] = buf[0::4]
        out[at + 1:at + k:2] = buf[1::4].translate(_FLIP)
    return bytes(out)


class Video:
    """An open Bink video. frame(n) is random access; sequential n is cheapest."""

    def __init__(self, path, dll=None):
        self.path = Path(path)
        self.header = read_header(self.path)
        self.dll = load(dll or find_dll(self.path))
        self._h = self.dll.open(self.path)
        got = self.dll.fields(self._h)
        want = {k: len(self.header["tracks"]) if k == "tracks" else self.header[k] for k in HBINK}
        if got != want:
            self.close()
            raise BinkError("HBINK of %s does not match the file header of %s -- wrong "
                            "struct offsets? DLL %s, header %s" % (self.dll.path, self.path.name, got, want))
        self.width, self.height, self.frames = got["width"], got["height"], got["frames"]
        self.fps = got["fps_num"] / got["fps_den"]
        self.tracks = self.header["tracks"]
        self._buf = ctypes.create_string_buffer(self.width * self.height * 4)
        self._opaque = b"\xff" * (self.width * self.height)
        self._next = 0  # frame the DLL decodes on the next BinkDoFrame

    def frame(self, n, step=1):
        """Frame n (0-based) as RGBA bytes, alpha 255: width*height*4, or with
        step > 1 every step-th pixel of every step-th row, scaled_size(step).

        Seeking decodes from the previous keyframe, and these videos have few
        (1..59, often only frame 0): a jump near the end of a long video costs
        a decode of most of it at ~550-730 fps. Sequential n costs one frame.
        The scaling is bytes slicing (C speed): ~2 ms for 1920x1080 at step 2.
        """
        if not 0 <= n < self.frames:
            raise IndexError("frame %d outside 0..%d" % (n, self.frames - 1))
        if self._h is None:
            raise BinkError("video is closed")
        lib = self.dll._lib
        if n != self._next:
            if 0 <= self._next < n and not any(self._next < k <= n for k in self.header["keyframe_list"]):
                for _ in range(n - self._next):  # no keyframe on the way: decode on from here,
                    lib.BinkDoFrame(self._h)      # BinkGoto would start again at the keyframe
                    lib.BinkNextFrame(self._h)
            else:
                lib.BinkGoto(self._h, n + 1, 0)  # 1-based; decodes from the keyframe, exact
        lib.BinkDoFrame(self._h)
        lib.BinkCopyToBuffer(self._h, self._buf, self.width * 4, self.height, 0, 0,
                             SURFACE_RGBX | COPY_ALL)
        lib.BinkNextFrame(self._h)
        self._next = n + 1
        if step == 1:
            memoryview(self._buf).cast("B")[3::4] = self._opaque
            return self._buf.raw
        w, h = self.scaled_size(step)
        row, mv = self.width * 4, memoryview(self._buf).cast("B")
        # keep every step-th row, cut to a multiple of step pixels; then every
        # step-th pixel of the joined rows is every step-th pixel of each row
        rows = b"".join([mv[y * row:y * row + 4 * w * step] for y in range(0, self.height, step)])
        out = bytearray(memoryview(rows).cast("I")[::step])
        out[3::4] = self._opaque[:w * h]
        return bytes(out)

    def scaled_size(self, step):
        """(width, height) of frame(n, step)."""
        if not 1 <= step <= min(self.width, self.height):
            raise ValueError("step %d outside 1..%d" % (step, min(self.width, self.height)))
        return self.width // step, -(-self.height // step)

    def iter_frames(self, start=0):
        for n in range(start, self.frames):
            yield self.frame(n)

    def pcm(self, track=0):
        """(rate, channels, 16-bit PCM bytes) of one audio track, by index."""
        if not 0 <= track < len(self.tracks):
            raise BinkError("%s has no audio track %d (%d present)"
                            % (self.path.name, track, len(self.tracks)))
        lib = self.dll._lib
        h = self.dll.open(self.path)  # own handle: leaves the video position alone
        try:
            t = lib.BinkOpenTrack(h, track)
            if not t:
                raise BinkError("BinkOpenTrack(%d) failed: %s"
                                % (track, (lib.BinkGetError() or b"?").decode("latin-1")))
            try:
                rate, bits, channels, max_size = struct.unpack("<4I", ctypes.string_at(t, 16))
                want = self.tracks[track]
                if (rate, channels, bits) != (want["rate"], want["channels"], 16):
                    raise BinkError("audio track %d: DLL reports %d Hz/%d channels/%d bit, header %d Hz/%d channels"
                                    % (track, rate, channels, bits, want["rate"], want["channels"]))
                buf = ctypes.create_string_buffer(max_size)
                out = bytearray()
                # Audio needs no BinkDoFrame: same bytes, ~45x faster.
                for _ in range(self.frames):
                    got = lib.BinkGetTrackData(t, buf)
                    if got > max_size:
                        raise BinkError("BinkGetTrackData returned %d > %d bytes" % (got, max_size))
                    out += buf.raw[:got]
                    lib.BinkNextFrame(h)
            finally:
                lib.BinkCloseTrack(t)
        finally:
            lib.BinkClose(h)
        return rate, channels, bytes(out)

    def audio(self, tracks=0):
        """WAV bytes of one track, or of several mixed to stereo (mono at -3 dB).

        The New Order splits its audio over 10-11 tracks: stereo beds plus
        mono voice tracks, one per language (ids 3/4/6/10 in cs_c01_theplan are
        four different languages; which is which is not known yet). A list
        plays tracks together, e.g. audio([0, 1, 3]).
        """
        picks = [tracks] if isinstance(tracks, int) else list(tracks)
        if not picks:
            raise BinkError("no audio track chosen")
        parts = [self.pcm(i) for i in picks]
        rate, channels, data = parts[0]
        if len(parts) > 1:
            for r, _, _ in parts:
                if r != rate:
                    raise BinkError("audio tracks with different sample rates (%d/%d Hz)" % (rate, r))
            channels, data = 2, mix_stereo([(ch, pcm) for _, ch, pcm in parts])
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(data)
        return out.getvalue()

    def close(self):
        if getattr(self, "_h", None):
            self.dll._lib.BinkClose(self._h)
        self._h = None

    __del__ = close

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open(path, dll=None):  # noqa: A001 -- the module API is bink.open(path)
    return Video(path, dll)
