use anyhow::Result;
use deckard_core::{
    engine::{InputEvent, Shared},
    model, plugin, render,
    store::{Entry, Network},
};
use eframe::egui::{self, Color32, RichText, Vec2};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, SyncSender},
    },
};

pub fn run(shared: Shared, events: SyncSender<InputEvent>, signal: Arc<AtomicBool>) -> Result<()> {
    let mut wgpu_options = eframe::egui_wgpu::WgpuConfiguration::default();
    if let eframe::egui_wgpu::WgpuSetup::CreateNew(setup) = &mut wgpu_options.wgpu_setup {
        setup.instance_descriptor.backends =
            eframe::wgpu::Backends::from_env().unwrap_or(eframe::wgpu::Backends::VULKAN);
        let original = setup.device_descriptor.clone();
        setup.device_descriptor = Arc::new(move |adapter| {
            let mut descriptor = original(adapter);
            descriptor.memory_hints = eframe::wgpu::MemoryHints::MemoryUsage;
            descriptor
        });
    }
    let options = eframe::NativeOptions {
        wgpu_options,
        renderer: if std::env::var("DECKARD_RENDERER").as_deref() == Ok("glow") {
            eframe::Renderer::Glow
        } else {
            eframe::Renderer::Wgpu
        },
        // Repaints follow input/frame changes. Wayland schedules presentation;
        // an additional EGL swap wait can busy-spin inside the GPU driver.
        vsync: std::env::var_os("WAYLAND_DISPLAY").is_none(),
        viewport: egui::ViewportBuilder::default()
            .with_title("Deckard")
            .with_app_id("io.github.nazbert.Deckard")
            .with_inner_size([1180.0, 780.0])
            .with_min_inner_size([850.0, 600.0]),
        ..Default::default()
    };
    eframe::run_native(
        "Deckard",
        options,
        Box::new(move |cc| {
            if let Some(state) = &cc.wgpu_render_state {
                let info = state.adapter.get_info();
                eprintln!("Deckard graphics: {} ({:?})", info.name, info.backend);
            } else {
                eprintln!("Deckard graphics: OpenGL");
            }
            cc.egui_ctx.set_visuals(egui::Visuals::dark());
            Ok(Box::new(App::new(
                shared,
                events,
                signal,
                cc.egui_ctx.clone(),
            )))
        }),
    )
    .map_err(|error| match error {
        eframe::Error::Wgpu(error) => GraphicsUnavailable(error.to_string()).into(),
        other => anyhow::anyhow!(other.to_string()),
    })
}
#[derive(Debug)]
pub struct GraphicsUnavailable(pub String);
impl std::fmt::Display for GraphicsUnavailable {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for GraphicsUnavailable {}
enum Task {
    Catalog(Vec<Entry>),
    Message(String),
    Ai(Value),
    Choices(Value),
    Players(Vec<String>),
}
type ActionDefinitions = Vec<(String, String, Vec<plugin::Field>)>;
struct App {
    definitions: Arc<ActionDefinitions>,
    definitions_revision: u64,
    repaint: egui::Context,
    watch_stop: Arc<AtomicBool>,
    watch_thread: Option<std::thread::JoinHandle<()>>,
    shared: Shared,
    events: SyncSender<InputEvent>,
    signal: Arc<AtomicBool>,
    serial: String,
    page: String,
    search: String,
    family: String,
    input: String,
    state: usize,
    sticky: bool,
    draft: Value,
    raw: String,
    selected: String,
    tab: usize,
    page_name: String,
    path: String,
    notice: String,
    migration_report: Option<Value>,
    textures: HashMap<u8, (u64, egui::TextureHandle)>,
    key_atlas: Option<egui::TextureHandle>,
    key_uvs: HashMap<u8, egui::Rect>,
    key_signature: Vec<(u8, u64)>,
    settings: String,
    settings_revision: u64,
    obs_id: String,
    obs_profile: Value,
    obs_choices: Value,
    media_players: Vec<String>,
    catalog_url: String,
    catalog: Vec<Entry>,
    store_tab: String,
    store_branch: String,
    busy: bool,
    send: SyncSender<Task>,
    receive: Receiver<Task>,
    ai_prompt: String,
    ai_key: String,
    ai_endpoint: String,
    ai_model: String,
    ai_proposal: Option<Value>,
    allow_commands: bool,
    started: std::time::Instant,
    capture_sent: bool,
    hidden: bool,
}
impl App {
    fn new(
        shared: Shared,
        events: SyncSender<InputEvent>,
        signal: Arc<AtomicBool>,
        repaint: egui::Context,
    ) -> Self {
        let engine = shared.lock().unwrap();
        let page = engine
            .docs
            .pages
            .keys()
            .next()
            .cloned()
            .unwrap_or("Main".into());
        let settings = serde_json::to_string_pretty(&engine.docs.settings).unwrap_or_default();
        let settings_revision = engine.docs.revision;
        let definitions = Arc::new(action_definitions(&engine.plugins));
        let definitions_revision = engine.plugin_revision;
        let obs_id = engine.docs.settings["obs"]["default_connection"]
            .as_str()
            .unwrap_or("default")
            .to_owned();
        let obs_profile = engine.obs_profile(&obs_id);
        let obs_profile = if obs_profile.is_object() {
            obs_profile
        } else {
            json!({"name":"Default","host":"localhost","port":4455,"password":"","tls":false})
        };
        drop(engine);
        let (send, receive) = mpsc::sync_channel(4);
        let watch_stop = Arc::new(AtomicBool::new(false));
        let stop = watch_stop.clone();
        let watched = shared.clone();
        let context = repaint.clone();
        let watch_signal = signal.clone();
        let watch_thread = std::thread::spawn(move || {
            let mut previous = None;
            while !stop.load(Ordering::Relaxed) {
                let signature = {
                    let engine = watched.lock().unwrap();
                    let mut frames: Vec<_> = engine
                        .devices
                        .iter()
                        .map(|(id, d)| {
                            (
                                id.clone(),
                                d.frame.as_ref().map(|frame| {
                                    frame
                                        .tiles
                                        .iter()
                                        .chain(frame.strip.iter())
                                        .map(|tile| (tile.key, tile.identity))
                                        .collect::<Vec<_>>()
                                }),
                            )
                        })
                        .collect();
                    frames.sort_unstable();
                    (
                        engine.generation,
                        engine.docs.revision,
                        engine.errors.back().cloned(),
                        engine.quit,
                        engine.show_window,
                        watch_signal.load(Ordering::Relaxed),
                        frames,
                    )
                };
                if previous.as_ref() != Some(&signature) {
                    context.request_repaint();
                    previous = Some(signature);
                }
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
        });
        Self {
            definitions,
            definitions_revision,
            repaint,
            watch_stop,
            watch_thread: Some(watch_thread),
            shared,
            events,
            signal,
            serial: String::new(),
            page,
            search: String::new(),
            family: "keys".into(),
            input: "0x0".into(),
            state: 0,
            sticky: false,
            draft: json!({}),
            raw: "{}".into(),
            selected: String::new(),
            tab: 0,
            page_name: String::new(),
            path: String::new(),
            notice: String::new(),
            migration_report: None,
            textures: HashMap::new(),
            key_atlas: None,
            key_uvs: HashMap::new(),
            key_signature: Vec::new(),
            settings,
            settings_revision,
            obs_id,
            obs_profile,
            obs_choices: json!({}),
            media_players: Vec::new(),
            catalog_url: format!(
                "https://github.com/pauljones0/Deckard/releases/latest/download/native-store-{}.json",
                std::env::consts::ARCH
            ),
            catalog: vec![],
            store_tab: "plugin".into(),
            store_branch: String::new(),
            busy: false,
            send,
            receive,
            ai_prompt: String::new(),
            ai_key: String::new(),
            ai_endpoint: "https://api.openai.com/v1/chat/completions".into(),
            ai_model: String::new(),
            ai_proposal: None,
            allow_commands: false,
            started: std::time::Instant::now(),
            capture_sent: false,
            hidden: false,
        }
    }
    fn command(&mut self, method: &str, params: Value) {
        let result = self
            .shared
            .lock()
            .unwrap()
            .command(&json!({"method":method,"params":params}));
        match result {
            Ok(_) => self.notice = "Saved".into(),
            Err(error) => self.notice = error.to_string(),
        }
    }
    fn select(&mut self) {
        let key = format!(
            "{}/{}/{}/{}/{}/{}",
            self.serial, self.page, self.family, self.input, self.state, self.sticky
        );
        if key != self.selected {
            self.selected = key;
            let engine = self.shared.lock().unwrap();
            let page = if self.sticky {
                engine
                    .docs
                    .sticky(&self.serial)
                    .unwrap_or_else(|_| json!({}))
            } else {
                engine
                    .docs
                    .pages
                    .get(&self.page)
                    .cloned()
                    .unwrap_or_default()
            };
            let mut draft = model::state(&page, &self.family, &self.input, self.state).clone();
            if !draft.is_object() {
                draft = json!({})
            }
            self.draft = draft;
            self.raw = serde_json::to_string_pretty(&self.draft).unwrap_or_default();
        }
    }
    fn save(&mut self) {
        if !self.draft.is_object() {
            self.notice = "State must be a JSON object".into();
            return;
        }
        if self.sticky {
            let result = (|| -> Result<()> {
                let mut engine = self.shared.lock().unwrap();
                let mut page = engine.docs.sticky(&self.serial)?;
                *model::state_mut(&mut page, &self.family, &self.input, self.state)? =
                    self.draft.clone();
                engine.docs.put_sticky(&self.serial, &page)?;
                Ok(())
            })();
            self.notice = result
                .map(|_| "Sticky input saved".into())
                .unwrap_or_else(|e| e.to_string());
        } else {
            self.command("set-state",json!({"page":self.page,"family":self.family,"input":self.input,"state":self.state,"document":self.draft}));
        }
        self.raw = serde_json::to_string_pretty(&self.draft).unwrap_or_default();
    }
    fn background_task(&mut self, work: impl FnOnce() -> Result<Task> + Send + 'static) {
        if self.busy {
            return;
        }
        self.busy = true;
        let send = self.send.clone();
        let context = self.repaint.clone();
        std::thread::spawn(move || {
            let result = work().unwrap_or_else(|e| Task::Message(e.to_string()));
            let _ = send.send(result);
            context.request_repaint();
        });
    }
    fn upload_frame(&mut self, ctx: &egui::Context, frame: &render::Frame, cols: usize) {
        let signature: Vec<_> = frame.tiles.iter().map(|t| (t.key, t.identity)).collect();
        if self.key_signature != signature
            && let Some(first) = frame.tiles.first()
        {
            let cols = cols.max(1);
            let rows = frame.tiles.len().div_ceil(cols);
            let tw = first.width as usize;
            let th = first.height as usize;
            // A gutter keeps linear filtering from sampling neighbouring keys.
            let aw = cols * (tw + 2);
            let ah = rows * (th + 2);
            let mut atlas = egui::ColorImage::filled([aw, ah], Color32::TRANSPARENT);
            self.key_uvs.clear();
            for (index, tile) in frame.tiles.iter().enumerate() {
                let x = (index % cols) * (tw + 2) + 1;
                let y = (index / cols) * (th + 2) + 1;
                for row in 0..th {
                    let source = &tile.rgb[row * tw * 3..(row + 1) * tw * 3];
                    for (rgb, pixel) in source
                        .as_chunks::<3>()
                        .0
                        .iter()
                        .zip(&mut atlas.pixels[(y + row) * aw + x..(y + row) * aw + x + tw])
                    {
                        *pixel = Color32::from_rgb(rgb[0], rgb[1], rgb[2]);
                    }
                    atlas.pixels[(y + row) * aw + x - 1] = atlas.pixels[(y + row) * aw + x];
                    atlas.pixels[(y + row) * aw + x + tw] =
                        atlas.pixels[(y + row) * aw + x + tw - 1];
                }
                for col in x - 1..=x + tw {
                    atlas.pixels[(y - 1) * aw + col] = atlas.pixels[y * aw + col];
                    atlas.pixels[(y + th) * aw + col] = atlas.pixels[(y + th - 1) * aw + col];
                }
                self.key_uvs.insert(
                    tile.key,
                    egui::Rect::from_min_max(
                        egui::pos2(x as f32 / aw as f32, y as f32 / ah as f32),
                        egui::pos2((x + tw) as f32 / aw as f32, (y + th) as f32 / ah as f32),
                    ),
                );
            }
            if let Some(texture) = self.key_atlas.as_mut() {
                texture.set(atlas, egui::TextureOptions::LINEAR);
            } else {
                self.key_atlas =
                    Some(ctx.load_texture("deck-keys", atlas, egui::TextureOptions::LINEAR));
            }
            self.key_signature = signature;
        }
        if let Some(tile) = &frame.strip
            && self
                .textures
                .get(&tile.key)
                .is_none_or(|(id, _)| *id != tile.identity)
        {
            let image =
                egui::ColorImage::from_rgb([tile.width as usize, tile.height as usize], &tile.rgb);
            if let Some((id, texture)) = self.textures.get_mut(&tile.key) {
                texture.set(image, egui::TextureOptions::LINEAR);
                *id = tile.identity;
            } else {
                self.textures.insert(
                    tile.key,
                    (
                        tile.identity,
                        ctx.load_texture("deck-strip", image, egui::TextureOptions::LINEAR),
                    ),
                );
            }
        }
    }
    fn editor(&mut self, ctx: &egui::Context, ui: &mut egui::Ui) {
        let devices = {
            let engine = self.shared.lock().unwrap();
            if self.definitions_revision != engine.plugin_revision {
                self.definitions = Arc::new(action_definitions(&engine.plugins));
                self.definitions_revision = engine.plugin_revision;
            }
            engine.devices.values().cloned().collect::<Vec<_>>()
        };
        let definitions = self.definitions.clone();
        if let Some(device) = devices.iter().find(|d| d.serial == self.serial)
            && self.page != device.page
        {
            self.page = device.page.clone();
            self.selected.clear();
        }
        if self.serial.is_empty()
            && let Some(device) = devices.first()
        {
            self.serial = device.serial.clone();
            self.page = device.page.clone();
        }
        ui.horizontal(|ui| {
            ui.label("Device");
            egui::ComboBox::from_id_salt("device")
                .selected_text(if self.serial.is_empty() {
                    "No device connected"
                } else {
                    &self.serial
                })
                .show_ui(ui, |ui| {
                    for device in &devices {
                        if ui
                            .selectable_value(
                                &mut self.serial,
                                device.serial.clone(),
                                format!("{:?}  {}", device.kind, device.serial),
                            )
                            .clicked()
                        {
                            self.page = device.page.clone();
                            self.textures.clear();
                            self.key_signature.clear();
                        }
                    }
                });
            if let Some(device) = devices.iter().find(|d| d.serial == self.serial) {
                let mut brightness = device.brightness;
                if ui
                    .add(egui::Slider::new(&mut brightness, 0..=100).text("Brightness"))
                    .changed()
                {
                    self.command(
                        "set-brightness",
                        json!({"serial":self.serial,"value":brightness}),
                    );
                }
                if ui
                    .button(if device.sleeping { "Wake" } else { "Sleep" })
                    .clicked()
                {
                    self.command(
                        if device.sleeping { "wake" } else { "sleep" },
                        json!({"serial":self.serial}),
                    );
                }
                ui.label(if device.connected {
                    "Connected"
                } else {
                    "Reconnecting…"
                });
            }
        });
        ui.separator();
        ui.columns(2,|columns|{
            let left=&mut columns[0];
            left.heading(&self.page);
            left.add(egui::TextEdit::singleline(&mut self.search).hint_text("Search pages"));
            let pages=self.shared.lock().unwrap().docs.pages.keys().filter(|name|name.to_lowercase().contains(&self.search.to_lowercase())).cloned().collect::<Vec<_>>();
            egui::ComboBox::from_id_salt("pages").selected_text(&self.page).show_ui(left,|ui|{for page in pages{if ui.selectable_value(&mut self.page,page.clone(),&page).clicked()&&!self.serial.is_empty(){self.command("change-page",json!({"serial":self.serial,"page":self.page}));}}});
            left.add_space(20.0);
            let device=devices.iter().find(|d|d.serial==self.serial);let kind=device.map(|d|d.kind).unwrap_or(deckard_core::engine::fake_kind("plus").unwrap());let rotation=self.shared.lock().unwrap().docs.settings["devices"][&self.serial]["rotation"].as_u64().unwrap_or(0)as u16;let(rows,cols)=render::layout(kind,rotation);
            if let Some(frame)=device.and_then(|d|d.frame.as_ref()){self.upload_frame(ctx, frame, cols as usize);}
            let size=((left.available_width()-f32::from(cols)*8.0)/f32::from(cols)).clamp(16.0,100.0);
            egui::Grid::new("key-grid").spacing([8.0,8.0]).show(left,|ui|{for y in 0..rows{for x in 0..cols{let input=format!("{x}x{y}");let physical=(0..kind.key_count()).find(|p|render::logical_input(kind,*p,rotation)==input).unwrap_or(0);let selected=self.family=="keys"&&self.input==input;let clicked=if let Some((texture, uv))=self.key_atlas.as_ref().zip(self.key_uvs.get(&physical)){ui.add(egui::Button::image(egui::Image::new((texture.id(),Vec2::splat(size))).uv(*uv)).selected(selected)).clicked()}else{ui.add_sized([size,size],egui::Button::new(&input).selected(selected)).clicked()};
if clicked{self.family="keys".into();self.input=input;self.state=0;}}ui.end_row();}});
            if let Some((_,texture))=self.textures.get(&255){left.add(egui::Image::new((texture.id(),Vec2::new(left.available_width(),70.0))));}
            left.horizontal_wrapped(|ui|{for i in 0..kind.encoder_count(){if ui.selectable_label(self.family=="dials"&&self.input==i.to_string(),format!("Dial {}",i+1)).clicked(){self.family="dials".into();self.input=i.to_string();self.state=0;}}if kind.lcd_strip_size().is_some()&&kind.encoder_count()>0&&ui.button("Touchscreen gestures").clicked(){self.family="touchscreens".into();self.input="0".into();self.state=0;}
if format!("{kind:?}")=="Neo"{if ui.button("Infobar").clicked(){self.family="infobar".into();self.input="0".into();self.state=0;}for i in 0..2{if ui.button(format!("Touch key {}",i+1)).clicked(){self.family="keys".into();self.input=format!("touch-{i}");self.state=0;}}}});
            left.add_space(12.0);
            if left.button("Test selected input").clicked(){let _=self.events.try_send(InputEvent{serial:self.serial.clone(),family:self.family.clone(),input:self.input.clone(),event:"press".into(),value:1});let _=self.events.try_send(InputEvent{serial:self.serial.clone(),family:self.family.clone(),input:self.input.clone(),event:"release".into(),value:0});}
            if device.is_none(){left.label("Connect a Stream Deck, or start with --fake-deck-model plus to preview without hardware.");}
            left.collapsing("Wallpaper",|ui| {
                let page=self.shared.lock().unwrap().docs.pages.get(&self.page).cloned().unwrap_or_default();
                let id = egui::Id::new(("wallpaper-path", &self.page));
                let current = page["background"]["media-path"].as_str().unwrap_or("");
                let mut path = ctx.data_mut(|d| d.get_temp::<String>(id)).unwrap_or_else(|| current.into());
                ui.label("Image, GIF or video path");
                ui.text_edit_singleline(&mut path);
                let mut apply = ui.button("Apply wallpaper").clicked();
                if ui.button("Choose file…").clicked() && let Some(file) = rfd::FileDialog::new().pick_file() { path = file.to_string_lossy().into_owned(); apply = true; }
                if apply { let mut page = page.clone(); set_nested(&mut page, &["background", "media-path"], json!(path)); set_nested(&mut page, &["background", "show"], json!(true)); set_nested(&mut page, &["background", "overwrite"], json!(true)); self.command("put-page", json!({"page":self.page,"document":page})); }
                ctx.data_mut(|d| d.insert_temp(id, path));
                ui.label("Slideshow, viewport and screensaver options are available in Page JSON.");
            });
            let right=&mut columns[1];
            right.heading(format!("{} {}",self.family,self.input));
            right.horizontal(|ui|{ui.label("State");ui.add(egui::DragValue::new(&mut self.state).range(0..=127));
if ui.button("Add state").clicked(){self.command("add-state",json!({"page":self.page,"family":self.family,"input":self.input}));}
if ui.button("Remove state").clicked(){self.command("remove-state",json!({"page":self.page,"family":self.family,"input":self.input,"state":self.state}));self.selected.clear();}});
            if !self.serial.is_empty(){right.checkbox(&mut self.sticky,"Edit sticky input across all pages");
let engine = self.shared.lock().unwrap();
let page = engine.docs.pages.get(&self.page).unwrap_or(&Value::Null);
if !self.sticky && !std::ptr::eq(render::effective_input_from(page, engine.docs.sticky_ref(&self.serial), &self.family, &self.input), &page[&self.family][&self.input]) {
right.colored_label(Color32::YELLOW,"This input is controlled by sticky settings. Enable the sticky editor to change it.");}}
            self.select();let mut changed=false;
            for(position,title)in[("top","Top label"),("center","Center label"),("bottom","Bottom label")]{let mut value=self.draft["labels"][position]["text"].as_str().unwrap_or("").to_owned();right.horizontal(|ui|{ui.label(title);
if ui.text_edit_singleline(&mut value).changed(){set_nested(&mut self.draft,&["labels",position,"text"],json!(value));changed=true;}});}
            let mut path=self.draft["media"]["path"].as_str().unwrap_or("").to_owned();right.label("Icon / animation path");
if right.button("Choose file…").clicked()&& let Some(file)=rfd::FileDialog::new().add_filter("Images and video",&["png","jpg","jpeg","webp","svg","gif","mp4","mkv","webm"]).pick_file(){path=file.to_string_lossy().into_owned();set_nested(&mut self.draft,&["media","path"],json!(path));changed=true;}

if right.text_edit_singleline(&mut path).changed(){set_nested(&mut self.draft,&["media","path"],json!(path));changed=true;}
            let mut color=render::color(&self.draft["background"]["color"],[0,0,0,255]);
if right.color_edit_button_srgba_unmultiplied(&mut color).changed(){set_nested(&mut self.draft,&["background","color"],json!(color));changed=true;}
            right.separator();right.heading("Actions");
            let mut actions=self.draft["actions"].as_array().cloned().unwrap_or_default();
            let mut remove=None;let mut movement=None;
            for(index,action)in actions.iter_mut().enumerate(){
                let group=right.group(|ui|{
                    ui.dnd_drag_source(egui::Id::new(("drag-action",&self.selected,index)),index,|ui|{ui.label("↕ Drag to reorder");});
                    changed|=edit_action(ui,action,&definitions,&self.selected,index,&self.obs_choices,&self.media_players);
                    ui.horizontal(|ui|{
                        if ui.button("Move up").clicked()&&index>0{movement=Some((index,index-1));}
                        if ui.button("Move down").clicked(){movement=Some((index,index+1));}
                        if ui.button("Remove").clicked(){remove=Some(index);}
                    });
                });
                if let Some(source)=group.response.dnd_release_payload::<usize>(){movement=Some((*source,index));}
            }
            if let Some((a,b))=movement&& b<actions.len(){actions.swap(a,b);changed=true;}
if let Some(index)=remove{actions.remove(index);changed=true;}
            if right.button("Add action").clicked(){actions.push(json!({"id":"native::url","settings":{"url":"https://"}}));changed=true;}
            if changed{self.draft["actions"]=json!(actions);self.save();}
            right.collapsing("Advanced state JSON",|ui|{ui.add(egui::TextEdit::multiline(&mut self.raw).code_editor().desired_rows(8).desired_width(f32::INFINITY));
if ui.button("Apply state JSON").clicked(){match serde_json::from_str(&self.raw){Ok(value)=>{self.draft=value;self.save()},Err(e)=>self.notice=e.to_string()}}});
        });
    }
    fn pages(&mut self, ui: &mut egui::Ui) {
        ui.heading("Pages and assets");
        ui.horizontal(|ui| {
            ui.add(egui::TextEdit::singleline(&mut self.page_name).hint_text("Page name"));
            if ui.button("Create page").clicked() {
                self.command("create-page", json!({"page":self.page_name}));
            }
            if ui.button("Duplicate selected").clicked() {
                self.command(
                    "duplicate-page",
                    json!({"page":self.page,"name":self.page_name}),
                );
            }
            if ui.button("Rename selected").clicked() {
                self.command(
                    "rename-page",
                    json!({"page":self.page,"name":self.page_name}),
                );
                self.page = self.page_name.clone();
            }
        });
        let pages = self
            .shared
            .lock()
            .unwrap()
            .docs
            .pages
            .keys()
            .cloned()
            .collect::<Vec<_>>();
        for page in pages {
            ui.selectable_value(&mut self.page, page.clone(), page);
        }
        ui.separator();
        ui.add(
            egui::TextEdit::singleline(&mut self.path)
                .hint_text("Bundle path (.deckardpage / .zip) or icon directory"),
        );
        ui.horizontal(|ui| {
            if ui.button("Export selected with assets").clicked() {
                self.command("export-page", json!({"page":self.page,"path":self.path}));
            }
            if ui.button("Import page bundle").clicked() {
                let path = self.path.clone();
                let root = self.shared.lock().unwrap().docs.root.clone();
                self.background_task(move || {
                    let bytes = std::fs::read(path)?;
                    let id = deckard_core::store::install_archive(&bytes, "page", &root)?;
                    Ok(Task::Message(format!("Imported {id}")))
                });
            }
            if ui.button("Create icon pack").clicked() {
                let source = PathBuf::from(&self.path);
                let name = self.page_name.clone();
                let root = self.shared.lock().unwrap().docs.root.clone();
                self.background_task(move || {
                    create_icon_pack(&source, &root, &name)?;
                    Ok(Task::Message("Icon pack created".into()))
                });
            }
        });
        ui.collapsing("Page JSON", |ui| {
            let mut raw = self
                .shared
                .lock()
                .unwrap()
                .docs
                .pages
                .get(&self.page)
                .map(|p| serde_json::to_string_pretty(p).unwrap_or_default())
                .unwrap_or_default();
            let key = egui::Id::new(("page-json", &self.page));
            let mut text = ui
                .ctx()
                .data_mut(|d| d.get_temp::<String>(key))
                .unwrap_or_else(|| std::mem::take(&mut raw));
            ui.add(
                egui::TextEdit::multiline(&mut text)
                    .code_editor()
                    .desired_rows(16)
                    .desired_width(f32::INFINITY),
            );
            if ui.button("Save page JSON").clicked() {
                match serde_json::from_str::<Value>(&text) {
                    Ok(page) => self.command("put-page", json!({"page":self.page,"document":page})),
                    Err(e) => self.notice = e.to_string(),
                }
            }
            ui.ctx().data_mut(|d| d.insert_temp(key, text));
        });
    }
    fn store(&mut self, ui: &mut egui::Ui) {
        ui.heading("Native plugin, page and icon store");
        ui.label(
            "Install trusted native packages. Plugins run with your user account's permissions.",
        );
        ui.horizontal(|ui| {
            ui.add(
                egui::TextEdit::singleline(&mut self.catalog_url)
                    .hint_text("HTTPS catalog URL")
                    .desired_width(550.0),
            );
            if ui
                .add_enabled(!self.busy, egui::Button::new("Load catalog"))
                .clicked()
            {
                let url = self.catalog_url.clone();
                self.background_task(move || Ok(Task::Catalog(Network::new()?.catalog(&url)?)));
            }
        });
        ui.horizontal(|ui| {
            for (kind, label) in [("plugin", "Plugins"), ("page", "Pages"), ("icons", "Icons")] {
                ui.selectable_value(&mut self.store_tab, kind.into(), label);
            }
        });
        let mut branches: Vec<_> = self
            .catalog
            .iter()
            .map(|e| e.branch.clone())
            .filter(|b| !b.is_empty())
            .collect();
        branches.sort();
        branches.dedup();
        if !branches.is_empty() {
            egui::ComboBox::from_id_salt("store-branch")
                .selected_text(if self.store_branch.is_empty() {
                    "All branches"
                } else {
                    &self.store_branch
                })
                .show_ui(ui, |ui| {
                    ui.selectable_value(&mut self.store_branch, String::new(), "All branches");
                    for branch in branches {
                        ui.selectable_value(&mut self.store_branch, branch.clone(), branch);
                    }
                });
        }
        let catalog = self.catalog.clone();
        let store_tab = self.store_tab.clone();
        let store_branch = self.store_branch.clone();
        for entry in catalog.iter().filter(|e| {
            e.kind == store_tab && (store_branch.is_empty() || e.branch == store_branch)
        }) {
            ui.group(|ui| {
                ui.heading(&entry.name);
                ui.label(&entry.description);
                if !entry.branch.is_empty() {
                    ui.label(format!("Branch: {}", entry.branch));
                }
                if entry.source.starts_with("https://") {
                    ui.hyperlink_to("Source", &entry.source);
                }
                if ui
                    .add_enabled(!self.busy, egui::Button::new("Install"))
                    .clicked()
                {
                    let entry = entry.clone();
                    let root = self.shared.lock().unwrap().docs.root.clone();
                    self.background_task(move || {
                        Ok(Task::Message(format!(
                            "Installed {}",
                            Network::new()?.install(&entry, &root)?
                        )))
                    });
                }
            });
        }
        ui.separator();
        if ui.button("Choose package…").clicked()
            && let Some(path) = rfd::FileDialog::new()
                .add_filter("Native package", &["zip", "deckardpage"])
                .pick_file()
        {
            self.path = path.to_string_lossy().into_owned();
        }
        ui.label("Install a local native plugin package");
        ui.text_edit_singleline(&mut self.path);
        if ui
            .add_enabled(!self.busy, egui::Button::new("Install local ZIP"))
            .clicked()
        {
            let path = self.path.clone();
            let root = self.shared.lock().unwrap().docs.root.clone();
            let kind = self.store_tab.clone();
            self.background_task(move || {
                Ok(Task::Message(format!(
                    "Installed {}",
                    deckard_core::store::install_archive(&std::fs::read(path)?, &kind, &root)?
                )))
            });
        }
        let plugins = self.shared.lock().unwrap().plugins.clone();
        for plugin in plugins {
            ui.label(format!(
                "{} {}",
                plugin.manifest.name, plugin.manifest.version
            ));
        }
    }
    fn integrations(&mut self, ui: &mut egui::Ui) {
        ui.collapsing("OBS connections",|ui| {
            let settings=self.shared.lock().unwrap().docs.settings.clone();
            egui::ComboBox::from_id_salt("obs-profile-picker").selected_text(&self.obs_id).show_ui(ui,|ui| {
                if let Some(profiles)=settings["obs"]["connections"].as_object(){for (id,profile) in profiles {
                    if ui.selectable_label(self.obs_id==*id,profile["name"].as_str().unwrap_or(id)).clicked(){self.obs_id=id.clone();self.obs_profile=profile.clone();self.obs_choices=json!({});}
                }}
            });
            ui.horizontal(|ui|{ui.label("Profile ID");ui.text_edit_singleline(&mut self.obs_id);});
            for (key,label) in [("name","Name"),("host","Host"),("password","Password")] {
                let mut value=self.obs_profile[key].as_str().or_else(||if key=="host"{self.obs_profile["ip"].as_str()}else{None}).unwrap_or("").to_owned();
                ui.horizontal(|ui| {ui.label(label);if ui.add(egui::TextEdit::singleline(&mut value).password(key=="password")).changed(){self.obs_profile[key]=json!(value);}});
            }
            let mut port=self.obs_profile["port"].as_u64().unwrap_or(4455);ui.horizontal(|ui|{ui.label("Port");if ui.add(egui::DragValue::new(&mut port).range(1..=65535)).changed(){self.obs_profile["port"]=json!(port);}});
            let mut tls=self.obs_profile["tls"].as_bool().unwrap_or(false);if ui.checkbox(&mut tls,"TLS (wss)").changed(){self.obs_profile["tls"]=json!(tls);}
            ui.horizontal(|ui| {
                if ui.button("New profile").clicked(){self.obs_id=format!("profile-{}",std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_nanos());self.obs_profile=json!({"name":"New profile","host":"localhost","port":4455,"password":"","tls":false});}
                if ui.button("Save profile").clicked(){if self.obs_id.is_empty(){self.notice="Enter a profile ID".into();}else{let mut next=settings.clone();set_nested(&mut next,&["obs","connections",&self.obs_id],self.obs_profile.clone());self.command("put-settings",next);self.sync_settings();}}
                if ui.button("Make default").clicked(){let mut next=settings.clone();set_nested(&mut next,&["obs","default_connection"],json!(self.obs_id));self.command("put-settings",next);self.sync_settings();}
                if ui.button("Delete profile").clicked(){let mut next=settings.clone();if let Some(profiles)=next["obs"]["connections"].as_object_mut(){profiles.remove(&self.obs_id);}self.command("put-settings",next);self.sync_settings();self.obs_choices=json!({});}
            });
            ui.horizontal(|ui| {
                if ui.add_enabled(!self.busy,egui::Button::new("Test connection")).clicked(){let profile=self.obs_profile.clone();self.background_task(move||{let mut client=deckard_core::obs::connect(&profile,0)?;let version=client.query("GetVersion",json!({}))?;Ok(Task::Message(format!("Connected to OBS {}",version["obsVersion"].as_str().unwrap_or(""))))});}
                if ui.add_enabled(!self.busy,egui::Button::new("Refresh OBS choices")).clicked(){let profile=self.obs_profile.clone();let id=self.obs_id.clone();self.background_task(move||{let mut client=deckard_core::obs::connect(&profile,0)?;let mut choices=json!({"connection":id});for (key,request) in [("scenes","GetSceneList"),("inputs","GetInputList"),("collections","GetSceneCollectionList")] {choices[key]=client.query(request,json!({}))?;}let scenes=choices["scenes"]["scenes"].as_array().cloned().unwrap_or_default();for scene in scenes {if let Some(name)=scene["sceneName"].as_str(){if let Ok(items)=client.query("GetSceneItemList",json!({"sceneName":name})){choices["items"][name]=items;}
if let Ok(filters)=client.query("GetSourceFilterList",json!({"sourceName":name})){choices["filters"][name]=filters;}}}Ok(Task::Choices(choices))});}
            });
        });
        ui.collapsing("Media players", |ui| {
            if ui
                .add_enabled(!self.busy, egui::Button::new("Refresh running players"))
                .clicked()
            {
                self.background_task(|| {
                    Ok(Task::Players(
                        deckard_core::mpris::snapshot()?
                            .into_iter()
                            .map(|p| p.identity)
                            .collect(),
                    ))
                });
            }
            for player in &self.media_players {
                ui.label(player);
            }
            ui.label("Leave player selection empty to control all matching players.");
        });
    }
    fn sync_settings(&mut self) {
        let e = self.shared.lock().unwrap();
        self.settings = serde_json::to_string_pretty(&e.docs.settings).unwrap_or_default();
        self.settings_revision = e.docs.revision;
    }
    fn settings(&mut self, ui: &mut egui::Ui) {
        ui.heading("Settings");
        self.integrations(ui);
        ui.label("Migrate actions from OS, Deck, Media, OBS and VolumeMixer pages. Originals are backed up before conversion.");
        ui.horizontal(|ui| {
            for (label, method) in [
                ("Inspect old plugin actions", "inspect-legacy-actions"),
                ("Migrate supported actions", "migrate-legacy-actions"),
            ] {
                if ui.button(label).clicked() {
                    let result = self
                        .shared
                        .lock()
                        .unwrap()
                        .command(&json!({"method":method}));
                    match result {
                        Ok(report) => {
                            self.migration_report = Some(report);
                            self.selected.clear();
                            let engine = self.shared.lock().unwrap();
                            self.settings = serde_json::to_string_pretty(&engine.docs.settings)
                                .unwrap_or_default();
                            self.settings_revision = engine.docs.revision;
                        }
                        Err(error) => self.notice = error.to_string(),
                    }
                }
            }
        });
        if let Some(report) = &self.migration_report
            && let Some(documents) = report["documents"].as_array()
        {
            let converted: usize = documents
                .iter()
                .map(|d| d["converted"].as_array().map_or(0, Vec::len))
                .sum();
            let remaining: usize = documents
                .iter()
                .map(|d| d["remaining"].as_array().map_or(0, Vec::len))
                .sum();
            ui.label(format!(
                "{converted} supported actions; {remaining} actions still need a replacement."
            ));
            ui.label("All 56 actions from the five recommended upstream plugins have Rust implementations, including live displays and timers.");
            for document in documents {
                if let Some(items) = document["remaining"].as_array() {
                    for item in items {
                        ui.label(format!(
                            "{} · {} {} · {}: {}",
                            item["document"].as_str().unwrap_or(""),
                            item["family"].as_str().unwrap_or(""),
                            item["input"].as_str().unwrap_or(""),
                            item["id"].as_str().unwrap_or(""),
                            item["reason"].as_str().unwrap_or("")
                        ));
                    }
                }
            }
        }
        ui.separator();
        let helper = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|p| p.join("install-udev.sh")))
            .filter(|p| p.exists());
        if let Some(helper) = helper
            && ui
                .add_enabled(
                    !self.busy,
                    egui::Button::new("Enable Stream Deck USB access…"),
                )
                .clicked()
        {
            self.background_task(move || {
                deckard_core::desktop::run_command(
                    &["pkexec".into(), helper.to_string_lossy().into_owned()],
                    std::time::Duration::from_secs(120),
                )?;
                Ok(Task::Message(
                    "USB access rules installed. Reconnect the deck if needed.".into(),
                ))
            });
        }

