//! Page documents retain unknown legacy fields so migration never discards plugin settings.
use crate::persistence::atomic_write;
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

pub fn valid_name(name: &str) -> Result<()> {
    ensure!(
        !name.trim().is_empty()
            && name.len() <= 180
            && !name.contains(['/', '\\', '\0'])
            && name != "."
            && name != "..",
        "invalid page or device name"
    );
    Ok(())
}
pub fn load_json(path: &Path, default: Value) -> Result<Value> {
    if fs::metadata(path).is_ok_and(|m| m.len() > 16 * 1024 * 1024) {
        bail!("JSON file exceeds 16 MiB: {}", path.display())
    }
    match fs::read(path) {
        Ok(bytes) => match serde_json::from_slice(&bytes) {
            Ok(value) => Ok(value),
            Err(error) => {
                // Preserve the exact damaged file before recovery. If quarantine fails, stop.
                let stamp = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
                fs::rename(path, path.with_extension(format!("corrupt-{stamp}.json")))
                    .context("quarantining invalid JSON")?;
                eprintln!("Quarantined invalid JSON in {}: {error}", path.display());
                save_json(path, &default)?;
                Ok(default)
            }
        },
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(default),
        Err(error) => Err(error.into()),
    }
}
fn load_validated(
    path: &Path,
    default: Value,
    validate: fn(&Value) -> Result<()>,
) -> Result<Value> {
    let value = load_json(path, default.clone())?;
    if let Err(error) = validate(&value) {
        let stamp = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
        fs::rename(path, path.with_extension(format!("corrupt-{stamp}.json")))
            .context("quarantining invalid document shape")?;
        eprintln!(
            "Quarantined invalid document in {}: {error}",
            path.display()
        );
        save_json(path, &default)?;
        return Ok(default);
    }
    Ok(value)
}
pub fn save_json(path: &Path, value: &Value) -> Result<()> {
    let bytes = serde_json::to_vec_pretty(value)?;
    if path.file_name().is_some_and(|n| n == "native.json") {
        crate::persistence::atomic_write_private(path, &bytes)?;
    } else {
        atomic_write(path, &bytes)?;
    }
    Ok(())
}
pub fn empty_page() -> Value {
    json!({"keys":{},"dials":{},"touchscreens":{},"settings":{}})
}
pub fn coords(value: &str) -> Result<String> {
    let parts: Vec<_> = value.split(['x', ',']).collect();
    ensure!(
        parts.len() == 2,
        "coordinates must be x,y or xxy, for example 0,0"
    );
    let x: u8 = parts[0].parse()?;
    let y: u8 = parts[1].parse()?;
    ensure!(x < 32 && y < 32, "coordinates outside supported layout");
    Ok(format!("{x}x{y}"))
}
pub fn state<'a>(page: &'a Value, family: &str, input: &str, state: usize) -> &'a Value {
    &page[family][input]["states"][state.to_string()]
}
pub fn state_mut<'a>(
    page: &'a mut Value,
    family: &str,
    input: &str,
    state: usize,
) -> Result<&'a mut Value> {
    ensure!(
        ["keys", "dials", "touchscreens", "infobar"].contains(&family),
        "unknown input family"
    );
    let root = page.as_object_mut().context("page must be an object")?;
    let group = root
        .entry(family)
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .context("input group must be an object")?;
    let input = group
        .entry(input)
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .context("input must be an object")?;
    let states = input
        .entry("states")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .context("states must be an object")?;
    Ok(states.entry(state.to_string()).or_insert_with(|| json!({})))
}

