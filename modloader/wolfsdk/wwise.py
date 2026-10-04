"""Wwise audio of Wolfenstein II: The New Colossus -- list it, make it playable.

Measured on the retail install (tools/verify_wwise.py re-checks all of it):

  * Every base/sound/soundbanks/pc/*.pack and dlc/dlc_N/base/sound/soundbanks/
    pc/*.pack is an ordinary IDCL archive (wolfsdk/idcl.py reads it unchanged):
    type 'file', compression 0 throughout, names '<id>.wem', 'init.bnk',
    '<lang>/<id>.wem', '<lang>/<bank>.bnk'. patch_N_<x>.pack overrides <x>.pack.
  * .wem = RIFF/WAVE. .bnk = sections (BKHD, DIDX, DATA, HIRC, ...); DIDX is
    (u32 media id, u32 offset into DATA, u32 size) and each slice is a WEM too.
    A slice whose RIFF size exceeds its DIDX size is the prefetch head of a
    streamed sound; the whole sound is the loose .wem of the same id.
  * Codecs actually used: Wwise Vorbis (fmt tag 0xFFFF, fmt size 0x42 with the
    'vorb' block inside fmt) and PCM (tag 0xFFFE, 16 bit). 43 bank slices
    (26 media ids) are not RIFF at all (codec 'other'): plugin media that
    only effect objects reference, never a Sound -- 25 belong to the Wwise
    Convolution Reverb (plugin 0x7F0003, impulse responses), 1 to plugin
    0x00021033 (third party). Not sounds, left undecoded. 78 DIDX slots are
    empty. No Opus, no ADPCM, so neither is implemented.
  * Wwise Vorbis strips the Vorbis headers: the setup packet names codebooks by
    a 10-bit id into a fixed library and drops fields the stock format has.
    Audio packets are 2-byte-size-prefixed and, unless the word at vorb+4 is
    one of ww2ogg's four "unmodified" values, lack the packet-type bit and the
    window-shape bits. 71 sounds are unmodified -- exactly the 71 with block
    sizes 512/512 -- and 29 of those are 1 s / 6 kHz placeholders of silence.
    That library -- ww2ogg ships its own copy as a separate .bin -- is linked
    into the game executable: a table of 599 ascending pointers in .data (598
    codebooks plus an end pointer) into .rdata, 71991 bytes, sha256 de31ec8d...
    It is read from NewColossus_x64vk.exe at run time, never copied into this
    repo, and each book must consume exactly its own bytes when rebuilt. The
    memory image re_probes/runtime_87400/module.bin holds the identical table
    (pointers at RVA 0x2E3E350, books at RVA 0x1E9A040):
    extract_codebooks(path, mapped=True).

Playback targets for the browser: PCM -> WAV (audio/wav), Vorbis -> standard
Ogg Vorbis (audio/ogg), rebuilt the way ww2ogg does it, plus correct granule
positions (ww2ogg leaves them 0 and needs revorb; a browser needs them for
duration and end trimming).
"""

import functools
import re
import struct
import zlib
from array import array
from pathlib import Path

from wolfsdk import idcl

EXE_NAME = "NewColossus_x64vk.exe"
SOUNDBANKS = ("sound", "soundbanks", "pc")

UNMODIFIED_SIGNALS = (0x4A, 0x4B, 0x69, 0x70)

CODECS = {0xFFFF: "vorbis", 0xFFFE: "pcm", 0x0001: "pcm", 0x0002: "adpcm",
          0x3039: "opus", 0x3040: "opus"}


class WwiseError(Exception):
    pass


# -- WEM header ---------------------------------------------------------------

