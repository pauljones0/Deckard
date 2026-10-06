//! Shared bounded networking and transactional archive installs.
use crate::{archive::unsafe_member_reason, model, plugin};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    fs,
    io::{Cursor, Read, Write},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    time::Duration,
};
const LIMIT: usize = 128 * 1024 * 1024;
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Entry {
    pub kind: String,
    pub name: String,
    pub description: String,
    pub url: String,
    pub sha256: String,
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub branch: String,
    #[serde(default)]
    pub architectures: Vec<String>,
}
pub struct Network {
    client: reqwest::blocking::Client,
}
impl Network {
    pub fn new() -> Result<Self> {
        Ok(Self {
            client: reqwest::blocking::Client::builder()
                .user_agent("Deckard/0.4 Rust")
                .connect_timeout(Duration::from_secs(10))
                .timeout(Duration::from_secs(45))
                .build()?,
        })
    }
    pub fn download(&self, url: &str) -> Result<Vec<u8>> {
        ensure!(url.starts_with("https://"), "store downloads require HTTPS");
        let mut last = None;
        for attempt in 0..3 {
            match self
                .client
                .get(url)
                .send()
                .and_then(reqwest::blocking::Response::error_for_status)
            {
                Ok(response) => {
                    ensure!(
                        response.content_length().unwrap_or(0) <= LIMIT as u64,
                        "download exceeds size limit"
                    );
                    let mut bytes = Vec::new();
                    response.take((LIMIT + 1) as u64).read_to_end(&mut bytes)?;
                    ensure!(bytes.len() <= LIMIT, "download exceeds size limit");
                    return Ok(bytes);
                }
                Err(error) => {
                    if error
                        .status()
                        .is_some_and(|s| s.is_client_error() && s.as_u16() != 429)
                    {
                        return Err(error.into());
                    }
                    last = Some(error);
                    if attempt < 2 {
                        std::thread::sleep(Duration::from_millis(250 * (1 << attempt)))
                    }
                }
            }
        }
        Err(last.context("download failed")?.into())
    }
    pub fn catalog(&self, url: &str) -> Result<Vec<Entry>> {
        let mut entries: Vec<Entry> = serde_json::from_slice(&self.download(url)?)?;
        entries.retain(|e| {
            e.architectures.is_empty()
                || e.architectures.iter().any(|a| a == std::env::consts::ARCH)
        });
        Ok(entries)
    }
    pub fn install(&self, entry: &Entry, root: &Path) -> Result<String> {
        let bytes = self.download(&entry.url)?;
        ensure!(
            format!("{:x}", Sha256::digest(&bytes))
                .eq_ignore_ascii_case(entry.sha256.trim_start_matches("sha256:")),
            "package checksum mismatch"
        );
        install_archive(&bytes, &entry.kind, root)
    }
    pub fn ai(
        &self,
        endpoint: &str,
        key: &str,
        model: &str,
        prompt: &str,
    ) -> Result<serde_json::Value> {
        ensure!(
            endpoint.starts_with("https://")
                || endpoint.starts_with("http://127.0.0.1:")
                || endpoint.starts_with("http://localhost:"),
            "AI requires HTTPS or a local endpoint"
        );
        let response:serde_json::Value=self.client.post(endpoint).bearer_auth(key).json(&serde_json::json!({"model":model,"messages":[{"role":"system","content":"Return only a Deckard page JSON object with keys, dials, touchscreens, settings. Keys are xxy, for example 0x0. Each has states keyed by numbers as strings. State contains labels.bottom.text and actions array. Supported action IDs: native::url (settings.url), native::page (settings.page), native::command (settings.argv, an array). Never invent plugins. Do not create commands unless explicitly requested."},{"role":"user","content":prompt}]})).send()?.error_for_status()?.json()?;
        let text = response["choices"][0]["message"]["content"]
            .as_str()
            .context("AI response missing content")?
            .trim()
            .trim_start_matches("```json")
            .trim_start_matches("```")
            .trim_end_matches("```")
            .trim();
        let page: serde_json::Value = serde_json::from_str(text)?;
        ensure!(page.is_object(), "AI response is not a page");
        Ok(page)
    }
}
pub fn extract(bytes: &[u8], destination: &Path) -> Result<()> {
    let mut archive = zip::ZipArchive::new(Cursor::new(bytes))?;
    ensure!(archive.len() <= 5000, "too many archive entries");
    let mut names = HashSet::new();
    let mut total = 0u64;
    for index in 0..archive.len() {
        let mut file = archive.by_index(index)?;
        ensure!(
            unsafe_member_reason(file.name()).is_none()
                && !file.name().split(['/', '\\']).any(|x| x == ".."),
            "unsafe archive member"
        );
        ensure!(!file.name().contains('\\'), "nonportable archive path");
        ensure!(
            names.insert(file.name().to_owned()),
            "duplicate archive member"
        );
        ensure!(
            file.unix_mode()
                .is_none_or(|mode| mode & 0o170000 != 0o120000),
            "archive symlinks are forbidden"
        );
        total = total
            .checked_add(file.size())
            .context("archive size overflow")?;
        ensure!(total <= LIMIT as u64, "unpacked archive exceeds size limit");
        let path = destination.join(file.enclosed_name().context("unsafe member")?);
        if file.is_dir() {
            fs::create_dir_all(path)?;
            continue;
        }
        fs::create_dir_all(path.parent().context("member parent")?)?;
        let mut output = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)?;
        let written = std::io::copy(&mut file.by_ref().take(LIMIT as u64 + 1), &mut output)?;
        ensure!(written == file.size(), "archive entry size mismatch");
        if file.unix_mode().is_some_and(|m| m & 0o111 != 0) {
            output.set_permissions(fs::Permissions::from_mode(0o755))?;
        } else {
            output.set_permissions(fs::Permissions::from_mode(0o644))?
        }
        output.flush()?;
        output.sync_all()?;
    }
    Ok(())
}
pub fn install_archive(bytes: &[u8], kind: &str, root: &Path) -> Result<String> {
    ensure!(
        ["plugin", "page", "icons", "wallpaper", "sdplusbar"].contains(&kind),
        "unknown package kind"
    );
    let parent = root.join(match kind {
        "plugin" => "plugins-native",
        "page" => "imports",
        "wallpaper" => "wallpapers-native",
        "sdplusbar" => "sdplusbar-native",
        _ => "icons-native",
    });
    fs::create_dir_all(&parent)?;
    let staging = tempfile::Builder::new()
        .prefix(".stage-")
        .tempdir_in(&parent)?;
    extract(bytes, staging.path())?;
    let id = if kind == "plugin" {
        plugin::read_manifest(staging.path())?.id
    } else {
        let meta = model::load_json(
            &staging.path().join("package.json"),
            serde_json::Value::Null,
        )?;
        meta["id"].as_str().context("package ID missing")?.into()
    };
    model::valid_name(&id)?;
    let target = parent.join(&id);
    if kind == "page" {
        let page = model::load_json(&staging.path().join("page.json"), serde_json::Value::Null)?;
        model::validate_page(&page)?;
        let mut checked = page.clone();
        rewrite_assets(&mut checked, staging.path())?;
        if let Ok(entries) = fs::read_dir(staging.path().join("plugins")) {
            for entry in entries {
                plugin::read_manifest(&entry?.path())?;
            }
        }
        let meta = model::load_json(
            &staging.path().join("package.json"),
            serde_json::Value::Null,
        )?;
        let name = meta["name"].as_str().context("page name missing")?;
        model::valid_name(name)?;
        ensure!(
            !root.join("pages").join(format!("{name}.json")).exists(),
            "page already exists; use a different name before importing"
        );
    }
    let stage_path = staging.keep();
    if let Err(error) = commit_directory(&stage_path, &target) {
        let _ = fs::remove_dir_all(stage_path);
        return Err(error);
    }
    if kind == "page" {
        import_page_bundle(&target, root)?;
    }
    Ok(id)
}
fn import_page_bundle(bundle: &Path, root: &Path) -> Result<()> {
    let page_path = bundle.join("page.json");
    let mut page = model::load_json(&page_path, serde_json::Value::Null)?;
    ensure!(page.is_object(), "bundle has no valid page");
    let package = model::load_json(&bundle.join("package.json"), serde_json::Value::Null)?;
    let name = package["name"].as_str().context("page name missing")?;
    model::valid_name(name)?;
    rewrite_assets(&mut page, bundle)?;
    if let Ok(entries) = fs::read_dir(bundle.join("plugins")) {
        for entry in entries {
            let directory = entry?.path();
            let manifest = plugin::read_manifest(&directory)?;
            let parent = root.join("plugins-native");
            fs::create_dir_all(&parent)?;
            let target = parent.join(&manifest.id);
            // Never silently replace a plugin already installed by the user.
            if !target.exists() {
                let stage = tempfile::Builder::new()
                    .prefix(".stage-")
                    .tempdir_in(&parent)?;
                copy_plugin_tree(&directory, stage.path())?;
                let path = stage.keep();
                commit_directory(&path, &target)?;
            }
        }
    }
    let path = root.join("pages").join(format!("{name}.json"));
    ensure!(!path.exists(), "page already exists");
    model::save_json(&path, &page)
}
fn rewrite_assets(value: &mut serde_json::Value, bundle: &Path) -> Result<()> {
    match value {
        serde_json::Value::String(s) if s.starts_with("assets/") => {
            ensure!(
                unsafe_member_reason(s).is_none(),
                "bundle asset path escapes package"
            );
            let path = bundle.join(&*s).canonicalize()?;
            ensure!(
                path.starts_with(bundle.canonicalize()?),
                "bundle asset path escapes package"
            );
            *s = path.to_string_lossy().into_owned();
        }
        serde_json::Value::Array(a) => {
            for v in a {
                rewrite_assets(v, bundle)?
            }
        }
        serde_json::Value::Object(o) => {
            for v in o.values_mut() {
                rewrite_assets(v, bundle)?
            }
        }
        _ => {}
    }
    Ok(())
}

