# Wolfenstein SDK — Language-Neutral Specification

Version: **0.1.0**

This document is the contract every language binding of the Wolfenstein SDK
(`sdk/python`, `sdk/typescript`, `sdk/rust`, `sdk/go`) implements. It is
derived from the reverse-engineering results published in the
[project wiki](https://github.com/LopeKinz/wolfenstein-modloader/wiki); section
references such as *File-Formats §2* point there.

The shared test vectors in [`vectors/`](vectors/) are normative: every binding
runs them in its test suite. When prose and vectors disagree, the vectors win
and the prose is a bug.

Keywords: **MUST**, **SHOULD**, **MAY** as in RFC 2119. `BE`/`LE` = big/little
endian. Offsets are in bytes and zero-based.

Scope of 0.1.0: *Wolfenstein: The New Order* (id Tech 5) archives, the Decl
text format of both games, BIM images, audio descriptors, save-game checksums
and the `mod.json` format. The id Tech 6 `IDCL` container of *The New
Colossus* (Oodle-compressed) is **out of scope**.

---

## 1. Module overview

| Module | Purpose | §  |
|---|---|---|
| `masterindex` | Parse `base/master.index` | 2 |
| `chunkindex` | Parse and patch `base/chunkN.index` | 3 |
| `compression` | Raw DEFLATE with sync flush, exact-size packing | 4 |
| `archive` | Game directory: find, read, patch in place, rebuild | 5 |
| `decl` | Decl parser / span-preserving editor | 6 |
| `mod` | `mod.json` loading, layering and conflict detection | 7 |
| `bim` | BIM image header, decode to RGBA, RGBA8 encode | 8 |
| `png` | Minimal RGBA PNG encode/decode | 9 |
| `audio` | `bsnf` descriptors, streamed-container lookup, Ogg walking | 10 |
| `save` | Save-game `MD5_BlockChecksum` header | 11 |

Naming follows each language's conventions (`chunk_index` / `chunkIndex` /
`ChunkIndex`), but the concepts, field names and error kinds are the same.

### Error kinds

Bindings MUST expose these as distinguishable errors (exception classes,
error enums, sentinel errors, …):

| Kind | Raised when |
|---|---|
| `FormatError` | Bad magic, truncated data, implausible structure |
| `DeclError` | Decl cannot be tokenised/parsed, unknown path, bad path syntax |
| `NoFitError` | A replacement does not fit into its archive slot (→ rebuild) |
| `ModError` | Invalid `mod.json` |

---

## 2. `base/master.index` (*File-Formats §1*)

```
u8[4]   magic   03 53 45 52  ("\x03SER")
BE u32  count   number of *pairs*
repeat 2*count:
  LE u32  length   (includes the terminating NUL)
  char[]  ASCII name, NUL-terminated
```

`parseMasterIndex(bytes) -> [(indexName, resourcesName), …]`. Names are
returned without the trailing NUL. Names alternate index/resources. A binding
MUST fail with `FormatError` on bad magic or truncation. A `length` of 0 or a
string without NUL at `length-1` is tolerated: the name is the bytes up to the
first NUL within `length`.

`buildMasterIndex(pairs) -> bytes` MUST produce the inverse (length = name + 1).

---

## 3. `base/chunkN.index` (*File-Formats §2*)

```
0x00  u8[4]   magic 03 53 45 52
0x04  BE u32  payload size (= file size − 32)
0x08  u8[24]  zero
0x20  BE u32  counter A
0x24  BE u32  counter B
0x28  BE u32  0
0x2C  entries …
```

Entry:

```
LE u32 + char[len]  type     (no NUL)
LE u32 + char[len]  name
LE u32 + char[len]  path
BE u32              offset   into chunkN.resources
BE u32              usize    uncompressed size
BE u32              csize    size on disk
u8[…]               trailer  variable length, preserved verbatim
```

### 3.1 Entry discovery (plausibility scan)

The trailer length is not known, so entries are located by a scan. A
candidate entry at position `p` is **plausible** iff:

