"""TNC cutscene sound: bink video -> Wwise event -> Play actions -> sounds -> media.

Most TNC cutscene videos carry no audio track; the engine posts the Wwise
event play_<video stem> when the bink starts (cvar bink_audioSyncToSoundEngine
= 1). Measured on the retail install (re_probes/cutsceneaudio/):

  * every TNC video has a `sound` decl play_<stem> (lower case, stem incl. _row);
    the Wwise event id is FNV-1 32 of that name; 108 of 123 resolve in a bank.
  * event (HIRC type 4, bank version 125): u8 count, u32 action ids.
    Action (type 3): u16 type (0x0403 = Play), u32 target, u8 isBus, props
    (0x0F DelayTime ms).
  * Sound (type 2): u32 plugin, u8 stream type, u32 source (= media id), u32
    size, u8 bits, then NodeBaseParams (FX, attachment byte, u32 bus, u32
    parent, u8 bits, props 0x00 Volume dB, 0x06 MakeUpGain dB).
  * all layers start with video frame 0 (+ the Play delay); envelope
    cross-correlation against the two videos that also embed the mix: +0.01 s.
  * a localized bank <lang>/<bank>.bnk carries its own copy of event and
    sounds with the same object ids; the dialogue media id differs per language.

Gains ignore in-game bus ducking/HDR (an approximation). Structurally checked,
not seen in the game.

    python -m wolfsdk.cutsceneaudio <game root> [stem [lang]]
"""
import functools
import struct
import sys
from pathlib import Path

from . import idcl, wwise

PLAY, DELAY, VOLUME, MAKEUP = 0x0403, 0x0F, 0x00, 0x06


def fnv1(name):
    h = 2166136261
    for b in name.lower().encode("utf-8"):
        h = ((h * 16777619) & 0xFFFFFFFF) ^ b
    return h


ROLES = {fnv1("cutscenes_vo"): "Dialogue", fnv1("cutscenes"): "Music & effects"}


def _hirc(fh, off, size):
    """[(type, id, body)] of the bank at `off`, DATA skipped."""
    p, out = 0, []
    while p + 8 <= size:
        fh.seek(off + p)
        tag, n = struct.unpack("<4sI", fh.read(8))
        if tag == b"BKHD":
            ver = struct.unpack("<I", fh.read(4))[0]
            if ver != 125:
                return []                           # not the layout parsed here
        elif tag == b"HIRC":
            buf = fh.read(n)
            q = 4
            for _ in range(struct.unpack_from("<I", buf)[0]):
                t, sz, oid = struct.unpack_from("<BII", buf, q)
                out.append((t, oid, buf[q + 9:q + 5 + sz]))
                q += 5 + sz
        p += 8 + n
    return out


@functools.lru_cache(maxsize=2)
def objects(root):
    """object id -> [(type, body, lang, scope, patch level, bank)], every copy in every pack."""
    objs = {}
    for scope, _lang, level, path in wwise._packs(root):
        arc = idcl.Archive(path)
        arc.close()
        with open(path, "rb") as fh:
            for e in arc.entries:
                if e.name.endswith(".bnk"):
                    lang = e.name.split("/")[0] if "/" in e.name else "sfx"
                    for t, oid, body in _hirc(fh, e.offset, e.csize):
                        objs.setdefault(oid, []).append((t, body, lang, scope, level, e.name))
    return objs


def _props(b, p):
    n = b[p]
    vals = struct.unpack_from("<%dI" % n, b, p + 1 + n)
    return dict(zip(b[p + 1:p + 1 + n], vals))


def _f32(v):
    return struct.unpack("<f", struct.pack("<I", v))[0]


def _node(body, p):
    """NodeBaseParams at p -> (bus, parent, props)."""
    nfx = body[p + 1]
    p += 2 + (1 + 7 * nfx if nfx else 0) + 1
    bus, parent = struct.unpack_from("<II", body, p)
    return bus, parent, _props(body, p + 9)


def _pick(copies, lang, type_):
    """The copy for `lang`: that language's bank, then English, then any; highest patch level."""
    rank = {lang: 2, "english(us)": 1}
    cs = [c for c in copies if c[0] == type_]
    return max(cs, key=lambda c: (rank.get(c[2], 0), c[4])) if cs else None


