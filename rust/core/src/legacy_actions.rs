//! Conservative, reversible page migration from audited Python plugin schemas.
use anyhow::{Context, Result};
use serde_json::{Value, json};
use std::{fs, path::Path};

pub(crate) fn replacement(action: &Value) -> std::result::Result<Vec<Value>, &'static str> {
    let id = action["id"].as_str().unwrap_or("");
    let mut settings = action["settings"].as_object().cloned().unwrap_or_default();
    let target;
    match id {
        "com_core447_OSPlugin::Launch" => {
            let path = settings
                .get("app_path")
                .cloned()
                .filter(Value::is_string)
                .ok_or("application path missing")?;
            settings.insert("path".into(), path);
            target = "native::launch";
        }
        "com_core447_OSPlugin::Hotkey" => {
            crate::uinput::sequence(&json!(settings)).map_err(|_| "invalid evdev key sequence")?;
            settings.insert("operation".into(), json!("keys"));
            target = "native::input";
        }
        "com_core447_OSPlugin::Click" => {
            settings.insert("operation".into(), json!("click"));
            target = "native::input";
        }
        "com_core447_OSPlugin::MoveXY" => {
            settings.insert("operation".into(), json!("position"));
            target = "native::input";
        }
        "com_core447_OSPlugin::Delay" => {
            target = "native::delay";
        }
        id if id.starts_with("com_core447_DeckPlugin::") => {
            target = match id.split_once("::").unwrap().1 {
                "ChangePage" => {
                    let path = settings
                        .get("selected_page")
                        .and_then(Value::as_str)
                        .ok_or("target page missing")?;
                    let page = Path::new(path)
                        .file_stem()
                        .and_then(|p| p.to_str())
                        .ok_or("invalid target page")?
                        .to_owned();
                    settings.insert("page".into(), json!(page));
                    if let Some(serial) = settings
                        .get("deck_number")
                        .and_then(Value::as_str)
                        .map(str::to_owned)
                    {
                        settings.insert("serial".into(), json!(serial));
                    }
                    "native::page"
                }
                "GoToSleep" => "native::sleep",
                "GoToPreviousPage" => "native::previous-page",
                "ChangeBrightness" => {
                    let value = settings.get("brightness").cloned().unwrap_or(json!(50));
                    settings.insert("value".into(), value);
                    "native::brightness"
                }
                "AdjustBrightness" => "native::adjust-brightness",
                "ChangeState" => {
                    if !settings.get("state").is_some_and(Value::is_number) {
                        return Err("target state missing");
                    }
                    if let Some(target) = settings
                        .get("target_input")
                        .filter(|t| t.is_object())
                        .cloned()
                    {
                        settings.insert("family".into(), target["input_type"].clone());
                        settings.insert("input".into(), target["json_identifier"].clone());
                    }
                    "native::state"
                }
                _ => return Err("no audited native Deck action"),
            };
        }
        id if id.starts_with("com_core447_OBSPlugin::") => {
            let suffix = id.split_once("::").unwrap().1;
            let mut data = json!({});
            let operation = match suffix {
                "OBSStats" => "GetStats",
                "ToggleStream" | "ToggleRecord" | "RecPlayPause" | "ToggleReplayBuffer"
                | "SaveReplayBuffer" | "ToggleStudioMode" | "TriggerTransition" => suffix,
                "ToggleVirtualCamera" => "ToggleVirtualCam",
                "SwitchScene" => {
                    data["sceneName"] = settings
                        .get("scene")
                        .cloned()
                        .filter(Value::is_string)
                        .ok_or("OBS scene missing")?;
                    "SetCurrentProgramScene"
                }
                "SwitchSceneCollection" => {
                    data["sceneCollectionName"] = settings
                        .get("scene_collection")
                        .cloned()
                        .filter(Value::is_string)
                        .ok_or("OBS scene collection missing")?;
                    "SetCurrentSceneCollection"
                }
                "ToggleInputMute" => {
                    data["inputName"] = settings
                        .get("input")
                        .cloned()
                        .filter(Value::is_string)
                        .ok_or("OBS input missing")?;
                    "ToggleInputMute"
                }
                "SetInputMute" => {
                    data["inputName"] = settings
                        .get("input")
                        .cloned()
                        .filter(Value::is_string)
                        .ok_or("OBS input missing")?;
                    data["inputMuted"] =
                        json!(settings.get("set_mode").and_then(Value::as_str) == Some("DISABLED"));
                    "SetInputMute"
                }
                "InputDial" => {
                    data["inputName"] = settings
                        .get("input")
                        .cloned()
                        .filter(Value::is_string)
                        .ok_or("OBS input missing")?;
                    let connection = settings
                        .get("connection_id")
                        .cloned()
                        .unwrap_or(json!("default"));
                    settings.insert("connection".into(), connection);
                    settings.insert("operation".into(), json!("ToggleInputMute"));
                    settings.insert("presentation".into(), json!(suffix));
                    settings.insert("data".into(), data);
                    let mut result = action.clone();
                    result["id"] = json!("native::obs");
                    result["event"] = json!("press");
                    result["settings"] = json!(settings);
                    let mut cw = result.clone();
                    cw["event"] = json!("turn-cw");
                    cw["settings"]["operation"] = json!("AdjustInputVolume");
                    cw["settings"]["data"]["increment"] = json!(5.0);
                    cw["settings"]["data"]["volume_curve"] = json!("legacy");
                    let mut ccw = cw.clone();
                    ccw["event"] = json!("turn-ccw");
                    ccw["settings"]["data"]["increment"] = json!(-5.0);
                    return Ok(vec![result, cw, ccw]);
                }
                "ToggleSceneItemEnabled" | "SetSceneItemEnabled" => {
                    data["sceneName"] = settings
                        .get("scene")
                        .cloned()
                        .filter(Value::is_string)
                        .ok_or("OBS scene missing")?;
                    data["sourceName"] = settings
                        .get("item")
                        .cloned()
                        .filter(Value::is_string)
                        .ok_or("OBS scene item missing")?;
                    data["enabled"] =
                        json!(settings.get("set_mode").and_then(Value::as_str) != Some("DISABLED"));
                    suffix
                }
                "ToggleSceneFilter" | "SetSceneFilter" => {
                    data["sourceName"] = settings
                        .get("scene")
                        .cloned()
                        .filter(Value::is_string)
                        .ok_or("OBS filter source missing")?;
                    data["filterName"] = settings
                        .get("filter")
                        .cloned()
                        .filter(Value::is_string)
                        .ok_or("OBS filter missing")?;
                    data["enabled"] =
                        json!(settings.get("set_mode").and_then(Value::as_str) != Some("DISABLED"));
                    suffix
                }
                _ => return Err("this OBS action requires a separate native implementation"),
            };
            let connection = settings
                .get("connection_id")
                .cloned()
                .unwrap_or(json!("default"));
            settings.insert("connection".into(), connection);
            settings.insert("operation".into(), json!(operation));
            settings.insert("presentation".into(), json!(suffix));
            settings.insert("data".into(), data);
            target = "native::obs";
        }
        id if id.starts_with("com_core447_VolumeMixer::") => {
            let suffix = id.split_once("::").unwrap().1;
            let operation = match suffix {
                "Open" => "open",
                "Exit" => "exit",
                "MoveLeft" => "left",
                "MoveRight" => "right",
                "VolumeMute" => "mute",
                "VolumeUp" => "volume-up",
                "VolumeDown" => "volume-down",
                "Dial" => "mute",
                _ => return Err("unknown mixer action"),
            };
            settings.insert("operation".into(), json!(operation));
            target = "native::mixer";
            if suffix == "Dial" {
                let mut result = action.clone();
                result["id"] = json!(target);
                result["settings"] = json!(settings);
                result["event"] = json!("release");
                let mut cw = result.clone();
                cw["event"] = json!("turn-cw");
                cw["settings"]["operation"] = json!("volume-up");
                let mut ccw = result.clone();
                ccw["event"] = json!("turn-ccw");
                ccw["settings"]["operation"] = json!("volume-down");
                return Ok(vec![result, cw, ccw]);
            }
        }
        id if ["CPU", "RAM", "CPUTemp", "CPU_Graph", "RAM_Graph", "Ping"]
            .iter()
            .any(|suffix| id == format!("com_core447_OSPlugin::{suffix}")) =>
        {
            settings.insert("metric".into(), json!(id.split_once("::").unwrap().1));
            target = "native::system";
        }
        "com_core447_OSPlugin::OpenInBrowser" => {
            let url = settings
                .get("url")
                .and_then(Value::as_str)
                .ok_or("URL is missing")?;
            let url = if url.contains(":") {
                url.to_owned()
            } else {
                format!("https://{url}")
            };
            settings.insert("url".into(), json!(url));
            target = "native::url";
        }
        "com_core447_OSPlugin::WriteText" => {
            if !settings.get("text").is_some_and(Value::is_string) {
                return Err("text is missing");
            }
            // The native typing helper accepts milliseconds; preserve the old per-character delay.
            settings.insert(
                "delay_ms".into(),
                json!(
                    (settings
                        .get("delay")
                        .and_then(Value::as_f64)
                        .unwrap_or(0.01)
                        * 1000.0)
                        .round()
                ),
            );
            target = "native::text";
        }
        "com_core447_OSPlugin::EasyHotkey" => {
            let text = settings
                .get("hotkey")
                .and_then(Value::as_str)
                .ok_or("hotkey is missing")?
                .to_owned();
            settings.insert("text".into(), json!(text));
            target = "native::hotkey";
        }
        "com_core447_OSPlugin::RunCommand" | "com_core447_OSPlugin::EasyCommand" => {
            if !settings.get("command").is_some_and(Value::is_string) {
                return Err("command is missing");
            }
            settings.entry("detached").or_insert(json!(true));
            target = "native::shell";
        }
        id if id.starts_with("com_core447_MediaPlugin::") => {
            let suffix = id.split_once("::").unwrap().1;
            let method = match suffix {
                "Play" => "Play",
                "Pause" => "Pause",
                "PlayPause" => "PlayPause",
                "Next" => "Next",
                "Previous" => "Previous",
                "MediaDial" => "PlayPause",
                "Info" => "Info",
                "Thumbnail" => "Thumbnail",
                _ => {
                    return Err(
                        "artwork, metadata labels and custom media UI need a native plugin",
                    );
                }
            };
            let player = settings
                .get("player_name")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            settings.insert("player".into(), json!(player));
            settings.insert("method".into(), json!(method));
            settings.insert("presentation".into(), json!(suffix));
            let mut result = action.clone();
            result["id"] = json!("native::media");
            result["settings"] = json!(settings);
            result["event"] = json!("press");
            if suffix == "MediaDial" {
                let mut cw = result.clone();
                cw["event"] = json!("turn-cw");
                cw["settings"]["method"] = json!("Next");
                let mut ccw = result.clone();
                ccw["event"] = json!("turn-ccw");
                ccw["settings"]["method"] = json!("Previous");
                return Ok(vec![result, cw, ccw]);
            }
            return Ok(vec![result]);
        }
        _ => return Err("no audited native replacement for this action"),
    }
    let mut result = action.clone();
    result["id"] = json!(target);
    result["settings"] = json!(settings);
    result["event"] = json!(if target == "native::input"
        && (settings_lifecycle(&result["settings"]))
    {
        "lifecycle"
    } else {
        "press"
    });
    Ok(vec![result])
}

