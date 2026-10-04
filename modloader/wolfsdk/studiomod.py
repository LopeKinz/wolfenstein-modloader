"""The Studio's 'Replace': a file picked for one item becomes a mod folder under mods/.

The Studio never writes the game. A replacement is saved as a mod in the forms
wolfsdk/mod.py applies (mods/<folder>/mod.json plus its files), and the loader
applies it like any other mod.

    replace_info(st, game, kind, item, skin=None)     GET  /api/<g>/<kind>/replace
        {ok, reason, form: "png"|"text", target, group, blood, notes}
    replace(st, game, kind, item, body, name=None, folder=None, author="", blood=True,
            skin=None, mods_dir=MODS)                 POST /api/<g>/<kind>/replace
        {mod, folder, name, assets, form, notes, message}
    studio_mods(mods_dir, game)                       GET  /api/<g>/mods: the Studio's mods to add to

Per kind:
  textures TNC  a PNG of any size -> the image's own BC1 mip chain (tncskin.check_image,
                image_assets): `image` for it and for every base name sharing its data,
                `texdb:<key>` for every streamed mip the game ships; with blood, its
                *_blood siblings get the same picture
  models TNC    a PNG + skin=<material of the model> -> that material's colour map, as above
  textures TNO  a PNG -> resized to the original's size, one RGBA8 mip under the original's
                header (image.to_bim, the old browser's way); A8 distance fields refused
  texts         UTF-8 text that parses (tokens, braces balanced; JSON for *.json) -> `set`
                with the changed values when decl.apply_patch turns the loader's text into
                exactly this one, else `file`; cfile/compfile always `file`, wrapped again
                under their own name. TNC: base/ only (the patcher writes nothing else).
  scripts TNC   save_script: edit ops on a Kiscule script (wolfsdk/kiscule.py apply_ops) -> the
                extkisclule entry as `file`, packed again exactly as retail packs it. base/ only.
                Saved into a Studio mod that changes the script already, the ops go onto that
                mod's copy, so edits pile up over saves; script_info shows a script as in a mod.
  sounds TNC    a 16-bit PCM WAV with the sound's channel count, at most its length -> a PCM
                WEM (wwise.pcm_wem) as `wem:<sound id>`; the loader rewrites every sound pack
                holding it (wwise.replace_plan, checked here first). Prefetched sounds, music
                tracks and ids stored once per language are refused (no Vorbis encoder: PCM).
  videos TNC    a Bink 2 file made with RAD's tools: same size and frame rate as the game's,
                at most its frames, opened by the game's own bink2w64.dll -> `video:<path>`,
                the loose file replaced whole. TNO sounds and videos: refused, with the reason.

A new mod goes to mods/<slug of its name>/ with the id studio.<slug>, and its
mod.json carries "studio": {"version": 1}. An existing one is named by its
folder, which must be one of studio_mods(): the request never makes up a path.
Errors are studio.BadRequest (400), Missing (404) and Unsupported (422), in
English (the Studio shows them); nothing is written before the last check.
Structurally checked, not confirmed in game.
"""

import contextlib
import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
import threading
import unicodedata
import zlib
from pathlib import Path, PurePosixPath

from . import bink, cutsceneaudio, decl, image, kiscule, maptex, modelexport, oodle, studio, tnccrypt, tncpatch, tncskin, tncview, wwise
from . import mod as modlib
from .bim import BimError, parse as parse_bim
from .game import TITLES, Game
from .idcl import COMP_KRAKEN_BLOCKS, IdclError
from .patch import Installation as TnoInstallation, PatchError
from .resources import ResourceError
from .tncimage import TncImageError

MODS = Path(__file__).resolve().parent.parent / "mods"
MAX_PNG = modelexport.MAX_SKIN          # bytes of an uploaded PNG
MAX_SIDE = 8192                         # pixels a side (the largest retail texture)
MAX_OPS = 2 << 20                       # bytes of a script save request (the edit ops, JSON)
MAX_TEXT = 8 << 20                      # bytes of an uploaded text (retail's largest: 1.7 MB)
MAX_WAV = 256 << 20                     # bytes of an uploaded WAV (10 min of 48 kHz stereo: 115 MB)
MAX_VIDEO = 1 << 30                     # bytes of an uploaded Bink video (retail's largest: 289 MB)
LONGER = 0.01                           # s a sound may run past the game's (export rounding)
FPS_SLACK = 0.0005                      # relative: 30/1 passes for 10000000/333333, 29.97 does not
MARK = "studio"
MESSAGE = "Saved as mod: %s. Tick it in the loader under Mods and apply."
_lock = threading.Lock()                # ponytail: one save at a time over every mod folder

REFUSED = {
    ("tno", "sounds"): "The New Order's sounds are Ogg streams in the .streamed files, outside the "
                       "archives the loader writes, and there is no Vorbis encoder here yet.",
    ("tno", "videos"): "The New Order's videos are loose Bink files the loader does not write for "
                       "that game; replacing videos works for Wolfenstein II.",
    # ponytail: held back until revert restores whole files (tools/verify_studio_media.py, "after revert")
    ("tnc", "sounds"): "Replacing sounds is held back in this release: resetting the mods does not yet "
                       "restore the replaced sound packs. It comes in an update.",
    ("tnc", "videos"): "Replacing videos is held back in this release: resetting the mods does not yet "
                       "restore the replaced video files. It comes in an update.",
}