1. three length-prefixed strings follow, each with `1 <= len <= 1024`,
   entirely inside the buffer, every byte in `0x20..0x7E`;
2. the 12-byte triple follows inside the buffer;
3. `0 < csize <= usize`;
4. if the resources file size `R` is known: `offset + csize <= R`;
   otherwise: `offset >= 16`.

Algorithm:

```
p = 0x2C
while p < len(buf):
    if plausible(p):
        record entry at p; p = tripleEnd(p)   # tripleEnd = offset of trailer
    else:
        p += 1
trailer(entry i) = buf[tripleEnd_i : start_{i+1}]   (last: … : len(buf))
```

Entry fields exposed: `index` (ordinal), `type`, `name`, `path`, `offset`,
`usize`, `csize`, `start` (byte position of the entry), `tripleOffset`
(position of `offset` field), `trailer` (bytes), `compressed`
(`csize != usize`), `key` (`"type:name"`).

### 3.2 Patching

The index is **never re-serialised**. A binding keeps the original buffer and
overwrites only the triple (`setTriple(entry, offset, usize, csize)` writes 12
bytes at `tripleOffset`). Unchanged → byte-identical output. The header
payload-size field is unaffected because the file length never changes.

### 3.3 Audio trailer field

For `sample` entries the BE u32 at `trailer[20:24]` (= triple + 12 + 20) is the
byte offset of the Ogg stream in the streamed container (§10.2). Exposed as
`streamOffset(entry) -> u32 | null` (null when the trailer is shorter than 24).

---

## 4. Compression (*File-Formats §3*, *Modding-Limits §3*)

* `csize == usize` → payload stored verbatim.
* otherwise → **raw DEFLATE** (no zlib/gzip header).

`inflate(stream, usize)` MUST return exactly `usize` bytes and MUST NOT
require a final block (retail streams end with a sync flush and BFINAL = 0).
Short output → `FormatError`.

`deflateSync(data)` MUST produce a raw DEFLATE stream terminated by a
**sync flush** (`Z_SYNC_FLUSH`): the stream ends with the bytes
`00 00 FF FF` and has no BFINAL block. Using `Z_FINISH` breaks the game
(*Modding-Limits §3*). Compression level: best (9). Byte output MAY differ
between bindings (different deflate engines); only the invariants are tested.

`isSyncFlushed(stream)` → last four bytes are `00 00 FF FF`.

### 4.1 `deflateExact(data, csize, pad) -> (stream, payload) | null`

Produce a stream of **exactly** `csize` bytes (the slot budget) so offsets and
`csize` stay untouched. `payload` is what the stream inflates to (= the new
`usize` content).

```
s = deflateSync(data)
if len(s) == csize: return (s, data)
if len(s) > csize or not pad: return null
r = csize - len(s)
# Strategy A — stored padding blocks (needs r >= 13)
if r >= 13:
    n = ceil((r - 5) / 65540)          # number of stored data blocks
    L = r - 5 - 5*n                     # total padding bytes, L >= 3
    padding = "\n//" + " " * (L - 3)    # a Decl line comment
    split padding into n chunks: every chunk 65535 bytes except the last
    for each chunk c: s += [0x00, len(c) LE u16, ~len(c) LE u16] + c
    s += 00 00 00 FF FF                 # empty stored block = sync marker
    return (s, data + padding)
# Strategy B — recompress with a short comment
for k in 3 .. 66:
    padding = "\n//" + " " * (k - 3)
    s2 = deflateSync(data + padding)
    if len(s2) == csize: return (s2, data + padding)
return null
```

`pad` MUST only be true for text assets (Decls) — padding appends a comment.

---

## 5. Archive / game directory

`Game.open(root)` where `root` is the game directory containing `base/`.

* Reads `base/master.index`; for each pair, loads the index into memory and
  records the resources path and its size.
* `chunks` — list of `Chunk { indexName, resourcesName, index (buffer), entries }`.
* `find(type, name) -> [Occurrence]` — all `(chunk, entry)` pairs with that
  key, in chunk order. The same asset MAY exist in several chunks.
