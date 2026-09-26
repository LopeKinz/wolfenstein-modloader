# wolfenstein-sdk

Rust SDK for modding **Wolfenstein: The New Order** (id Tech 5) and the Decl
text format of **Wolfenstein II: The New Colossus**.

It reads and patches `.index` / `.resources` archives, edits Decls without
touching anything but the value you change, decodes BIM textures to PNG,
parses audio descriptors, fixes save-game checksums and layers `mod.json`
mods with conflict detection.

This crate is one binding of a language-neutral contract,
[`spec/SPEC.md`](https://github.com/LopeKinz/wolfenstein-modloader/blob/main/spec/SPEC.md), and passes the shared test vectors in
[`spec/vectors/`](https://github.com/LopeKinz/wolfenstein-modloader/blob/main/spec/vectors/). Background on the file formats is in the
[project wiki](https://github.com/LopeKinz/wolfenstein-modloader/wiki)
(start with the
[Modding Guide](https://github.com/LopeKinz/wolfenstein-modloader/wiki/Modding-Guide)
and [File Formats](https://github.com/LopeKinz/wolfenstein-modloader/wiki/File-Formats)).

- Pure Rust; dependencies: `flate2`, `md-5`, `serde_json`
- The library is imported as `wolfsdk`
- The full command line tool and the journaled `apply` / `revert` patcher live
  in the Python package ([`sdk/python`](../python/)); this crate provides the
  building blocks

## Install

```toml
[dependencies]
wolfenstein-sdk = "0.1"
```

or `cargo add wolfenstein-sdk`.

## Examples

### Open a game and read an asset

```rust,no_run
use wolfsdk::Game;

fn main() -> wolfsdk::Result<()> {
    let game = Game::open("/games/Wolfenstein The New Order")?;
    for chunk in &game.chunks {
        println!("{}: {} entries", chunk.index_name, chunk.entries().len());
    }
    // Every chunk that contains the asset:
    for occ in game.find("weapon", "weapon/shotgun_base") {
        println!("found in {}", game.chunk(&occ).index_name);
    }
    let bytes = game.read_asset("weapon", "weapon/shotgun_base")?.expect("asset exists");
    println!("{}", String::from_utf8_lossy(&bytes));
    Ok(())
}
```

### Edit a Decl

Only the value span changes; everything else (comments, whitespace, number
style) stays byte for byte.

```rust
use wolfsdk::Decl;

fn main() -> wolfsdk::Result<()> {
    let mut decl = Decl::parse("{ edit = { damageParms = { maxDamage = 17.500000; } } }")?;
    decl.set("edit.damageParms.maxDamage", 22)?;
    assert_eq!(decl.raw("edit.damageParms.maxDamage")?, "22.000000");
    for (path, kind, raw) in decl.paths() {
        println!("{path} ({kind}) = {raw}");
    }
    Ok(())
}
```

Write it back into the archive without moving any offsets
(`Error::NoFit` means the new text does not fit its slot; use
`Game::rebuild` instead):

```rust,no_run
# fn main() -> wolfsdk::Result<()> {
# let mut game = wolfsdk::Game::open("game")?;
# let decl = wolfsdk::Decl::parse("{}")?;
game.write_asset("weapon", "weapon/shotgun_base", decl.text().as_bytes())?;
# Ok(())
# }
```

### Apply mods in memory

```rust,no_run
use wolfsdk::{apply_mods, Game, Mod};

fn main() -> wolfsdk::Result<()> {
    let game = Game::open("/games/Wolfenstein The New Order")?;
    let mods = vec![Mod::load("mods/faster-shotgun")?, Mod::load("mods/white-handgun")?];
    let result = apply_mods(
        &mods,
        |ty, name| {
            game.read_asset(ty, name)
                .ok()
                .flatten()
                .map(|b| String::from_utf8_lossy(&b).into_owned())
        },
        None, // resolve `file` entries relative to each mod directory
    );
    for c in &result.conflicts {
        println!("{} conflict on {}: {:?} -> {}", c.kind.as_str(), c.asset, c.mods, c.winner);
    }
    for e in &result.errors {
        eprintln!("{} / {}: {}", e.asset, e.mod_name, e.message);
    }
    for (key, text) in &result.results {
        println!("{key}: {} bytes", text.len());
    }
    Ok(())
}
```

### BIM texture to PNG

```rust,no_run
use wolfsdk::{decode, encode_png, parse_bim, Game};

fn main() -> wolfsdk::Result<()> {
    let game = Game::open("/games/Wolfenstein The New Order")?;
    let data = game.read_asset("image", "ui/test_bc1")?.expect("asset exists");
    let bim = parse_bim(&data)?;
    println!("{}x{} {}", bim.width(), bim.height(), bim.format_name());
    let (w, h, rgba) = decode(&bim, 0)?;
    std::fs::write("out.png", encode_png(w, h, &rgba)?)?;
    Ok(())
}
```

### Save-game checksum

```rust,no_run
use wolfsdk::{fix_save, verify_save};

fn main() -> wolfsdk::Result<()> {
    let data = std::fs::read("save.dat")?;
    if !verify_save(&data) {
        std::fs::write("save.dat", fix_save(&data)?)?;
    }
    Ok(())
}
```

## Modules

| Module | Purpose |
|---|---|
| `master_index` | Parse / build `base/master.index` |
| `chunk_index` | Parse and patch `base/chunkN.index` (never re-serialised) |
| `compression` | Raw DEFLATE with sync flush, exact-size packing |
| `archive` | `Game`: find, read, write in place, rebuild, validate |
| `decl` | Span-preserving Decl parser and editor |
| `mods` | `mod.json` loading, priority layering, conflicts |
| `bim` | BIM header, decode RGBA8/A8/BC1/BC3, RGBA8 encode |
| `png` | Minimal RGBA PNG encode / decode |
| `audio` | `bsnf` descriptors, streamed containers, Ogg stream lengths |
| `save` | Save-game `MD5_BlockChecksum` |

Errors are one enum, `wolfsdk::Error`, with the spec's kinds
`Format`, `Decl`, `NoFit` and `Mod` plus `Io`.

## Tests

```sh
cargo test
```

The integration tests in `tests/vectors.rs` read the shared vectors from
`../../spec/vectors` (override with `WOLFSDK_VECTORS=/path/to/vectors`).
Write tests run on a temporary copy of the synthetic game.

## License

MIT