# -- checks ----------------------------------------------------------------------

def check_name(name):
    """The mod's display name, or BadRequest."""
    name = (name or "").strip()
    if not name:
        raise studio.BadRequest("Give the new mod a name.")
    if len(name) > 60:
        raise studio.BadRequest("The mod name is too long (at most 60 characters).")
    if re.search(r"[\x00-\x1f\x7f/\\:]|\.\.", name):
        raise studio.BadRequest("The mod name must not contain / \\ : .. or control characters.")
    return name


def check_author(author):
    author = (author or "").strip()
    if len(author) > 60 or re.search(r"[\x00-\x1f\x7f]", author):
        raise studio.BadRequest("The author must be at most 60 characters, without control characters.")
    return author


def slug(name):
    """The folder of a new mod: ASCII [a-z0-9_] only ('Meine Flinte!' -> 'meine_flinte')."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", plain.lower()).strip("_")[:40].rstrip("_") or "mod"


def check_png(data):
    """(w, h, RGBA) of an uploaded PNG, or BadRequest -- before any pixel is decoded
    the size, the end and the IHDR are checked (a 60000 px IHDR would need 14 GB)."""
    if not data:
        raise studio.BadRequest("The file is empty.")
    if len(data) > MAX_PNG:
        raise studio.BadRequest("The PNG is too large (at most %d MB)." % (MAX_PNG >> 20))
    size = modelexport.png_size(data)
    if size is None:
        raise studio.BadRequest("That is not a PNG file.")
    if max(size) > MAX_SIDE:
        raise studio.BadRequest("The PNG is %d x %d pixels; at most %d a side." % (size + (MAX_SIDE,)))
    if data[-8:-4] != b"IEND":
        raise studio.BadRequest("The PNG is cut off (it does not end in IEND).")
    depth, ctype, interlace = data[24], data[25], data[28]
    if depth != 8 or ctype not in (0, 2, 4, 6) or interlace:
        raise studio.BadRequest("Please save the PNG as 8-bit RGB or RGBA without interlacing "
                                "(this one: %d bit, colour type %d%s)." % (depth, ctype, ", interlaced" if interlace else ""))
    try:
        w, h, px = tncskin.read_png(data, alpha=True)
    except (zlib.error, struct.error, ValueError, IndexError, TypeError, TncImageError):
        raise studio.BadRequest("The PNG is damaged or cut off.")
    if len(px) != 4 * w * h:
        raise studio.BadRequest("The PNG is damaged or cut off.")
    return w, h, px


def parse_problem(text, name, original=None):
    """Why `text` does not parse (English, with the line), or None.

    decl.Decl alone accepts 'a = { b = 1;', so: every character a token, the
    braces never below 0 and back to 0 at the end (0 false refusals over every
    readable retail text, re_probes/studioimport/probe_texts.py). *.json: json.loads.
    With `original`, the text must also keep its shape: a value after every '=',
    and a text that is one { ... } block stays one block, so 'x' cannot replace a decl."""
    if name.lower().endswith(".json"):
        try:
            json.loads(text)
        except ValueError as exc:
            return "not valid JSON (%s)" % exc
        return None
    line = lambda at: text.count("\n", 0, at) + 1  # noqa: E731
    try:
        toks = decl.tokenize(text)
    except decl.DeclError as exc:
        m = re.search(r"offset (\d+)", str(exc))
        return "line %d: a character that starts no token (an unclosed quote?)" % line(int(m.group(1)) if m else 0)
    depth = 0
    for t in toks:
        if t.text == "{":
            depth += 1
        elif t.text == "}":
            depth -= 1
            if depth < 0:
                return "line %d: a '}' that closes nothing" % line(t.start)
    if depth:
        return "%d '{' not closed at the end" % depth
    if original is None:
        return None
    for t, nxt in zip(toks, toks[1:] + [None]):
        if t.text == "=" and (nxt is None or nxt.text in ("=", ";", "}")):
            return "line %d: a value is missing after '='" % line(t.start)
    if _one_block(decl.tokenize(original)) and not _one_block(toks):
        return "the game's text is one { ... } block and yours is not"
    return None


def _one_block(toks):
    """The tokens are exactly one { ... } block (the usual shape of a decl)."""
    if not toks or toks[0].text != "{" or toks[-1].text != "}":
        return False
    depth = 0
    for t in toks[:-1]:
        depth += (t.text == "{") - (t.text == "}")
        if depth == 0:
            return False
    return True


def _edits(old, new):
    """{path: value} of the scalars `new` changes in `old`, or None when anything
    else differs (paths added, removed or moved: then only a whole file says it)."""
    try:
        a, b = decl.Decl(old), decl.Decl(new)
    except decl.DeclError:
        return None
    if list(a.values) != list(b.values):
        return None
    return {p: b.get(p) for p in b.paths(decl.SCALAR) if a.get(p) != b.get(p)} or None


# -- the installs, read only -------------------------------------------------------

@contextlib.contextmanager
def _tnc(st):
    root = st.root("tnc")
    oo = oodle.load(root)
    inst = tncpatch.Installation(root / "base", oo=oo)
    try:
        yield inst, oo
    except (TncImageError, tncpatch.TncPatchError, IdclError, OSError) as exc:
        raise studio.Unsupported(str(exc))
    finally:
        inst.close()


@contextlib.contextmanager
def _tno(st):
    inst = TnoInstallation(Game(st.root("tno"), TITLES["tno"]))
    try:
        yield inst
    except (PatchError, ResourceError, BimError, OSError) as exc:
        raise studio.Unsupported(str(exc))
    finally:
        inst.close()


def _listed(st, game, kind, item):
    if item not in st.catalog(game, kind).extra:
        raise studio.Missing("Not found: %s %s" % (kind[:-1], item))


def _tnc_image(inst, oo, name):
    try:
        return tncskin.check_image(inst, name, oo, normal=None)
    except TncImageError as exc:
        raise studio.Unsupported(str(exc))


def material_image(st, mid, material):
    """The image name of `material`'s colour map, a material of model `mid` (as maptex finds it)."""
    info = st.info("tnc", studio.MODELS, mid)
    if material not in {s["material"] for s in info["surfaces"]}:
        raise studio.BadRequest("%s is not a material of this model." % material)
    mount = st.catalog("tnc", studio.MODELS).mount
    hit = mount.find("material", material)
    if hit is None:
        raise studio.Unsupported("Material %s is in no archive." % material)
    slots = dict(line.split("\t", 1) for line in
                 mount.read(*hit).decode("utf-8", "replace").splitlines() if "\t" in line)
    for key, type_ in maptex.ALBEDO_KEYS:
        value = slots.get(key, "").strip().strip('"')
        if value and mount.find(type_, value) is not None:
            if type_ != "image":
                raise studio.Unsupported("This material's colour map is a decal atlas, not a texture: not writable.")
            return value
    raise studio.Unsupported("This material has no colour map to replace.")


