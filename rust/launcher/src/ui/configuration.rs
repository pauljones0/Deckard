//! The in-window deck settings and reusable page/deck media controls.
use super::*;
use controls::*;

fn slideshow_summary(count: usize) -> String {
    if count < 2 {
        "Single image (no slideshow)".into()
    } else {
        format!("{count} images in slideshow")
    }
}

#[derive(Clone)]
pub(super) enum ConfigTarget {
    Device(String),
    Page(String),
}
impl ConfigTarget {
    pub(super) fn document(&self, ui: &Ui) -> Value {
        let engine = ui.shared.lock().unwrap();
        match self {
            Self::Device(serial) => engine.docs.settings["devices"][serial].clone(),
            Self::Page(page) => engine
                .docs
                .pages
                .get(page)
                .cloned()
                .unwrap_or_else(|| json!({})),
        }
    }
}
fn content_row(vertical: bool) -> (adw::PreferencesRow, gtk::Box) {
    let row = adw::PreferencesRow::new();
    let content = gtk::Box::new(
        if vertical {
            gtk::Orientation::Vertical
        } else {
            gtk::Orientation::Horizontal
        },
        0,
    );
    content.set_hexpand(true);
    margins(&content, 15);
    row.set_child(Some(&content));
    (row, content)
}
fn line(title: &str) -> gtk::Box {
    let line = gtk::Box::new(gtk::Orientation::Horizontal, 0);
    line.set_hexpand(true);
    line.append(
        &gtk::Label::builder()
            .label(title)
            .hexpand(true)
            .xalign(0.)
            .build(),
    );
    line
}
impl Ui {
    pub(super) fn config_property(
        self: &Rc<Self>,
        target: ConfigTarget,
        path: Vec<String>,
        value: Value,
    ) {
        match target {
            ConfigTarget::Device(serial) => {
                let mut keys = vec!["devices".into(), serial];
                keys.extend(path);
                self.settings_property(keys, value);
            }
            ConfigTarget::Page(page) => self.page_property(page, path, value),
        }
    }
    pub(super) fn device_settings(self: &Rc<Self>) {
        let visible = !self.deck_settings_visible.get();
        let serial = self.selection.borrow().serial.clone();
        let Some(view) = self
            .deck_stack
            .child_by_name(&serial)
            .and_then(|main| main.last_child())
            .and_downcast::<gtk::Stack>()
        else {
            return;
        };
        if visible {
            if let Some(old) = view.child_by_name("deck-settings") {
                view.remove(&old);
            }
            view.add_named(&self.deck_settings_view(&serial), Some("deck-settings"));
        }
        view.set_visible_child_name(if visible { "deck-settings" } else { "key-grid" });
        self.deck_settings_visible.set(visible);
        self.deck_settings_caption();
        if visible {
            focus_deck_name(view.upcast_ref());
        }
    }
    pub(super) fn deck_settings_caption(&self) {
        let content = gtk::Box::new(gtk::Orientation::Horizontal, 10);
        content.append(&gtk::Image::from_icon_name(
            if self.deck_settings_visible.get() {
                "input-dialpad-symbolic"
            } else {
                "drive-removable-media-symbolic"
            },
        ));
        content.append(&gtk::Label::new(Some(
            if self.deck_settings_visible.get() {
                "Key Grid"
            } else {
                "Deck Settings"
            },
        )));
        self.deck_settings.set_child(Some(&content));
    }
    pub(super) fn deck_settings_view(self: &Rc<Self>, serial: &str) -> gtk::Overlay {
        let overlay = gtk::Overlay::builder()
            .margin_start(50)
            .margin_end(50)
            .margin_bottom(50)
            .build();
        let main = gtk::Box::builder()
            .orientation(gtk::Orientation::Vertical)
            .hexpand(true)
            .build();
        overlay.set_child(Some(&main));
        let scroll = gtk::ScrolledWindow::builder()
            .hexpand(true)
            .vexpand(true)
            .margin_top(50)
            .build();
        main.append(&scroll);
        let clamp = adw::Clamp::new();
        scroll.set_child(Some(&clamp));
        let body = gtk::Box::builder()
            .orientation(gtk::Orientation::Vertical)
            .hexpand(true)
            .build();
        clamp.set_child(Some(&body));
        let target = ConfigTarget::Device(serial.into());
        let settings = target.document(self);
        let group = adw::PreferencesGroup::builder()
            .title("Deck Settings")
            .description("Applies to the whole deck unless overwritten")
            .build();
        body.append(&group);
        let name = adw::EntryRow::builder()
            .title("Name")
            .name("deck-name")
            .show_apply_button(true)
            .max_length(32)
            .text(settings["name"].as_str().unwrap_or(""))
            .build();
        name.set_widget_name("deck-name");
        let weak = Rc::downgrade(self);
        let owner = target.clone();
        name.connect_apply(move |row| {
            let value = row.text().trim().to_owned();
            row.set_text(&value);
            if let Some(ui) = weak.upgrade() {
                ui.config_property(owner.clone(), vec!["name".into()], json!(value));
            }
        });
        group.add(&name);
        let brightness = self
            .shared
            .lock()
            .unwrap()
            .devices
            .get(serial)
            .map(|d| d.brightness as f64)
            .unwrap_or(75.);
        let (row, content) = content_row(true);
        content.append(
            &gtk::Label::builder()
                .label("Brightness")
                .hexpand(true)
                .xalign(0.)
                .build(),
        );
        let scale = gtk::Scale::with_range(gtk::Orientation::Horizontal, 0., 100., 1.);
        scale.set_draw_value(true);
        scale.set_value(brightness);
        content.append(&scale);
        let weak = Rc::downgrade(self);
        let serial_owned = serial.to_owned();
        scale.connect_value_changed(move |scale| {
            if let Some(ui) = weak.upgrade() {
                ui.command(
                    "set-brightness",
                    json!({"serial":serial_owned, "value":scale.value() as u64}),
                    "",
                    false,
                );
            }
        });
        group.add(&row);
        group.add(&self.config_scale_row(
            "Saturation",
            &target,
            &["display", "saturation"],
            1.,
            1.,
            1.5,
            0.05,
            2,
        ));
        group.add(&self.deck_media_row(&target, true));
        let (row, content) = content_row(false);
        content.append(
            &gtk::Label::builder()
                .label("Rotation")
                .hexpand(true)
                .xalign(0.)
                .build(),
        );
        let toggles = adw::ToggleGroup::new();
        for angle in [0, 90, 180, 270] {
            toggles.add(
                adw::Toggle::builder()
                    .label(format!("{angle}°"))
                    .name(angle.to_string())
                    .build(),
            );
        }
        toggles.set_active_name(Some(
            &settings["rotation"].as_u64().unwrap_or(0).to_string(),
        ));
        let weak = Rc::downgrade(self);
        let owner = target.clone();
        toggles.connect_active_name_notify(move |toggles| {
            if let (Some(ui), Some(name)) = (weak.upgrade(), toggles.active_name()) {
                ui.config_property(
                    owner.clone(),
                    vec!["rotation".into()],
                    json!(name.parse::<u16>().unwrap_or(0)),
                );
            }
        });
        content.append(&toggles);
        group.add(&row);
        let background = adw::PreferencesGroup::builder()
            .title("Background")
            .description("Applies to the whole deck unless overwritten")
            .margin_top(50)
            .build();
        background.add(&self.deck_media_row(&target, false));
        body.append(&background);
        let serial = gtk::Label::builder()
            .label(format!("Serial: {serial}"))
            .margin_top(20)
            .sensitive(false)
            .build();
        serial.add_css_class("dim-label");
        body.append(&serial);
        overlay
    }
    #[allow(clippy::too_many_arguments)]
    pub(super) fn config_scale_row(
        self: &Rc<Self>,
        title: &str,
        target: &ConfigTarget,
        path: &[&str],
        default: f64,
        min: f64,
        max: f64,
        step: f64,
        digits: i32,
    ) -> adw::PreferencesRow {
        let (row, content) = content_row(true);
        content.append(
            &gtk::Label::builder()
                .label(title)
                .hexpand(true)
                .xalign(0.)
                .build(),
        );
        let scale = gtk::Scale::with_range(gtk::Orientation::Horizontal, min, max, step);
        scale.set_digits(digits);
        scale.set_draw_value(true);
        scale.set_value(
            get_path(&target.document(self), path)
                .as_f64()
                .unwrap_or(default),
        );
        content.append(&scale);
        let keys = path.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let weak = Rc::downgrade(self);
        let owner = target.clone();
        scale.connect_value_changed(move |scale| {
            if let Some(ui) = weak.upgrade() {
                ui.config_property(owner.clone(), keys.clone(), json!(scale.value()));
            }
        });
        row
    }
    fn config_switch(
        self: &Rc<Self>,
        title: &str,
        target: &ConfigTarget,
        path: &[&str],
        default: bool,
    ) -> (gtk::Box, gtk::Switch) {
        let content = line(title);
        let switch = gtk::Switch::new();
        switch.set_active(
            get_path(&target.document(self), path)
                .as_bool()
                .unwrap_or(default),
        );
        content.append(&switch);
        let keys = path.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let weak = Rc::downgrade(self);
        let owner = target.clone();
        switch.connect_active_notify(move |switch| {
            if let Some(ui) = weak.upgrade() {
                ui.config_property(owner.clone(), keys.clone(), json!(switch.is_active()));
            }
        });
        (content, switch)
    }
    fn config_spin(
        self: &Rc<Self>,
        title: &str,
        target: &ConfigTarget,
        path: &[&str],
        default: f64,
        min: f64,
        max: f64,
    ) -> gtk::Box {
        let content = line(title);
        let spin = gtk::SpinButton::with_range(min, max, 1.);
        spin.set_value(
            get_path(&target.document(self), path)
                .as_f64()
                .unwrap_or(default),
        );
        content.append(&spin);
        let keys = path.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let weak = Rc::downgrade(self);
        let owner = target.clone();
        spin.connect_value_changed(move |spin| {
            if let Some(ui) = weak.upgrade() {
                ui.config_property(owner.clone(), keys.clone(), json!(spin.value() as u64));
            }
        });
        content
    }
    pub(super) fn config_media_button(
        self: &Rc<Self>,
        target: &ConfigTarget,
        section: &str,
    ) -> gtk::Button {
        let button = gtk::Button::with_label("Select");
        button.add_css_class("page-settings-media-selector");
        let image = gtk::Picture::builder()
            .content_fit(gtk::ContentFit::Contain)
            .width_request(150)
            .height_request(150)
            .build();
        let value = target.document(self);
        if let Some(path) = value[section]["media-path"]
            .as_str()
            .filter(|path| !path.is_empty())
        {
            image.set_filename(Some(path));
            button.set_child(Some(&image));
        }
        let weak = Rc::downgrade(self);
        let owner = target.clone();
        let section = section.to_owned();
        let weak_image = image.downgrade();
        button.connect_clicked(move |button| {
            if let Some(ui) = weak.upgrade() {
                let weak = Rc::downgrade(&ui);
                let owner = owner.clone();
                let section = section.clone();
                let image = weak_image.clone();
                let button = button.downgrade();
                ui.asset_manager(Rc::new(move |path| {
                    if let Some(ui) = weak.upgrade() {
                        ui.config_property(
                            owner.clone(),
                            vec![section.clone(), "view".into()],
                            json!({"x":0.5,"y":0.5,"scale":1.}),
                        );
                        ui.config_property(
                            owner.clone(),
                            vec![section.clone(), "media-path".into()],
                            json!(path),
                        );
                        if let (Some(image), Some(button)) = (image.upgrade(), button.upgrade()) {
                            image.set_filename(Some(&path));
                            button.set_child(Some(&image));
                        }
                    }
                }));
            }
        });
        button
    }
    fn deck_media_row(self: &Rc<Self>, target: &ConfigTarget, saver: bool) -> adw::PreferencesRow {
        let section = if saver { "screensaver" } else { "background" };
        let (row, main) = content_row(true);
        let (enable, switch) = self.config_switch(
            if saver {
                "Enable Screensaver"
            } else {
                "Enable Background"
            },
            target,
            &[section, "enable"],
            false,
        );
        main.append(&enable);
        let config = gtk::Box::builder()
            .orientation(gtk::Orientation::Vertical)
            .hexpand(true)
            .visible(switch.is_active())
            .build();
        main.append(&config);
        config.append(
            &gtk::Separator::builder()
                .orientation(gtk::Orientation::Horizontal)
                .hexpand(true)
                .margin_top(10)
                .margin_bottom(10)
                .build(),
        );
        let weak_config = config.downgrade();
        switch.connect_active_notify(move |switch| {
            if let Some(config) = weak_config.upgrade() {
                config.set_visible(switch.is_active());
            }
        });
        if saver {
            config.append(&self.config_spin(
                "Enable after (min)",
                target,
                &[section, "time-delay"],
                5.,
                1.,
                1440.,
            ));
            config.append(
                &gtk::Label::builder()
                    .label("Media to show")
                    .hexpand(true)
                    .xalign(0.)
                    .build(),
            );
        }
        let media = self.config_media_button(target, section);
        media.set_halign(gtk::Align::Center);
        config.append(&media);
        if !saver {
            let weak = Rc::downgrade(self);
            let owner = target.clone();
            let adjust = button("Adjust View", "", move || {
                if let Some(ui) = weak.upgrade() {
                    ui.viewport(&owner);
                }
            });
            adjust.set_halign(gtk::Align::Center);
            adjust.set_margin_top(10);
            config.append(&adjust);
            let buttons = gtk::Box::builder()
                .orientation(gtk::Orientation::Horizontal)
                .halign(gtk::Align::Center)
                .spacing(6)
                .margin_top(10)
                .build();
            let count = target.document(self)["background"]["media-paths"]
                .as_array()
                .map_or(0, Vec::len);
            let summary = gtk::Label::builder()
                .label(slideshow_summary(count))
                .halign(gtk::Align::Center)
                .margin_top(6)
                .build();
            for (title, clear) in [("Add Image", false), ("Clear Slideshow", true)] {
                let weak = Rc::downgrade(self);
                let owner = target.clone();
                let summary = summary.downgrade();
                buttons.append(&button(title, "", move || {
                    if let Some(ui) = weak.upgrade() {
                        if clear {
                            ui.config_property(
                                owner.clone(),
                                vec!["background".into(), "media-paths".into()],
                                json!([]),
                            );
                            if let Some(summary) = summary.upgrade() { summary.set_text(&slideshow_summary(0)); }
                        } else {
                            let weak = Rc::downgrade(&ui);
                            let owner = owner.clone();
                            let summary = summary.clone();
                            ui.asset_manager(Rc::new(move |path| {
                                if let Some(ui) = weak.upgrade() {
                                    let owner = owner.clone();
                                    let summary = summary.clone();
                                    ui.submit_then(Work {
                                        key: None, title: String::new(), rebuild: false,
                                        execute: Box::new(move |engine| {
                                            let mut document = match &owner {
                                                ConfigTarget::Device(_) => engine.docs.settings.clone(),
                                                ConfigTarget::Page(page) => engine.docs.pages.get(page).cloned().context("Page disappeared")?,
                                            };
                                            let background = match &owner {
                                                ConfigTarget::Device(serial) => &mut document["devices"][serial]["background"],
                                                ConfigTarget::Page(_) => &mut document["background"],
                                            };
                                            let mut slides = background["media-paths"].as_array().cloned().unwrap_or_default();
                                            if !slides.iter().any(|s| s.as_str().or_else(|| s["path"].as_str()) == Some(&path)) { slides.push(json!(path)); }
                                            let count = slides.len();
                                            background["media-paths"] = json!(slides);
                                            let command = match owner {
                                                ConfigTarget::Device(_) => json!({"method":"put-settings","params":document}),
                                                ConfigTarget::Page(page) => json!({"method":"put-page","params":{"name":page,"document":document}}),
                                            };
                                            engine.command(&command)?;
                                            Ok(json!(count))
                                        }),
                                    }, move |_, result| {
                                        if let (Ok(count), Some(summary)) = (result, summary.upgrade()) { summary.set_text(&slideshow_summary(count.as_u64().unwrap_or(0) as usize)); }
                                    });
                                }
                            }));
                        }
                    }
                }));
            }
            config.append(&buttons);
            config.append(&summary);
            let interval = self.config_spin(
                "Slideshow interval (s)",
                target,
                &[section, "slideshow-interval"],
                10.,
                1.,
                3600.,
            );
            interval.set_margin_top(15);
            config.append(&interval);
            let shuffle = line("Shuffle order");
            let switch = gtk::Switch::builder()
                .valign(gtk::Align::Center)
                .active(
                    target.document(self)[section]["slideshow-order"].as_str() == Some("shuffle"),
                )
                .build();
            let weak = Rc::downgrade(self);
            let owner = target.clone();
            switch.connect_active_notify(move |switch| {
                if let Some(ui) = weak.upgrade() {
                    ui.config_property(
                        owner.clone(),
                        vec!["background".into(), "slideshow-order".into()],
                        json!(if switch.is_active() {
                            "shuffle"
                        } else {
                            "in-order"
                        }),
                    );
                }
            });
            shuffle.append(&switch);
            shuffle.set_margin_top(15);
            config.append(&shuffle);
        }
        let (loop_, _) = self.config_switch("Loop", target, &[section, "loop"], true);
        loop_.set_margin_bottom(15);
        config.append(&loop_);
        config.append(&self.config_spin("FPS", target, &[section, "fps"], 0., 0., 120.));
        if saver {
            config.append(&self.config_scale_row(
                "Brightness",
                target,
                &[section, "brightness"],
                30.,
                0.,
                100.,
                1.,
                0,
            ));
        } else {
            let (extend, _) = self.config_switch(
                "Extend the background across the buttons and touchscreen",
                target,
                &[section, "extend-to-touchscreen"],
                false,
            );
            extend.set_margin_top(15);
            config.append(&extend);
        }
        row
    }
}

fn focus_deck_name(widget: &gtk::Widget) -> bool {
    if widget.widget_name() == "deck-name" {
        return widget.grab_focus();
    }
    let mut child = widget.first_child();
    while let Some(widget) = child {
        child = widget.next_sibling();
        if focus_deck_name(&widget) {
            return true;
        }
    }
    false
}