fn settings_lifecycle(settings: &Value) -> bool {
    settings["repeat"].as_bool().unwrap_or(false)
        || settings["hold_until_release"].as_bool().unwrap_or(false)
}

pub fn obs_connections(root: &Path) -> Result<Value> {
    let path = root.join("settings/plugins/com_core447_OBSPlugin/settings.json");
    if !path.exists() {
        return Ok(json!({}));
    }
    let legacy = crate::model::load_json(&path, json!({}))?;
    let mut connections = json!({});
    if let Some(profiles) = legacy["connections"].as_array() {
        for profile in profiles {
            if let Some(id) = profile["id"].as_str() {
                connections[id] = profile.clone();
            }
        }
    } else {
        connections["default"] = json!({"host":legacy["ip"].as_str().unwrap_or("localhost"),"port":legacy["port"].as_u64().or_else(|| legacy["port"].as_str().and_then(|port|port.parse().ok())).unwrap_or(4455),"password":legacy["password"].as_str().unwrap_or("")});
    }
    Ok(connections)
}

pub fn translate(page: &mut Value, document: &str, apply: bool) -> Value {
    let mut converted = Vec::new();
    let mut remaining = Vec::new();
    for family in ["keys", "dials", "touchscreens", "infobar"] {
        let Some(inputs) = page.get_mut(family).and_then(Value::as_object_mut) else {
            continue;
        };
        for (input, config) in inputs {
            let Some(states) = config.get_mut("states").and_then(Value::as_object_mut) else {
                continue;
            };
            for (state, value) in states {
                let Some(actions) = value.get_mut("actions").and_then(Value::as_array_mut) else {
                    continue;
                };
                let mut new = Vec::new();
                let mut index_map = Vec::new();
                for (index, action) in actions.iter().enumerate() {
                    index_map.push(new.len());
                    let id = action["id"].as_str().unwrap_or("");
                    if id.starts_with("native::") || id == "example::hello" {
                        new.push(action.clone());
                        continue;
                    }
                    let location = json!({"document":document,"family":family,"input":input,"state":state,"index":index,"id":id});
                    match replacement(action) {
                        Ok(replacements) => {
                            let mut item = location;
                            item["replacement"] = replacements.clone().into();
                            converted.push(item);
                            new.extend(replacements);
                        }
                        Err(reason) => {
                            let mut item = location;
                            item["reason"] = json!(reason);
                            remaining.push(item);
                            new.push(action.clone());
                        }
                    }
                }
                if apply {
                    *actions = new;
                    for name in ["image-control-action", "background-control-action"] {
                        if let Some(index) = value[name]
                            .as_u64()
                            .and_then(|index| index_map.get(index as usize))
                        {
                            value[name] = json!(index);
                        }
                    }
                    if let Some(owners) = value["label-control-actions"].as_array_mut() {
                        for owner in owners {
                            if let Some(index) = owner
                                .as_u64()
                                .and_then(|index| index_map.get(index as usize))
                            {
                                *owner = json!(index);
                            }
                        }
                    }
                }
            }
        }
    }
    json!({"converted":converted,"remaining":remaining})
}

