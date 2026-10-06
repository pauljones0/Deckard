//! The direct upstream's sidebar widgets, translated to Rust GTK bindings.
//! GtkColorButton/GtkFontButton intentionally match upstream, including their dialogs.
#![allow(deprecated)]
use super::*;
use controls::*;
use gtk::glib::translate::IntoGlib;

fn linked() -> gtk::Box {
    let box_ = gtk::Box::new(gtk::Orientation::Horizontal, 0);
    box_.add_css_class("linked");
    box_
}
fn property_row(title: &str) -> (adw::PreferencesRow, gtk::Box) {
    let row = adw::PreferencesRow::new();
    let content = gtk::Box::new(gtk::Orientation::Horizontal, 0);
    content.set_hexpand(true);
    margins(&content, 15);
    content.append(
        &gtk::Label::builder()
            .label(title)
            .hexpand(true)
            .xalign(0.)
            .build(),
    );
    row.set_child(Some(&content));
    (row, content)
}
fn rgba(value: [u8; 4]) -> gdk::RGBA {
    gdk::RGBA::new(
        value[0] as f32 / 255.,
        value[1] as f32 / 255.,
        value[2] as f32 / 255.,
        value[3] as f32 / 255.,
    )
}
fn rgba_json(value: &gdk::RGBA) -> Value {
    json!(
        [value.red(), value.green(), value.blue(), value.alpha()].map(|v| (v * 255.).round() as u8)
    )
}
impl Ui {
    pub(super) fn append_editor_panel(&self, group: &impl IsA<gtk::Widget>, top: i32) {
        let clamp = adw::Clamp::new();
        clamp.set_margin_top(top);
        let content = gtk::Box::new(gtk::Orientation::Vertical, 0);
        content.set_hexpand(true);
        content.append(group);
        clamp.set_child(Some(&content));
        self.controls.append(&clamp);
    }
    fn composed_state(&self) -> Value {
        let selection = self.selection.borrow();
        let engine = self.shared.lock().unwrap();
        let mut result = engine
            .config(&selection.serial)
            .map(|config| {
                render::effective_input(&config, &selection.family, &selection.input)["states"]
                    [selection.state.to_string()]
                .clone()
            })
            .unwrap_or_else(|| self.draft.borrow().clone());
        for position in ["top", "center", "bottom"] {
            for (key, alias) in [
                ("outline_width", "outline-width"),
                ("outline_color", "outline-color"),
                ("style", "font-style"),
            ] {
                if result["labels"][position][key].is_null() {
                    result["labels"][position][key] = result["labels"][position][alias].clone();
                }
            }
        }
        result
    }
    fn reset_properties(self: &Rc<Self>, owner: Selection, paths: Vec<Vec<String>>) {
        self.submit(Work {
            key: None,
            title: String::new(),
            rebuild: true,
            execute: Box::new(move |engine| {
                owner.edit(engine, |state| {
                    for path in paths {
                        fn remove(value: &mut Value, path: &[String]) {
                            if path.len() == 1 {
                                if let Some(object) = value.as_object_mut() {
                                    object.remove(&path[0]);
                                }
                            } else if let Some(child) = value.get_mut(&path[0]) {
                                remove(child, &path[1..]);
                            }
                        }
                        remove(state, &path);
                    }
                    Ok(())
                })
            }),
        });
    }
    fn revert(self: &Rc<Self>, paths: &[Vec<&str>]) -> gtk::Button {
        let button = gtk::Button::from_icon_name("edit-undo-symbolic");
        button.set_tooltip_text(Some("Revert to action defaults"));
        button.set_visible(
            paths
                .iter()
                .any(|path| !get_path(&self.draft.borrow(), path).is_null()),
        );
        let paths = paths
            .iter()
            .map(|path| path.iter().map(|s| (*s).to_owned()).collect())
            .collect::<Vec<Vec<String>>>();
        let owner = self.selection.borrow().clone();
        let weak = Rc::downgrade(self);
        button.connect_clicked(move |button| {
            if let Some(ui) = weak.upgrade() {
                ui.reset_properties(owner.clone(), paths.clone());
            }
            button.set_visible(false);
        });
        button
    }
    fn inline_spin(
        self: &Rc<Self>,
        path: &[&str],
        default: f64,
        min: f64,
        max: f64,
        step: f64,
        scale: f64,
    ) -> gtk::Box {
        let box_ = linked();
        let spin = gtk::SpinButton::with_range(min, max, step);
        spin.set_value(
            get_path(&self.composed_state(), path)
                .as_f64()
                .unwrap_or(default)
                * scale,
        );
        spin.set_widget_name(&path.join("/"));
        let revert = self.revert(&[path.to_vec()]);
        box_.append(&spin);
        box_.append(&revert);
        let keys = path.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let owner = self.selection.borrow().clone();
        let weak = Rc::downgrade(self);
        let revert = revert.downgrade();
        spin.connect_value_changed(move |spin| {
            if let Some(ui) = weak.upgrade() {
                ui.edit(
                    owner.clone(),
                    keys.clone(),
                    if scale == 1. && step >= 1. {
                        json!(spin.value() as i64)
                    } else {
                        json!(spin.value() / scale)
                    },
                );
            }
            if let Some(revert) = revert.upgrade() {
                revert.set_visible(true);
            }
        });
        box_
    }
    #[allow(clippy::too_many_arguments)]
    pub(super) fn upstream_spin_row(
        self: &Rc<Self>,
        title: &str,
        path: &[&str],
        default: f64,
        min: f64,
        max: f64,
        step: f64,
        scale: f64,
    ) -> adw::PreferencesRow {
        let (row, content) = property_row(title);
        content.append(&self.inline_spin(path, default, min, max, step, scale));
        row
    }
    pub(super) fn upstream_color_row(
        self: &Rc<Self>,
        title: &str,
        path: &[&str],
        default: [u8; 4],
    ) -> adw::PreferencesRow {
        let (row, content) = property_row(title);
        let box_ = linked();
        let dialog = gtk::ColorDialog::builder()
            .title("Select color")
            .with_alpha(true)
            .build();
        let button = gtk::ColorDialogButton::new(Some(dialog));
        button.set_rgba(&rgba(render::color(
            get_path(&self.composed_state(), path),
            default,
        )));
        let revert = self.revert(&[path.to_vec()]);
        box_.append(&button);
        box_.append(&revert);
        content.append(&box_);
        let keys = path.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let owner = self.selection.borrow().clone();
        let weak = Rc::downgrade(self);
        let revert = revert.downgrade();
        button.connect_rgba_notify(move |button| {
            if let Some(ui) = weak.upgrade() {
                ui.edit(owner.clone(), keys.clone(), rgba_json(&button.rgba()));
            }
            if let Some(revert) = revert.upgrade() {
                revert.set_visible(true);
            }
        });
        row
    }
    fn label_color(self: &Rc<Self>, path: &[&str], default: [u8; 4]) -> gtk::Box {
        let box_ = linked();
        let button = gtk::ColorButton::new();
        button.set_rgba(&rgba(render::color(
            get_path(&self.composed_state(), path),
            default,
        )));
        let revert = self.revert(&[path.to_vec()]);
        box_.append(&button);
        box_.append(&revert);
        let keys = path.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let owner = self.selection.borrow().clone();
        let weak = Rc::downgrade(self);
        let revert = revert.downgrade();
        button.connect_color_set(move |button| {
            if let Some(ui) = weak.upgrade() {
                ui.edit(owner.clone(), keys.clone(), rgba_json(&button.rgba()));
            }
            if let Some(revert) = revert.upgrade() {
                revert.set_visible(true);
            }
        });
        box_
    }
    pub(super) fn upstream_label_row(
        self: &Rc<Self>,
        position: &str,
        title: &str,
    ) -> adw::PreferencesRow {
        let row = adw::PreferencesRow::new();
        let main = gtk::Box::new(gtk::Orientation::Vertical, 0);
        main.set_hexpand(true);
        margins(&main, 15);
        row.set_child(Some(&main));
        let caption = gtk::Label::builder()
            .label(title)
            .xalign(0.)
            .margin_bottom(3)
            .build();
        caption.add_css_class("bold");
        main.append(&caption);
        let warning = gtk::Label::builder()
            .label("Controlled by action")
            .xalign(0.)
            .margin_bottom(3)
            .visible(false)
            .build();
        warning.add_css_class("bold");
        warning.add_css_class("red-color");
        main.append(&warning);
        let line = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        line.set_hexpand(true);
        main.append(&line);
        let text_box = linked();
        text_box.set_margin_end(5);
        let entry = gtk::Entry::builder()
            .hexpand(true)
            .placeholder_text("Label")
            .build();
        let text_path = ["labels", position, "text"];
        entry.set_widget_name(&text_path.join("/"));
        entry.set_text(
            get_path(&self.composed_state(), &text_path)
                .as_str()
                .unwrap_or(""),
        );
        let revert = self.revert(&[text_path.to_vec()]);
        text_box.append(&entry);
        text_box.append(&revert);
        line.append(&text_box);
        line.append(&self.label_color(&["labels", position, "color"], [255, 255, 255, 255]));
        let font_line = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        font_line.set_hexpand(true);
        font_line.set_margin_top(6);
        font_line.append(
            &gtk::Label::builder()
                .label("Font:")
                .xalign(0.)
                .hexpand(true)
                .margin_start(2)
                .build(),
        );
        let font_box = linked();
        let font = gtk::FontButton::new();
        let draft = self.composed_state();
        let label = &draft["labels"][position];
        let mut desc = gtk::pango::FontDescription::new();
        desc.set_family(label["font-family"].as_str().unwrap_or("Roboto"));
        desc.set_absolute_size(
            label["font-size"].as_f64().unwrap_or(15.) * gtk::pango::SCALE as f64,
        );
        desc.set_weight(match label["font-weight"].as_i64().unwrap_or(400) {
            ..=149 => gtk::pango::Weight::Thin,
            150..=249 => gtk::pango::Weight::Ultralight,
            250..=349 => gtk::pango::Weight::Light,
            350..=449 => gtk::pango::Weight::Normal,
            450..=549 => gtk::pango::Weight::Medium,
            550..=649 => gtk::pango::Weight::Semibold,
            650..=749 => gtk::pango::Weight::Bold,
            750..=849 => gtk::pango::Weight::Ultrabold,
            _ => gtk::pango::Weight::Heavy,
        });
        desc.set_style(
            match label["style"]
                .as_str()
                .or_else(|| label["font-style"].as_str())
                .unwrap_or("normal")
            {
                "italic" => gtk::pango::Style::Italic,
                "oblique" => gtk::pango::Style::Oblique,
                _ => gtk::pango::Style::Normal,
            },
        );
        font.set_font_desc(&desc);
        drop(draft);
        let paths = [
            vec!["labels", position, "font-family"],
            vec!["labels", position, "font-size"],
            vec!["labels", position, "font-weight"],
            vec!["labels", position, "style"],
        ];
        let font_revert = self.revert(&paths);
        font_box.append(&font);
        font_box.append(&font_revert);
        font_line.append(&font_box);
        main.append(&font_line);
        let weak = Rc::downgrade(self);
        let owner = self.selection.borrow().clone();
        let position_owned = position.to_owned();
        let font_revert = font_revert.downgrade();
        font.connect_font_set(move |font| {
            if let (Some(ui), Some(desc)) = (weak.upgrade(), font.font_desc()) {
                for (key, value) in [
                    (
                        "font-family",
                        json!(desc.family().map(|s| s.to_string()).unwrap_or_default()),
                    ),
                    (
                        "font-size",
                        json!(desc.size() as f64 / gtk::pango::SCALE as f64),
                    ),
                    ("font-weight", json!(desc.weight().into_glib())),
                    (
                        "style",
                        json!(match desc.style() {
                            gtk::pango::Style::Italic => "italic",
                            gtk::pango::Style::Oblique => "oblique",
                            _ => "normal",
                        }),
                    ),
                ] {
                    ui.edit(
                        owner.clone(),
                        vec!["labels".into(), position_owned.clone(), key.into()],
                        value,
                    );
                }
                if let Some(revert) = font_revert.upgrade() {
                    revert.set_visible(true);
                }
            }
        });
        let align_line = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        align_line.set_hexpand(true);
        align_line.set_margin_top(6);
        align_line.append(
            &gtk::Label::builder()
                .label("Align:")
                .xalign(0.)
                .hexpand(true)
                .margin_start(2)
                .build(),
        );
        let align_box = linked();
        let alignment_path = ["labels", position, "alignment"];
        let alignment = get_path(&self.composed_state(), &alignment_path)
            .as_str()
            .unwrap_or("center")
            .to_owned();
        let align_revert = self.revert(&[alignment_path.to_vec()]);
        let mut group = None::<gtk::ToggleButton>;
        for (name, icon) in [
            ("left", "format-justify-left-symbolic"),
            ("center", "format-justify-center-symbolic"),
            ("right", "format-justify-right-symbolic"),
        ] {
            let toggle = gtk::ToggleButton::builder()
                .icon_name(icon)
                .tooltip_text(match name {
                    "left" => "Left",
                    "right" => "Right",
                    _ => "Center",
                })
                .build();
            toggle.set_group(group.as_ref());
            if group.is_none() {
                group = Some(toggle.clone());
            }
            toggle.set_active(name == alignment);
            align_box.append(&toggle);
            let weak = Rc::downgrade(self);
            let owner = self.selection.borrow().clone();
            let keys = alignment_path.map(str::to_owned).to_vec();
            let revert = align_revert.downgrade();
            toggle.connect_toggled(move |toggle| {
                if toggle.is_active() {
                    if let Some(ui) = weak.upgrade() {
                        ui.edit(owner.clone(), keys.clone(), json!(name));
                    }
                    if let Some(revert) = revert.upgrade() {
                        revert.set_visible(true);
                    }
                }
            });
        }
        align_box.append(&align_revert);
        align_line.append(&align_box);
        main.append(&align_line);
        let outline_line = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        outline_line.set_hexpand(true);
        outline_line.set_margin_top(6);
        outline_line.append(
            &gtk::Label::builder()
                .label("Outline width:")
                .xalign(0.)
                .margin_start(2)
                .margin_end(5)
                .build(),
        );
        outline_line.append(&self.inline_spin(
            &["labels", position, "outline_width"],
            2.,
            0.,
            10.,
            1.,
            1.,
        ));
        outline_line.append(
            &gtk::Label::builder()
                .label("Outline color:")
                .xalign(0.)
                .hexpand(true)
                .halign(gtk::Align::End)
                .margin_start(2)
                .margin_end(5)
                .build(),
        );
        outline_line
            .append(&self.label_color(&["labels", position, "outline_color"], [0, 0, 0, 255]));
        main.append(&outline_line);
        for line in [&font_line, &align_line, &outline_line] {
            line.set_visible(!entry.text().trim().is_empty());
        }
        let weak = Rc::downgrade(self);
        let owner = self.selection.borrow().clone();
        let keys = text_path.map(str::to_owned).to_vec();
        let revert = revert.downgrade();
        entry.connect_changed(move |entry| {
            if let Some(ui) = weak.upgrade() {
                ui.edit(owner.clone(), keys.clone(), json!(entry.text().as_str()));
            }
            if let Some(revert) = revert.upgrade() {
                revert.set_visible(true);
            }
            for line in [&font_line, &align_line, &outline_line] {
                line.set_visible(!entry.text().trim().is_empty());
            }
        });
        row
    }
    pub(super) fn upstream_loop_row(self: &Rc<Self>) -> adw::PreferencesRow {
        let (row, content) = property_row("Loop");
        let switch = gtk::Switch::builder()
            .valign(gtk::Align::Center)
            .active(
                self.draft.borrow()["media"]["loop"]
                    .as_bool()
                    .unwrap_or(true),
            )
            .build();
        content.append(&switch);
        let owner = self.selection.borrow().clone();
        let weak = Rc::downgrade(self);
        switch.connect_active_notify(move |switch| {
            if let Some(ui) = weak.upgrade() {
                ui.edit(
                    owner.clone(),
                    vec!["media".into(), "loop".into()],
                    json!(switch.is_active()),
                );
            }
        });
        row
    }
    pub(super) fn upstream_media_row(self: &Rc<Self>) -> adw::PreferencesRow {
        let (row, content) = property_row("Background");
        let preview = gtk::Picture::builder()
            .width_request(96)
            .height_request(48)
            .content_fit(gtk::ContentFit::Cover)
            .overflow(gtk::Overflow::Hidden)
            .build();
        let frame = gtk::Frame::builder().child(&preview).build();
        frame.add_css_class("card");
        let path = self.draft.borrow()["media"]["path"]
            .as_str()
            .unwrap_or("")
            .to_owned();
        if !path.is_empty() {
            preview.set_filename(Some(&path));
        }
        content.append(&frame);
        let buttons = linked();
        let weak = Rc::downgrade(self);
        buttons.append(&button("", "folder-open-symbolic", move || {
            if let Some(ui) = weak.upgrade() {
                ui.assets();
            }
        }));
        let weak = Rc::downgrade(self);
        let owner = self.selection.borrow().clone();
        buttons.append(&button("", "edit-clear-symbolic", move || {
            if let Some(ui) = weak.upgrade() {
                ui.reset_properties(owner.clone(), vec![vec!["media".into(), "path".into()]]);
            }
        }));
        content.append(&buttons);
        row
    }
}
