//! Lifecycle-owned native readouts. IO never runs while holding the engine lock.
use crate::{engine::Shared, render};
use anyhow::{Context, Result};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    fs,
    io::Read,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
#[derive(Clone, Debug, PartialEq)]
pub struct Action {
    pub serial: String,
    pub page: String,
    pub epoch: u64,
    pub family: String,
    pub input: String,
    pub state: usize,
    pub index: usize,
    pub raw: Value,
    pub action: Value,
}
impl Action {
    pub fn key(&self) -> String {
        format!(
            "{}/{}/{}/{}/{}/{}",
            self.serial, self.epoch, self.family, self.input, self.state, self.index
        )
    }
    pub fn valid(&self, shared: &Shared) -> bool {
        self.valid_in(&shared.lock().unwrap())
    }
    fn valid_in(&self, e: &crate::engine::Engine) -> bool {
        let Some(d) = e.devices.get(&self.serial) else {
            return false;
        };
        if !(d.fake || d.connected) || d.epoch != self.epoch || d.page != self.page {
            return false;
        }
        e.active_input(&self.serial, &self.family, &self.input)
            .is_some_and(|(number, data)| {
                number == self.state
                    && data["states"][self.state.to_string()]["actions"][self.index] == self.raw
            })
    }
}
pub fn collect(shared: &Shared) -> Vec<Action> {
    let e = shared.lock().unwrap();
    let mut actions = Vec::new();
    for d in e.devices.values().filter(|d| d.fake || d.connected) {
        if e.locked || e.saver_active(&d.serial) {
            continue;
        }
        let Some(page) = e.docs.pages.get(&d.page) else {
            continue;
        };
        for family in ["keys", "dials", "touchscreens", "infobar"] {
            let mut inputs = HashSet::new();
            for doc in [page, e.docs.sticky_ref(&d.serial)] {
                if let Some(map) = doc[family].as_object() {
                    inputs.extend(map.keys().cloned());
                }
            }
            for input in inputs {
                let Some((number, data)) = e.active_input(&d.serial, family, &input) else {
                    continue;
                };
                if let Some(list) = data["states"][number.to_string()]["actions"].as_array() {
                    for (index, action) in list.iter().enumerate() {
                        if action["id"]
                            .as_str()
                            .is_some_and(|id| id.starts_with("native::"))
                        {
                            actions.push(Action {
                                serial: d.serial.clone(),
                                page: d.page.clone(),
                                epoch: d.epoch,
                                family: family.into(),
                                input: input.clone(),
                                state: number,
                                index,
                                raw: action.clone(),
                                action: crate::builtins::resolve(action, "press")
                                    .unwrap_or_else(|_| action.clone()),
                            });
                        }
                    }
                }
            }
        }
    }
    actions.sort_by_key(Action::key);
    actions
}
pub fn overlay(shared: &Shared, a: &Action, value: Value) {
    let mut e = shared.lock().unwrap();
    if !a.valid_in(&e) {
        return;
    }
    let key = a.key();
    if e.live_overlays
        .get(&key)
        .is_none_or(|(old, v)| old != a || v != &value)
    {
        e.live_overlays.insert(key, (a.clone(), value));
        e.generation += 1;
    }
}
/// Preserve user labels/media and the legacy per-action ownership selectors.
pub fn apply(state: &mut Value, index: usize, overlay: &Value) {
    for (slot, name) in ["top", "center", "bottom"].iter().enumerate() {
        let permitted = state
            .get("label-control-actions")
            .map(|v| v[slot].as_u64() == Some(index as u64))
            .unwrap_or(index == 0);
        if permitted && let Some(label) = overlay["labels"][name].as_object() {
            for (key, value) in label {
                if state["labels"][name][key].is_null() {
                    state["labels"][name][key] = value.clone();
                }
            }
        }
    }
    let image = state
        .get("image-control-action")
        .map(|v| v.as_u64() == Some(index as u64))
        .unwrap_or(index == 0);
    if image && state["media"]["path"].as_str().is_none_or(str::is_empty) {
        if overlay["media"].is_object() {
            state["media"] = overlay["media"].clone();
        }
        if overlay["visual"].is_object() {
            state["native-visual"] = overlay["visual"].clone();
        }
    }
    let background = state
        .get("background-control-action")
        .map(|v| v.as_u64() == Some(index as u64))
        .unwrap_or(index == 0);
    if background && overlay["color"].is_array() && state["background"]["color"].is_null() {
        state["background"]["color"] = overlay["color"].clone();
    }
}
fn signature(e: &crate::engine::Engine) -> u64 {
    use std::hash::{Hash, Hasher};
    fn hash(value: impl Hash) -> u64 {
        let mut h = std::collections::hash_map::DefaultHasher::new();
        value.hash(&mut h);
        h.finish()
    }
    let devices = e.devices.values().fold(0, |all, d| {
        let states = d.states.iter().fold(0, |acc, pair| acc ^ hash(pair));
        all ^ hash((
            &d.serial,
            &d.page,
            d.epoch,
            d.fake,
            d.connected,
            states,
            e.saver_active(&d.serial),
        ))
    });
    hash((e.docs.revision, e.locked, e.devices.len(), devices))
}
struct Worker {
    stop: Arc<AtomicBool>,
    actions: Arc<Mutex<Arc<Vec<Action>>>>,
    thread: JoinHandle<()>,
}
pub fn watch(shared: Shared, stop: Arc<AtomicBool>) {
    let mut workers = HashMap::<String, Worker>::new();
    let mut previous_signature = None;
    while !stop.load(Ordering::Relaxed) {
        let current_signature = signature(&shared.lock().unwrap());
        if previous_signature == Some(current_signature) {
            thread::sleep(Duration::from_millis(100));
            continue;
        }
        previous_signature = Some(current_signature);
        let actions = collect(&shared);
        let keys = actions
            .iter()
            .map(|a| (a.key(), a))
            .collect::<HashMap<_, _>>();
        {
            let mut e = shared.lock().unwrap();
            let before = e.live_overlays.len() + e.background_overlays.len();
            e.live_overlays
                .retain(|key, (a, _)| keys.get(key).is_some_and(|current| *current == a));
            e.background_overlays.retain(|_, (source, _, _)| {
                keys.get(&source.key())
                    .is_some_and(|current| *current == source)
            });
            if before != e.live_overlays.len() + e.background_overlays.len() {
                e.generation += 1;
            }
        }
        let mut groups = BTreeMap::<String, Vec<Action>>::new();
        for a in actions.iter() {
            let id = a.action["id"].as_str().unwrap_or("");
            let s = &a.action["settings"];
            let group = match id {
                "native::obs" => format!("obs:{}", s["connection"].as_str().unwrap_or("default")),
                "native::media" => "media".into(),
                "native::system" if s["metric"] == "Ping" => format!("ping:{}", a.key()),
                "native::shell" if s["auto_run"].as_f64().unwrap_or(0.0) > 0.0 => {
                    format!("shell:{}", a.key())
                }
                "native::system"
                | "native::adjust-brightness"
                | "native::input"
                | "native::hotkey"
                | "native::text"
                | "native::url"
                | "native::shell"
                | "native::launch"
                | "native::delay"
                | "native::page"
                | "native::previous-page"
                | "native::state"
                | "native::sleep"
                | "native::brightness"
                | "native::mixer" => "system".into(),
                _ => continue,
            };
            groups.entry(group).or_default().push(a.clone());
        }
        // Background commands are explicitly opted in, retained until disconnect/reload/shutdown.
        for (name, worker) in &workers {
            if name.starts_with("shell:") && !groups.contains_key(name) {
                let previous = worker.actions.lock().unwrap().clone();
                if let Some(a) = previous.first()
                    && a.action["settings"]["keep_auto_run_in_background"].as_bool() == Some(true)
                {
                    let e = shared.lock().unwrap();
                    if e.devices
                        .get(&a.serial)
                        .is_some_and(|d| d.fake || d.connected)
                        && e.docs.pages.get(&a.page).is_some_and(|p| {
                            p[&a.family][&a.input]["states"][a.state.to_string()]["actions"]
                                [a.index]
                                == a.raw
                        })
                    {
                        groups.insert(name.clone(), previous.as_ref().clone());
                    }
                }
            }
        }
        {
            let keys = groups
                .values()
                .flatten()
                .filter(|a| a.action["id"] == "native::shell")
                .map(Action::key)
                .collect::<HashSet<_>>();
            shared
                .lock()
                .unwrap()
                .command_runs
                .retain(|key, _| keys.contains(key));
        }
        let removed = workers
            .keys()
            .filter(|key| !groups.contains_key(*key))
            .cloned()
            .collect::<Vec<_>>();
        for key in &removed {
            if let Some(worker) = workers.get(key) {
                worker.stop.store(true, Ordering::Relaxed);
            }
        }
        for key in removed {
            if let Some(worker) = workers.remove(&key) {
                worker.stop.store(true, Ordering::Relaxed);
                let _ = worker.thread.join();
            }
        }
        for (group, actions) in groups {
            if let Some(worker) = workers.get(&group) {
                *worker.actions.lock().unwrap() = Arc::new(actions);
                continue;
            }
            if workers.len() >= 128 {
                shared
                    .lock()
                    .unwrap()
                    .error("live action limit exceeded (128 services)");
                continue;
            }
            let local_stop = Arc::new(AtomicBool::new(false));
            let list = Arc::new(Mutex::new(Arc::new(actions)));
            let thread_stop = local_stop.clone();
            let thread_list = list.clone();
            let thread_shared = shared.clone();
            let kind = group.clone();
            let app_stop = stop.clone();
            let thread = thread::spawn(move || {
                crate::desktop::with_cancellation(
                    Arc::new(move || {
                        thread_stop.load(Ordering::Relaxed) || app_stop.load(Ordering::Relaxed)
                    }),
                    || {
                        if kind == "media" {
                            media_loop(&thread_shared, &thread_list)
                        } else if kind.starts_with("obs:") {
                            obs_loop(&thread_shared, &thread_list)
                        } else if kind.starts_with("ping:") || kind.starts_with("shell:") {
                            timed_loop(&thread_shared, &thread_list, kind.starts_with("shell:"))
                        } else {
                            system_loop(&thread_shared, &thread_list)
                        }
                    },
                )
            });
            workers.insert(
                group,
                Worker {
                    stop: local_stop,
                    actions: list,
                    thread,
                },
            );
        }
        thread::sleep(Duration::from_millis(100));
    }
    for worker in workers.values() {
        worker.stop.store(true, Ordering::Relaxed);
    }
    for (_, worker) in workers {
        let _ = worker.thread.join();
    }
}
fn label(position: &str, text: String, size: u32) -> Value {
    json!({"labels":{position:{"text":text,"font-size":size}}})
}
fn system_loop(shared: &Shared, list: &Mutex<Arc<Vec<Action>>>) {
    let mut metrics = crate::system::Metrics::default();
    let mut history = HashMap::<String, Vec<f64>>::new();
    while !crate::desktop::cancelled() {
        let actions = list.lock().unwrap().clone();
        let (cpu, ram) = if actions.iter().any(|a| {
            matches!(
                a.action["settings"]["metric"].as_str(),
                Some("CPU" | "RAM" | "CPU_Graph" | "RAM_Graph")
            )
        }) {
            metrics.read().unwrap_or((0.0, 0.0))
        } else {
            (0.0, 0.0)
        };
        let valid = actions.iter().map(Action::key).collect::<HashSet<_>>();
        history.retain(|key, _| valid.contains(key));
        for a in actions.iter() {
            let s = &a.action["settings"];
            let id = a.action["id"].as_str().unwrap_or("");
            let value = if id == "native::system" {
                match s["metric"].as_str().unwrap_or("CPU") {
                    "CPUTemp" => {
                        let unit = s["unit"].as_str().unwrap_or("C");
                        let text = crate::system::temperature()
                            .map(|v| {
                                format!(
                                    "{:.0} °{unit}",
                                    if unit == "F" { v * 1.8 + 32.0 } else { v }
                                )
                            })
                            .unwrap_or("N/A".into());
                        label("center", text, 18)
                    }
                    metric => {
                        let value = if metric.starts_with("RAM") { ram } else { cpu };
                        let mut result = label("center", format!("{value:.0}%"), 24);
                        if metric.ends_with("_Graph") {
                            let points = history.entry(a.key()).or_default();
                            points.push(value);
                            let length =
                                s["time-period"].as_u64().unwrap_or(15).clamp(2, 3600) as usize;
                            if points.len() > length {
                                points.drain(..points.len() - length);
                            }
                            result["visual"] = json!({"graph":points,"settings":s});
                        }
                        result
                    }
                }
            } else if id == "native::adjust-brightness" {
                let value = shared
                    .lock()
                    .unwrap()
                    .devices
                    .get(&a.serial)
                    .map(|d| d.brightness)
                    .unwrap_or(0) as f64;
                let text = if s["adjust"].as_f64().unwrap_or(0.0) > 0.0 && value >= 100.0 {
                    "Max"
                } else if s["adjust"].as_f64().unwrap_or(0.0) < 0.0
                    && value <= s["min_brightness"].as_f64().unwrap_or(0.0)
                {
                    "Min"
                } else {
                    ""
                };
                let mut value = label("bottom", text.into(), 14);
                value["visual"] = json!({"symbol":"brightness","active":true});
                value
            } else {
                json!({"visual":{"symbol":id.trim_start_matches("native::"),"active":true}})
            };
            overlay(shared, a, value);
        }
        let _ = crate::desktop::wait(Duration::from_secs(1));
    }
}
fn timed_loop(shared: &Shared, list: &Mutex<Arc<Vec<Action>>>, shell: bool) {
    let Some(mut a) = list.lock().unwrap().first().cloned() else {
        return;
    };
    let mut next = Instant::now();
    let mut last_run = None;
    let mut was_held = false;
    if shell {
        next += seconds(a.action["settings"]["auto_run"].as_f64().unwrap_or(0.0));
    }
    while !crate::desktop::cancelled() {
        let current = list.lock().unwrap().clone();
        if let Some(current) = current.first()
            && current != &a
        {
            next = Instant::now();
            a = current.clone();
        }
        let (held, completed) = {
            let e = shared.lock().unwrap();
            (
                e.command_holds
                    .contains(&format!("{}/{}/{}", a.serial, a.family, a.input)),
                e.command_runs.get(&a.key()).copied(),
            )
        };
        if shell && (completed != last_run || was_held && !held) {
            next = completed.unwrap_or_else(Instant::now).max(Instant::now())
                + seconds(a.action["settings"]["auto_run"].as_f64().unwrap_or(0.0));
            last_run = completed;
        }
        was_held = held;
        if Instant::now() >= next {
            let s = &a.action["settings"];
            if !shell || !held {
                if shell {
                    let _ = execute_shell(shared, &a);
                } else {
                    match crate::system::ping(s) {
                        Ok(value) => overlay(shared, &a, value),
                        Err(e) => shared.lock().unwrap().error(e),
                    }
                }
            }
            let interval = if shell {
                s["auto_run"].as_f64().unwrap_or(0.0)
            } else {
                s["interval"].as_f64().unwrap_or(0.0)
            };
            if interval <= 0.0 {
                next = Instant::now() + Duration::from_secs(86400 * 365);
            } else {
                next = Instant::now() + seconds(interval);
            }
        }
        let _ = crate::desktop::wait(
            next.saturating_duration_since(Instant::now())
                .clamp(Duration::from_millis(20), Duration::from_millis(250)),
        );
    }
}
fn seconds(value: f64) -> Duration {
    Duration::from_secs_f64(if value.is_finite() {
        value.clamp(0.001, 86400.0 * 365.0)
    } else {
        1.0
    })
}
pub fn execute_shell(shared: &Shared, a: &Action) -> Result<()> {
    static LOCKS: std::sync::OnceLock<Mutex<HashMap<String, std::sync::Weak<Mutex<()>>>>> =
        std::sync::OnceLock::new();
    let lock = {
        let mut locks = LOCKS.get_or_init(Default::default).lock().unwrap();
        locks.retain(|_, v| v.strong_count() > 0);
        let key = a.key();
        let lock = locks
            .get(&key)
            .and_then(std::sync::Weak::upgrade)
            .unwrap_or_else(|| Arc::new(Mutex::new(())));
        locks.insert(key, Arc::downgrade(&lock));
        lock
    };
    let _guard = lock.lock().unwrap();
    if crate::desktop::cancelled() {
        return Ok(());
    }
    let s = &a.action["settings"];
    let position = s["label_position"]
        .as_str()
        .filter(|p| ["top", "center", "bottom"].contains(p))
        .unwrap_or("center");
    if s["display_output"] == true && s["clear_output"] == true {
        overlay(shared, a, label(position, String::new(), 14));
    }
    let result = crate::desktop::shell_settings(s);
    shared
        .lock()
        .unwrap()
        .command_runs
        .insert(a.key(), Instant::now());
    let result = result?;
    if s["display_output"] == true {
        overlay(shared, a, label(position, result, 14));
    }
    Ok(())
}
struct Artwork {
    dir: PathBuf,
    paths: hashlink::LinkedHashMap<String, ArtworkEntry>,
}
struct ArtworkEntry {
    path: Option<PathBuf>,
    stamp: Option<(u64, std::time::SystemTime)>,
    checked: Instant,
}
impl Artwork {
    fn new() -> Result<Self> {
        let temp = tempfile::Builder::new()
            .prefix("deckard-artwork-")
            .tempdir()?;
        let dir = temp.keep();
        Ok(Self {
            dir,
            paths: Default::default(),
        })
    }
    fn get(&mut self, url: &str) -> Option<PathBuf> {
        if url.is_empty() {
            return None;
        }
        let source = if url.starts_with("file://") {
            reqwest::Url::parse(url)
                .ok()
                .and_then(|u| u.to_file_path().ok())
        } else if Path::new(url).is_absolute() {
            Some(PathBuf::from(url))
        } else {
            None
        };
        let stamp = source.and_then(|p| {
            fs::metadata(p)
                .ok()
                .and_then(|m| m.modified().ok().map(|t| (m.len(), t)))
        });
        if let Some(entry) = self.paths.get(url)
            && entry.stamp == stamp
            && (entry.path.is_some() || entry.checked.elapsed() < Duration::from_secs(5))
        {
            let path = entry.path.clone();
            self.paths.to_back(url);
            return path;
        }
        if let Some(old) = self.paths.remove(url).and_then(|entry| entry.path) {
            let _ = fs::remove_file(old);
        }
        while self.paths.len() >= 32 {
            if let Some((_, entry)) = self.paths.pop_front()
                && let Some(path) = entry.path
            {
                let _ = fs::remove_file(path);
            }
        }
        let result = self.fetch(url).ok();
        self.paths.insert(
            url.into(),
            ArtworkEntry {
                path: result.clone(),
                stamp,
                checked: Instant::now(),
            },
        );
        result
    }
    fn fetch(&self, url: &str) -> Result<PathBuf> {
        use sha2::{Digest, Sha256};
        let bytes = if url.starts_with("file://") {
            let parsed = reqwest::Url::parse(url)?;
            let source = parsed
                .to_file_path()
                .map_err(|_| anyhow::anyhow!("invalid artwork file URL"))?;
            anyhow::ensure!(
                fs::metadata(&source)?.len() <= 16 * 1024 * 1024,
                "artwork too large"
            );
            fs::read(source)?
        } else if Path::new(url).is_absolute() {
            anyhow::ensure!(
                fs::metadata(url)?.len() <= 16 * 1024 * 1024,
                "artwork too large"
            );
            fs::read(url)?
        } else {
            let parsed = reqwest::Url::parse(url)?;
            anyhow::ensure!(
                ["http", "https"].contains(&parsed.scheme()),
                "unsupported artwork scheme"
            );
            let response = reqwest::blocking::Client::builder()
                .timeout(Duration::from_secs(3))
                .build()?
                .get(parsed)
                .send()?
                .error_for_status()?;
            let mut bytes = Vec::new();
            response
                .take(16 * 1024 * 1024 + 1)
                .read_to_end(&mut bytes)?;
            anyhow::ensure!(bytes.len() <= 16 * 1024 * 1024, "artwork too large");
            bytes
        };
        // A changed local cover needs a new render identity even if its URL is unchanged.
        let mut identity = Sha256::new();
        identity.update(url.as_bytes());
        identity.update(&bytes);
        let path = self.dir.join(format!("{:x}.png", identity.finalize()));
        let mut reader =
            image::ImageReader::new(std::io::Cursor::new(bytes)).with_guessed_format()?;
        let mut limits = image::Limits::default();
        limits.max_image_width = Some(8192);
        limits.max_image_height = Some(8192);
        limits.max_alloc = Some(64 * 1024 * 1024);
        reader.limits(limits);
        let image = reader.decode()?;
        let image = if image.width() > 1200 || image.height() > 1200 {
            image.thumbnail(1200, 1200)
        } else {
            image
        };
        image.save(&path)?;
        Ok(path)
    }
}
impl Drop for Artwork {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}
fn media_loop(shared: &Shared, list: &Mutex<Arc<Vec<Action>>>) {
    let Ok(mut art) = Artwork::new() else {
        return;
    };
    let mut monitor = None;
    while !crate::desktop::cancelled() {
        if monitor.is_none() {
            monitor = crate::mpris::Monitor::connect().ok();
        }
        let players = match monitor.as_ref().map(crate::mpris::Monitor::snapshot) {
            Some(Ok(players)) => players,
            _ => {
                monitor = None;
                Vec::new()
            }
        };
        let actions = list.lock().unwrap().clone();
        for a in actions.iter() {
            let s = &a.action["settings"];
            let player = players
                .iter()
                .find(|p| p.matches(s["player"].as_str().unwrap_or("")));
            let kind = s["presentation"]
                .as_str()
                .or_else(|| s["method"].as_str())
                .unwrap_or("PlayPause");
            let kind = if a.family == "dials" && s["presentation"].is_null() {
                "MediaDial"
            } else {
                kind
            };
            let artwork = player.and_then(|p| art.get(&p.artwork));
            let mut value = json!({});
            if kind == "Thumbnail" {
                if artwork.is_none() {
                    let mut e = shared.lock().unwrap();
                    let before = e.background_overlays.len();
                    e.background_overlays
                        .retain(|_, (source, _, _)| source.key() != a.key());
                    if before != e.background_overlays.len() {
                        e.generation += 1;
                    }
                }
                let mode = s["size_mode"].as_str().unwrap_or("fill");
                let group = actions
                    .iter()
                    .filter(|other| {
                        other.family == "keys"
                            && other.serial == a.serial
                            && other.action["settings"]["method"] == "Thumbnail"
                    })
                    .collect::<Vec<_>>();
                let cfg = {
                    let e = shared.lock().unwrap();
                    e.devices.get(&a.serial).map(|d| {
                        (
                            d.kind,
                            e.docs.settings["devices"][&a.serial]["rotation"]
                                .as_u64()
                                .unwrap_or(0) as u16,
                        )
                    })
                };
                if let (Some(path), Some((kind, rotation))) = (artwork, cfg) {
                    let (rows, cols) = render::layout(kind, rotation);
                    let mut origin = (0, 0);
                    let mut extent = (cols as u32, rows as u32);
                    if let Some(n) = mode
                        .split_once('x')
                        .and_then(|(n, _)| n.parse::<u32>().ok())
                    {
                        origin = coordinates(&a.input);
                        extent = (n.clamp(1, 4), n.clamp(1, 4));
                    }
                    // Grid-sized actions also paint neighbouring inputs, matching their background scope.
                    for row in 0..rows as u32 {
                        for col in 0..cols as u32 {
                            if col >= origin.0
                                && row >= origin.1
                                && col < origin.0 + extent.0
                                && row < origin.1 + extent.1
                                && (mode.contains('x')
                                    || group.iter().any(|b| coordinates(&b.input) == (col, row)))
                            {
                                let mut target = a.clone();
                                target.family = "keys".into();
                                target.input = format!("{col}x{row}");
                                let v = json!({"visual":{"artwork":path,"crop":[col-origin.0,row-origin.1,extent.0,extent.1],"spacing":render::key_spacing(kind,rotation),"fit":if mode=="stretch"{"stretch"}else{"cover"}}});
                                background_overlay(shared, a, &target, v);
                            }
                        }
                    }
                }
                continue;
            }
            if let Some(player) = player {
                if kind == "Info" {
                    value = json!({"labels":{"top":{"text":player.title,"font-size":12},"center":{"text":s["seperator_text"].as_str().unwrap_or("--"),"font-size":12},"bottom":{"text":player.artist,"font-size":12}}});
                } else if kind == "MediaDial" {
                    value = json!({"labels":{"top":{"text":player.artist,"font-family":font_family(s["artist_font_desc"].as_str().unwrap_or("DejaVu Sans Book 18")),"font-size":s["artist_font_size"].as_u64().unwrap_or(18),"outline-width":s["artist_outline_size"].as_u64().unwrap_or(2)},"center":{"text":player.title,"font-family":font_family(s["song_font_desc"].as_str().unwrap_or("DejaVu Sans Bold 30")),"font-size":s["song_font_size"].as_u64().unwrap_or(30),"outline-width":s["song_outline_size"].as_u64().unwrap_or(2)},"bottom":{"text":format!("{} / {}",timecode(player.position),timecode(player.duration)),"font-size":14}},"visual":{"progress":if player.duration>0.0{player.position/player.duration}else{0.0},"darken":true}});
                } else {
                    if s["show_label"].as_bool().unwrap_or(true) {
                        value = label("bottom", player.title.clone(), 12);
                    }
                    value["visual"] = json!({"symbol":if kind=="PlayPause"&&player.status=="Playing"{"Pause"}else{kind},"active":player.status=="Playing"});
                }
                if (s["show_thumbnail"].as_bool().unwrap_or(true) || kind == "MediaDial")
                    && let Some(path) = artwork
                {
                    value["visual"]["artwork"] = json!(path);
                }
            } else {
                value = json!({"labels":{"top":{"text":""},"center":{"text":if kind=="MediaDial"{"No Media Playing"}else{""}},"bottom":{"text":""}},"visual":{"symbol":if kind=="Info"{""}else{kind},"active":false}});
                if let Some(path) = s["idle_icon"]
                    .as_str()
                    .filter(|p| !p.is_empty() && Path::new(p).is_file())
                {
                    value["media"] = json!({"path":path});
                }
            }
            overlay(shared, a, value);
        }
        let _ = crate::desktop::wait(Duration::from_millis(250));
    }
}
fn font_family(description: &str) -> String {
    description
        .split_whitespace()
        .filter(|s| s.parse::<u32>().is_err() && !["Bold", "Book", "Regular", "Italic"].contains(s))
        .collect::<Vec<_>>()
        .join(" ")
}
fn timecode(seconds: f64) -> String {
    let seconds = seconds.max(0.0) as u64;
    format!("{:02}:{:02}", seconds / 60, seconds % 60)
}
fn coordinates(input: &str) -> (u32, u32) {
    input
        .split_once('x')
        .map(|(x, y)| (x.parse().unwrap_or(0), y.parse().unwrap_or(0)))
        .unwrap_or((0, 0))
}
fn background_overlay(shared: &Shared, source: &Action, target: &Action, value: Value) {
    let mut e = shared.lock().unwrap();
    if !source.valid_in(&e) {
        return;
    }
    let key = format!("background:{}:{}", source.key(), target.input);
    if e.background_overlays
        .get(&key)
        .is_none_or(|(_, _, v)| v != &value)
    {
        e.background_overlays
            .insert(key, (source.clone(), target.input.clone(), value));
        e.generation += 1;
    }
}
fn obs_loop(shared: &Shared, list: &Mutex<Arc<Vec<Action>>>) {
    let mut client = None;
    let mut profile = Value::Null;
    let mut bandwidth: Option<(u64, Instant)> = None;
    let mut peaks = HashMap::<String, f64>::new();
    let mut previous_actions: Option<Arc<Vec<Action>>> = None;
    while !crate::desktop::cancelled() {
        let actions = list.lock().unwrap().clone();
        let Some(first) = actions.first() else {
            break;
        };
        if previous_actions
            .as_ref()
            .is_none_or(|old| !Arc::ptr_eq(old, &actions))
        {
            let keys = actions.iter().map(Action::key).collect::<HashSet<_>>();
            peaks.retain(|key, _| keys.contains(key));
            previous_actions = Some(actions.clone());
        }
        let next_profile = {
            let e = shared.lock().unwrap();
            e.obs_profile(
                first.action["settings"]["connection"]
                    .as_str()
                    .unwrap_or("default"),
            )
        };
        if next_profile != profile {
            client = None;
            profile = next_profile;
        }
        if client.is_none() {
            client = crate::obs::connect(&profile, 1023 | 65536).ok();
        }
        let mut cache = HashMap::new();
        let mut failed = false;
        for a in actions.iter() {
            let value = if let Some(c) = client.as_mut() {
                let mut settings = a.action["settings"].clone();
                if a.family == "dials"
                    && settings["presentation"].is_null()
                    && settings["data"]["inputName"].is_string()
                {
                    settings["presentation"] = json!("InputDial");
                }
                match obs_overlay(c, &settings, &mut cache, &mut bandwidth) {
                    Ok(mut value) => {
                        if settings["live_meter"] == true {
                            let raw = value["visual"]["meter-peak"].as_f64().unwrap_or(0.0);
                            let previous = peaks.entry(a.key()).or_default();
                            *previous = if raw >= *previous {
                                raw
                            } else {
                                *previous - 0.23 * (*previous - raw)
                            };
                            value["visual"]["progress"] = json!(if *previous <= 0.001 {
                                0.0
                            } else {
                                ((20.0 * previous.log10() + 60.0) / 60.0).clamp(0.0, 1.0)
                            });
                        }
                        value
                    }
                    Err(error) => {
                        let rejected = error
                            .downcast_ref::<crate::obs::RequestRejected>()
                            .is_some();
                        failed |= !rejected;
                        json!({"labels":{"center":{"text":if rejected {"Check selection"} else {"Offline"}}},"visual":{"symbol":"obs","active":false}})
                    }
                }
            } else {
                json!({"labels":{"center":{"text":"Offline"}},"visual":{"symbol":"obs","active":false}})
            };
            overlay(shared, a, value);
        }
        if failed {
            client = None;
        }
        if let Some(c) = client.as_mut()
            && c.poll().is_err()
        {
            client = None;
        }
        let meter = actions
            .iter()
            .any(|a| a.action["settings"]["live_meter"] == true);
        let _ = crate::desktop::wait(Duration::from_millis(if meter { 75 } else { 500 }));
    }
}
fn obs_overlay(
    client: &mut crate::obs::Client,
    s: &Value,
    cache: &mut HashMap<String, Value>,
    bandwidth: &mut Option<(u64, Instant)>,
) -> Result<Value> {
    fn query(
        c: &mut crate::obs::Client,
        cache: &mut HashMap<String, Value>,
        request: &str,
        data: Value,
    ) -> Result<Value> {
        let key = format!("{request}{data}");
        if let Some(value) = cache.get(&key) {
            return Ok(value.clone());
        }
        let value = c.readout(request, data)?;
        cache.insert(key, value.clone());
        Ok(value)
    }
    let operation = s["operation"].as_str().unwrap_or("ToggleRecord");
    let d = &s["data"];
    let kind = s["presentation"].as_str().unwrap_or(operation);
    let mut value =
        json!({"labels":{"bottom":{"text":""}},"visual":{"symbol":"obs","active":false}});
    let active;
    let filename;
    match kind {
        "ToggleStream"
        | "ToggleRecord"
        | "RecPlayPause"
        | "ToggleReplayBuffer"
        | "ToggleVirtualCamera"
        | "ToggleVirtualCam"
        | "ToggleStudioMode" => {
            let (request, prefix, on, off) = match kind {
                "ToggleStream" => ("GetStreamStatus", "stream", "active", "inactive"),
                "ToggleRecord" | "RecPlayPause" => {
                    ("GetRecordStatus", "record", "active", "inactive")
                }
                "ToggleReplayBuffer" => (
                    "GetReplayBufferStatus",
                    "replay_buffer",
                    "enabled",
                    "disabled",
                ),
                "ToggleStudioMode" => {
                    ("GetStudioModeEnabled", "studio_mode", "enabled", "disabled")
                }
                _ => (
                    "GetVirtualCamStatus",
                    "virtual_camera",
                    "enabled",
                    "disabled",
                ),
            };
            let status = query(client, cache, request, json!({}))?;
            active = status["outputActive"]
                .as_bool()
                .or_else(|| status["studioModeEnabled"].as_bool())
                .unwrap_or(false);
            filename = if kind == "ToggleStream" && status["outputReconnecting"] == true {
                "stream_reconnecting.png".into()
            } else if kind == "RecPlayPause" && active {
                if status["outputPaused"] == true {
                    "record_resume.png".into()
                } else {
                    "record_pause.png".into()
                }
            } else if kind == "ToggleRecord" && status["outputPaused"] == true {
                "record_resume.png".into()
            } else {
                format!("{prefix}_{}.png", if active { on } else { off })
            };
            value["visual"]["symbol"] = json!(if kind == "RecPlayPause" {
                if status["outputPaused"] == true {
                    "Play"
                } else {
                    "Pause"
                }
            } else {
                prefix
            });
            if kind == "RecPlayPause" {
                value["labels"]["bottom"]["text"] = json!(if !active {
                    ""
                } else if status["outputPaused"] == true {
                    "Resume"
                } else {
                    "Pause"
                });
            } else if active && ["ToggleStream", "ToggleRecord"].contains(&kind) {
                value["labels"]["bottom"]["text"] = json!(
                    status["outputTimecode"]
                        .as_str()
                        .map(|s| s.split('.').next().unwrap_or(s).to_owned())
                        .unwrap_or_else(|| timecode(
                            status["outputDuration"].as_f64().unwrap_or(0.0) / 1000.0
                        ))
                );
            }
        }
        "ToggleInputMute" | "SetInputMute" | "InputDial" => {
            let name = d["inputName"]
                .as_str()
                .or_else(|| s["input"].as_str())
                .context("OBS input missing")?;
            let mute = query(client, cache, "GetInputMute", json!({"inputName":name}))?;
            active = mute["inputMuted"] != true;
            filename = if active {
                "input_unmuted.png"
            } else {
                "input_muted.png"
            }
            .into();
            value["visual"]["symbol"] = json!(if active { "audio" } else { "mute" });
            if kind == "InputDial" {
                let v = query(client, cache, "GetInputVolume", json!({"inputName":name}))?;
                let db = v["inputVolumeDb"].as_f64().unwrap_or(-100.0);
                let percent = if db < -100.0 {
                    0.0
                } else {
                    (1.5_f64.powf(db / 10.0) * 100.0).floor().clamp(0.0, 100.0)
                };
                value["labels"] = json!({"top":{"text":name,"font-size":14},"bottom":{"text":if active{format!("{percent:.0}%")}else{"Muted".into()},"font-size":16}});
                let meters = &client
                    .events
                    .get("InputVolumeMeters")
                    .unwrap_or(&Value::Null)["inputs"];
                let peak = meters
                    .as_array()
                    .and_then(|v| v.iter().find(|v| v["inputName"] == name))
                    .and_then(|v| v["inputLevelsMul"].as_array())
                    .map(|channels| {
                        channels
                            .iter()
                            .filter_map(|c| {
                                c[1].as_f64()
                                    .or_else(|| c[0].as_f64())
                                    .or_else(|| c.as_f64())
                            })
                            .fold(0.0_f64, f64::max)
                    })
                    .unwrap_or(0.0);
                let peak = if client
                    .event_received
                    .get("InputVolumeMeters")
                    .is_some_and(|time| time.elapsed() < Duration::from_millis(500))
                {
                    peak
                } else {
                    0.0
                };
                value["visual"]["meter-peak"] = json!(peak);
                value["visual"]["progress"] = json!(if s["live_meter"] == true {
                    if peak <= 0.001 {
                        0.0
                    } else {
                        ((20.0 * peak.log10() + 60.0) / 60.0).clamp(0.0, 1.0)
                    }
                } else {
                    percent / 100.0
                });
                value["visual"]["bar-color"] = s
                    .get("bar_color")
                    .cloned()
                    .unwrap_or(json!([66, 133, 244, 255]));
            }
        }
        "SwitchScene" | "SetCurrentProgramScene" => {
            let status = query(client, cache, "GetCurrentProgramScene", json!({}))?;
            active = status["currentProgramSceneName"] == d["sceneName"];
            filename = if active {
                "scene_active.png"
            } else {
                "scene_inactive.png"
            }
            .into();
            value["visual"]["symbol"] = json!("scene");
        }
        "ToggleSceneItemEnabled" | "SetSceneItemEnabled" => {
            let id = query(
                client,
                cache,
                "GetSceneItemId",
                json!({"sceneName":d["sceneName"],"sourceName":d["sourceName"]}),
            )?["sceneItemId"]
                .clone();
            let status = query(
                client,
                cache,
                "GetSceneItemEnabled",
                json!({"sceneName":d["sceneName"],"sceneItemId":id}),
            )?;
            active = status["sceneItemEnabled"] == true;
            filename = if active {
                "scene_item_enabled.png"
            } else {
                "scene_item_disabled.png"
            }
            .into();
            value["visual"]["symbol"] = json!("scene");
        }
        "ToggleSceneFilter" | "SetSceneFilter" => {
            let status = query(
                client,
                cache,
                "GetSourceFilter",
                json!({"sourceName":d["sourceName"],"filterName":d["filterName"]}),
            )?;
            active = status["filterEnabled"] == true;
            filename = if active {
                "scene_item_enabled.png"
            } else {
                "scene_item_disabled.png"
            }
            .into();
            value["visual"]["symbol"] = json!("filter");
        }
        "OBSStats" | "GetStats" => {
            let stats = query(client, cache, "GetStats", json!({}))?;
            let stream = query(client, cache, "GetStreamStatus", json!({}))?;
            let video = query(client, cache, "GetVideoSettings", json!({}))?;
            let current = stream["outputBytes"].as_u64().unwrap_or(0);
            let now = Instant::now();
            let cached_rate = cache.get("native:stream-rate").and_then(Value::as_f64);
            let rate = cached_rate.unwrap_or_else(|| {
                bandwidth
                    .map(|(bytes, time)| {
                        current.saturating_sub(bytes) as f64 * 8.0
                            / (now.duration_since(time).as_secs_f64().max(0.001) * 1000.0)
                    })
                    .unwrap_or(0.0)
            });
            if cached_rate.is_none() {
                *bandwidth = Some((current, now));
                cache.insert("native:stream-rate".into(), json!(rate));
            }
            let stream_text = if stream["outputReconnecting"] == true {
                "Reconn...".into()
            } else if stream["outputActive"] == true {
                if rate > 1000.0 {
                    format!("Live: {:.1}M", rate / 1000.0)
                } else {
                    format!("Live: {rate:.0}k")
                }
            } else {
                "Stream Off".into()
            };
            let texts = HashMap::from([
                (
                    "CPU",
                    format!("CPU: {:.1}%", stats["cpuUsage"].as_f64().unwrap_or(0.0)),
                ),
                (
                    "FPS",
                    format!(
                        "FPS: {:.0}/{:.0}",
                        stats["activeFps"].as_f64().unwrap_or(0.0),
                        video["fpsNumerator"].as_f64().unwrap_or(60.0)
                            / video["fpsDenominator"].as_f64().unwrap_or(1.0).max(1.0)
                    ),
                ),
                ("Stream", stream_text),
            ]);
            let n = s["stats_count"]
                .as_str()
                .and_then(|v| v.parse::<usize>().ok())
                .or_else(|| s["stats_count"].as_u64().map(|v| v as usize))
                .unwrap_or(3)
                .clamp(1, 3);
            value["labels"] = json!({"top":{"text":""},"center":{"text":""},"bottom":{"text":""}});
            let positions = match n {
                1 => vec!["center"],
                2 => vec!["top", "bottom"],
                _ => vec!["top", "center", "bottom"],
            };
            for (i, position) in positions.iter().enumerate() {
                let default = ["CPU", "FPS", "Stream"][i];
                let selection = s[format!("stat_{}", i + 1)].as_str().unwrap_or(default);
                value["labels"][position]["text"] =
                    json!(texts.get(selection).cloned().unwrap_or_default());
            }
            active = true;
            filename = "stats.png".into();
            value["visual"]["symbol"] = json!("");
        }
        _ => {
            active = true;
            filename = match kind {
                "SaveReplayBuffer" => "replay_buffer_save.png",
                "TriggerTransition" => "transition.png",
                _ => "scene.png",
            }
            .into();
            value["visual"]["symbol"] = json!(kind);
        }
    }
    value["visual"]["active"] = json!(active);
    if let Some(path) = s[format!("custom_icon_{filename}")]
        .as_str()
        .filter(|p| Path::new(p).is_file())
    {
        value["media"] = json!({"path":path});
        value.as_object_mut().unwrap().remove("visual");
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn overlays_respect_explicit_user_properties_and_action_owners() {
        let mut state = json!({"image-control-action":2,"label-control-actions":[0,2,2],"labels":{"center":{"text":"My label","font-size":30}},"media":{"path":"/my/icon.png"}});
        let overlay = json!({"labels":{"top":{"text":"Wrong owner"},"center":{"text":"Track","color":[90,180,245,255]},"bottom":{"text":"Artist"}},"media":{"path":"/album.png"},"visual":{"symbol":"Play"}});
        apply(&mut state, 2, &overlay);
        assert!(state["labels"]["top"].is_null());
        assert_eq!(state["labels"]["center"]["text"], "My label");
        assert_eq!(state["labels"]["center"]["font-size"], 30);
        assert_eq!(
            state["labels"]["center"]["color"],
            json!([90, 180, 245, 255])
        );
        assert_eq!(state["labels"]["bottom"]["text"], "Artist");
        assert_eq!(state["media"]["path"], "/my/icon.png");
    }
    #[test]
    fn live_discovery_ignores_frames_but_invalidates_on_inputs_and_documents() {
        let tmp = tempfile::tempdir().unwrap();
        let shared = crate::engine::Engine::open(tmp.path().into()).unwrap();
        let runtime = crate::engine::Runtime::start(
            shared.clone(),
            vec![elgato_streamdeck::info::Kind::Plus],
            false,
        )
        .unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        while !shared.lock().unwrap().devices.contains_key("FAKE-PLUS-0") {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(10));
        }
        let mut e = shared.lock().unwrap();
        let initial = signature(&e);
        e.generation += 1;
        e.devices.get_mut("FAKE-PLUS-0").unwrap().rendered_frames += 1;
        assert_eq!(
            signature(&e),
            initial,
            "live repaint updates must not rediscover or clone actions"
        );
        e.devices
            .get_mut("FAKE-PLUS-0")
            .unwrap()
            .states
            .insert("keys/0x0".into(), 1);
        let states = signature(&e);
        assert_ne!(states, initial);
        e.docs.revision += 1;
        assert_ne!(signature(&e), states);
        let before = signature(&e);
        e.locked = true;
        assert_ne!(signature(&e), before);
        drop(e);
        drop(runtime);
    }
    #[test]
    fn artwork_is_decoded_bounded_and_reused() {
        let tmp = tempfile::tempdir().unwrap();
        let file = tmp.path().join("cover with space.png");
        image::RgbaImage::from_pixel(4, 4, image::Rgba([25, 100, 200, 255]))
            .save(&file)
            .unwrap();
        let mut cache = Artwork::new().unwrap();
        let url = reqwest::Url::from_file_path(&file).unwrap().to_string();
        let a = cache.get(&url).unwrap();
        assert_eq!(
            image::open(&a).unwrap().width(),
            4,
            "small covers must not be enlarged"
        );
        assert_eq!(
            image::open(&a).unwrap().to_rgba8().get_pixel(0, 0).0,
            [25, 100, 200, 255]
        );
        assert_eq!(cache.get(&url), Some(a.clone()));
        // Players can replace a local cover while retaining the same MPRIS URL.
        image::RgbaImage::from_pixel(5, 5, image::Rgba([200, 10, 25, 255]))
            .save(&file)
            .unwrap();
        let replacement = cache.get(&url).unwrap();
        assert_ne!(replacement, a);
        assert!(!a.exists());
        assert_eq!(
            image::open(&replacement)
                .unwrap()
                .to_rgba8()
                .get_pixel(0, 0)
                .0,
            [200, 10, 25, 255]
        );
        let missing = tmp.path().join("late-cover.png");
        let late_url = reqwest::Url::from_file_path(&missing).unwrap().to_string();
        assert!(cache.get(&late_url).is_none());
        fs::copy(&file, missing).unwrap();
        assert!(cache.get(&late_url).is_some());
        for index in 0..40 {
            let other = tmp.path().join(format!("cover-{index}.png"));
            fs::copy(&file, &other).unwrap();
            assert!(cache.get(other.to_str().unwrap()).is_some());
            cache.get(&url).unwrap();
        }
        assert_eq!(cache.paths.len(), 32);
        assert_eq!(fs::read_dir(&cache.dir).unwrap().count(), 32);
        assert_eq!(cache.get(&url), Some(replacement));
        assert!(cache.get("data:bad").is_none());
    }
}
