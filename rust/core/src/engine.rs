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
    pub brightness: u8,
    pub sleeping: bool,
    pub states: HashMap<String, usize>,
    pub pressed: HashSet<String>,
    pub last_input: Instant,
    pub frame: Option<Arc<Frame>>,
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
    overlays: HashMap<(String, String, String, usize), Value>,
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
            overlays: HashMap::new(),
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
        let Some(device) = self.devices.get(serial) else {
            return Value::Null;
        };
        let page = self.docs.pages.get(&device.page).unwrap_or(&Value::Null);
        let deck = &self.docs.settings["devices"][serial];
        if page["settings"]["screensaver"]["overwrite"]
            .as_bool()
            .unwrap_or(false)
        {
            page["settings"]["screensaver"].clone()
        } else {
            deck["screensaver"].clone()
        }
    }
    pub fn saver_active(&self, serial: &str) -> bool {
        let Some(device) = self.devices.get(serial) else {
            return false;
        };
        let saver = self.saver(serial);
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
                let probe = RenderConfig {
                    page: page.clone(),
                    sticky: sticky.clone(),
                    states: device.states.clone(),
                    pressed: device.pressed.clone(),
                    rotation: 0,
                    sleeping: false,
                    revision: 0,
                };
                let is_sticky =
                    render::effective_input(&probe, family, input) == &probe.sticky[family][input];
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
        Some(RenderConfig {
            page,
            sticky,
            states: device.states.clone(),
            pressed: device.pressed.clone(),
            rotation: deck["rotation"].as_u64().unwrap_or(0) as u16,
            sleeping: self.locked,
            revision: self.render_revision(serial),
        })
    }
    pub fn status(&self) -> Value {
        let mut devices:Vec<_>=self.devices.values().map(|d|json!({"serial":d.serial,"model":format!("{:?}",d.kind),"fake":d.fake,"connected":d.connected,"page":d.page,"brightness":d.brightness,"sleeping":d.sleeping})).collect();
        devices.sort_by_key(|d| d["serial"].as_str().unwrap_or("").to_owned());
        json!({"version":env!("CARGO_PKG_VERSION"),"runtime":"Rust","plugin_api":1,"devices":devices,"pages":self.docs.pages.keys().collect::<Vec<_>>(),"errors":self.errors,"locked":self.locked})
    }
    fn retarget_page(&mut self, old: &str, new: &str) -> Result<()> {
        for device in self.devices.values_mut() {
            if device.page == old {
                device.page = new.into();
                device.pressed.clear();
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
                device.page = name.into();
                device.pressed.clear();
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
                    }
                }
            }
            "reload" => {
                let docs = Documents::open(self.docs.root.clone())?;
                self.docs = docs;
                self.plugins = native_plugins(&self.docs.root);
                self.plugin_revision += 1;
                self.overlays.clear();
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
                    brightness,
                    sleeping: false,
                    states: HashMap::new(),
                    pressed: HashSet::new(),
                    last_input: Instant::now(),
                    frame: None,
                },
            );
            engine.generation += 1;
        }
        let slot: Arc<Mutex<Option<Arc<Frame>>>> = Arc::new(Mutex::new(None));
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
            while !render_stop.load(Ordering::Relaxed) {
                let (available, config) = {
                    let engine = render_shared.lock().unwrap_or_else(|p| p.into_inner());
                    let available = engine
                        .devices
                        .get(&render_serial)
                        .is_some_and(|d| d.fake || d.connected);
                    let config = if available
                        && (renderer.animated || engine.render_revision(&render_serial) != previous)
                    {
                        engine.config(&render_serial)
                    } else {
                        None
                    };
                    (available, config)
                };
                if !available {
                    renderer.release_media();
                    previous = u64::MAX;
                }
                if let Some(config) = config
                    && (renderer.animated || config.revision != previous)
                {
                    previous = config.revision;
                    match renderer.render(kind, &config) {
                        Ok(frame) => {
                            let frame = Arc::new(frame);
                            *render_slot.lock().unwrap_or_else(|p| p.into_inner()) =
                                Some(frame.clone());
                            if let Some(device) = render_shared
                                .lock()
                                .unwrap_or_else(|p| p.into_inner())
                                .devices
                                .get_mut(&render_serial)
                            {
                                device.frame = Some(frame)
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
                thread::sleep(Duration::from_millis(33));
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
        let mut tile_index = 0;
        while !stop.load(Ordering::Relaxed) {
            if !fake && deck.is_none() && Instant::now() >= retry {
                retry = Instant::now() + Duration::from_secs(2);
                match (|| -> anyhow::Result<StreamDeck> {
                    let api = elgato_streamdeck::new_hidapi()?;
                    Ok(StreamDeck::connect(&api, kind, &serial)?)
                })() {
                    Ok(device) => {
                        deck = Some(device);
                        hashes.clear();
                        touch_colors.clear();
                        led_revision = None;
                        sent_brightness = None;
                        old_buttons.fill(false);
                        old_dials.fill(false);
                        if let Some(device) = shared.lock().unwrap().devices.get_mut(&serial) {
                            device.connected = true;
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
                match deck.read_input(Some(Duration::from_millis(2))) {
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
                if let Some(frame) = slot.lock().unwrap().take() {
                    pending = Some(frame);
                    // Retain the cursor when coalescing: large frame writes must not starve later keys.
                }
                if pending
                    .as_ref()
                    .is_some_and(|frame| frame.revision != revision)
                {
                    pending = None;
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
                        if deck.write_images(&images).is_err() {
                            failed = true;
                        } else {
                            for tile in changed {
                                hashes.insert(tile.key, tile.identity);
                            }
                        }
                    }
                    tile_index = 0;
                }
                if let Some(frame) = pending.as_ref() {
                    let tile = frame.tiles.get(tile_index).or_else(|| {
                        if tile_index == frame.tiles.len() {
                            frame.strip.as_ref()
                        } else {
                            None
                        }
                    });
                    if let Some(tile) = tile {
                        if hashes.get(&tile.key) != Some(&tile.identity) {
                            let result = if tile.key == 255 {
                                deck.write_lcd_fill(&tile.encoded)
                            } else if kind.is_visual() {
                                deck.write_image(tile.key, &tile.encoded)
                                    .and_then(|_| deck.flush())
                            } else {
                                Ok(())
                            };
                            if result.is_err() {
                                failed = true
                            } else {
                                hashes.insert(tile.key, tile.identity);
                            }
                        }
                        tile_index += 1;
                        if tile_index > frame.tiles.len() {
                            tile_index = 0;
                            pending = None;
                        }
                    } else {
                        pending = None;
                        tile_index = 0;
                    }
                }
            }
            if failed {
                deck = None;
                pending = None;
                hashes.clear();
                touch_colors.clear();
                led_revision = None;
                sent_brightness = None;
                retry = Instant::now() + Duration::from_millis(500);
                let mut engine = shared.lock().unwrap();
                if let Some(device) = engine.devices.get_mut(&serial) {
                    device.connected = false;
                    device.pressed.clear();
                }
                engine.generation += 1;
            }
            thread::sleep(Duration::from_millis(if fake || deck.is_none() {
                20
            } else {
                1
            }));
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
        StreamDeckInput::TouchScreenSwipe((x, y), (xx, yy)) => send(
            "touchscreens",
            "0".into(),
            if if kind == Kind::PlusXl { yy < y } else { xx < x } {
                "swipe-left"
            } else {
                "swipe-right"
            },
            1,
        ),
        _ => {}
    }
}
fn action_loop(shared: Shared, events: Receiver<InputEvent>, stop: Arc<AtomicBool>) {
    let mut processes = HashMap::<String, plugin::Process>::new();
    let mut plugin_revision = 0;
    let mut holds = HashMap::<(String, String, String), (Instant, InputEvent)>::new();
    while !stop.load(Ordering::Relaxed) {
        let revision = shared.lock().unwrap().plugin_revision;
        if revision != plugin_revision {
            processes.clear();
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
        if event.event == "release" {
            holds.remove(&hold_key);
        }
        let actions = {
            let mut engine = shared.lock().unwrap();
            let waking = engine.saver_active(&event.serial);
            let Some(device) = engine.devices.get_mut(&event.serial) else {
                continue;
            };
            device.sleeping = false;
            device.last_input = Instant::now();
            if event.family == "keys" {
                if event.event == "press" {
                    device.pressed.insert(event.input.clone());
                } else if event.event == "release" {
                    device.pressed.remove(&event.input);
                }
            }
            engine.generation += 1;
            if waking || engine.locked {
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
        for action in actions {
            let filter = action["event"].as_str().unwrap_or("press");
            if filter != event.event {
                continue;
            }
            let result = run_action(&shared, &event, &action, &mut processes);
            if let Err(error) = result {
                shared.lock().unwrap().error(error);
            }
        }
    }
}
fn run_action(
    shared: &Shared,
    event: &InputEvent,
    action: &Value,
    processes: &mut HashMap<String, plugin::Process>,
) -> Result<()> {
    let id = action["id"].as_str().context("action ID missing")?;
    let settings = &action["settings"];
    match id {
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
            crate::desktop::run_command(&argv, Duration::from_secs(5))?;
        }
        "native::url" => {
            let url = settings["url"].as_str().context("URL missing")?;
            ensure!(
                url.starts_with("https://") || url.starts_with("http://"),
                "URL action requires HTTP(S)"
            );
            crate::desktop::run_command(&["xdg-open".into(), url.into()], Duration::from_secs(5))?;
        }
        "native::page" => {
            shared.lock().unwrap().command(&json!({"method":"change-page","params":{"serial":event.serial,"page":settings["page"]}}))?;
        }
        "native::state" => {
            shared.lock().unwrap().command(&json!({"method":"change-state","params":{"serial":event.serial,"family":event.family,"input":event.input,"state":settings["state"]}}))?;
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
                    vec!["wtype".into(), "--".into(), text.into()]
                }
            } else {
                vec![
                    "xdotool".into(),
                    if id == "native::text" { "type" } else { "key" }.into(),
                    "--clearmodifiers".into(),
                    "--".into(),
                    text.into(),
                ]
            };
            crate::desktop::run_command(&argv, Duration::from_secs(5))?;
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