def _png_info(info):
    notes = []
    group = [n for n in info["group"] if n != info["name"]]
    if group:
        notes.append("%d other texture(s) share this one's data and change with it: %s"
                     % (len(group), ", ".join(n.split("/")[-1] for n in group[:5]) + (" ..." if len(group) > 5 else "")))
    top = info["mips"][0]
    if top:
        notes.append("The game ships this texture from mip %d down; the %d larger level(s) stay as they are."
                     % (top, top))
    notes += ["Not included: %s" % why for why in info["blood_skipped"]]
    if info["format"] == "BC5":
        notes.append("A normal map: the PNG must be exactly %d x %d, green pointing down (DirectX). "
                     "From Blender or another OpenGL tool, invert the green channel first."
                     % (info["width"], info["height"]))
    return {"form": "png", "image": info["name"], "group": group, "blood": [b["name"] for b in info["blood"]],
            "target": {"width": info["width"], "height": info["height"], "format": info["format"],
                       "mips": len(info["mips"])}, "notes": notes}


def _tno_image(st, item):
    """(BIM, bytes) of a TNO texture the loader can write, or Unsupported."""
    a, e = st.catalog("tno", "textures").extra[item]
    data = st._read("tno", a, e)
    try:
        bim = parse_bim(data)
    except BimError as exc:
        raise studio.Unsupported("Not readable as an image: %s" % exc)
    if bim.format_code == image.A8:
        raise studio.Unsupported("An A8 distance-field (font) texture: how its channel maps to RGBA8 "
                                 "is unknown, so it is not replaced.")
    if bim.format_code not in (image.RGBA8, image.BC1, image.BC3):
        raise studio.Unsupported("Image format %d is not known." % bim.format_code)
    with _tno(st) as inst:
        if not inst.find("image", item):
            raise studio.Unsupported("The loader does not find this texture in the archives it writes.")
    return bim, data


def _refuse_modded(inst, item):
    """The archives hold the modded text while a mod changes it: a replacement made
    from it would carry that mod's changes into the new one."""
    # ponytail: refuse instead of reading the pristine copy from the backup; add a pristine read if users hit this often
    if item in ((inst.read_journal() or {}).get("assets") or []):
        raise studio.Unsupported("An applied mod changes this text right now. Reset the mods in the loader "
                                 "first, then replace it here - otherwise that mod's changes would end up in yours.")


