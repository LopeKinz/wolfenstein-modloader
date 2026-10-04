# Golden Pistol (Simple)

Colors the standard pistol `weapon/player/sp/handgun` gold -- the weapon from
the campaign and from the DLC "Agent Silent Death" (`agent_shade`). A pure
material skin, no new images: it only uses existing, always-loaded images
from `gameresources.resources`.

## Recipe
Three slots are redirected per material; everything else (e.g. `loosenormal`,
i.e. the shape) stays:

| Slot | new | effect |
|------|-----|--------|
| `loosealbedo` | `textures/system/constant_color/black.tga` | black base |
| `loosespec` | `textures/system/specular/gold.tga` (sRGB 247,219,148) | gold reflection |
| `loosesmoothness` | `textures/common/flatcolour/glosshgh_pm.tga` | high gloss/polished |

This is exactly the recipe retail uses to make the material `textures/pickups/gold`
gold (black albedo + gold specular). All three images are flat and
UV-independent, so they fit any weapon.

## Covered materials (9)
Body, blood body, silencer, standard magazine, extended magazine,
bullet, elephant round, blood elephant round, pickup. Not touched:
`glow_ironsight_01` (the glowing sight keeps glowing). The blood variants
are included so the weapon stays golden when it is bloody, too.

All nine materials live only in `base/gameresources.resources`
(no DLC copy), which is loaded for every campaign and DLC map.

## Status: structurally valid, not yet confirmed in game
`tools/verify_skin_simple.py` checks on a COPY of `gameresources.resources`:
apply through the real `mod` + `tncpatch` path, re-reading the decls (the gold is
in), revert bit-identical (sha256). All of that holds.

**Formerly a blocker, fixed since:** a `file` replacement changes
`csize`/`usize` of the entry. Since the texture mod
(`mods/tnc_skin_gold_pistol`), `tncpatch` recomputes the header hashes
(+0x20/+0x08) after every write; only the data checksum (+0x50) stays old, and the
engine does not check it with the default setting. Before that, `gameresources`
was invalid at the header hash after the apply. The engine checks
+0x20 in `OpenContainer` (see `re_probes/agent_reports/idclhash.md`) and
otherwise rejects the archive as "corrupt metadata" -- this matches the startup crash of
the 24-decl mod in `re_probes/agent_reports/gore_runtime.md`. The verifier now
checks that both header hashes are valid after the real apply. That the
engine accepts the recomputed hashes has not been observed in game yet.

## Loader
WolfSDK.cmd -> "Mods" tab -> enable "Golden Pistol (Simple)" ->
Spielen.cmd. Disable it + Spielen.cmd undoes it (the revert is
bit-identical).
