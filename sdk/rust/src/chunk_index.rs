//! `base/chunkN.index` (SPEC §3).
//!
//! The index is never re-serialised: the original buffer is kept and only the
//! 12-byte offset/usize/csize triple of an entry is overwritten.

use crate::error::{be32, le32, Error, Result};
use crate::master_index::MAGIC;

/// Offset of the first entry.
pub const ENTRIES_START: usize = 0x2C;
const MAX_STRING: usize = 1024;

/// One resource entry of a chunk index.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    /// Ordinal within the index.
    pub index: usize,
    /// Resource type (`weapon`, `material`, …).
    pub type_: String,
    /// Resource name.
    pub name: String,
    /// Source path.
    pub path: String,
    /// Offset into `chunkN.resources`.
    pub offset: u32,
    /// Uncompressed size.
    pub usize: u32,
    /// Size on disk.
    pub csize: u32,
    /// Byte position of the entry in the index.
    pub start: usize,
    /// Byte position of the offset/usize/csize triple.
    pub triple_offset: usize,
    /// Bytes after the triple up to the next entry, preserved verbatim.
    pub trailer: Vec<u8>,
}

impl Entry {
    /// `"type:name"`.
    pub fn key(&self) -> String {
        format!("{}:{}", self.type_, self.name)
    }

    /// `csize != usize`.
    pub fn compressed(&self) -> bool {
        self.csize != self.usize
    }

    /// Offset into the streamed audio container (`sample` entries, SPEC §3.3).
    pub fn stream_offset(&self) -> Option<u32> {
        if self.trailer.len() < 24 {
            None
        } else {
            Some(be32(&self.trailer, 20))
        }
    }
}

fn string_at(buf: &[u8], pos: usize) -> Option<(&str, usize)> {
    if pos + 4 > buf.len() {
        return None;
    }
    let n = le32(buf, pos) as usize;
    if !(1..=MAX_STRING).contains(&n) || pos + 4 + n > buf.len() {
        return None;
    }
    let raw = &buf[pos + 4..pos + 4 + n];
    if raw.iter().any(|&b| !(0x20..=0x7E).contains(&b)) {
        return None;
    }
    Some((std::str::from_utf8(raw).ok()?, pos + 4 + n))
}

struct Candidate<'a> {
    strings: [&'a str; 3],
    triple: usize,
    offset: u32,
    usize: u32,
    csize: u32,
}

fn plausible(buf: &[u8], pos: usize, resources_size: Option<u64>) -> Option<Candidate<'_>> {
    let mut strings = [""; 3];
    let mut p = pos;
    for s in strings.iter_mut() {
        let (text, next) = string_at(buf, p)?;
        *s = text;
        p = next;
    }
    if p + 12 > buf.len() {
        return None;
    }
    let (offset, usize, csize) = (be32(buf, p), be32(buf, p + 4), be32(buf, p + 8));
    if !(0 < csize && csize <= usize) {
        return None;
    }
    match resources_size {
        Some(r) if offset as u64 + csize as u64 > r => return None,
        None if offset < 16 => return None,
        _ => {}
    }
    Some(Candidate {
        strings,
        triple: p,
        offset,
        usize,
        csize,
    })
}

/// A parsed chunk index backed by its original bytes.
#[derive(Debug, Clone)]
pub struct ChunkIndex {
    /// The original (possibly patched) index bytes.
    pub buffer: Vec<u8>,
    /// Size of the matching `.resources` file, if known.
    pub resources_size: Option<u64>,
    /// Header counter A.
    pub counter_a: u32,
    /// Header counter B.
    pub counter_b: u32,
    /// Entries found by the plausibility scan (SPEC §3.1).
    pub entries: Vec<Entry>,
}

impl ChunkIndex {
    /// Parse an index. `resources_size` tightens the plausibility check.
    pub fn parse(data: &[u8], resources_size: Option<u64>) -> Result<ChunkIndex> {
        if data.len() < 4 || &data[..4] != MAGIC {
            return Err(Error::Format("chunk index: bad magic".into()));
        }
        if data.len() < ENTRIES_START {
            return Err(Error::Format("chunk index: truncated header".into()));
        }
        let mut ci = ChunkIndex {
            buffer: data.to_vec(),
            resources_size,
            counter_a: be32(data, 0x20),
            counter_b: be32(data, 0x24),
            entries: Vec::new(),
        };
        ci.scan();
        Ok(ci)
    }

