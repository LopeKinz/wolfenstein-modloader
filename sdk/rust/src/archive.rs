//! Game directory access: find, read, patch in place, rebuild (SPEC §5).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use crate::chunk_index::{ChunkIndex, Entry};
use crate::compression::{deflate_exact, deflate_sync, inflate, is_sync_flushed};
use crate::error::{Error, Result};
use crate::master_index::{parse_master_index, MAGIC};

/// Header of a `.resources` file: magic + 12 zero bytes.
pub const RESOURCES_HEADER_LEN: usize = 16;

/// `true` if `data` is valid UTF-8 without NUL bytes (the `pad=auto` rule).
pub fn looks_textual(data: &[u8]) -> bool {
    !data.contains(&0) && std::str::from_utf8(data).is_ok()
}

/// One `chunkN.index` / `chunkN.resources` pair.
#[derive(Debug, Clone)]
pub struct Chunk {
    /// Index file name as listed in `master.index`.
    pub index_name: String,
    /// Resources file name as listed in `master.index`.
    pub resources_name: String,
    /// Full path of the index file.
    pub index_path: PathBuf,
    /// Full path of the resources file.
    pub resources_path: PathBuf,
    /// The parsed index.
    pub index: ChunkIndex,
}

impl Chunk {
    /// Entries of the index.
    pub fn entries(&self) -> &[Entry] {
        &self.index.entries
    }

    /// Write the (patched) index back to disk.
    pub fn save_index(&self) -> Result<()> {
        fs::write(&self.index_path, &self.index.buffer)?;
        Ok(())
    }
}

/// An asset occurrence: indices into [`Game::chunks`] and that chunk's entries.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Occurrence {
    /// Index into [`Game::chunks`].
    pub chunk: usize,
    /// Index into the chunk's entries (equals [`Entry::index`]).
    pub entry: usize,
}

/// A game installation (the directory that contains `base/`).
#[derive(Debug, Clone)]
pub struct Game {
    /// The game directory.
    pub root: PathBuf,
    /// `root/base`.
    pub base: PathBuf,
    /// Pairs from `master.index`.
    pub pairs: Vec<(String, String)>,
    /// Chunks whose index and resources files both exist.
    pub chunks: Vec<Chunk>,
}

impl Game {
    /// Open a game directory.
    pub fn open<P: AsRef<Path>>(root: P) -> Result<Game> {
        let root = root.as_ref().to_path_buf();
        let base = root.join("base");
        let pairs = parse_master_index(&fs::read(base.join("master.index"))?)?;
        let mut chunks = Vec::new();
        for (idx_name, res_name) in &pairs {
            let ip = base.join(idx_name);
            let rp = base.join(res_name);
            if !ip.exists() || !rp.exists() {
                continue;
            }
            let data = fs::read(&ip)?;
            let size = fs::metadata(&rp)?.len();
            let index = ChunkIndex::parse(&data, Some(size))?;
            chunks.push(Chunk {
                index_name: idx_name.clone(),
                resources_name: res_name.clone(),
                index_path: ip,
                resources_path: rp,
                index,
            });
        }
        Ok(Game {
            root,
            base,
            pairs,
            chunks,
        })
    }

    /// Every occurrence in chunk order.
    pub fn occurrences(&self) -> Vec<Occurrence> {
        self.chunks
            .iter()
            .enumerate()
            .flat_map(|(ci, c)| {
                (0..c.entries().len()).map(move |ei| Occurrence {
                    chunk: ci,
                    entry: ei,
                })
            })
            .collect()
    }

    /// All occurrences of `type:name`, in chunk order.
    pub fn find(&self, type_: &str, name: &str) -> Vec<Occurrence> {
        let mut out = Vec::new();
        for (ci, c) in self.chunks.iter().enumerate() {
            for e in c.index.find(type_, name) {
                out.push(Occurrence {
                    chunk: ci,
                    entry: e.index,
                });
            }
        }
        out
    }

    /// Distinct asset keys in order of first appearance.
    pub fn keys(&self) -> Vec<String> {
        let mut seen = HashSet::new();
        let mut out = Vec::new();
        for c in &self.chunks {
            for e in c.entries() {
                let k = e.key();
                if seen.insert(k.clone()) {
                    out.push(k);
                }
            }
        }
        out
    }

