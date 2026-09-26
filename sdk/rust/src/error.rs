//! Error kinds shared by all bindings (SPEC §1).

use std::fmt;

/// Every error the SDK returns.
#[derive(Debug)]
pub enum Error {
    /// Bad magic, truncated data or implausible structure.
    Format(String),
    /// A Decl cannot be parsed, or a path is unknown or malformed.
    Decl(String),
    /// A replacement does not fit into its archive slot; rebuild instead.
    NoFit(String),
    /// A `mod.json` file is invalid.
    Mod(String),
    /// An I/O error from the file system.
    Io(std::io::Error),
}

/// Result alias used throughout the crate.
pub type Result<T> = std::result::Result<T, Error>;

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Format(m) => write!(f, "format error: {m}"),
            Error::Decl(m) => write!(f, "decl error: {m}"),
            Error::NoFit(m) => write!(f, "no fit: {m}"),
            Error::Mod(m) => write!(f, "mod error: {m}"),
            Error::Io(e) => write!(f, "I/O error: {e}"),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e)
    }
}

pub(crate) fn be32(b: &[u8], pos: usize) -> u32 {
    u32::from_be_bytes([b[pos], b[pos + 1], b[pos + 2], b[pos + 3]])
}

pub(crate) fn le32(b: &[u8], pos: usize) -> u32 {
    u32::from_le_bytes([b[pos], b[pos + 1], b[pos + 2], b[pos + 3]])
}

pub(crate) fn le16(b: &[u8], pos: usize) -> u16 {
    u16::from_le_bytes([b[pos], b[pos + 1]])
}

/// Decode ASCII the way Python's `decode("ascii", "replace")` does.
pub(crate) fn ascii_replace(b: &[u8]) -> String {
    b.iter()
        .map(|&c| if c < 0x80 { c as char } else { '\u{FFFD}' })
        .collect()
}
