//! `mod.json` loading, layering and conflict detection (SPEC §7).

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use serde_json::Value as Json;

use crate::decl::{Decl, Value};
use crate::error::{Error, Result};

/// The changes one mod makes to one asset.
#[derive(Debug, Clone, PartialEq)]
pub struct AssetChange {
    /// `type:name`.
    pub key: String,
    /// Decl path → value, in file order.
    pub set: Vec<(String, Value)>,
    /// Replacement file, relative to the mod directory.
    pub file: Option<String>,
}

impl AssetChange {
    /// The asset type (before the first `:`).
    pub fn type_(&self) -> &str {
        self.key.split_once(':').map_or(&self.key[..], |(t, _)| t)
    }

    /// The asset name (after the first `:`).
    pub fn name(&self) -> &str {
        self.key.split_once(':').map_or("", |(_, n)| n)
    }
}

/// A loaded `mod.json`.
#[derive(Debug, Clone, PartialEq)]
pub struct Mod {
    /// Display name (defaults to the mod directory name).
    pub name: String,
    /// Low priorities are applied first.
    pub priority: i64,
    /// `tno` or `tnc`.
    pub game: Option<String>,
    /// Free-form version.
    pub version: Option<String>,
    /// Free-form description.
    pub description: Option<String>,
    /// Asset changes in file order.
    pub assets: Vec<AssetChange>,
    /// Directory the mod was loaded from (resolves `file` entries).
    pub root: Option<PathBuf>,
}

fn opt_str(v: Option<&Json>) -> Option<String> {
    v.and_then(|v| v.as_str()).map(str::to_string)
}

impl Mod {
    /// Build a mod from parsed JSON.
    pub fn from_json(data: &Json, default_name: &str, root: Option<PathBuf>) -> Result<Mod> {
        let obj = data
            .as_object()
            .ok_or_else(|| Error::Mod("mod.json must contain a JSON object".into()))?;
        let priority = match obj.get("priority") {
            None => 0,
            Some(Json::Number(n)) => {
                if let Some(i) = n.as_i64() {
                    i
                } else {
                    match n.as_f64() {
                        Some(f) if f.is_finite() && f.fract() == 0.0 => f as i64,
                        _ => return Err(Error::Mod("priority must be an integer".into())),
                    }
                }
            }
            Some(_) => return Err(Error::Mod("priority must be an integer".into())),
        };
        let game = match obj.get("game").or_else(|| obj.get("spiel")) {
            None | Some(Json::Null) => None,
            Some(Json::String(g)) if g == "tno" || g == "tnc" => Some(g.clone()),
            Some(g) => {
                return Err(Error::Mod(format!(
                    "unknown game {g} (expected 'tno' or 'tnc')"
                )))
            }
        };
        let raw_assets: Vec<(&String, &Json)> = match obj.get("assets") {
            Some(Json::Object(m)) => m.iter().collect(),
            Some(_) => return Err(Error::Mod("assets must be an object".into())),
            None => obj.iter().filter(|(k, _)| k.contains(':')).collect(),
        };
        let mut assets = Vec::new();
        for (key, change) in raw_assets {
            if !key.contains(':') || key.starts_with(':') || key.ends_with(':') {
                return Err(Error::Mod(format!("asset key {key:?} must be 'type:name'")));
            }
            let change = change
                .as_object()
                .ok_or_else(|| Error::Mod(format!("{key}: change must be an object")))?;
            let sets = change.get("set").filter(|v| !v.is_null());
            let file = change.get("file").filter(|v| !v.is_null());
            if sets.is_none() && file.is_none() {
                return Err(Error::Mod(format!("{key}: needs 'set' or 'file'")));
            }
            let sets = match sets {
                None => None,
                Some(Json::Object(m)) => Some(m),
                Some(_) => return Err(Error::Mod(format!("{key}: 'set' must be an object"))),
            };
            let file = match file {
                None => None,
                Some(Json::String(s)) => Some(s.clone()),
                Some(_) => return Err(Error::Mod(format!("{key}: 'file' must be a string"))),
            };
            let mut pairs = Vec::new();
            for (path, value) in sets.into_iter().flatten() {
                let v = json_to_value(value).ok_or_else(|| {
                    Error::Mod(format!(
                        "{key}: value of {path:?} must be string, number or bool"
                    ))
                })?;
                pairs.push((path.clone(), v));
            }
            assets.push(AssetChange {
                key: key.clone(),
                set: pairs,
                file,
            });
        }
        let name = match obj.get("name") {
            Some(Json::String(s)) if !s.is_empty() => s.clone(),
            _ => default_name.to_string(),
        };
        Ok(Mod {
            name,
            priority,
            game,
            version: opt_str(obj.get("version")),
            description: opt_str(obj.get("description")),
            assets,
            root,
        })
    }

