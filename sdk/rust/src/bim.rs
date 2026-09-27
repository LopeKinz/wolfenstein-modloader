//! BIM images: header, decode to RGBA, RGBA8 encode (SPEC §8).

use crate::error::{be32, le16, le32, Error, Result};

/// Magic at offset 4.
pub const MAGIC: &[u8; 4] = b"\x09MIB";
/// Number of BE u32 header fields.
pub const HEADER_FIELDS: usize = 13;
/// Offset of the first mip.
pub const DATA_START: usize = 8 + HEADER_FIELDS * 4;

/// Format code: 8-bit RGBA.
pub const FORMAT_RGBA8: u32 = 3;
/// Format code: 8-bit alpha / luminance.
pub const FORMAT_A8: u32 = 5;
/// Format code: BC1 (DXT1).
pub const FORMAT_BC1: u32 = 10;
/// Format code: BC3 (DXT5).
pub const FORMAT_BC3: u32 = 11;

/// One mip level.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mip {
    /// Width in pixels.
    pub width: u32,
    /// Height in pixels.
    pub height: u32,
    /// Raw pixel/block data.
    pub data: Vec<u8>,
}

/// A parsed BIM image.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Bim {
    /// LE u32 hash / id at offset 0.
    pub hash: u32,
    /// The 13 header fields.
    pub header: Vec<u32>,
    /// Mip levels that were present in the data.
    pub mips: Vec<Mip>,
}

impl Bim {
    /// `h[0]`.
    pub fn texture_type(&self) -> u32 {
        self.header[0]
    }
    /// `h[1]`.
    pub fn width(&self) -> u32 {
        self.header[1]
    }
    /// `h[2]`.
    pub fn height(&self) -> u32 {
        self.header[2]
    }
    /// `h[3]`.
    pub fn depth(&self) -> u32 {
        self.header[3]
    }
    /// `h[4]`.
    pub fn mip_count(&self) -> u32 {
        self.header[4]
    }
    /// `h[6]`.
    pub fn format(&self) -> u32 {
        self.header[6]
    }
    /// Human-readable format name.
    pub fn format_name(&self) -> String {
        match self.format() {
            FORMAT_RGBA8 => "RGBA8".into(),
            FORMAT_A8 => "A8".into(),
            FORMAT_BC1 => "BC1".into(),
            FORMAT_BC3 => "BC3".into(),
            f => format!("unknown({f})"),
        }
    }
    /// `h[11]`.
    pub fn base_width(&self) -> u32 {
        self.header[11]
    }
    /// `h[12]`.
    pub fn base_height(&self) -> u32 {
        self.header[12]
    }
}

/// Parse a BIM file. Stops early (no error) when mips are missing.
pub fn parse_bim(data: &[u8]) -> Result<Bim> {
    if data.len() < DATA_START || &data[4..8] != MAGIC {
        return Err(Error::Format("BIM: bad magic".into()));
    }
    let hash = le32(data, 0);
    let header: Vec<u32> = (0..HEADER_FIELDS).map(|i| be32(data, 8 + 4 * i)).collect();
    let mut mips = Vec::new();
    let mut pos = DATA_START;
    for _ in 0..header[4] {
        if pos + 12 > data.len() {
            break;
        }
        let (w, h, size) = (
            be32(data, pos),
            be32(data, pos + 4),
            be32(data, pos + 8) as usize,
        );
        pos += 12;
        if size > data.len() - pos {
            break;
        }
        mips.push(Mip {
            width: w,
            height: h,
            data: data[pos..pos + size].to_vec(),
        });
        pos += size;
    }
    if mips.is_empty() {
        return Err(Error::Format("BIM: no mip levels".into()));
    }
    Ok(Bim { hash, header, mips })
}

/// Byte size of a `w×h` mip in format `fmt`.
pub fn expected_size(fmt: u32, w: u32, h: u32) -> Result<u64> {
    let (w, h) = (w as u64, h as u64);
    let (bw, bh) = ((w + 3) / 4, (h + 3) / 4);
    Ok(match fmt {
        FORMAT_RGBA8 => w * h * 4,
        FORMAT_A8 => w * h,
        FORMAT_BC1 => bw * bh * 8,
        FORMAT_BC3 => bw * bh * 16,
        _ => return Err(Error::Format(format!("BIM: unsupported format code {fmt}"))),
    })
}

fn rgb565(c: u16) -> [u32; 3] {
    let (r, g, b) = ((c >> 11) as u32 & 31, (c >> 5) as u32 & 63, c as u32 & 31);
    [
        (r << 3) | (r >> 2),
        (g << 2) | (g >> 4),
        (b << 3) | (b >> 2),
    ]
}

fn color_palette(block: &[u8], four_color: bool) -> ([[u8; 4]; 4], u32) {
    let (c0, c1) = (le16(block, 0), le16(block, 2));
    let (p0, p1) = (rgb565(c0), rgb565(c1));
    let mix = |f: &dyn Fn(u32, u32) -> u32| -> [u8; 4] {
        [
            f(p0[0], p1[0]) as u8,
            f(p0[1], p1[1]) as u8,
            f(p0[2], p1[2]) as u8,
            255,
        ]
    };
    let (p2, p3) = if c0 > c1 || four_color {
        (mix(&|a, b| (2 * a + b) / 3), mix(&|a, b| (a + 2 * b) / 3))
    } else {
        (mix(&|a, b| (a + b) / 2), [0, 0, 0, 0])
    };
    let full = |p: [u32; 3]| [p[0] as u8, p[1] as u8, p[2] as u8, 255];
    ([full(p0), full(p1), p2, p3], le32(block, 4))
}