/// Back up exact source bytes before replacing anything. Re-running is idempotent.
pub fn migrate_file(path: &Path, root: &Path, apply: bool) -> Result<Value> {
    let bytes = fs::read(path)?;
    let mut page: Value = serde_json::from_slice(&bytes)?;
    let relative = path
        .strip_prefix(root)
        .context("migration source outside data root")?;
    let mut report = translate(&mut page, &relative.to_string_lossy(), apply);
    if apply
        && report["converted"]
            .as_array()
            .is_some_and(|a| !a.is_empty())
    {
        use sha2::{Digest, Sha256};
        let digest = format!("{:x}", Sha256::digest(&bytes));
        let backup = root
            .join("backups/native-actions")
            .join(relative)
            .with_extension(format!("{digest}.json"));
        fs::create_dir_all(backup.parent().context("backup parent")?)?;
        if backup.exists() {
            anyhow::ensure!(
                fs::read(&backup)? == bytes,
                "migration backup does not match original"
            );
        } else {
            crate::persistence::atomic_write_private(&backup, &bytes)?;
        }
        crate::model::save_json(path, &page)?;
        report["backup"] = json!(backup.strip_prefix(root)?.to_string_lossy());
    }
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn conversion_preserves_unknown_fields_and_all_command_modes() {
        let mut page = json!({"unknown":42,"keys":{"0x0":{"states":{"0":{"actions":[
            {"id":"com_core447_OSPlugin::WriteText","settings":{"text":"Hello 🦀","delay":0.03,"unknown":17},"sticky":true},
            {"id":"com_core447_OSPlugin::RunCommand","settings":{"command":"date","auto_run":1}},
            {"id":"missing::action","settings":{"secret":"retained"}}
        ]}}}},"dials":{"0":{"states":{"0":{"actions":[{"id":"com_core447_MediaPlugin::MediaDial","settings":{"player_name":"VLC"}}]}}}}});
        let original = page.clone();
        let preview = translate(&mut page, "Test", false);
        assert_eq!(page, original);
        assert_eq!(preview["converted"].as_array().unwrap().len(), 3);
        assert_eq!(preview["remaining"].as_array().unwrap().len(), 1);
        translate(&mut page, "Test", true);
        assert_eq!(page["unknown"], 42);
        let text = &page["keys"]["0x0"]["states"]["0"]["actions"][0];
        assert_eq!(text["settings"]["delay_ms"], 30.0);
        assert_eq!(text["settings"]["unknown"], 17);
        assert_eq!(text["sticky"], true);
        assert_eq!(
            page["keys"]["0x0"]["states"]["0"]["actions"][1],
            json!({"id":"native::shell","event":"press","settings":{"command":"date","auto_run":1,"detached":true}})
        );
        let dial = page["dials"]["0"]["states"]["0"]["actions"]
            .as_array()
            .unwrap();
        assert_eq!(dial.len(), 3);
        assert_eq!(dial[1]["settings"]["method"], "Next");
        assert_eq!(dial[2]["event"], "turn-ccw");
        assert!(
            translate(&mut page, "Test", true)["converted"]
                .as_array()
                .unwrap()
                .is_empty()
        );
    }
    #[test]
    fn dial_expansion_remaps_visual_owners_and_retains_all_modes() {
        let mut page = json!({"keys":{"0x0":{"states":{"0":{"image-control-action":1,"label-control-actions":[0,1,null],"background-control-action":1,"actions":[
            {"id":"com_core447_MediaPlugin::MediaDial","settings":{}},
            {"id":"com_core447_OSPlugin::RunCommand","settings":{"command":"printf hello","auto_run":1,"display_output":true,"interactive_shell":true,"keep_auto_run_in_background":true,"detached":false}}
        ]}}}},"dials":{"0":{"states":{"0":{"actions":[{"id":"com_core447_OSPlugin::Hotkey","settings":{"keys":[[29,1],[30,1],[30,0],[29,0]],"repeat":true,"hold_until_release":false}}]}}}}});
        let report = translate(&mut page, "fixture", true);
        assert!(report["remaining"].as_array().unwrap().is_empty());
        let state = &page["keys"]["0x0"]["states"]["0"];
        assert_eq!(state["image-control-action"], 3);
        assert_eq!(state["label-control-actions"], json!([0, 3, null]));
        assert_eq!(state["background-control-action"], 3);
        assert_eq!(
            state["actions"][3]["settings"]["keep_auto_run_in_background"],
            true
        );
        assert_eq!(
            page["dials"]["0"]["states"]["0"]["actions"][0]["event"],
            "lifecycle"
        );
    }
    #[test]
    fn exact_backup_precedes_atomic_migration_and_is_reused() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("pages/Test.json");
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let bytes=br#"{ "keys": {"0x0":{"states":{"0":{"actions":[{"id":"com_core447_OSPlugin::EasyCommand","settings":{"command":"echo hello"}}]}}}} }"#;
        fs::write(&path, bytes).unwrap();
        let report = migrate_file(&path, tmp.path(), true).unwrap();
        assert_eq!(
            fs::read(tmp.path().join(report["backup"].as_str().unwrap())).unwrap(),
            bytes
        );
        let page: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(
            page["keys"]["0x0"]["states"]["0"]["actions"][0]["id"],
            "native::shell"
        );
        assert!(
            migrate_file(&path, tmp.path(), true).unwrap()["converted"]
                .as_array()
                .unwrap()
                .is_empty()
        );
    }
}
