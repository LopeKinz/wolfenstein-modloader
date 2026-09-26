//! Minimal RGBA PNG encoder/decoder (SPEC §9).

use std::io::{Read, Write};

use flate2::read::ZlibDecoder;
use flate2::write::ZlibEncoder;
use flate2::Compression;

use crate::error::{be32, Error, Result};

/// PNG file signature.
pub const SIGNATURE: &[u8; 8] = b"\x89PNG\r\n\x1a\n";

fn crc_table() -> [u32; 256] {
    let mut t = [0u32; 256];
    for (n, slot) in t.iter_mut().enumerate() {
        let mut c = n as u32;
        for _ in 0..8 {
            c = if c & 1 != 0 {
                0xEDB8_8320 ^ (c >> 1)
            } else {
                c >> 1
            };
        }
        *slot = c;
    }
    t
}

/// CRC-32 (ISO-HDLC, as used by PNG and zlib).
pub fn crc32(data: &[u8]) -> u32 {
    let t = crc_table();
    let mut c = 0xFFFF_FFFFu32;
    for &b in data {
        c = t[((c ^ b as u32) & 0xFF) as usize] ^ (c >> 8);
    }
    c ^ 0xFFFF_FFFF
}

fn push_chunk(out: &mut Vec<u8>, kind: &[u8; 4], data: &[u8]) {
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    let start = out.len();
    out.extend_from_slice(kind);
    out.extend_from_slice(data);
    let crc = crc32(&out[start..]);
    out.extend_from_slice(&crc.to_be_bytes());
}

/// Encode 8-bit RGBA pixels as a PNG (filter 0, one IDAT).
pub fn encode_png(width: u32, height: u32, rgba: &[u8]) -> Result<Vec<u8>> {
    if rgba.len() as u64 != width as u64 * height as u64 * 4 {
        return Err(Error::Format(
            "RGBA buffer size does not match dimensions".into(),
        ));
    }
    let stride = width as usize * 4;
    let mut raw = Vec::with_capacity((stride + 1) * height as usize);
    for y in 0..height as usize {
        raw.push(0);
        raw.extend_from_slice(&rgba[y * stride..(y + 1) * stride]);
    }
    let mut z = ZlibEncoder::new(Vec::new(), Compression::best());
    z.write_all(&raw)?;
    let idat = z.finish()?;
    let mut ihdr = Vec::with_capacity(13);
    ihdr.extend_from_slice(&width.to_be_bytes());
    ihdr.extend_from_slice(&height.to_be_bytes());
    ihdr.extend_from_slice(&[8, 6, 0, 0, 0]);
    let mut out = SIGNATURE.to_vec();
    push_chunk(&mut out, b"IHDR", &ihdr);
    push_chunk(&mut out, b"IDAT", &idat);
    push_chunk(&mut out, b"IEND", &[]);
    Ok(out)
}

fn paeth(a: i32, b: i32, c: i32) -> i32 {
    let p = a + b - c;
    let (pa, pb, pc) = ((p - a).abs(), (p - b).abs(), (p - c).abs());
    if pa <= pb && pa <= pc {
        a
    } else if pb <= pc {
        b
    } else {
        c
    }
}

/// Decode a non-interlaced 8-bit PNG (grey, RGB, grey+alpha, RGBA) to RGBA.
pub fn decode_png(data: &[u8]) -> Result<(u32, u32, Vec<u8>)> {
    if data.len() < 8 || &data[..8] != SIGNATURE {
        return Err(Error::Format("PNG: bad signature".into()));
    }
    let mut pos = 8usize;
    let mut idat = Vec::new();
    let mut ihdr: Option<&[u8]> = None;
    while pos + 8 <= data.len() {
        let n = be32(data, pos) as usize;
        let kind = &data[pos + 4..pos + 8];
        let body_end = (pos + 8).saturating_add(n).min(data.len());
        let body = &data[pos + 8..body_end];
        pos = pos.saturating_add(12).saturating_add(n);
        match kind {
            b"IHDR" => ihdr = Some(body),
            b"IDAT" => idat.extend_from_slice(body),
            b"IEND" => break,
            _ => {}
        }
    }
    let ihdr = ihdr.ok_or_else(|| Error::Format("PNG: missing IHDR".into()))?;
    if ihdr.len() != 13 {
        return Err(Error::Format("PNG: bad IHDR".into()));
    }
    let (w, h) = (be32(ihdr, 0), be32(ihdr, 4));
    let (depth, ctype, interlace) = (ihdr[8], ihdr[9], ihdr[12]);
    let channels = match ctype {
        0 => 1usize,
        2 => 3,
        4 => 2,
        6 => 4,
        _ => 0,
    };
    if depth != 8 || channels == 0 || interlace != 0 {
        return Err(Error::Format(format!(
            "PNG: unsupported (depth {depth}, colour type {ctype}, interlace {interlace})"
        )));
    }
    let mut raw = Vec::new();
    ZlibDecoder::new(&idat[..])
        .read_to_end(&mut raw)
        .map_err(|e| Error::Format(format!("PNG: {e}")))?;
    let (wu, hu) = (w as usize, h as usize);
    let bpp = channels;
    let stride = wu * channels;
    if (raw.len() as u64) < hu as u64 * (stride as u64 + 1) {
        return Err(Error::Format("PNG: image data truncated".into()));
    }
    let mut prev = vec![0u8; stride];
    let mut pixels = Vec::with_capacity(stride * hu);
    for y in 0..hu {
        let base = y * (stride + 1);
        let f = raw[base];
        let mut line = raw[base + 1..base + 1 + stride].to_vec();
        if f > 4 && stride > 0 {
            return Err(Error::Format(format!("PNG: bad filter {f}")));
        }
        for i in 0..stride {
            let a = if i >= bpp { line[i - bpp] } else { 0 };
            let b = prev[i];
            let c = if i >= bpp { prev[i - bpp] } else { 0 };
            line[i] = match f {
                1 => line[i].wrapping_add(a),
                2 => line[i].wrapping_add(b),
                3 => line[i].wrapping_add(((a as u16 + b as u16) >> 1) as u8),
                4 => line[i].wrapping_add(paeth(a as i32, b as i32, c as i32) as u8),
                _ => line[i],
            };
        }
        pixels.extend_from_slice(&line);
        prev = line;
    }
    if channels == 4 {
        return Ok((w, h, pixels));
    }
    let mut out = Vec::with_capacity(wu * hu * 4);
    for px in pixels.chunks_exact(channels) {
        match channels {
            3 => out.extend_from_slice(&[px[0], px[1], px[2], 255]),
            1 => out.extend_from_slice(&[px[0], px[0], px[0], 255]),
            _ => out.extend_from_slice(&[px[0], px[0], px[0], px[1]]),
        }
    }
    Ok((w, h, out))
}