* `read(occurrence) -> bytes` — reads `csize` bytes at `offset` from the
  resources file; returns them verbatim if stored, else `inflate(…, usize)`.
* `readRaw(occurrence) -> bytes` — the on-disk slot bytes.

### 5.1 In-place write

`writeInPlace(occurrence, data, pad=auto) -> original slot bytes`

* stored entry: requires `len(data) == csize` else `NoFitError`; writes
  `data` at `offset`; usize unchanged.
* compressed entry: `deflateExact(data, csize, pad)`; `null` → `NoFitError`;
  writes the stream at `offset`, sets `usize = len(payload)` in the index
  buffer, and persists the index file.
* `pad=auto` means: `data` is valid UTF-8 and contains no NUL byte.
* Offsets and `csize` MUST NOT change; resources length MUST NOT change.
* Returns the overwritten on-disk bytes so a caller can journal them.

`writeAsset(type, name, data)` applies `writeInPlace` to **every**
occurrence (*Modding-Guide §5*); an unknown asset is an error.

### 5.2 Rebuild

`rebuild(chunk, replacements: {entryIndex: data}) -> newResourcesBytes/file`

Rewrites the whole `.resources` in **original offset order**:

```
header: 03 53 45 52 + 12 zero bytes; cursor = 16
for each distinct (offset, csize) slot in ascending offset order:
    entries sharing a slot share the new slot
    payload = replacement ? pack(replacement) : original slot bytes
    new offset = cursor; write payload; cursor += len(payload)
    pad with zero bytes to the next multiple of 16
pack(d) = s = deflateSync(d); if len(s) < len(d): (s, csize=len(s), usize=len(d))
                               else: (d, csize=usize=len(d))
```

All index triples are updated. Offsets remain strictly ascending
(`FileTable is out of order` otherwise, *Modding-Limits §4*).

`validateChunk(chunk)` → list of problems: non-ascending offsets, offsets not
16-aligned, `offset + csize > R`, compressed payloads not sync-flushed.

---

## 6. Decls (*File-Formats §4*, *TNC-Modding-Surface §1–2*)

A Decl is text. The editor records the source span of every value and
replaces only that span. Parse + render with no edits MUST be identical to
the input.

### 6.1 Tokens

| Token | Definition |
|---|---|
| whitespace | space, tab, CR, LF, FF, VT — skipped; LF increments the line |
| comment | `//` to end of line; `/* … */` (unterminated → to EOF). Skipped |
| string | `"` … `"`; a backslash escapes the next character; unterminated → `DeclError` |
| punct | one of `{ } ( ) = ; ,` |
| word | maximal run of any other characters, stopping before whitespace, punct, `"`, or the start of `//` / `/*` |

A word is **numeric** iff it matches
`^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?f?$`.

### 6.2 Grammar

```
document := items-until-EOF
   (a leading "{" … matching "}" is the root block: its items are the
    document's top-level items; items after it are appended to the root)
block    := "{" items "}"
tuple    := "(" … ")"          opaque, nested parentheses balanced
items    := item*
```

Parsing an item at *key position* (current token `t`):

1. `t` is `;` or `,` or `=` or `)` → skip it.
2. `t` is `}` → ends the current block (at top level: skip it).
3. `t` is `{` → anonymous block; key = positional (`[i]`). Exception: if
   this is the very first token of the document it opens the **root block**.
4. `t` is `(` → anonymous tuple; key = positional.
5. `t` is word or string; `n` = next token:
   * `n` is `,`, `}` or EOF → **positional element**: value = `t`, key = `[i]`.
   * `n` is `=` → consume; value := next token parsed as *value* (below).
   * `n` is `{` → value := block.
   * `n` is `(` → value := tuple.
   * `n` is a string or a non-numeric word on the **same line** as `t`
     **and** the token after `n` is `{` → **qualified block**: key = `key(t) + ":" + text(n)`,
     value := block.
   * `n` is word/string on the **same line** as `t` → value := `n` (no-`=`
     dialect); then **vector
     mode**: while the next token is a numeric word on the **same line** as
     the previous value token, and the token after it is not `=`, extend the
     value span over it. Kind stays `word` (or `vector` if extended, see
     below).
   * otherwise (`n` is `;`, `)`, or a word/string on a later line) →
     value-less **flag**: kind `flag`, span = the key token; nothing else is
     consumed. `set` on a flag → `DeclError` (use `setRaw`).