    /// The chunk of an occurrence.
    pub fn chunk(&self, occ: &Occurrence) -> &Chunk {
        &self.chunks[occ.chunk]
    }

    /// The entry of an occurrence.
    pub fn entry(&self, occ: &Occurrence) -> &Entry {
        &self.chunks[occ.chunk].index.entries[occ.entry]
    }

    // ------------------------------------------------------------ reading

    /// The on-disk slot bytes.
    pub fn read_raw(&self, occ: &Occurrence) -> Result<Vec<u8>> {
        let e = self.entry(occ);
        let mut f = File::open(&self.chunk(occ).resources_path)?;
        f.seek(SeekFrom::Start(e.offset as u64))?;
        let mut data = Vec::with_capacity(e.csize as usize);
        f.take(e.csize as u64).read_to_end(&mut data)?;
        if data.len() != e.csize as usize {
            return Err(Error::Format(format!("{}: slot truncated", e.key())));
        }
        Ok(data)
    }

    /// The decoded payload (inflated if compressed).
    pub fn read(&self, occ: &Occurrence) -> Result<Vec<u8>> {
        let raw = self.read_raw(occ)?;
        let e = self.entry(occ);
        if e.compressed() {
            inflate(&raw, e.usize as usize)
        } else {
            Ok(raw)
        }
    }

    /// The payload of the first occurrence of `type:name`, if any.
    pub fn read_asset(&self, type_: &str, name: &str) -> Result<Option<Vec<u8>>> {
        match self.find(type_, name).first() {
            Some(o) => self.read(o).map(Some),
            None => Ok(None),
        }
    }

    // ------------------------------------------------------------ writing

    /// Overwrite the entry's slot in place (SPEC §5.1). `pad = None` means
    /// auto ([`looks_textual`]). Returns the previous slot bytes.
    pub fn write_in_place(
        &mut self,
        occ: &Occurrence,
        data: &[u8],
        pad: Option<bool>,
    ) -> Result<Vec<u8>> {
        let original = self.read_raw(occ)?;
        let e = self.entry(occ).clone();
        let (stream, usize) = if !e.compressed() {
            if data.len() != e.csize as usize {
                return Err(Error::NoFit(format!(
                    "{}: stored entry needs exactly {} bytes, got {}",
                    e.key(),
                    e.csize,
                    data.len()
                )));
            }
            (data.to_vec(), e.usize)
        } else {
            let pad = pad.unwrap_or_else(|| looks_textual(data));
            let (stream, payload) =
                deflate_exact(data, e.csize as usize, pad).ok_or_else(|| {
                    Error::NoFit(format!(
                        "{}: does not fit into its {}-byte slot",
                        e.key(),
                        e.csize
                    ))
                })?;
            let usize = u32::try_from(payload.len())
                .map_err(|_| Error::NoFit(format!("{}: payload too large", e.key())))?;
            (stream, usize)
        };
        let chunk = &mut self.chunks[occ.chunk];
        let mut f = OpenOptions::new().write(true).open(&chunk.resources_path)?;
        f.seek(SeekFrom::Start(e.offset as u64))?;
        f.write_all(&stream)?;
        drop(f);
        if usize != e.usize {
            chunk.index.set_triple(e.index, e.offset, usize, e.csize);
            chunk.save_index()?;
        }
        Ok(original)
    }

    /// Write `data` into every occurrence of `type:name`.
    /// Returns `(occurrence, previous slot bytes)`.
    pub fn write_asset(
        &mut self,
        type_: &str,
        name: &str,
        data: &[u8],
    ) -> Result<Vec<(Occurrence, Vec<u8>)>> {
        let occs = self.find(type_, name);
        if occs.is_empty() {
            return Err(Error::Io(io::Error::new(
                io::ErrorKind::NotFound,
                format!("{type_}:{name}: asset not found"),
            )));
        }
        let mut out = Vec::new();
        for o in occs {
            let orig = self.write_in_place(&o, data, None)?;
            out.push((o, orig));
        }
        Ok(out)
    }