def wem_info(buf, total=None):
    """Header facts of a WEM (or its first bytes). Raises on anything malformed.

    `total` is how many bytes of this WEM actually exist; a RIFF size beyond it
    marks a prefetch head (complete=False) instead of an error.
    """
    if len(buf) < 12 or buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        raise WwiseError("no RIFF/WAVE header: %r" % bytes(buf[:12]))
    riff_end = struct.unpack_from("<I", buf, 4)[0] + 8
    total = len(buf) if total is None else total
    if riff_end < total:
        raise WwiseError("RIFF length %d smaller than the data (%d)" % (riff_end, total))
    ch = {}
    p = 12
    while p + 8 <= min(len(buf), riff_end):
        cid, size = buf[p:p + 4], struct.unpack_from("<I", buf, p + 4)[0]
        ch[cid] = (p + 8, size)
        if cid == b"data":
            break
        p += 8 + size + (size & 1)
    if b"fmt " not in ch or b"data" not in ch:
        raise WwiseError("fmt or data chunk missing")
    fo, fsize = ch[b"fmt "]
    if fsize < 16:
        raise WwiseError("fmt chunk too short (%d)" % fsize)
    tag, chans, rate, avg, align, bits = struct.unpack_from("<HHIIHH", buf, fo)
    do, dsize = ch[b"data"]
    if do + dsize > riff_end:
        raise WwiseError("data chunk runs past the RIFF end")
    if not (1 <= chans <= 8 and 1000 <= rate <= 192000):
        raise WwiseError("implausible format: %d channels, %d Hz" % (chans, rate))
    info = dict(codec=CODECS.get(tag, "0x%04X" % tag), tag=tag, channels=chans,
                sample_rate=rate, avg_bytes=avg, block_align=align, bits=bits,
                data_offset=do, data_size=dsize, riff_size=riff_end,
                complete=riff_end <= total, channel_mask=None)
    if fsize >= 24:
        # AkChannelConfig where WAVE_FORMAT_EXTENSIBLE keeps dwChannelMask:
        # bits 0-7 channel count, 8-11 config type (1 = standard), 12-31 the
        # speaker mask. Retail: 0x4 mono, 0x3 stereo, 0x603 quad, 0x60F 5.1.
        cfg = struct.unpack_from("<I", buf, fo + 20)[0]
        if (cfg >> 8) & 0xF == 1 and cfg & 0xFF == chans:
            info["channel_mask"] = cfg >> 12
    if info["codec"] == "vorbis":
        if fsize != 0x42:
            raise WwiseError("Vorbis with fmt size 0x%X, only 0x42 occurs in the game" % fsize)
        v = fo + 0x18
        info["samples"], = struct.unpack_from("<I", buf, v)
        info["setup_offset"], info["audio_offset"] = struct.unpack_from("<2I", buf, v + 0x10)
        # ww2ogg's rule, and it holds here: these four words at vorb+4 mean the
        # audio packets still carry the Vorbis type bit and window bits.
        info["mod_packets"] = struct.unpack_from("<I", buf, v + 4)[0] not in UNMODIFIED_SIGNALS
        info["blocksizes"] = (buf[v + 0x28], buf[v + 0x29])
        if not (6 <= info["blocksizes"][0] <= info["blocksizes"][1] <= 13):
            raise WwiseError("implausible block sizes %r" % (info["blocksizes"],))
    elif info["codec"] == "pcm":
        if bits != 16 or align != 2 * chans:
            raise WwiseError("PCM with %d bit / block align %d not supported" % (bits, align))
        info["samples"] = dsize // align
    if "samples" in info:
        info["duration"] = info["samples"] / rate
    return info


# -- index over all packs ------------------------------------------------------

def _packs(root):
    """(scope, lang, patch level, path) of every sound pack, base first.

    A pack reachable under two folders (a custom-map DLC folder like dlc_0
    hardlinks retail dlc_2's banks) is one file and listed once, under the
    later folder: such a folder must sort before the retail one to win, so the
    later name is the retail one.
    """
    root = Path(root)
    scopes = [("base", root / "base")]
    scopes += [(d.name, d / "base") for d in sorted((root / "dlc").glob("dlc_*"))]
    out = {}
    for scope, base in scopes:
        for p in sorted(base.joinpath(*SOUNDBANKS).glob("*.pack")):
            m = re.fullmatch(r"(?:patch_(\d+)_)?(.+)\.pack", p.name)
            lang = "sfx" if m.group(2) == "sound" else m.group(2)
            st = p.stat()
            out[(st.st_dev, st.st_ino) if st.st_ino else p] = (scope, lang, int(m.group(1) or 0), p)
    return list(out.values())


def _read_at(fh, off, size):
    fh.seek(off)
    buf = fh.read(size)
    if len(buf) != size:
        raise WwiseError("short read at %d (%d/%d)" % (off, len(buf), size))
    return buf


