"""Wolfenstein II (TNC) entries in the asset browser: where they are, what to
show, and how an edit goes back in.

Three layers sit between an IDCL entry and something a person can read:

  1. the archive's own compression (idcl mode 0 / 2 / 4, see payload()),
  2. for `cfile` and `compfile` a second wrapper inside the payload
     (wolfsdk/tnccrypt.py: AES + HMAC keyed by the entry name, or Kraken),
  3. for `image` the .bimage mip table plus the .texdb blocks
     (wolfsdk/tncimage.py).

The DLC folders (<game>/dlc/dlc_N/base/*.resources) are listed too, but only
for reading: the mod pipeline (wolfsdk/tncpatch.py) writes base/ and nothing
else, so a change saved from a DLC row would go nowhere.
"""

import re
import struct
from pathlib import Path

from . import tnccrypt, tncimage
from .idcl import Archive, COMP_KRAKEN, COMP_KRAKEN_BLOCKS, COMP_STORED, IdclError
from .oodle import OodleError

# Constant in all 25 mode-4 payloads of the retail game (tools/extract_tnc.py
# measured it); the Kraken frame starts right behind it.
BLOCK_PREFIX = bytes.fromhex("010000000000000100008000")
WRAPPED = ("cfile", "compfile")
# C0 controls other than tab/newline/CR: binary, not text. DEL as well.
# Matched on the bytes: in UTF-8 and latin-1 alike these characters are
# exactly these bytes, and a binary payload is turned away at its first one
# instead of after decoding all of it.
_CONTROL = re.compile(b"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# -- archives -----------------------------------------------------------------

def dlc_archives(base):
    """Every <game>/dlc/*/base/*.resources beside `base`, table-only."""
    root = Path(base).parent
    return [Archive(p) for p in sorted(root.glob("dlc/*/base/*.resources"))]


def read_only(archive, base):
    """True for an archive outside base/ -- the patcher never writes there."""
    return archive.path.parent != Path(base)


def where(archive, base):
    """Short archive name for the info line."""
    if not read_only(archive, base):
        return archive.path.name
    return "%s/%s (DLC, read-only)" % (archive.path.parent.parent.name, archive.path.name)


def payload(archive, entry, oo):
    """The entry's bytes behind the archive's compression, exactly usize long."""
    raw = archive.read_raw(entry)
    if entry.compression == COMP_STORED:
        return raw
    if entry.compression == COMP_KRAKEN_BLOCKS:
        if raw[:len(BLOCK_PREFIX)] != BLOCK_PREFIX:
            raise IdclError("%s: mode 4 without the known header (%s)"
                            % (entry.name, raw[:12].hex()))
        raw = raw[len(BLOCK_PREFIX):]
    elif entry.compression != COMP_KRAKEN:
        raise IdclError("%s: unknown compression mode %d" % (entry.name, entry.compression))
    try:
        return oo.decompress(raw, entry.usize, fuzz_safe=True)
    except OodleError as exc:
        raise IdclError("%s: %s" % (entry.name, exc))


# -- the second wrapper -------------------------------------------------------

def unwrap(entry, data):
    """(inner bytes, what was done). Plain entries come back as they are."""
    if entry.type == "cfile":
        packed = struct.unpack_from("<i", data)[0] > 0 if len(data) >= 4 else False
        return (tnccrypt.cfile_decrypt(data, entry.name),
                "decrypted and unpacked" if packed else "decrypted")
    if entry.type == "compfile":
        return tnccrypt.compfile_unwrap(data), "unpacked"
    return data, ""


def wrap(entry, inner):
    """Inverse of unwrap(): the payload a mod has to carry for this entry.

    A cfile is bound to its resource name, so it is always encrypted under
    entry.name -- under any other name the game's HMAC check rejects it.
    The result is read back the way the preview reads it before it is handed
    out: a mod that does not open again is never written.
    """
    if entry.type == "cfile":
        out = tnccrypt.cfile_encrypt(inner, entry.name)
    elif entry.type == "compfile":
        out = tnccrypt.compfile_wrap(inner)
    else:
        return bytes(inner)
    if unwrap(entry, out)[0] != bytes(inner):
        raise tnccrypt.TncCryptError("%s: does not read back after re-wrapping" % entry.name)
    return out


def for_import(entry, data):
    """A file picked in 'Importieren' as the payload to store.

    For cfile/compfile only two kinds of file are right:
      - the stored form of exactly this entry (it unwraps): kept as it is;
      - readable text, what a text export writes: wrapped for this entry.
    Anything else is refused: the stored form of another resource (a cfile
    opens only under its own name), a stored form that cannot be opened
    here (Oodle or AES missing), other binary data. Wrapping those would
    ship bytes the game reads as garbage.
    """
    if entry.type not in WRAPPED:
        return data
    try:
        unwrap(entry, data)
        return data
    except tnccrypt.TncCryptError as exc:
        why = exc
    if as_text(data) is None:
        hint = ("A cfile only opens under its own resource name; the .raw "
                "of another entry never fits here.\n" if entry.type == "cfile" else "")
        raise tnccrypt.TncCryptError(
            "The file is not text and does not open as %s %s:\n%s\n\n%s"
            "To replace it, import the exported plain text." % (entry.type, entry.name, why, hint))
    return wrap(entry, data)


# -- text -----------------------------------------------------------------------

def as_text(data):
    """(text, encoding, tail) when `data` is text a Tk Text widget keeps intact.

    Every retail .cfg ends in one NUL byte and Tk drops NULs, so trailing NULs
    are held back as `tail` and re-appended by from_text(). Eight retail
    .entities are not valid UTF-8 (c09p1: ASCII plus a stray 0xA8); those
    fall back to latin-1, which maps every byte to one character and back.
    None for anything binary.
    default_mp.cfg is a lone NUL and comes out as empty, editable text.
    """
    body = bytes(data).rstrip(b"\0")
    if _CONTROL.search(body):
        return None
    try:
        text, encoding = body.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = body.decode("latin-1"), "latin-1"
    return text, encoding, len(data) - len(body)


def from_text(text, encoding, tail):
    """The bytes as_text() came from, with `text` possibly edited."""
    try:
        return text.encode(encoding) + b"\0" * tail
    except UnicodeEncodeError as exc:
        raise tnccrypt.TncCryptError("Character %r does not fit this file's encoding %s"
                                     % (exc.object[exc.start:exc.end], encoding))


# -- one entry -----------------------------------------------------------------

def present(entry, data, texdbs, max_side=512):
    """Everything the browser shows for one TNC entry, as a dict:

      inner     bytes for the Text tab (decrypted/unpacked, or `data`)
      note      "decrypted" / "unpacked" / why unwrapping failed / ""
      unwrapped True when inner came out of a cfile/compfile wrapper
      text      as_text(inner); None for binary and for a cfile/compfile
                that did not unwrap -- only this goes through the Text tab
                for saving
      png, image_info, image_error   for an `image` entry
    """
    view = {"inner": data, "note": "", "unwrapped": False, "text": None,
            "png": None, "image_info": "", "image_error": ""}
    if entry.type in WRAPPED:
        try:
            view["inner"], view["note"] = unwrap(entry, data)
            view["unwrapped"] = True
        except tnccrypt.TncCryptError as exc:
            view["note"] = "not readable: %s" % exc
    if view["unwrapped"] or entry.type not in WRAPPED:
        view["text"] = as_text(view["inner"])
    if entry.type == "image":
        try:
            view["png"], view["image_info"] = tncimage.to_png(entry, data, texdbs, max_side)
        except Exception as exc:  # noqa: BLE001 -- a broken user DLC must not take the tab down
            # tncimage raises TncImageError for everything it recognises; a
            # few malformed inputs still escape as ValueError/IndexError/
            # KeyError (see re_probes/agent_reports/assetviewer.md)
            view["image_error"] = str(exc) if isinstance(exc, tncimage.TncImageError) \
                else "Image data broken (%s: %s)" % (type(exc).__name__, exc)
    return view