def _text_source(st, game, item):
    """(entry, payload, (text, encoding, tail), wrapped) of a text as the loader will read it."""
    _listed(st, game, "texts", item)
    type_, name = item.split(":", 1)
    if game == "tnc":
        with _tnc(st) as (inst, _oo):
            hits = inst.find(type_, name)
            if not hits:
                raise studio.Unsupported("This text ships only in a DLC archive; the loader writes base/ only.")
            if any(e.compression == COMP_KRAKEN_BLOCKS for _a, e in hits):
                raise studio.Unsupported("Stored in archive mode 4, which the loader does not write.")
            _refuse_modded(inst, item)
            entry, payload = hits[0][1], inst.read(type_, name)
    else:
        with _tno(st) as inst:
            hits = inst.find(type_, name)
            if not hits:
                raise studio.Unsupported("The loader does not find this text in the archives it writes.")
            _refuse_modded(inst, item)
            entry, payload = hits[0][1], inst.read(type_, name)
    wrapped = game == "tnc" and entry.type in tncview.WRAPPED
    try:
        inner = tncview.unwrap(entry, payload)[0] if wrapped else payload
    except tnccrypt.TncCryptError as exc:
        raise studio.Unsupported("%s cannot be opened: %s" % (name, exc))
    got = tncview.as_text(inner)
    if got is None:
        raise studio.Unsupported("Binary data, not text: it cannot be replaced as a text file.")
    return entry, payload, got, wrapped


# -- what a replacement writes -------------------------------------------------------

def replace_info(st, game, kind, item, skin=None):
    """What replacing `item` writes ({ok: True, form, target, group, blood, notes}),
    or why it cannot be replaced ({ok: False, reason})."""
    try:
        out = _info(st, game, kind, item, skin)
    except studio.Unsupported as exc:
        return {"ok": False, "reason": studio._english(str(exc)), "form": None}
    return dict(out, ok=True, reason=None)


def _info(st, game, kind, item, skin):
    st.root(game)
    if kind in ("sounds", "videos"):
        _listed(st, game, kind, item)
        if (game, kind) in REFUSED:
            raise studio.Unsupported(REFUSED[(game, kind)])
        return _sound_info(st, item) if kind == "sounds" else _video_info(st, item)
    if kind == "texts":
        _entry, _payload, (text, enc, _tail), wrapped = _text_source(st, game, item)
        return {"form": "text", "target": {"lines": text.count("\n") + 1, "encoding": enc},
                "notes": ["Saved as the whole file, encrypted or packed again for its name."] if wrapped else []}
    if kind == "models":
        if game != "tnc":
            st.catalog(game, kind)          # TNO: Unsupported with the Studio's reason
        if not skin:
            raise studio.BadRequest("skin= names the material whose colour map is replaced.")
        name = material_image(st, item, skin)
        with _tnc(st) as (inst, oo):
            return _png_info(_tnc_image(inst, oo, name))
    if kind != "textures":
        raise studio.Missing("Unknown kind: %s" % kind)
    _listed(st, game, kind, item)
    if game == "tnc":
        with _tnc(st) as (inst, oo):
            return _png_info(_tnc_image(inst, oo, item))
    bim, _data = _tno_image(st, item)
    return {"form": "png", "image": item, "group": [], "blood": [],
            "target": {"width": bim.width, "height": bim.height, "format": "RGBA8", "mips": 1},
            "notes": ["Stored uncompressed (RGBA8, %.1f MB, the old texture path of The New Order); "
                      "the loader rebuilds the archive holding it when you apply." % (bim.width * bim.height * 4 / 1048576)]}


def build(st, game, kind, item, body, blood=True, skin=None):
    """({asset key: ("file", bytes) | ("set", {path: value})}, form, notes). Writes nothing.
    The target is checked before the file, the file before anything is encoded."""
    if kind == "texts":
        return _text_assets(st, game, item, body)
    if kind in ("sounds", "videos") and (game, kind) not in REFUSED:
        _listed(st, game, kind, item)
        return (_sound_assets if kind == "sounds" else _video_assets)(st, item, body)
    if kind not in ("textures", "models") or game != "tnc":
        _info(st, game, kind, item, skin)           # refused sounds and videos, TNO models: raises with the reason
    notes = []
    if game == "tno":
        bim, _data = _tno_image(st, item)
        w, h, px = check_png(body)
        if (w, h) != (bim.width, bim.height):
            px = tncskin.resize(w, h, px, bim.width, bim.height)
            notes.append("Scaled from %d x %d to %d x %d." % (w, h, bim.width, bim.height))
        return {"image:" + item: ("file", image.to_bim(bim.width, bim.height, px, template=bim))}, "png", notes
    if kind == "models":
        if not skin:
            raise studio.BadRequest("skin= names the material whose colour map is replaced.")
        name = material_image(st, item, skin)
    else:
        _listed(st, game, kind, item)
        name = item
    with _tnc(st) as (inst, oo):
        info = _tnc_image(inst, oo, name)
        w, h, px = check_png(body)
        if info["format"] != "BC5" and px[3::4] != b"\xff" * (w * h):
            notes.append("Its transparency is dropped: BC1 stores none.")
            px = bytearray(px)
            px[3::4] = b"\xff" * (w * h)
        if (w, h) != (info["width"], info["height"]):
            notes.append("Scaled from %d x %d to %d x %d, %d mip levels, %s."
                         % (w, h, info["width"], info["height"], len(info["mips"]), info["format"]))
        got = tncskin.image_assets(inst, info, (w, h, bytes(px)), oo, blood)
    return {k: ("file", v) for k, v in got.items()}, "png", notes


