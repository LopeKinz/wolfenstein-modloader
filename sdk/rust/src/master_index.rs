//! `base/master.index` (SPEC §2).

use crate::error::{ascii_replace, be32, le32, Error, Result};

/// File magic shared by master index, chunk index and resources files.
pub const MAGIC: &[u8; 4] = b"\x03SER";

/// Parse `master.index` into `(index name, resources name)` pairs.
pub fn parse_master_index(data: &[u8]) -> Result<Vec<(String, String)>> {
    if data.len() < 4 || &data[..4] != MAGIC {
        return Err(Error::Format("master.index: bad magic".into()));
    }
    if data.len() < 8 {
        return Err(Error::Format("master.index: truncated header".into()));
    }
    let count = be32(data, 4) as usize;
    let mut pos = 8usize;
    let mut names = Vec::new();
    for _ in 0..count.saturating_mul(2) {
        if pos + 4 > data.len() {
            return Err(Error::Format("master.index: truncated".into()));
        }
        let length = le32(data, pos) as usize;
        pos += 4;
        if length > data.len() - pos {
            return Err(Error::Format("master.index: truncated name".into()));
        }
        let raw = &data[pos..pos + length];
        let end = raw.iter().position(|&b| b == 0).unwrap_or(raw.len());
        names.push(ascii_replace(&raw[..end]));
        pos += length;
    }
    let mut it = names.into_iter();
    let mut pairs = Vec::new();
    while let (Some(a), Some(b)) = (it.next(), it.next()) {
        pairs.push((a, b));
    }
    Ok(pairs)
}

/// Serialise pairs back to a `master.index` (inverse of [`parse_master_index`]).
pub fn build_master_index<S: AsRef<str>, T: AsRef<str>>(pairs: &[(S, T)]) -> Vec<u8> {
    let mut out = MAGIC.to_vec();
    out.extend_from_slice(&(pairs.len() as u32).to_be_bytes());
    for (a, b) in pairs {
        for name in [a.as_ref(), b.as_ref()] {
            out.extend_from_slice(&(name.len() as u32 + 1).to_le_bytes());
            out.extend_from_slice(name.as_bytes());
            out.push(0);
        }
    }
    out
}
