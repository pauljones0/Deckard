//! Upstream PreferencesWindow and its seven pages. Native settings are written
//! through the existing serialized worker, rather than Python widget callbacks.
#![allow(deprecated)]
use super::*;
use controls::*;
use glib::translate::IntoGlib;

fn page(title: &str, icon: &str) -> adw::PreferencesPage {
    adw::PreferencesPage::builder()
        .title(title)
        .name(title)
        .icon_name(icon)
        .build()
}
fn group(page: &adw::PreferencesPage, title: &str) -> adw::PreferencesGroup {
    let group = adw::PreferencesGroup::builder().title(title).build();
    page.add(&group);
    group
}
impl Ui {
    pub(super) fn keep_window(self: &Rc<Self>, name: &str, window: &impl IsA<gtk::Window>) {
        let window = window.clone().upcast::<gtk::Window>();
        window.set_application(self.window.application().as_ref());
        window.set_transient_for(Some(&self.window));
        let weak = Rc::downgrade(self);
        let key = name.to_owned();
        window.connect_close_request(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.auxiliary_windows.borrow_mut().remove(&key);
            }
            glib::Propagation::Proceed
        });
        self.auxiliary_windows
            .borrow_mut()
            .insert(name.into(), window.clone());
        window.present();
    }
    fn preference_switch(
        self: &Rc<Self>,
        title: &str,
        subtitle: &str,
        path: &[&str],
        default: bool,
    ) -> adw::SwitchRow {
        let engine = self.shared.lock().unwrap();
        let value = get_path(&engine.docs.settings, path)
            .as_bool()
            .or_else(|| {
                (path == ["warnings", "enable-fps-warnings"])
                    .then(|| engine.docs.settings["ui"]["enable-fps-warnings"].as_bool())
                    .flatten()
            })
            .unwrap_or(default);
        drop(engine);
        let keys = path.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let weak = Rc::downgrade(self);
        let row = toggle(title, value, move |value| {
            if let Some(ui) = weak.upgrade() {
                ui.settings_property(keys.clone(), json!(value));
            }
        });
        row.set_subtitle(subtitle);
        row
    }
    #[allow(clippy::too_many_arguments)]
    fn preference_spin(
        self: &Rc<Self>,
        title: &str,
        subtitle: &str,
        path: &[&str],
        default: f64,
        min: f64,
        max: f64,
        step: f64,
        factor: f64,
    ) -> adw::SpinRow {
        let row = adw::SpinRow::with_range(min, max, step);
        row.set_title(title);
        row.set_subtitle(subtitle);
        row.set_digits(if step < 1. { 1 } else { 0 });
        row.set_value(
            get_path(&self.shared.lock().unwrap().docs.settings, path)
                .as_f64()
                .unwrap_or(default)
                / factor,
        );
        let keys = path.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let weak = Rc::downgrade(self);
        row.connect_changed(move |row| {
            if let Some(ui) = weak.upgrade() {
                ui.settings_property(keys.clone(), json!((row.value() * factor).round() as u64));
            }
        });
        row
    }
    pub(super) fn settings(self: &Rc<Self>) {
        if let Some(window) = self.auxiliary_windows.borrow().get("Settings") {
            window.present();
            return;
        }
        let window = adw::PreferencesWindow::builder()
            .title("Settings")
            .default_width(1000)
            .default_height(700)
            .modal(true)
            .build();
        let general = page("General", "open-menu-symbolic");
        let ui_page = page("UI", "window-new-symbolic");
        let store = page("Store", "go-home-symbolic");
        let performance = page("Performance", "power-profile-performance-symbolic");
        let system = page("System", "system-run-symbolic");
        let developer = page("Developer", "text-editor-symbolic");
        let plugins = page("Plugins", "application-x-addon-symbolic");
        for page in [
            &general,
            &ui_page,
            &store,
            &performance,
            &system,
            &developer,
            &plugins,
        ] {
            window.add(page);
        }
        let main = group(&general, "General app settings");
        main.add(&self.preference_spin(
            "Minimum hold duration (s)",
            "Minimum hold duration for keys and dials",
            &["hold_ms"],
            500.,
            0.1,
            3.,
            0.1,
            1000.,
        ));
        main.add(&self.preference_switch(
            "Rolling labels",
            "Enable automatic rolling/scrolling of too long labels",
            &["rolling_labels"],
            true,
        ));
        main.add(&self.preference_switch(
            "Shrink keys while pressed",
            "Draw a held key smaller, so the background shows at its edges",
            &["shrink_on_press"],
            true,
        ));
        self.preference_fonts(&group(&general, "Default font"));
        let grid = group(&ui_page, "Key Grid");
        for (title, subtitle, key, default) in [
            ("Show Tray Icon", "", "tray-icon", true),
            (
                "Emulate button press on double click",
                "",
                "emulate-at-double-click",
                true,
            ),
            ("Enable FPS warnings", "", "enable-fps-warnings", true),
            (
                "Allow white mode",
                "Allow white mode for the ui - Experimental",
                "allow-white-mode",
                false,
            ),
            (
                "Show notifications",
                "Show notifications for outdated assets",
                "show-notifications",
                true,
            ),
            (
                "Automatically open config options",
                "Automatically open config options for new actions",
                "auto-open-action-config",
                true,
            ),
        ] {
            let section = if key == "enable-fps-warnings" {
                "warnings"
            } else {
                "ui"
            };
            grid.add(&self.preference_switch(title, subtitle, &[section, key], default));
        }
        let store_group = group(&store, "Store");
        store_group.add(&self.preference_switch(
            "Auto-update assets",
            "",
            &["store", "auto-update"],
            true,
        ));
        let policy = self.shared.lock().unwrap().docs.settings["store"]["install-script-policy"]
            .as_str()
            .unwrap_or("Ask")
            .to_owned();
        let weak = Rc::downgrade(self);
        let row = choice(
            "Plugin install scripts",
            &["Ask", "Always run", "Never run"],
            &policy,
            move |value| {
                if let Some(ui) = weak.upgrade() {
                    ui.settings_property(
                        vec!["store".into(), "install-script-policy".into()],
                        json!(value),
                    );
                }
            },
        );
        row.set_subtitle("Run when a plugin ships a setup step at install time");
        store_group.add(&row);
        self.custom_repositories(
            &group(&store, "Custom Stores"),
            "stores",
            "Add custom store repos",
        );
        self.custom_repositories(
            &group(&store, "Custom Plugins"),
            "plugins",
            "Add custom plugin repos",
        );
        let perf = group(&performance, "Performance &amp; Optimizations");
        perf.add(&self.preference_spin(
            "Number of cached pages",
            "Number of pages to keep in cache",
            &["performance", "n-cached-pages"],
            3.,
            0.,
            50.,
            1.,
            1.,
        ));
        perf.add(&self.preference_switch(
            "Cache Videos",
            "Only applies to new videos or after a restart",
            &["performance", "cache-videos"],
            true,
        ));
        let mode = self.shared.lock().unwrap().docs.settings["animation_pause_mode"]
            .as_str()
            .unwrap_or("screensaver")
            .to_owned();
        let weak = Rc::downgrade(self);
        let pause = choice(
            "Pause animations",
            &[
                "Only while the deck screensaver is active",
                "Also when the system is idle or the screen is locked",
            ],
            if mode == "system-idle" {
                "Also when the system is idle or the screen is locked"
            } else {
                "Only while the deck screensaver is active"
            },
            move |value| {
                if let Some(ui) = weak.upgrade() {
                    ui.settings_property(
                        vec!["animation_pause_mode".into()],
                        json!(if value.starts_with("Also") {
                            "system-idle"
                        } else {
                            "screensaver"
                        }),
                    );
                }
            },
        );
        pause.set_subtitle("When to stop background videos, key animations and scrolling labels");
        perf.add(&pause);
        let idle = self.preference_spin(
            "Idle delay",
            "Minutes the system must have been idle before deck animations pause",
            &["animation_idle_minutes"],
            5.,
            1.,
            120.,
            1.,
            1.,
        );
        idle.set_sensitive(mode == "system-idle");
        let weak_idle = idle.downgrade();
        pause.connect_selected_notify(move |row| {
            if let Some(idle) = weak_idle.upgrade() {
                idle.set_sensitive(row.selected() == 1);
            }
        });
        perf.add(&idle);
        let sys = group(&system, "System settings");
        sys.add(&self.preference_switch(
            "Keep running",
            "Keep the app running after closing the main window",
            &["keep_running"],
            true,
        ));
        let weak = Rc::downgrade(self);
        let autostart = toggle(
            "Autostart",
            super::settings::autostart_path().is_file(),
            move |enabled| {
                if let Some(ui) = weak.upgrade() {
                    ui.job("Autostart updated", false, move || {
                        crate::editor_model::autostart(enabled)?;
                        Ok(Value::Null)
                    });
                }
            },
        );
        autostart.set_subtitle("Start app on system boot");
        sys.add(&autostart);
        sys.add(&self.preference_switch(
            "Lock decks when screen is locked",
            "Works on GNOME, KDE, Cinnamon and Hyprland; other environments use systemd-logind",
            &["auto_lock"],
            true,
        ));
        let fake = group(&developer, "Fake Decks");
        let fake_count = self.preference_spin(
            "Number of fake decks",
            "For testing purposes (might require restart of the app)",
            &["dev", "n-fake-decks"],
            0.,
            0.,
            3.,
            1.,
            1.,
        );
        fake.add(&fake_count);
        let remote = group(&developer, "Remote Decks");
        remote.add(&self.preference_spin(
            "Number of remote decks",
            "Runs a token-protected server on this computer, port 8765 (beta)",
            &["dev", "n-remote-decks"],
            0.,
            0.,
            1.,
            1.,
            1.,
        ));
        let data = group(&developer, "Data path");
        let path = adw::EntryRow::builder()
            .title("Data path (requires restart)")
            .show_apply_button(true)
            .text(self.shared.lock().unwrap().docs.root.to_string_lossy())
            .build();
        let weak = Rc::downgrade(self);
        path.connect_apply(move |row| {
            if let Some(ui) = weak.upgrade() {
                let entered = row.text();
                let path = if let Some(tail) = entered.strip_prefix("~/") {
                    PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(tail)
                } else {
                    PathBuf::from(entered.as_str())
                };
                if path.is_absolute() {
                    row.remove_css_class("error");
                    row.set_text(&path.to_string_lossy());
                    ui.settings_property(vec!["data_path".into()], json!(path));
                } else {
                    row.add_css_class("error");
                    ui.toast("Data path must be absolute");
                }
            }
        });
        let weak = Rc::downgrade(self);
        let open = button("Open", "", move || {
            if let Some(ui) = weak.upgrade() {
                let root = ui.shared.lock().unwrap().docs.root.clone();
                let uri = gio::File::for_path(root).uri();
                gtk::UriLauncher::new(&uri).launch(
                    Some(&ui.window),
                    None::<&gio::Cancellable>,
                    |_| {},
                );
            }
        });
        open.set_valign(gtk::Align::Center);
        path.add_suffix(&open);
        data.add(&path);
        self.preference_plugins(&group(&plugins, "Plugin Settings"));
        self.keep_window("Settings", &window);
    }
    fn preference_fonts(self: &Rc<Self>, group: &adw::PreferencesGroup) {
        let row = adw::ActionRow::builder()
            .title("Default font")
            .subtitle("Default font for labels")
            .build();
        let button = gtk::FontButton::new();
        button.set_valign(gtk::Align::Center);
        let settings = self.shared.lock().unwrap().docs.settings.clone();
        let default = &settings["font_defaults"];
        let mut desc = gtk::pango::FontDescription::new();
        desc.set_family(default["font-family"].as_str().unwrap_or("Liberation Sans"));
        desc.set_absolute_size(
            default["font-size"].as_f64().unwrap_or(15.) * gtk::pango::SCALE as f64,
        );
        desc.set_weight(gtk::pango::Weight::Normal);
        if let Some(weight) = default["font-weight"].as_i64() {
            desc.set_weight(match weight {
                100..=299 => gtk::pango::Weight::Light,
                500..=599 => gtk::pango::Weight::Medium,
                600..=699 => gtk::pango::Weight::Semibold,
                700..=899 => gtk::pango::Weight::Bold,
                900.. => gtk::pango::Weight::Heavy,
                _ => gtk::pango::Weight::Normal,
            });
        }
        desc.set_style(match default["font-style"].as_str().unwrap_or("normal") {
            "italic" => gtk::pango::Style::Italic,
            "oblique" => gtk::pango::Style::Oblique,
            _ => gtk::pango::Style::Normal,
        });
        button.set_font_desc(&desc);
        row.add_suffix(&button);
        let weak = Rc::downgrade(self);
        button.connect_font_set(move |button| {
            if let (Some(ui), Some(desc)) = (weak.upgrade(), button.font_desc()) {
                ui.settings_property(
                    vec!["font_defaults".into(), "font-family".into()],
                    json!(desc.family().map(|s| s.to_string()).unwrap_or_default()),
                );
                ui.settings_property(
                    vec!["font_defaults".into(), "font-size".into()],
                    json!(desc.size() as f64 / gtk::pango::SCALE as f64),
                );
                ui.settings_property(
                    vec!["font_defaults".into(), "font-weight".into()],
                    json!(desc.weight().into_glib()),
                );
                ui.settings_property(
                    vec!["font_defaults".into(), "font-style".into()],
                    json!(match desc.style() {
                        gtk::pango::Style::Italic => "italic",
                        gtk::pango::Style::Oblique => "oblique",
                        _ => "normal",
                    }),
                );
            }
        });
        group.add(&row);
        for (title, subtitle, key, fallback) in [
            (
                "Font color",
                "Default font color",
                "color",
                [255, 255, 255, 255],
            ),
            (
                "Outline color",
                "Default color for the label outline",
                "outline-color",
                [0, 0, 0, 255],
            ),
        ] {
            if key == "outline-color" {
                group.add(&self.preference_spin(
                    "Width of the outline",
                    "Default width of the outline",
                    &["font_defaults", "outline-width"],
                    2.,
                    0.,
                    10.,
                    1.,
                    1.,
                ));
            }
            let row = adw::ActionRow::builder()
                .title(title)
                .subtitle(subtitle)
                .build();
            let button = gtk::ColorButton::new();
            button.set_valign(gtk::Align::Center);
            let values = render::color(
                &default[if key == "color" { "font-color" } else { key }],
                fallback,
            );
            button.set_rgba(&gdk::RGBA::new(
                values[0] as f32 / 255.,
                values[1] as f32 / 255.,
                values[2] as f32 / 255.,
                values[3] as f32 / 255.,
            ));
            row.add_suffix(&button);
            let weak = Rc::downgrade(self);
            button.connect_color_set(move |button| {
                if let Some(ui) = weak.upgrade() {
                    let c = button.rgba();
                    ui.settings_property(
                        vec![
                            "font_defaults".into(),
                            (if key == "color" { "font-color" } else { key }).into(),
                        ],
                        json!(
                            [c.red(), c.green(), c.blue(), c.alpha()]
                                .map(|v| (v * 255.).round() as u8)
                        ),
                    );
                }
            });
            group.add(&row);
        }
    }
    fn custom_repositories(
        self: &Rc<Self>,
        group: &adw::PreferencesGroup,
        kind: &str,
        description: &str,
    ) {
        group.set_description(Some(description));
        let header = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        let enable = gtk::Switch::builder()
            .valign(gtk::Align::Center)
            .active(
                self.shared.lock().unwrap().docs.settings["store"][format!("enable-custom-{kind}")]
                    .as_bool()
                    .unwrap_or(false),
            )
            .build();
        header.append(&enable);
        let add = gtk::Button::from_icon_name("list-add-symbolic");
        add.add_css_class("flat");
        header.append(&add);
        group.set_header_suffix(Some(&header));
        let weak = Rc::downgrade(self);
        let key = format!("enable-custom-{kind}");
        enable.connect_active_notify(move |enable| {
            if let Some(ui) = weak.upgrade() {
                ui.settings_property(vec!["store".into(), key.clone()], json!(enable.is_active()));
            }
        });
        let key = format!("custom-{kind}");
        let entries = self.shared.lock().unwrap().docs.settings["store"][&key]
            .as_array()
            .cloned()
            .unwrap_or_default();
        let repositories = Rc::new(RefCell::new(
            entries.into_iter().enumerate().collect::<Vec<_>>(),
        ));
        for (id, entry) in repositories.borrow().iter() {
            self.repository_row(group, &key, *id, entry, repositories.clone());
        }
        let next_id = Cell::new(repositories.borrow().len());
        let weak = Rc::downgrade(self);
        let target = group.downgrade();
        add.connect_clicked(move |_| {
            if let (Some(ui), Some(group)) = (weak.upgrade(), target.upgrade()) {
                let id = next_id.get();
                next_id.set(id + 1);
                repositories
                    .borrow_mut()
                    .push((id, json!({"url":"", "branch":""})));
                ui.save_repositories(&key, &repositories);
                ui.repository_row(&group, &key, id, &json!({}), repositories.clone());
            }
        });
    }
    fn save_repositories(self: &Rc<Self>, key: &str, entries: &RefCell<Vec<(usize, Value)>>) {
        let values = entries
            .borrow()
            .iter()
            .map(|(_, value)| value.clone())
            .collect::<Vec<_>>();
        self.settings_property(vec!["store".into(), key.into()], json!(values));
    }
    fn repository_row(
        self: &Rc<Self>,
        group: &adw::PreferencesGroup,
        key: &str,
        id: usize,
        entry: &Value,
        repositories: Rc<RefCell<Vec<(usize, Value)>>>,
    ) {
        let row = adw::PreferencesRow::new();
        let content = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        let entries = gtk::Box::new(gtk::Orientation::Vertical, 0);
        entries.set_hexpand(true);
        for (field, title) in [("url", "Repository URL"), ("branch", "Branch")] {
            let value = entry[field].as_str().unwrap_or("");
            let input = adw::EntryRow::builder()
                .title(title)
                .text(value)
                .valign(gtk::Align::Center)
                .build();
            let weak = Rc::downgrade(self);
            let key = key.to_owned();
            let data = repositories.clone();
            input.connect_changed(move |input| {
                if let Some(ui) = weak.upgrade() {
                    let changed = {
                        let mut data = data.borrow_mut();
                        if let Some((_, entry)) = data.iter_mut().find(|(key, _)| *key == id) {
                            entry[field] = json!(input.text().as_str());
                            true
                        } else {
                            false
                        }
                    };
                    if changed {
                        ui.save_repositories(&key, &data);
                    }
                }
            });
            entries.append(&input);
        }
        content.append(&entries);
        let weak = Rc::downgrade(self);
        let weak_group = group.downgrade();
        let weak_row = row.downgrade();
        let key = key.to_owned();
        content.append(&button("", "user-trash-symbolic", move || {
            if let (Some(ui), Some(group), Some(row)) =
                (weak.upgrade(), weak_group.upgrade(), weak_row.upgrade())
            {
                repositories.borrow_mut().retain(|(key, _)| *key != id);
                ui.save_repositories(&key, &repositories);
                group.remove(&row);
            }
        }));
        row.set_child(Some(&content));
        group.add(&row);
    }
    fn preference_plugins(self: &Rc<Self>, group: &adw::PreferencesGroup) {
        let mut native = self.shared.lock().unwrap().plugins.clone();
        for (name, id, assets) in [
            (
                "Deck",
                "DeckPlugin",
                &[
                    "sleep",
                    "sidebar",
                    "light",
                    "increase_brightness",
                    "decrease_brightness",
                    "go_to_previous_page",
                    "folder",
                ][..],
            ),
            (
                "Media",
                "MediaPlugin",
                &["play", "pause", "stop", "next", "previous", "idle"][..],
            ),
            ("OBS", "OBSPlugin", &[][..]),
            (
                "OS",
                "OSPlugin",
                &[
                    "web",
                    "terminal",
                    "keyboard",
                    "mouse",
                    "click",
                    "joystick",
                    "controller",
                    "hourglass_empty-inv",
                ][..],
            ),
            ("Volume Mixer", "VolumeMixer", &[][..]),
        ] {
            let artwork = assets
                .iter()
                .map(|asset| (asset.to_string(), format!("builtin://{id}/{asset}.png")))
                .collect::<std::collections::BTreeMap<_, _>>();
            let manifest = serde_json::from_value(json!({
                "api":1,"id":format!("com_core447_{id}"),"name":name,"version":env!("CARGO_PKG_VERSION"),"executable":"",
                "description":"Native Rust replacement","source":format!("https://github.com/StreamController/{id}"),"actions":[],"assets":artwork
            })).expect("Built-in manifest");
            native.push(deckard_core::plugin::Installed {
                directory: PathBuf::from("builtin:"),
                manifest,
            });
        }
        native.sort_by_key(|plugin| plugin.manifest.name.to_lowercase());
        for plugin in native {
            let manifest = &plugin.manifest;
            let row = adw::ActionRow::builder()
                .title(&manifest.name)
                .subtitle(&manifest.id)
                .build();
            let suffix = gtk::Box::new(gtk::Orientation::Horizontal, 6);
            let weak = Rc::downgrade(self);
            let settings_plugin = plugin.clone();
            let open = button(
                "Open Settings",
                "preferences-desktop-remote-desktop-symbolic",
                move || {
                    if let Some(ui) = weak.upgrade() {
                        if settings_plugin.manifest.id == "com_core447_OBSPlugin" {
                            ui.obs_profiles();
                        } else {
                            ui.plugin_settings(&settings_plugin);
                        }
                    }
                },
            );
            open.set_valign(gtk::Align::Center);
            suffix.append(&open);
            let weak = Rc::downgrade(self);
            let about_plugin = plugin.clone();
            let about = button("About", "", move || {
                if let Some(ui) = weak.upgrade() {
                    let manifest = &about_plugin.manifest;
                    let dialog = adw::AboutDialog::builder()
                        .application_name(&manifest.name)
                        .version(&manifest.version)
                        .comments(&manifest.description)
                        .website(&manifest.source)
                        .build();
                    dialog.present(Some(&ui.window));
                }
            });
            about.set_valign(gtk::Align::Center);
            suffix.append(&about);
            let weak = Rc::downgrade(self);
            let id = manifest.id.clone();
            let name = manifest.name.clone();
            let weak_group = group.downgrade();
            let weak_row = row.downgrade();
            let uninstall = button("", "user-trash-symbolic", move || {
                if let Some(ui) = weak.upgrade() {
                    let confirm = adw::MessageDialog::builder()
                        .transient_for(&ui.window)
                        .modal(true)
                        .heading("Uninstall ?")
                        .body(format!("Are you sure you want to uninstall \"{name}\"?"))
                        .build();
                    confirm.add_response("cancel", "Cancel");
                    confirm.add_response("delete", "Delete");
                    confirm.set_response_appearance("delete", adw::ResponseAppearance::Destructive);
                    let weak = Rc::downgrade(&ui);
                    let id = id.clone();
                    let group = weak_group.clone();
                    let row = weak_row.clone();
                    confirm.connect_response(None, move |_, response| {
                        if response == "delete"
                            && let Some(ui) = weak.upgrade()
                        {
                            let id = id.clone();
                            let group = group.clone();
                            let row = row.clone();
                            ui.submit_then(
                                Work {
                                    key: None,
                                    title: String::new(),
                                    rebuild: true,
                                    execute: Box::new(move |engine| {
                                        let installed = engine
                                            .plugins
                                            .iter()
                                            .find(|p| p.manifest.id == id)
                                            .context("Plugin already removed")?;
                                        let root = engine
                                            .docs
                                            .root
                                            .join("plugins-native")
                                            .canonicalize()?;
                                        let directory = installed.directory.canonicalize()?;
                                        anyhow::ensure!(
                                            directory.parent() == Some(root.as_path()),
                                            "This bundled plugin cannot be uninstalled"
                                        );
                                        std::fs::remove_dir_all(directory)?;
                                        engine.command(&json!({"method":"reload"}))
                                    }),
                                },
                                move |_, result| {
                                    if result.is_ok()
                                        && let (Some(group), Some(row)) =
                                            (group.upgrade(), row.upgrade())
                                    {
                                        group.remove(&row);
                                    }
                                },
                            );
                        }
                    });
                    confirm.present();
                }
            });
            uninstall.set_valign(gtk::Align::Center);
            uninstall.add_css_class("destructive-action");
            let removable = plugin
                .directory
                .starts_with(self.shared.lock().unwrap().docs.root.join("plugins-native"));
            uninstall.set_sensitive(removable);
            if !removable {
                uninstall.set_tooltip_text(Some("Included with Deckard"));
            }
            suffix.append(&uninstall);
            row.add_suffix(&suffix);
            group.add(&row);
        }
    }
    fn plugin_settings(self: &Rc<Self>, plugin: &deckard_core::plugin::Installed) {
        let manifest = &plugin.manifest;
        let dialog = adw::PreferencesDialog::builder()
            .title(format!("{} Settings", manifest.name))
            .width_request(500)
            .height_request(500)
            .build();
        let settings = adw::PreferencesPage::builder()
            .title("Settings")
            .icon_name("preferences-system-symbolic")
            .build();
        dialog.add(&settings);
        let group = adw::PreferencesGroup::new();
        settings.add(&group);
        let values =
            self.shared.lock().unwrap().docs.settings["plugins"][&manifest.id]["settings"].clone();
        for field in &manifest.settings {
            let value = values.get(&field.key).unwrap_or(&field.default);
            let weak = Rc::downgrade(self);
            let id = manifest.id.clone();
            let key = field.key.clone();
            match field.kind.as_str() {
                "bool" => group.add(&toggle(
                    &field.label,
                    value.as_bool().unwrap_or(false),
                    move |v| {
                        if let Some(ui) = weak.upgrade() {
                            ui.settings_property(
                                vec!["plugins".into(), id.clone(), "settings".into(), key.clone()],
                                json!(v),
                            );
                        }
                    },
                )),
                "number" => group.add(&number(
                    &field.label,
                    value.as_f64().unwrap_or(0.),
                    -1e9,
                    1e9,
                    1.,
                    move |v| {
                        if let Some(ui) = weak.upgrade() {
                            ui.settings_property(
                                vec!["plugins".into(), id.clone(), "settings".into(), key.clone()],
                                json!(v),
                            );
                        }
                    },
                )),
                _ => group.add(&text(
                    &field.label,
                    value.as_str().unwrap_or(""),
                    move |v| {
                        if let Some(ui) = weak.upgrade() {
                            ui.settings_property(
                                vec!["plugins".into(), id.clone(), "settings".into(), key.clone()],
                                json!(v),
                            );
                        }
                    },
                )),
            }
        }
        for (kind, title, icon) in [
            ("assets", "Assets", "image-x-generic-symbolic"),
            ("colors", "Colors", "applications-graphics-symbolic"),
        ] {
            let page = adw::PreferencesPage::builder()
                .title(title)
                .icon_name(icon)
                .build();
            dialog.add(&page);
            let group = adw::PreferencesGroup::new();
            page.add(&group);
            let search = gtk::SearchEntry::builder()
                .placeholder_text("Search Asset...")
                .build();
            let main = gtk::Box::new(gtk::Orientation::Vertical, 10);
            main.append(&search);
            group.add(&main);
            let flow = gtk::FlowBox::builder()
                .hexpand(true)
                .orientation(gtk::Orientation::Horizontal)
                .selection_mode(gtk::SelectionMode::Single)
                .valign(gtk::Align::Start)
                .max_children_per_line(3)
                .row_spacing(5)
                .column_spacing(5)
                .build();
            let scroll = gtk::ScrolledWindow::builder()
                .hexpand(true)
                .vexpand(true)
                .child(&flow)
                .build();
            group.add(&scroll);
            let names = if kind == "assets" {
                manifest.assets.keys().cloned().collect::<Vec<_>>()
            } else {
                manifest.colors.keys().cloned().collect::<Vec<_>>()
            };
            for name in names {
                let tile = gtk::FlowBoxChild::new();
                tile.set_widget_name(&name);
                tile.add_css_class("asset-preview");
                margins(&tile, 5);
                let box_ = gtk::Box::new(gtk::Orientation::Vertical, 0);
                let overlay = gtk::Overlay::new();
                let image = gtk::Picture::builder()
                    .width_request(100)
                    .height_request(100)
                    .content_fit(gtk::ContentFit::Contain)
                    .build();
                overlay.set_child(Some(&image));
                box_.append(&overlay);
                box_.append(
                    &gtk::Label::builder()
                        .label(&name)
                        .xalign(0.5)
                        .ellipsize(gtk::pango::EllipsizeMode::End)
                        .max_width_chars(20)
                        .margin_start(20)
                        .margin_end(20)
                        .build(),
                );
                tile.set_child(Some(&box_));
                if kind == "assets" {
                    let stored = self.shared.lock().unwrap().docs.settings["plugins"][&manifest.id]
                        [kind][&name]
                        .clone();
                    let stored = stored
                        .as_str()
                        .or_else(|| stored["path"].as_str())
                        .map(str::to_owned);
                    set_plugin_picture(&image, plugin, &name, stored.as_deref());
                } else {
                    let value = self.shared.lock().unwrap().docs.settings["plugins"][&manifest.id]
                        [kind][&name]
                        .clone();
                    let value = if value.is_null() {
                        &manifest.colors[&name]
                    } else {
                        &value
                    };
                    let rgba = render::color(value, [0, 0, 0, 255]).map(|v| f32::from(v) / 255.);
                    let swatch = gtk::ColorButton::builder()
                        .title("Pick Color")
                        .sensitive(false)
                        .width_request(100)
                        .height_request(100)
                        .rgba(&gdk::RGBA::new(rgba[0], rgba[1], rgba[2], rgba[3]))
                        .build();
                    overlay.set_child(Some(&swatch));
                }
                if kind == "assets" {
                    let edit = gtk::Button::builder()
                        .icon_name("document-edit-symbolic")
                        .halign(gtk::Align::Start)
                        .valign(gtk::Align::Start)
                        .margin_top(5)
                        .margin_start(5)
                        .build();
                    overlay.add_overlay(&edit);
                    let weak = Rc::downgrade(self);
                    let plugin = plugin.clone();
                    let key = name.clone();
                    let picture = image.downgrade();
                    edit.connect_clicked(move |_| {
                        if let Some(ui) = weak.upgrade() {
                            ui.plugin_icon_layout(&plugin, &key, &picture);
                        }
                    });
                }
                let weak = Rc::downgrade(self);
                let id = manifest.id.clone();
                let key = name.clone();
                let button = gtk::Button::builder()
                    .icon_name("edit-undo-symbolic")
                    .halign(gtk::Align::End)
                    .valign(gtk::Align::Start)
                    .margin_top(5)
                    .margin_end(5)
                    .build();
                overlay.add_overlay(&button);
                let original = plugin.clone();
                let picture = image.downgrade();
                let swatch = overlay
                    .child()
                    .and_downcast::<gtk::ColorButton>()
                    .map(|w| w.downgrade());
                button.connect_clicked(move |_| {
                    if let Some(ui) = weak.upgrade() {
                        ui.settings_property(
                            vec!["plugins".into(), id.clone(), kind.into(), key.clone()],
                            Value::Null,
                        );
                        if kind == "assets" {
                            if let Some(picture) = picture.upgrade() {
                                set_plugin_picture(&picture, &original, &key, None);
                            }
                        } else if let Some(swatch) = swatch.as_ref().and_then(|w| w.upgrade()) {
                            let color =
                                render::color(&original.manifest.colors[&key], [0, 0, 0, 255])
                                    .map(|v| f32::from(v) / 255.);
                            swatch
                                .set_rgba(&gdk::RGBA::new(color[0], color[1], color[2], color[3]));
                        }
                    }
                });
                flow.append(&tile);
            }
            let query = search.clone();
            flow.set_filter_func(move |row| {
                row.widget_name()
                    .to_lowercase()
                    .contains(&query.text().to_lowercase())
            });
            let weak = flow.downgrade();
            search.connect_search_changed(move |_| {
                if let Some(flow) = weak.upgrade() {
                    flow.invalidate_filter();
                }
            });
            let weak = Rc::downgrade(self);
            let id = manifest.id.clone();
            flow.connect_child_activated(move |_, row| {
                if let Some(ui) = weak.upgrade() {
                    let name = row.widget_name().to_string();
                    let id = id.clone();
                    let weak = Rc::downgrade(&ui);
                    if kind == "assets" {
                        let picture = widgets_picture(row);
                        ui.choose_file("Icon", false, move |path| {
                            if let Some(ui) = weak.upgrade() {
                                ui.settings_property(
                                    vec!["plugins".into(), id.clone(), kind.into(), name.clone()],
                                    json!(path),
                                );
                                if let Some(picture) = picture.as_ref().and_then(|p| p.upgrade()) {
                                    picture.set_filename(Some(&path));
                                }
                            }
                        });
                    } else {
                        let swatch = widgets_color(row);
                        let initial = swatch.as_ref().and_then(|w| w.upgrade()).map(|w| w.rgba());
                        let chooser = gtk::ColorDialog::builder().with_alpha(true).build();
                        chooser.choose_rgba(
                            Some(&ui.window),
                            initial.as_ref(),
                            gio::Cancellable::NONE,
                            move |result| {
                                if let (Ok(color), Some(ui)) = (result, weak.upgrade()) {
                                    if let Some(swatch) = swatch.as_ref().and_then(|w| w.upgrade())
                                    {
                                        swatch.set_rgba(&color);
                                    }
                                    ui.settings_property(
                                        vec!["plugins".into(), id, kind.into(), name],
                                        json!(
                                            [
                                                color.red(),
                                                color.green(),
                                                color.blue(),
                                                color.alpha()
                                            ]
                                            .map(|v| (v * 255.).round() as u8)
                                        ),
                                    );
                                }
                            },
                        );
                    }
                }
            });
        }
        dialog.present(Some(&self.window));
    }
    fn plugin_icon_layout(
        self: &Rc<Self>,
        plugin: &deckard_core::plugin::Installed,
        name: &str,
        picture: &glib::WeakRef<gtk::Picture>,
    ) {
        let stored =
            self.shared.lock().unwrap().docs.settings["plugins"][&plugin.manifest.id]["assets"]
                [name]
                .clone();
        let path = stored
            .as_str()
            .or_else(|| stored["path"].as_str())
            .map(str::to_owned)
            .unwrap_or_else(|| {
                let original = &plugin.manifest.assets[name];
                if original.starts_with("builtin://") {
                    original.clone()
                } else {
                    plugin
                        .directory
                        .join(original)
                        .to_string_lossy()
                        .into_owned()
                }
            });
        let values = Rc::new(RefCell::new(if stored.is_object() {
            stored
        } else {
            json!({"path":path})
        }));
        let dialog = adw::PreferencesDialog::new();
        let page = adw::PreferencesPage::new();
        let group = adw::PreferencesGroup::new();
        page.add(&group);
        dialog.add(&page);
        let preview = gtk::Picture::builder()
            .width_request(100)
            .height_request(100)
            .halign(gtk::Align::Center)
            .valign(gtk::Align::Center)
            .build();
        if let Some(picture) = picture.upgrade() {
            preview.set_paintable(picture.paintable().as_ref());
        }
        group.add(&preview);
        for (title, key, min, default) in [
            ("Horizontal Align", "halign", -1., 0.),
            ("Vertical Align", "valign", -1., 0.),
            ("Size", "size", 0.1, 1.),
        ] {
            let row = gtk::Box::new(gtk::Orientation::Horizontal, 0);
            row.append(&gtk::Label::new(Some(title)));
            let scale = gtk::Scale::with_range(gtk::Orientation::Horizontal, min, 1., 0.01);
            scale.set_draw_value(true);
            scale.set_digits(2);
            scale.set_hexpand(true);
            scale.set_value(values.borrow()[key].as_f64().unwrap_or(default));
            row.append(&scale);
            group.add(&row);
            let values = values.clone();
            let weak = Rc::downgrade(self);
            let id = plugin.manifest.id.clone();
            let name = name.to_owned();
            scale.connect_value_changed(move |scale| {
                values.borrow_mut()[key] = json!(scale.value());
                if let Some(ui) = weak.upgrade() {
                    ui.settings_property(
                        vec!["plugins".into(), id.clone(), "assets".into(), name.clone()],
                        values.borrow().clone(),
                    );
                }
            });
        }
        dialog.present(Some(&self.window));
    }
}