def _text_assets(st, game, item, body):
    if len(body) > MAX_TEXT:
        raise studio.BadRequest("The text is too large (at most %d MB)." % (MAX_TEXT >> 20))
    entry, payload, (old, enc, tail), wrapped = _text_source(st, game, item)
    try:
        new = body.decode("utf-8")
    except UnicodeDecodeError:
        raise studio.BadRequest("The file is not UTF-8 text.")
    new = new[1:] if new.startswith("﻿") else new
    if new == old:
        raise studio.BadRequest("Nothing changed: the file equals the game's text.")
    why = parse_problem(new, item.split(":", 1)[1], old)
    if why:
        raise studio.BadRequest("The text does not parse: %s. Nothing was saved." % why)
    try:
        data = tncview.from_text(new, enc, tail)
    except tnccrypt.TncCryptError:
        raise studio.BadRequest("A character does not fit this file's encoding (%s)." % enc)
    if wrapped:
        return ({item: ("file", tncview.wrap(entry, data))}, "file",
                ["Saved as the whole file, encrypted or packed again for its name."])
    edits = _edits(old, new)
    if edits and enc == "utf-8":
        try:        # exactly what build_payloads will do with it
            if decl.apply_patch(payload.decode("utf-8"), edits).encode("utf-8") == data:
                return {item: ("set", edits)}, "set", ["Saved as %d value change(s): %s"
                                                       % (len(edits), ", ".join(sorted(edits)[:5]))]
        except (decl.DeclError, UnicodeDecodeError):
            pass
    return {item: ("file", data)}, "file", ["Saved as the whole file."]


# -- sounds and videos (TNC): whole media, see tncpatch's "Whole files" -------------------

def _refuse_applied(st, key):
    """Unsupported while an applied mod replaces this medium: the limits would be that mod's."""
    try:
        journal = tncpatch.Installation(st.root("tnc") / "base").read_journal() or {}
    except tncpatch.TncPatchError as exc:
        raise studio.Unsupported(str(exc))
    if key in (journal.get("assets") or []):
        raise studio.Unsupported("An applied mod replaces this right now. Reset the mods in the loader first, "
                                 "then replace it here - the limits come from the game's original.")


def _sound_plan(st, item, wem):
    """{pack path: changes} the loader will write for `wem` (wwise.replace_plan), or Unsupported."""
    _refuse_applied(st, "%s:%s" % (tncpatch.WEM, item))
    try:
        index, _ = wwise._index(str(st.root("tnc")))
        wwise.check_replaceable(index.get(item, []), index, item)
        return wwise.replace_plan(st.root("tnc"), {item: wem})
    except (wwise.WwiseError, IdclError, OSError, struct.error) as exc:
        raise studio.Unsupported(str(exc))


def _packs_note(plan):
    mb = sum(p.stat().st_size for p in plan) / 1048576
    return ("Applying rewrites %d sound pack(s), %s MB: %s. The originals stay as second names of "
            "the files until you reset." % (len(plan), format(round(mb), ","), ", ".join(sorted({p.name for p in plan}))))


def _sound_info(st, item):
    r = st.catalog("tnc", "sounds").extra[item]
    ch, rate = r["channels"], r["sample_rate"]
    plan = _sound_plan(st, item, wwise.pcm_wem(ch, rate, bytes(2 * ch)))   # the packs do not depend on the bytes
    return {"form": "wav", "target": {"channels": ch, "sample_rate": rate, "seconds": round(r["duration"], 3),
                                      "codec": r["codec"]},
            "notes": ["A 16-bit PCM WAV with %d channel(s), at most %.2f s long (the game's sound), any sample rate."
                      % (ch, r["duration"]),
                      "Stored uncompressed (about %.0f MB a minute at 48 kHz): the game's own sounds are Wwise Vorbis, "
                      "which cannot be encoded here; the game's other codec, PCM, can." % (5.76 * ch),
                      _packs_note(plan)]}


def _sound_assets(st, item, body):
    if len(body) > MAX_WAV:
        raise studio.BadRequest("The WAV is too large (at most %d MB)." % (MAX_WAV >> 20))
    r = st.catalog("tnc", "sounds").extra[item]
    # the target is refused before the file is judged; the packs do not depend on the bytes
    plan = _sound_plan(st, item, wwise.pcm_wem(r["channels"], r["sample_rate"], bytes(2 * r["channels"])))
    try:
        ch, rate, pcm = wwise.parse_wav(body)
    except wwise.WwiseError as exc:
        raise studio.BadRequest(str(exc))
    if ch != r["channels"]:
        raise studio.BadRequest("Your WAV has %d channel(s), the game's sound %d. Convert it to %d channel(s); "
                                "nothing was saved." % (ch, r["channels"], r["channels"]))
    seconds = len(pcm) / (2 * ch) / rate
    if seconds > r["duration"] + LONGER:
        raise studio.BadRequest("Your WAV is %.2f s long, the game's sound %.2f s: a replacement may be at most "
                                "that long. Trim it; nothing was saved." % (seconds, r["duration"]))
    wem = wwise.pcm_wem(ch, rate, pcm, r.get("channel_mask"))
    return ({"%s:%s" % (tncpatch.WEM, item): ("file", wem)}, "wav",
            ["Saved as a 16-bit PCM WEM: %d channel(s), %d Hz, %.2f s, %.1f MB." % (ch, rate, seconds, len(wem) / 1048576),
             _packs_note(plan)])


