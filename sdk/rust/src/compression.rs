//! Raw DEFLATE with sync flush and exact-size packing (SPEC §4).

use flate2::{Compress, Compression, Decompress, FlushCompress, FlushDecompress, Status};

use crate::error::{Error, Result};

/// Trailer of a sync-flushed stream.
pub const SYNC_MARKER: [u8; 4] = [0x00, 0x00, 0xFF, 0xFF];
const MAX_STORED: usize = 0xFFFF;

/// Inflate a raw DEFLATE stream to exactly `usize` bytes.
///
/// A final block is not required (retail streams end with a sync flush).
pub fn inflate(stream: &[u8], usize: usize) -> Result<Vec<u8>> {
    let mut d = Decompress::new(false);
    // usize == 0 means "no limit" in the reference (zlib max_length = 0).
    let unlimited = usize == 0;
    let limit = if unlimited { usize::MAX } else { usize };
    let mut out = vec![0u8; limit.min(1 << 16)];
    let mut produced = 0usize;
    loop {
        let in_pos = d.total_in() as usize;
        let before = (d.total_in(), d.total_out());
        let status = d
            .decompress(
                &stream[in_pos..],
                &mut out[produced..],
                FlushDecompress::None,
            )
            .map_err(|e| Error::Format(format!("inflate failed: {e}")))?;
        produced = d.total_out() as usize;
        if status == Status::StreamEnd {
            break;
        }
        if produced == out.len() {
            if out.len() == limit {
                break;
            }
            let grown = out.len().saturating_mul(2).min(limit);
            out.resize(grown, 0);
            continue;
        }
        if (d.total_in(), d.total_out()) == before {
            break;
        }
    }
    out.truncate(produced);
    if produced != usize {
        return Err(Error::Format(format!(
            "inflate produced {produced} bytes, expected {usize}"
        )));
    }
    Ok(out)
}

/// Compress `data` (level 9) to a raw DEFLATE stream ending in a sync flush.
pub fn deflate_sync(data: &[u8]) -> Vec<u8> {
    let mut c = Compress::new(Compression::best(), false);
    let mut out = Vec::with_capacity(data.len() + data.len() / 8 + 64);
    loop {
        let consumed = c.total_in() as usize;
        c.compress_vec(&data[consumed..], &mut out, FlushCompress::Sync)
            .expect("deflate never fails on in-memory buffers");
        if c.total_in() as usize == data.len() && out.len() < out.capacity() {
            break;
        }
        let extra = out.capacity().max(64);
        out.reserve(extra);
    }
    out
}

/// `true` when the stream ends with `00 00 FF FF`.
pub fn is_sync_flushed(stream: &[u8]) -> bool {
    stream.len() >= 4 && stream[stream.len() - 4..] == SYNC_MARKER
}

fn push_stored_block(out: &mut Vec<u8>, chunk: &[u8]) {
    let n = chunk.len() as u16;
    out.push(0);
    out.extend_from_slice(&n.to_le_bytes());
    out.extend_from_slice(&(!n).to_le_bytes());
    out.extend_from_slice(chunk);
}

fn comment_padding(len: usize) -> Vec<u8> {
    let mut p = b"\n//".to_vec();
    p.resize(len, b' ');
    p
}

/// Produce a stream of exactly `csize` bytes (SPEC §4.1).
///
/// Returns `(stream, payload)` where `payload` is what the stream inflates to,
/// or `None` if it cannot fit. `pad` appends a Decl line comment and must only
/// be used for text assets.
pub fn deflate_exact(data: &[u8], csize: usize, pad: bool) -> Option<(Vec<u8>, Vec<u8>)> {
    let s = deflate_sync(data);
    if s.len() == csize {
        return Some((s, data.to_vec()));
    }
    if s.len() > csize || !pad {
        return None;
    }
    let r = csize - s.len();
    if r >= 13 {
        let n = (r - 5 + MAX_STORED + 5 - 1) / (MAX_STORED + 5);
        let total = r - 5 - 5 * n;
        let padding = comment_padding(total);
        let mut out = s;
        for i in 0..n {
            let a = (i * MAX_STORED).min(padding.len());
            let b = ((i + 1) * MAX_STORED).min(padding.len());
            push_stored_block(&mut out, &padding[a..b]);
        }
        out.push(0);
        out.extend_from_slice(&SYNC_MARKER);
        let mut payload = data.to_vec();
        payload.extend_from_slice(&padding);
        return Some((out, payload));
    }
    for k in 3..67 {
        let mut payload = data.to_vec();
        payload.extend_from_slice(&comment_padding(k));
        let s2 = deflate_sync(&payload);
        if s2.len() == csize {
            return Some((s2, payload));
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_sizes_across_stored_block_boundaries() {
        let data = b"{\n\tedit = {\n\t\tammoPerShot = 1;\n\t}\n}\n".repeat(20);
        let base = deflate_sync(&data).len();
        for delta in (1..20).chain([
            65539, 65540, 65541, 65545, 65546, 65549, 131079, 131080, 131081,
        ]) {
            let csize = base + delta;
            if let Some((stream, payload)) = deflate_exact(&data, csize, true) {
                assert_eq!(stream.len(), csize, "delta {delta}");
                assert!(is_sync_flushed(&stream));
                assert_eq!(inflate(&stream, payload.len()).unwrap(), payload);
            } else {
                assert!(delta < 13, "delta {delta} must fit");
            }
        }
    }
}
