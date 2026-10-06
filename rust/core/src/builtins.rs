//! Complete native action catalog for the five upstream recommended integrations.
use anyhow::{Context, Result};
use serde_json::{Value, json};
pub fn definitions() -> Vec<crate::plugin::Action> {
    serde_json::from_str(include_str!("builtins.json")).expect("checked builtin catalog")
}
pub fn resolve(action: &Value, event: &str) -> Result<Value> {
    let id = action["id"].as_str().unwrap_or("");
    let Some(alias) = id.strip_prefix("native::").filter(|a| {
        a.split_once('-').is_some_and(|(p, _)| {
            [
                "OSPlugin",
                "DeckPlugin",
                "MediaPlugin",
                "OBSPlugin",
                "VolumeMixer",
            ]
            .contains(&p)
        })
    }) else {
        return Ok(action.clone());
    };
    let (plugin, suffix) = alias.split_once('-').unwrap();
    let mut legacy = action.clone();
    legacy["id"] = json!(format!("com_core447_{plugin}::{suffix}"));
    let replacements = crate::legacy_actions::replacement(&legacy).map_err(anyhow::Error::msg)?;
    replacements
        .iter()
        .find(|a| a["event"] == event || a["event"] == "lifecycle")
        .or_else(|| replacements.first())
        .cloned()
        .context("native builtin has no implementation")
}
/// Application-owned Event Assigner rows; names match upstream's stored keys.
pub fn input_events(family: &str) -> &'static [(&'static str, &'static str)] {
    match family {
        "dials" => &[
            ("Dial Down", "press"),
            ("Dial Up", "release"),
            ("Dial Short Up", "short-release"),
            ("Dial Hold Start", "long-press"),
            ("Dial Hold Stop", "long-release"),
            ("Dial Turn CW", "turn-cw"),
            ("Dial Turn CCW", "turn-ccw"),
            ("Dial Touchscreen Short Press", "touch"),
            ("Dial Touchscreen Long Press", "long-touch"),
        ],
        "touchscreens" => &[
            ("Touchscreen Drag Left", "swipe-left"),
            ("Touchscreen Drag Right", "swipe-right"),
        ],
        _ => &[
            ("Key Down", "press"),
            ("Key Up", "release"),
            ("Key Short Up", "short-release"),
            ("Key Hold Start", "long-press"),
            ("Key Hold Stop", "long-release"),
        ],
    }
}
pub fn assigned_event_matches(action: &Value, family: &str, event: &str, was_long: bool) -> bool {
    input_events(family).iter().any(|(title, native)| {
        let actual = match *native {
            "short-release" => event == "release" && !was_long,
            "long-release" => event == "release" && was_long,
            native => native == event,
        };
        actual
            && action["event-assignments"]
                .get(title)
                .map_or_else(|| event_matches(action, native), |v| !v.is_null())
    })
}
pub fn event_matches(action: &Value, event: &str) -> bool {
    let filter = action["event"].as_str().unwrap_or("press");
    if filter == "auto" {
        let id = action["id"].as_str().unwrap_or("");
        if id == "native::VolumeMixer-Dial" {
            return ["release", "turn-cw", "turn-ccw"].contains(&event);
        }
        if id.ends_with("-MediaDial") || id.ends_with("-InputDial") || id.ends_with("-Dial") {
            return ["press", "turn-cw", "turn-ccw"].contains(&event);
        }
        if id.ends_with("-Hotkey") {
            return ["press", "release"].contains(&event);
        }
        return event == "press";
    }
    filter == event || filter == "lifecycle" && ["press", "release"].contains(&event)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn every_registered_upstream_action_has_a_native_implementation() {
        let definitions = definitions();
        assert_eq!(definitions.len(), 56);
        let mut ids = std::collections::HashSet::new();
        for a in definitions {
            assert!(ids.insert(a.id.clone()));
            let settings = a
                .fields
                .iter()
                .map(|f| (f.key.clone(), f.default.clone()))
                .collect::<serde_json::Map<_, _>>();
            let mut value =
                json!({"id":format!("native::{}",a.id),"event":"auto","settings":settings});
            // Required selections must have values to validate translation; missing setup remains an actionable error.
            for key in [
                "app_path",
                "selected_page",
                "scene",
                "item",
                "input",
                "filter",
                "scene_collection",
            ] {
                if value["settings"].get(key).is_some() {
                    value["settings"][key] = json!("Example");
                }
            }
            for key in ["text", "command", "hotkey", "url"] {
                if value["settings"].get(key).is_some() {
                    value["settings"][key] = json!("example");
                }
            }
            let resolved = resolve(&value, "press").unwrap_or_else(|e| panic!("{}: {e}", a.id));
            assert!(resolved["id"].as_str().unwrap().starts_with("native::"));
            assert_ne!(resolved["id"], value["id"]);
        }
    }
}