def _video_source(st, item):
    path, h = st.catalog("tnc", "videos").extra[item]
    if "error" in h:
        raise studio.Unsupported("The game's video is not readable: %s" % h["error"])
    _refuse_applied(st, "%s:%s" % (tncpatch.VIDEO, item))
    return path, h


def _video_info(st, item):
    _path, h = _video_source(st, item)
    fps = h["fps_num"] / h["fps_den"]
    notes = ["Made with RAD's Bink 2 tools: %d x %d, %.3f fps, at most %d frames (%.2f s). The file is checked "
             "and opened with the game's own Bink DLL, not converted." % (h["width"], h["height"], fps, h["frames"],
                                                                          h["frames"] / fps)]
    if cutsceneaudio.languages(st.root("tnc"), PurePosixPath(item).stem):
        notes.append("The game plays this video's sound from its sound banks (Wwise event play_%s), timed to "
                     "the original; a replacement's own audio tracks are not played." % PurePosixPath(item).stem.lower())
    return {"form": "bink", "target": {"width": h["width"], "height": h["height"], "fps": round(fps, 3),
                                       "frames": h["frames"], "seconds": round(h["frames"] / fps, 3)},
            "notes": notes}


def _video_assets(st, item, body):
    if len(body) > MAX_VIDEO:
        raise studio.BadRequest("The video is too large (at most %d MB)." % (MAX_VIDEO >> 20))
    _path, h = _video_source(st, item)
    try:
        new = bink.header_of(body, "Your file")
    except bink.BinkError as exc:
        raise studio.BadRequest("%s. Nothing was saved." % exc)
    want, got = (h["width"], h["height"]), (new["width"], new["height"])
    if got != want:
        raise studio.BadRequest("Your video is %d x %d, the game's %d x %d: make it the same size; nothing was saved."
                                % (got + want))
    fps, mine = h["fps_num"] / h["fps_den"], new["fps_num"] / new["fps_den"]
    if abs(mine - fps) > fps * FPS_SLACK:
        raise studio.BadRequest("Your video runs at %.3f fps, the game's at %.3f fps: make it the same frame rate; "
                                "nothing was saved." % (mine, fps))
    if new["frames"] > h["frames"]:
        raise studio.BadRequest("Your video has %d frames (%.2f s), the game's %d (%.2f s): a replacement may be at "
                                "most that long. Shorten it; nothing was saved."
                                % (new["frames"], new["frames"] / mine, h["frames"], h["frames"] / fps))
    notes = ["%d x %d, %.3f fps, %d frames, %.1f MB." % (got + (mine, new["frames"], len(body) / 1048576))]
    dll = st.root("tnc") / bink.DLL_NAME
    if dll.is_file():
        with tempfile.TemporaryDirectory(prefix="wolfsdk_video_") as tmp:
            probe = Path(tmp) / "check.bk2"
            probe.write_bytes(body)
            try:
                with bink.open(probe, dll) as v:
                    v.frame(0)
            except bink.BinkError as exc:
                raise studio.BadRequest("The game's Bink DLL cannot play your video (%s); nothing was saved." % exc)
        notes.append("The game's own Bink DLL opened it and decoded its first frame.")
    else:
        notes.append("Not test-decoded: no %s next to the game." % bink.DLL_NAME)
    if new["tracks"]:
        notes.append("Its %d audio track(s) are kept in the file; whether the game plays them was not tested."
                     % len(new["tracks"]))
    return {"%s:%s" % (tncpatch.VIDEO, item): ("file", body)}, "bink", notes


# -- mod folders ------------------------------------------------------------------------

def studio_mods(mods_dir, game):
    """[{folder, id, name, author, assets}] of the mods the Studio made for `game`."""
    out = []
    for p in sorted(Path(mods_dir).glob("*/" + modlib.MANIFEST)):
        try:
            d = json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and MARK in d and d.get("spiel", "tno") == game:
            out.append({"folder": p.parent.name, "id": d.get("id"), "name": d.get("name"),
                        "author": d.get("author", ""), "assets": len(d.get("assets") or {})})
    return out


def _asset_path(key):
    type_, name = key.split(":", 1)
    if type_ == tncpatch.TEXDB:
        return "texdb/%s.bin" % name
    stem = re.sub(r"[^a-z0-9]+", "_", name.split("$", 1)[0].lower())[-50:].strip("_") or "asset"
    ext = ".bimage" if type_ == "image" else ".bin" if type_ in tncview.WRAPPED + (kiscule.TYPE,) else ".txt"
    ext = {tncpatch.WEM: ".wem", tncpatch.VIDEO: PurePosixPath(name).suffix.lower()}.get(type_, ext)
    return "assets/%s_%s%s" % (stem, hashlib.sha1(key.encode("utf-8")).hexdigest()[:8], ext)


