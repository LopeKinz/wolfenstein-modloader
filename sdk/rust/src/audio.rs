//! Audio descriptors and streamed containers (SPEC §10).

use std::io::{Read, Seek, SeekFrom};

use crate::error::{ascii_replace, be32, le16, le32, Error, Result};

/// Prefix of assets stored in `english.streamed`.
pub const ENGLISH_PREFIX: &str = "sound/vo/english/";

/// One language entry of a `bsnf` descriptor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SampleInfo {
    /// Language name (`""` for single-language descriptors).
    pub language: String,
    /// Sample hash.
    pub hash: u32,
    /// Ogg stream length in bytes.
    pub length: u32,
    /// Length in PCM samples.
    pub granule: u32,
    /// WAVE format tag (0x674F).
    pub format_tag: u16,
    /// Channel count.
    pub channels: u16,
    /// Sample rate in Hz.
    pub sample_rate: u32,
    /// Average bytes per second.
    pub avg_bytes_per_sec: u32,
    /// Block alignment.
    pub block_align: u16,
    /// Bits per sample.
    pub bits_per_sample: u16,
}

impl SampleInfo {
    /// `granule / sample_rate` in seconds (0 when the rate is 0).
    pub fn duration(&self) -> f64 {
        if self.sample_rate == 0 {
            0.0
        } else {
            self.granule as f64 / self.sample_rate as f64
        }
    }
}

fn block(data: &[u8], off: usize, language: String) -> Result<SampleInfo> {
    if off.saturating_add(42) > data.len() {
        return Err(Error::Format("bsnf: truncated sample block".into()));
    }
    Ok(SampleInfo {
        language,
        hash: be32(data, off),
        granule: be32(data, off + 8),
        length: be32(data, off + 16),
        format_tag: le16(data, off + 20),
        channels: le16(data, off + 22),
        sample_rate: le32(data, off + 24),
        avg_bytes_per_sec: le32(data, off + 28),
        block_align: le16(data, off + 32),
        bits_per_sample: le16(data, off + 34),
    })
}

/// Parse a `bsnf` sound descriptor.
pub fn parse_bsnf(data: &[u8]) -> Result<Vec<SampleInfo>> {
    if data.len() < 8 || &data[..4] != b"bsnf" {
        return Err(Error::Format("bsnf: bad magic".into()));
    }
    let count = be32(data, 4) as usize;
    if count == 1 {
        return Ok(vec![block(data, 0x20, String::new())?]);
    }
    let mut out = Vec::new();
    for i in 0..count {
        let p = 8 + i * 24;
        if p + 24 > data.len() {
            return Err(Error::Format("bsnf: truncated language table".into()));
        }
        let raw = &data[p..p + 16];
        let end = raw.iter().position(|&b| b == 0).unwrap_or(16);
        let off = be32(data, p + 20) as usize;
        out.push(block(data, off, ascii_replace(&raw[..end]))?);
    }
    Ok(out)
}

/// The streamed container holding `asset_name`'s Ogg data.
pub fn stream_container_for(asset_name: &str) -> &'static str {
    if asset_name.starts_with(ENGLISH_PREFIX) {
        "english.streamed"
    } else {
        "streamed.resources"
    }
}

fn walk<F: FnMut(u64, usize) -> Result<Vec<u8>>>(mut read: F, offset: u64) -> Result<u64> {
    let mut pos = offset;
    loop {
        let head = read(pos, 27)?;
        if head.len() < 27 || &head[..4] != b"OggS" {
            return Err(Error::Format(format!("Ogg: no page at offset {pos}")));
        }
        let (flags, nseg) = (head[5], head[26] as usize);
        let table = read(pos + 27, nseg)?;
        if table.len() < nseg {
            return Err(Error::Format("Ogg: truncated segment table".into()));
        }
        pos += 27 + nseg as u64 + table.iter().map(|&b| b as u64).sum::<u64>();
        if flags & 0x04 != 0 {
            return Ok(pos - offset);
        }
    }
}

/// Walk Ogg pages from `offset` up to and including the EOS page; returns
/// the total byte length.
pub fn ogg_stream_length(data: &[u8], offset: u64) -> Result<u64> {
    walk(
        |pos, n| {
            let start = usize::try_from(pos).unwrap_or(usize::MAX).min(data.len());
            let end = start.saturating_add(n).min(data.len());
            Ok(data[start..end].to_vec())
        },
        offset,
    )
}

/// [`ogg_stream_length`] over a seekable reader (e.g. an open container file).
pub fn ogg_stream_length_reader<R: Read + Seek>(src: &mut R, offset: u64) -> Result<u64> {
    walk(
        |pos, n| {
            src.seek(SeekFrom::Start(pos))?;
            let mut buf = Vec::with_capacity(n);
            src.by_ref().take(n as u64).read_to_end(&mut buf)?;
            Ok(buf)
        },
        offset,
    )
}
