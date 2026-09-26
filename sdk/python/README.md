# wolfenstein-sdk (Python)

Python binding and reference implementation of the Wolfenstein modding SDK
for *Wolfenstein: The New Order* (id Tech 5) and the Decl format shared with
*Wolfenstein II: The New Colossus*. Pure standard library, Python ≥ 3.9.

This package also ships the **`wolfsdk` command line tool** with a
journaled patcher (`apply` / `revert`).

```bash
pip install wolfenstein-sdk
```

## Command line

```bash
export WOLFSDK_GAME="C:/Games/Wolfenstein The New Order"   # or pass -g PATH

wolfsdk info                                   # archives and entry counts
wolfsdk list --type damage --grep tungsten     # what exists?
wolfsdk cat weapon:weapon/shotgun_base         # what does it look like?
wolfsdk paths damage:damage/tungsten/mg60      # what can I change?
wolfsdk plan mods/faster_shotgun               # dry run, prints a diff
wolfsdk apply mods/faster_shotgun              # journaled write
wolfsdk revert                                 # undo everything
wolfsdk validate                               # archive invariants
wolfsdk image-export image:ui/foo out.png      # BIM -> PNG
wolfsdk audio-info sample:sound/.../imp_01.wav # bsnf descriptor + stream offset
wolfsdk save --fix checkpoint.dat              # recompute save checksum
```

`apply` writes into the existing archive slots (offsets and `csize` never
move). When a change does not fit, only that archive is rebuilt in original
order. Every change is recorded in `base/_wolfsdk_backup/journal.json`;
`revert` restores the files byte for byte. After a game update run `revert`
first.

## Library

```python
from wolfsdk import Game, Decl, Mod, apply_mods, parse_bim, decode, encode_png

game = Game("C:/Games/Wolfenstein The New Order")
text = game.read_asset("damage", "damage/tungsten/mg60").decode()

decl = Decl(text)
for path, kind, raw in decl.paths():
    print(path, raw)
decl.set("edit.damageParms.maxDamage", 30)      # keeps "17.500000" style -> 30.000000
game.write_asset("damage", "damage/tungsten/mg60", decl.text.encode())

# mods in memory
mod = Mod.load("mods/faster_shotgun")
result = apply_mods([mod], lambda t, n: game.read_asset(t, n).decode())
print(result.conflicts, result.errors)

# images
w, h, rgba = decode(parse_bim(game.read_asset("image", "ui/foo")))
open("foo.png", "wb").write(encode_png(w, h, rgba))
```

## mod.json

```json
{
  "name": "Faster shotgun",
  "game": "tno",
  "priority": 10,
  "assets": {
    "weapon:weapon/shotgun_base": {
      "set": {
        "edit.firingIntervals.firingIntervals[0]": 120,
        "edit.validAmmoClips.item[0].clipSize": 40
      }
    }
  }
}
```

Schema: [`spec/mod.schema.json`](https://github.com/LopeKinz/wolfenstein-modloader/blob/main/spec/mod.schema.json).

## Links

* Specification: [`spec/SPEC.md`](https://github.com/LopeKinz/wolfenstein-modloader/blob/main/spec/SPEC.md)
* Research and format documentation: [project wiki](https://github.com/LopeKinz/wolfenstein-modloader/wiki)
* Other languages: TypeScript (`npm i wolfenstein-sdk`), Rust (`cargo add wolfenstein-sdk`),
  Go (`go get github.com/LopeKinz/wolfenstein-modloader/sdk/go`)

Not affiliated with MachineGames, Bethesda Softworks, ZeniMax Media or
Microsoft. Use only with game files you are legally entitled to access.