    fn scan(&mut self) {
        let buf = &self.buffer;
        let mut pos = ENTRIES_START;
        let mut found: Vec<Entry> = Vec::new();
        while pos < buf.len() {
            match plausible(buf, pos, self.resources_size) {
                None => pos += 1,
                Some(c) => {
                    found.push(Entry {
                        index: found.len(),
                        type_: c.strings[0].to_string(),
                        name: c.strings[1].to_string(),
                        path: c.strings[2].to_string(),
                        offset: c.offset,
                        usize: c.usize,
                        csize: c.csize,
                        start: pos,
                        triple_offset: c.triple,
                        trailer: Vec::new(),
                    });
                    pos = c.triple + 12;
                }
            }
        }
        for i in 0..found.len() {
            let end = found.get(i + 1).map_or(buf.len(), |n| n.start);
            found[i].trailer = buf[found[i].triple_offset + 12..end].to_vec();
        }
        self.entries = found;
    }

    /// Number of entries.
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// `true` when no entry was found.
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// All entries with the given type and name.
    pub fn find(&self, type_: &str, name: &str) -> Vec<&Entry> {
        self.entries
            .iter()
            .filter(|e| e.type_ == type_ && e.name == name)
            .collect()
    }

    /// Overwrite the triple of entry number `entry` (its `index`) in the buffer.
    pub fn set_triple(&mut self, entry: usize, offset: u32, usize: u32, csize: u32) {
        let e = &mut self.entries[entry];
        let p = e.triple_offset;
        self.buffer[p..p + 4].copy_from_slice(&offset.to_be_bytes());
        self.buffer[p + 4..p + 8].copy_from_slice(&usize.to_be_bytes());
        self.buffer[p + 8..p + 12].copy_from_slice(&csize.to_be_bytes());
        e.offset = offset;
        e.usize = usize;
        e.csize = csize;
    }

    /// The index bytes (byte-identical to the input when nothing was patched).
    pub fn to_bytes(&self) -> Vec<u8> {
        self.buffer.clone()
    }
}

/// Input for [`build_chunk_index`].
#[derive(Debug, Clone, Copy)]
pub struct NewEntry<'a> {
    /// Resource type.
    pub type_: &'a str,
    /// Resource name.
    pub name: &'a str,
    /// Source path.
    pub path: &'a str,
    /// Offset into the resources file.
    pub offset: u32,
    /// Uncompressed size.
    pub usize: u32,
    /// Size on disk.
    pub csize: u32,
    /// Trailer bytes.
    pub trailer: &'a [u8],
}

/// Build a chunk index (tests and synthetic archives; game files are never
/// re-serialised). `counter_b` defaults to the number of entries.
pub fn build_chunk_index(
    entries: &[NewEntry<'_>],
    counter_a: u32,
    counter_b: Option<u32>,
) -> Vec<u8> {
    let mut body = Vec::new();
    for e in entries {
        for s in [e.type_, e.name, e.path] {
            body.extend_from_slice(&(s.len() as u32).to_le_bytes());
            body.extend_from_slice(s.as_bytes());
        }
        for v in [e.offset, e.usize, e.csize] {
            body.extend_from_slice(&v.to_be_bytes());
        }
        body.extend_from_slice(e.trailer);
    }
    let counter_b = counter_b.unwrap_or(entries.len() as u32);
    let total = ENTRIES_START + body.len();
    let mut out = MAGIC.to_vec();
    out.extend_from_slice(&((total - 32) as u32).to_be_bytes());
    out.extend_from_slice(&[0u8; 24]);
    for v in [counter_a, counter_b, 0] {
        out.extend_from_slice(&v.to_be_bytes());
    }
    out.extend_from_slice(&body);
    out
}
