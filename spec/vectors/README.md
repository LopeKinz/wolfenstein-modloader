# Shared test vectors

Normative inputs and expected outputs for every SDK binding (SPEC §12).
Regenerate with `python spec/vectors/generate.py`. The generator is
deterministic for a given zlib build; a different zlib may produce different
compressed bytes (and therefore different offsets in `game/`). The committed
files are normative — regenerate only on purpose and commit the result.

| File | Checked by |
|---|---|
| `decl.json`, `decl/` | parse (`paths`, block paths), edits → `edit*.out`, parse/set/path errors, path quoting |
| `format.json` | `format(node, value)` |
| `bim.json`, `bim/`, `png/` | BIM header + decoded RGBA sha256, RGBA8 encode, PNG decode (all filters, RGB/grey/grey+alpha, split IDAT) |
| `audio.json`, `audio/` | `bsnf` (mono + 4 languages), Ogg page walk, container selection |
| `archive.json`, `game/` | master index, chunk entries (all fields + payload sha256), `find`, in-place writes, rebuild |
| `mod.json`, `mod/` | layering results, conflicts, errors, invalid mod files |
| `save.json` | save-game checksum |
| `compression.json`, `compression/` | `deflateExact` invariants (sizes relative to the binding's own `deflateSync`) |

`game/` is a tiny synthetic *Wolfenstein: The New Order* installation; it
contains no game data. Bindings MUST run write tests on a temporary copy.