    /// Rewrite chunk `chunk`'s `.resources` in original offset order
    /// (SPEC §5.2). `replacements` maps entry index to new payload. `dest`
    /// defaults to the chunk's resources file. The index is saved afterwards.
    pub fn rebuild(
        &mut self,
        chunk: usize,
        replacements: &HashMap<usize, Vec<u8>>,
        dest: Option<&Path>,
    ) -> Result<()> {
        let c = &mut self.chunks[chunk];
        let dest = dest.map_or_else(|| c.resources_path.clone(), Path::to_path_buf);
        let mut tmp_name = dest.clone().into_os_string();
        tmp_name.push(".tmp");
        let tmp = PathBuf::from(tmp_name);

        let mut slots: BTreeMap<(u32, u32), Vec<usize>> = BTreeMap::new();
        for e in c.entries() {
            slots.entry((e.offset, e.csize)).or_default().push(e.index);
        }
        let mut src = File::open(&c.resources_path)?;
        let mut out = io::BufWriter::new(File::create(&tmp)?);
        let mut header = MAGIC.to_vec();
        header.resize(RESOURCES_HEADER_LEN, 0);
        out.write_all(&header)?;
        let mut cursor = RESOURCES_HEADER_LEN as u64;
        let mut updates = Vec::new();
        for ((offset, csize), group) in &slots {
            let repl = group.iter().find_map(|i| replacements.get(i));
            let (payload, usize) = match repl {
                None => {
                    src.seek(SeekFrom::Start(*offset as u64))?;
                    let mut buf = Vec::with_capacity(*csize as usize);
                    (&mut src).take(*csize as u64).read_to_end(&mut buf)?;
                    (buf, c.index.entries[group[0]].usize as u64)
                }
                Some(r) => {
                    let s = deflate_sync(r);
                    if s.len() < r.len() {
                        (s, r.len() as u64)
                    } else {
                        (r.clone(), r.len() as u64)
                    }
                }
            };
            for &i in group {
                updates.push((i, cursor, usize, payload.len() as u64));
            }
            out.write_all(&payload)?;
            cursor += payload.len() as u64;
            let pad = (16 - cursor % 16) % 16;
            out.write_all(&vec![0u8; pad as usize])?;
            cursor += pad;
        }
        out.flush()?;
        drop(out);
        drop(src);
        fs::rename(&tmp, &dest)?;
        let too_big = || Error::Format("rebuilt archive exceeds 4 GiB".into());
        for (i, off, usize, csize) in updates {
            let off = u32::try_from(off).map_err(|_| too_big())?;
            let usize = u32::try_from(usize).map_err(|_| too_big())?;
            let csize = u32::try_from(csize).map_err(|_| too_big())?;
            c.index.set_triple(i, off, usize, csize);
        }
        c.index.resources_size = Some(fs::metadata(&dest)?.len());
        c.save_index()
    }
}

/// Invariant violations of a chunk (empty = valid): non-ascending or
/// unaligned offsets, slots past the end of the file, compressed payloads not
/// sync-flushed.
pub fn validate_chunk(chunk: &Chunk) -> Result<Vec<String>> {
    validate_chunk_with(chunk, true)
}

/// [`validate_chunk`] with optional stream checks.
pub fn validate_chunk_with(chunk: &Chunk, check_streams: bool) -> Result<Vec<String>> {
    let mut problems = Vec::new();
    let size = fs::metadata(&chunk.resources_path)?.len();
    let mut f = File::open(&chunk.resources_path)?;
    let mut sorted: Vec<&Entry> = chunk.entries().iter().collect();
    sorted.sort_by_key(|e| (e.offset, e.index));
    let mut last: i64 = -1;
    let mut seen = HashSet::new();
    for e in sorted {
        if !seen.insert((e.offset, e.csize)) {
            continue;
        }
        if e.offset as i64 <= last {
            problems.push(format!("{}: offset {} not ascending", e.key(), e.offset));
        }
        last = e.offset as i64;
        if e.offset % 16 != 0 {
            problems.push(format!(
                "{}: offset {} not 16-byte aligned",
                e.key(),
                e.offset
            ));
        }
        if e.offset as u64 + e.csize as u64 > size {
            problems.push(format!("{}: slot exceeds resources file", e.key()));
            continue;
        }
        if check_streams && e.compressed() {
            f.seek(SeekFrom::Start(e.offset as u64))?;
            let mut buf = Vec::with_capacity(e.csize as usize);
            (&mut f).take(e.csize as u64).read_to_end(&mut buf)?;
            if !is_sync_flushed(&buf) {
                problems.push(format!("{}: stream not sync-flushed", e.key()));
            }
        }
    }
    Ok(problems)
}
