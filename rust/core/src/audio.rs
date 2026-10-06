//! PulseAudio / PipeWire-Pulse control through a bundled native pactl helper.
use anyhow::{Context, Result, ensure};
use elgato_streamdeck::info::Kind;
use serde_json::Value;
use serde_json::json;
use std::time::Duration;
#[derive(Default)]
pub struct Mixer {
    pub offsets: std::collections::HashMap<String, usize>,
    pub originals: std::collections::HashMap<String, String>,
    pub increment: f64,
}
pub fn mixer_page(
    kind: Kind,
    rotation: u16,
    streams: &[Stream],
    offset: usize,
    increment: f64,
) -> Value {
    let (rows, cols) = crate::render::layout(kind, rotation);
    let mut page = crate::model::empty_page();
    page["deckard"] = json!({"native-mixer":true});
    for row in 0..rows {
        for col in 0..cols {
            let mut actions = Vec::new();
            let label;
            if col == 0 || (rows == 2 && row == 1 && col == 1) {
                let (name, operation) = if col == 1 {
                    ("Next", "right")
                } else {
                    match row {
                        0 => ("Back", "exit"),
                        1 => ("Prev", "left"),
                        _ => ("Next", "right"),
                    }
                };
                label = name.to_owned();
                actions.push(json!({"id":"native::mixer","event":"press","settings":{"operation":operation}}));
            } else if let Some(stream) = streams.get(offset + col as usize - 1) {
                label = format!(
                    "{}\n{}",
                    stream.name,
                    if stream.muted {
                        "Muted".into()
                    } else {
                        format!("{:.0}%", stream.volume)
                    }
                );
                let (operation, value) = match row {
                    0 => ("toggle-mute", 0.0),
                    1 => ("adjust-volume", increment),
                    _ => ("adjust-volume", -increment),
                };
                actions.push(json!({"id":"native::audio","event":"press","settings":{"target":format!("stream:{}",stream.id),"operation":operation,"value":value}}));
            } else {
                label = "No stream".into();
            }
            page["keys"][format!("{col}x{row}")] = json!({"states":{"0":{"labels":{"center":{"text":label,"font-size":13}},"background":{"color":[25,35,45,255]},"actions":actions}}});
            if col > 0
                && let Some(stream) = streams.get(offset + col as usize - 1)
            {
                let state = &mut page["keys"][format!("{col}x{row}")]["states"]["0"];
                if row == 0 {
                    state["native-visual"] = json!({"symbol":if stream.muted{"mute"}else{"audio"},"active":!stream.muted});
                    if let Some(path) = &stream.icon {
                        state["media"] = json!({"path":path,"size":0.6,"valign":-0.5});
                    }
                }
                if stream.muted {
                    state["labels"]["center"]["color"] = json!([255, 90, 90, 255]);
                }
            }
        }
    }
    for dial in 0..kind.encoder_count() {
        let mut actions = Vec::new();
        let mut label = "No stream".into();
        if let Some(stream) = streams.get(offset + dial as usize) {
            label = format!(
                "{}\n{}",
                stream.name,
                if stream.muted {
                    "Muted".into()
                } else {
                    format!("{:.0}%", stream.volume)
                }
            );
            for (event, operation, value) in [
                ("press", "toggle-mute", 0.0),
                ("turn-cw", "adjust-volume", increment),
                ("turn-ccw", "adjust-volume", -increment),
            ] {
                actions.push(json!({"id":"native::audio","event":event,"settings":{"target":format!("stream:{}",stream.id),"operation":operation,"value":value}}));
            }
        }
        page["dials"][dial.to_string()] = json!({"states":{"0":{"labels":{"center":{"text":label,"font-size":16}},"actions":actions}}});
        if let Some(stream) = streams.get(offset + dial as usize) {
            let state = &mut page["dials"][dial.to_string()]["states"]["0"];
            state["native-visual"] = json!({"progress":stream.volume/100.0,"bar-color":if stream.muted{json!([240,65,75,255])}else{json!([90,180,245,255])}});
        }
    }
    page
}
#[derive(Clone, Debug)]
pub struct Stream {
    pub id: u64,
    pub name: String,
    pub volume: f64,
    pub muted: bool,
    pub icon: Option<String>,
}
pub fn streams() -> Result<Vec<Stream>> {
    let text = crate::desktop::run_command(
        &[
            "pactl".into(),
            "--format=json".into(),
            "list".into(),
            "sink-inputs".into(),
        ],
        Duration::from_secs(2),
    )?;
    parse_streams(&serde_json::from_str(&text)?)
}
fn parse_streams(value: &Value) -> Result<Vec<Stream>> {
    value
        .as_array()
        .context("invalid PulseAudio stream list")?
        .iter()
        .map(|input| {
            let volumes = input["volume"]
                .as_object()
                .context("missing stream volume")?;
            let volume = volumes
                .values()
                .filter_map(|v| v["value"].as_f64())
                .sum::<f64>()
                / volumes.len().max(1) as f64
                / 65536.0
                * 100.0;
            Ok(Stream {
                id: input["index"].as_u64().context("missing stream ID")?,
                name: input["properties"]["application.name"]
                    .as_str()
                    .or_else(|| input["name"].as_str())
                    .unwrap_or("Audio stream")
                    .into(),
                volume,
                muted: input["mute"].as_bool().unwrap_or(false),
                icon: input["properties"]["application.icon_name"]
                    .as_str()
                    .or_else(|| input["properties"]["application.process.binary"].as_str())
                    .and_then(icon_path),
            })
        })
        .collect()
}
pub fn icon_path(name: &str) -> Option<String> {
    use std::{
        collections::HashMap,
        path::{Path, PathBuf},
        sync::{Mutex, OnceLock},
    };
    if Path::new(name).is_absolute() {
        return Path::new(name).is_file().then(|| name.into());
    }
    if name.contains('/') || name.is_empty() {
        return None;
    }
    static CACHE: OnceLock<Mutex<HashMap<String, Option<String>>>> = OnceLock::new();
    let mut cache = CACHE.get_or_init(Default::default).lock().unwrap();
    if let Some(path) = cache.get(name) {
        return path.clone();
    }
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default();
    let data = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".local/share"));
    let mut roots = vec![
        home.join(".icons"),
        data.join("icons"),
        PathBuf::from("/usr/share/icons"),
        PathBuf::from("/usr/local/share/icons"),
    ];
    let mut found = None;
    for root in &roots {
        for theme in ["hicolor", "Adwaita", "breeze", "Papirus"] {
            for size in [
                "128x128", "96x96", "64x64", "48x48", "256x256", "512x512", "scalable",
            ] {
                for ext in ["png", "svg", "xpm"] {
                    let path = root
                        .join(theme)
                        .join(size)
                        .join("apps")
                        .join(format!("{name}.{ext}"));
                    if path.is_file() {
                        found = Some(path.to_string_lossy().into_owned());
                        break;
                    }
                }
                if found.is_some() {
                    break;
                }
            }
            if found.is_some() {
                break;
            }
        }
        if found.is_some() {
            break;
        }
    }
    if found.is_none() {
        roots = vec![data.join("pixmaps"), PathBuf::from("/usr/share/pixmaps")];
        for root in roots {
            for ext in ["png", "svg"] {
                let path = root.join(format!("{name}.{ext}"));
                if path.is_file() {
                    found = Some(path.to_string_lossy().into_owned());
                    break;
                }
            }
            if found.is_some() {
                break;
            }
        }
    }
    if cache.len() >= 256 {
        cache.clear();
    }
    cache.insert(name.into(), found.clone());
    found
}
pub fn control(target: &str, operation: &str, value: f64) -> Result<()> {
    ensure!(value.is_finite(), "audio value must be finite");
    let (volume, mute, target) = if let Some(id) = target.strip_prefix("stream:") {
        let id: u64 = id.parse()?;
        (
            "set-sink-input-volume",
            "set-sink-input-mute",
            id.to_string(),
        )
    } else if target == "microphone" {
        (
            "set-source-volume",
            "set-source-mute",
            "@DEFAULT_SOURCE@".into(),
        )
    } else {
        ensure!(
            target == "output",
            "audio target must be output, microphone or stream:ID"
        );
        ("set-sink-volume", "set-sink-mute", "@DEFAULT_SINK@".into())
    };
    let (method, argument) = match operation {
        "mute" => (mute, "1".into()),
        "unmute" => (mute, "0".into()),
        "toggle-mute" => (mute, "toggle".into()),
        "set-volume" => {
            ensure!((0.0..=100.0).contains(&value), "volume must be 0–100");
            (volume, pulse_units(value))
        }
        "adjust-volume" => {
            ensure!(
                (-100.0..=100.0).contains(&value),
                "volume step must be -100–100"
            );
            let current = if target.parse::<u64>().is_ok() {
                streams()?
                    .into_iter()
                    .find(|stream| stream.id.to_string() == target)
                    .context("audio stream disappeared")?
                    .volume
            } else {
                let query = if target == "@DEFAULT_SOURCE@" {
                    "get-source-volume"
                } else {
                    "get-sink-volume"
                };
                let output = crate::desktop::run_command(
                    &["pactl".into(), query.into(), target.clone()],
                    Duration::from_secs(2),
                )?;
                regex::Regex::new(r"(\d+)%")?
                    .captures(&output)
                    .context("cannot read audio volume")?[1]
                    .parse::<f64>()?
            };
            (volume, pulse_units((current + value).clamp(0.0, 100.0)))
        }
        _ => anyhow::bail!("unknown audio operation"),
    };
    crate::desktop::run_command(
        &["pactl".into(), method.into(), target, argument],
        Duration::from_secs(2),
    )?;
    Ok(())
}
fn pulse_units(percent: f64) -> String {
    // Older pactl treats decimal percent strings as dB. Integer PulseAudio
    // units are unambiguous on both the bundled 15.x helper and newer clients.
    ((percent * 65536.0 / 100.0).round() as u32).to_string()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parses_real_pactl_channel_values_and_preserves_app_identity() {
        let value = serde_json::json!([{"index":37,"name":"Music stream","mute":true,"properties":{"application.name":"VLC"},"volume":{"front-left":{"value":32768},"front-right":{"value":65536}}}]);
        let result = parse_streams(&value).unwrap();
        assert_eq!(result[0].id, 37);
        assert_eq!(result[0].name, "VLC");
        assert_eq!(result[0].volume, 75.0);
        assert!(result[0].muted);
    }
    #[test]
    fn volume_arguments_use_unambiguous_pulse_units() {
        assert_eq!(pulse_units(0.0), "0");
        assert_eq!(pulse_units(50.0), "32768");
        assert_eq!(pulse_units(60.0), "39322");
        assert_eq!(pulse_units(100.0), "65536");
    }
}