fn widgets_picture(root: &impl IsA<gtk::Widget>) -> Option<glib::WeakRef<gtk::Picture>> {
    if let Ok(picture) = root
        .clone()
        .upcast::<gtk::Widget>()
        .downcast::<gtk::Picture>()
    {
        return Some(picture.downgrade());
    }
    let mut child = root.first_child();
    while let Some(widget) = child {
        if let Some(picture) = widgets_picture(&widget) {
            return Some(picture);
        }
        child = widget.next_sibling();
    }
    None
}

fn set_plugin_picture(
    picture: &gtk::Picture,
    plugin: &deckard_core::plugin::Installed,
    name: &str,
    custom: Option<&str>,
) {
    if let Some(path) = custom {
        picture.set_filename(Some(path));
        return;
    }
    let original = &plugin.manifest.assets[name];
    if let Some(bytes) = deckard_core::builtin_artwork(original) {
        if let Ok(decoded) = image::load_from_memory(bytes) {
            let rgba = decoded.thumbnail(100, 100).into_rgba8();
            let stride = rgba.width() as usize * 4;
            let texture = gdk::MemoryTexture::new(
                rgba.width() as i32,
                rgba.height() as i32,
                gdk::MemoryFormat::R8g8b8a8,
                &glib::Bytes::from_owned(rgba.into_raw()),
                stride,
            );
            picture.set_paintable(Some(&texture));
        }
    } else {
        picture.set_filename(Some(plugin.directory.join(original)));
    }
}
fn widgets_color(root: &impl IsA<gtk::Widget>) -> Option<glib::WeakRef<gtk::ColorButton>> {
    if let Ok(swatch) = root
        .clone()
        .upcast::<gtk::Widget>()
        .downcast::<gtk::ColorButton>()
    {
        return Some(swatch.downgrade());
    }
    let mut child = root.first_child();
    while let Some(widget) = child {
        if let Some(swatch) = widgets_color(&widget) {
            return Some(swatch);
        }
        child = widget.next_sibling();
    }
    None
}