def _bank_media(fh, off, size):
    """(media id, absolute offset, size) of every DIDX slot of the bank at `off`."""
    secs, p = {}, 0
    while p + 8 <= size:
        tag, n = struct.unpack("<4sI", _read_at(fh, off + p, 8))
        secs[tag] = (off + p + 8, n)
        p += 8 + n
    if p != size:
        raise WwiseError("bank sections do not add up to the bank length")
    if b"DIDX" not in secs:
        return []
    didx = _read_at(fh, *secs[b"DIDX"])
    data_off, data_size = secs[b"DATA"]
    out = []
    for k in range(len(didx) // 12):
        mid, o, n = struct.unpack_from("<3I", didx, 12 * k)
        if o + n > data_size:
            raise WwiseError("DIDX entry %d points past DATA" % mid)
        out.append((mid, data_off + o, n))
    return out


@functools.lru_cache(maxsize=4)
def _index(root):
    """key -> list of copies, plus counters. One pass over all packs, headers only."""
    index, empty = {}, 0
    for scope, lang, level, path in _packs(root):
        arc = idcl.Archive(path)
        arc.close()
        with open(path, "rb") as fh:
            for e in arc.entries:
                if e.compression != idcl.COMP_STORED:
                    raise WwiseError("%s: %s is compressed" % (path.name, e.name))
                if e.name.endswith(".wem"):
                    slots = [(int(e.name.rsplit("/", 1)[-1][:-4]), e.offset, e.csize)]
                    where = e.name
                elif e.name.endswith(".bnk"):
                    slots = _bank_media(fh, e.offset, e.csize)
                    where = e.name + "#"
                else:
                    raise WwiseError("%s: unexpected entry %s" % (path.name, e.name))
                for mid, off, size in slots:
                    if size == 0:
                        empty += 1
                        continue
                    head = _read_at(fh, off, min(size, 4096))
                    if head[:4] == b"RIFF":
                        info = wem_info(head, size)
                    else:
                        info = dict(codec="other", complete=True)
                    copy = dict(pack=str(path), entry=where + (str(mid) if where.endswith("#") else ""),
                                level=level, loose=where.endswith(".wem"),
                                offset=off, size=size, info=info)
                    index.setdefault("%s/%s/%d" % (scope, lang, mid), []).append(copy)
    # A localized bank may prefetch non-localized streamed media: its only
    # copy under <lang> is a head whose whole stream sits in the scope's sfx
    # pack. That is the same sound, so the head joins the sfx key.
    for key in [k for k, cs in index.items() if not any(c["info"]["complete"] for c in cs)]:
        scope, lang, mid = key.split("/")
        home = "%s/sfx/%s" % (scope, mid)
        if lang != "sfx" and any(c["info"]["complete"] for c in index.get(home, ())):
            index[home] += index.pop(key)
    return index, empty


def _best(copies):
    """Complete beats prefetch, higher patch level beats lower, loose beats bank."""
    return max(copies, key=lambda c: (c["info"]["complete"], c["level"], c["loose"]))


# Vorbis I fixes the order of 3, 5, 6, 7 and 8 channels (6: L C R RL RR LFE),
# and Ogg has no field to say otherwise. Wwise leaves its WAVE order in the
# stream (6: FL FR FC LFE SL SR, mask 0x60F), so every player puts those
# channels on the wrong speakers. 1, 2 and 4 channels mean the same in both.
VORBIS_REORDERED = (3, 5, 6, 7, 8)


def sounds(game_root):
    """Every distinct sound: one row per (scope, language, media id).

    channel_order is 'wave' for a Vorbis sound whose Ogg carries its channels
    in WAVE order where Vorbis defines another (the 150 5.1 sounds): the
    player has to remap them, the bytes cannot say so. None otherwise.
    bank is the soundbank a copy of the sound sits in (for a streamed sound
    the bank with its prefetch head), None for streams no bank holds.
    """
    index, _ = _index(str(Path(game_root)))
    rows = []
    for key, copies in sorted(index.items()):
        c = _best(copies)
        i = c["info"]
        scope, lang, mid = key.split("/")
        bank = next((b["entry"].split("#")[0] for b in copies if not b["loose"]), None)
        rows.append(dict(id=key, media_id=int(mid), scope=scope, lang=lang,
                         codec=i["codec"], channels=i.get("channels"),
                         sample_rate=i.get("sample_rate"), samples=i.get("samples"),
                         duration=i.get("duration"), size=c["size"],
                         complete=i["complete"], copies=len(copies),
                         source="%s:%s" % (Path(c["pack"]).name, c["entry"]),
                         bank=bank, channel_mask=i.get("channel_mask"),
                         channel_order="wave" if i["codec"] == "vorbis"
                         and i["channels"] in VORBIS_REORDERED else None))
    return rows


def census(game_root):
    """Codec counts: per distinct sound, per stored copy, plus empty DIDX slots."""
    index, empty = _index(str(Path(game_root)))
    unique, copies = {}, {}
    for cs in index.values():
        k = _best(cs)["info"]["codec"]
        unique[k] = unique.get(k, 0) + 1
        for c in cs:
            copies[c["info"]["codec"]] = copies.get(c["info"]["codec"], 0) + 1
    return dict(unique=unique, copies=copies, empty_slots=empty)


def read_wem(game_root, sound_id):
    """The stored bytes of the chosen copy of `sound_id`."""
    index, _ = _index(str(Path(game_root)))
    if sound_id not in index:
        raise WwiseError("unknown sound id %r" % (sound_id,))
    c = _best(index[sound_id])
    with open(c["pack"], "rb") as fh:
        return _read_at(fh, c["offset"], c["size"])


def decode(game_root, sound_id):
    """(mime, bytes) a browser <audio> plays: WAV for PCM, Ogg for Vorbis."""
    return decode_wem(read_wem(game_root, sound_id), lambda: codebooks(game_root))


def decode_wem(wem, books):
    """Convert one complete WEM. `books` is the codebook list or a thunk for it."""
    info = wem_info(wem)
    if not info["complete"]:
        raise WwiseError("only the prefetch head is present (%d of %d bytes)"
                         % (len(wem), info["riff_size"]))
    if info["codec"] == "pcm":
        return "audio/wav", _wav(info, wem)
    if info["codec"] == "vorbis":
        return "audio/ogg", _ogg_vorbis(info, wem, books() if callable(books) else books)
    raise WwiseError("codec %s is not decoded" % info["codec"])


MIN_WAV_RATE = 8000  # Edge refuses the game's 1 kHz PCM (MEDIA_ERR_SRC_NOT_SUPPORTED)


def _wav(info, wem):
    data = wem[info["data_offset"]:info["data_offset"] + info["data_size"]]
    ch, rate = info["channels"], info["sample_rate"]
    k = -(-MIN_WAV_RATE // rate)
    if k > 1:
        # Linear interpolation by an integer factor: every k-th output sample
        # is an input sample, so the original stays recoverable bit for bit.
        a, out = array("h", data), array("h")
        for i in range(0, len(a), ch):
            nxt = a[i + ch:i + 2 * ch] if i + ch < len(a) else a[i:i + ch]
            for j in range(k):
                out.extend(int(x + (y - x) * j / k) for x, y in zip(a[i:i + ch], nxt))
        data, rate = out.tobytes(), rate * k
    fmt = struct.pack("<HHIIHH", 1, ch, rate, rate * 2 * ch, 2 * ch, 16)
    return (b"RIFF" + struct.pack("<I", 4 + 24 + 8 + len(data)) + b"WAVE"
            + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data)


# -- replacing a sound -------------------------------------------------------------
#
# A replacement is 16-bit PCM: there is no Vorbis encoder here, and PCM is the
# game's other codec. Measured on the retail install (tools/verify_studio_media.py
# re-checks it):
#   * a Sound object (HIRC type 2) names its medium's codec in its plugin field:
#     0x00040001 for all 129 705 Vorbis sources, 0x00010001 for all 409 PCM
#     ones. So every Sound object of a replaced medium is switched to PCM too.
#   * its u32 at body+9 is the in-memory size: stream type 0 the whole medium
#     (= its DIDX slice, 0 mismatches), type 1 the prefetched head, type 2
#     (streamed) header + 1/10 s of samples in all 34 retail PCM streams.
#   * DATA starts 16-aligned and its slices sit 16-aligned (590 banks); a new
#     slice goes to DATA's end, the old one stays unreferenced (r2_wwisereplace).
# Refused: prefetched sounds (type 1, 2 429 ids: head in a bank, rest streamed,
# how Wwise joins a replaced pair is not measured), the 940 media of music
# tracks (type 11: they record codec and length themselves) and a media id
# stored once per language (70 in dlc_2).

PCM_PLUGIN = 0x00010001
STANDARD_MASKS = {1: 0x4, 2: 0x3, 4: 0x603, 6: 0x60F}
PREFETCHED = ("This sound streams with its start prefetched in a sound bank; replacing "
              "such sounds is not supported yet.")
MUSIC = "This sound is part of a music track; replacing music is not supported yet."
SHARED = ("This sound's id is stored once per language; replacing it in one language "
          "only is not supported.")


def parse_wav(data):
    """(channels, sample rate, 16-bit PCM bytes) of a WAV file, or WwiseError in English."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise WwiseError("That is not a WAV file (no RIFF/WAVE header).")
    fmt = pcm = None
    p = 12
    while p + 8 <= len(data):
        cid, n = data[p:p + 4], struct.unpack_from("<I", data, p + 4)[0]
        if cid == b"fmt ":
            fmt = data[p + 8:p + 8 + n]
        elif cid == b"data":
            if p + 8 + n > len(data):
                raise WwiseError("The WAV file is cut off: its data chunk says %d bytes, %d are there."
                                 % (n, len(data) - p - 8))
            pcm = data[p + 8:p + 8 + n]
            break
        p += 8 + n + (n & 1)
    if fmt is None or len(fmt) < 16 or pcm is None:
        raise WwiseError("The WAV file has no fmt or no data chunk.")
    tag, ch, rate, _avg, align, bits = struct.unpack_from("<HHIIHH", fmt)
    if tag == 0xFFFE and len(fmt) >= 26:        # WAVE_FORMAT_EXTENSIBLE: the sub format GUID starts with the tag
        tag = struct.unpack_from("<H", fmt, 24)[0]
    if tag != 1 or bits != 16:
        kind = "%d-bit PCM" % bits if tag == 1 else "32-bit float" if tag == 3 else "format 0x%04X" % tag
        raise WwiseError("Only 16-bit PCM WAV is accepted (this one is %s). Export it as WAV, "
                         "signed 16-bit PCM." % kind)
    if not (1 <= ch <= 8 and align == 2 * ch and 1000 <= rate <= 192000):
        raise WwiseError("Implausible WAV format: %d channel(s), %d Hz, block align %d." % (ch, rate, align))
    if not pcm or len(pcm) % align:
        raise WwiseError("The WAV file holds no samples or ends inside a sample.")
    return ch, rate, pcm


def pcm_wem(channels, rate, pcm, mask=None):
    """16-bit PCM as a WEM laid out like the game's own PCM sounds: fmt tag 0xFFFE
    (24 bytes, AkChannelConfig where WAVE_FORMAT_EXTENSIBLE keeps the channel mask),
    a JUNK chunk, data at offset 64."""
    mask = STANDARD_MASKS.get(channels, 0) if mask is None else mask
    cfg = channels | (1 << 8 | mask << 12 if mask else 0)
    fmt = struct.pack("<HHIIHHHHI", 0xFFFE, channels, rate, rate * 2 * channels, 2 * channels, 16, 6, 0, cfg)
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"JUNK" + struct.pack("<I", 4) + bytes(4)
            + b"data" + struct.pack("<I", len(pcm)) + bytes(pcm))
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _stream_head(wem):
    """A streamed PCM Sound object's size field: header + 1/10 s, whole sample frames."""
    i = wem_info(wem)
    return min(len(wem), i["data_offset"] + i["avg_bytes"] // 10 // i["block_align"] * i["block_align"])


def edit_bank(bnk, mid, wem):
    """`bnk` with medium `mid` replaced by the PCM WEM `wem`; `bnk` itself when nothing names it.

    A DIDX slice of `mid` goes to the end of DATA (16-aligned) and its row is
    repointed; every Sound object of `mid` gets the PCM plugin and its size
    field by stream type (see above). WwiseError for a prefetched sound, a music
    track's medium, or a bank version other than 125."""
    secs, p = [], 0
    while p + 8 <= len(bnk):
        tag, n = struct.unpack_from("<4sI", bnk, p)
        secs.append((tag, bytearray(bnk[p + 8:p + 8 + n])))
        p += 8 + n
    if p != len(bnk):
        raise WwiseError("bank sections do not add up to the bank length")
    sec = dict(secs)                    # the same bytearrays: edits land in secs
    if struct.unpack_from("<I", sec[b"BKHD"])[0] != 125:
        raise WwiseError("bank version %d is not the one parsed here (125)" % struct.unpack_from("<I", sec[b"BKHD"])[0])
    changed, at = False, None
    didx, data = sec.get(b"DIDX", b""), sec.get(b"DATA")
    for k in range(len(didx) // 12):
        if struct.unpack_from("<I", didx, 12 * k)[0] == mid:
            if at is None:
                data += bytes(-len(data) % 16)
                at = len(data)
                data += wem
            struct.pack_into("<II", didx, 12 * k + 4, at, len(wem))
            changed = True
    h = sec.get(b"HIRC")
    q = 4
    for _ in range(struct.unpack_from("<I", h)[0] if h else 0):
        t, size = struct.unpack_from("<BI", h, q)
        if t == 2 and struct.unpack_from("<I", h, q + 14)[0] == mid:
            if h[q + 13] == 1:
                raise WwiseError(PREFETCHED)
            struct.pack_into("<I", h, q + 9, PCM_PLUGIN)
            struct.pack_into("<I", h, q + 18, len(wem) if h[q + 13] == 0 else _stream_head(wem))
            changed = True
        elif t == 11:                   # music track: u8 flags, u32 n, n x (plugin, stream type, id, size, bits)
            n = struct.unpack_from("<I", h, q + 10)[0]
            if any(struct.unpack_from("<I", h, q + 19 + 14 * k)[0] == mid for k in range(n)):
                raise WwiseError(MUSIC)
        q += 5 + size
    if not changed:
        return bnk
    return b"".join(tag + struct.pack("<I", len(b)) + bytes(b) for tag, b in secs)


def _mentions(fh, off, size, mid):
    """False when the bank at `off` cannot name `mid`: its DIDX and HIRC lack the 4 bytes."""
    needle, p = struct.pack("<I", mid), 0
    while p + 8 <= size:
        tag, n = struct.unpack("<4sI", _read_at(fh, off + p, 8))
        if tag in (b"DIDX", b"HIRC") and needle in _read_at(fh, off + p + 8, n):
            return True
        p += 8 + n
    return False


def check_replaceable(copies, index, key):
    """WwiseError unless the sound `key` with these copies can be replaced (see above)."""
    if not all(c["info"]["complete"] for c in copies):
        raise WwiseError(PREFETCHED)
    scope, _lang, mid = key.split("/")
    if any(k != key and k.startswith(scope + "/") and k.endswith("/" + mid) for k in index):
        raise WwiseError(SHARED)


def replace_plan(game_root, edits):
    """{pack path: {entry index: payload}} that puts each WEM of `edits` ({sound id: WEM})
    in place of its sound. Read fresh from disk (not the cached index), writes nothing.

    Every copy the index lists is replaced (loose entries whole, bank slices by
    edit_bank), in every patch level, and every Sound object naming the medium
    is switched over in the banks of its scope: of its language and the
    unlocalized packs, or of every pack for an unlocalized sound."""
    root = Path(game_root)
    index, _ = _index.__wrapped__(str(root))
    packs, out = _packs(root), {}
    for key, wem in sorted(edits.items()):
        copies = index.get(key)
        if not copies:
            raise WwiseError("unknown sound id %r" % (key,))
        check_replaceable(copies, index, key)
        scope, lang, mid = key.split("/")
        mid, loose, done = int(mid), {(c["pack"], c["entry"]) for c in copies if c["loose"]}, set()
        for pscope, plang, _level, path in packs:
            if pscope != scope or lang not in ("sfx", plang) and plang != "sfx":
                continue
            arc = idcl.Archive(path)
            arc.close()
            mine = out.setdefault(path, {})
            with open(path, "rb") as fh:
                for e in arc.entries:
                    if (str(path), e.name) in loose:
                        mine[e.index] = wem
                        done.add((str(path), e.name))
                    elif e.name.endswith(".bnk") and (e.index in mine or _mentions(fh, e.offset, e.csize, mid)):
                        old = mine[e.index] if e.index in mine else _read_at(fh, e.offset, e.csize)
                        new = edit_bank(old, mid, wem)
                        if new is not old:
                            mine[e.index] = new
                            done.add((str(path), e.name + "#" + str(mid)))
            if not mine:
                del out[path]
        missed = {(c["pack"], c["entry"]) for c in copies} - done
        if missed:
            raise WwiseError("copies of %s not reached: %s" % (key, sorted(missed)[:3]))
    return out


# -- bits ----------------------------------------------------------------------

class _Reader:
    """LSB-first bit reader, the Vorbis bit order."""

    def __init__(self, data):
        self.v, self.n, self.pos = int.from_bytes(data, "little"), 8 * len(data), 0

    def read(self, k):
        if self.pos + k > self.n:
            raise WwiseError("bit stream ended (%d+%d > %d bits)" % (self.pos, k, self.n))
        r = (self.v >> self.pos) & ((1 << k) - 1)
        self.pos += k
        return r


class _Writer:
    def __init__(self):
        self.out, self.acc, self.nb = bytearray(), 0, 0

    def write(self, v, k):
        self.acc |= (v & ((1 << k) - 1)) << self.nb
        self.nb += k
        while self.nb >= 8:
            self.out.append(self.acc & 0xFF)
            self.acc >>= 8
            self.nb -= 8

    def copy(self, r, k):
        v = r.read(k)
        self.write(v, k)
        return v

    def bytes(self):
        return bytes(self.out) + (bytes((self.acc,)) if self.nb else b"")


def ilog(v):
    return v.bit_length()


# -- codebook library ----------------------------------------------------------

def _quantvals(entries, dims):
    """Largest v with v**dims <= entries (Vorbis lookup1_values)."""
    v = int(round(entries ** (1.0 / dims)))
    while v ** dims > entries:
        v -= 1
    while (v + 1) ** dims <= entries:
        v += 1
    return v


def _rebuild_codebook(r, w):
    """One packed Wwise codebook from `r` -> a stock Vorbis codebook into `w`."""
    dims, entries = r.read(4), r.read(14)
    if dims == 0 or entries == 0:
        raise WwiseError("codebook without dimension or entries")
    w.write(0x564342, 24)
    w.write(dims, 16)
    w.write(entries, 24)
    if w.copy(r, 1):  # ordered
        w.copy(r, 5)
        cur = 0
        while cur < entries:
            n = r.read(ilog(entries - cur))
            w.write(n, ilog(entries - cur))
            cur += n
        if cur > entries:
            raise WwiseError("codebook: ordered lengths overflow")
    else:
        lenbits, sparse = r.read(3), r.read(1)
        if not 1 <= lenbits <= 5:
            raise WwiseError("codebook: nonsensical length width %d" % lenbits)
        w.write(sparse, 1)
        for _ in range(entries):
            if sparse:
                present = r.read(1)
                w.write(present, 1)
                if not present:
                    continue
            w.write(r.read(lenbits), 5)
    lookup = r.read(1)
    w.write(lookup, 4)
    if lookup:
        w.copy(r, 32)
        w.copy(r, 32)
        vbits = r.read(4)
        w.write(vbits, 4)
        w.copy(r, 1)
        for _ in range(_quantvals(entries, dims)):
            w.copy(r, vbits + 1)
    return dims, entries, lookup


def _check_codebook(book):
    """A library codebook must consume exactly its bytes (ww2ogg's size rule)."""
    r = _Reader(book)
    _rebuild_codebook(r, _Writer())
    if r.pos // 8 + 1 != len(book):
        raise WwiseError("codebook size does not fit: %d bits in %d bytes" % (r.pos, len(book)))


def _pe_sections(fh, mapped=False):
    """image base, {name: (rva, vsize, file offset, raw size)}.

    `mapped`: the file is a memory image (file offset == RVA, like
    re_probes/runtime_87400/module.bin). The loader wrote the real load base
    into its header, so the pointers in .data still match `image base + rva`.
    """
    head = _read_at(fh, 0, 4096)
    pe = struct.unpack_from("<I", head, 0x3C)[0]
    if head[pe:pe + 4] != b"PE\0\0":
        raise WwiseError("not a PE file")
    count, opt = struct.unpack_from("<H", head, pe + 6)[0], struct.unpack_from("<H", head, pe + 20)[0]
    image_base = struct.unpack_from("<Q", head, pe + 24 + 24)[0]
    secs = {}
    for i in range(count):
        s = pe + 24 + opt + 40 * i
        name = head[s:s + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, va, rsize, raw = struct.unpack_from("<4I", head, s + 8)
        secs[name] = (va, vsize, va if mapped else raw, rsize)
    return image_base, secs


def extract_codebooks(exe_path, mapped=False):
    """The Wwise Vorbis codebook library, read out of the game executable
    (or, with `mapped`, out of a memory image of the running game).

    Found by shape, not by address: the only run of >= 500 strictly ascending
    pointers in .data that all land in .rdata with gaps of 4..2048 bytes. Its
    last pointer is the end of the blob. Each codebook is then rebuilt once
    and must consume exactly its own bytes -- a table read from the wrong place
    fails that for nearly every entry, it does not produce plausible books.
    """
    with open(exe_path, "rb") as fh:
        base, secs = _pe_sections(fh, mapped)
        if ".data" not in secs or ".rdata" not in secs:
            raise WwiseError("%s: .data/.rdata missing" % exe_path)
        _, _, draw, dsize = secs[".data"]
        rva, rvsize, rraw, _ = secs[".rdata"]
        q = array("Q")
        q.frombytes(_read_at(fh, draw, dsize - dsize % 8))
        lo, hi = base + rva, base + rva + rvsize
        runs, i = [], 0
        while i < len(q):
            j = i
            while j + 1 < len(q) and lo <= q[j] < hi and 4 <= q[j + 1] - q[j] <= 2048:
                j += 1
            if j - i + 1 >= 500:
                runs.append(q[i:j + 1])
            i = j + 1
        if len(runs) != 1:
            raise WwiseError("%d candidates for the codebook table instead of exactly 1" % len(runs))
        ptrs = runs[0]
        start = rraw + (ptrs[0] - lo)
        blob = _read_at(fh, start, ptrs[-1] - ptrs[0])
    books = [blob[a - ptrs[0]:b - ptrs[0]] for a, b in zip(ptrs, ptrs[1:])]
    for k, b in enumerate(books):
        try:
            _check_codebook(b)
        except WwiseError as exc:
            raise WwiseError("Codebook %d: %s" % (k, exc))
    return books


@functools.lru_cache(maxsize=2)
def _codebooks_at(exe):
    return extract_codebooks(exe)


def codebooks(game_root):
    return _codebooks_at(str(Path(game_root) / EXE_NAME))


# -- Wwise Vorbis -> Ogg Vorbis ---------------------------------------------------

def _rebuild_setup(r, w, books, channels):
    """ww2ogg's generate_ogg_header setup part. Returns the mode blockflags."""
    for c in b"\x05vorbis":
        w.write(c, 8)
    count = r.read(8) + 1
    w.write(count - 1, 8)
    shape = []
    for _ in range(count):
        cid = r.read(10)
        if cid >= len(books):
            raise WwiseError("codebook id %d outside the library (%d)" % (cid, len(books)))
        shape.append(_rebuild_codebook(_Reader(books[cid]), w))
    w.write(0, 6)   # one time-domain transform ...
    w.write(0, 16)  # ... of type 0
    floors = r.read(6) + 1
    w.write(floors - 1, 6)
    for _ in range(floors):
        w.write(1, 16)  # floor type 1, the only one Wwise emits
        parts = r.read(5)
        w.write(parts, 5)
        classes = [w.copy(r, 4) for _ in range(parts)]
        dims = []
        for _ in range(max(classes) + 1 if classes else 0):
            d = r.read(3)
            w.write(d, 3)
            dims.append(d + 1)
            sub = r.read(2)
            w.write(sub, 2)
            if sub:
                mb = r.read(8)
                w.write(mb, 8)
                if mb >= count:
                    raise WwiseError("Floor-Masterbook %d >= %d" % (mb, count))
            for _ in range(1 << sub):
                b = r.read(8)
                w.write(b, 8)
                if b - 1 >= count:
                    raise WwiseError("floor subclass book invalid")
        w.copy(r, 2)
        rangebits = r.read(4)
        w.write(rangebits, 4)
        for c in classes:
            for _ in range(dims[c]):
                w.copy(r, rangebits)
    residues = r.read(6) + 1
    w.write(residues - 1, 6)
    for _ in range(residues):
        rtype = r.read(2)
        if rtype > 2:
            raise WwiseError("residue type %d" % rtype)
        w.write(rtype, 16)
        w.copy(r, 24)
        w.copy(r, 24)
        psize = w.copy(r, 24) + 1
        ncls = r.read(6) + 1
        w.write(ncls - 1, 6)
        cb = r.read(8)
        w.write(cb, 8)
        # libvorbis' own setup rules (res0_unpack). They are what turns a
        # codebook library that is valid but not the one this stream was
        # encoded against into an error instead of a stream of garbage.
        if cb >= count or shape[cb][1] < ncls ** shape[cb][0]:
            raise WwiseError("residue classbook does not fit %d classes" % ncls)
        cascade = []
        for _ in range(ncls):
            low = r.read(3)
            w.write(low, 3)
            flag = r.read(1)
            w.write(flag, 1)
            high = r.read(5) if flag else 0
            if flag:
                w.write(high, 5)
            cascade.append(high * 8 + low)
        for c in cascade:
            for k in range(8):
                if c & (1 << k):
                    b = r.read(8)
                    w.write(b, 8)
                    if b >= count or not shape[b][2] or psize % shape[b][0]:
                        raise WwiseError("residue book %d invalid or without VQ table" % b)
    mappings = r.read(6) + 1
    w.write(mappings - 1, 6)
    cbits = ilog(channels - 1)
    for _ in range(mappings):
        w.write(0, 16)
        submaps = w.copy(r, 4) + 1 if w.copy(r, 1) else 1
        if w.copy(r, 1):
            steps = r.read(8) + 1
            w.write(steps - 1, 8)
            for _ in range(steps):
                m, a = r.read(cbits), r.read(cbits)
                w.write(m, cbits)
                w.write(a, cbits)
                if m == a or m >= channels or a >= channels:
                    raise WwiseError("invalid channel coupling")
        if r.read(2):
            raise WwiseError("mapping reserved field not 0")
        w.write(0, 2)
        if submaps > 1:
            for _ in range(channels):
                mux = r.read(4)
                w.write(mux, 4)
                if mux >= submaps:
                    raise WwiseError("mapping mux invalid")
        for _ in range(submaps):
            w.copy(r, 8)
            fl, rs = r.read(8), r.read(8)
            w.write(fl, 8)
            w.write(rs, 8)
            if fl >= floors or rs >= residues:
                raise WwiseError("mapping points to a missing floor/residue")
    modes = r.read(6) + 1
    w.write(modes - 1, 6)
    flags = []
    for _ in range(modes):
        f = r.read(1)
        w.write(f, 1)
        w.write(0, 16)
        w.write(0, 16)
        m = r.read(8)
        w.write(m, 8)
        if m >= mappings:
            raise WwiseError("mode points to a missing mapping")
        flags.append(bool(f))
    w.write(1, 1)  # framing
    return flags


def _wwise_packets(data, start):
    """(offset, payload) of the 2-byte-size-prefixed packets from `start` to the end."""
    out, p = [], start
    while p < len(data):
        if p + 2 > len(data):
            raise WwiseError("packet header cut off at %d" % p)
        n = struct.unpack_from("<H", data, p)[0]
        if p + 2 + n > len(data):
            raise WwiseError("packet at %d runs past the data end" % p)
        out.append((p, data[p + 2:p + 2 + n]))
        p += 2 + n
    return out


def _ogg_vorbis(info, wem, books):
    data = wem[info["data_offset"]:info["data_offset"] + info["data_size"]]
    ch, rate = info["channels"], info["sample_rate"]
    bs0, bs1 = info["blocksizes"]

    so = info["setup_offset"]
    if so + 2 > len(data):
        raise WwiseError("setup packet lies past the data")
    slen = struct.unpack_from("<H", data, so)[0]
    if so + 2 + slen != info["audio_offset"]:
        raise WwiseError("first audio packet does not follow the setup packet")
    r = _Reader(data[so + 2:so + 2 + slen])
    w = _Writer()
    flags = _rebuild_setup(r, w, books, ch)
    if (r.pos + 7) // 8 != slen:
        # The check that makes a wrong codebook library loud: every codebook
        # length feeds the bit position, so a wrong book derails the rest.
        raise WwiseError("setup packet not read exactly (%d bits of %d bytes)" % (r.pos, slen))
    setup = w.bytes()
    mbits = ilog(len(flags) - 1)

    ident = (b"\x01vorbis" + struct.pack("<IBIiii", 0, ch, rate, 0, info["avg_bytes"] * 8, 0)
             + bytes((bs0 | bs1 << 4, 1)))
    vendor = b"wolfsdk.wwise"
    comment = b"\x03vorbis" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0) + b"\x01"

    raw = [p for _, p in _wwise_packets(data, info["audio_offset"])]
    if not raw:
        raise WwiseError("no audio packets")
    mod = info["mod_packets"]
    modes = []
    for p in raw:
        if not p or (not mod and p[0] & 1):
            raise WwiseError("empty audio packet or none of type audio")
        m = (p[0] >> (0 if mod else 1)) & ((1 << mbits) - 1)
        if m >= len(flags):
            raise WwiseError("audio packet names mode %d of %d" % (m, len(flags)))
        modes.append(m)

    # Modified packets lack the packet-type bit and the two window-shape bits
    # of long blocks; both follow from the neighbours' modes and are put back.
    audio, granules = [], []
    gran, prev_long, prev_n = 0, False, 0
    for k, p in enumerate(raw):
        long_ = flags[modes[k]]
        if mod:
            v = int.from_bytes(p, "little")
            out, shift = (v & ((1 << mbits) - 1)) << 1, 1 + mbits
            if long_:
                nxt = flags[modes[k + 1]] if k + 1 < len(raw) else False
                out |= (prev_long << shift) | (nxt << (shift + 1))
                shift += 2
            out |= (v >> mbits) << shift
            p = out.to_bytes((8 * len(p) - mbits + shift + 7) // 8, "little")
        audio.append(p)
        n = 1 << (bs1 if long_ else bs0)
        if prev_n:
            gran += prev_n // 4 + n // 4
        granules.append(gran)
        prev_long, prev_n = long_, n
    if info["samples"] > gran:
        raise WwiseError("header promises %d samples, the packets give only %d" % (info["samples"], gran))
    granules[-1] = info["samples"]  # end trim: the last page carries the true length

    return _ogg([(ident, 0), (comment, 0), (setup, 0)] + list(zip(audio, granules)))


# -- Ogg --------------------------------------------------------------------------

_REV8 = bytes(int("{:08b}".format(i)[::-1], 2) for i in range(256))


def ogg_crc(data):
    """Ogg's CRC-32 (poly 0x04C11DB7, MSB-first, init 0, no xorout) via zlib.

    zlib computes the reflected variant; reflecting every input byte and the
    result turns one into the other, and xoring the CRC of an equally long run
    of zeros cancels zlib's init/xorout.
    """
    r = bytes(data).translate(_REV8)
    c = zlib.crc32(r) ^ zlib.crc32(bytes(len(r)))
    return int("{:032b}".format(c)[::-1], 2)


def _ogg(packets, serial=0x57574953):
    """One packet per page (split when > 255 lacing values); last page gets EOS."""
    out, seq = bytearray(), 0
    for k, (pkt, gran) in enumerate(packets):
        lace = [255] * (len(pkt) // 255) + [len(pkt) % 255]
        pos, first = 0, True
        while lace:
            seg, lace = lace[:255], lace[255:]
            body = pkt[pos:pos + sum(seg)]
            pos += sum(seg)
            flags = (0 if first else 1) | (2 if seq == 0 else 0) | (4 if k == len(packets) - 1 and not lace else 0)
            head = bytearray(struct.pack("<4sBBqIIIB", b"OggS", 0, flags, gran if not lace else -1,
                                         serial, seq, 0, len(seg)) + bytes(seg))
            struct.pack_into("<I", head, 22, ogg_crc(bytes(head) + body))
            out += head + body
            seq += 1
            first = False
    return bytes(out)