6. After an item, a following `;` is consumed.

A *value* after `=` is: `{` → block, `(` → tuple, string, word. A `}`, `;`
or EOF right after `=` → `DeclError`.

Kinds: `string`, `word`, `vector` (multiple words), `block`, `tuple`, `flag`.

Positional counter `i` counts positional items within one block, starting at
0, independent of named keys.

### 6.3 Keys and paths

* Key text: word as written; string without quotes; qualified blocks
  `key:qualifier`; positional `[i]`.
* Duplicate keys in one block: the first keeps its name, later ones get
  `#1`, `#2`, … (`name#1`).
* A path is the list of keys from the root. Its string form joins keys with
  `.`. A key containing `.`, `"`, `\\` or whitespace is written quoted:
  `"…"` with `\\` and `\"` escaped.
* Path parsing is the inverse: split on `.` outside quotes; quoted segments
  are unescaped. An empty segment → `DeclError`.

Examples: `edit.damageParms.maxDamage`, `edit.validAmmoClips.item[0].clipSize`,
`props.prop:_info.tag:muzzle.trans`, `virtualmapping.[2]`,
`white."models/weapons/handgun/handgun_hg"`.

### 6.4 API

* `Decl.parse(text)` / `new Decl(text)`.
* `text()` — current source.
* `paths()` — ordered list of `(path, kind, raw)` for every node **except
  blocks** (leaves), in source order. `raw` = exact source text of the value.
* `nodes()` — same but including blocks (raw = full `{…}` text).
* `has(path)`, `raw(path)`, `get(path)` — `get` returns the raw text, except
  for strings where the quotes are removed and `\x` escapes unescaped.
* `set(path, value)` — value is string, number or bool; splice
  `format(node, value)` into the span, then re-parse. Unknown path →
  `DeclError`.
* `setRaw(path, text)` — splice verbatim.

### 6.5 Value formatting — `format(node, value)`

* **bool** → `true` / `false`.
* **number** (IEEE double):
  * not finite → `DeclError`.
  * `s` = shortest round-trip decimal **without exponent**; integral values
    have no fractional part; zero (also `-0`) is `0`. Examples: `22`, `-3`,
    `0.1`, `17.5`, `0.0000001`, `100000000000000000000`.
  * If the node kind is `word` and its raw text is numeric: let `m` be the
    raw text without a trailing `f` and without an exponent part. If `m`
    contains `.` with `k` fraction digits, pad `s` with zeros to at least `k`
    fraction digits (adding `.` if `s` has none; `k = 0` → just `.`). If the
    raw text ends in `f`, append `f`.
    Examples with raw `17.500000`: `22` → `22.000000`, `0.0625` →
    `0.062500`, `0.1234567` → `0.1234567`. Raw `25` + `30` → `30`;
    raw `1.0f` + `2` → `2.0f`.
