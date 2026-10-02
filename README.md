# Wolfenstein Modloader

Community documentation and reverse-engineering research for modding:

- **Wolfenstein: The New Order** (id Tech 5)
- **Wolfenstein II: The New Colossus** (id Tech 6)

🇬🇧 English · [🇩🇪 Deutsch](README.de.md)

> [!NOTE]
> This repository publishes the documentation and the **Wolfenstein SDK** in
> four languages. Mods, game files, and large research artifacts are not part
> of the public release.

## SDK

The SDK turns the documented formats into code. Every binding implements the
same [language-neutral specification](spec/SPEC.md) and is tested against the
same [shared test vectors](spec/vectors/).

| Language | Package | Install | Docs |
|---|---|---|---|
| Python (reference + CLI) | `wolfenstein-sdk` on PyPI | `pip install wolfenstein-sdk` | [sdk/python](sdk/python/README.md) |
| TypeScript / Node.js | `wolfenstein-sdk` on npm | `npm install wolfenstein-sdk` | [sdk/typescript](sdk/typescript/README.md) |
| Rust | `wolfenstein-sdk` on crates.io | `cargo add wolfenstein-sdk` | [sdk/rust](sdk/rust/README.md) |
| Go | `github.com/LopeKinz/wolfenstein-modloader/sdk/go` | `go get github.com/LopeKinz/wolfenstein-modloader/sdk/go` | [sdk/go](sdk/go/README.md) |

What it covers (*The New Order* archives; the Decl format of both games):

| Area | Features |
|---|---|
| Archives | `master.index`, `chunkN.index` (byte-preserving), raw-DEFLATE with sync flush, in-place slot writes, ordered rebuild, invariant validation |
| Decls | Span-preserving parser/editor for both dialects, dotted paths, number formatting that keeps the original style |
| Mods | `mod.json` ([schema](spec/mod.schema.json)), priority layering, conflict reports |
| Images | BIM header, decode RGBA8/A8/BC1/BC3, RGBA8 encode, PNG in/out |
| Audio | `bsnf` descriptors, streamed-container offsets, Ogg stream lengths |
| Saves | `MD5_BlockChecksum` header: compute, verify, fix |

The Python package adds the `wolfsdk` command line tool with a journaled
`apply` / `revert` patcher:

```bash
pip install wolfenstein-sdk
wolfsdk -g "C:/Games/Wolfenstein The New Order" paths damage:damage/tungsten/mg60
wolfsdk -g "C:/Games/Wolfenstein The New Order" apply mods/faster_shotgun
wolfsdk -g "C:/Games/Wolfenstein The New Order" revert
```

Not covered yet: the Oodle-compressed `IDCL` container of *The New Colossus*,
virtual-texture pixels, and BC1/BC3 compression.

Releases: pushing a tag `vX.Y.Z` runs
[`sdk-release.yml`](.github/workflows/sdk-release.yml), which publishes all
four packages. `python scripts/sync_version.py X.Y.Z` bumps every version.

## Documentation

The complete English documentation is available in the
**[Wolfenstein Modloader Wiki](https://github.com/LopeKinz/wolfenstein-modloader/wiki)**.

Recommended starting points:

- [Modding Guide](https://github.com/LopeKinz/wolfenstein-modloader/wiki/Modding-Guide)
- [File Formats](https://github.com/LopeKinz/wolfenstein-modloader/wiki/File-Formats)
- [Modding Limits](https://github.com/LopeKinz/wolfenstein-modloader/wiki/Modding-Limits)
- [The New Order Modding Surface](https://github.com/LopeKinz/wolfenstein-modloader/wiki/TNO-Modding-Surface)
- [The New Colossus Modding Surface](https://github.com/LopeKinz/wolfenstein-modloader/wiki/TNC-Modding-Surface)
- [Reverse Engineering Research](https://github.com/LopeKinz/wolfenstein-modloader/wiki/Reverse-Engineering-Research)
- [Known Contradictions](https://github.com/LopeKinz/wolfenstein-modloader/wiki/Known-Contradictions)

## Evidence Standards

The documentation deliberately distinguishes between:

- **Measured** — confirmed at runtime, on disk, or against real game data.
- **Inferred** — supported by binary analysis or structural evidence, but not
  yet confirmed in the running game.
- **Hypothesized** — a plausible lead that still requires verification.

Negative results, failed approaches, and unresolved questions are retained so
that readers can tell established behavior from ongoing research.

## Current Scope

The Wiki covers archive and resource formats, declarations, audio, UI assets,
maps, save games, developer mode, loading paths, patch precedence, textures,
Kiscule scripting, custom-map research, and detailed reverse-engineering reports.

Large generated artifacts from the research workspace are represented by an
[artifact inventory](https://github.com/LopeKinz/wolfenstein-modloader/wiki/Research-Artifacts)
instead of being duplicated in the Wiki repository.

## Support

WolfSDK is free and stays free. If it helps you, you can [buy the developer a coffee](https://buymeacoffee.com/swscc5yr7rs). Nothing is locked behind it.

## Disclaimer

This is an independent community project. It is not affiliated with or endorsed
by MachineGames, Bethesda Softworks, ZeniMax Media, or Microsoft. Wolfenstein and
the names of the referenced games are trademarks of their respective owners.

Do not redistribute copyrighted game assets. Use the documentation and tooling
only with game files you are legally entitled to access.

The SDK source code is released under the [MIT License](LICENSE).
