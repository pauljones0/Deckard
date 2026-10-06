//! Native device ownership and bounded event delivery, independent of the GUI.
use crate::{
    model::{self, Documents},
    plugin,
    render::{self, Frame, RenderConfig, Renderer},
};
use anyhow::{Context, Result, bail, ensure};
use elgato_streamdeck::{StreamDeck, StreamDeckInput, info::Kind};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, SyncSender},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
#[derive(Clone)]
pub struct Device {
    pub serial: String,
    pub kind: Kind,
    pub fake: bool,
    pub connected: bool,
    pub page: String,
    pub epoch: u64,
    pub previous_page: Option<String>,
    pub brightness: u8,
    pub sleeping: bool,
    pub states: HashMap<String, usize>,
    pub pressed: HashSet<String>,
    pub last_input: Instant,
    pub frame: Option<Arc<Frame>>,
    pub rendered_frames: u64,
    pub key_size: (usize, usize),
    pub tile_updates: HashMap<u8, u64>,
    pub frame_clock: Instant,
    pub written_tiles: u64,
    pub written_bytes: u64,
    pub write_time_us: u64,
}
pub struct Engine {
    pub docs: Documents,
    pub devices: HashMap<String, Device>,
    pub errors: VecDeque<String>,
    pub plugins: Vec<plugin::Installed>,
    pub show_window: bool,
    pub quit: bool,
    pub restart: bool,
    pub locked: bool,
    pub generation: u64,
    pub plugin_revision: u64,
    pub frame_ready: Arc<crate::transport::FrameSignal>,
    pub mixer: crate::audio::Mixer,
    overlays: HashMap<(String, String, String, usize), Value>,
    pub(crate) live_overlays: HashMap<String, (crate::live::Action, Value)>,
    pub(crate) background_overlays: HashMap<String, (crate::live::Action, String, Value)>,
    pub(crate) command_holds: HashSet<String>,
    pub(crate) command_runs: HashMap<String, Instant>,
    returns: Vec<TimedReturn>,
}
pub type Shared = Arc<Mutex<Engine>>;
impl Engine {
    pub fn open(root: PathBuf) -> Result<Shared> {
        crate::store::recover_installs(&root)?;
        let docs = Documents::open(root)?;
        let plugins = native_plugins(&docs.root);
        Ok(Arc::new(Mutex::new(Self {
            docs,
            devices: HashMap::new(),
            errors: VecDeque::new(),
            plugins,
            show_window: false,
            quit: false,
            restart: false,
            locked: false,
            generation: 0,
            plugin_revision: 0,
            frame_ready: Arc::new(crate::transport::FrameSignal::default()),
            mixer: crate::audio::Mixer {
                increment: 10.0,
                ..Default::default()
            },
            overlays: HashMap::new(),
            live_overlays: HashMap::new(),
            background_overlays: HashMap::new(),
            command_holds: HashSet::new(),
            command_runs: HashMap::new(),
            returns: Vec::new(),
        })))
    }
    pub fn error(&mut self, error: impl ToString) {
        let message = redact(&error.to_string());
        if self.errors.back() != Some(&message) {
            self.errors.push_back(message);
            while self.errors.len() > 32 {
                self.errors.pop_front();
            }
        }
    }
    pub fn saver(&self, serial: &str) -> Value {
        self.saver_ref(serial).clone()
    }
    fn saver_ref(&self, serial: &str) -> &Value {
        let Some(device) = self.devices.get(serial) else {
            return &Value::Null;
        };
        let page = self.docs.pages.get(&device.page).unwrap_or(&Value::Null);
        let deck = &self.docs.settings["devices"][serial];
        if page["settings"]["screensaver"]["overwrite"]
            .as_bool()
            .unwrap_or(false)
        {
            &page["settings"]["screensaver"]
        } else {
            &deck["screensaver"]
        }
    }
    pub fn saver_active(&self, serial: &str) -> bool {
        let Some(device) = self.devices.get(serial) else {
            return false;
        };
        let saver = self.saver_ref(serial);
        device.sleeping
            || (saver["enable"].as_bool().unwrap_or(false)
                && device.last_input.elapsed().as_secs()
                    >= saver["time-delay"].as_u64().unwrap_or(5).saturating_mul(60))
    }
    pub fn render_revision(&self, serial: &str) -> u64 {
        use std::hash::{Hash, Hasher};
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        self.docs.revision.hash(&mut hasher);
        self.generation.hash(&mut hasher);
        self.saver_active(serial).hash(&mut hasher);
        hasher.finish()
    }
    pub fn effective_brightness(&self, serial: &str) -> u8 {
        if self.locked {
            return 0;
        }
        let Some(device) = self.devices.get(serial) else {
            return 75;
        };
        if self.saver_active(serial) {
            return self.saver(serial)["brightness"]
                .as_u64()
                .unwrap_or(30)
                .min(100) as u8;
        }
        let brightness =
            &self.docs.pages.get(&device.page).unwrap_or(&Value::Null)["settings"]["brightness"];
        if brightness["overwrite"].as_bool().unwrap_or(false) {
            brightness["value"].as_u64().unwrap_or(75).min(100) as u8
        } else {
            device.brightness
        }
    }
    pub(crate) fn active_input<'a>(
        &'a self,
        serial: &str,
        family: &str,
        input: &str,
    ) -> Option<(usize, &'a Value)> {
        let d = self.devices.get(serial)?;
        let page = self.docs.pages.get(&d.page)?;
        let data = render::effective_input_from(page, self.docs.sticky_ref(serial), family, input);
        let wanted = d
            .states
            .get(&format!("{family}/{input}"))
            .copied()
            .or_else(|| data["active-state"].as_u64().map(|v| v as usize))
            .unwrap_or(0);
        let number = if data["states"].get(wanted.to_string()).is_some() {
            wanted
        } else {
            0
        };
        Some((number, data))
    }
    pub fn config(&self, serial: &str) -> Option<RenderConfig> {
        let device = self.devices.get(serial)?;
        let mut page = self.docs.pages.get(&device.page)?.clone();
        let deck = &self.docs.settings["devices"][serial];
        if page["background"].is_null()
            || !page["background"]["overwrite"].as_bool().unwrap_or(true)
        {
            page["background"] = deck["background"].clone();
        }
        let active = self.saver_active(serial);
        if active {
            let saver = self.saver(serial);
            page = json!({"background":{"media-path":saver["media-path"],"extend-to-touchscreen":true}});
        }
        page["name"] = json!(device.page);
        page["native-options"] = json!({"shrink-on-press": self.docs.settings["shrink_on_press"].as_bool().unwrap_or(true)});
        let mut sticky = if active {
            json!({})
        } else {
            self.docs.sticky(serial).unwrap_or_else(|_| json!({}))
        };
        if !active {
            for ((owner, family, input, number), response) in &self.overlays {
                if owner != serial {
                    continue;
                }
                let is_sticky = std::ptr::eq(
                    render::effective_input_from(&page, &sticky, family, input),
                    &sticky[family][input],
                );
                if let Ok(state) = model::state_mut(
                    if is_sticky { &mut sticky } else { &mut page },
                    family,
                    input,
                    *number,
                ) {
                    if response["label"].is_string() {
                        state["labels"]["bottom"]["text"] = response["label"].clone();
                    }
                    if response["color"].is_array() {
                        state["background"]["color"] = response["color"].clone();
                    }
                }
            }
        }
        if !active && (!self.background_overlays.is_empty() || !self.live_overlays.is_empty()) {
            let sticky_owner = |family: &str, input: &str| {
                std::ptr::eq(
                    render::effective_input_from(
                        self.docs.pages.get(&device.page).unwrap_or(&Value::Null),
                        self.docs.sticky_ref(serial),
                        family,
                        input,
                    ),
                    &self.docs.sticky_ref(serial)[family][input],
                )
            };
            for (source, target, value) in self.background_overlays.values() {
                if source.serial != serial || source.epoch != device.epoch {
                    continue;
                }
                let sticky_owner = sticky_owner("keys", target);
                if let Ok(state) = model::state_mut(
                    if sticky_owner { &mut sticky } else { &mut page },
                    "keys",
                    target,
                    *device.states.get(&format!("keys/{target}")).unwrap_or(&0),
                ) {
                    state["native-background"] = value["visual"].clone();
                }
            }
            let mut live = self.live_overlays.values().collect::<Vec<_>>();
            live.sort_by_key(|(a, _)| a.index);
            for (a, value) in live {
                if a.serial != serial || a.epoch != device.epoch || a.page != device.page {
                    continue;
                }
                let sticky_owner = sticky_owner(&a.family, &a.input);
                if let Ok(state) = model::state_mut(
                    if sticky_owner { &mut sticky } else { &mut page },
                    &a.family,
                    &a.input,
                    a.state,
                ) {
                    crate::live::apply(state, a.index, value);
                }
            }
        }
        Some(RenderConfig {
            page,
            sticky,
            states: device.states.clone(),
            pressed: device.pressed.clone(),
            rotation: deck["rotation"].as_u64().unwrap_or(0) as u16,
            sleeping: self.locked,
            revision: self.render_revision(serial),
            key_size: Some(device.key_size),
            max_fps: deck["max_fps"]
                .as_u64()
                .filter(|fps| *fps > 0)
                .unwrap_or(u64::from(crate::animation::MAX_FPS))
                .clamp(1, u64::from(crate::animation::MAX_FPS)) as u32,
        })
    }
    pub fn obs_profile(&self, name: &str) -> Value {
        let profiles = &self.docs.settings["obs"]["connections"];
        if profiles[name].is_object() {
            profiles[name].clone()
        } else if name == "default" {
            profiles[self.docs.settings["obs"]["default_connection"]
                .as_str()
                .unwrap_or("default")]
            .clone()
        } else {
            Value::Null
        }
    }
    pub fn status(&self) -> Value {
        let mut devices:Vec<_>=self.devices.values().map(|d|json!({"serial":d.serial,"model":format!("{:?}",d.kind),"fake":d.fake,"connected":d.connected,"page":d.page,"brightness":d.brightness,"sleeping":d.sleeping,"rendered_frames":d.rendered_frames,"tile_updates":d.tile_updates,"frame_clock_us":d.frame_clock.elapsed().as_micros() as u64,"native_key_size":d.key_size,"native_strip_size":d.kind.lcd_strip_size(),"max_fps":self.docs.settings["devices"][&d.serial]["max_fps"].as_u64().unwrap_or(0),"written_tiles":d.written_tiles,"written_bytes":d.written_bytes,"write_time_us":d.write_time_us,"frame_tiles":d.frame.as_ref().map(|f|f.tiles.iter().map(|t|json!({"key":t.key,"width":t.width,"height":t.height,"identity":t.identity})).collect::<Vec<_>>())})).collect();
        devices.sort_by_key(|d| d["serial"].as_str().unwrap_or("").to_owned());
        json!({"version":env!("CARGO_PKG_VERSION"),"runtime":"Rust","plugin_api":1,"devices":devices,"pages":self.docs.pages.keys().collect::<Vec<_>>(),"errors":self.errors,"locked":self.locked})
    }
    fn retarget_page(&mut self, old: &str, new: &str) -> Result<()> {
        for device in self.devices.values_mut() {
            if device.page == old {
                device.page = new.into();
                device.pressed.clear();
                device.epoch += 1;
                device.states.clear();
            }
        }
        if let Some(devices) = self.docs.settings["devices"].as_object_mut() {
            for device in devices.values_mut() {
                if device["page"].as_str() == Some(old) {
                    device["page"] = json!(new);
                }
            }
        }
        if let Some(rules) = self.docs.settings["rules"].as_array_mut() {
            for rule in rules {
                if rule["page"].as_str() == Some(old) {
                    rule["page"] = json!(new);
                }
            }
        }
        self.overlays.clear();
        self.live_overlays.clear();
        self.background_overlays.clear();
        self.docs.save_settings()
    }
    pub fn command(&mut self, request: &Value) -> Result<Value> {
        let method = request["method"]
            .as_str()
            .context("command method missing")?;
        let p = &request["params"];
        let name = p["page"].as_str().unwrap_or("Main");
        let serial = p["serial"].as_str().unwrap_or("");
        match method {
            "inspect-legacy-actions" | "migrate-legacy-actions" => {
                let apply = method == "migrate-legacy-actions";
                if apply {
                    for (plugin, key) in [
                        ("OBSPlugin", "obs"),
                        ("MediaPlugin", "media"),
                        ("OSPlugin", "os"),
                        ("DeckPlugin", "deck"),
                        ("VolumeMixer", "mixer"),
                    ] {
                        let path = self.docs.root.join(format!(
                            "settings/plugins/com_core447_{plugin}/settings.json"
                        ));
                        if path.exists() {
                            let old = model::load_json(&path, json!({}))?;
                            if let Some(fields) = old.as_object() {
                                for (name, value) in fields {
                                    if name != "connections"
                                        && self.docs.settings[key][name].is_null()
                                    {
                                        self.docs.settings[key][name] = value.clone();
                                    }
                                }
                            }
                        }
                    }
                    let connections = crate::legacy_actions::obs_connections(&self.docs.root)?;
                    if let Some(profiles) = connections.as_object() {
                        for (name, profile) in profiles {
                            if self.docs.settings["obs"]["connections"][name].is_null() {
                                self.docs.settings["obs"]["connections"][name] = profile.clone();
                            }
                        }
                        if !profiles.is_empty() {
                            self.docs.save_settings()?;
                        }
                    }
                }
                let mut reports = Vec::new();
                for folder in ["pages", "sticky"] {
                    if let Ok(entries) = std::fs::read_dir(self.docs.root.join(folder)) {
                        for entry in entries {
                            let path = entry?.path();
                            if path.extension().is_some_and(|e| e == "json")
                                && !path.to_string_lossy().contains(".corrupt-")
                            {
                                let mut report = crate::legacy_actions::migrate_file(
                                    &path,
                                    &self.docs.root,
                                    apply,
                                )?;
                                if let Some(remaining) = report["remaining"].as_array_mut() {
                                    remaining.retain(|item| {
                                        !self.plugins.iter().any(|plugin| {
                                            plugin.manifest.actions.iter().any(|action| {
                                                item["id"].as_str()
                                                    == Some(
                                                        format!(
                                                            "{}::{}",
                                                            plugin.manifest.id, action.id
                                                        )
                                                        .as_str(),
                                                    )
                                            })
                                        })
                                    });
                                }
                                reports.push(report);
                            }
                        }
                    }
                }
                if apply {
                    self.docs = Documents::open(self.docs.root.clone())?;
                    self.overlays.clear();
                    self.live_overlays.clear();
                    self.background_overlays.clear();
                    self.generation += 1;
                }
                return Ok(json!({"applied":apply,"documents":reports}));
            }
            "status" | "list-devices" => return Ok(self.status()),
            "list-pages" => return Ok(json!(self.docs.pages.keys().collect::<Vec<_>>())),
            "show" => self.show_window = true,
            "quit" => self.quit = true,
            "restart" => {
                self.restart = true;
                self.quit = true;
                return Ok(json!({"restart":true}));
            }
            "get-page" => return Ok(self.docs.pages.get(name).context("page not found")?.clone()),
            "create-page" => self.docs.create(name)?,
            "put-page" => self.docs.put(name, p["document"].clone())?,
            "delete-page" => {
                self.docs.delete(name)?;
                let replacement = self.docs.pages.keys().next().unwrap().clone();
                self.retarget_page(name, &replacement)?;
            }
            "rename-page" => {
                let new = p["name"].as_str().context("new name missing")?;
                self.docs.rename(name, new)?;
                self.retarget_page(name, new)?;
            }
            "duplicate-page" => self
                .docs
                .duplicate(name, p["name"].as_str().context("new name missing")?)?,
            "export-page" => crate::store::export_page_with_plugins(
                name,
                self.docs.pages.get(name).context("page not found")?,
                std::path::Path::new(p["path"].as_str().context("output path missing")?),
                &self.plugins,
            )?,
            "export-all" | "export-all-pages" => {
                let directory =
                    std::path::Path::new(p["path"].as_str().context("output directory missing")?);
                std::fs::create_dir_all(directory)?;
                for (name, page) in &self.docs.pages {
                    crate::store::export_page_with_plugins(
                        name,
                        page,
                        &directory.join(format!("{name}.deckard.zip")),
                        &self.plugins,
                    )?;
                }
            }
            "change-page" => {
                ensure!(self.docs.pages.contains_key(name), "page not found");
                let device = self.devices.get_mut(serial).context("device not found")?;
                if device.page != name {
                    device.previous_page = Some(device.page.clone());
                }
                device.page = name.into();
                device.pressed.clear();
                device.epoch += 1;
                device.states.clear();
                self.overlays.retain(|(owner, ..), _| owner != serial);
                device.last_input = Instant::now();
                self.docs.settings["devices"][serial]["page"] = json!(name);
                self.docs.save_settings()?;
            }
            "rename-device" => {
                let name = p["name"].as_str().context("name missing")?;
                model::valid_name(name)?;
                self.docs.settings["devices"][serial]["name"] = json!(name);
                self.docs.save_settings()?;
            }
            "set-brightness" => {
                let value = p["value"].as_f64().context("brightness missing")?;
                ensure!(
                    value.is_finite() && value >= 0.0,
                    "brightness must be finite and nonnegative"
                );
                ensure!(value <= 100.0, "brightness must be 0..100");
                self.devices
                    .get_mut(serial)
                    .context("device not found")?
                    .brightness = value as u8;
                self.docs.settings["devices"][serial]["brightness"] = json!(value as u8);
                self.docs.save_settings()?;
            }
            "get-brightness" => {
                return Ok(json!(
                    self.devices
                        .get(serial)
                        .context("device not found")?
                        .brightness
                ));
            }
            "sleep" | "wake" => {
                self.devices
                    .get_mut(serial)
                    .context("device not found")?
                    .sleeping = method == "sleep"
            }
            "change-state" => {
                let family = p["family"].as_str().unwrap_or("keys");
                let input = p["input"].as_str().context("input missing")?;
                let number = p["state"]
                    .as_u64()
                    .or_else(|| {
                        p["state"]
                            .as_f64()
                            .filter(|n| n.is_finite() && *n >= 0.0 && n.fract() == 0.0)
                            .map(|n| n as u64)
                    })
                    .context("state missing or invalid")? as usize;
                let device = self.devices.get(serial).context("device not found")?;
                let page = device.page.clone();
                ensure!(
                    p["page"].as_str().is_none_or(|wanted| wanted == page),
                    "requested page is not active on this device"
                );
                let config = self.config(serial).context("device config")?;
                ensure!(
                    render::effective_input(&config, family, input)["states"]
                        .get(number.to_string())
                        .is_some(),
                    "state not found"
                );
                self.devices
                    .get_mut(serial)
                    .unwrap()
                    .states
                    .insert(format!("{family}/{input}"), number);
                if self.docs.settings["persist_states"]
                    .as_bool()
                    .unwrap_or(true)
                {
                    let sticky = render::effective_input(&config, family, input)
                        == &config.sticky[family][input];
                    if sticky {
                        let mut doc = config.sticky;
                        doc[family][input]["active-state"] = json!(number);
                        self.docs.put_sticky(serial, &doc)?
                    } else {
                        self.docs.edit(&page, |p| {
                            p[family][input]["active-state"] = json!(number);
                            Ok(())
                        })?;
                    }
                }
            }
            "set-state" => {
                let family = p["family"].as_str().unwrap_or("keys");
                let input = p["input"].as_str().context("input missing")?;
                let number = p["state"].as_u64().unwrap_or(0) as usize;
                self.docs.edit(name, |page| {
                    *model::state_mut(page, family, input, number)? = p["document"].clone();
                    Ok(())
                })?;
            }
            "get-property" | "set-property" => {
                let family = p["family"].as_str().unwrap_or("keys");
                let input = p["input"].as_str().context("input missing")?;
                let number = p["state"].as_u64().unwrap_or(0) as usize;
                let path = p["path"].as_array().context("property path missing")?;
                let path: Vec<&str> = path
                    .iter()
                    .map(|v| v.as_str().context("property path must contain strings"))
                    .collect::<Result<_>>()?;
                ensure!(
                    !path.is_empty()
                        && path.len() <= 4
                        && ["labels", "media", "background"].contains(&path[0]),
                    "unsupported property path"
                );
                if method == "get-property" {
                    let mut value = model::state(
                        self.docs.pages.get(name).context("page not found")?,
                        family,
                        input,
                        number,
                    );
                    for key in &path {
                        value = &value[*key];
                    }
                    return Ok(value.clone());
                }
                self.docs.edit(name, |page| {
                    let mut value = model::state_mut(page, family, input, number)?;
                    for key in &path {
                        if value.is_null() {
                            *value = json!({});
                        }
                        let object = value
                            .as_object_mut()
                            .context("property parent is not an object")?;
                        value = object.entry(*key).or_insert(Value::Null);
                    }
                    *value = p["value"].clone();
                    Ok(())
                })?;
            }
            "list-actions" => {
                return Ok(model::state(
                    self.docs.pages.get(name).context("page not found")?,
                    p["family"].as_str().unwrap_or("keys"),
                    p["input"].as_str().context("input missing")?,
                    p["state"].as_u64().unwrap_or(0) as usize,
                )["actions"]
                    .clone());
            }
            "add-state" => {
                let family = p["family"].as_str().unwrap_or("keys");
                let input = p["input"].as_str().context("input missing")?;
                self.docs.edit(name, |page| {
                    let index = page[family][input]["states"]
                        .as_object()
                        .map(|s| {
                            s.keys()
                                .filter_map(|s| s.parse::<usize>().ok())
                                .max()
                                .unwrap_or(0)
                                + 1
                        })
                        .unwrap_or(0);
                    model::state_mut(page, family, input, index)?;
                    Ok(())
                })?
            }
            "remove-state" => {
                let family = p["family"].as_str().unwrap_or("keys");
                let input = p["input"].as_str().context("input missing")?;
                let number = p["state"].as_u64().context("state missing")?;
                self.docs.edit(name, |page| {
                    let states = page[family][input]["states"]
                        .as_object_mut()
                        .context("states not found")?;
                    ensure!(states.len() > 1, "keep at least one state");
                    states
                        .remove(&number.to_string())
                        .context("state not found")?;
                    Ok(())
                })?
            }
            "put-sticky" => self.docs.put_sticky(serial, &p["document"])?,
            "get-sticky" => return self.docs.sticky(serial),
            "settings" => return Ok(self.docs.settings.clone()),
            "put-settings" => {
                model::validate_settings(p)?;
                model::save_json(&self.docs.root.join("settings/native.json"), p)?;
                self.docs.settings = p.clone();
                self.docs.revision += 1;
                for (serial, device) in &mut self.devices {
                    if let Some(brightness) = p["devices"][serial]["brightness"].as_u64() {
                        device.brightness = brightness.min(100) as u8;
                    }
                    if let Some(page) = p["devices"][serial]["page"]
                        .as_str()
                        .filter(|page| self.docs.pages.contains_key(*page))
                        && page != device.page
                    {
                        device.page = page.into();
                        device.states.clear();
                        device.pressed.clear();
                        device.epoch += 1;
                    }
                }
            }
            "reload" => {
                for d in self.devices.values_mut() {
                    d.epoch += 1;
                    d.pressed.clear();
                }
                self.command_holds.clear();
                self.command_runs.clear();
                self.returns.clear();
                let docs = Documents::open(self.docs.root.clone())?;
                self.docs = docs;
                self.plugins = native_plugins(&self.docs.root);
                self.plugin_revision += 1;
                self.overlays.clear();
                self.live_overlays.clear();
                self.background_overlays.clear();
                let missing: Vec<_> = self
                    .devices
                    .values()
                    .filter(|d| !self.docs.pages.contains_key(&d.page))
                    .map(|d| d.page.clone())
                    .collect();
                for page in missing {
                    let replacement = self.docs.pages.keys().next().unwrap().clone();
                    self.retarget_page(&page, &replacement)?;
                }
            }
            _ => bail!("unknown command {method}"),
        }
        self.generation = self.generation.wrapping_add(1);
        Ok(json!({"ok":true}))
    }
}
pub fn native_plugins(root: &std::path::Path) -> Vec<plugin::Installed> {
    let mut plugins = plugin::discover(&root.join("plugins-native"));
    if let Ok(exe) = std::env::current_exe()
        && let Some(prefix) = exe.parent().and_then(|p| p.parent())
    {
        plugins.extend(plugin::discover(&prefix.join("share/deckard/plugins")));
    }
    let mut ids = HashSet::new();
    plugins.retain(|p| ids.insert(p.manifest.id.clone()));
    plugins.sort_by_key(|p| p.manifest.name.to_lowercase());
    plugins
}
#[derive(Clone)]
pub struct InputEvent {
    pub serial: String,
    pub family: String,
    pub input: String,
    pub event: String,
    pub value: i32,
}
pub struct Runtime {
    pub shared: Shared,
    pub events: SyncSender<InputEvent>,
    stop: Arc<AtomicBool>,
    threads: Vec<JoinHandle<()>>,
}
impl Runtime {
    pub fn start(shared: Shared, fakes: Vec<Kind>, hardware: bool) -> Result<Self> {
        let stop = Arc::new(AtomicBool::new(false));
        let (events, receiver) = mpsc::sync_channel(256);
        let mut threads = Vec::new();
        let actions_shared = shared.clone();
        let actions_stop = stop.clone();
        threads.push(thread::spawn(move || {
            action_loop(actions_shared, receiver, actions_stop)
        }));
        let live_shared = shared.clone();
        let live_stop = stop.clone();
        threads.push(thread::spawn(move || {
            crate::live::watch(live_shared, live_stop)
        }));
        let monitor_shared = shared.clone();
        let monitor_stop = stop.clone();
        threads.push(thread::spawn(move || {
            let mut next = Instant::now();
            while !monitor_stop.load(Ordering::Relaxed) {
                process_returns(&monitor_shared);
                if Instant::now() >= next {
                    next = Instant::now() + Duration::from_secs(1);
                    let active = {
                        let engine = monitor_shared.lock().unwrap();
                        engine.devices.values().any(|d| {
                            engine
                                .docs
                                .pages
                                .get(&d.page)
                                .is_some_and(|p| p["deckard"]["native-mixer"] == true)
                        })
                    };
                    if active && let Err(error) = refresh_mixers(&monitor_shared) {
                        monitor_shared.lock().unwrap().error(error);
                    }
                }
                thread::sleep(Duration::from_millis(25));
            }
        }));
        let manager_shared = shared.clone();
        let manager_stop = stop.clone();
        let manager_events = events.clone();
        threads.push(thread::spawn(move || {
            let mut workers = HashMap::<String, JoinHandle<()>>::new();
            for (index, kind) in fakes.into_iter().enumerate() {
                let serial = format!("FAKE-{}-{index}", format!("{kind:?}").to_uppercase());
                workers.insert(
                    serial.clone(),
                    start_device(
                        manager_shared.clone(),
                        manager_events.clone(),
                        manager_stop.clone(),
                        serial,
                        kind,
                        true,
                    ),
                );
            }
            let mut api = if hardware {
                elgato_streamdeck::new_hidapi().ok()
            } else {
                None
            };
            let mut next_scan = Instant::now();
            while !manager_stop.load(Ordering::Relaxed) {
                if hardware && Instant::now() >= next_scan {
                    next_scan = Instant::now() + Duration::from_secs(2);
                    if api.is_none() {
                        api = elgato_streamdeck::new_hidapi().ok();
                    }
                    if let Some(api) = api.as_mut() {
                        let _ = elgato_streamdeck::refresh_device_list(api);
                        for (kind, serial) in elgato_streamdeck::list_devices(api) {
                            if !workers.contains_key(&serial) {
                                workers.insert(
                                    serial.clone(),
                                    start_device(
                                        manager_shared.clone(),
                                        manager_events.clone(),
                                        manager_stop.clone(),
                                        serial,
                                        kind,
                                        false,
                                    ),
                                );
                            }
                        }
                    }
                }
                thread::sleep(Duration::from_millis(40));
            }
            for (_, worker) in workers {
                let _ = worker.join();
            }
        }));
        let context_shared = shared.clone();
        let context_stop = stop.clone();
        threads.push(thread::spawn(move || {
            crate::desktop::watch(context_shared, context_stop)
        }));
        Ok(Self {
            shared,
            events,
            stop,
            threads,
        })
    }
}
impl Drop for Runtime {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        for thread in self.threads.drain(..) {
            let _ = thread.join();
        }
        crate::uinput::shutdown();
    }
}
fn start_device(
    shared: Shared,
    events: SyncSender<InputEvent>,
    stop: Arc<AtomicBool>,
    serial: String,
    kind: Kind,
    fake: bool,
) -> JoinHandle<()> {
    thread::spawn(move || {
        {
            let mut engine = shared.lock().unwrap_or_else(|p| p.into_inner());
            let settings = &engine.docs.settings["devices"][&serial];
            let page = settings["page"]
                .as_str()
                .filter(|p| engine.docs.pages.contains_key(*p))
                .unwrap_or_else(|| engine.docs.pages.keys().next().unwrap())
                .to_owned();
            let brightness = settings["brightness"].as_u64().unwrap_or(75).min(100) as u8;
            engine.devices.insert(
                serial.clone(),
                Device {
                    serial: serial.clone(),
                    kind,
                    fake,
                    connected: fake,
                    page,
                    epoch: 0,
                    previous_page: None,
                    brightness,
                    sleeping: false,
                    states: HashMap::new(),
                    pressed: HashSet::new(),
                    last_input: Instant::now(),
                    frame: None,
                    rendered_frames: 0,
                    key_size: kind.key_image_format().size,
                    tile_updates: HashMap::new(),
                    frame_clock: Instant::now(),
                    written_tiles: 0,
                    written_bytes: 0,
                    write_time_us: 0,
                },
            );
            engine.generation += 1;
        }
        let slot = Arc::new(crate::transport::FrameMailbox::default());
        let render_slot = slot.clone();
        let render_shared = shared.clone();
        let render_stop = stop.clone();
        let render_serial = serial.clone();
        let renderer_thread = thread::spawn(move || {
            let budget = render_shared.lock().unwrap().docs.settings["cache_mib"]
                .as_u64()
                .unwrap_or(64)
                .clamp(8, 256) as usize
                * 1024
                * 1024;
            let Ok(mut renderer) = Renderer::new(budget) else {
                return;
            };
            let mut previous = u64::MAX;
            let mut cached_config = None;
            while !render_stop.load(Ordering::Relaxed) {
                let (available, config) = {
                    let engine = render_shared.lock().unwrap_or_else(|p| p.into_inner());
                    let available = engine
                        .devices
                        .get(&render_serial)
                        .is_some_and(|d| d.fake || d.connected);
                    let revision = engine.render_revision(&render_serial);
                    if available && (cached_config.is_none() || revision != previous) {
                        cached_config = engine.config(&render_serial).map(Arc::new);
                    }
                    let due = renderer
                        .next_deadline()
                        .is_some_and(|deadline| Instant::now() >= deadline);
                    let config = if available
                        && (revision != previous || (due && render_slot.available()))
                    {
                        cached_config.clone()
                    } else {
                        None
                    };
                    (available, config)
                };
                if !available {
                    if cached_config.take().is_some() {
                        renderer.release_media();
                    }
                    previous = u64::MAX;
                }
                if let Some(config) = config {
                    previous = config.revision;
                    match renderer.render_shared(kind, config) {
                        Ok(frame) => {
                            let frame = Arc::new(frame);
                            let mut engine =
                                render_shared.lock().unwrap_or_else(|p| p.into_inner());
                            let mut pixels_changed = false;
                            let mut revision_changed = false;
                            if let Some(device) = engine.devices.get_mut(&render_serial) {
                                revision_changed = device
                                    .frame
                                    .as_ref()
                                    .is_none_or(|old| old.revision != frame.revision);
                                for tile in frame.tiles.iter().chain(frame.strip.iter()) {
                                    let old = device.frame.as_ref().and_then(|old| {
                                        if tile.key == 255 {
                                            old.strip.as_ref()
                                        } else {
                                            old.tiles.get(tile.key as usize)
                                        }
                                    });
                                    if old.is_none_or(|old| old.identity != tile.identity) {
                                        *device.tile_updates.entry(tile.key).or_default() += 1;
                                        pixels_changed = true;
                                    }
                                }
                                device.frame = Some(frame.clone());
                                device.rendered_frames += 1;
                            }
                            if pixels_changed || revision_changed {
                                render_slot.publish(frame, !fake);
                            }
                            if pixels_changed {
                                engine.frame_ready.notify();
                            }
                        }
                        Err(error) => render_shared
                            .lock()
                            .unwrap_or_else(|p| p.into_inner())
                            .error(format!("Renderer {render_serial}: {error}")),
                    }
                }
                for error in renderer.errors.drain(..) {
                    render_shared.lock().unwrap().error(error);
                }
                render_slot.wait(renderer.next_deadline());
            }
        });
        let mut deck: Option<StreamDeck> = None;
        let mut retry = Instant::now();
        let mut hashes = HashMap::new();
        let mut old_buttons = vec![false; (kind.key_count() + kind.touchpoint_count()) as usize];
        let mut old_dials = vec![false; kind.encoder_count() as usize];
        let mut sent_brightness = None;
        let mut touch_colors = HashMap::new();
        let mut led_revision = None;
        let mut pending: Option<Arc<Frame>> = None;
        let mut cursor = crate::transport::TileCursor::default();
        while !stop.load(Ordering::Relaxed) {
            if !fake && deck.is_none() && Instant::now() >= retry {
                retry = Instant::now() + Duration::from_secs(2);
                match (|| -> anyhow::Result<StreamDeck> {
                    let api = elgato_streamdeck::new_hidapi()?;
                    Ok(StreamDeck::connect(&api, kind, &serial)?)
                })() {
                    Ok(device) => {
                        let key_size = device
                            .native_key_size()
                            .ok()
                            .flatten()
                            .unwrap_or_else(|| kind.key_image_format().size);
                        deck = Some(device);
                        hashes.clear();
                        touch_colors.clear();
                        led_revision = None;
                        sent_brightness = None;
                        old_buttons.fill(false);
                        old_dials.fill(false);
                        if let Some(device) = shared.lock().unwrap().devices.get_mut(&serial) {
                            device.connected = true;
                            device.key_size = key_size;
                        }
                    }
                    Err(error) => shared
                        .lock()
                        .unwrap()
                        .error(format!("Device {serial}: {error}; check USB permissions")),
                }
            }
            let (rotation, brightness, revision) = {
                let engine = shared.lock().unwrap();
                (
                    engine.docs.settings["devices"][&serial]["rotation"]
                        .as_u64()
                        .unwrap_or(0) as u16,
                    engine.effective_brightness(&serial),
                    engine.render_revision(&serial),
                )
            };
            let mut failed = false;
            if let Some(deck) = deck.as_ref() {
                if kind.is_visual() && sent_brightness != Some(brightness) {
                    if deck.set_brightness(brightness).is_err() {
                        failed = true
                    } else {
                        sent_brightness = Some(brightness)
                    }
                }
                if matches!(kind, Kind::Studio | Kind::Neo) && led_revision != Some(revision) {
                    // Clone only when the configuration changes; never hold the engine lock over USB writes.
                    let config = shared.lock().unwrap().config(&serial);
                    if let Some(config) = config {
                        let count = if kind == Kind::Studio {
                            kind.encoder_count()
                        } else {
                            kind.touchpoint_count()
                        };
                        for point in 0..count {
                            let (family, input) = if kind == Kind::Studio {
                                ("dials", point.to_string())
                            } else {
                                ("keys", format!("touch-{point}"))
                            };
                            let state = render::active_state(&config, family, &input);
                            let color = if config.sleeping {
                                [0, 0, 0, 255]
                            } else {
                                render::color(
                                    &render::effective_input(&config, family, &input)["states"]
                                        [state.to_string()]["background"]["color"],
                                    [255, 255, 255, 255],
                                )
                            };
                            if touch_colors.get(&point) != Some(&color) {
                                let result = if kind == Kind::Studio {
                                    deck.set_encoder_color(point, color[0], color[1], color[2])
                                } else {
                                    deck.set_touchpoint_color(point, color[0], color[1], color[2])
                                };
                                if result.is_err() {
                                    failed = true;
                                } else {
                                    touch_colors.insert(point, color);
                                }
                            }
                        }
                        if !failed {
                            led_revision = Some(revision);
                        }
                    }
                }
                match deck.read_input(Some(Duration::ZERO)) {
                    Ok(input) => route_input(
                        input,
                        kind,
                        rotation,
                        &serial,
                        &events,
                        &shared,
                        (&mut old_buttons, &mut old_dials),
                    ),
                    Err(_) => failed = true,
                }
                if let Some(frame) = slot.take(pending.as_ref().map(|frame| frame.revision)) {
                    cursor.replace(frame.tiles.len() + usize::from(frame.strip.is_some()));
                    pending = Some(frame);
                    // Retain the cursor when coalescing: large frame writes must not starve later keys.
                }
                if pending
                    .as_ref()
                    .is_some_and(|frame| frame.revision != revision)
                {
                    pending = None;
                    slot.finish();
                }
                if kind == Kind::UlanziD200
                    && let Some(frame) = pending.take()
                {
                    let changed: Vec<_> = frame
                        .tiles
                        .iter()
                        .filter(|tile| {
                            tile.key != 14 && hashes.get(&tile.key) != Some(&tile.identity)
                        })
                        .collect();
                    let images: Vec<_> = changed
                        .iter()
                        .map(|tile| (tile.key, tile.encoded.as_ref()))
                        .collect();
                    if !images.is_empty() {
                        let write_start = Instant::now();
                        if deck.write_images(&images).is_err() {
                            failed = true;
                        } else {
                            let elapsed = write_start.elapsed().as_micros() as u64;
                            if let Some(device) = shared.lock().unwrap().devices.get_mut(&serial) {
                                device.written_tiles += changed.len() as u64;
                                device.written_bytes += changed
                                    .iter()
                                    .map(|tile| tile.encoded.len() as u64)
                                    .sum::<u64>();
                                device.write_time_us += elapsed;
                            }
                            for tile in changed {
                                hashes.insert(tile.key, tile.identity);
                            }
                        }
                    }
                    slot.finish();
                }
                if let Some(frame) = pending.as_ref() {
                    let tile = frame.tiles.get(cursor.index()).or_else(|| {
                        if cursor.index() == frame.tiles.len() {
                            frame.strip.as_ref()
                        } else {
                            None
                        }
                    });
                    if let Some(tile) = tile {
                        if kind.is_visual() && hashes.get(&tile.key) != Some(&tile.identity) {
                            let write_start = Instant::now();
                            let result = if tile.key == 255 {
                                deck.write_lcd_fill(&tile.encoded)
                            } else if kind.is_visual() {
                                deck.write_image_immediate(tile.key, &tile.encoded)
                            } else {
                                Ok(())
                            };
                            if result.is_err() {
                                failed = true
                            } else {
                                hashes.insert(tile.key, tile.identity);
                                let write_time_us = write_start.elapsed().as_micros() as u64;
                                if let Some(device) =
                                    shared.lock().unwrap().devices.get_mut(&serial)
                                {
                                    device.written_tiles += 1;
                                    device.written_bytes += tile.encoded.len() as u64;
                                    device.write_time_us += write_time_us;
                                }
                            }
                        }
                        if cursor.advance() {
                            pending = None;
                            slot.finish();
                        }
                    } else {
                        pending = None;
                        slot.finish();
                    }
                }
            }
            if failed {
                deck = None;
                pending = None;
                hashes.clear();
                slot.clear();
                touch_colors.clear();
                led_revision = None;
                sent_brightness = None;
                retry = Instant::now() + Duration::from_millis(500);
                let mut engine = shared.lock().unwrap();
                if let Some(device) = engine.devices.get_mut(&serial) {
                    device.connected = false;
                    device.pressed.clear();
                    device.epoch += 1;
                }
                engine.generation += 1;
            }
            if fake || deck.is_none() {
                slot.clear();
                thread::sleep(Duration::from_millis(20));
            } else if pending.is_none() {
                slot.wait_output();
            }
        }
        if let Some(deck) = deck {
            let _ = deck.set_brightness(0);
        }
        let _ = renderer_thread.join();
    })
}
fn route_input(
    input: StreamDeckInput,
    kind: Kind,
    rotation: u16,
    serial: &str,
    events: &SyncSender<InputEvent>,
    shared: &Shared,
    previous: (&mut [bool], &mut [bool]),
) {
    let (buttons, dials) = previous;
    let dial = |i: usize| {
        if rotation % 360 == 180 {
            usize::from(kind.encoder_count()) - 1 - i
        } else {
            i
        }
    };
    let send = |family: &str, input: String, event: &str, value: i32| {
        if family == "keys" && ["press", "release"].contains(&event) {
            let mut engine = shared.lock().unwrap();
            if let Some(device) = engine.devices.get_mut(serial) {
                if event == "press" {
                    device.pressed.insert(input.clone());
                } else {
                    device.pressed.remove(&input);
                }
            }
            engine.generation += 1;
        }
        if events
            .try_send(InputEvent {
                serial: serial.into(),
                family: family.into(),
                input,
                event: event.into(),
                value,
            })
            .is_err()
        {
            shared
                .lock()
                .unwrap()
                .error("Input action queue full; an action was skipped");
        }
    };
    match input {
        StreamDeckInput::ButtonStateChange(states) => {
            for (i, pressed) in states.into_iter().enumerate().take(buttons.len()) {
                if buttons[i] != pressed {
                    buttons[i] = pressed;
                    let input = if i < kind.key_count() as usize {
                        render::logical_input(kind, i as u8, rotation)
                    } else {
                        format!("touch-{}", i - kind.key_count() as usize)
                    };
                    send(
                        "keys",
                        input,
                        if pressed { "press" } else { "release" },
                        i32::from(pressed),
                    );
                }
            }
        }
        StreamDeckInput::EncoderStateChange(states) => {
            for (i, pressed) in states.into_iter().enumerate().take(dials.len()) {
                if dials[i] != pressed {
                    dials[i] = pressed;
                    send(
                        "dials",
                        dial(i).to_string(),
                        if pressed { "press" } else { "release" },
                        i32::from(pressed),
                    );
                }
            }
        }
        StreamDeckInput::EncoderTwist(values) => {
            for (i, value) in values.into_iter().enumerate() {
                if value != 0 {
                    send(
                        "dials",
                        dial(i).to_string(),
                        if value > 0 { "turn-cw" } else { "turn-ccw" },
                        value.into(),
                    )
                }
            }
        }
        StreamDeckInput::TouchScreenPress(x, y) | StreamDeckInput::TouchScreenLongPress(x, y) => {
            let long = matches!(input, StreamDeckInput::TouchScreenLongPress(..));
            let (sw, sh) = kind.lcd_strip_size().unwrap_or((800, 100));
            if usize::from(x) >= sw || usize::from(y) >= sh {
                return;
            }
            let index = if sh > sw {
                usize::from(y) * usize::from(kind.encoder_count()) / sh
            } else {
                usize::from(x) * usize::from(kind.encoder_count()) / sw
            };
            send(
                "dials",
                dial(index).to_string(),
                if long { "long-touch" } else { "touch" },
                1,
            )
        }
        StreamDeckInput::TouchScreenSwipe((x, _y), (xx, _yy)) => send(
            "touchscreens",
            "0".into(),
            if xx < x { "swipe-left" } else { "swipe-right" },
            1,
        ),
        _ => {}
    }
}
#[derive(Clone)]
enum ReturnTarget {
    Page(String),
    State(String, String, usize),
}
struct TimedReturn {
    deadline: Instant,
    serial: String,
    epoch: u64,
    target: ReturnTarget,
}
fn schedule_return(
    engine: &mut Engine,
    serial: &str,
    settings: &Value,
    target: ReturnTarget,
) -> Result<()> {
    let timeout = settings["return_timeout"].as_f64().unwrap_or(0.0);
    ensure!(
        timeout.is_finite() && (0.0..=86400.0 * 365.0).contains(&timeout),
        "invalid return timeout"
    );
    if timeout > 0.0 {
        let device = engine
            .devices
            .get(serial)
            .context("return device missing")?;
        engine.returns.retain(|old|!(old.serial==serial&&matches!((&old.target,&target),(ReturnTarget::Page(_),ReturnTarget::Page(_))) || old.serial==serial&&matches!((&old.target,&target),(ReturnTarget::State(f,i,_),ReturnTarget::State(g,j,_)) if f==g&&i==j)));
        engine.returns.push(TimedReturn {
            deadline: Instant::now() + Duration::from_secs_f64(timeout),
            serial: serial.into(),
            epoch: device.epoch,
            target,
        });
    }
    Ok(())
}
fn process_returns(shared: &Shared) {
    let mut e = shared.lock().unwrap();
    let mut pending = std::mem::take(&mut e.returns);
    for entry in pending.drain(..) {
        if e.devices
            .get(&entry.serial)
            .is_none_or(|d| d.epoch != entry.epoch || !(d.fake || d.connected))
        {
            continue;
        }
        if Instant::now() < entry.deadline {
            e.returns.push(entry);
            continue;
        }
        let params = match entry.target {
            ReturnTarget::Page(page) => {
                json!({"method":"change-page","params":{"serial":entry.serial,"page":page}})
            }
            ReturnTarget::State(family, input, state) => {
                json!({"method":"change-state","params":{"serial":entry.serial,"family":family,"input":input,"state":state}})
            }
        };
        if let Err(error) = e.command(&params) {
            e.error(error);
        }
    }
}
fn action_origin(
    shared: &Shared,
    event: &InputEvent,
    action: &Value,
) -> Result<crate::live::Action> {
    let e = shared.lock().unwrap();
    let d = e
        .devices
        .get(&event.serial)
        .context("action device missing")?;
    let c = e.config(&event.serial).context("device config")?;
    let state = render::active_state(&c, &event.family, &event.input);
    let index = render::effective_input(&c, &event.family, &event.input)["states"]
        [state.to_string()]["actions"]
        .as_array()
        .and_then(|a| a.iter().position(|a| a == action))
        .unwrap_or(0);
    Ok(crate::live::Action {
        serial: event.serial.clone(),
        page: d.page.clone(),
        epoch: d.epoch,
        family: event.family.clone(),
        input: event.input.clone(),
        state,
        index,
        raw: action.clone(),
        action: crate::builtins::resolve(action, "press")?,
    })
}

