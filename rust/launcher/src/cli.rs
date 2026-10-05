use anyhow::{Context, Result, ensure};
use clap::{ArgAction, Parser};
use serde_json::{Value, json};
use std::path::PathBuf;
#[derive(Parser)]
#[command(
    version,
    about = "Native Rust Stream Deck controller",
    long_about = "Deckard controls Elgato Stream Decks without Python. Native plugins use executable JSON-RPC API v1. Use --fake-deck-model plus to explore without hardware."
)]
pub struct Args {
    /// Preview supported replacements and list actions needing a native implementation.
    #[arg(long)]
    pub inspect_legacy_actions: bool,
    /// Back up pages/sticky actions and apply audited native replacements.
    #[arg(long)]
    pub migrate_legacy_actions: bool,
    #[arg(long)]
    pub data: Option<PathBuf>,
    #[arg(short = 'b', long = "background")]
    pub background: bool,
    #[arg(long)]
    pub daemon_only: bool,
    #[arg(long)]
    pub skip_load_hardware_decks: bool,
    #[arg(long,action=ArgAction::Append)]
    pub fake_deck_model: Vec<String>,
    #[arg(long)]
    pub close_running: bool,
    #[arg(long)]
    pub json: bool,
    #[arg(long)]
    pub doctor: bool,
    #[arg(long)]
    pub list_devices: bool,
    #[arg(long)]
    pub list_pages: bool,
    #[arg(long)]
    pub rpc: Option<String>,
    #[arg(long)]
    pub smoke_test: bool,
    #[arg(long, hide = true)]
    pub ui_smoke_test: bool,
    #[arg(long,num_args=2,action=ArgAction::Append)]
    pub change_page: Vec<String>,
    #[arg(long,num_args=4,action=ArgAction::Append)]
    pub change_state: Vec<String>,
    #[arg(long,num_args=4,action=ArgAction::Append)]
    pub emulate_input: Vec<String>,
    #[arg(long,num_args=3,action=ArgAction::Append)]
    pub list_actions: Vec<String>,
    #[arg(long,action=ArgAction::Append)]
    pub create_page: Vec<String>,
    #[arg(long,action=ArgAction::Append)]
    pub delete_page: Vec<String>,
    #[arg(long,num_args=2,action=ArgAction::Append)]
    pub rename_page: Vec<String>,
    #[arg(long,num_args=2,action=ArgAction::Append)]
    pub duplicate_page: Vec<String>,
    #[arg(long,num_args=2,action=ArgAction::Append)]
    pub export_page: Vec<String>,
    #[arg(long,action=ArgAction::Append)]
    pub export_all: Vec<String>,
    #[arg(long,num_args=2,action=ArgAction::Append)]
    pub add_state: Vec<String>,
    #[arg(long,num_args=3,action=ArgAction::Append)]
    pub remove_state: Vec<String>,
    #[arg(long,num_args=5,action=ArgAction::Append)]
    pub get_label: Vec<String>,
    #[arg(long,num_args=6,action=ArgAction::Append)]
    pub set_label: Vec<String>,
    #[arg(long,num_args=3,action=ArgAction::Append)]
    pub get_background_color: Vec<String>,
    #[arg(long,num_args=4,action=ArgAction::Append)]
    pub set_background_color: Vec<String>,
    #[arg(long,num_args=3,action=ArgAction::Append)]
    pub get_icon: Vec<String>,
    #[arg(long,num_args=4,action=ArgAction::Append)]
    pub set_icon: Vec<String>,
    #[arg(long,num_args=4,action=ArgAction::Append)]
    pub get_icon_layout: Vec<String>,
    #[arg(long,num_args=5,action=ArgAction::Append)]
    pub set_icon_layout: Vec<String>,
    #[arg(long,action=ArgAction::Append)]
    pub get_brightness: Vec<String>,
    #[arg(long,num_args=2,action=ArgAction::Append)]
    pub set_brightness: Vec<String>,
    #[arg(long,action=ArgAction::Append)]
    pub sleep: Vec<String>,
    #[arg(long,action=ArgAction::Append)]
    pub wake: Vec<String>,
}
impl Args {
    pub fn requests(&self) -> Result<Vec<Value>> {
        let mut requests = Vec::new();
        let mut push =
            |method: &str, params: Value| requests.push(json!({"method":method,"params":params}));
        if self.json {
            push("status", json!({}));
        }
        if self.inspect_legacy_actions {
            push("inspect-legacy-actions", json!({}));
        }
        if self.migrate_legacy_actions {
            push("migrate-legacy-actions", json!({}));
        }
        if self.close_running {
            push("quit", json!({}))
        }
        if self.list_devices {
            push("list-devices", json!({}))
        }
        if self.list_pages {
            push("list-pages", json!({}))
        }
        for p in self.change_page.as_chunks::<2>().0 {
            push("change-page", json!({"serial":p[0],"page":p[1]}))
        }
        for p in self.change_state.as_chunks::<4>().0 {
            push(
                "change-state",
                json!({"serial":p[0],"page":p[1],"input":deckard_core::model::coords(&p[2])?,"state":p[3].parse::<u32>()?}),
            )
        }
        for p in self.emulate_input.as_chunks::<4>().0 {
            ensure!(
                ["press", "release", "long-press"].contains(&p[0].as_str()),
                "event must be press, release or long-press"
            );
            push(
                "emulate-input",
                json!({"event":p[0],"serial":p[1],"page":p[2],"input":deckard_core::model::coords(&p[3])?}),
            )
        }
        for p in self.list_actions.as_chunks::<3>().0 {
            push(
                "list-actions",
                json!({"page":p[0],"input":deckard_core::model::coords(&p[1])?,"state":p[2].parse::<u32>()?}),
            )
        }
        for name in &self.create_page {
            push("create-page", json!({"page":name}))
        }
        for name in &self.delete_page {
            push("delete-page", json!({"page":name}))
        }
        for (method, items) in [
            ("rename-page", &self.rename_page),
            ("duplicate-page", &self.duplicate_page),
        ] {
            for p in items.as_chunks::<2>().0 {
                push(method, json!({"page":p[0],"name":p[1]}))
            }
        }
        for p in self.export_page.as_chunks::<2>().0 {
            push("export-page", json!({"page":p[0],"path":p[1]}))
        }
        for path in &self.export_all {
            push("export-all", json!({"path":path}))
        }
        for p in self.add_state.as_chunks::<2>().0 {
            push(
                "add-state",
                json!({"page":p[0],"input":deckard_core::model::coords(&p[1])?}),
            )
        }
        for p in self.remove_state.as_chunks::<3>().0 {
            push(
                "remove-state",
                json!({"page":p[0],"input":deckard_core::model::coords(&p[1])?,"state":p[2].parse::<u32>()?}),
            )
        }
        for (get, items) in [(true, &self.get_label), (false, &self.set_label)] {
            let n = if get { 5 } else { 6 };
            for p in items.chunks_exact(n) {
                ensure!(
                    ["top", "center", "bottom"].contains(&p[3].as_str()),
                    "label position must be top, center or bottom"
                );
                push(
                    if get { "get-property" } else { "set-property" },
                    json!({"page":p[0],"input":deckard_core::model::coords(&p[1])?,"state":p[2].parse::<u32>()?,"path":["labels",p[3],p[4]],"value":if get{Value::Null}else{parse_value(&p[5])}}),
                )
            }
        }
        for (get, items) in [
            (true, &self.get_background_color),
            (false, &self.set_background_color),
        ] {
            let n = if get { 3 } else { 4 };
            for p in items.chunks_exact(n) {
                let value = if get {
                    Value::Null
                } else {
                    let color: Vec<u8> = p[3]
                        .split(',')
                        .map(str::parse)
                        .collect::<std::result::Result<_, _>>()?;
                    ensure!(
                        [3, 4].contains(&color.len()),
                        "color must be R,G,B or R,G,B,A"
                    );
                    json!(color)
                };
                push(
                    if get { "get-property" } else { "set-property" },
                    json!({"page":p[0],"input":deckard_core::model::coords(&p[1])?,"state":p[2].parse::<u32>()?,"path":["background","color"],"value":value}),
                )
            }
        }
        for (get, items) in [(true, &self.get_icon), (false, &self.set_icon)] {
            let n = if get { 3 } else { 4 };
            for p in items.chunks_exact(n) {
                push(
                    if get { "get-property" } else { "set-property" },
                    json!({"page":p[0],"input":deckard_core::model::coords(&p[1])?,"state":p[2].parse::<u32>()?,"path":["media","path"],"value":if get{Value::Null}else{json!(p[3])}}),
                )
            }
        }
        for (get, items) in [
            (true, &self.get_icon_layout),
            (false, &self.set_icon_layout),
        ] {
            let n = if get { 4 } else { 5 };
            for p in items.chunks_exact(n) {
                push(
                    if get { "get-property" } else { "set-property" },
                    json!({"page":p[0],"input":deckard_core::model::coords(&p[1])?,"state":p[2].parse::<u32>()?,"path":["media",p[3]],"value":if get{Value::Null}else{parse_value(&p[4])}}),
                )
            }
        }
        for serial in &self.get_brightness {
            push("get-brightness", json!({"serial":serial}))
        }
        for p in self.set_brightness.as_chunks::<2>().0 {
            push(
                "set-brightness",
                json!({"serial":p[0],"value":p[1].parse::<u8>()?}),
            )
        }
        for serial in &self.sleep {
            push("sleep", json!({"serial":serial}))
        }
        for serial in &self.wake {
            push("wake", json!({"serial":serial}))
        }
        if let Some(raw) = &self.rpc {
            requests.push(serde_json::from_str(raw).context("invalid RPC JSON")?)
        }
        Ok(requests)
    }
}
fn parse_value(s: &str) -> Value {
    serde_json::from_str(s).unwrap_or_else(|_| json!(s))
}