def _describe(assets):
    count = lambda *types: sum(1 for k in assets if k.split(":", 1)[0] in types)  # noqa: E731
    images, scripts = count("image"), count(kiscule.TYPE)
    sounds, videos = count(tncpatch.WEM), count(tncpatch.VIDEO)
    texts = len(assets) - images - scripts - sounds - videos - count(tncpatch.TEXDB)
    parts = ["%d texture(s)" % images if images else "", "%d text(s)" % texts if texts else "",
             "%d script(s)" % scripts if scripts else "", "%d sound(s)" % sounds if sounds else "",
             "%d video(s)" % videos if videos else ""]
    return ("Made in Wolfenstein Studio: %s replaced. Tick it in the loader under Mods and apply. "
            "Structurally checked, not confirmed in game." % " and ".join(p for p in parts if p))


def save(mods_dir, game, assets, name=None, folder=None, author=""):
    """The Mod after writing `assets` ({key: ("file", bytes) | ("set", dict)}) into a new
    mod `name` or the Studio mod in `folder`. A new "file" replaces the entry, a "set"
    merges into it. Files first, then mod.json (tmp + replace), then Mod.load checks it."""
    mods_dir = Path(mods_dir)
    author = check_author(author)
    with _lock:
        if folder:
            if folder not in {m["folder"] for m in studio_mods(mods_dir, game)}:
                raise studio.BadRequest("There is no Studio mod of this game in folder %r." % folder)
            root, created = mods_dir / folder, False
            manifest = json.loads((root / modlib.MANIFEST).read_text("utf-8"))
        else:
            name = check_name(name)
            root, created = mods_dir / slug(name), True
            if root.exists():
                raise studio.BadRequest("A mod folder %s already exists. Pick another name, or add to "
                                        "one of your Studio mods instead." % root.name)
            manifest = {"id": "studio." + root.name, "name": name, "version": "1.0.0", "author": author,
                        "description": "", "priority": 100, "spiel": game, MARK: {"version": 1}, "assets": {}}
            mods_dir.mkdir(parents=True, exist_ok=True)
            root.mkdir()
        try:
            got = manifest.setdefault("assets", {})
            for key, (form, value) in assets.items():
                if form == "set":
                    old = got.get(key, {})
                    got[key] = dict(old, set=dict(old.get("set") or {}, **value))
                    continue
                rel = _asset_path(key)
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_bytes(value)
                got[key] = {"file": rel}
            manifest["description"] = _describe(got)
            tmp = root / (modlib.MANIFEST + ".tmp")
            tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", "utf-8")
            os.replace(tmp, root / modlib.MANIFEST)
            return modlib.Mod.load(root)
        except BaseException:
            if created:
                shutil.rmtree(root, ignore_errors=True)
            raise


def _clashes(mods_dir, game, mod):
    """English notes: other mods of this game writing one of `mod`'s textures (the loader refuses both)."""
    mine = {k.lower() for k in mod.assets if k.split(":", 1)[0] in modlib.EXCLUSIVE_TYPES}
    found, _errors = modlib.discover(mods_dir)
    out = []
    for other in modlib.for_title(found, game):
        both = mine & {k.lower() for k in other.assets}
        if other.id != mod.id and both:
            out.append("Mod '%s' also replaces %d of these texture assets: tick only one of the two."
                       % (other.name, len(both)))
    return out


def _check_target(mods_dir, game, name, folder, author):
    """BadRequest unless exactly one of a new mod's name and a Studio mod's folder is given and usable."""
    if bool(name) == bool(folder):
        raise studio.BadRequest("Name a new mod (name=) or pick one of your Studio mods (folder=), one of the two.")
    check_author(author)
    if name:
        check_name(name)
        if (Path(mods_dir) / slug(name)).exists():
            raise studio.BadRequest("A mod folder %s already exists. Pick another name, or add to one of "
                                    "your Studio mods instead." % slug(name))
    elif folder not in {m["folder"] for m in studio_mods(mods_dir, game)}:
        raise studio.BadRequest("There is no Studio mod of this game in folder %r." % folder)


def replace(st, game, kind, item, body, name=None, folder=None, author="", blood=True, skin=None, mods_dir=MODS):
    """Build the replacement of `item` from `body` and save it as a mod (see the module docstring)."""
    _check_target(mods_dir, game, name, folder, author)
    assets, form, notes = build(st, game, kind, item, body, blood, skin)
    mod = save(mods_dir, game, assets, name, folder, author)
    return {"mod": mod.id, "folder": mod.root.name, "name": mod.name, "assets": sorted(assets), "form": form,
            "notes": notes + _clashes(mods_dir, game, mod), "message": MESSAGE % mod.name}


# -- scripts -------------------------------------------------------------------------------