struct ActionBatch {
    event: InputEvent,
    actions: Vec<Value>,
    epoch: u64,
    pressed: Arc<AtomicBool>,
}
struct ActionWorker {
    sender: SyncSender<ActionBatch>,
    stop: Arc<AtomicBool>,
    thread: JoinHandle<()>,
    last: Instant,
    pending: Arc<std::sync::atomic::AtomicUsize>,
}
fn action_loop(shared: Shared, events: Receiver<InputEvent>, stop: Arc<AtomicBool>) {
    let processes = Arc::new(Mutex::new(HashMap::<String, plugin::Process>::new()));
    let mut workers = HashMap::<String, ActionWorker>::new();
    let mut presses = HashMap::<String, (u64, Arc<AtomicBool>)>::new();
    let mut long_inputs = HashSet::<String>::new();
    let mut plugin_revision = 0;
    let mut holds = HashMap::<(String, String, String), (Instant, InputEvent)>::new();
    let mut wake_gestures = HashSet::new();
    while !stop.load(Ordering::Relaxed) {
        presses.retain(|owner, (epoch, flag)| {
            let serial = owner.split('/').next().unwrap_or("");
            let valid = shared
                .lock()
                .unwrap()
                .devices
                .get(serial)
                .is_some_and(|d| d.epoch == *epoch && (d.fake || d.connected));
            if !valid {
                flag.store(false, Ordering::Relaxed);
                crate::uinput::release_owner(owner);
            }
            valid
        });
        let stale = workers
            .iter()
            .filter(|(_, worker)| {
                worker.last.elapsed() > Duration::from_secs(30)
                    && worker.pending.load(Ordering::Relaxed) == 0
            })
            .map(|(k, _)| k.clone())
            .collect::<Vec<_>>();
        for key in stale {
            if let Some(worker) = workers.remove(&key) {
                worker.stop.store(true, Ordering::Relaxed);
                let _ = worker.thread.join();
            }
        }
        let revision = shared.lock().unwrap().plugin_revision;
        if revision != plugin_revision {
            processes.lock().unwrap().clear();
            plugin_revision = revision;
        }
        let due = holds
            .iter()
            .find(|(_, (deadline, _))| Instant::now() >= *deadline)
            .map(|(key, (_, event))| (key.clone(), event.clone()));
        let event = if let Some((key, mut event)) = due {
            holds.remove(&key);
            let engine = shared.lock().unwrap();
            if event.family == "keys"
                && engine
                    .devices
                    .get(&event.serial)
                    .is_none_or(|d| !d.pressed.contains(&event.input))
            {
                continue;
            }
            event.event = "long-press".into();
            event
        } else {
            let Ok(event) = events.recv_timeout(Duration::from_millis(25)) else {
                continue;
            };
            event
        };
        let hold_key = (
            event.serial.clone(),
            event.family.clone(),
            event.input.clone(),
        );
        if event.event == "press" {
            holds.insert(
                hold_key.clone(),
                (
                    Instant::now()
                        + Duration::from_millis(
                            shared.lock().unwrap().docs.settings["hold_ms"]
                                .as_u64()
                                .unwrap_or(500)
                                .clamp(100, 5000),
                        ),
                    event.clone(),
                ),
            );
        }
        let owner = format!("{}/{}/{}", event.serial, event.family, event.input);
        if event.event == "long-press" {
            long_inputs.insert(owner.clone());
            shared.lock().unwrap().command_holds.insert(owner.clone());
        }
        let was_long = event.event == "release" && long_inputs.remove(&owner);
        if event.event == "release" {
            if let Some((_, flag)) = presses.remove(&owner) {
                flag.store(false, Ordering::Relaxed);
            }
            crate::uinput::release_owner(&owner);
            shared.lock().unwrap().command_holds.remove(&owner);
        }
        let suppressed = wake_gestures.contains(&hold_key);
        if event.event == "release" {
            holds.remove(&hold_key);
            wake_gestures.remove(&hold_key);
        }
        let actions = {
            let mut engine = shared.lock().unwrap();
            let waking = engine.saver_active(&event.serial);
            let Some(device) = engine.devices.get_mut(&event.serial) else {
                continue;
            };
            // A release/hold following a Sleep action must not immediately wake the deck.
            if !["release", "long-press"].contains(&event.event.as_str()) {
                device.sleeping = false;
                device.last_input = Instant::now();
            }
            if event.family == "keys" {
                if event.event == "press" {
                    device.pressed.insert(event.input.clone());
                } else if event.event == "release" {
                    device.pressed.remove(&event.input);
                }
            }
            engine.generation += 1;
            if waking || engine.locked || suppressed {
                // Consume the entire wake gesture, including release and held-input actions.
                if event.event == "press" {
                    wake_gestures.insert(hold_key.clone());
                    holds.remove(&hold_key);
                }
                continue;
            }
            let Some(config) = engine.config(&event.serial) else {
                continue;
            };
            let state = render::active_state(&config, &event.family, &event.input);
            render::effective_input(&config, &event.family, &event.input)["states"]
                [state.to_string()]["actions"]
                .as_array()
                .cloned()
                .unwrap_or_default()
        };
        let epoch = shared
            .lock()
            .unwrap()
            .devices
            .get(&event.serial)
            .map(|d| d.epoch)
            .unwrap_or(0);
        let pressed = if event.event == "press" {
            let flag = Arc::new(AtomicBool::new(true));
            if let Some((_, old)) = presses.insert(owner.clone(), (epoch, flag.clone())) {
                old.store(false, Ordering::Relaxed);
            }
            flag
        } else {
            presses
                .get(&owner)
                .map(|(_, flag)| flag.clone())
                .unwrap_or_else(|| Arc::new(AtomicBool::new(false)))
        };
        let selected = actions
            .into_iter()
            .filter(|action| {
                let canonical =
                    crate::builtins::resolve(action, "press").unwrap_or_else(|_| action.clone());
                if canonical["id"] == "native::shell"
                    && canonical["settings"]["auto_run"].as_f64().unwrap_or(0.0) > 0.0
                {
                    return event.event == "release" && !was_long;
                }
                if canonical["id"] == "native::mixer"
                    && canonical["settings"]["operation"] == "mute"
                    && event.family == "dials"
                    && action["event"] == "release"
                {
                    return event.event == "release" && !was_long;
                }
                crate::builtins::event_matches(action, &event.event)
            })
            .collect::<Vec<_>>();
        if selected.is_empty() {
            continue;
        }
        if workers
            .get(&owner)
            .is_some_and(|worker| worker.thread.is_finished())
            && let Some(worker) = workers.remove(&owner)
        {
            let _ = worker.thread.join();
        }
        if !workers.contains_key(&owner) {
            if workers.len() >= 128 {
                shared
                    .lock()
                    .unwrap()
                    .error("128 action inputs are busy; action skipped");
                continue;
            }
            let (sender, receiver) = mpsc::sync_channel::<ActionBatch>(32);
            let worker_stop = Arc::new(AtomicBool::new(false));
            let worker_flag = worker_stop.clone();
            let app_stop = stop.clone();
            let worker_shared = shared.clone();
            let plugins = processes.clone();
            let pending = Arc::new(std::sync::atomic::AtomicUsize::new(0));
            let worker_pending = pending.clone();
            let thread = thread::spawn(move || {
                while !app_stop.load(Ordering::Relaxed) && !worker_flag.load(Ordering::Relaxed) {
                    let batch = match receiver.recv_timeout(Duration::from_millis(25)) {
                        Ok(batch) => batch,
                        Err(mpsc::RecvTimeoutError::Timeout) => continue,
                        Err(_) => break,
                    };
                    struct Completed(Arc<std::sync::atomic::AtomicUsize>);
                    impl Drop for Completed {
                        fn drop(&mut self) {
                            self.0.fetch_sub(1, Ordering::Relaxed);
                        }
                    }
                    let _completed = Completed(worker_pending.clone());
                    let expected = Arc::new(std::sync::atomic::AtomicU64::new(batch.epoch));
                    let shared_check = worker_shared.clone();
                    let serial = batch.event.serial.clone();
                    let expected_check = expected.clone();
                    let app_check = app_stop.clone();
                    let worker_check = worker_flag.clone();
                    let cancellation: Arc<dyn Fn() -> bool + Send + Sync> = Arc::new(move || {
                        app_check.load(Ordering::Relaxed)
                            || worker_check.load(Ordering::Relaxed)
                            || shared_check
                                .lock()
                                .unwrap()
                                .devices
                                .get(&serial)
                                .is_none_or(|d| {
                                    d.epoch != expected_check.load(Ordering::Relaxed)
                                        || !(d.fake || d.connected)
                                })
                    });
                    crate::desktop::with_cancellation(cancellation.clone(), || {
                        let mut unused = HashMap::new();
                        for action in batch.actions {
                            if crate::desktop::cancelled() {
                                break;
                            }
                            let input = action["id"] == "native::input"
                                || action["id"] == "native::OSPlugin-Hotkey";
                            let result = if input {
                                let press = batch.pressed.clone();
                                let check = cancellation.clone();
                                crate::desktop::with_cancellation(
                                    Arc::new(move || check() || !press.load(Ordering::Relaxed)),
                                    || {
                                        run_action(
                                            &worker_shared,
                                            &batch.event,
                                            &action,
                                            &mut unused,
                                        )
                                    },
                                )
                            } else if action["id"]
                                .as_str()
                                .is_some_and(|id| id.starts_with("native::"))
                            {
                                run_action(&worker_shared, &batch.event, &action, &mut unused)
                            } else {
                                run_action(
                                    &worker_shared,
                                    &batch.event,
                                    &action,
                                    &mut plugins.lock().unwrap(),
                                )
                            };
                            if let Err(error) = result
                                && !crate::desktop::cancelled()
                            {
                                worker_shared.lock().unwrap().error(error);
                            }
                            // A deliberate navigation action owns the continuation of its own sequence.
                            let canonical = crate::builtins::resolve(&action, &batch.event.event)
                                .unwrap_or_else(|_| action.clone());
                            if ["native::page", "native::previous-page", "native::mixer"]
                                .contains(&canonical["id"].as_str().unwrap_or(""))
                                && let Some(device) = worker_shared
                                    .lock()
                                    .unwrap()
                                    .devices
                                    .get(&batch.event.serial)
                            {
                                expected.store(device.epoch, Ordering::Relaxed);
                            }
                        }
                    });
                }
            });
            workers.insert(
                owner.clone(),
                ActionWorker {
                    sender,
                    stop: worker_stop,
                    thread,
                    last: Instant::now(),
                    pending,
                },
            );
        }
        let worker = workers.get_mut(&owner).unwrap();
        worker.last = Instant::now();
        worker.pending.fetch_add(1, Ordering::Relaxed);
        if worker
            .sender
            .try_send(ActionBatch {
                event,
                actions: selected,
                epoch,
                pressed,
            })
            .is_err()
        {
            worker.pending.fetch_sub(1, Ordering::Relaxed);
            shared
                .lock()
                .unwrap()
                .error("input action queue full; action skipped");
        }
    }
    for (owner, (_, flag)) in presses {
        flag.store(false, Ordering::Relaxed);
        crate::uinput::release_owner(&owner);
    }
    for worker in workers.values() {
        worker.stop.store(true, Ordering::Relaxed);
    }
    for (_, worker) in workers {
        let _ = worker.thread.join();
    }
}

