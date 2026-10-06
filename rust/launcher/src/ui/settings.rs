use super::*;
use controls::*;

type ProfileRefresh = Rc<dyn Fn(&Rc<Ui>, Option<String>)>;

impl Ui {
    pub(super) fn settings_property(self: &Rc<Self>, path: Vec<String>, value: Value) {
        self.submit(Work {
            key: Some(format!("settings/{path:?}")),
            title: String::new(),
            rebuild: false,
            execute: Box::new(move |engine| {
                let mut settings = engine.docs.settings.clone();
                set_nested(
                    &mut settings,
                    &path.iter().map(String::as_str).collect::<Vec<_>>(),
                    value,
                );
                engine.command(&json!({"method":"put-settings","params":settings}))
            }),
        });
    }
    fn setting_text(self: &Rc<Self>, title: &str, path: &[&str], default: &str) -> adw::EntryRow {
        let value = get_path(&self.shared.lock().unwrap().docs.settings, path)
            .as_str()
            .unwrap_or(default)
            .to_owned();
        let keys = path.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let weak = Rc::downgrade(self);
        text(title, &value, move |value| {
            if let Some(ui) = weak.upgrade() {
                ui.settings_property(keys.clone(), json!(value));
            }
        })
    }
    fn setting_toggle(
        self: &Rc<Self>,
        title: &str,
        path: &[&str],
        default: bool,
    ) -> adw::SwitchRow {
        let value = get_path(&self.shared.lock().unwrap().docs.settings, path)
            .as_bool()
            .unwrap_or(default);
        let keys = path.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let weak = Rc::downgrade(self);
        toggle(title, value, move |value| {
            if let Some(ui) = weak.upgrade() {
                ui.settings_property(keys.clone(), json!(value));
            }
        })
    }
    fn setting_number(
        self: &Rc<Self>,
        title: &str,
        path: &[&str],
        default: f64,
        min: f64,
        max: f64,
    ) -> adw::ActionRow {
        let value = get_path(&self.shared.lock().unwrap().docs.settings, path)
            .as_f64()
            .unwrap_or(default);
        let keys = path.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let weak = Rc::downgrade(self);
        number(title, value, min, max, 1., move |value| {
            if let Some(ui) = weak.upgrade() {
                ui.settings_property(keys.clone(), json!(value as u64));
            }
        })
    }
    fn setting_file(self: &Rc<Self>, title: &str, path: &[&str]) -> adw::EntryRow {
        let value = get_path(&self.shared.lock().unwrap().docs.settings, path)
            .as_str()
            .unwrap_or("")
            .to_owned();
        let keys = path.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let weak = Rc::downgrade(self);
        file_row(&self.window, title, &value, move |value| {
            if let Some(ui) = weak.upgrade() {
                ui.settings_property(keys.clone(), json!(value));
            }
        })
    }
    pub(super) fn settings(self: &Rc<Self>) {
        let (dialog, body) = self.dialog("Settings");
        let group = adw::PreferencesGroup::new();
        group.set_title("General");
        body.append(&group);
        group.add(&self.setting_toggle(
            "Keep running when the window closes",
            &["keep_running"],
            true,
        ));
        group.add(&self.setting_toggle("Lock decks with the desktop", &["auto_lock"], true));
        group.add(&self.setting_toggle("Remember active states", &["persist_states"], true));
        group.add(&self.setting_toggle("Shrink keys while pressed", &["shrink_on_press"], true));
        group.add(&self.setting_number("Render cache budget (MiB)", &["cache_mib"], 64., 4., 512.));
        group.add(&self.setting_number(
            "Long press threshold (ms)",
            &["hold_ms"],
            500.,
            100.,
            10000.,
        ));
        let scheme = self.shared.lock().unwrap().docs.settings["appearance"]
            .as_str()
            .unwrap_or("dark")
            .to_owned();
        let weak = Rc::downgrade(self);
        group.add(&choice(
            "Appearance",
            &["dark", "light", "system"],
            &scheme,
            move |value| {
                adw::StyleManager::default().set_color_scheme(match value.as_str() {
                    "light" => adw::ColorScheme::ForceLight,
                    "system" => adw::ColorScheme::Default,
                    _ => adw::ColorScheme::ForceDark,
                });
                if let Some(ui) = weak.upgrade() {
                    ui.settings_property(vec!["appearance".into()], json!(value));
                }
            },
        ));
        let desktop = adw::PreferencesGroup::new();
        desktop.set_title("Desktop");
        body.append(&desktop);
        let enabled = autostart_path().is_file();
        let weak = Rc::downgrade(self);
        desktop.add(&toggle("Start at login", enabled, move |enabled| {
            if let Some(ui) = weak.upgrade() {
                ui.job("Autostart updated", false, move || {
                    crate::editor_model::autostart(enabled)?;
                    Ok(Value::Null)
                });
            }
        }));
        let weak = Rc::downgrade(self);
        let row = adw::ActionRow::builder()
            .title("Open data folder")
            .activatable(true)
            .build();
        row.connect_activated(move |_| {
            if let Some(ui) = weak.upgrade() {
                let path = ui.shared.lock().unwrap().docs.root.clone();
                if let Err(e) = gio::AppInfo::launch_default_for_uri(
                    &gio::File::for_path(path).uri(),
                    None::<&gio::AppLaunchContext>,
                ) {
                    ui.toast(&e.to_string());
                }
            }
        });
        desktop.add(&row);
        let helper = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|p| p.join("install-udev.sh")))
            .filter(|p| p.exists());
        if let Some(helper) = helper {
            let weak = Rc::downgrade(self);
            let row = adw::ActionRow::builder()
                .title("Enable USB access…")
                .subtitle("Install device access rules, then reconnect the deck")
                .activatable(true)
                .build();
            row.connect_activated(move |_| {
                if let Some(ui) = weak.upgrade() {
                    let helper = helper.clone();
                    ui.job(
                        "USB access rules installed. Reconnect the deck.",
                        false,
                        move || {
                            deckard_core::desktop::run_command(
                                &["pkexec".into(), helper.to_string_lossy().into_owned()],
                                Duration::from_secs(120),
                            )?;
                            Ok(Value::Null)
                        },
                    );
                }
            });
            desktop.add(&row);
        }
        let migration = adw::PreferencesGroup::new();
        migration.set_title("Legacy plugin migration");
        migration.set_description(Some("OS, Deck, Media, OBS and VolumeMixer actions have native replacements. Original documents are backed up before conversion."));
        body.append(&migration);
        for (title, method) in [
            ("Inspect legacy actions", "inspect-legacy-actions"),
            ("Migrate supported actions", "migrate-legacy-actions"),
        ] {
            let row = adw::ActionRow::builder()
                .title(title)
                .activatable(true)
                .build();
            let weak = Rc::downgrade(self);
            row.connect_activated(move |_| {
                if let Some(ui) = weak.upgrade() {
                    ui.command(method, json!({}), "migration", true);
                }
            });
            migration.add(&row);
        }
        self.integration_rows(&body);
        self.automatic_pages(&body);
        self.ai_settings(&body);
        let advanced = adw::PreferencesGroup::new();
        advanced.set_title("Advanced");
        body.append(&advanced);
        let weak = Rc::downgrade(self);
        let row = adw::ActionRow::builder()
            .title("Settings document")
            .subtitle("For options not exposed above")
            .activatable(true)
            .build();
        row.connect_activated(move |_| {
            if let Some(ui) = weak.upgrade() {
                let settings = ui.shared.lock().unwrap().docs.settings.clone();
                let original = settings.clone();
                let weak = Rc::downgrade(&ui);
                ui.json_dialog("Advanced settings", settings, move |settings| {
                    if let Some(ui) = weak.upgrade() {
                        let original = original.clone();
                        ui.submit(Work {
                            key: None,
                            title: "Settings saved".into(),
                            rebuild: true,
                            execute: Box::new(move |engine| {
                                anyhow::ensure!(
                                    engine.docs.settings == original,
                                    "Settings changed while editing; reopen before saving"
                                );
                                engine.command(&json!({"method":"put-settings","params":settings}))
                            }),
                        });
                    }
                });
            }
        });
        advanced.add(&row);
        dialog.present(Some(&self.window));
    }
    pub(super) fn device_settings(self: &Rc<Self>) {
        let serial = self.selection.borrow().serial.clone();
        let device = self.shared.lock().unwrap().devices.get(&serial).cloned();
        let Some(device) = device else {
            self.toast("Connect a deck first");
            return;
        };
        let (dialog, body) = self.dialog("Deck Settings");
        let group = adw::PreferencesGroup::new();
        group.set_title(&format!("{:?} · {serial}", device.kind));
        body.append(&group);
        group.add(&self.setting_text("Name", &["devices", &serial, "name"], &serial));
        let weak = Rc::downgrade(self);
        let target = serial.clone();
        group.add(&number(
            "Brightness",
            device.brightness as f64,
            0.,
            100.,
            1.,
            move |v| {
                if let Some(ui) = weak.upgrade() {
                    ui.command(
                        "set-brightness",
                        json!({"serial":target,"value":v}),
                        "",
                        false,
                    );
                }
            },
        ));
        let rotation = self.shared.lock().unwrap().docs.settings["devices"][&serial]["rotation"]
            .as_u64()
            .unwrap_or(0)
            .to_string();
        let weak = Rc::downgrade(self);
        let target = serial.clone();
        group.add(&choice(
            "Rotation",
            &["0", "90", "180", "270"],
            &rotation,
            move |v| {
                if let Some(ui) = weak.upgrade() {
                    ui.settings_property(
                        vec!["devices".into(), target.clone(), "rotation".into()],
                        json!(v.parse::<u16>().unwrap_or(0)),
                    );
                }
            },
        ));
        group.add(&self.setting_number(
            "Animation FPS cap · 0 = Auto",
            &["devices", &serial, "max_fps"],
            0.,
            0.,
            120.,
        ));
        let size = adw::ActionRow::builder()
            .title("Native key resolution")
            .subtitle(format!(
                "{} × {} pixels · detected automatically",
                device.key_size.0, device.key_size.1
            ))
            .build();
        group.add(&size);
        let weak = Rc::downgrade(self);
        let target = serial.clone();
        let row = adw::ActionRow::builder()
            .title(if device.sleeping {
                "Wake deck"
            } else {
                "Sleep deck"
            })
            .activatable(true)
            .build();
        row.connect_activated(move |_| {
            if let Some(ui) = weak.upgrade() {
                let sleeping = ui
                    .shared
                    .lock()
                    .unwrap()
                    .devices
                    .get(&target)
                    .is_some_and(|d| d.sleeping);
                ui.command(
                    if sleeping { "wake" } else { "sleep" },
                    json!({"serial":target}),
                    "",
                    false,
                );
            }
        });
        group.add(&row);
        let saver = adw::PreferencesGroup::new();
        saver.set_title("Screensaver");
        body.append(&saver);
        saver.add(&self.setting_toggle(
            "Enable screensaver",
            &["devices", &serial, "screensaver", "enable"],
            false,
        ));
        saver.add(&self.setting_number(
            "Delay (minutes)",
            &["devices", &serial, "screensaver", "time-delay"],
            5.,
            1.,
            1440.,
        ));
        saver.add(&self.setting_number(
            "Screensaver brightness",
            &["devices", &serial, "screensaver", "brightness"],
            30.,
            0.,
            100.,
        ));
        saver.add(&self.setting_file(
            "Image, GIF or video",
            &["devices", &serial, "screensaver", "media-path"],
        ));
        let background = adw::PreferencesGroup::new();
        background.set_title("Deck wallpaper");
        body.append(&background);
        background.add(&self.setting_toggle(
            "Show wallpaper",
            &["devices", &serial, "background", "show"],
            true,
        ));
        background.add(&self.setting_file(
            "Image, GIF or video",
            &["devices", &serial, "background", "media-path"],
        ));
        background.add(&self.setting_toggle(
            "Extend to touchscreen",
            &["devices", &serial, "background", "extend-to-touchscreen"],
            false,
        ));
        dialog.present(Some(&self.window));
    }
    fn integration_rows(self: &Rc<Self>, body: &gtk::Box) {
        let group = adw::PreferencesGroup::new();
        group.set_title("Integrations");
        body.append(&group);
        let row = adw::ActionRow::builder()
            .title("OBS Studio connections")
            .subtitle("Profiles, authentication and available scenes")
            .activatable(true)
            .build();
        let weak = Rc::downgrade(self);
        row.connect_activated(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.obs_profiles();
            }
        });
        group.add(&row);
        let row = adw::ActionRow::builder()
            .title("Media players")
            .subtitle("Refresh MPRIS players for action selection")
            .activatable(true)
            .build();
        let weak = Rc::downgrade(self);
        row.connect_activated(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.job("media-players", false, || {
                    Ok(json!(
                        deckard_core::mpris::snapshot()?
                            .into_iter()
                            .map(|p| p.identity)
                            .collect::<Vec<_>>()
                    ))
                });
            }
        });
        group.add(&row);
    }
    pub(super) fn obs_profiles(self: &Rc<Self>) {
        let (dialog, body) = self.dialog("OBS Connections");
        let settings = self.shared.lock().unwrap().docs.settings.clone();
        let profiles = settings["obs"]["connections"]
            .as_object()
            .cloned()
            .unwrap_or_default();
        let mut ids = profiles.keys().cloned().collect::<Vec<_>>();
        if ids.is_empty() {
            ids.push("default".into());
        }
        let profiles = Rc::new(RefCell::new(profiles));
        let selected = Rc::new(RefCell::new(ids[0].clone()));
        let refs = ids.iter().map(String::as_str).collect::<Vec<_>>();
        let group = adw::PreferencesGroup::new();
        body.append(&group);
        let fields = Rc::new(RefCell::new(profiles.borrow().get(&ids[0]).cloned().unwrap_or_else(
            || json!({"name":"Default","host":"localhost","port":4455,"password":"","tls":false}),
        )));
        let name = adw::EntryRow::builder()
            .title("Name")
            .text(fields.borrow()["name"].as_str().unwrap_or("Default"))
            .build();
        let host = adw::EntryRow::builder()
            .title("Host")
            .text(fields.borrow()["host"].as_str().unwrap_or("localhost"))
            .build();
        let password = adw::PasswordEntryRow::builder()
            .title("Password")
            .text(fields.borrow()["password"].as_str().unwrap_or(""))
            .build();
        let port = gtk::SpinButton::with_range(1., 65535., 1.);
        port.set_value(fields.borrow()["port"].as_f64().unwrap_or(4455.));
        port.set_valign(gtk::Align::Center);
        let tls = adw::SwitchRow::builder()
            .title("TLS (wss)")
            .active(fields.borrow()["tls"].as_bool().unwrap_or(false))
            .build();
        let loading = Rc::new(Cell::new(false));
        let chosen = selected.clone();
        let values = fields.clone();
        let guarded = loading.clone();
        let widgets = (
            name.clone(),
            host.clone(),
            password.clone(),
            port.clone(),
            tls.clone(),
        );
        let available = profiles.clone();
        let profile_choice = choice("Profile", &refs, &ids[0], move |id| {
            *chosen.borrow_mut() = id.clone();
            let value = available
                .borrow()
                .get(&id)
                .cloned()
                .unwrap_or_else(|| json!({}));
            guarded.set(true);
            widgets.0.set_text(value["name"].as_str().unwrap_or(""));
            widgets.1.set_text(
                value["host"]
                    .as_str()
                    .or_else(|| value["ip"].as_str())
                    .unwrap_or("localhost"),
            );
            widgets.2.set_text(value["password"].as_str().unwrap_or(""));
            widgets.3.set_value(value["port"].as_f64().unwrap_or(4455.));
            widgets
                .4
                .set_active(value["tls"].as_bool().unwrap_or(false));
            *values.borrow_mut() = value;
            guarded.set(false);
        });
        profile_choice.set_widget_name("obs-profile");
        group.add(&profile_choice);
        let weak_choice = profile_choice.downgrade();
        let available = profiles.clone();
        let chosen = selected.clone();
        let refresh_profiles: ProfileRefresh = Rc::new(move |ui, select| {
            let Some(row) = weak_choice.upgrade() else {
                return;
            };
            let profiles = ui.shared.lock().unwrap().docs.settings["obs"]["connections"]
                .as_object()
                .cloned()
                .unwrap_or_default();
            let mut ids = profiles.keys().cloned().collect::<Vec<_>>();
            if ids.is_empty() {
                ids.push("default".into());
            }
            *available.borrow_mut() = profiles;
            let selected = select.unwrap_or_else(|| chosen.borrow().clone());
            let index = ids.iter().position(|id| id == &selected).unwrap_or(0);
            let model = gtk::StringList::new(&ids.iter().map(String::as_str).collect::<Vec<_>>());
            row.set_model(Some(&model));
            row.set_selected(index as u32);
        });
        group.add(&name);
        group.add(&host);
        group.add(&password);
        let row = adw::ActionRow::builder().title("Port").build();
        row.add_suffix(&port);
        group.add(&row);
        group.add(&tls);
        for (row, key) in [(name.clone(), "name"), (host.clone(), "host")] {
            let values = fields.clone();
            let loading = loading.clone();
            row.connect_changed(move |row| {
                if !loading.get() {
                    values.borrow_mut()[key] = json!(row.text().as_str());
                }
            });
        }
        let values = fields.clone();
        let guarded = loading.clone();
        password.connect_changed(move |row| {
            if !guarded.get() {
                values.borrow_mut()["password"] = json!(row.text().as_str());
            }
        });
        let values = fields.clone();
        let guarded = loading.clone();
        port.connect_value_changed(move |spin| {
            if !guarded.get() {
                values.borrow_mut()["port"] = json!(spin.value() as u16);
            }
        });
        let values = fields.clone();
        let guarded = loading.clone();
        tls.connect_active_notify(move |row| {
            if !guarded.get() {
                values.borrow_mut()["tls"] = json!(row.is_active());
            }
        });
        let actions = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        body.append(&actions);
        let weak = Rc::downgrade(self);
        let values = fields.clone();
        let id = selected.clone();
        let refresh = refresh_profiles.clone();
        actions.append(&button("Save", "document-save-symbolic", move || {
            if let Some(ui) = weak.upgrade() {
                let id = id.borrow().clone();
                let profile = values.borrow().clone();
                let refreshed = refresh.clone();
                ui.save_obs_profile(id, Some(profile), move |ui, _| {
                    refreshed(ui, None);
                });
            }
        }));
        let weak = Rc::downgrade(self);
        let id = selected.clone();
        actions.append(&button(
            "Make default",
            "emblem-default-symbolic",
            move || {
                if let Some(ui) = weak.upgrade() {
                    ui.settings_property(
                        vec!["obs".into(), "default_connection".into()],
                        json!(*id.borrow()),
                    );
                }
            },
        ));
        let weak = Rc::downgrade(self);
        let values = fields.clone();
        let id = selected.clone();
        actions.append(&button(
            "Test / refresh",
            "view-refresh-symbolic",
            move || {
                if let Some(ui) = weak.upgrade() {
                    let profile = values.borrow().clone();
                    let id = id.borrow().clone();
                    ui.job("obs-choices", false, move || {
                        let mut client = deckard_core::obs::connect(&profile, 0)?;
                        let mut choices = json!({"connection":id});
                        for (key, request) in [
                            ("scenes", "GetSceneList"),
                            ("inputs", "GetInputList"),
                            ("collections", "GetSceneCollectionList"),
                        ] {
                            choices[key] = client.query(request, json!({}))?;
                        }
                        for scene in choices["scenes"]["scenes"]
                            .as_array()
                            .cloned()
                            .unwrap_or_default()
                        {
                            if let Some(name) = scene["sceneName"].as_str() {
                                if let Ok(items) =
                                    client.query("GetSceneItemList", json!({"sceneName":name}))
                                {
                                    choices["items"][name] = items;
                                }
                                if let Ok(filters) =
                                    client.query("GetSourceFilterList", json!({"sourceName":name}))
                                {
                                    choices["filters"][name] = filters;
                                }
                            }
                        }
                        Ok(choices)
                    });
                }
            },
        ));
        let weak = Rc::downgrade(self);
        let values = fields.clone();
        let refresh = refresh_profiles.clone();
        actions.append(&button("Save as new", "list-add-symbolic", move || {
            if let Some(ui) = weak.upgrade() {
                let id = format!(
                    "profile-{}",
                    std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_nanos()
                );
                let selected = id.clone();
                let refreshed = refresh.clone();
                ui.save_obs_profile(id, Some(values.borrow().clone()), move |ui, result| {
                    if result.is_ok() {
                        refreshed(ui, Some(selected));
                    }
                });
            }
        }));
        let weak = Rc::downgrade(self);
        let id = selected.clone();
        let refresh = refresh_profiles.clone();
        actions.append(&button("Delete", "user-trash-symbolic", move || {
            if let Some(ui) = weak.upgrade() {
                let id = id.borrow().clone();
                let refreshed = refresh.clone();
                ui.save_obs_profile(id, None, move |ui, _| {
                    refreshed(ui, None);
                });
            }
        }));
        dialog.present(Some(&self.window));
    }
    fn save_obs_profile(
        self: &Rc<Self>,
        id: String,
        profile: Option<Value>,
        done: impl FnOnce(&Rc<Ui>, &Result<Value>) + 'static,
    ) {
        self.submit_then(
            Work {
                key: None,
                title: String::new(),
                rebuild: false,
                execute: Box::new(move |engine| {
                    let mut settings = engine.docs.settings.clone();
                    if let Some(profile) = profile {
                        set_nested(&mut settings, &["obs", "connections", &id], profile);
                    } else {
                        if let Some(profiles) = settings["obs"]["connections"].as_object_mut() {
                            profiles.remove(&id);
                        }
                        if settings["obs"]["default_connection"] == id
                            && let Some(obs) = settings["obs"].as_object_mut()
                        {
                            obs.remove("default_connection");
                        }
                    }
                    engine.command(&json!({"method":"put-settings", "params":settings}))
                }),
            },
            done,
        );
    }
    fn automatic_pages(self: &Rc<Self>, body: &gtk::Box) {
        let list = gtk::Box::new(gtk::Orientation::Vertical, 0);
        list.set_widget_name("automatic-rules");
        body.append(&list);
        self.populate_rules(&list);
    }
    fn populate_rules(self: &Rc<Self>, list: &gtk::Box) {
        clear(list);
        list.set_sensitive(true);
        let group = adw::PreferencesGroup::new();
        group.set_title("Automatic page switching");
        list.append(&group);
        let rules = self.shared.lock().unwrap().docs.settings["rules"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        for (index, rule) in rules.iter().enumerate() {
            let row = adw::ExpanderRow::builder()
                .title(rule["page"].as_str().unwrap_or("Rule"))
                .subtitle(rule["class"].as_str().unwrap_or(".*"))
                .build();
            for (key, title) in [
                ("class", "Window class regex"),
                ("title", "Window title regex"),
                ("page", "Page"),
                ("serial", "Device serial · empty = all"),
            ] {
                let weak = Rc::downgrade(self);
                let weak_row = row.downgrade();
                let value = rule[key]
                    .as_str()
                    .unwrap_or(if key == "class" || key == "title" {
                        ".*"
                    } else {
                        ""
                    });
                row.add_row(&text(title, value, move |v| {
                    if let Some(row) = weak_row.upgrade() {
                        match key {
                            "page" => row.set_title(&v),
                            "class" => row.set_subtitle(&v),
                            _ => {}
                        }
                    }
                    if let Some(ui) = weak.upgrade() {
                        ui.rule_edit(index, Some((key.into(), json!(v))));
                    }
                }));
            }
            let weak = Rc::downgrade(self);
            let weak_list = list.downgrade();
            let remove = adw::ActionRow::builder()
                .title("Remove rule")
                .activatable(true)
                .build();
            remove.set_widget_name(&format!("remove-rule-{index}"));
            remove.connect_activated(move |_| {
                if let (Some(ui), Some(list)) = (weak.upgrade(), weak_list.upgrade()) {
                    ui.change_rule_list(&list, Some(index));
                }
            });
            row.add_row(&remove);
            group.add(&row);
        }
        let weak = Rc::downgrade(self);
        let weak_list = list.downgrade();
        let add = adw::ActionRow::builder()
            .title("Add rule")
            .activatable(true)
            .build();
        add.set_widget_name("add-rule");
        add.connect_activated(move |_| {
            if let (Some(ui), Some(list)) = (weak.upgrade(), weak_list.upgrade()) {
                ui.change_rule_list(&list, None);
            }
        });
        group.add(&add);
    }
    fn change_rule_list(self: &Rc<Self>, list: &gtk::Box, remove: Option<usize>) {
        list.set_sensitive(false);
        let weak_list = list.downgrade();
        let page = self.selection.borrow().page.clone();
        self.submit_then(
            Work {
                key: None,
                title: String::new(),
                rebuild: false,
                execute: Box::new(move |engine| {
                    let mut settings = engine.docs.settings.clone();
                    let mut rules = settings["rules"].as_array().cloned().unwrap_or_default();
                    if let Some(index) = remove {
                        anyhow::ensure!(index < rules.len(), "Rule no longer exists");
                        rules.remove(index);
                    } else {
                        rules.push(json!({"page":page,"class":".*","title":".*"}));
                    }
                    settings["rules"] = json!(rules);
                    engine.command(&json!({"method":"put-settings","params":settings}))
                }),
            },
            move |ui, _| {
                if let Some(list) = weak_list.upgrade() {
                    ui.populate_rules(&list);
                }
            },
        );
    }
    fn rule_edit(self: &Rc<Self>, index: usize, field: Option<(String, Value)>) {
        self.submit(Work {
            key: field.as_ref().map(|(key, _)| format!("rule/{index}/{key}")),
            title: String::new(),
            rebuild: false,
            execute: Box::new(move |engine| {
                let mut settings = engine.docs.settings.clone();
                let rules = settings["rules"].as_array_mut().context("Rules missing")?;
                anyhow::ensure!(index < rules.len(), "Rule no longer exists");
                if let Some((key, value)) = field {
                    if key == "serial" && value == "" {
                        rules[index]
                            .as_object_mut()
                            .context("Invalid rule")?
                            .remove(&key);
                    } else {
                        rules[index][key] = value;
                    }
                }
                engine.command(&json!({"method":"put-settings","params":settings}))
            }),
        });
    }
    fn ai_settings(self: &Rc<Self>, body: &gtk::Box) {
        let group = adw::PreferencesGroup::new();
        group.set_title("AI page assistant");
        body.append(&group);
        group.add(&self.setting_toggle("Enable page suggestions", &["ai", "enabled"], false));
        let endpoint = adw::EntryRow::builder()
            .title("Endpoint")
            .text("https://api.openai.com/v1/chat/completions")
            .build();
        let model = adw::EntryRow::builder().title("Model").build();
        let key = adw::PasswordEntryRow::builder()
            .title("API key · kept only in memory")
            .build();
        let prompt = adw::EntryRow::builder().title("Describe your page").build();
        group.add(&endpoint);
        group.add(&model);
        group.add(&key);
        group.add(&prompt);
        let weak = Rc::downgrade(self);
        let generate = adw::ActionRow::builder()
            .title("Generate suggestion")
            .activatable(true)
            .build();
        generate.connect_activated(move |_| {
            if let Some(ui) = weak.upgrade() {
                if !ui.shared.lock().unwrap().docs.settings["ai"]["enabled"]
                    .as_bool()
                    .unwrap_or(false)
                {
                    ui.toast("Enable page suggestions first");
                    return;
                }
                let endpoint = endpoint.text().to_string();
                let model = model.text().to_string();
                let key = key.text().to_string();
                let prompt = prompt.text().to_string();
                ui.job("ai-proposal", false, move || {
                    deckard_core::store::Network::new()?.ai(&endpoint, &key, &model, &prompt)
                });
            }
        });
        group.add(&generate);
    }
    pub(super) fn review_ai(self: &Rc<Self>, proposal: Value) {
        let (dialog, body) = self.dialog("Review page suggestion");
        let view = gtk::TextView::builder()
            .editable(false)
            .monospace(true)
            .wrap_mode(gtk::WrapMode::WordChar)
            .height_request(350)
            .build();
        view.buffer()
            .set_text(&serde_json::to_string_pretty(&proposal).unwrap_or_default());
        body.append(&view);
        let allow =
            gtk::CheckButton::with_label("Allow commands, typing and hotkeys in this suggestion");
        body.append(&allow);
        let page = self.selection.borrow().page.clone();
        let weak = Rc::downgrade(self);
        let close = dialog.downgrade();
        body.append(&button(
            "Apply to selected page",
            "document-save-symbolic",
            move || {
                if let Some(ui) = weak.upgrade() {
                    if !allow.is_active() && crate::editor_model::has_privileged_action(&proposal) {
                        ui.toast("Review and allow commands, typing and hotkeys before applying");
                        return;
                    }
                    ui.command(
                        "put-page",
                        json!({"page":page,"document":proposal}),
                        "Suggestion applied",
                        true,
                    );
                    if let Some(dialog) = close.upgrade() {
                        dialog.close();
                    }
                }
            },
        ));
        dialog.present(Some(&self.window));
    }
}
fn autostart_path() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".config")
        })
        .join("autostart/deckard.desktop")
}