* **string**:
  * node kind `string` → `"` + value with `\` → `\\`, `"` → `\"` + `"`.
  * otherwise → inserted verbatim (blocks, tuples, words: the caller writes
    braces themselves, *Modding-Guide §5*).

---

## 7. `mod.json`

JSON Schema: [`mod.schema.json`](mod.schema.json).

```json
{
  "name": "Faster shotgun",
  "version": "1.0.0",
  "game": "tno",
  "priority": 10,
  "description": "…",
  "assets": {
    "weapon:weapon/shotgun_base": {
      "set": { "edit.validAmmoClips.item[0].clipSize": 40 }
    },
    "material:models/weapons/handgun/handgun_hg": { "file": "decls/handgun.decl" }
  }
}
```

* `priority` default 0. `game` ∈ `tno`, `tnc` (legacy key `spiel` accepted).
* Legacy form: if `assets` is absent, every top-level key containing `:` is
  an asset entry.
* Per asset: `set` (object, **order preserved**), `file` (relative path), or
  both (`file` first, then `set` on top). Neither → `ModError`.
* Asset key = `type:name`, split at the **first** `:`.

### 7.1 Layering — `applyMods(mods, readAsset, readFile)`

* Mods are sorted by `priority` ascending; ties keep input order.
* For every asset key (in order of first appearance across the sorted mods):
  `text = readAsset(type, name)` (missing asset → error entry, skip);
  for each mod touching it, in order: `file` replaces `text` with
  `readFile(mod, path)`; then each `set` pair is applied with `Decl.set`.
* Result: `{ key: newText }`, `conflicts`, `errors`.

Details that bindings MUST match:

* If `readFile` fails, an error is recorded and that mod's `set` for this
  asset is skipped as well.
* If the current text cannot be parsed as a Decl, one error is recorded and
  that mod's `set` is skipped.
* Results are keyed and ordered by first appearance (see above).

Conflicts (non-blocking, *Modding-Guide §4*), computed per asset in this
order: the `file` conflict (if any), then `path` conflicts in order of first
appearance of the path, then one `file-over-set` per `file` change (in mod
order) whose `mods` list is the distinct names of earlier mods with a `set` on
the asset, followed by the file mod's own name:

| Kind | Condition |
|---|---|
| `path` | two or more mods `set` the same path of the same asset; winner = last |
| `file` | two or more mods use `file` on the same asset; winner = last |
| `file-over-set` | a mod's `file` replaces an asset that a lower-priority mod `set` |

Conflict record: `{kind, asset, path|null, mods: [names…], winner}`.
Errors (`{asset, mod, message}`) come from missing assets (`mod` = first mod
touching it), unknown paths and parse errors; the failing `set` is skipped,
the rest continues. Messages are free-form; the vectors compare only
`asset` and `mod`.

---

## 8. BIM images (*File-Formats §5*)

```
0x00  LE u32     hash / id
0x04  u8[4]      09 4D 49 42
0x08  BE u32×13  header fields h[0..12]
0x3C  mips: repeat h[4] times: BE u32 width, BE u32 height, BE u32 size, u8[size]
```

`h[0]` texture type, `h[1]` width, `h[2]` height, `h[3]` depth, `h[4]` mip
count, `h[6]` format, `h[11]`, `h[12]` base width/height. Reading stops early
(no error) if data runs out before `h[4]` mips; zero mips → `FormatError`.

Formats:

| Code | Name | Size of mip `w×h` |
|---|---|---|
| 3 | RGBA8 | `w*h*4`, bytes R,G,B,A |
| 5 | A8 | `w*h` |
| 10 | BC1 | `ceil(w/4)*ceil(h/4)*8` |
| 11 | BC3 | `ceil(w/4)*ceil(h/4)*16` |

`decode(bim, mip=0) -> (width, height, rgba)` (RGBA8, row-major):

* A8 → `(v, v, v, 255)`.
* BC1 block: `c0`, `c1` LE u16 RGB565, then LE u32 indices (2 bits per texel,
  texel `i` = bits `2i..2i+1`, row-major in the 4×4 block). Expand 565:
  `r8 = r5<<3 | r5>>2`, `g8 = g6<<2 | g6>>4`, `b8` like r. If `c0 > c1`:
  `p2 = (2*p0 + p1) / 3`, `p3 = (p0 + 2*p1) / 3` per channel; else
  `p2 = (p0 + p1) / 2`, `p3 = (0,0,0,0)`. Integer division truncates. Alpha
  255 except `p3` in 3-colour mode.
* BC3 block: 8-byte alpha block (`a0`, `a1`, 48-bit LE index field, 3 bits per
  texel) then a BC1 colour block that is **always** decoded in 4-colour mode.
  Alpha palette: if `a0 > a1`: `a_i = ((7-i)*a0 + i*a1) / 7` for i=1..6 (index
  2..7); else `a_i = ((5-i)*a0 + i*a1) / 5` for i=1..4, index 6 = 0, 7 = 255.
* Texels outside `w×h` (partial blocks) are discarded.

`encodeRgba8(template, width, height, rgba) -> bim`: copies the template's
hash and header, sets `h[1]=h[11]=width`, `h[2]=h[12]=height`, `h[4]=1`,
`h[6]=3`, writes one mip.

---

## 9. PNG

`encodePng(width, height, rgba)` — 8-bit RGBA, non-interlaced, one IDAT,
filter 0 on every row, zlib stream (any level). `decodePng(bytes)` — MUST
accept 8-bit colour types 2 (RGB), 6 (RGBA), 0 (grey), 4 (grey+alpha),
non-interlaced, all five filter types, multiple IDAT; returns RGBA. Others →
`FormatError`.

---

## 10. Audio (*Audio §2–3*, *Streamed-Audio-Containers*)

### 10.1 `bsnf` descriptor

```
0x00 "bsnf"   0x04 BE u32 languageCount
```

* `languageCount == 1` (74 bytes): one language named `""` whose 42-byte
  block starts at `0x20`.
* otherwise (272 bytes for 4): `languageCount` × 24-byte headers at `0x08`:
  `char[16]` name (NUL-padded), BE u32 length, BE u32 offset of its 42-byte
  block within the descriptor.

42-byte block (*inferred* from the 74-byte layout):

```
+0  BE u32 hash        +8  BE u32 granule (PCM samples)
+16 BE u32 length (Ogg bytes)
+20 LE u16 formatTag (0x674F) +22 LE u16 channels +24 LE u32 sampleRate
+28 LE u32 avgBytesPerSec +32 LE u16 blockAlign +34 LE u16 bitsPerSample
+36 LE u16 cbSize +38 LE u32 granule (repeat)
```

`parseBsnf(bytes) -> [{language, hash, length, granule, formatTag, channels,
sampleRate, avgBytesPerSec, blockAlign, bitsPerSample}]` (bindings use their
language's casing; `audio.json` uses snake_case keys). Duration =
`granule / sampleRate`.

### 10.2 Streamed containers

`streamContainerFor(assetName)` → `"english.streamed"` if the name starts with
`sound/vo/english/`, else `"streamed.resources"` (both in `base/`, not in
`master.index`).

`oggStreamLength(bytes|reader, offset)` walks Ogg pages from `offset`:
page = `"OggS"`, header 27 bytes, `segments = byte[26]`, segment table,
body = sum(table); stop after the page with header-type bit `0x04` (EOS).
Returns total bytes; missing `OggS` → `FormatError`.

---

## 11. Save games (*Save-Games-and-Profiles §3*)

```
checksum(payload) = BE u32( w0 ^ w1 ^ w2 ^ w3 ),  w = LE u32×4 of MD5(payload)
```

`.dat` files and TNO `profile.bin`: `file[0:4] == checksum(file[4:])`.
`checksum(b"") = 3b 75 65 5e`. API: `saveChecksum(payload) -> 4 bytes`,
`verifySave(file) -> bool`, `fixSave(file) -> bytes` (input shorter than 4
bytes → error).

---

## 12. Test vectors

`spec/vectors/` (generated by `generate.py`, which uses the Python reference
binding, and committed):

| File | Content |
|---|---|
| `game/base/*` | A tiny synthetic TNO game: `master.index`, `chunk0/1.index/.resources` |
| `archive.json` | Expected pairs, entries, sha256 of each decoded payload, stream offsets |
| `decl/*.decl` + `decl.json` | Decl inputs, expected `paths()`, edit cases with expected output, error cases |
| `format.json` | `format(node, value)` cases |
| `bim/*.bim` + `bim.json` | Header fields and sha256 of the decoded RGBA |
| `audio/*` + `audio.json` | `bsnf` descriptors, a synthetic Ogg container |
| `save.json` | Payload hex → checksum hex |
| `mod/` + `mod.json` | Mods, base decls, expected results and conflicts |
| `compression.json` | `deflateExact` cases (invariants only) |
