//! # wolfenstein-sdk
//!
//! Modding SDK for *Wolfenstein: The New Order* and *The New Colossus*:
//! id Tech 5 archives (`master.index`, `chunkN.index` / `.resources`), the
//! Decl text format with a span-preserving editor, BIM images, audio
//! descriptors, save-game checksums and `mod.json` layering.
//!
//! This crate is the Rust binding of the language-neutral contract in
//! [`spec/SPEC.md`](https://github.com/LopeKinz/wolfenstein-modloader/blob/main/spec/SPEC.md);
//! all bindings run the same shared test vectors.
//!
//! ```no_run
//! use wolfsdk::{Decl, Game};
//!
//! # fn main() -> wolfsdk::Result<()> {
//! let mut game = Game::open("/path/to/Wolfenstein The New Order")?;
//! let text = game.read_asset("weapon", "weapon/shotgun_base")?.expect("asset exists");
//! let mut decl = Decl::parse(std::str::from_utf8(&text).expect("utf-8"))?;
//! decl.set("edit.validAmmoClips.item[0].clipSize", 40)?;
//! // Writes into every occurrence without moving any offsets.
//! game.write_asset("weapon", "weapon/shotgun_base", decl.text().as_bytes())?;
//! # Ok(())
//! # }
//! ```

#![warn(missing_docs)]

pub mod archive;
pub mod audio;
pub mod bim;
pub mod chunk_index;
pub mod compression;
pub mod decl;
pub mod error;
pub mod master_index;
pub mod mods;
pub mod png;
pub mod save;

/// Crate version (matches the spec version it implements).
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

pub use archive::{looks_textual, validate_chunk, validate_chunk_with, Chunk, Game, Occurrence};
pub use audio::{
    ogg_stream_length, ogg_stream_length_reader, parse_bsnf, stream_container_for, SampleInfo,
};
pub use bim::{build_bim, decode, encode_rgba8, parse_bim, Bim, Mip};
pub use chunk_index::{build_chunk_index, ChunkIndex, Entry, NewEntry};
pub use compression::{deflate_exact, deflate_sync, inflate, is_sync_flushed};
pub use decl::{format_path, format_value, parse_path, Decl, Kind, Node, Value};
pub use error::{Error, Result};
pub use master_index::{build_master_index, parse_master_index};
pub use mods::{apply_mods, ApplyError, ApplyResult, AssetChange, Conflict, ConflictKind, Mod};
pub use png::{decode_png, encode_png};
pub use save::{fix_save, save_checksum, verify_save};

/// Compiles the README examples as doctests.
#[cfg(doctest)]
#[doc = include_str!("../README.md")]
pub struct ReadmeDoctests;