pub fn export_page(name: &str, page: &serde_json::Value, path: &Path) -> Result<()> {
    export_page_with_plugins(name, page, path, &[])
}
pub fn export_page_with_plugins(
    name: &str,
    page: &serde_json::Value,
    path: &Path,
    plugins: &[plugin::Installed],
) -> Result<()> {
    let dependencies = required_plugins(page.clone());
    let mut page = page.clone();
    let mut assets = Vec::<(PathBuf, String)>::new();
    collect_assets(&mut page, &mut assets);
    let mut writer = zip::ZipWriter::new(Cursor::new(Vec::new()));
    let options = zip::write::SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Deflated);
    for (original, name) in assets {
        ensure!(
            fs::metadata(&original)?.len() <= LIMIT as u64,
            "asset too large"
        );
        writer.start_file(&name, options)?;
        std::io::copy(&mut fs::File::open(original)?, &mut writer)?;
    }
    for id in &dependencies {
        if let Some(plugin) = plugins.iter().find(|p| &p.manifest.id == id) {
            archive_plugin(
                &mut writer,
                &plugin.directory,
                &plugin.directory,
                &format!("plugins/{id}"),
            )?;
        }
    }
    writer.start_file("page.json", options)?;
    writer.write_all(&serde_json::to_vec_pretty(&page)?)?;
    writer.start_file("package.json", options)?;
    writer.write_all(&serde_json::to_vec(&serde_json::json!({"api":1,"id":format!("page-{}",format!("{:x}",Sha256::digest(name.as_bytes()))),"name":name,"required_plugins":required_plugins(page.clone())}))?)?;
    crate::persistence::atomic_write(path, &writer.finish()?.into_inner())?;
    Ok(())
}
fn copy_plugin_tree(source: &Path, target: &Path) -> Result<()> {
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let ty = entry.file_type()?;
        let destination = target.join(entry.file_name());
        ensure!(!ty.is_symlink(), "plugin symlinks cannot be bundled");
        if ty.is_dir() {
            fs::create_dir(&destination)?;
            copy_plugin_tree(&entry.path(), &destination)?;
        } else if ty.is_file() {
            fs::copy(entry.path(), destination)?;
        }
    }
    Ok(())
}
fn archive_plugin(
    writer: &mut zip::ZipWriter<Cursor<Vec<u8>>>,
    directory: &Path,
    root: &Path,
    prefix: &str,
) -> Result<()> {
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let ty = entry.file_type()?;
        ensure!(!ty.is_symlink(), "plugin symlinks cannot be bundled");
        if ty.is_dir() {
            archive_plugin(writer, &entry.path(), root, prefix)?;
        } else if ty.is_file() {
            ensure!(
                entry.metadata()?.len() <= LIMIT as u64,
                "plugin file too large"
            );
            let path = entry.path();
            let name = format!("{prefix}/{}", path.strip_prefix(root)?.to_string_lossy());
            let mode = entry.metadata()?.permissions().mode() & 0o777;
            writer.start_file(
                name,
                zip::write::SimpleFileOptions::default()
                    .compression_method(zip::CompressionMethod::Deflated)
                    .unix_permissions(mode),
            )?;
            std::io::copy(&mut fs::File::open(path)?, writer)?;
        }
    }
    Ok(())
}
fn collect_assets(value: &mut serde_json::Value, assets: &mut Vec<(PathBuf, String)>) {
    match value {
        serde_json::Value::String(s) => {
            let path = Path::new(s);
            if path.is_absolute() && path.is_file() {
                let extension = path.extension().unwrap_or_default().to_string_lossy();
                let existing = assets
                    .iter()
                    .find(|(original, _)| original == path)
                    .map(|(_, name)| name.clone());
                let name = existing.unwrap_or_else(|| {
                    let name = format!("assets/{}.{extension}", assets.len());
                    assets.push((path.into(), name.clone()));
                    name
                });
                *s = name
            }
        }
        serde_json::Value::Array(a) => {
            for v in a {
                collect_assets(v, assets)
            }
        }
        serde_json::Value::Object(o) => {
            for (k, v) in o {
                if ["path", "image", "background", "media-path", "media-paths"]
                    .contains(&k.as_str())
                    || v.is_object()
                    || v.is_array()
                {
                    collect_assets(v, assets)
                }
            }
        }
        _ => {}
    }
}
fn required_plugins(page: serde_json::Value) -> Vec<String> {
    let mut ids = HashSet::new();
    fn visit(v: &serde_json::Value, ids: &mut HashSet<String>) {
        if let Some(id) = v["id"].as_str()
            && let Some((plugin, _)) = id.split_once("::")
            && plugin != "native"
        {
            ids.insert(plugin.into());
        }
        match v {
            serde_json::Value::Array(a) => {
                for v in a {
                    visit(v, ids)
                }
            }
            serde_json::Value::Object(o) => {
                for v in o.values() {
                    visit(v, ids)
                }
            }
            _ => {}
        }
    }
    visit(&page, &mut ids);
    let mut ids: Vec<_> = ids.into_iter().collect();
    ids.sort();
    ids
}