def mod_script(mods_dir, game, folder, item):
    """The payload of script `item` in the Studio mod in `folder`, None when that mod does not change it."""
    if folder not in {m["folder"] for m in studio_mods(mods_dir, game)}:
        raise studio.BadRequest("There is no Studio mod of this game in folder %r." % folder)
    key = "%s:%s" % (kiscule.TYPE, item)
    try:
        m = modlib.Mod.load(Path(mods_dir) / folder)
        return m.content_for(key) if (m.assets.get(key) or {}).get("file") else None
    except modlib.ModError as exc:
        raise studio.Unsupported(str(exc))


def script_info(st, game, item, folder=None, mods_dir=MODS):
    """GET scripts/info: the script as the loader reads it from the game or, with `folder`, as that
    Studio mod has it (`mod`: the folder or None), plus `mods`: [{folder, name}] of the Studio mods
    that change it."""
    out = dict(st.info(game, studio.SCRIPTS, item), mod=None, mods=[])
    key = "%s:%s" % (kiscule.TYPE, item)
    for m in studio_mods(mods_dir, game):
        try:
            if (modlib.Mod.load(Path(mods_dir) / m["folder"]).assets.get(key) or {}).get("file"):
                out["mods"].append({"folder": m["folder"], "name": m["name"]})
        except modlib.ModError:
            pass                                # a broken mod folder is not this script's problem
    if folder:
        payload = mod_script(mods_dir, game, folder, item)
        if payload is None:
            raise studio.Missing("The Studio mod in folder %s does not change this script." % folder)
        try:
            graph = kiscule.Graph(kiscule.Doc(kiscule.unpack(payload, oodle.load(st.root(game)))))
        except (kiscule.KisculeError, oodle.OodleError, ValueError) as exc:
            raise studio.Unsupported("The mod's copy of this script is not readable: %s" % exc)
        out.update(kiscule.view(graph), mod=folder)
    return out


def script_assets(st, game, item, ops, base=None):
    """({asset key: ("file", payload)}, {op id: new node id}) of Kiscule edit ops on script `item`.
    Writes nothing: the target is checked first, the ops are applied and validated on a parsed copy.

    Which copy the ops go onto: `base` (a Studio mod's own copy) when given, else the game's as the
    loader reads it from the archives. Only the game's copy can carry another mod's changes -- while
    a mod is applied the archives hold its version -- so only then does an applied mod changing the
    script refuse the edit (409). A Studio mod's copy was made from a clean game copy and holds only
    Studio edits; with it the archives are not read for the script, whatever is applied."""
    cat = st.catalog(game, studio.SCRIPTS)          # TNO: Unsupported with the Studio's reason
    if item not in cat.extra:
        raise studio.Missing("Not found: script %s" % item)
    dlc = "This script ships only in a DLC archive; the loader writes base/ only."
    if not cat.by_id[item]["editable"]:
        raise studio.Unsupported(dlc)
    key = "%s:%s" % (kiscule.TYPE, item)
    with _tnc(st) as (inst, oo):
        hits = inst.find(kiscule.TYPE, item)
        if not hits:
            raise studio.Unsupported(dlc)
        if any(e.compression == COMP_KRAKEN_BLOCKS for _a, e in hits):
            raise studio.Unsupported("Stored in archive mode 4, which the loader does not write.")
        if base is None and key in ((inst.read_journal() or {}).get("assets") or []):
            raise studio.Conflict("An applied mod changes this script right now. Reset the mods in the loader "
                                  "first, then edit it here - otherwise that mod's changes would end up in yours.")
        payload = inst.read(kiscule.TYPE, item) if base is None else base
        try:
            old = kiscule.unpack(payload, oo)
            doc = kiscule.Doc(old)
            added = kiscule.apply_ops(doc, ops, seed=item)
            new = doc.render()
            if new == old:
                raise studio.BadRequest("The edits change nothing: every value already is what you set.")
            return {key: ("file", kiscule.pack(new, oo))}, added
        except kiscule.EditError as exc:
            raise studio.BadRequest(str(exc))
        except kiscule.KisculeError as exc:
            raise studio.Unsupported("%s cannot be edited: %s" % (item, exc))


def save_script(st, game, item, ops, name=None, folder=None, author="", mods_dir=MODS):
    """Apply `ops` to a script and save it as a mod (new `name` or Studio mod `folder`). POST scripts/save.
    Into a Studio mod that changes the script already, the ops go onto its copy (`base`: the folder),
    so edits accumulate; else onto the game's (`base`: None)."""
    _check_target(mods_dir, game, name, folder, author)
    # ponytail: the base is read outside save()'s lock; two saves racing into one folder keep only one edit
    base = mod_script(mods_dir, game, folder, item) if folder else None
    assets, added = script_assets(st, game, item, ops, base)
    mod = save(mods_dir, game, assets, name, folder, author)
    return {"mod": mod.id, "folder": mod.root.name, "name": mod.name, "assets": sorted(assets), "added": added,
            "base": folder if base is not None else None, "message": MESSAGE % mod.name}