    /// Parse `mod.json` text.
    pub fn loads(text: &str, default_name: &str, root: Option<PathBuf>) -> Result<Mod> {
        let data: Json =
            serde_json::from_str(text).map_err(|e| Error::Mod(format!("invalid JSON: {e}")))?;
        Mod::from_json(&data, default_name, root)
    }

    /// Parse `mod.json` text with the default name `"mod"` and no root.
    #[allow(clippy::should_implement_trait)]
    pub fn from_str(text: &str) -> Result<Mod> {
        Mod::loads(text, "mod", None)
    }

    /// Load `mod.json`, or a directory containing one.
    pub fn load<P: AsRef<Path>>(path: P) -> Result<Mod> {
        let mut path = path.as_ref().to_path_buf();
        if path.is_dir() {
            path = path.join("mod.json");
        }
        let abs = if path.is_absolute() {
            path.clone()
        } else {
            std::env::current_dir()?.join(&path)
        };
        let root = abs.parent().map(Path::to_path_buf).unwrap_or_default();
        let default_name = root
            .file_name()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default();
        let text = std::fs::read_to_string(&path)?;
        Mod::loads(&text, &default_name, Some(root))
    }

    /// Read a file relative to the mod directory.
    pub fn read_file(&self, rel: &str) -> Result<String> {
        let root = self.root.as_ref().ok_or_else(|| {
            Error::Mod(format!(
                "{}: cannot resolve file {rel:?} without a mod directory",
                self.name
            ))
        })?;
        Ok(std::fs::read_to_string(root.join(rel))?)
    }
}

impl std::str::FromStr for Mod {
    type Err = Error;

    fn from_str(s: &str) -> Result<Mod> {
        Mod::loads(s, "mod", None)
    }
}

fn json_to_value(v: &Json) -> Option<Value> {
    match v {
        Json::String(s) => Some(Value::Str(s.clone())),
        Json::Bool(b) => Some(Value::Bool(*b)),
        Json::Number(n) => n.as_f64().map(Value::Num),
        _ => None,
    }
}

/// Kind of a layering conflict.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConflictKind {
    /// Two or more mods `set` the same path.
    Path,
    /// Two or more mods replace the same asset with `file`.
    File,
    /// A `file` replaces an asset a lower-priority mod `set`.
    FileOverSet,
}

impl ConflictKind {
    /// `"path"`, `"file"` or `"file-over-set"`.
    pub fn as_str(self) -> &'static str {
        match self {
            ConflictKind::Path => "path",
            ConflictKind::File => "file",
            ConflictKind::FileOverSet => "file-over-set",
        }
    }
}

/// A non-blocking conflict between mods.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Conflict {
    /// Conflict kind.
    pub kind: ConflictKind,
    /// `type:name` of the asset.
    pub asset: String,
    /// The Decl path (`path` conflicts only).
    pub path: Option<String>,
    /// Names of the mods involved, in application order.
    pub mods: Vec<String>,
    /// The mod whose change survives.
    pub winner: String,
}

/// A change that could not be applied.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApplyError {
    /// `type:name` of the asset.
    pub asset: String,
    /// Name of the mod.
    pub mod_name: String,
    /// Free-form message.
    pub message: String,
}

/// Result of [`apply_mods`].
#[derive(Debug, Clone, Default)]
pub struct ApplyResult {
    /// `(asset key, new text)` in order of first appearance.
    pub results: Vec<(String, String)>,
    /// Conflicts, per asset in spec order.
    pub conflicts: Vec<Conflict>,
    /// Errors; the failing changes were skipped.
    pub errors: Vec<ApplyError>,
}

impl ApplyResult {
    /// The new text of an asset, if it was produced.
    pub fn get(&self, key: &str) -> Option<&str> {
        self.results
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
    }
}

/// Mods sorted by priority ascending; ties keep input order.
pub fn sort_mods(mods: &[Mod]) -> Vec<&Mod> {
    let mut v: Vec<&Mod> = mods.iter().collect();
    v.sort_by_key(|m| m.priority);
    v
}

/// Callback reading a mod's `file` entry.
pub type ReadFile<'a> = &'a dyn Fn(&Mod, &str) -> Result<String>;