/// Recover an interrupted directory swap before any native plugin can start.
pub fn recover_installs(root: &Path) -> Result<()> {
    for directory in ["plugins-native", "imports", "icons-native"] {
        let parent = root.join(directory);
        if !parent.exists() {
            continue;
        }
        for entry in fs::read_dir(&parent)? {
            let path = entry?.path();
            let name = path.file_name().unwrap_or_default().to_string_lossy();
            if !name.starts_with(".transaction-") || !name.ends_with(".json") {
                continue;
            }
            let journal: serde_json::Value = serde_json::from_slice(&fs::read(&path)?)?;
            let target = journal["target"].as_str().context("transaction target")?;
            let stage = journal["stage"].as_str().context("transaction stage")?;
            let backup = journal["backup"].as_str().context("transaction backup")?;
            for name in [target, stage, backup] {
                model::valid_name(name)?;
            }
            let target = parent.join(target);
            let stage = parent.join(stage);
            let backup = parent.join(backup);
            if !target.exists() && backup.exists() {
                fs::rename(&backup, &target)?;
            }
            if target.exists() && backup.exists() {
                fs::remove_dir_all(&backup)?;
            }
            if stage.exists() {
                fs::remove_dir_all(stage)?;
            }
            fs::remove_file(path)?;
            fs::File::open(&parent)?.sync_all()?;
        }
    }
    Ok(())
}
fn commit_directory(stage: &Path, target: &Path) -> Result<()> {
    let parent = target.parent().context("install directory parent")?;
    let id = target
        .file_name()
        .context("install name")?
        .to_string_lossy();
    let backup = parent.join(format!(".backup-{id}"));
    let journal = parent.join(format!(".transaction-{id}.json"));
    ensure!(
        !backup.exists() && !journal.exists(),
        "unfinished install needs recovery"
    );
    model::save_json(
        &journal,
        &serde_json::json!({"target":id,"stage":stage.file_name().context("stage name")?,"backup":backup.file_name().context("backup name")?}),
    )?;
    if target.exists() {
        fs::rename(target, &backup)?;
        fs::File::open(parent)?.sync_all()?;
    }
    if let Err(error) = fs::rename(stage, target) {
        if backup.exists() {
            fs::rename(&backup, target)?;
        }
        let _ = fs::remove_file(&journal);
        return Err(error.into());
    }
    fs::File::open(parent)?.sync_all()?;
    if backup.exists() {
        fs::remove_dir_all(backup)?;
    }
    fs::remove_file(journal)?;
    fs::File::open(parent)?.sync_all()?;
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn interrupted_install_restores_old_or_keeps_committed_new() {
        for committed in [false, true] {
            let tmp = tempfile::tempdir().unwrap();
            let parent = tmp.path().join("plugins-native");
            fs::create_dir(&parent).unwrap();
            let backup = parent.join(".backup-demo");
            fs::create_dir(&backup).unwrap();
            fs::write(backup.join("version"), b"old").unwrap();
            let stage = parent.join(".stage-demo");
            fs::create_dir(&stage).unwrap();
            fs::write(stage.join("version"), b"new").unwrap();
            if committed {
                fs::rename(&stage, parent.join("demo")).unwrap();
            }
            model::save_json(
                &parent.join(".transaction-demo.json"),
                &serde_json::json!({"target":"demo","stage":".stage-demo","backup":".backup-demo"}),
            )
            .unwrap();
            recover_installs(tmp.path()).unwrap();
            assert_eq!(
                fs::read(parent.join("demo/version")).unwrap(),
                if committed { b"new" } else { b"old" }
            );
            assert!(!backup.exists());
            assert!(!stage.exists());
        }
    }
    #[test]
    fn traversal_and_symlink_archives_do_not_extract() {
        for name in ["../escape", "a/../../escape", "C:escape"] {
            let mut writer = zip::ZipWriter::new(Cursor::new(Vec::new()));
            writer
                .start_file(name, zip::write::SimpleFileOptions::default())
                .unwrap();
            writer.write_all(b"evil").unwrap();
            let bytes = writer.finish().unwrap().into_inner();
            let tmp = tempfile::tempdir().unwrap();
            assert!(extract(&bytes, tmp.path()).is_err());
            assert_eq!(fs::read_dir(tmp.path()).unwrap().count(), 0);
        }
    }
    #[test]
    fn page_bundle_round_trip_carries_assets_and_plugin_requirements() {
        let tmp = tempfile::tempdir().unwrap();
        let icon = tmp.path().join("icon.png");
        fs::write(&icon, b"icon").unwrap();
        let page = serde_json::json!({"keys":{"0x0":{"states":{"0":{"media":{"path":icon},"actions":[{"id":"example::hello"}]}}}}});
        let archive = tmp.path().join("page.zip");
        export_page("Example", &page, &archive).unwrap();
        let out = tmp.path().join("out");
        fs::create_dir_all(&out).unwrap();
        extract(&fs::read(archive).unwrap(), &out).unwrap();
        assert!(out.join("assets/0.png").exists());
        let meta = model::load_json(&out.join("package.json"), serde_json::Value::Null).unwrap();
        assert_eq!(meta["required_plugins"][0], "example");
    }
}
