use super::*;
use controls::*;

type ProfileRefresh = Rc<dyn Fn(&Rc<Ui>, Option<String>)>;

impl Ui {
    pub(super) fn settings_property(self: &Rc<Self>, path: Vec<String>, value: Value) {
        if path == ["ui", "allow-white-mode"] {
            adw::StyleManager::default().set_color_scheme(if value == true {
                adw::ColorScheme::Default
            } else {
                adw::ColorScheme::ForceDark
            });
        }
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
                let result = engine.command(&json!({"method":"put-settings","params":settings}))?;
                if path == ["ui", "tray-icon"] {
                    crate::tray::set_visible(
                        engine.docs.settings["ui"]["tray-icon"]
                            .as_bool()
                            .unwrap_or(true),
                    );
                }
                Ok(result)
            }),
        });
    }
    pub(super) fn obs_profiles(self: &Rc<Self>) {
        let dialog = adw::PreferencesDialog::builder()
            .title("OBS Settings")
            .width_request(500)
            .height_request(500)
            .build();
        for (title, icon) in [
            ("Settings", "preferences-system-symbolic"),
            ("Assets", "image-x-generic-symbolic"),
            ("Colors", "applications-graphics-symbolic"),
        ] {
            let page = adw::PreferencesPage::builder()
                .title(title)
                .icon_name(icon)
                .build();
            if title == "Settings" {
                page.add(&self.obs_settings_area());
            }
            dialog.add(&page);
        }
        dialog.present(Some(&self.window));
    }
    fn obs_settings_area(self: &Rc<Self>) -> adw::PreferencesGroup {
        let group = adw::PreferencesGroup::new();
        let main = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        group.add(&main);
        let left = gtk::Box::new(gtk::Orientation::Vertical, 6);
        left.set_size_request(200, -1);
        let list = gtk::ListBox::new();
        list.set_widget_name("obs-profiles");
        list.set_selection_mode(gtk::SelectionMode::Single);
        left.append(
            &gtk::ScrolledWindow::builder()
                .hscrollbar_policy(gtk::PolicyType::Never)
                .vscrollbar_policy(gtk::PolicyType::Automatic)
                .height_request(200)
                .child(&list)
                .build(),
        );
        let add = gtk::Button::with_label("Add Profile");
        add.set_widget_name("Add Profile");
        left.append(&add);
        main.append(&left);
        let right = gtk::Box::new(gtk::Orientation::Vertical, 6);
        right.set_hexpand(true);
        main.append(&right);
        let name = adw::EntryRow::builder().title("Profile Name").build();
        let host = adw::EntryRow::builder().title("IP Address").build();
        let port = adw::SpinRow::with_range(0., 65535., 1.);
        port.set_title("Port");
        let password = adw::PasswordEntryRow::builder().title("Password").build();
        for row in [
            name.clone().upcast::<gtk::Widget>(),
            host.clone().upcast(),
            port.clone().upcast(),
            password.clone().upcast(),
        ] {
            right.append(&row);
        }
        let status = gtk::Label::builder().halign(gtk::Align::Start).build();
        right.append(&status);
        let actions = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        right.append(&actions);
        let test = gtk::Button::with_label("Test Connection");
        let delete = gtk::Button::with_label("Delete Profile");
        let save = gtk::Button::with_label("Save Profile");
        delete.set_widget_name("Delete Profile");
        save.set_widget_name("Save Profile");
        save.add_css_class("suggested-action");
        for button in [&test, &delete, &save] {
            actions.append(button);
        }
        let profiles = Rc::new(RefCell::new(
            self.shared.lock().unwrap().docs.settings["obs"]["connections"]
                .as_object()
                .cloned()
                .unwrap_or_default(),
        ));
        let selected = Rc::new(RefCell::new(None::<String>));
        let values = Rc::new(RefCell::new(json!({})));
        let available = profiles.clone();
        let chosen = selected.clone();
        let draft = values.clone();
        let editor = right.downgrade();
        let fields = (name.clone(), host.clone(), port.clone(), password.clone());
        list.connect_row_selected(move |_, row| {
            let id = row.map(|row| row.widget_name().to_string());
            let value = id
                .as_ref()
                .and_then(|id| available.borrow().get(id).cloned())
                .unwrap_or_else(|| json!({}));
            if let Some(editor) = editor.upgrade() {
                editor.set_sensitive(id.is_some());
            }
            fields.0.set_text(value["name"].as_str().unwrap_or(""));
            fields.1.set_text(
                value["host"]
                    .as_str()
                    .or_else(|| value["ip"].as_str())
                    .unwrap_or("localhost"),
            );
            fields.2.set_value(value["port"].as_f64().unwrap_or(4455.));
            fields.3.set_text(value["password"].as_str().unwrap_or(""));
            *chosen.borrow_mut() = id;
            *draft.borrow_mut() = value;
        });
        let weak_list = list.downgrade();
        let available = profiles.clone();
        let chosen = selected.clone();
        let refresh: ProfileRefresh = Rc::new(move |_, select| {
            let Some(list) = weak_list.upgrade() else {
                return;
            };
            let select = select.or_else(|| chosen.borrow().clone());
            while let Some(child) = list.first_child() {
                list.remove(&child);
            }
            for (id, profile) in available.borrow().iter() {
                let row = gtk::ListBoxRow::new();
                row.set_widget_name(id);
                let label = gtk::Label::builder()
                    .label(profile["name"].as_str().unwrap_or("Unnamed"))
                    .halign(gtk::Align::Start)
                    .margin_top(6)
                    .margin_bottom(6)
                    .margin_start(6)
                    .margin_end(6)
                    .build();
                row.set_child(Some(&label));
                list.append(&row);
                if select.as_deref() == Some(id.as_str()) {
                    list.select_row(Some(&row));
                }
            }
            if list.selected_row().is_none() {
                list.select_row(list.row_at_index(0).as_ref());
            }
        });
        right.set_sensitive(false);
        refresh(self, None);
        let weak = Rc::downgrade(self);
        let available = profiles.clone();
        let refresh_add = refresh.clone();
        add.connect_clicked(move |_| {
            if let Some(ui) = weak.upgrade() {
                let id = format!("profile-{}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_nanos());
                let value = json!({"id":id,"name":"New Profile","host":"localhost","port":4455,"password":""});
                available.borrow_mut().insert(id.clone(), value.clone());
                refresh_add(&ui, Some(id.clone()));
                ui.save_obs_profile(id, Some(value), |_, _| {});
            }
        });
        let weak = Rc::downgrade(self);
        let chosen = selected.clone();
        let available = profiles.clone();
        let values_save = values.clone();
        let refresh_save = refresh.clone();
        let fields = (name.clone(), host.clone(), port.clone(), password.clone());
        save.connect_clicked(move |_| {
            let id = chosen.borrow().clone();
            if let (Some(ui), Some(id)) = (weak.upgrade(), id) {
                let mut value = values_save.borrow().clone();
                set_nested(&mut value, &["name"], json!(fields.0.text().as_str()));
                value["host"] = json!(fields.1.text().as_str());
                value["ip"] = value["host"].clone();
                value["port"] = json!(fields.2.value() as u16);
                value["password"] = json!(fields.3.text().as_str());
                available.borrow_mut().insert(id.clone(), value.clone());
                refresh_save(&ui, Some(id.clone()));
                ui.save_obs_profile(id, Some(value), |_, _| {});
            }
        });
        let weak = Rc::downgrade(self);
        let chosen = selected.clone();
        let available = profiles;
        delete.connect_clicked(move |_| {
            let id = chosen.borrow().clone();
            if let (Some(ui), Some(id)) = (weak.upgrade(), id) {
                available.borrow_mut().remove(&id);
                // Drop the selected borrow before selection notifications run.
                refresh(&ui, Some(String::new()));
                ui.save_obs_profile(id, None, |_, _| {});
            }
        });
        let fields = (host, port, password);
        let status = status.downgrade();
        let weak = Rc::downgrade(self);
        let chosen = selected.clone();
        test.connect_clicked(move |_| {
            let profile = json!({"host":fields.0.text().as_str(),"port":fields.1.value() as u16,"password":fields.2.text().as_str()});
            let (send, receive) = async_channel::bounded(1);
            let id = chosen.borrow().clone().unwrap_or_else(|| "default".into());
            std::thread::spawn(move || { let _ = send.send_blocking(deckard_core::obs::choices(&profile, &id, "")); });
            let status = status.clone();
            let weak = weak.clone();
            glib::MainContext::default().spawn_local(async move {
                if let (Ok(result), Some(status)) = (receive.recv().await, status.upgrade()) {
                    match result {
                        Ok(choices) => { if let Some(ui) = weak.upgrade() { *ui.choices.borrow_mut() = choices; } status.set_label("Connected successfully"); }
                        Err(error) => status.set_label(&deckard_core::engine::redact(&error.to_string())),
                    }
                }
            });
        });
        group
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
                        json!({"name":page,"document":proposal}),
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
pub(super) fn autostart_path() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".config")
        })
        .join("autostart/deckard.desktop")
}