def languages(root, stem):
    """Languages of the banks that hold the video's event ([] = no event)."""
    ev = objects(str(root)).get(fnv1("play_" + stem))
    return sorted({c[2] for c in ev or [] if c[2] != "sfx"}) or (["sfx"] if ev else [])


def resolve(root, stem, lang="english(us)"):
    """The layers of the video's event. stem = file name without .bk2."""
    root = str(root)
    objs = objects(root)
    name = "play_" + stem.lower()
    out = dict(event=name, layers=[], skipped=[])
    ev = _pick(objs.get(fnv1(name), []), lang, 4)
    if ev is None:
        out["reason"] = "No Wwise event %s in any sound bank" % name
        return out
    _t, body, ev_lang, scope, _lvl, bank = ev
    out.update(bank=bank, lang=ev_lang)
    index, _ = wwise._index(root)
    for aid in struct.unpack_from("<%dI" % body[0], body, 1):
        act = _pick(objs.get(aid, []), lang, 3)
        if act is None:
            continue
        atype, target = struct.unpack_from("<HI", act[1], 0)
        if atype != PLAY:
            continue
        snd = _pick(objs.get(target, []), lang, 2)
        if snd is None:                             # e.g. a music playlist (type 13): not resolved
            out["skipped"].append(dict(target=target, type=next((c[0] for c in objs.get(target, [])), None)))
            continue
        mid = struct.unpack_from("<I", snd[1], 5)[0]
        bus, parent, props = _node(snd[1], 14)
        gain = _f32(props.get(VOLUME, 0)) + _f32(props.get(MAKEUP, 0))
        seen = set()
        while parent in objs and parent not in seen:  # actor-mixer chain
            seen.add(parent)
            b2, parent, p2 = _node(objs[parent][0][1], 0)
            bus = bus or b2
            gain += _f32(p2.get(VOLUME, 0)) + _f32(p2.get(MAKEUP, 0))
        key = next((k for k in ("%s/%s/%d" % (scope, snd[2], mid), "%s/sfx/%d" % (scope, mid)) if k in index), None)
        if key is None:
            out["skipped"].append(dict(target=target, type=2, media=mid))
            continue
        info = wwise._best(index[key])["info"]
        out["layers"].append(dict(
            sound=key, media=mid, role=ROLES.get(bus, "Extra"), lang=snd[2],
            start=_props(act[1], 7).get(DELAY, 0) / 1000.0, gain_db=round(gain, 2),
            channels=info.get("channels"), duration=info.get("duration"),
            channel_order="wave" if info.get("codec") == "vorbis"
            and info.get("channels") in wwise.VORBIS_REORDERED else None))
    if not out["layers"]:
        out["reason"] = "Event %s plays no sound that could be resolved" % name
    return out


def bank_audio(root, stem):
    """What the Studio shows and plays for a video: {event, bank, langs: {lang: layers}, default,
    note}, or {reason} when nothing resolves."""
    langs = [lg for lg in languages(root, stem) if lg != "sfx"] or ["english(us)"]
    by = {}
    for lg in langs:
        r = resolve(root, stem, lg)
        if r["layers"]:
            by[lg] = r
    if not by:
        return dict(event="play_" + stem.lower(), reason=resolve(root, stem)["reason"])
    default = "english(us)" if "english(us)" in by else sorted(by)[0]
    r = by[default]
    return dict(event=r["event"], bank=r["bank"], default=default,
                langs={lg: v["layers"] for lg, v in by.items()},
                skipped=r["skipped"],
                note="Wwise event %s in bank %s, played in sync with the video. "
                     "Mix levels are approximate (no in-game ducking)." % (r["event"], r["bank"]))


if __name__ == "__main__":
    import json
    root = Path(sys.argv[1])
    if len(sys.argv) > 2:
        print(json.dumps(resolve(root, *sys.argv[2:4]), indent=1))
        sys.exit()
    assert fnv1("play_c02p0_sickbay_traps_row") == 0x9956893C
    got = {(l["media"], l["role"]) for l in resolve(root, "c02p0_sickbay_traps_row")["layers"]}
    assert got == {(198554657, "Dialogue"), (957348772, "Music & effects")}, got
    assert resolve(root, "c02p0_sickbay_traps_row_x")["layers"] == []          # control
    sp = resolve(root, "c10_surpriseparty_fergtime_row")["layers"]
    assert sorted(l["start"] for l in sp) == [0.0, 0.0, 3.5], sp
    print("self-check ok")
