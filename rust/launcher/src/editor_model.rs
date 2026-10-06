//! Toolkit-independent action schemas and editing helpers.
use anyhow::Result;
use deckard_core::{model, plugin};
use serde_json::{Value, json};
use std::path::PathBuf;

pub(crate) fn set_nested(value: &mut Value, path: &[&str], new: Value) {
    let mut cursor = value;
    for key in path {
        if !cursor.is_object() {
            *cursor = json!({});
        }
        cursor = cursor
            .as_object_mut()
            .unwrap()
            .entry(*key)
            .or_insert(Value::Null);
    }
    *cursor = new;
}
pub(crate) fn action_definitions(
    plugins: &[plugin::Installed],
) -> Vec<(String, String, Vec<plugin::Field>)> {
    let mut result = Vec::new();
    for (id, name, key, label, kind) in [
        ("url", "Open URL", "url", "URL", "string"),
        (
            "command",
            "Run command",
            "argv",
            "Arguments as a JSON array",
            "array",
        ),
        ("page", "Switch page", "page", "Page name", "string"),
        ("state", "Switch state", "state", "State", "number"),
        (
            "brightness",
            "Set brightness",
            "value",
            "Brightness (0–100)",
            "number",
        ),
        ("text", "Type text", "text", "Text", "string"),
        ("hotkey", "Press key", "text", "Key name", "string"),
        ("sleep", "Sleep", "", "", "string"),
    ] {
        result.push((
            format!("native::{id}"),
            name.into(),
            if key.is_empty() {
                vec![]
            } else {
                vec![plugin::Field {
                    key: key.into(),
                    label: label.into(),
                    kind: kind.into(),
                    default: Value::Null,
                }]
            },
        ));
    }
    for plugin in plugins {
        for action in &plugin.manifest.actions {
            result.push((
                format!("{}::{}", plugin.manifest.id, action.id),
                format!("{} · {}", plugin.manifest.name, action.name),
                action.fields.clone(),
            ));
        }
    }
    let field = |key: &str, label: &str, kind: &str, default: Value| plugin::Field {
        key: key.into(),
        label: label.into(),
        kind: kind.into(),
        default,
    };
    result.extend([
        (
            "native::previous-page".into(),
            "Previous page".into(),
            vec![],
        ),
        (
            "native::adjust-brightness".into(),
            "Adjust brightness".into(),
            vec![
                field("adjust", "Change (%)", "number", json!(5)),
                field("min_brightness", "Minimum (%)", "number", json!(0)),
            ],
        ),
        (
            "native::audio".into(),
            "Audio volume / mute".into(),
            vec![
                field(
                    "target",
                    "output / microphone / stream:ID",
                    "string",
                    json!("output"),
                ),
                field(
                    "operation",
                    "toggle-mute / set-volume / adjust-volume",
                    "string",
                    json!("toggle-mute"),
                ),
                field("value", "Volume or change (%)", "number", json!(5)),
            ],
        ),
        (
            "native::mixer".into(),
            "Application volume mixer".into(),
            vec![
                field(
                    "operation",
                    "open / exit / left / right",
                    "string",
                    json!("open"),
                ),
                field("increments", "Volume step (%)", "number", json!(10)),
            ],
        ),
        (
            "native::obs".into(),
            "OBS Studio".into(),
            vec![
                field(
                    "connection",
                    "Connection profile",
                    "string",
                    json!("default"),
                ),
                field(
                    "operation",
                    "OBS WebSocket operation",
                    "string",
                    json!("ToggleRecord"),
                ),
                field("data", "Request fields as JSON object", "array", json!({})),
            ],
        ),
        (
            "native::input".into(),
            "Linux key sequence / mouse".into(),
            vec![
                field("operation", "keys / click / move", "string", json!("keys")),
                field("keys", "Evdev [code, value] pairs", "array", json!([])),
                field("delay", "Delay per key (seconds)", "number", json!(0.02)),
                field("button", "left / middle / right", "string", json!("left")),
                field("x", "Mouse horizontal movement", "number", json!(0)),
                field("y", "Mouse vertical movement", "number", json!(0)),
            ],
        ),
        (
            "native::launch".into(),
            "Launch application".into(),
            vec![field("path", "Application executable", "string", json!(""))],
        ),
        (
            "native::delay".into(),
            "Delay action sequence".into(),
            vec![field("delay", "Delay (0–5 seconds)", "number", json!(0))],
        ),
    ]);
    result.push((
        "native::media".into(),
        "Media playback (MPRIS)".into(),
        vec![
            plugin::Field {
                key: "method".into(),
                label: "Play / Pause / PlayPause / Next / Previous / Stop".into(),
                kind: "string".into(),
                default: json!("PlayPause"),
            },
            plugin::Field {
                key: "player".into(),
                label: "Player identity or bus name (empty = all players)".into(),
                kind: "string".into(),
                default: json!(""),
            },
        ],
    ));
    result.push((
        "native::shell".into(),
        "Run shell command".into(),
        vec![
            plugin::Field {
                key: "command".into(),
                label: "Shell command".into(),
                kind: "string".into(),
                default: json!(""),
            },
            plugin::Field {
                key: "detached".into(),
                label: "Launch in background".into(),
                kind: "bool".into(),
                default: json!(true),
            },
        ],
    ));
    result.extend(
        deckard_core::builtins::definitions()
            .into_iter()
            .map(|a| (format!("native::{}", a.id), a.name, a.fields)),
    );
    result
}
pub(crate) fn has_privileged_action(v: &Value) -> bool {
    if v["id"].as_str().is_some_and(|id| {
        matches!(
            id,
            "native::OSPlugin-RunCommand"
                | "native::OSPlugin-EasyCommand"
                | "native::OSPlugin-Launch"
                | "native::OSPlugin-Hotkey"
                | "native::OSPlugin-EasyHotkey"
                | "native::OSPlugin-WriteText"
                | "native::OSPlugin-Click"
                | "native::OSPlugin-MoveXY"
        )
    }) {
        return true;
    }
    if v["id"].as_str().is_some_and(|id| {
        [
            "native::command",
            "native::shell",
            "native::launch",
            "native::input",
            "native::text",
            "native::hotkey",
        ]
        .contains(&id)
    }) {
        return true;
    }
    match v {
        Value::Array(a) => a.iter().any(has_privileged_action),
        Value::Object(o) => o.values().any(has_privileged_action),
        _ => false,
    }
}
pub(crate) fn autostart(enable: bool) -> Result<()> {
    let config = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".config")
        });
    let path = config.join("autostart/deckard.desktop");
    if enable {
        let exe = std::env::var_os("APPIMAGE")
            .map(PathBuf::from)
            .unwrap_or(std::env::current_exe()?);
        let wrapper = exe
            .parent()
            .map(|dir| dir.join("deckard"))
            .filter(|p| p.is_file());
        let exe = if std::env::var_os("APPIMAGE").is_some() {
            exe
        } else {
            wrapper.unwrap_or(exe)
        };
        let exe = exe
            .to_string_lossy()
            .replace('\\', "\\\\")
            .replace('"', "\\\"")
            .replace('`', "\\`")
            .replace('$', "\\$");
        deckard_core::persistence::atomic_write(&path,format!("[Desktop Entry]\nType=Application\nName=Deckard\nExec=\"{exe}\" --daemon-only\nTerminal=false\n").as_bytes())?;
    } else if path.exists() {
        std::fs::remove_file(path)?;
    }
    Ok(())
}
pub(crate) fn create_asset_pack(
    source: &std::path::Path,
    root: &std::path::Path,
    name: &str,
    description: &str,
    banner: Option<&std::path::Path>,
    kind: &str,
) -> Result<PathBuf> {
    model::valid_name(name)?;
    let parent = root.join(match kind {
        "wallpaper" => "wallpapers-native",
        "sdplusbar" => "sdplusbar-native",
        _ => "icons-native",
    });
    let destination = parent.join(name);
    anyhow::ensure!(!destination.exists(), "pack already exists");
    std::fs::create_dir_all(&parent)?;
    let staging = tempfile::Builder::new()
        .prefix(".pack-")
        .tempdir_in(&parent)?;
    let extracted = tempfile::tempdir()?;
    let source = if source.is_file() {
        deckard_core::store::extract(&std::fs::read(source)?, extracted.path())?;
        extracted.path()
    } else {
        source
    };
    fn copy(
        directory: &std::path::Path,
        base: &std::path::Path,
        target: &std::path::Path,
        count: &mut usize,
        bytes: &mut u64,
        depth: usize,
    ) -> Result<()> {
        anyhow::ensure!(depth < 20, "pack folders too deep");
        for entry in std::fs::read_dir(directory)? {
            let entry = entry?;
            let ty = entry.file_type()?;
            if ty.is_dir() {
                copy(&entry.path(), base, target, count, bytes, depth + 1)?;
            } else if ty.is_file()
                && entry.path().extension().is_some_and(|e| {
                    [
                        "png", "jpg", "jpeg", "webp", "gif", "svg", "bmp", "mp4", "webm",
                    ]
                    .iter()
                    .any(|x| e.to_string_lossy().eq_ignore_ascii_case(x))
                })
            {
                let length = entry.metadata()?.len();
                anyhow::ensure!(length <= 32 * 1024 * 1024, "asset exceeds size limit");
                *bytes += length;
                *count += 1;
                anyhow::ensure!(
                    *bytes <= 256 * 1024 * 1024 && *count <= 50000,
                    "pack exceeds import limits"
                );
                let path = entry.path();
                let target = target.join(path.strip_prefix(base)?);
                std::fs::create_dir_all(target.parent().unwrap())?;
                std::fs::copy(path, target)?;
            }
        }
        Ok(())
    }
    let mut count = 0;
    let mut bytes = 0;
    copy(source, source, staging.path(), &mut count, &mut bytes, 0)?;
    anyhow::ensure!(count > 0, "No images found in this pack");
    if let Some(banner) = banner {
        anyhow::ensure!(
            std::fs::metadata(banner)?.len() <= 32 * 1024 * 1024,
            "banner exceeds limit"
        );
        std::fs::copy(banner, staging.path().join("banner.png"))?;
    }
    model::save_json(
        &staging.path().join("package.json"),
        &json!({"api":1,"id":name,"name":name,"kind":kind,"description":description,"banner":banner.map(|_|"banner.png")}),
    )?;
    std::fs::rename(staging.path(), &destination)?;
    Ok(destination)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ai_review_gates_native_aliases_inside_proposed_pages() {
        let page = json!({"keys":{"0x0":{"states":{"0":{"actions":[{"id":"native::OSPlugin-RunCommand","settings":{"command":"touch /tmp/should-require-review"}}]}}}}});
        assert!(has_privileged_action(&page));
        assert!(has_privileged_action(
            &json!({"id":"native::OSPlugin-Hotkey","settings":{"keys":[[29,1],[29,0]]}})
        ));
        assert!(!has_privileged_action(
            &json!({"id":"native::MediaPlugin-Info","settings":{"player_name":"Music"}})
        ));
    }
}