fn refresh_mixers(shared: &Shared) -> Result<()> {
    let streams = crate::audio::streams()?;
    let mut engine = shared.lock().unwrap();
    let devices: Vec<_> = engine
        .devices
        .values()
        .filter(|d| {
            engine
                .docs
                .pages
                .get(&d.page)
                .is_some_and(|p| p["deckard"]["native-mixer"] == true)
        })
        .map(|d| (d.serial.clone(), d.kind, d.page.clone()))
        .collect();
    for (serial, kind, name) in devices {
        let page = crate::audio::mixer_page(
            kind,
            engine.docs.settings["devices"][&serial]["rotation"]
                .as_u64()
                .unwrap_or(0) as u16,
            &streams,
            *engine.mixer.offsets.get(&serial).unwrap_or(&0),
            engine.mixer.increment,
        );
        if engine.docs.pages.get(&name) != Some(&page) {
            engine.docs.pages.insert(name, page);
            engine.generation += 1;
        }
    }
    Ok(())
}
fn run_action(
    shared: &Shared,
    event: &InputEvent,
    action: &Value,
    processes: &mut HashMap<String, plugin::Process>,
) -> Result<()> {
    let raw_action = action;
    let resolved = crate::builtins::resolve(action, &event.event)?;
    let action = &resolved;
    let id = action["id"].as_str().context("action ID missing")?;
    let settings = &action["settings"];
    match id {
        "native::input" => {
            if event.event == "press" {
                crate::uinput::execute_owned(
                    settings["operation"].as_str().unwrap_or("keys"),
                    settings,
                    &format!("{}/{}/{}", event.serial, event.family, event.input),
                )?;
            } else if event.event == "release" {
                crate::uinput::release_owner(&format!(
                    "{}/{}/{}",
                    event.serial, event.family, event.input
                ));
            }
        }
        "native::delay" => {
            let delay = settings["delay"].as_f64().unwrap_or(0.0);
            ensure!(
                delay.is_finite() && (0.0..=86400.0 * 365.0).contains(&delay),
                "invalid delay"
            );
            crate::desktop::wait(Duration::from_secs_f64(delay))?;
        }
        "native::launch" => crate::desktop::launch(
            settings["path"]
                .as_str()
                .context("application path missing")?,
        )?,
        "native::audio" => crate::audio::control(
            settings["target"].as_str().unwrap_or("output"),
            settings["operation"].as_str().unwrap_or("toggle-mute"),
            settings["value"].as_f64().unwrap_or(5.0),
        )?,
        "native::obs" => {
            let connection = {
                let engine = shared.lock().unwrap();
                let name = settings["connection"].as_str().unwrap_or("default");
                engine.obs_profile(name)
            };
            ensure!(
                connection.is_object(),
                "OBS connection profile is missing; configure obs.connections in Settings"
            );
            crate::obs::execute(settings, &connection)?;
        }
        "native::mixer" => {
            let operation = settings["operation"].as_str().unwrap_or("open");
            if operation == "exit" {
                let mut engine = shared.lock().unwrap();
                if let Some(page) = engine.mixer.originals.remove(&event.serial) {
                    engine.command(&json!({"method":"change-page","params":{"serial":event.serial,"page":page}}))?;
                }
            } else if ["mute", "volume-up", "volume-down"].contains(&operation) {
                let streams = crate::audio::streams()?;
                let (offset, increment) = {
                    let engine = shared.lock().unwrap();
                    (
                        *engine.mixer.offsets.get(&event.serial).unwrap_or(&0),
                        engine.mixer.increment,
                    )
                };
                let index = if event.family == "dials" {
                    event.input.parse::<usize>()?
                } else {
                    event
                        .input
                        .split('x')
                        .next()
                        .context("mixer coordinates")?
                        .parse::<usize>()?
                        .saturating_sub(1)
                };
                if let Some(stream) = streams.get(offset + index) {
                    crate::audio::control(
                        &format!("stream:{}", stream.id),
                        if operation == "mute" {
                            "toggle-mute"
                        } else {
                            "set-volume"
                        },
                        (stream.volume
                            + if operation == "volume-up" {
                                increment
                            } else {
                                -increment
                            })
                        .clamp(0.0, 100.0),
                    )?;
                }
            } else {
                let streams = crate::audio::streams()?;
                let mut engine = shared.lock().unwrap();
                let device = engine
                    .devices
                    .get(&event.serial)
                    .context("mixer device missing")?;
                let kind = device.kind;
                let rotation = engine.docs.settings["devices"][&event.serial]["rotation"]
                    .as_u64()
                    .unwrap_or(0) as u16;
                let original = device.page.clone();
                let page = format!("Native Mixer {}", event.serial);
                ensure!(
                    engine
                        .docs
                        .pages
                        .get(&page)
                        .is_none_or(|p| p["deckard"]["native-mixer"] == true),
                    "mixer page name is already in use"
                );
                if operation == "open" {
                    if original != page {
                        engine
                            .mixer
                            .originals
                            .insert(event.serial.clone(), original);
                    }
                    engine.mixer.offsets.insert(event.serial.clone(), 0);
                    engine.mixer.increment = settings["increments"]
                        .as_f64()
                        .unwrap_or(10.0)
                        .clamp(0.0, 100.0);
                }
                let step = (render::layout(kind, rotation).1 as usize)
                    .saturating_sub(1)
                    .max(1);
                let offset = engine
                    .mixer
                    .offsets
                    .entry(event.serial.clone())
                    .or_default();
                match operation {
                    "right" => *offset = (*offset + step).min(streams.len().saturating_sub(1)),
                    "left" => *offset = offset.saturating_sub(step),
                    "open" => {}
                    _ => anyhow::bail!("unsupported mixer operation"),
                };
                let offset = *offset;
                let increment = engine.mixer.increment;
                engine.docs.put(
                    &page,
                    crate::audio::mixer_page(kind, rotation, &streams, offset, increment),
                )?;
                engine.command(
                    &json!({"method":"change-page","params":{"serial":event.serial,"page":page}}),
                )?;
            }
        }
        "native::media"
            if ["Info", "Thumbnail"].contains(&settings["method"].as_str().unwrap_or("")) => {}
        "native::media" => crate::mpris::control(
            settings["method"].as_str().unwrap_or("PlayPause"),
            settings["player"].as_str().unwrap_or(""),
        )?,
        "native::shell" => {
            let origin = action_origin(shared, event, raw_action)?;
            crate::live::execute_shell(shared, &origin)?;
        }
        "native::system" => {
            let origin = action_origin(shared, event, raw_action)?;
            if settings["metric"] == "Ping" {
                crate::live::overlay(shared, &origin, crate::system::ping(settings)?);
            } else if settings["toggle-dynamic-scaling-on-press"] == true {
                let mut engine = shared.lock().unwrap();
                let config = engine.config(&origin.serial).context("device config")?;
                let sticky = render::effective_input(&config, &origin.family, &origin.input)
                    == &config.sticky[&origin.family][&origin.input];
                let mut doc = if sticky {
                    engine.docs.sticky(&origin.serial)?
                } else {
                    engine.docs.pages[&origin.page].clone()
                };
                let state =
                    model::state_mut(&mut doc, &origin.family, &origin.input, origin.state)?;
                if let Some(actions) = state["actions"].as_array_mut()
                    && let Some(action) = actions.get_mut(origin.index)
                {
                    action["settings"]["dynamic-scaling"] =
                        json!(!settings["dynamic-scaling"].as_bool().unwrap_or(false));
                }
                if sticky {
                    engine.docs.put_sticky(&origin.serial, &doc)?;
                } else {
                    engine.docs.put(&origin.page, doc)?;
                }
            }
        }
        "native::command" => {
            let argv: Vec<String> = settings["argv"]
                .as_array()
                .context("command needs argv array")?
                .iter()
                .map(|v| {
                    v.as_str()
                        .map(str::to_owned)
                        .context("argv must contain strings")
                })
                .collect::<Result<_>>()?;
            crate::desktop::run_command(&argv, Duration::from_secs(86400))?;
        }
        "native::url" => crate::desktop::open_url(settings)?,
        "native::page" => {
            let mut engine = shared.lock().unwrap();
            let serial = settings["serial"]
                .as_str()
                .filter(|s| engine.devices.contains_key(*s))
                .unwrap_or(&event.serial)
                .to_owned();
            let previous = engine.devices[&serial].page.clone();
            engine.command(
                &json!({"method":"change-page","params":{"serial":serial,"page":settings["page"]}}),
            )?;
            schedule_return(&mut engine, &serial, settings, ReturnTarget::Page(previous))?;
        }
        "native::previous-page" => {
            let mut engine = shared.lock().unwrap();
            let page = engine.devices[&event.serial]
                .previous_page
                .clone()
                .context("no previous page")?;
            engine.command(
                &json!({"method":"change-page","params":{"serial":event.serial,"page":page}}),
            )?;
        }
        "native::state" => {
            let mut engine = shared.lock().unwrap();
            let family = settings["family"].as_str().unwrap_or(&event.family);
            let input = settings["input"].as_str().unwrap_or(&event.input);
            let config = engine.config(&event.serial).context("device config")?;
            let previous = render::active_state(&config, family, input);
            engine.command(&json!({"method":"change-state","params":{"serial":event.serial,"family":family,"input":input,"state":settings["state"]}}))?;
            schedule_return(
                &mut engine,
                &event.serial,
                settings,
                ReturnTarget::State(family.into(), input.into(), previous),
            )?;
        }
        "native::adjust-brightness" => {
            let mut engine = shared.lock().unwrap();
            let value = (engine.devices[&event.serial].brightness as f64
                + settings["adjust"].as_f64().unwrap_or(0.0))
            .clamp(
                settings["min_brightness"]
                    .as_f64()
                    .unwrap_or(0.0)
                    .clamp(0.0, 100.0),
                100.0,
            );
            engine.command(
                &json!({"method":"set-brightness","params":{"serial":event.serial,"value":value}}),
            )?;
        }
        "native::brightness" => {
            shared.lock().unwrap().command(&json!({"method":"set-brightness","params":{"serial":event.serial,"value":settings["value"]}}))?;
        }
        "native::sleep" => {
            shared
                .lock()
                .unwrap()
                .command(&json!({"method":"sleep","params":{"serial":event.serial}}))?;
        }
        "native::text" | "native::hotkey" => {
            let text = settings["text"].as_str().context("text or key missing")?;
            let wayland = std::env::var_os("WAYLAND_DISPLAY").is_some();
            let argv = if wayland {
                if id == "native::hotkey" {
                    let mut parts: Vec<_> = text.split('+').collect();
                    let key = parts.pop().context("hotkey missing")?;
                    let modifiers: Vec<_> = parts
                        .into_iter()
                        .map(|m| {
                            match m.to_lowercase().as_str() {
                                "control" | "ctrl" => "ctrl",
                                "alt" => "alt",
                                "super" | "win" | "meta" | "logo" => "logo",
                                "shift" => "shift",
                                _ => "",
                            }
                            .to_owned()
                        })
                        .collect();
                    ensure!(
                        modifiers.iter().all(|m| !m.is_empty()),
                        "unsupported hotkey modifier"
                    );
                    let mut argv = vec!["wtype".into()];
                    for modifier in &modifiers {
                        argv.extend(["-M".into(), modifier.clone()]);
                    }
                    argv.extend(["-k".into(), key.into()]);
                    for modifier in modifiers.into_iter().rev() {
                        argv.extend(["-m".into(), modifier]);
                    }
                    argv
                } else {
                    vec![
                        "wtype".into(),
                        "-d".into(),
                        settings["delay_ms"]
                            .as_f64()
                            .unwrap_or(10.0)
                            .clamp(0.0, 1000.0)
                            .round()
                            .to_string(),
                        "--".into(),
                        text.into(),
                    ]
                }
            } else {
                let mut argv = vec![
                    "xdotool".into(),
                    if id == "native::text" { "type" } else { "key" }.into(),
                    "--clearmodifiers".into(),
                ];
                if id == "native::text" {
                    argv.extend([
                        "--delay".into(),
                        settings["delay_ms"]
                            .as_f64()
                            .unwrap_or(10.0)
                            .clamp(0.0, 1000.0)
                            .round()
                            .to_string(),
                    ]);
                }
                argv.extend(["--".into(), text.into()]);
                argv
            };
            crate::desktop::run_command(&argv, Duration::from_secs(86400))?;
        }
        _ => {
            let (plugin_id, action_id) = id
                .split_once("::")
                .context("unknown legacy action; replace it with a native action")?;
            let (origin_page, origin_state) = {
                let engine = shared.lock().unwrap();
                let config = engine.config(&event.serial).context("device config")?;
                (
                    engine.devices[&event.serial].page.clone(),
                    render::active_state(&config, &event.family, &event.input),
                )
            };
            let installed = shared
                .lock()
                .unwrap()
                .plugins
                .iter()
                .find(|p| {
                    p.manifest.id == plugin_id
                        && p.manifest.actions.iter().any(|a| a.id == action_id)
                })
                .cloned()
                .context("Python or unavailable plugin action; replace it with a native action")?;
            if !processes.contains_key(plugin_id) {
                if processes.len() >= 16
                    && let Some(key) = processes.keys().next().cloned()
                {
                    processes.remove(&key);
                }
                processes.insert(plugin_id.into(), plugin::Process::start(&installed)?);
            }
            let result=processes.get_mut(plugin_id).unwrap().call("event",json!({"action":action_id,"event":event.event,"value":event.value,"serial":event.serial,"input":{"family":event.family,"id":event.input},"settings":settings}));
            let response = match result {
                Ok(v) => v,
                Err(e) => {
                    processes.remove(plugin_id);
                    return Err(e);
                }
            };
            if response["label"].is_string() || response["color"].is_array() {
                let mut engine = shared.lock().unwrap();
                let config = engine.config(&event.serial).context("device config")?;
                let number = render::active_state(&config, &event.family, &event.input);
                if engine.devices[&event.serial].page != origin_page || number != origin_state {
                    return Ok(());
                }
                engine.overlays.insert(
                    (
                        event.serial.clone(),
                        event.family.clone(),
                        event.input.clone(),
                        number,
                    ),
                    response,
                );
                engine.generation += 1;
            }
        }
    }
    Ok(())
}
pub fn redact(message: &str) -> String {
    static PATTERNS: std::sync::OnceLock<Vec<(regex::Regex, &'static str)>> =
        std::sync::OnceLock::new();
    let patterns = PATTERNS.get_or_init(|| {
        vec![
            (
                regex::Regex::new(r"(https?://)[^/@\s]+:[^/@\s]+@").unwrap(),
                "$1[redacted]@",
            ),
            (
                regex::Regex::new(
                    r"(?i)([?&](?:token|api[_-]?key|access[_-]?token|password|secret)=)[^&\s]+",
                )
                .unwrap(),
                "$1[redacted]",
            ),
            (
                regex::Regex::new(r"(?i)(bearer\s+)[A-Za-z0-9._~/+=-]+").unwrap(),
                "$1[redacted]",
            ),
            (
                regex::Regex::new(r"sk-(?:proj-)?[A-Za-z0-9_-]{12,}").unwrap(),
                "[redacted]",
            ),
        ]
    });
    let mut result = message.to_owned();
    for (pattern, replacement) in patterns {
        result = pattern.replace_all(&result, *replacement).into_owned();
    }
    result
}
pub fn fake_kind(name: &str) -> Result<Kind> {
    Ok(match name.to_lowercase().as_str() {
        "studio" => Kind::Studio,
        "mirabox-293s" => Kind::Mirabox293s,
        "ulanzi-d200" => Kind::UlanziD200,
        "original" => Kind::Original,
        "original-v2" => Kind::OriginalV2,
        "mk2" => Kind::Mk2,
        "mini" => Kind::Mini,
        "xl" => Kind::Xl,
        "xl-v2" => Kind::XlV2,
        "mk2-scissor" => Kind::Mk2Scissor,
        "mini-mk2" => Kind::MiniMk2,
        "mini-discord" => Kind::MiniDiscord,
        "mini-module" => Kind::MiniMk2Module,
        "mk2-module" => Kind::Mk2Module,
        "xl-module" => Kind::XlV2Module,
        "plus" => Kind::Plus,
        "plus-xl" => Kind::PlusXl,
        "neo" => Kind::Neo,
        "pedal" => Kind::Pedal,
        _ => bail!("unknown fake model {name}"),
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    fn wait_for(shared: &Shared, predicate: impl Fn(&Engine) -> bool) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while !predicate(&shared.lock().unwrap()) {
            assert!(
                Instant::now() < deadline,
                "native transition timed out: {}",
                shared.lock().unwrap().status()
            );
            thread::sleep(Duration::from_millis(10));
        }
    }
    fn event(runtime: &Runtime, input: &str, name: &str) {
        runtime
            .events
            .send(InputEvent {
                serial: "FAKE-PLUS-0".into(),
                family: "keys".into(),
                input: input.into(),
                event: name.into(),
                value: 1,
            })
            .unwrap();
    }
    #[test]
    fn long_delays_do_not_block_other_inputs_and_shutdown_cancels_sequences() {
        let tmp = tempfile::tempdir().unwrap();
        let shared = Engine::open(tmp.path().into()).unwrap();
        shared.lock().unwrap().docs.put("Main",json!({"keys":{
            "0x0":{"states":{"0":{"actions":[{"id":"native::delay","settings":{"delay":60}},{"id":"native::brightness","settings":{"value":99}}]}}},
            "1x0":{"states":{"0":{"actions":[{"id":"native::brightness","settings":{"value":42}}]}}}
        }})).unwrap();
        let runtime = Runtime::start(shared.clone(), vec![Kind::Plus], false).unwrap();
        wait_for(&shared, |e| e.devices.contains_key("FAKE-PLUS-0"));
        event(&runtime, "0x0", "press");
        thread::sleep(Duration::from_millis(50));
        event(&runtime, "1x0", "press");
        wait_for(&shared, |e| e.devices["FAKE-PLUS-0"].brightness == 42);
        let start = Instant::now();
        drop(runtime);
        assert!(start.elapsed() < Duration::from_secs(2));
        assert_eq!(shared.lock().unwrap().devices["FAKE-PLUS-0"].brightness, 42);
    }
    #[test]
    fn timed_page_and_other_input_state_return_and_manual_navigation_cancels_old_return() {
        let tmp = tempfile::tempdir().unwrap();
        let shared = Engine::open(tmp.path().into()).unwrap();
        {
            let mut e = shared.lock().unwrap();
            e.docs.put("Main",json!({"keys":{
            "0x0":{"states":{"0":{"actions":[{"id":"native::page","settings":{"page":"Menu","return_timeout":0.15}}]}}},
            "1x0":{"states":{"0":{},"1":{}}},
            "2x0":{"states":{"0":{"actions":[{"id":"native::state","settings":{"input":"1x0","state":1,"return_timeout":0.15}}]}}}
        }})).unwrap();
            e.docs.create("Menu").unwrap();
            e.docs.create("Other").unwrap();
        }
        let runtime = Runtime::start(shared.clone(), vec![Kind::Plus], false).unwrap();
        wait_for(&shared, |e| e.devices.contains_key("FAKE-PLUS-0"));
        event(&runtime, "0x0", "press");
        wait_for(&shared, |e| e.devices["FAKE-PLUS-0"].page == "Menu");
        wait_for(&shared, |e| e.devices["FAKE-PLUS-0"].page == "Main");
        event(&runtime, "2x0", "press");
        wait_for(&shared, |e| {
            e.devices["FAKE-PLUS-0"].states.get("keys/1x0") == Some(&1)
        });
        wait_for(&shared, |e| {
            e.devices["FAKE-PLUS-0"].states.get("keys/1x0") == Some(&0)
        });
        event(&runtime, "0x0", "press");
        wait_for(&shared, |e| e.devices["FAKE-PLUS-0"].page == "Menu");
        shared
            .lock()
            .unwrap()
            .command(
                &json!({"method":"change-page","params":{"serial":"FAKE-PLUS-0","page":"Other"}}),
            )
            .unwrap();
        thread::sleep(Duration::from_millis(250));
        assert_eq!(shared.lock().unwrap().devices["FAKE-PLUS-0"].page, "Other");
    }
    #[test]
    fn periodic_shell_output_updates_without_presses_and_stops_on_page_change() {
        let tmp = tempfile::tempdir().unwrap();
        let shared = Engine::open(tmp.path().into()).unwrap();
        let count = tmp.path().join("count");
        let command = format!(
            "n=$(cat {} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {}; printf 'tick-%s' $n",
            count.display(),
            count.display()
        );
        {
            let mut e = shared.lock().unwrap();
            e.docs.put("Main",json!({"keys":{"0x0":{"states":{"0":{"actions":[{"id":"native::OSPlugin-RunCommand","event":"auto","settings":{"command":command,"auto_run":0.1,"detached":false,"display_output":true,"label_position":"top"}}]}}}}})).unwrap();
            e.docs.create("Other").unwrap();
        }
        let runtime = Runtime::start(shared.clone(), vec![Kind::Plus], false).unwrap();
        wait_for(&shared, |e| {
            e.live_overlays.values().any(|(_, v)| {
                v["labels"]["top"]["text"]
                    .as_str()
                    .is_some_and(|s| s.starts_with("tick-"))
            })
        });
        shared
            .lock()
            .unwrap()
            .command(
                &json!({"method":"change-page","params":{"serial":"FAKE-PLUS-0","page":"Other"}}),
            )
            .unwrap();
        thread::sleep(Duration::from_millis(250));
        let before = std::fs::read(&count).unwrap();
        thread::sleep(Duration::from_millis(250));
        assert_eq!(std::fs::read(&count).unwrap(), before);
        assert!(shared.lock().unwrap().errors.is_empty());
        drop(runtime);
    }
    #[test]
    fn sleep_action_survives_release_and_wake_gesture_does_not_run_actions() {
        let tmp = tempfile::tempdir().unwrap();
        let shared = Engine::open(tmp.path().into()).unwrap();
        shared
            .lock()
            .unwrap()
            .docs
            .put(
                "Main",
                json!({"keys":{"0x0":{"states":{"0":{"actions":[
                    {"id":"native::sleep","event":"press"},
                    {"id":"native::brightness","event":"release","settings":{"value":99}},
                    {"id":"native::brightness","event":"long-press","settings":{"value":88}}
                ]}}}}}),
            )
            .unwrap();
        let runtime = Runtime::start(shared.clone(), vec![Kind::Plus], false).unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        while shared.lock().unwrap().devices.is_empty() {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(10));
        }
        let serial = shared
            .lock()
            .unwrap()
            .devices
            .keys()
            .next()
            .unwrap()
            .clone();
        let send = |event: &str| {
            runtime
                .events
                .send(InputEvent {
                    serial: serial.clone(),
                    family: "keys".into(),
                    input: "0x0".into(),
                    event: event.into(),
                    value: 1,
                })
                .unwrap()
        };
        let wait = |predicate: &dyn Fn(&Device) -> bool| {
            let deadline = Instant::now() + Duration::from_secs(3);
            loop {
                if predicate(&shared.lock().unwrap().devices[&serial]) {
                    break;
                }
                assert!(Instant::now() < deadline, "input transition timed out");
                thread::sleep(Duration::from_millis(10));
            }
        };
        send("press");
        wait(&|d| d.sleeping);
        // A held Sleep key must not wake the deck either.
        thread::sleep(Duration::from_millis(650));
        assert!(shared.lock().unwrap().devices[&serial].sleeping);
        send("release");
        wait(&|d| d.pressed.is_empty());
        assert!(shared.lock().unwrap().devices[&serial].sleeping);
        send("press");
        wait(&|d| !d.sleeping && !d.pressed.is_empty());
        send("release");
        wait(&|d| d.pressed.is_empty());
        let engine = shared.lock().unwrap();
        assert!(!engine.devices[&serial].sleeping);
        assert_eq!(engine.devices[&serial].brightness, 75);
    }
    #[test]
    fn error_redaction_preserves_useful_context_without_credentials() {
        let result = redact(
            "https://user:password@host/path?token=secret&other=ok Bearer abc.def sk-proj-abcdefghijklmno",
        );
        assert!(!result.contains("password"));
        assert!(!result.contains("secret"));
        assert!(!result.contains("abc.def"));
        assert!(!result.contains("abcdefghijklmno"));
        assert!(result.contains("other=ok"));
    }
    #[test]
    fn rename_and_delete_active_pages_retarget_devices_and_persist_defaults() {
        let tmp = tempfile::tempdir().unwrap();
        let shared = Engine::open(tmp.path().into()).unwrap();
        let runtime = Runtime::start(shared.clone(), vec![Kind::Plus], false).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        while shared.lock().unwrap().devices.is_empty() {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(10));
        }
        let mut engine = shared.lock().unwrap();
        let serial = engine.devices.keys().next().unwrap().clone();
        engine
            .command(&json!({"method":"change-page","params":{"serial":serial,"page":"Main"}}))
            .unwrap();
        engine
            .command(&json!({"method":"rename-page","params":{"page":"Main","name":"Renamed"}}))
            .unwrap();
        assert_eq!(engine.devices[&serial].page, "Renamed");
        assert_eq!(engine.docs.settings["devices"][&serial]["page"], "Renamed");
        engine.docs.create("Survivor").unwrap();
        engine
            .command(&json!({"method":"delete-page","params":{"page":"Renamed"}}))
            .unwrap();
        assert_eq!(engine.devices[&serial].page, "Survivor");
        assert!(engine.config(&serial).is_some());
        assert_eq!(
            Documents::open(tmp.path().into()).unwrap().settings["devices"][&serial]["page"],
            "Survivor"
        );
        drop(engine);
        drop(runtime);
    }
    #[test]
    fn held_input_dispatches_long_press_once_and_releases_visual_state() {
        let tmp = tempfile::tempdir().unwrap();
        let shared = Engine::open(tmp.path().into()).unwrap();
        let runtime = Runtime::start(shared.clone(), vec![Kind::Plus], false).unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        while shared.lock().unwrap().devices.is_empty() {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(10));
        }
        let serial = shared
            .lock()
            .unwrap()
            .devices
            .keys()
            .next()
            .unwrap()
            .clone();
        shared.lock().unwrap().docs.edit("Main", |page| { model::state_mut(page, "keys", "0x0", 0)?["actions"] = json!([{"id":"native::brightness","event":"long-press","settings":{"value":42}}]); Ok(()) }).unwrap();
        runtime
            .events
            .send(InputEvent {
                serial: serial.clone(),
                family: "keys".into(),
                input: "0x0".into(),
                event: "press".into(),
                value: 1,
            })
            .unwrap();
        while shared.lock().unwrap().devices[&serial].brightness != 42 {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(20));
        }
        runtime
            .events
            .send(InputEvent {
                serial: serial.clone(),
                family: "keys".into(),
                input: "0x0".into(),
                event: "release".into(),
                value: 0,
            })
            .unwrap();
        while shared.lock().unwrap().devices[&serial]
            .pressed
            .contains("0x0")
        {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(10));
        }
        drop(runtime);
    }
    #[test]
    fn page_switch_releases_pressed_state_and_state_survives_restart() {
        let tmp = tempfile::tempdir().unwrap();
        let shared = Engine::open(tmp.path().into()).unwrap();
        let runtime = Runtime::start(shared.clone(), vec![Kind::Plus], false).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        while shared.lock().unwrap().devices.is_empty() {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(10));
        }
        let mut engine = shared.lock().unwrap();
        let serial = engine.devices.keys().next().unwrap().clone();
        engine
            .docs
            .edit("Main", |p| {
                model::state_mut(p, "keys", "0x0", 0)?;
                model::state_mut(p, "keys", "0x0", 1)?;
                Ok(())
            })
            .unwrap();
        engine.command(&json!({"method":"change-state","params":{"serial":serial,"input":"0x0","state":1}})).unwrap();
        assert_eq!(engine.docs.pages["Main"]["keys"]["0x0"]["active-state"], 1);
        engine
            .devices
            .get_mut(&serial)
            .unwrap()
            .pressed
            .insert("0x0".into());
        engine.docs.create("Second").unwrap();
        engine
            .command(&json!({"method":"change-page","params":{"serial":serial,"page":"Second"}}))
            .unwrap();
        assert!(engine.devices[&serial].pressed.is_empty());
        drop(engine);
        drop(runtime);
    }
}