        ui.label("Device names, rotation, persistent states, wallpaper and automatic page rules are saved here.");
        ui.add(
            egui::TextEdit::multiline(&mut self.settings)
                .code_editor()
                .desired_rows(15)
                .desired_width(f32::INFINITY),
        );
        if ui.button("Save settings").clicked() {
            match serde_json::from_str(&self.settings) {
                Ok(settings) => {
                    if self.shared.lock().unwrap().docs.revision != self.settings_revision {
                        self.notice = "Settings changed while editing. Reopen Settings to refresh before saving.".into();
                    } else {
                        self.command("put-settings", settings);
                        self.settings_revision = self.shared.lock().unwrap().docs.revision;
                    }
                }
                Err(e) => self.notice = e.to_string(),
            }
        }
        ui.horizontal(|ui| {
            if ui.button("Start at login").clicked() {
                self.notice = autostart(true)
                    .map(|_| "Autostart enabled".into())
                    .unwrap_or_else(|e| e.to_string());
            }
            if ui.button("Disable autostart").clicked() {
                self.notice = autostart(false)
                    .map(|_| "Autostart disabled".into())
                    .unwrap_or_else(|e| e.to_string());
            }
            if ui.button("Open data folder").clicked() {
                let path = self.shared.lock().unwrap().docs.root.clone();
                let _ = std::process::Command::new("xdg-open").arg(path).spawn();
            }
        });
        ui.separator();
        ui.heading("AI page assistant (optional)");
        let mut enabled = self.shared.lock().unwrap().docs.settings["ai"]["enabled"]
            .as_bool()
            .unwrap_or(false);
        if ui
            .checkbox(&mut enabled, "Enable AI page suggestions")
            .changed()
        {
            let mut settings = self.shared.lock().unwrap().docs.settings.clone();
            set_nested(&mut settings, &["ai", "enabled"], json!(enabled));
            self.command("put-settings", settings);
        }
        if enabled {
            ui.label("Endpoint");
            ui.text_edit_singleline(&mut self.ai_endpoint);
            ui.label("Model");
            ui.text_edit_singleline(&mut self.ai_model);
            ui.label("API key (kept only in memory)");
            ui.add(egui::TextEdit::singleline(&mut self.ai_key).password(true));
            ui.add(
                egui::TextEdit::multiline(&mut self.ai_prompt)
                    .hint_text("Describe the page you want")
                    .desired_rows(3),
            );
            if ui
                .add_enabled(!self.busy, egui::Button::new("Generate suggestion"))
                .clicked()
            {
                let (endpoint, key, model, prompt) = (
                    self.ai_endpoint.clone(),
                    self.ai_key.clone(),
                    self.ai_model.clone(),
                    self.ai_prompt.clone(),
                );
                self.background_task(move || {
                    Ok(Task::Ai(
                        Network::new()?.ai(&endpoint, &key, &model, &prompt)?,
                    ))
                });
            }
            if let Some(proposal) = self.ai_proposal.clone() {
                ui.label("Review the complete suggestion before applying it:");
                let mut raw = serde_json::to_string_pretty(&proposal).unwrap_or_default();
                ui.add(
                    egui::TextEdit::multiline(&mut raw)
                        .code_editor()
                        .desired_rows(8),
                );
                ui.checkbox(
                    &mut self.allow_commands,
                    "Allow commands, typing and hotkeys in this suggestion",
                );
                if ui.button("Apply to selected page").clicked() {
                    if !self.allow_commands && has_privileged_action(&proposal) {
                        self.notice="Suggestion includes commands, typing or hotkeys. Review and explicitly allow them.".into();
                    } else {
                        self.command("put-page", json!({"page":self.page,"document":proposal}));
                        self.ai_proposal = None;
                        self.allow_commands = false;
                    }
                }
            }
        }
        ui.separator();
        ui.label(format!(
            "Deckard {} · Rust · Native plugin API 1",
            env!("CARGO_PKG_VERSION")
        ));
        ui.hyperlink_to(
            "Project and upstream history",
            "https://github.com/pauljones0/Deckard",
        );
        ui.label("StreamController by Core447; Deckard improvements by nazbert and contributors. GPL-3.0-or-later.");
    }
}
impl Drop for App {
    fn drop(&mut self) {
        self.watch_stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.watch_thread.take() {
            let _ = thread.join();
        }
    }
}
impl eframe::App for App {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        if self.signal.load(Ordering::Relaxed) || self.shared.lock().unwrap().quit {
            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
        }
        let (show, keep_running, quit) = {
            let mut engine = self.shared.lock().unwrap();
            let show = engine.show_window;
            engine.show_window = false;
            (
                show,
                engine.docs.settings["keep_running"]
                    .as_bool()
                    .unwrap_or(true),
                engine.quit,
            )
        };
        if show {
            self.hidden = false;
            ctx.send_viewport_cmd(egui::ViewportCommand::Visible(true));
            ctx.send_viewport_cmd(egui::ViewportCommand::Focus);
        }
        if !quit
            && !self.signal.load(Ordering::Relaxed)
            && keep_running
            && ctx.input(|i| i.viewport().close_requested())
        {
            self.hidden = true;
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            ctx.send_viewport_cmd(egui::ViewportCommand::Visible(false));
        }
        if self.hidden {
            ctx.request_repaint_after(std::time::Duration::from_millis(500));
            return;
        }
        if let Some(path) = std::env::var_os("DECKARD_UI_CAPTURE") {
            if !self.capture_sent && self.started.elapsed() > std::time::Duration::from_secs(1) {
                self.capture_sent = true;
                ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(Default::default()));
            }
            let screenshot = ctx.input(|i| {
                i.events.iter().find_map(|e| {
                    if let egui::Event::Screenshot { image, .. } = e {
                        Some(image.clone())
                    } else {
                        None
                    }
                })
            });
            if let Some(image) = screenshot {
                let pixels: Vec<u8> = image.pixels.iter().flat_map(|p| p.to_array()).collect();
                if let Err(error) = image::save_buffer(
                    std::path::PathBuf::from(path),
                    &pixels,
                    image.size[0] as u32,
                    image.size[1] as u32,
                    image::ColorType::Rgba8,
                ) {
                    self.notice = error.to_string();
                }
            }
        }
        while let Ok(task) = self.receive.try_recv() {
            self.busy = false;
            match task {
                Task::Choices(choices) => {
                    self.obs_choices = choices;
                    self.notice = "OBS choices refreshed".into();
                }
                Task::Players(players) => {
                    self.media_players = players;
                    self.notice = "Media players refreshed".into();
                }
                Task::Catalog(entries) => {
                    self.catalog = entries;
                    self.notice = "Catalog loaded".into()
                }
                Task::Message(message) => {
                    self.notice = message;
                    let _ = self
                        .shared
                        .lock()
                        .unwrap()
                        .command(&json!({"method":"reload"}));
                }
                Task::Ai(page) => self.ai_proposal = Some(page),
            }
        }
        egui::TopBottomPanel::top("navigation").show(ctx, |ui| {
            ui.add_space(6.0);
            ui.horizontal(|ui| {
                ui.label(RichText::new("Deckard").size(24.0).strong());
                ui.add_space(24.0);
                for (index, name) in ["Editor", "Pages & assets", "Store", "Settings"]
                    .iter()
                    .enumerate()
                {
                    if ui.selectable_value(&mut self.tab, index, *name).clicked() && index == 3 {
                        let engine = self.shared.lock().unwrap();
                        self.settings =
                            serde_json::to_string_pretty(&engine.docs.settings).unwrap_or_default();
                        self.settings_revision = engine.docs.revision;
                    }
                }
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if ui.button("Quit").clicked() {
                        self.shared.lock().unwrap().quit = true;
                        ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                    }
                });
            });
            ui.add_space(6.0);
        });
        egui::TopBottomPanel::bottom("status").show(ctx, |ui| {
            ui.horizontal(|ui| {
                if self.busy {
                    ui.spinner();
                }
                ui.label(&self.notice);
            });
            let error = self.shared.lock().unwrap().errors.back().cloned();
            if let Some(error) = error {
                ui.colored_label(Color32::YELLOW, error);
            }
        });
        egui::CentralPanel::default().show(ctx, |ui| {
            egui::ScrollArea::vertical().show(ui, |ui| match self.tab {
                0 => self.editor(ctx, ui),
                1 => self.pages(ui),
                2 => self.store(ui),
                _ => self.settings(ui),
            });
        });
        // Input events and changed engine frames wake the UI. Static pages stay asleep.
        if std::env::var_os("DECKARD_UI_CAPTURE").is_some()
            || std::env::var_os("DECKARD_UI_SMOKE_TEST").is_some()
        {
            ctx.request_repaint_after(std::time::Duration::from_millis(100));
        }
    }
}
fn set_nested(value: &mut Value, path: &[&str], new: Value) {
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
fn action_definitions(plugins: &[plugin::Installed]) -> Vec<(String, String, Vec<plugin::Field>)> {
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
fn has_privileged_action(v: &Value) -> bool {
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
fn autostart(enable: bool) -> Result<()> {
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
fn create_icon_pack(source: &std::path::Path, root: &std::path::Path, name: &str) -> Result<()> {
    model::valid_name(name)?;
    let destination = root.join("icons-native").join(name);
    anyhow::ensure!(!destination.exists(), "icon pack already exists");
    let parent = destination.parent().unwrap();
    std::fs::create_dir_all(parent)?;
    let staging = tempfile_dir(parent)?;
    let mut count = 0;
    for entry in std::fs::read_dir(source)? {
        let entry = entry?;
        if entry.file_type()?.is_file()
            && entry.path().extension().is_some_and(|e| {
                ["png", "jpg", "jpeg", "webp", "gif", "svg"]
                    .iter()
                    .any(|x| e == *x)
            })
        {
            anyhow::ensure!(
                entry.metadata()?.len() <= 32 * 1024 * 1024,
                "icon exceeds size limit"
            );
            std::fs::copy(entry.path(), staging.join(entry.file_name()))?;
            count += 1;
            anyhow::ensure!(count <= 2000, "too many icons");
        }
    }
    model::save_json(
        &staging.join("package.json"),
        &json!({"api":1,"id":name,"name":name,"kind":"icons"}),
    )?;
    std::fs::rename(staging, destination)?;
    Ok(())
}
fn tempfile_dir(parent: &std::path::Path) -> Result<PathBuf> {
    let path = parent.join(format!(".pack-{}", std::process::id()));
    std::fs::create_dir(&path)?;
    Ok(path)
}

fn edit_action(
    ui: &mut egui::Ui,
    action: &mut Value,
    definitions: &[(String, String, Vec<plugin::Field>)],
    selected: &str,
    index: usize,
    choices: &Value,
    players: &[String],
) -> bool {
    let mut changed = false;
    let id = action["id"].as_str().unwrap_or("").to_owned();
    if !definitions.iter().any(|d| d.0 == id) {
        ui.colored_label(Color32::YELLOW, format!("Replacement needed: {id}"));
    }
    egui::ComboBox::from_id_salt(("action", index))
        .selected_text(
            definitions
                .iter()
                .find(|d| d.0 == id)
                .map(|d| d.1.as_str())
                .unwrap_or(&id),
        )
        .show_ui(ui, |ui| {
            for (def_id, name, fields) in definitions {
                if ui.selectable_label(id == *def_id, name).clicked() {
                    let mut settings = json!({});
                    for field in fields {
                        settings[&field.key] = field.default.clone();
                    }
                    *action = json!({"id":def_id,"event":if def_id.contains("Plugin-") || def_id.contains("VolumeMixer-"){"auto"}else{"press"},"settings":settings});
                    changed = true;
                }
            }
        });
    let mut event = action["event"].as_str().unwrap_or("press").to_owned();
    egui::ComboBox::from_id_salt(("event", index))
        .selected_text(&event)
        .show_ui(ui, |ui| {
            for choice in [
                "auto",
                "lifecycle",
                "press",
                "release",
                "long-press",
                "touch",
                "long-touch",
                "turn-cw",
                "turn-ccw",
                "swipe-left",
                "swipe-right",
            ] {
                if ui
                    .selectable_value(&mut event, choice.into(), choice)
                    .changed()
                {
                    action["event"] = json!(event);
                    changed = true;
                }
            }
        });
    if let Some((_, _, fields)) = definitions
        .iter()
        .find(|d| d.0 == action["id"].as_str().unwrap_or(""))
    {
        for field in fields {
            ui.label(&field.label);
            let current = action["settings"][&field.key].clone();
            let mut options = Vec::<String>::new();
            match field.key.as_str() {
                "player_name" | "player" => options.extend(players.iter().cloned()),
                "scene" => {
                    if let Some(scenes) = choices["scenes"]["scenes"].as_array() {
                        options.extend(
                            scenes
                                .iter()
                                .filter_map(|s| s["sceneName"].as_str().map(str::to_owned)),
                        );
                    }
                }
                "input" => {
                    if let Some(inputs) = choices["inputs"]["inputs"].as_array() {
                        options.extend(
                            inputs
                                .iter()
                                .filter_map(|s| s["inputName"].as_str().map(str::to_owned)),
                        );
                    }
                }
                "scene_collection" => {
                    if let Some(items) = choices["collections"]["sceneCollections"].as_array() {
                        options.extend(
                            items.iter().filter_map(|s| {
                                s["sceneCollectionName"].as_str().map(str::to_owned)
                            }),
                        );
                    }
                }
                "item" => {
                    let scene = action["settings"]["scene"].as_str().unwrap_or("");
                    if let Some(items) = choices["items"][scene]["sceneItems"].as_array() {
                        options.extend(
                            items
                                .iter()
                                .filter_map(|s| s["sourceName"].as_str().map(str::to_owned)),
                        );
                    }
                }
                "filter" => {
                    let scene = action["settings"]["scene"].as_str().unwrap_or("");
                    if let Some(items) = choices["filters"][scene]["filters"].as_array() {
                        options.extend(
                            items
                                .iter()
                                .filter_map(|s| s["filterName"].as_str().map(str::to_owned)),
                        );
                    }
                }
                _ => {}
            }
            if !options.is_empty() {
                egui::ComboBox::from_id_salt(("integration-choice", selected, index, &field.key))
                    .selected_text("Choose available…")
                    .show_ui(ui, |ui| {
                        for option in options {
                            if ui.selectable_label(current == option, &option).clicked() {
                                set_nested(action, &["settings", &field.key], json!(option));
                                changed = true;
                            }
                        }
                    });
            }
            if (field.key == "app_path"
                || field.key == "idle_icon"
                || field.key.starts_with("custom_icon_"))
                && ui.button("Choose file…").clicked()
                && let Some(path) = rfd::FileDialog::new().pick_file()
            {
                set_nested(
                    action,
                    &["settings", &field.key],
                    json!(path.to_string_lossy()),
                );
                changed = true;
            }
            match field.kind.as_str() {
                "bool" => {
                    let mut value = current.as_bool().unwrap_or(false);
                    if ui.checkbox(&mut value, &field.label).changed() {
                        set_nested(action, &["settings", &field.key], json!(value));
                        changed = true;
                    }
                }
                "number" => {
                    let mut value = current.as_f64().unwrap_or(0.0);
                    if ui.add(egui::DragValue::new(&mut value)).changed() {
                        set_nested(action, &["settings", &field.key], json!(value));
                        changed = true;
                    }
                }
                "array" => {
                    let key = egui::Id::new(("array-field", selected, index, &field.key));
                    let mut text = ui
                        .ctx()
                        .data_mut(|d| d.get_temp::<String>(key))
                        .unwrap_or_else(|| {
                            if current.is_array() || current.is_object() {
                                current.to_string()
                            } else {
                                "[]".into()
                            }
                        });
                    if ui.text_edit_singleline(&mut text).changed() {
                        match serde_json::from_str::<Value>(&text) {
                            Ok(parsed) if parsed.is_array() || parsed.is_object() => {
                                set_nested(action, &["settings", &field.key], parsed);
                                changed = true;
                            }
                            _ => {
                                ui.colored_label(Color32::YELLOW, "Enter a JSON array or object.");
                            }
                        }
                    }
                    ui.ctx().data_mut(|d| d.insert_temp(key, text));
                }
                _ => {
                    let mut value = current.as_str().unwrap_or("").to_owned();
                    if ui.text_edit_singleline(&mut value).changed() {
                        set_nested(action, &["settings", &field.key], json!(value));
                        changed = true;
                    }
                }
            }
        }
    }
    changed
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
