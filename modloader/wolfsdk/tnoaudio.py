"""Wolfenstein: The New Order audio -- list sounds, hand out playable bytes.

TNO ships plain Ogg Vorbis (docs/audio.md). The streams sit in two containers
that master.index does not list; the index entry of each `sample` asset points
into them. Every stream is a complete Ogg Vorbis file (identification, comment
and setup headers included), so a browser <audio> element plays the extracted
bytes as they are -- no transcoding.

Index tail of a `sample` entry (right after offset/usize/csize), all BE:
    u32 count, then count x (char[16] language, u32 offset, u32 length)
    language "" -> streamed.resources, "english" -> english.streamed, ...
A `sound/*.samplepack` entry uses the same tail, one triple per variant; its
payload ("1KPS", LE u16 count, count x (LE u32 n, path[n], LE u32 rel, LE u32
len)) names each variant's bsnf descriptor.

bsnf descriptor (the entry payload), BE unless noted:
    "bsnf", u32 count, count x (char[16] language, u32 length, u32 sub)
    at sub: u32 hash, u32 0, u32 frames (rounded: 94 loops/music end 1-2
            frames later), u32 0, u32 length,
            LE WAVEFORMATEX(tag 0x674F, channels, rate, ..., cbSize 4),
            LE u32 exact PCM frames = the Ogg stream's last granule (all 23 164)

A samplepack variant (3 619 ids) is the same Ogg stream as the sample it is
named after, stored a second time: sounds() marks it alias_of that sample.

Reads the pristine index from base/_wolfsdk_backup when the install is patched.
"""

import struct
from functools import lru_cache
from pathlib import Path

from .resources import Archive, load_master

MIME = "audio/ogg"


def _index_dir(root):
    base = Path(root) / "base"
    backup = base / "_wolfsdk_backup"
    return backup if (backup / "chunk0.index").is_file() else base


def _container(lang):
    return "%s.streamed" % lang if lang else "streamed.resources"


def _triples(buf, pos):
    """(language, offset, length) from a sample/samplepack index tail."""
    (count,) = struct.unpack_from(">I", buf, pos)
    out = []
    for k in range(count):
        p = pos + 4 + 24 * k
        lang = bytes(buf[p:p + 16]).split(b"\0")[0].decode("ascii")
        out.append((lang,) + struct.unpack_from(">II", buf, p + 16))
    return out


def _bsnf(data):
    """{language: (length, channels, rate, frames)} from a bsnf descriptor."""
    if data[:4] != b"bsnf":
        return {}
    (count,) = struct.unpack_from(">I", data, 4)
    out = {}
    for k in range(count):
        p = 8 + 24 * k
        lang = bytes(data[p:p + 16]).split(b"\0")[0].decode("ascii")
        length, sub = struct.unpack_from(">II", data, p + 16)
        if sub + 42 > len(data):
            continue
        tag, channels, rate = struct.unpack_from("<HHI", data, sub + 20)
        frames = struct.unpack_from("<I", data, sub + 38)[0]
        if tag == 0x674F:
            out[lang] = (length, channels, rate, frames)
    return out


def _pack_paths(data):
    (count,) = struct.unpack_from("<H", data, 4)
    pos, out = 6, []
    for _ in range(count):
        (n,) = struct.unpack_from("<I", data, pos)
        out.append(data[pos + 4:pos + 4 + n].decode("ascii"))
        pos += 4 + n + 8
    return out


@lru_cache(maxsize=4)
def _catalog(root):
    """id -> (container, offset, length, channels, rate, frames)."""
    base = Path(root) / "base"
    src = _index_dir(root)
    have = {p.name for p in base.glob("*") if p.suffix in (".streamed", ".resources")}
    samples, packs, variant_meta = {}, [], {}
    for index_name, res_name in load_master(base):
        with Archive(src / index_name, src / res_name) as a:
            for e in a.entries:
                if e.type == "sample" and e.name not in samples:
                    meta = _bsnf(a.read(e))
                    for lang, off, length in _triples(a.index.buf, e.pos + 12):
                        m = meta.get(lang)
                        if m and m[0] == length and _container(lang) in have:
                            samples[e.name] = (_container(lang), off) + m
                            break
                elif e.type == "sound" and e.path.endswith(".samplepack"):
                    packs.append((_pack_paths(a.read(e)), _triples(a.index.buf, e.pos + 12)))
                elif e.type == "sound" and e.path.endswith(".bsnd") and e.path not in variant_meta:
                    variant_meta[e.path] = _bsnf(a.read(e)).get("")
    for paths, triples in packs:
        for path, (lang, off, length) in zip(paths, triples):
            m = variant_meta.get(path)
            if m and m[0] == length and _container(lang) in have:
                sid = path[len("generated/"):-len("_vorbis.bsnd")] if path.startswith("generated/") else path
                samples.setdefault(sid, (_container(lang), off) + m)
    return samples


def _alias(cat, stems, sid):
    """The .wav sample a samplepack variant id repeats, or None.

    A variant is named <sample stem>_<suffix>; the longest such stem whose
    stream has the same length and format is the sample it duplicates.
    """
    if sid.endswith(".wav"):
        return None
    stem = sid
    while "_" in stem:
        stem = stem.rsplit("_", 1)[0]
        hit = stems.get(stem)
        if hit and cat[hit][2:] == cat[sid][2:]:
            return hit
    return None


def sounds(game_root):
    """Every playable TNO sound, sorted by id. Row keys match wwise.sounds(),
    plus alias_of: the sample id a samplepack variant repeats byte for byte
    (None for the 19 545 distinct clips)."""
    cat = _catalog(str(Path(game_root)))
    stems = {sid[:-4]: sid for sid in cat if sid.endswith(".wav")}
    out = []
    for sid, (container, off, length, channels, rate, frames) in sorted(cat.items()):
        out.append(dict(id=sid, lang=container.partition(".streamed")[0] if container.endswith(".streamed") else "",
                        codec="vorbis", channels=channels, sample_rate=rate, samples=frames,
                        duration=frames / rate, size=length, source="%s@%d" % (container, off),
                        alias_of=_alias(cat, stems, sid)))
    return out


FRAME_SLACK = 0  # the descriptor's exact frame count is the last granule, always


def _check_ogg(buf, frames):
    """Walk the page chain: must end exactly at EOS with the header's granule."""
    off = 0
    while off + 27 <= len(buf):
        if buf[off:off + 4] != b"OggS":
            break
        nseg = buf[off + 26]
        end = off + 27 + nseg + sum(buf[off + 27:off + 27 + nseg])
        if buf[off + 5] & 4:
            granule = struct.unpack_from("<q", buf, off + 6)[0]
            return end == len(buf) and granule - frames == FRAME_SLACK
        off = end
    return False


def decode(game_root, sound_id):
    """(mime, bytes) of one sound: the untouched Ogg Vorbis stream."""
    entry = _catalog(str(Path(game_root))).get(sound_id)
    if entry is None:
        raise KeyError(sound_id)
    container, off, length, _ch, _rate, frames = entry
    with open(Path(game_root) / "base" / container, "rb") as fh:
        fh.seek(off)
        buf = fh.read(length)
    if not _check_ogg(buf, frames):
        raise ValueError("%s: bytes at %s@%d are not the expected Ogg stream"
                         % (sound_id, container, off))
    return MIME, buf
