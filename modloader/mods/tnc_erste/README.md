# tnc_erste -- Invulnerable

The first mod for **Wolfenstein II: The New Colossus**.

## What it changes

The seven `table` decls that `entityDef:player` points to with `veryEasyTable`
through `meinLebenTable`. Their value is the multiplier on the
damage the player takes.

| Decl | before | after | Bytes |
|---|---|---|---:|
| `table:playerdamage/veryeasy` | `min 0.1 max 0.1` | `min 0.0 max 0.0` | 58 |
| `table:playerdamage/easy` | `min 0.4 max 0.4` | `min 0.0 max 0.0` | 58 |
| `table:playerdamage/normal` | `min 0.5 max 0.5` | `min 0.0 max 0.0` | 58 |
| `table:playerdamage/hard` | `min 0.75 max 0.75` | `min 0.00 max 0.00` | 60 |
| `table:playerdamage/veryhard` | `min 1.25 max 1.25` | `min 0.00 max 0.00` | 60 |
| `table:playerdamage/nightmare` | `min 1.75 max 1.75` | `min 0.00 max 0.00` | 60 |
| `table:playerdamage/meinleben` | `min 1.75 max 1.75` | `min 0.00 max 0.00` | 60 |

In full, using `playerdamage/normal` as the example:

```
before   { spline clamp min 0.5 max 0.5 left 0 right 1 {0:0, 1:1} }
after    { spline clamp min 0.0 max 0.0 left 0 right 1 {0:0, 1:1} }
```

All fourteen numbers are replaced with the same number of characters, so the decls stay
exactly as long as in the retail archive. Outside these fourteen spans
not a single byte is touched -- recomputed byte by byte, see `PLAN.md`.

Side finding from the data: `nightmare` and `meinleben` are identical
(both 1.75, even the same `data_hash`). "Mein Leben" is not
harder than `nightmare`, just less forgiving.

## How to see in game that it works

Walk into a line of fire and do not take cover. The health bar
stays put, the red hit border stays away, the armor does not
drop. Without the mod, BJ is dead within a few seconds on Normal.

Counter-check in game: set the difficulty to "Mein Leben" -- the mod
works the same there, because all seven tables are changed. Anyone who
accidentally patches only *one* table sees the difference when
switching the difficulty.

## What it does not change

Fall damage, drowning and scripted deaths may not go
through these tables -- that is **unverified**. Enemy health,
weapon damage and ammo stay untouched.

## Cost

* `gameresources.resources` -- 702,059,123 bytes (669.5 MiB), 50,839 entries. The only touched
  archive; a backup of it costs the same amount of disk space.

## To see it in game

Three steps, and the third is still missing:

1. Back up `gameresources.resources` -- 669.5 MiB. Without this copy there is no way back
   except Steam's file verification.
2. Write the seven payloads from `mod.json` into their slots:
   `Archive.write_in_place({entry: bytes, ...})`. Because every replacement
   has the same number of characters, it stays an in-place write -- no
   `rebuild()`, no shifted offsets, the file keeps its
   size to the byte.
3. **Missing:** an applier for part 2. `wolfsdk/patch.py` and
   `wolfsdk/mod.py` only know the id Tech 5 format of part 1;
   `wolfsdk/tncpatch.py` is being built elsewhere right now. Until it
   is ready, this mod is a checked manifest, not a button press.
   It is not enabled, `config.json` is untouched.

Also open: whether the engine reads `min 0.0 max 0.0` as "no damage".
If not, the same-length fallback `0.1` /
`0.10` is one build run away and costs the same budget.

## Status

Built and dry-run computed, **not applied and not tested in
game**. The planning numbers are in `PLAN.md`.

Rebuild:          `python tools/build_tnc_mod.py`
Counter-checks:   `python tools/build_tnc_mod.py --gegenprobe`
