//! Save-game `MD5_BlockChecksum` header (SPEC §11).

use md5::{Digest, Md5};

use crate::error::{le32, Error, Result};

/// `BE u32(w0 ^ w1 ^ w2 ^ w3)` where `w` are the LE u32 words of `MD5(payload)`.
pub fn save_checksum(payload: &[u8]) -> [u8; 4] {
    let d = Md5::digest(payload);
    let x = le32(&d, 0) ^ le32(&d, 4) ^ le32(&d, 8) ^ le32(&d, 12);
    x.to_be_bytes()
}

/// `true` if `file[0..4] == save_checksum(file[4..])`.
pub fn verify_save(data: &[u8]) -> bool {
    data.len() >= 4 && data[..4] == save_checksum(&data[4..])
}

/// Return the file with a corrected checksum header.
pub fn fix_save(data: &[u8]) -> Result<Vec<u8>> {
    if data.len() < 4 {
        return Err(Error::Format(
            "save file shorter than its 4-byte header".into(),
        ));
    }
    let mut out = save_checksum(&data[4..]).to_vec();
    out.extend_from_slice(&data[4..]);
    Ok(out)
}