/// Layer `mods` over the assets returned by `read_asset` (SPEC §7.1).
///
/// `read_file` defaults to [`Mod::read_file`].
pub fn apply_mods<F>(mods: &[Mod], read_asset: F, read_file: Option<ReadFile<'_>>) -> ApplyResult
where
    F: Fn(&str, &str) -> Option<String>,
{
    let ordered = sort_mods(mods);
    let mut keys: Vec<String> = Vec::new();
    let mut per_asset: HashMap<String, Vec<(usize, &AssetChange)>> = HashMap::new();
    for (mi, m) in ordered.iter().enumerate() {
        for ch in &m.assets {
            per_asset
                .entry(ch.key.clone())
                .or_insert_with(|| {
                    keys.push(ch.key.clone());
                    Vec::new()
                })
                .push((mi, ch));
        }
    }

    let mut out = ApplyResult::default();
    for key in keys {
        let changes = &per_asset[&key];
        let name_of = |mi: usize| ordered[mi].name.clone();

        let file_mods: Vec<String> = changes
            .iter()
            .filter(|(_, ch)| ch.file.is_some())
            .map(|(mi, _)| name_of(*mi))
            .collect();
        if file_mods.len() > 1 {
            out.conflicts.push(Conflict {
                kind: ConflictKind::File,
                asset: key.clone(),
                path: None,
                winner: file_mods[file_mods.len() - 1].clone(),
                mods: file_mods,
            });
        }
        let mut path_mods: Vec<(String, Vec<String>)> = Vec::new();
        for (mi, ch) in changes {
            for (p, _) in &ch.set {
                let idx = match path_mods.iter().position(|(q, _)| q == p) {
                    Some(i) => i,
                    None => {
                        path_mods.push((p.clone(), Vec::new()));
                        path_mods.len() - 1
                    }
                };
                let n = name_of(*mi);
                if !path_mods[idx].1.contains(&n) {
                    path_mods[idx].1.push(n);
                }
            }
        }
        for (p, names) in path_mods {
            if names.len() > 1 {
                out.conflicts.push(Conflict {
                    kind: ConflictKind::Path,
                    asset: key.clone(),
                    path: Some(p),
                    winner: names[names.len() - 1].clone(),
                    mods: names,
                });
            }
        }
        for (i, (mi, ch)) in changes.iter().enumerate() {
            if ch.file.is_none() {
                continue;
            }
            let mut setters: Vec<String> = changes[..i]
                .iter()
                .filter(|(pmi, pch)| !pch.set.is_empty() && pmi != mi)
                .map(|(pmi, _)| name_of(*pmi))
                .collect();
            if !setters.is_empty() {
                setters.push(name_of(*mi));
                out.conflicts.push(Conflict {
                    kind: ConflictKind::FileOverSet,
                    asset: key.clone(),
                    path: None,
                    mods: setters,
                    winner: name_of(*mi),
                });
            }
        }

        let (typ, name) = key.split_once(':').unwrap_or((&key[..], ""));
        let mut text = match read_asset(typ, name) {
            Some(t) => t,
            None => {
                out.errors.push(ApplyError {
                    asset: key.clone(),
                    mod_name: name_of(changes[0].0),
                    message: "asset not found".into(),
                });
                continue;
            }
        };
        for (mi, ch) in changes {
            let m = ordered[*mi];
            if let Some(file) = &ch.file {
                let r = match read_file {
                    Some(f) => f(m, file),
                    None => m.read_file(file),
                };
                match r {
                    Ok(t) => text = t,
                    Err(e) => {
                        out.errors.push(ApplyError {
                            asset: key.clone(),
                            mod_name: m.name.clone(),
                            message: format!("cannot read {file}: {e}"),
                        });
                        continue;
                    }
                }
            }
            if ch.set.is_empty() {
                continue;
            }
            let mut decl = match Decl::parse(&text) {
                Ok(d) => d,
                Err(e) => {
                    out.errors.push(ApplyError {
                        asset: key.clone(),
                        mod_name: m.name.clone(),
                        message: e.to_string(),
                    });
                    continue;
                }
            };
            for (p, v) in &ch.set {
                if let Err(e) = decl.set(p, v.clone()) {
                    out.errors.push(ApplyError {
                        asset: key.clone(),
                        mod_name: m.name.clone(),
                        message: e.to_string(),
                    });
                }
            }
            text = decl.text().to_string();
        }
        out.results.push((key.clone(), text));
    }
    out
}