pub struct Documents {
    pub root: PathBuf,
    pub pages: BTreeMap<String, Value>,
    pub settings: Value,
    stickies: BTreeMap<String, Value>,
    pub revision: u64,
}
impl Documents {
    pub fn open(root: PathBuf) -> Result<Self> {
        fs::create_dir_all(root.join("pages"))?;
        fs::create_dir_all(root.join("plugins-native"))?;
        let mut pages = BTreeMap::new();
        for entry in fs::read_dir(root.join("pages"))? {
            let entry = entry?;
            let path = entry.path();
            if path.extension().is_some_and(|x| x == "json")
                && !path
                    .file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .contains(".corrupt-")
            {
                let name = path
                    .file_stem()
                    .context("page name")?
                    .to_string_lossy()
                    .into_owned();
                let value = load_validated(&path, empty_page(), validate_page)?;
                ensure!(
                    value.is_object(),
                    "page {name} is not an object; it was left intact"
                );
                pages.insert(name, value);
            }
        }
        let mut settings = load_validated(
            &root.join("settings/native.json"),
            json!({"devices":{},"ai":{"enabled":false},"cache_mib":64}),
            validate_settings,
        )?;
        ensure!(
            settings.is_object(),
            "native settings must be an object; existing file was left intact"
        );
        validate_settings(&settings)
            .context("invalid native settings; original file left intact")?;
        if !root.join("settings/native.json").exists() {
            let app = load_json(&root.join("settings/settings.json"), json!({}))?;
            settings["legacy"] = app.clone();
            settings["shrink_on_press"] =
                json!(app["general"]["shrink-on-press"].as_bool().unwrap_or(true));
            settings["hold_ms"] = json!(
                (app["general"]["hold-time"]
                    .as_f64()
                    .unwrap_or(0.5)
                    .clamp(0.1, 5.0)
                    * 1000.0) as u64
            );
            settings["auto_lock"] = app["system"]["lock-on-lock-screen"]
                .as_bool()
                .map_or(json!(true), Value::Bool);
            settings["keep_running"] = app["system"]["keep-running"]
                .as_bool()
                .map_or(json!(true), Value::Bool);
            let page_settings = load_json(&root.join("settings/pages.json"), json!({}))?;
            if let Ok(entries) = fs::read_dir(root.join("settings/decks")) {
                for entry in entries.flatten() {
                    let path = entry.path();
                    if path.extension().is_some_and(|e| e == "json") {
                        let serial = path
                            .file_stem()
                            .unwrap_or_default()
                            .to_string_lossy()
                            .into_owned();
                        let old = load_json(&path, json!({}))?;
                        let mut device = old.clone();
                        if !device.is_object() {
                            continue;
                        }
                        device["brightness"] = old["brightness"]["value"].clone();
                        device["page"] = page_settings["default-pages"][&serial]
                            .as_str()
                            .and_then(|p| Path::new(p).file_stem())
                            .map(|p| json!(p.to_string_lossy()))
                            .unwrap_or(Value::Null);
                        settings["devices"][&serial] = device;
                    }
                }
            }
            save_json(&root.join("settings/native.json"), &settings)?;
        }
        let mut stickies = BTreeMap::new();
        if let Ok(entries) = fs::read_dir(root.join("sticky")) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.extension().is_some_and(|e| e == "json")
                    && !path.to_string_lossy().contains(".corrupt-")
                {
                    let serial = path
                        .file_stem()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .into_owned();
                    stickies.insert(serial, load_validated(&path, empty_page(), validate_page)?);
                }
            }
        }
        let mut docs = Self {
            root,
            pages,
            settings,
            stickies,
            revision: 0,
        };
        if docs.pages.is_empty() {
            docs.create("Main")?;
        }
        Ok(docs)
    }
    pub fn create(&mut self, name: &str) -> Result<()> {
        valid_name(name)?;
        ensure!(!self.pages.contains_key(name), "page already exists");
        self.put(name, empty_page())
    }
    pub fn put(&mut self, name: &str, page: Value) -> Result<()> {
        valid_name(name)?;
        validate_page(&page)?;
        save_json(&self.root.join("pages").join(format!("{name}.json")), &page)?;
        self.pages.insert(name.into(), page);
        self.revision = self.revision.wrapping_add(1);
        Ok(())
    }
    pub fn edit(&mut self, name: &str, edit: impl FnOnce(&mut Value) -> Result<()>) -> Result<()> {
        let mut copy = self.pages.get(name).context("page not found")?.clone();
        edit(&mut copy)?;
        self.put(name, copy)
    }
    pub fn duplicate(&mut self, name: &str, new: &str) -> Result<()> {
        ensure!(!self.pages.contains_key(new), "page already exists");
        let page = self.pages.get(name).context("page not found")?.clone();
        self.put(new, page)
    }
    pub fn rename(&mut self, name: &str, new: &str) -> Result<()> {
        self.duplicate(name, new)?;
        // Keep the old file if removal fails, rather than losing either version.
        fs::remove_file(self.root.join("pages").join(format!("{name}.json")))?;
        self.pages.remove(name);
        Ok(())
    }
    pub fn delete(&mut self, name: &str) -> Result<()> {
        ensure!(self.pages.len() > 1, "keep at least one page");
        if !self.pages.contains_key(name) {
            bail!("page not found")
        };
        valid_name(name)?;
        fs::remove_file(self.root.join("pages").join(format!("{name}.json")))?;
        self.pages.remove(name);
        self.revision += 1;
        Ok(())
    }
    pub fn save_settings(&mut self) -> Result<()> {
        validate_settings(&self.settings)?;
        save_json(&self.root.join("settings/native.json"), &self.settings)?;
        self.revision += 1;
        Ok(())
    }
    pub fn sticky(&self, serial: &str) -> Result<Value> {
        valid_name(serial)?;
        Ok(self
            .stickies
            .get(serial)
            .cloned()
            .unwrap_or_else(empty_page))
    }
    pub fn put_sticky(&mut self, serial: &str, page: &Value) -> Result<()> {
        valid_name(serial)?;
        validate_page(page)?;
        save_json(
            &self.root.join("sticky").join(format!("{serial}.json")),
            page,
        )?;
        self.stickies.insert(serial.into(), page.clone());
        self.revision += 1;
        Ok(())
    }
}

