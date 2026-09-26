# wolfenstein-sdk

TypeScript / Node.js SDK for modding **Wolfenstein: The New Order** (id Tech 5)
and the Decl text format of **Wolfenstein II: The New Colossus**.

It reads and patches `.index` / `.resources` archives, edits Decls without
touching anything but the value you change, decodes BIM textures to PNG,
parses audio descriptors, fixes save-game checksums and layers `mod.json`
mods with conflict detection.

This package is one binding of a language-neutral contract,
[`spec/SPEC.md`](../../spec/SPEC.md), and passes the shared test vectors in
[`spec/vectors/`](../../spec/vectors/). Background on the file formats is in the
[project wiki](https://github.com/LopeKinz/wolfenstein-modloader/wiki)
(start with the
[Modding Guide](https://github.com/LopeKinz/wolfenstein-modloader/wiki/Modding-Guide)
and [File Formats](https://github.com/LopeKinz/wolfenstein-modloader/wiki/File-Formats)).

- Zero runtime dependencies (`node:zlib`, `node:crypto`, `node:fs` only)
- ESM, TypeScript declarations included, Node.js >= 18

## Install

```sh
npm install wolfenstein-sdk
```

## Quick examples

### Open a game and read an asset

```ts
import { Game } from "wolfenstein-sdk";

const game = Game.open("C:/Games/Wolfenstein The New Order"); // the directory containing base/

for (const chunk of game.chunks) {
  console.log(chunk.indexName, chunk.entries.length, "entries");
}

const occurrences = game.find("weapon", "weapon/shotgun_base"); // may live in several chunks
const bytes = game.readAsset("weapon", "weapon/shotgun_base"); // Buffer | null
console.log(bytes?.toString("utf-8"));
```

### Edit a Decl

Edits replace only the value's source span; everything else (comments,
whitespace, number formatting) stays byte-identical.

```ts
import { Decl } from "wolfenstein-sdk";

const decl = Decl.parse(bytes!.toString("utf-8"));
for (const [path, kind, raw] of decl.paths()) console.log(path, kind, raw);

decl.set("edit.validAmmoClips.item[0].clipSize", 40);
decl.set("edit.isDualWieldable", false);
console.log(decl.get("inherit"), decl.text);

// write it back into every occurrence without moving any archive offsets
game.writeAsset("weapon", "weapon/shotgun_base", Buffer.from(decl.text, "utf-8"));
```

`writeAsset` / `writeInPlace` throw `NoFitError` when the new text does not
compress into the original slot; use `game.rebuild(chunk, new Map([[entry.index, data]]))`
to rewrite the whole `.resources` file instead. Always work on a backup.

### Apply mods in memory

```ts
import { Mod, applyMods } from "wolfenstein-sdk";

const mods = [Mod.load("mods/faster-shotgun"), Mod.load("mods/white-handgun/mod.json")];
const { results, conflicts, errors } = applyMods(mods, (type, name) => game.readAsset(type, name));

for (const c of conflicts) console.warn(`${c.kind} conflict on ${c.asset}: ${c.mods.join(", ")} -> ${c.winner}`);
for (const [key, text] of results) console.log(key, text.length);
```

### Decode a BIM texture to PNG

```ts
import fs from "node:fs";
import { decodeBim, encodePng, parseBim } from "wolfenstein-sdk";

const bim = parseBim(game.readAsset("image", "ui/test_bc1")!);
console.log(bim.formatName, bim.width, bim.height, bim.mips.length);
const { width, height, rgba } = decodeBim(bim); // RGBA8, mip 0
fs.writeFileSync("out.png", encodePng(width, height, rgba));
```

### Save-game checksum

```ts
import fs from "node:fs";
import { fixSave, verifySave } from "wolfenstein-sdk";

const save = fs.readFileSync("savegame.dat");
if (!verifySave(save)) fs.writeFileSync("savegame.dat", fixSave(save));
```

## API overview

| Module (SPEC §) | Exports |
|---|---|
| master index (§2) | `parseMasterIndex`, `buildMasterIndex` |
| chunk index (§3) | `ChunkIndex` (`entries`, `find`, `setTriple`, `toBytes`, `counterA`, `counterB`), `Entry` (`tripleOffset`, `streamOffset`, `compressed`, `key`, …), `buildChunkIndex` |
| compression (§4) | `inflate`, `deflateSync`, `deflateExact`, `isSyncFlushed` |
| archive (§5) | `Game` (`open`, `chunks`, `find`, `read`, `readRaw`, `readAsset`, `writeInPlace`, `writeAsset`, `rebuild`), `validateChunk`, `looksTextual` |
| decl (§6) | `Decl` (`parse`, `text`, `paths`, `nodes`, `has`, `raw`, `get`, `set`, `setRaw`), `parsePath`, `formatPath`, `formatValue` |
| mod (§7) | `Mod` (`load`, `loads`, `fromObject`), `applyMods` |
| bim (§8) | `parseBim`, `decodeBim` (alias `decode`), `encodeRgba8`, `buildBim` |
| png (§9) | `encodePng`, `decodePng` |
| audio (§10) | `parseBsnf`, `streamContainerFor`, `oggStreamLength` (bytes, file path, file descriptor or `{ read(pos, n) }`) |
| save (§11) | `saveChecksum`, `verifySave`, `fixSave` |
| errors (§1) | `WolfSdkError` ⊃ `FormatError`, `DeclError`, `NoFitError`, `ModError` |

## Notes and limitations

- **Numbers.** JavaScript cannot tell `30` from `30.0`; `Decl.set` follows
  SPEC §6.5, which keeps the original formatting of the value it replaces
  (`17.500000` + `22` → `22.000000`), so this does not matter in practice.
- **Integer-like keys in `mod.json`.** `JSON.parse` moves integer-like
  object keys (e.g. `"0"`) in front of all other keys, so the order of such
  paths inside a `set` object is not preserved by `Mod.loads` / `Mod.load`.
  Real Decl paths practically never look like that; if you need exact order,
  build the mod with `Mod.fromObject` and pass `set` (or `assets`) as a `Map`.
- **Command line and patcher.** The full CLI and the journaled
  patcher (backups, `apply` / `revert`) live in the Python package
  (`sdk/python`). This package provides the library building blocks.
- The id Tech 6 `IDCL` containers of *The New Colossus* are out of scope
  for 0.1.0 (Oodle-compressed).

## Development

```sh
npm install
npm test        # builds with tsc, then runs the shared spec vectors with node --test
```

Set `WOLFSDK_VECTORS` to point the tests at another copy of `spec/vectors`.

## License

MIT