fn alpha_palette(block: &[u8]) -> ([u8; 8], u64) {
    let (a0, a1) = (block[0] as u32, block[1] as u32);
    let mut pal = [a0 as u8, a1 as u8, 0, 0, 0, 0, 0, 0];
    if a0 > a1 {
        for i in 1..7 {
            pal[i + 1] = (((7 - i as u32) * a0 + i as u32 * a1) / 7) as u8;
        }
    } else {
        for i in 1..5 {
            pal[i + 1] = (((5 - i as u32) * a0 + i as u32 * a1) / 5) as u8;
        }
        pal[6] = 0;
        pal[7] = 255;
    }
    let mut bits = 0u64;
    for (k, &b) in block[2..8].iter().enumerate() {
        bits |= (b as u64) << (8 * k);
    }
    (pal, bits)
}

fn decode_blocks(data: &[u8], w: usize, h: usize, bc3: bool) -> Vec<u8> {
    let mut out = vec![0u8; w * h * 4];
    let (bw, bh) = ((w + 3) / 4, (h + 3) / 4);
    let size = if bc3 { 16 } else { 8 };
    for by in 0..bh {
        for bx in 0..bw {
            let off = (by * bw + bx) * size;
            let blk = &data[off..off + size];
            let (apal, abits, pal, idx) = if bc3 {
                let (apal, abits) = alpha_palette(&blk[..8]);
                let (pal, idx) = color_palette(&blk[8..], true);
                (Some(apal), abits, pal, idx)
            } else {
                let (pal, idx) = color_palette(blk, false);
                (None, 0, pal, idx)
            };
            for t in 0..16usize {
                let (x, y) = (bx * 4 + (t & 3), by * 4 + (t >> 2));
                if x >= w || y >= h {
                    continue;
                }
                let mut px = pal[((idx >> (2 * t)) & 3) as usize];
                if let Some(apal) = apal {
                    px[3] = apal[((abits >> (3 * t)) & 7) as usize];
                }
                let o = (y * w + x) * 4;
                out[o..o + 4].copy_from_slice(&px);
            }
        }
    }
    out
}

/// Decode mip `mip` to `(width, height, rgba)`.
pub fn decode(bim: &Bim, mip: usize) -> Result<(u32, u32, Vec<u8>)> {
    let m = bim
        .mips
        .get(mip)
        .ok_or_else(|| Error::Format(format!("BIM: no mip {mip}")))?;
    let (w, h, fmt) = (m.width, m.height, bim.format());
    let need = expected_size(fmt, w, h)?;
    if (m.data.len() as u64) < need {
        return Err(Error::Format(format!(
            "BIM: mip {mip} has {} bytes, needs {need}",
            m.data.len()
        )));
    }
    let need = need as usize;
    let rgba = match fmt {
        FORMAT_RGBA8 => m.data[..need].to_vec(),
        FORMAT_A8 => m.data[..need]
            .iter()
            .flat_map(|&v| [v, v, v, 255])
            .collect(),
        _ => decode_blocks(&m.data, w as usize, h as usize, fmt == FORMAT_BC3),
    };
    Ok((w, h, rgba))
}

fn write_header(out: &mut Vec<u8>, hash: u32, header: &[u32]) -> Result<()> {
    if header.len() != HEADER_FIELDS {
        return Err(Error::Format(format!(
            "BIM: header needs {HEADER_FIELDS} fields, got {}",
            header.len()
        )));
    }
    out.extend_from_slice(&hash.to_le_bytes());
    out.extend_from_slice(MAGIC);
    for v in header {
        out.extend_from_slice(&v.to_be_bytes());
    }
    Ok(())
}

/// Build an RGBA8 BIM with one mip; hash and header come from `template`.
pub fn encode_rgba8(template: &Bim, width: u32, height: u32, rgba: &[u8]) -> Result<Vec<u8>> {
    if rgba.len() as u64 != width as u64 * height as u64 * 4 {
        return Err(Error::Format(
            "RGBA buffer size does not match dimensions".into(),
        ));
    }
    let mut header = template.header.clone();
    if header.len() != HEADER_FIELDS {
        return Err(Error::Format("BIM: template header is malformed".into()));
    }
    header[1] = width;
    header[11] = width;
    header[2] = height;
    header[12] = height;
    header[4] = 1;
    header[6] = FORMAT_RGBA8;
    build_bim(template.hash, &header, &[(width, height, rgba)])
}

/// Serialise a BIM from raw parts (tests and synthetic data).
pub fn build_bim(hash: u32, header: &[u32], mips: &[(u32, u32, &[u8])]) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    write_header(&mut out, hash, header)?;
    for (w, h, data) in mips {
        out.extend_from_slice(&w.to_be_bytes());
        out.extend_from_slice(&h.to_be_bytes());
        out.extend_from_slice(&(data.len() as u32).to_be_bytes());
        out.extend_from_slice(data);
    }
    Ok(out)
}
