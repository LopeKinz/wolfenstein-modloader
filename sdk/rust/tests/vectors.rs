//! Run the shared test vectors (`spec/vectors`) against the Rust binding.
//!
//! The vector directory defaults to `../../spec/vectors` relative to this
//! crate and can be overridden with the `WOLFSDK_VECTORS` environment variable.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

use serde_json::Value as Json;
use sha2::{Digest, Sha256};
use wolfsdk::decl::format_value;
use wolfsdk::{Decl, Error, Game, Kind, Mod, Node, Value};

fn vec_dir() -> PathBuf {
    match std::env::var_os("WOLFSDK_VECTORS") {
        Some(p) => PathBuf::from(p),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../../spec/vectors"),
    }
}

fn load(rel: &str) -> Json {
    serde_json::from_str(&read_text(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"))
}

fn read_bytes(rel: &str) -> Vec<u8> {
    fs::read(vec_dir().join(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"))
}

fn read_text(rel: &str) -> String {
    String::from_utf8(read_bytes(rel)).expect("utf-8")
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

fn sha(b: &[u8]) -> String {
    hex(&Sha256::digest(b))
}

fn s(v: &Json) -> &str {
    v.as_str().expect("string")
}

fn u(v: &Json) -> u64 {
    v.as_u64().expect("unsigned")
}

fn arr(v: &Json) -> &Vec<Json> {
    v.as_array().expect("array")
}

fn json_value(v: &Json) -> Value {
    match v {
        Json::String(x) => Value::Str(x.clone()),
        Json::Bool(b) => Value::Bool(*b),
        Json::Number(n) => Value::Num(n.as_f64().unwrap()),
        other => panic!("unsupported value {other}"),
    }
}

/// `payload[len(data):]` matches `^(\n// *)?$`.
fn is_comment_padding(rest: &[u8]) -> bool {
    rest.is_empty() || (rest.starts_with(b"\n//") && rest[3..].iter().all(|&c| c == b' '))
}

fn is_decl_err<T: std::fmt::Debug>(r: wolfsdk::Result<T>) -> bool {
    matches!(r, Err(Error::Decl(_)))
}

fn is_format_err<T: std::fmt::Debug>(r: wolfsdk::Result<T>) -> bool {
    matches!(r, Err(Error::Format(_)))
}

// ---------------------------------------------------------------- decl

#[test]
fn decl_parse() {
    let v = load("decl.json");
    for case in arr(&v["parse"]) {
        let file = s(&case["file"]);
        let text = read_text(&format!("decl/{file}"));
        let d = Decl::parse(&text).unwrap();
        assert_eq!(d.text(), text);
        let got: Vec<Json> = d
            .paths()
            .into_iter()
            .map(|(p, k, r)| serde_json::json!([p, k.as_str(), r]))
            .collect();
        assert_eq!(&got, arr(&case["paths"]), "{file}");
        let blocks: Vec<Json> = d
            .nodes()
            .iter()
            .filter(|n| n.kind == Kind::Block)
            .map(|n| Json::String(n.path_str()))
            .collect();
        assert_eq!(&blocks, arr(&case["blocks"]), "{file}");
    }
}

#[test]
fn decl_edits() {
    for case in arr(&load("decl.json")["edits"]) {
        let mut d = Decl::parse(&read_text(&format!("decl/{}", s(&case["file"])))).unwrap();
        for pair in arr(&case["set"]) {
            d.set(s(&pair[0]), json_value(&pair[1]))
                .unwrap_or_else(|e| panic!("{case}: {e}"));
        }
        let want = read_text(&format!("decl/{}", s(&case["expect"])));
        assert_eq!(d.text(), want, "{case}");
    }
}

#[test]
fn decl_errors() {
    let v = load("decl.json");
    for t in arr(&v["parseErrors"]) {
        assert!(is_decl_err(Decl::parse(s(t))), "{t}");
    }
    for c in arr(&v["setErrors"]) {
        let mut d = Decl::parse(&read_text(&format!("decl/{}", s(&c["file"])))).unwrap();
        let r = d.set(s(&c["path"]), json_value(&c["value"])).map(|_| ());
        assert!(is_decl_err(r), "{c}");
    }
    for p in arr(&v["pathErrors"]) {
        assert!(is_decl_err(wolfsdk::parse_path(s(p))), "{p}");
    }
}

#[test]
fn decl_paths() {
    for pair in arr(&load("decl.json")["paths"]) {
        let text = s(&pair[0]);
        let segs: Vec<String> = arr(&pair[1]).iter().map(|x| s(x).to_string()).collect();
        assert_eq!(wolfsdk::parse_path(text).unwrap(), segs);
        assert_eq!(wolfsdk::format_path(&segs), text);
    }
}

#[test]
fn decl_format() {
    for c in arr(&load("format.json")) {
        let raw = s(&c["raw"]).to_string();
        let node = Node {
            path: Vec::new(),
            kind: s(&c["kind"]).parse().unwrap(),
            start: 0,
            end: raw.len(),
            raw,
        };
        let got = format_value(&node, &json_value(&c["value"])).unwrap();
        assert_eq!(got, s(&c["expected"]), "{c}");
    }
}

// ---------------------------------------------------------------- images

#[test]
fn bim_decode() {
    let v = load("bim.json");
    for c in arr(&v["decode"]) {
        let file = s(&c["file"]);
        let b = wolfsdk::parse_bim(&read_bytes(&format!("bim/{file}"))).unwrap();
        assert_eq!(b.hash as u64, u(&c["hash"]), "{file}");
        let header: Vec<u64> = arr(&c["header"]).iter().map(u).collect();
        assert_eq!(
            b.header.iter().map(|&x| x as u64).collect::<Vec<_>>(),
            header
        );
        let mips: Vec<Vec<u64>> = arr(&c["mips"])
            .iter()
            .map(|m| arr(m).iter().map(u).collect())
            .collect();
        let got: Vec<Vec<u64>> = b
            .mips
            .iter()
            .map(|m| vec![m.width as u64, m.height as u64, m.data.len() as u64])
            .collect();
        assert_eq!(got, mips, "{file}");
        let (w, h, rgba) = wolfsdk::decode(&b, 0).unwrap();
        assert_eq!((w as u64, h as u64), (u(&c["width"]), u(&c["height"])));
        assert_eq!(sha(&rgba), s(&c["rgbaSha256"]), "{file}");
    }
    for f in arr(&v["badMagic"]) {
        assert!(is_format_err(wolfsdk::parse_bim(&read_bytes(&format!(
            "bim/{}",
            s(f)
        )))));
    }
}

#[test]
fn bim_encode() {
    let e = &load("bim.json")["encode"];
    let tmpl = wolfsdk::parse_bim(&read_bytes(&format!("bim/{}", s(&e["template"])))).unwrap();
    let out = wolfsdk::encode_rgba8(
        &tmpl,
        u(&e["width"]) as u32,
        u(&e["height"]) as u32,
        &unhex(s(&e["rgbaHex"])),
    )
    .unwrap();
    assert_eq!(out, read_bytes(&format!("bim/{}", s(&e["expect"]))));
}

#[test]
fn png() {
    for c in arr(&load("bim.json")["png"]) {
        let file = s(&c["file"]);
        let (w, h, rgba) = wolfsdk::decode_png(&read_bytes(&format!("png/{file}"))).unwrap();
        assert_eq!(
            (w as u64, h as u64),
            (u(&c["width"]), u(&c["height"])),
            "{file}"
        );
        assert_eq!(sha(&rgba), s(&c["rgbaSha256"]), "{file}");
        let again = wolfsdk::decode_png(&wolfsdk::encode_png(w, h, &rgba).unwrap()).unwrap();
        assert_eq!(again, (w, h, rgba), "{file}");
    }
}

// ---------------------------------------------------------- audio / save

#[test]
fn audio_bsnf() {
    for c in arr(&load("audio.json")["bsnf"]) {
        let got = wolfsdk::parse_bsnf(&read_bytes(&format!("audio/{}", s(&c["file"])))).unwrap();
        let got: Vec<Json> = got
            .iter()
            .map(|x| {
                serde_json::json!({
                    "language": x.language, "hash": x.hash, "length": x.length,
                    "granule": x.granule, "format_tag": x.format_tag, "channels": x.channels,
                    "sample_rate": x.sample_rate, "avg_bytes_per_sec": x.avg_bytes_per_sec,
                    "block_align": x.block_align, "bits_per_sample": x.bits_per_sample,
                })
            })
            .collect();
        assert_eq!(&got, arr(&c["samples"]), "{}", c["file"]);
    }
}

#[test]
fn audio_ogg() {
    let v = &load("audio.json")["ogg"];
    let rel = format!("audio/{}", s(&v["file"]));
    let data = read_bytes(&rel);
    let streams = arr(&v["streams"]);
    for st in streams {
        assert_eq!(
            wolfsdk::ogg_stream_length(&data, u(&st[0])).unwrap(),
            u(&st[1])
        );
    }
    let mut f = fs::File::open(vec_dir().join(&rel)).unwrap();
    assert_eq!(
        wolfsdk::ogg_stream_length_reader(&mut f, u(&streams[1][0])).unwrap(),
        u(&streams[1][1])
    );
    assert!(is_format_err(wolfsdk::ogg_stream_length(
        &data,
        u(&v["badOffset"])
    )));
}

#[test]
fn audio_containers() {
    for pair in arr(&load("audio.json")["containers"]) {
        assert_eq!(wolfsdk::stream_container_for(s(&pair[0])), s(&pair[1]));
    }
}

#[test]
fn save() {
    for c in arr(&load("save.json")) {
        let p = unhex(s(&c["payloadHex"]));
        let want = s(&c["checksumHex"]);
        assert_eq!(hex(&wolfsdk::save_checksum(&p)), want);
        let mut good = unhex(want);
        good.extend_from_slice(&p);
        assert!(wolfsdk::verify_save(&good));
        let mut zero = vec![0u8; 4];
        zero.extend_from_slice(&p);
        assert!(!(wolfsdk::verify_save(&zero) && want != "00000000"));
        let mut ff = vec![0xFFu8; 4];
        ff.extend_from_slice(&p);
        assert_eq!(wolfsdk::fix_save(&ff).unwrap(), good);
    }
}

// ----------------------------------------------------------- compression

#[test]
fn compression_deflate_exact() {
    let v = load("compression.json");
    for c in arr(&v["cases"]) {
        let data = read_bytes(s(&v["files"][s(&c["data"])]));
        let st = wolfsdk::deflate_sync(&data);
        assert!(wolfsdk::is_sync_flushed(&st));
        assert_eq!(wolfsdk::inflate(&st, data.len()).unwrap(), data);
        let delta = c["delta"].as_i64().unwrap();
        let pad = c["pad"].as_bool().unwrap();
        let csize = (st.len() as i64 + delta) as usize;
        let r = wolfsdk::deflate_exact(&data, csize, pad);
        if c["mustFit"].as_bool().unwrap() {
            assert!(r.is_some(), "{c}");
        }
        if delta < 0 || (delta > 0 && !pad) {
            assert!(r.is_none(), "{c}");
        }
        if let Some((stream, payload)) = r {
            assert_eq!(stream.len(), csize, "{c}");
            assert!(wolfsdk::is_sync_flushed(&stream));
            assert!(payload.starts_with(&data));
            assert!(is_comment_padding(&payload[data.len()..]), "{c}");
            assert_eq!(wolfsdk::inflate(&stream, payload.len()).unwrap(), payload);
        }
    }
}

#[test]
fn compression_inflate_short() {
    assert!(is_format_err(wolfsdk::inflate(
        &wolfsdk::deflate_sync(b"abc"),
        10
    )));
}

// --------------------------------------------------------------- archive

struct TempGame {
    dir: PathBuf,
    root: PathBuf,
}

impl TempGame {
    fn new() -> TempGame {
        static N: AtomicUsize = AtomicUsize::new(0);
        let dir = std::env::temp_dir().join(format!(
            "wolfsdk-test-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::SeqCst)
        ));
        let _ = fs::remove_dir_all(&dir);
        let root = dir.join("game");
        copy_dir(&vec_dir().join("game"), &root);
        TempGame { dir, root }
    }
}

impl Drop for TempGame {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

fn copy_dir(src: &Path, dst: &Path) {
    fs::create_dir_all(dst).unwrap();
    for e in fs::read_dir(src).unwrap() {
        let e = e.unwrap();
        let to = dst.join(e.file_name());
        if e.file_type().unwrap().is_dir() {
            copy_dir(&e.path(), &to);
        } else {
            fs::copy(e.path(), to).unwrap();
        }
    }
}

fn file_size(p: &Path) -> u64 {
    fs::metadata(p).unwrap().len()
}

#[test]
fn archive_master_index() {
    let v = load("archive.json");
    let data = read_bytes("game/base/master.index");
    let pairs = wolfsdk::parse_master_index(&data).unwrap();
    let got: Vec<Json> = pairs
        .iter()
        .map(|(a, b)| serde_json::json!([a, b]))
        .collect();
    assert_eq!(&got, arr(&v["pairs"]));
    assert_eq!(wolfsdk::build_master_index(&pairs), data);
    let mut bad = b"XXXX".to_vec();
    bad.extend_from_slice(&data[4..]);
    assert!(is_format_err(wolfsdk::parse_master_index(&bad)));
    assert!(is_format_err(wolfsdk::parse_master_index(&data[..20])));
}

#[test]
fn archive_entries() {
    let t = TempGame::new();
    let v = load("archive.json");
    let g = Game::open(&t.root).unwrap();
    let chunks = arr(&v["chunks"]);
    assert_eq!(g.chunks.len(), chunks.len());
    for (ci, (c, exp)) in g.chunks.iter().zip(chunks).enumerate() {
        assert_eq!(c.index_name, s(&exp["index"]));
        assert_eq!(c.index.counter_a as u64, u(&exp["counterA"]));
        assert_eq!(c.index.counter_b as u64, u(&exp["counterB"]));
        let entries = arr(&exp["entries"]);
        assert_eq!(c.entries().len(), entries.len());
        for (e, x) in c.entries().iter().zip(entries) {
            let occ = wolfsdk::Occurrence {
                chunk: ci,
                entry: e.index,
            };
            let got = serde_json::json!({
                "index": e.index, "type": e.type_, "name": e.name, "path": e.path,
                "offset": e.offset, "usize": e.usize, "csize": e.csize, "start": e.start,
                "tripleOffset": e.triple_offset, "trailerHex": hex(&e.trailer),
                "streamOffset": e.stream_offset(), "compressed": e.compressed(),
                "payloadSha256": sha(&g.read(&occ).unwrap()),
            });
            assert_eq!(&got, x);
        }
        // An index parsed without the resources size must give the same entries.
        let bare = wolfsdk::ChunkIndex::parse(&c.index.to_bytes(), None).unwrap();
        assert_eq!(bare.len(), entries.len());
        assert_eq!(wolfsdk::validate_chunk(c).unwrap(), Vec::<String>::new());
    }
    for (key, want) in v["find"].as_object().unwrap() {
        let (ty, name) = key.split_once(':').unwrap();
        let got: Vec<Json> = g
            .find(ty, name)
            .iter()
            .map(|o| Json::String(g.chunk(o).index_name.clone()))
            .collect();
        assert_eq!(&got, arr(want), "{key}");
    }
}

#[test]
fn archive_write_in_place() {
    let t = TempGame::new();
    for case in arr(&load("archive.json")["writeInPlace"]) {
        let mut g = Game::open(&t.root).unwrap();
        let (ty, name) = s(&case["asset"]).split_once(':').unwrap();
        let data = s(&case["text"]).as_bytes();
        let sizes: HashMap<String, u64> = g
            .chunks
            .iter()
            .map(|c| (c.resources_name.clone(), file_size(&c.resources_path)))
            .collect();
        let slots = |g: &Game| -> Vec<(String, u32, u32)> {
            g.find(ty, name)
                .iter()
                .map(|o| {
                    let e = g.entry(o);
                    (g.chunk(o).index_name.clone(), e.offset, e.csize)
                })
                .collect()
        };
        let before = slots(&g);
        if s(&case["expect"]) == "nofit" {
            let r = g.write_asset(ty, name, data);
            assert!(matches!(r, Err(Error::NoFit(_))), "{case}");
            continue;
        }
        g.write_asset(ty, name, data).unwrap();
        let g2 = Game::open(&t.root).unwrap();
        assert_eq!(before, slots(&g2));
        for o in g2.find(ty, name) {
            let payload = g2.read(&o).unwrap();
            assert!(payload.starts_with(data));
            assert!(is_comment_padding(&payload[data.len()..]));
        }
        for c in &g2.chunks {
            assert_eq!(file_size(&c.resources_path), sizes[&c.resources_name]);
            assert_eq!(wolfsdk::validate_chunk(c).unwrap(), Vec::<String>::new());
        }
    }
}

#[test]
fn archive_rebuild() {
    let t = TempGame::new();
    let r = &load("archive.json")["rebuild"];
    let text = s(&r["text"]).as_bytes().to_vec();
    let mut g = Game::open(&t.root).unwrap();
    let ci = g
        .chunks
        .iter()
        .position(|c| c.index_name == s(&r["chunk"]))
        .unwrap();
    let (ty, name) = s(&r["asset"]).split_once(':').unwrap();
    let target = g.chunks[ci].index.find(ty, name)[0].index;
    let before: HashMap<usize, Vec<u8>> = g.chunks[ci]
        .entries()
        .iter()
        .map(|e| {
            let occ = wolfsdk::Occurrence {
                chunk: ci,
                entry: e.index,
            };
            (e.index, g.read(&occ).unwrap())
        })
        .collect();
    let mut repl = HashMap::new();
    repl.insert(target, text.clone());
    g.rebuild(ci, &repl, None).unwrap();
    let g2 = Game::open(&t.root).unwrap();
    let ci2 = g2
        .chunks
        .iter()
        .position(|c| c.index_name == s(&r["chunk"]))
        .unwrap();
    let chunk2 = &g2.chunks[ci2];
    assert_eq!(
        wolfsdk::validate_chunk(chunk2).unwrap(),
        Vec::<String>::new()
    );
    for e in chunk2.entries() {
        let want = if e.index == target {
            &text
        } else {
            &before[&e.index]
        };
        let occ = wolfsdk::Occurrence {
            chunk: ci2,
            entry: e.index,
        };
        assert_eq!(&g2.read(&occ).unwrap(), want);
    }
}

// ------------------------------------------------------------------- mod

#[test]
fn mod_apply() {
    let v = load("mod.json");
    let base: HashMap<String, String> = v["base"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, p)| (k.clone(), read_text(&format!("mod/{}", s(p)))))
        .collect();
    let mods: Vec<Mod> = arr(&v["order"])
        .iter()
        .map(|n| Mod::load(vec_dir().join("mod").join(s(n))).unwrap())
        .collect();
    let sorted: Vec<Json> = wolfsdk::mods::sort_mods(&mods)
        .iter()
        .map(|m| Json::String(m.name.clone()))
        .collect();
    assert_eq!(&sorted, arr(&v["sorted"]));
    let res = wolfsdk::apply_mods(&mods, |t, n| base.get(&format!("{t}:{n}")).cloned(), None);
    let order: Vec<Json> = res
        .results
        .iter()
        .map(|(k, _)| Json::String(k.clone()))
        .collect();
    assert_eq!(&order, arr(&v["resultOrder"]));
    for (k, p) in v["results"].as_object().unwrap() {
        assert_eq!(
            res.get(k).unwrap(),
            read_text(&format!("mod/{}", s(p))),
            "{k}"
        );
    }
    let conflicts: Vec<Json> = res
        .conflicts
        .iter()
        .map(|c| {
            serde_json::json!({
                "kind": c.kind.as_str(), "asset": c.asset, "path": c.path,
                "mods": c.mods, "winner": c.winner,
            })
        })
        .collect();
    assert_eq!(&conflicts, arr(&v["conflicts"]));
    let errors: Vec<Json> = res
        .errors
        .iter()
        .map(|e| serde_json::json!({"asset": e.asset, "mod": e.mod_name}))
        .collect();
    assert_eq!(&errors, arr(&v["errors"]));
}

#[test]
fn mod_invalid() {
    for pair in arr(&load("mod.json")["invalid"]) {
        let r = Mod::loads(s(&pair[1]), "mod", None);
        assert!(matches!(r, Err(Error::Mod(_))), "{pair}");
    }
}
