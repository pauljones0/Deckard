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
    let options = eframe::NativeOptions {
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
            cc.egui_ctx.set_visuals(egui::Visuals::dark());
            Ok(Box::new(App::new(shared, events, signal)))
        }),
    )
    .map_err(|error| anyhow::anyhow!(error.to_string()))
}
enum Task {
    Catalog(Vec<Entry>),
    Message(String),
    Ai(Value),
}
struct App {
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
    textures: HashMap<u8, (u64, egui::TextureHandle)>,
    settings: String,
    settings_revision: u64,
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
    fn new(shared: Shared, events: SyncSender<InputEvent>, signal: Arc<AtomicBool>) -> Self {
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
        drop(engine);
        let (send, receive) = mpsc::sync_channel(4);
        Self {
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
            textures: HashMap::new(),
            settings,
            settings_revision,
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
        std::thread::spawn(move || {
            let result = work().unwrap_or_else(|e| Task::Message(e.to_string()));
            let _ = send.send(result);
        });
    }
    fn editor(&mut self, ctx: &egui::Context, ui: &mut egui::Ui) {
        let (devices, plugins) = {
            let engine = self.shared.lock().unwrap();
            (
                engine.devices.values().cloned().collect::<Vec<_>>(),
                engine.plugins.clone(),
            )
        };
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
            if let Some(frame)=device.and_then(|d|d.frame.as_ref()){for tile in frame.tiles.iter().chain(frame.strip.iter()){if self.textures.get(&tile.key).is_none_or(|(identity,_)|*identity!=tile.identity){let texture=ctx.load_texture(format!("tile{}",tile.key),egui::ColorImage::from_rgb([tile.width as usize,tile.height as usize],&tile.rgb),egui::TextureOptions::LINEAR);self.textures.insert(tile.key,(tile.identity,texture));}}}
            let size=((left.available_width()-f32::from(cols)*8.0)/f32::from(cols)).clamp(16.0,100.0);
            egui::Grid::new("key-grid").spacing([8.0,8.0]).show(left,|ui|{for y in 0..rows{for x in 0..cols{let input=format!("{x}x{y}");let physical=(0..kind.key_count()).find(|p|render::logical_input(kind,*p,rotation)==input).unwrap_or(0);let selected=self.family=="keys"&&self.input==input;let clicked=if let Some((_,texture))=self.textures.get(&physical){ui.add(egui::Button::image(egui::Image::new((texture.id(),Vec2::splat(size)))).selected(selected)).clicked()}else{ui.add_sized([size,size],egui::Button::new(&input).selected(selected)).clicked()};
if clicked{self.family="keys".into();self.input=input;self.state=0;}}ui.end_row();}});
            if let Some((_,texture))=self.textures.get(&255){left.add(egui::Image::new((texture.id(),Vec2::new(left.available_width(),70.0))));}
            left.horizontal_wrapped(|ui|{for i in 0..kind.encoder_count(){if ui.selectable_label(self.family=="dials"&&self.input==i.to_string(),format!("Dial {}",i+1)).clicked(){self.family="dials".into();self.input=i.to_string();self.state=0;}}if kind.lcd_strip_size().is_some()&&kind.encoder_count()>0&&ui.button("Touchscreen gestures").clicked(){self.family="touchscreens".into();self.input="0".into();self.state=0;}
if format!("{kind:?}")=="Neo"{if ui.button("Infobar").clicked(){self.family="infobar".into();self.input="0".into();self.state=0;}for i in 0..2{if ui.button(format!("Touch key {}",i+1)).clicked(){self.family="keys".into();self.input=format!("touch-{i}");self.state=0;}}}});
            left.add_space(12.0);
            if left.button("Test selected input").clicked(){let _=self.events.try_send(InputEvent{serial:self.serial.clone(),family:self.family.clone(),input:self.input.clone(),event:"press".into(),value:1});let _=self.events.try_send(InputEvent{serial:self.serial.clone(),family:self.family.clone(),input:self.input.clone(),event:"release".into(),value:0});}
            if device.is_none(){left.label("Connect a Stream Deck, or start with --fake-deck-model plus to preview without hardware.");}
            let page=self.shared.lock().unwrap().docs.pages.get(&self.page).cloned().unwrap_or_default();
            left.collapsing("Wallpaper",|ui| {
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
            if !self.serial.is_empty(){right.checkbox(&mut self.sticky,"Edit sticky input across all pages");let config=self.shared.lock().unwrap().config(&self.serial);
if let Some(config)=config{let effective=render::effective_input(&config,&self.family,&self.input);
if !self.sticky&&effective!=&config.page[&self.family][&self.input]{right.colored_label(Color32::YELLOW,"This input is controlled by sticky settings. Enable the sticky editor to change it.");}}}
            self.select();let mut changed=false;
            for(position,title)in[("top","Top label"),("center","Center label"),("bottom","Bottom label")]{let mut value=self.draft["labels"][position]["text"].as_str().unwrap_or("").to_owned();right.horizontal(|ui|{ui.label(title);
if ui.text_edit_singleline(&mut value).changed(){set_nested(&mut self.draft,&["labels",position,"text"],json!(value));changed=true;}});}
            let mut path=self.draft["media"]["path"].as_str().unwrap_or("").to_owned();right.label("Icon / animation path");
if right.button("Choose file…").clicked()&& let Some(file)=rfd::FileDialog::new().add_filter("Images and video",&["png","jpg","jpeg","webp","svg","gif","mp4","mkv","webm"]).pick_file(){path=file.to_string_lossy().into_owned();set_nested(&mut self.draft,&["media","path"],json!(path));changed=true;}

if right.text_edit_singleline(&mut path).changed(){set_nested(&mut self.draft,&["media","path"],json!(path));changed=true;}
            let mut color=render::color(&self.draft["background"]["color"],[0,0,0,255]);
if right.color_edit_button_srgba_unmultiplied(&mut color).changed(){set_nested(&mut self.draft,&["background","color"],json!(color));changed=true;}
            right.separator();right.heading("Actions");
            let mut actions=self.draft["actions"].as_array().cloned().unwrap_or_default();let definitions=action_definitions(&plugins);
            let mut remove=None;let mut movement=None;
            for(index,action)in actions.iter_mut().enumerate(){
                let group=right.group(|ui|{
                    ui.dnd_drag_source(egui::Id::new(("drag-action",&self.selected,index)),index,|ui|{ui.label("↕ Drag to reorder");});
                    changed|=edit_action(ui,action,&definitions,&self.selected,index);
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
    fn settings(&mut self, ui: &mut egui::Ui) {
        ui.heading("Settings");
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
        ctx.request_repaint_after(std::time::Duration::from_millis(50));
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
    result
}
fn has_privileged_action(v: &Value) -> bool {
    if v["id"]
        .as_str()
        .is_some_and(|id| ["native::command", "native::text", "native::hotkey"].contains(&id))
    {
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
                    *action = json!({"id":def_id,"event":"press","settings":settings});
                    changed = true;
                }
            }
        });
    let mut event = action["event"].as_str().unwrap_or("press").to_owned();
    egui::ComboBox::from_id_salt(("event", index))
        .selected_text(&event)
        .show_ui(ui, |ui| {
            for choice in [
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
                            if current.is_array() {
                                current.to_string()
                            } else {
                                "[]".into()
                            }
                        });
                    if ui.text_edit_singleline(&mut text).changed() {
                        match serde_json::from_str::<Value>(&text) {
                            Ok(parsed) if parsed.is_array() => {
                                set_nested(action, &["settings", &field.key], parsed);
                                changed = true;
                            }
                            _ => {
                                ui.colored_label(Color32::YELLOW,"Enter a JSON array, for example [\"playerctl\", \"play-pause\"].");
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