pub fn validate_page(page: &Value) -> Result<()> {
    ensure!(page.is_object(), "page must be an object");
    for family in ["keys", "dials", "touchscreens", "infobar"] {
        let group = &page[family];
        ensure!(
            group.is_null() || group.is_object(),
            "{family} must be an object"
        );
        if let Some(inputs) = group.as_object() {
            for input in inputs.values() {
                ensure!(input.is_object(), "input must be an object");
                let states = &input["states"];
                ensure!(
                    states.is_null() || states.is_object(),
                    "states must be an object"
                );
                if let Some(states) = states.as_object() {
                    ensure!(states.len() <= 128, "too many input states");
                    for (number, state) in states {
                        ensure!(
                            number.parse::<u32>().is_ok(),
                            "state keys must be nonnegative integers"
                        );
                        ensure!(state.is_object(), "state must be an object");
                        if let Some(labels) = state["labels"].as_object() {
                            ensure!(
                                labels
                                    .values()
                                    .all(|label| label.is_object() || label.is_null()),
                                "labels must contain objects"
                            );
                        }
                        for property in ["labels", "media", "background"] {
                            ensure!(
                                state[property].is_null() || state[property].is_object(),
                                "{property} must be an object"
                            );
                        }
                        ensure!(
                            state["actions"].is_null() || state["actions"].is_array(),
                            "actions must be an array"
                        );
                    }
                }
            }
        }
    }
    Ok(())
}
pub fn validate_settings(settings: &Value) -> Result<()> {
    ensure!(settings.is_object(), "settings must be an object");
    ensure!(
        settings["devices"].is_null() || settings["devices"].is_object(),
        "devices must be an object"
    );
    if let Some(devices) = settings["devices"].as_object() {
        for (serial, device) in devices {
            valid_name(serial)?;
            ensure!(device.is_object(), "device settings must be an object");
            if !device["rotation"].is_null() {
                ensure!(
                    device["rotation"]
                        .as_u64()
                        .is_some_and(|r| [0, 90, 180, 270].contains(&r)),
                    "rotation must be 0, 90, 180 or 270"
                );
            }
        }
    }
    ensure!(
        settings["rules"].is_null() || settings["rules"].is_array(),
        "rules must be an array"
    );
    if let Some(rules) = settings["rules"].as_array() {
        for rule in rules {
            ensure!(rule.is_object(), "rules must be objects");
            for field in ["class", "title"] {
                if let Some(pattern) = rule[field].as_str() {
                    regex::Regex::new(pattern)
                        .with_context(|| format!("invalid {field} regular expression"))?;
                }
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn edits_retain_unknown_plugin_data_and_failed_edits_do_not_mutate() {
        let tmp = tempfile::tempdir().unwrap();
        let mut docs = Documents::open(tmp.path().into()).unwrap();
        docs.put("Main",json!({"future":{"secret":9},"keys":{"0x0":{"states":{"0":{"actions":[{"id":"python::old","settings":{"x":8}}]}}}}})).unwrap();
        docs.edit("Main", |p| {
            state_mut(p, "keys", "0x0", 0)?["labels"] = json!({"bottom":{"text":"Rust"}});
            Ok(())
        })
        .unwrap();
        assert_eq!(docs.pages["Main"]["future"]["secret"], 9);
        assert_eq!(
            state(&docs.pages["Main"], "keys", "0x0", 0)["actions"][0]["settings"]["x"],
            8
        );
        let before = docs.pages["Main"].clone();
        assert!(
            docs.edit("Main", |p| {
                p["future"] = json!(null);
                bail!("failed")
            })
            .is_err()
        );
        assert_eq!(docs.pages["Main"], before);
    }
    #[test]
    fn invalid_settings_shape_is_quarantined_and_healed_without_panicking() {
        let tmp = tempfile::tempdir().unwrap();
        let settings = tmp.path().join("settings");
        fs::create_dir(&settings).unwrap();
        fs::write(settings.join("native.json"), b"{\"devices\":[]}").unwrap();
        let docs = Documents::open(tmp.path().into()).unwrap();
        assert!(docs.settings["devices"].is_object());
        assert!(fs::read_dir(settings).unwrap().any(|entry| {
            entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .contains("corrupt-")
        }));
    }
    #[test]
    fn corruption_is_preserved_and_names_cannot_escape() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("bad.json");
        fs::write(&path, b"{broken").unwrap();
        assert_eq!(load_json(&path, json!({})).unwrap(), json!({}));
        assert!(fs::read_dir(tmp.path()).unwrap().any(|p| {
            p.unwrap()
                .file_name()
                .to_string_lossy()
                .contains("corrupt-")
        }));
        for name in ["../bad", "/bad", "a\\b", "..", ""] {
            assert!(valid_name(name).is_err())
        }
    }
}
