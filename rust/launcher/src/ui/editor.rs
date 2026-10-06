use super::*;
use controls::*;

impl Ui {
    pub(super) fn refresh(self: &Rc<Self>) {
        if self.signal.load(Ordering::Relaxed) || self.shared.lock().unwrap().quit {
            self.window.set_sensitive(false);
            if self.pending_count.get() == 0 && self.pending.borrow().is_empty() {
                if let Some(app) = self.window.application() {
                    app.quit();
                }
                return;
            }
        }
        let (show, devices, settings, revision, plugins) = {
            let mut engine = self.shared.lock().unwrap();
            let show = engine.show_window;
            engine.show_window = false;
            let plugins = (self.definition_revision.get() != engine.plugin_revision)
                .then(|| (engine.plugin_revision, action_definitions(&engine.plugins)));
            let mut devices = engine.devices.values().cloned().collect::<Vec<_>>();
            devices.sort_by(|a, b| a.serial.cmp(&b.serial));
            (
                show,
                devices,
                engine.docs.settings.clone(),
                engine.docs.revision,
                plugins,
            )
        };
        if let Some((revision, definitions)) = plugins {
            self.definition_revision.set(revision);
            *self.definitions.borrow_mut() = definitions;
            self.force_reload.set(true);
        }
        if show {
            self.bridge.hidden.store(false, Ordering::Relaxed);
            self.window.present();
            self.force_reload.set(true);
        }
        if !devices
            .iter()
            .any(|d| d.serial == self.selection.borrow().serial)
            && let Some(d) = devices.first()
        {
            let mut sel = self.selection.borrow_mut();
            sel.serial = d.serial.clone();
            sel.page = d.page.clone();
            sel.state = 0;
            drop(sel);
            self.force_reload.set(true);
        }
        if let Some(device) = devices
            .iter()
            .find(|d| d.serial == self.selection.borrow().serial)
            && device.page != self.selection.borrow().page
        {
            let mut sel = self.selection.borrow_mut();
            sel.page = device.page.clone();
            sel.state = 0;
            drop(sel);
            self.force_reload.set(true);
        }
        let signature = devices
            .iter()
            .map(|d| {
                (
                    d.serial.clone(),
                    d.epoch,
                    settings["devices"][&d.serial]["rotation"]
                        .as_u64()
                        .unwrap_or(0) as u16,
                    settings["devices"][&d.serial]["name"]
                        .as_str()
                        .unwrap_or(&d.serial)
                        .to_owned(),
                    d.connected,
                )
            })
            .collect::<Vec<_>>();
        if *self.deck_signature.borrow() != signature {
            self.rebuild_decks(&devices, &settings);
            *self.deck_signature.borrow_mut() = signature;
        }
        let pages = self
            .shared
            .lock()
            .unwrap()
            .docs
            .pages
            .keys()
            .cloned()
            .collect::<Vec<_>>();
        if *self.page_names.borrow() != pages {
            self.rebuild_pages(&pages);
            *self.page_names.borrow_mut() = pages;
        }
        self.page_button
            .set_label(&format!("Page: {}", self.selection.borrow().page));
        self.deck_settings.set_sensitive(!devices.is_empty());
        if self.bridge.hidden.load(Ordering::Relaxed) {
            return;
        }
        let mut sel = self.selection.borrow().clone();
        if (self.loaded_revision.get() != revision || self.force_reload.get())
            && self.pending_count.get() == 0
            && self.pending.borrow().is_empty()
        {
            let document = sel.document(&self.shared.lock().unwrap());
            if let Some(states) = document[&sel.family][&sel.input]["states"].as_object()
                && !states.contains_key(&sel.state.to_string())
                && let Some(first) = states.keys().filter_map(|s| s.parse::<usize>().ok()).min()
            {
                sel.state = first;
                self.selection.borrow_mut().state = first;
            }
            let draft = model::state(&document, &sel.family, &sel.input, sel.state).clone();
            let draft = if draft.is_object() { draft } else { json!({}) };
            let force = self.force_reload.replace(false);
            if draft != *self.draft.borrow() || force {
                *self.draft.borrow_mut() = draft;
                self.rebuild_editor(&document);
            }
            self.loaded_revision.set(revision);
        }
        if let Some(device) = devices.iter().find(|d| d.serial == sel.serial)
            && let Some(frame) = &device.frame
        {
            let rotation = settings["devices"][&sel.serial]["rotation"]
                .as_u64()
                .unwrap_or(0) as u16;
            self.update_previews(device, frame, rotation);
        }
    }
    fn rebuild_pages(self: &Rc<Self>, pages: &[String]) {
        let popover = gtk::Popover::new();
        let content = gtk::Box::new(gtk::Orientation::Vertical, 6);
        margins(&content, 12);
        let search = gtk::SearchEntry::builder()
            .placeholder_text("Search pages")
            .build();
        content.append(&search);
        let list = gtk::ListBox::new();
        list.set_selection_mode(gtk::SelectionMode::None);
        list.add_css_class("boxed-list");
        let scroll = gtk::ScrolledWindow::builder()
            .max_content_height(380)
            .propagate_natural_height(true)
            .child(&list)
            .build();
        content.append(&scroll);
        for page in pages {
            let row = adw::ActionRow::builder()
                .title(page)
                .activatable(true)
                .build();
            row.set_widget_name(page);
            let weak = Rc::downgrade(self);
            let page = page.clone();
            let pop = popover.downgrade();
            row.connect_activated(move |_| {
                if let Some(ui) = weak.upgrade() {
                    ui.change_page(&page);
                }
                if let Some(pop) = pop.upgrade() {
                    pop.popdown();
                }
            });
            list.append(&row);
        }
        list.set_filter_func({
            let search = search.clone();
            move |row| {
                row.widget_name()
                    .to_lowercase()
                    .contains(&search.text().to_lowercase())
            }
        });
        let weak_list = list.downgrade();
        search.connect_search_changed(move |_| {
            if let Some(list) = weak_list.upgrade() {
                list.invalidate_filter();
            }
        });
        popover.set_child(Some(&content));
        self.page_button.set_popover(Some(&popover));
    }
    pub(super) fn change_page(self: &Rc<Self>, page: &str) {
        let serial = self.selection.borrow().serial.clone();
        if serial.is_empty() {
            self.selection.borrow_mut().page = page.into();
            self.force_reload.set(true);
            self.refresh();
        } else {
            self.command(
                "change-page",
                json!({"serial":serial,"page":page}),
                "",
                true,
            );
        }
    }
    pub(super) fn select_device(self: &Rc<Self>, serial: &str) {
        let device = self.shared.lock().unwrap().devices.get(serial).cloned();
        if let Some(device) = device {
            *self.selection.borrow_mut() = Selection {
                serial: serial.into(),
                page: device.page,
                family: "keys".into(),
                input: "0x0".into(),
                state: 0,
                sticky: false,
            };
            self.clear_textures();
            self.deck_stack.set_visible_child_name(serial);
            self.force_reload.set(true);
            self.refresh();
        }
    }
    pub(super) fn select_input(self: &Rc<Self>, serial: &str, family: &str, input: &str) {
        let engine = self.shared.lock().unwrap();
        let Some(device) = engine.devices.get(serial) else {
            return;
        };
        let state = engine
            .config(serial)
            .map(|config| render::active_state(&config, family, input))
            .unwrap_or(0);
        let page = device.page.clone();
        drop(engine);
        *self.selection.borrow_mut() = Selection {
            serial: serial.into(),
            page,
            family: family.into(),
            input: input.into(),
            state,
            sticky: false,
        };
        self.force_reload.set(true);
        self.sidebar.set_visible_child_name("editor");
        self.dial_preview.set(None);
        self.split.set_show_content(false);
        self.refresh();
    }
    fn rebuild_decks(self: &Rc<Self>, devices: &[deckard_core::engine::Device], settings: &Value) {
        self.loading_shell.set(true);
        while let Some(child) = self.deck_stack.first_child() {
            self.deck_stack.remove(&child);
        }
        self.previews.borrow_mut().clear();
        if devices.is_empty() {
            let page=adw::StatusPage::builder().title("No Stream Deck connected").description("Connect your device to start editing. Your saved pages remain available from the menu.").icon_name("input-keyboard-symbolic").build();
            self.deck_stack
                .add_titled(&page, Some("disconnected"), "Deckard");
            self.preview.set_paintable(None::<&gdk::Paintable>);
        }
        for device in devices {
            let serial = &device.serial;
            let rotation = settings["devices"][serial]["rotation"]
                .as_u64()
                .unwrap_or(0) as u16;
            let (_, cols) = render::layout(device.kind, rotation);
            let body = gtk::Box::new(gtk::Orientation::Vertical, 0);
            body.set_halign(gtk::Align::Center);
            body.set_valign(gtk::Align::Center);
            let grid = gtk::Grid::new();
            body.append(&grid);
            for key in 0..device.kind.key_count() {
                let input = render::logical_input(device.kind, key, rotation);
                let index = render::logical_index(device.kind, key, rotation);
                let image = PreviewImage::new(75, 75);
                image.add_css_class("key-image");
                image.add_css_class("key-button");
                image.set_overflow(gtk::Overflow::Hidden);
                let button = gtk::Button::builder()
                    .child(&image)
                    .tooltip_text(format!("Key {}", input))
                    .build();
                button.add_css_class("deckard-key");
                button.set_widget_name(&format!("key-{serial}-{input}"));
                let frame = gtk::Frame::new(None);
                frame.set_child(Some(&button));
                frame.add_css_class("key-button-frame-hidden");
                grid.attach(
                    &frame,
                    i32::from(index % cols),
                    i32::from(index / cols),
                    1,
                    1,
                );
                self.connect_input(&button, serial, "keys", &input);
                self.previews.borrow_mut().push(Preview {
                    serial: serial.clone(),
                    physical: key,
                    input,
                    family: "keys".into(),
                    frame,
                    image,
                    identity: Cell::new(None),
                });
            }
            if device.kind.lcd_strip_size().is_some() {
                let image = PreviewImage::new(i32::from(cols) * 101 - 26, 50);
                image.add_css_class("deckard-strip");
                image.set_overflow(gtk::Overflow::Hidden);
                let button = gtk::Button::builder()
                    .child(&image)
                    .tooltip_text("Touchscreen")
                    .build();
                button.add_css_class("deckard-key");
                button.set_halign(gtk::Align::Center);

                self.connect_input(
                    &button,
                    serial,
                    if format!("{:?}", device.kind) == "Neo" {
                        "infobar"
                    } else {
                        "touchscreens"
                    },
                    "0",
                );
                let frame = gtk::Frame::new(None);
                frame.set_halign(gtk::Align::Center);
                frame.set_child(Some(&button));
                frame.add_css_class("key-button-frame-hidden");
                body.append(&frame);
                self.previews.borrow_mut().push(Preview {
                    serial: serial.clone(),
                    physical: 255,
                    input: "0".into(),
                    family: if format!("{:?}", device.kind) == "Neo" {
                        "infobar"
                    } else {
                        "touchscreens"
                    }
                    .into(),
                    frame,
                    image,
                    identity: Cell::new(None),
                });
            }
            if device.kind.encoder_count() > 0 {
                let dials = gtk::Box::new(gtk::Orientation::Horizontal, 0);
                dials.set_halign(gtk::Align::Center);
                body.append(&dials);
                for index in 0..device.kind.encoder_count() {
                    let button = gtk::Button::new();
                    button.add_css_class("deckard-dial");
                    button.set_widget_name(&format!("dial-{serial}-{index}"));
                    button.set_tooltip_text(Some(&format!("Dial {}", index + 1)));
                    let frame = gtk::Frame::new(None);
                    frame.add_css_class("dial-frame");
                    frame.add_css_class("dial-frame-hidden");
                    frame.set_child(Some(&button));
                    dials.append(&frame);
                    self.connect_input(&button, serial, "dials", &index.to_string());
                    self.previews.borrow_mut().push(Preview {
                        serial: serial.clone(),
                        physical: 254,
                        input: index.to_string(),
                        family: "dials".into(),
                        frame: frame.clone(),
                        image: PreviewImage::new(75, 75),
                        identity: Cell::new(None),
                    });
                    let weak = Rc::downgrade(self);
                    let serial = serial.clone();
                    let scroll =
                        gtk::EventControllerScroll::new(gtk::EventControllerScrollFlags::VERTICAL);
                    scroll.connect_scroll(move |_, _, dy| {
                        if let Some(ui) = weak.upgrade() {
                            ui.send_event(
                                &serial,
                                "dials",
                                &index.to_string(),
                                if dy < 0. { "turn-cw" } else { "turn-ccw" },
                                1,
                            );
                        }
                        glib::Propagation::Stop
                    });
                    button.add_controller(scroll);
                }
            }
            if format!("{:?}", device.kind) == "Neo" {
                let touches = gtk::Box::new(gtk::Orientation::Horizontal, 12);
                touches.set_halign(gtk::Align::Center);
                for index in 0..2 {
                    let button = gtk::Button::with_label(&format!("Touch {}", index + 1));
                    self.connect_input(&button, serial, "keys", &format!("touch-{index}"));
                    touches.append(&button);
                }
                body.append(&touches);
            }
            if !device.connected {
                let label = gtk::Label::new(Some("Reconnecting…"));
                label.add_css_class("dim-label");
                body.append(&label);
            }
            let scroll = gtk::ScrolledWindow::builder()
                .child(&body)
                .hscrollbar_policy(gtk::PolicyType::Automatic)
                .vscrollbar_policy(gtk::PolicyType::Automatic)
                .build();
            let title = settings["devices"][serial]["name"]
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| {
                    format!(
                        "{} ({:?})",
                        if device.fake { "Fake Deck" } else { serial },
                        device.kind
                    )
                });
            self.deck_stack.add_titled(&scroll, Some(serial), &title);
        }
        self.deck_stack
            .set_visible_child_name(&self.selection.borrow().serial);
        self.loading_shell.set(false);
    }
    fn connect_input(
        self: &Rc<Self>,
        button: &gtk::Button,
        serial: &str,
        family: &str,
        input: &str,
    ) {
        let weak = Rc::downgrade(self);
        let serial = serial.to_owned();
        let family = family.to_owned();
        let input = input.to_owned();
        let owner = (serial.clone(), family.clone(), input.clone());
        button.connect_clicked(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.select_input(&owner.0, &owner.1, &owner.2);
            }
        });
        let menu_click = gtk::GestureClick::new();
        menu_click.set_button(3);
        let weak = Rc::downgrade(self);
        let weak_button = button.downgrade();
        let owner = (serial.clone(), family.clone(), input.clone());
        menu_click.connect_pressed(move |_, _, x, y| {
            let (Some(ui), Some(button)) = (weak.upgrade(), weak_button.upgrade()) else {
                return;
            };
            ui.select_input(&owner.0, &owner.1, &owner.2);
            let menu = gio::Menu::new();
            for (title, action) in [
                ("Copy state", "win.copy-input"),
                ("Cut state", "win.cut-input"),
                ("Paste state", "win.paste-input"),
                ("Clear state", "win.clear-input"),
            ] {
                menu.append(Some(title), Some(action));
            }
            let popover = gtk::PopoverMenu::from_model(Some(&menu));
            popover.set_parent(&button);
            popover.set_pointing_to(Some(&gdk::Rectangle::new(x as i32, y as i32, 1, 1)));
            popover.connect_closed(|popover| popover.unparent());
            popover.popup();
        });
        button.add_controller(menu_click);
        let keys = gtk::EventControllerKey::new();
        let weak = Rc::downgrade(self);
        keys.connect_key_pressed(move |_, key, _, modifiers| {
            let Some(ui) = weak.upgrade() else {
                return glib::Propagation::Proceed;
            };
            if modifiers.contains(gdk::ModifierType::CONTROL_MASK)
                && [gdk::Key::c, gdk::Key::x, gdk::Key::v].contains(&key)
            {
                ui.clipboard(key == gdk::Key::v, key == gdk::Key::x);
                return glib::Propagation::Stop;
            }
            if key == gdk::Key::Delete {
                ui.clear_input();
                return glib::Propagation::Stop;
            }
            glib::Propagation::Proceed
        });
        button.add_controller(keys);
        let drop = gtk::DropTarget::new(gio::File::static_type(), gdk::DragAction::COPY);
        let weak = Rc::downgrade(self);
        drop.connect_drop(move |_, value, _, _| {
            if let (Some(ui), Ok(file)) = (weak.upgrade(), value.get::<gio::File>())
                && let Some(path) = file.path()
            {
                ui.select_input(&serial, &family, &input);
                let sel = ui.selection.borrow().clone();
                ui.edit(
                    sel,
                    vec!["media".into(), "path".into()],
                    json!(path.to_string_lossy()),
                );
                return true;
            }
            false
        });
        button.add_controller(drop);
    }
    fn update_previews(
        &self,
        device: &deckard_core::engine::Device,
        frame: &render::Frame,
        rotation: u16,
    ) {
        let sel = self.selection.borrow();
        for preview in self.previews.borrow().iter() {
            if preview.serial != device.serial {
                continue;
            }
            let tile = if preview.physical == 255 {
                frame.strip.as_ref()
            } else {
                frame.tiles.iter().find(|t| t.key == preview.physical)
            };
            let selected = preview.family == sel.family && preview.input == sel.input;
            {
                let (visible, hidden) = if preview.family == "dials" {
                    ("dial-frame-visible", "dial-frame-hidden")
                } else {
                    ("key-button-frame", "key-button-frame-hidden")
                };
                let class = if selected { visible } else { hidden };
                if !preview.frame.has_css_class(class) {
                    preview.frame.remove_css_class(visible);
                    preview.frame.remove_css_class(hidden);
                    preview.frame.add_css_class(class);
                }
            }
            if let Some(tile) = tile {
                if preview.identity.get() != Some(tile.identity) {
                    let bytes = glib::Bytes::from_owned(tile.rgb.clone());
                    let texture = gdk::MemoryTexture::new(
                        tile.width as i32,
                        tile.height as i32,
                        gdk::MemoryFormat::R8g8b8,
                        &bytes,
                        tile.width as usize * 3,
                    );
                    preview.image.set_paintable(Some(&texture));
                    preview.identity.set(Some(tile.identity));
                }
                if selected {
                    self.preview
                        .set_paintable(preview.image.paintable().as_ref());
                }
            }
        }
        if sel.family == "dials"
            && let (Some(strip), Ok(index)) = (&frame.strip, sel.input.parse::<usize>())
        {
            let count = device.kind.encoder_count() as usize;
            if count > 0
                && index < count
                && self.dial_preview.get() != Some((index, strip.identity))
            {
                let vertical = strip.height > strip.width;
                let logical = if rotation % 360 == 270 {
                    count - 1 - index
                } else {
                    index
                };
                let (x, y, w, h) = if vertical {
                    (
                        0,
                        logical * strip.height as usize / count,
                        strip.width as usize,
                        strip.height as usize / count,
                    )
                } else {
                    (
                        logical * strip.width as usize / count,
                        0,
                        strip.width as usize / count,
                        strip.height as usize,
                    )
                };
                let bytes = glib::Bytes::from_owned(strip.rgb.clone());
                let offset = (y * strip.width as usize + x) * 3;
                let bytes = glib::Bytes::from_bytes(&bytes, offset..);
                let texture = gdk::MemoryTexture::new(
                    w as i32,
                    h as i32,
                    gdk::MemoryFormat::R8g8b8,
                    &bytes,
                    strip.width as usize * 3,
                );
                self.preview.set_paintable(Some(&texture));
                self.dial_preview.set(Some((index, strip.identity)));
            }
        }
    }
    pub(super) fn clear_textures(&self) {
        for preview in self.previews.borrow().iter() {
            preview.image.set_paintable(None::<&gdk::Paintable>);
            preview.identity.set(None);
        }
        self.preview.set_paintable(None::<&gdk::Paintable>);
        self.dial_preview.set(None);
    }
    fn rebuild_editor(self: &Rc<Self>, document: &Value) {
        clear(&self.state_box);
        let selection = self.selection.borrow().clone();
        let mut states = document[&selection.family][&selection.input]["states"]
            .as_object()
            .map(|s| {
                s.keys()
                    .filter_map(|s| s.parse::<usize>().ok())
                    .collect::<Vec<_>>()
            })
            .unwrap_or_else(|| vec![0]);
        states.sort_unstable();
        if states.is_empty() {
            states.push(0);
        }
        for state in &states {
            let button = gtk::ToggleButton::with_label(&format!("State {}", state + 1));
            button.add_css_class("deckard-state");
            button.set_active(*state == selection.state);
            button.set_widget_name(&format!("state-{state}"));
            let weak = Rc::downgrade(self);
            let sel = selection.clone();
            let number = *state;
            button.connect_clicked(move |_| {
                if let Some(ui) = weak.upgrade() {
                    ui.selection.borrow_mut().state = number;
                    ui.force_reload.set(true);
                    let mut params = sel.params();
                    params["state"] = json!(number);
                    if !sel.serial.is_empty() {
                        ui.command("change-state", params, "", true);
                    }
                    ui.refresh();
                }
            });
            self.state_box.append(&button);
        }
        let weak = Rc::downgrade(self);
        let plus = button("", "list-add-symbolic", move || {
            if let Some(ui) = weak.upgrade() {
                ui.change_states(true);
            }
        });
        plus.set_widget_name("state-add");
        self.state_box.append(&plus);
        if states.len() > 1 {
            let weak = Rc::downgrade(self);
            self.state_box
                .append(&button("", "list-remove-symbolic", move || {
                    if let Some(ui) = weak.upgrade() {
                        ui.change_states(false);
                    }
                }));
        }
        // The state switcher and selected-key picture are persistent; replace
        // only document controls, and keep each group's expansion state.
        while let Some(child) = self.controls.last_child() {
            if child == self.preview_host.clone().upcast::<gtk::Widget>() {
                break;
            }
            self.controls.remove(&child);
        }
        let (layout, expander) = self.editor_group("Layout", "Layout for this key");
        expander.add_row(&self.bind_file("Image, GIF or video", &["media", "path"]));
        expander.add_row(&self.bind_number("Size", &["media", "size"], 1., 0., 2., 0.01));
        expander.add_row(&self.bind_number(
            "Horizontal alignment",
            &["media", "halign"],
            0.,
            -1.,
            1.,
            0.05,
        ));
        expander.add_row(&self.bind_number(
            "Vertical alignment",
            &["media", "valign"],
            0.,
            -1.,
            1.,
            0.05,
        ));
        expander.add_row(&self.bind_number(
            "Animation FPS cap · 0 = Auto",
            &["media", "fps"],
            0.,
            0.,
            120.,
            1.,
        ));
        expander.add_row(&self.bind_toggle("Loop animation", &["media", "loop"], true));
        self.controls.append(&layout);
        let (background, expander) = self.editor_group("Background", "Background for this key");
        expander.add_row(&self.bind_color("Color", &["background", "color"], [0, 0, 0, 255]));
        self.controls.append(&background);
        let (labels, expander) = self.editor_group("Labels", "Labels for this key");
        for (position, title) in [("top", "Top"), ("center", "Center"), ("bottom", "Bottom")] {
            let (_, label) = controls::expander(title, "");
            label.add_row(&self.bind_text("Text", &["labels", position, "text"], ""));
            label.add_row(&self.bind_text(
                "Font family",
                &["labels", position, "font-family"],
                "Roboto",
            ));
            label.add_row(&self.bind_number(
                "Font size",
                &["labels", position, "font-size"],
                14.,
                6.,
                72.,
                1.,
            ));
            label.add_row(&self.bind_color(
                "Text color",
                &["labels", position, "color"],
                [255, 255, 255, 255],
            ));
            label.add_row(&self.bind_color(
                "Outline color",
                &["labels", position, "outline-color"],
                [0, 0, 0, 255],
            ));
            label.add_row(&self.bind_number(
                "Outline width",
                &["labels", position, "outline-width"],
                2.,
                0.,
                10.,
                1.,
            ));
            expander.add_row(&label);
        }
        self.controls.append(&labels);
        self.action_rows();
        let (more, expander) = self.editor_group("More", "Sticky inputs and testing");
        let weak = Rc::downgrade(self);
        expander.add_row(&toggle(
            "Edit across all pages",
            selection.sticky,
            move |enabled| {
                if let Some(ui) = weak.upgrade() {
                    ui.selection.borrow_mut().sticky = enabled;
                    ui.force_reload.set(true);
                    ui.refresh();
                }
            },
        ));
        let test = adw::ActionRow::builder()
            .title("Test selected input")
            .build();
        let weak = Rc::downgrade(self);
        let sel = selection.clone();
        let run = button("Test", "media-playback-start-symbolic", move || {
            if let Some(ui) = weak.upgrade() {
                if sel.family == "touchscreens" {
                    ui.send_event(&sel.serial, &sel.family, &sel.input, "short-touch", 1);
                } else {
                    ui.send_event(&sel.serial, &sel.family, &sel.input, "press", 1);
                    ui.send_event(&sel.serial, &sel.family, &sel.input, "release", 0);
                }
            }
        });
        test.add_suffix(&run);
        expander.add_row(&test);
        let weak = Rc::downgrade(self);
        let advanced = adw::ActionRow::builder()
            .title("Advanced state")
            .activatable(true)
            .build();
        advanced.connect_activated(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.advanced_state();
            }
        });
        expander.add_row(&advanced);
        self.controls.append(&more);
        self.sidebar.set_visible_child_name("editor");
    }
    pub(super) fn editor_group(
        self: &Rc<Self>,
        title: &str,
        subtitle: &str,
    ) -> (adw::PreferencesGroup, adw::ExpanderRow) {
        let (group, row) = expander(title, subtitle);
        row.set_expanded(
            *self
                .expanded
                .borrow()
                .get(title)
                .unwrap_or(&(title == "Actions")),
        );
        let weak = Rc::downgrade(self);
        let title = title.to_owned();
        row.connect_expanded_notify(move |row| {
            if let Some(ui) = weak.upgrade() {
                ui.expanded
                    .borrow_mut()
                    .insert(title.clone(), row.is_expanded());
            }
        });
        (group, row)
    }
    pub(super) fn change_states(self: &Rc<Self>, add: bool) {
        let sel = self.selection.borrow().clone();
        self.submit(Work {
            key: None,
            title: String::new(),
            rebuild: true,
            execute: Box::new(move |engine| {
                let mut page = sel.document(engine);
                let data = &mut page[&sel.family][&sel.input];
                if !data.is_object() {
                    *data = json!({"states":{"0":{}}});
                }
                if add {
                    let number = data["states"]
                        .as_object()
                        .and_then(|s| s.keys().filter_map(|k| k.parse::<usize>().ok()).max())
                        .unwrap_or(0)
                        + 1;
                    model::state_mut(&mut page, &sel.family, &sel.input, number)?;
                } else {
                    let states = data["states"].as_object_mut().context("States missing")?;
                    anyhow::ensure!(states.len() > 1, "Keep at least one state");
                    states.remove(&sel.state.to_string());
                }
                if sel.sticky {
                    engine.docs.put_sticky(&sel.serial, &page)?;
                } else {
                    engine.docs.put(&sel.page, page)?;
                }
                Ok(Value::Null)
            }),
        });
    }
    fn send_event(&self, serial: &str, family: &str, input: &str, event: &str, value: i32) {
        if self
            .events
            .try_send(InputEvent {
                serial: serial.into(),
                family: family.into(),
                input: input.into(),
                event: event.into(),
                value,
            })
            .is_err()
        {
            self.toast("Input queue busy; try again");
        }
    }
    pub(super) fn clipboard(self: &Rc<Self>, paste: bool, cut: bool) {
        let clipboard = self.window.clipboard();
        let selection = self.selection.borrow().clone();
        if paste {
            let weak = Rc::downgrade(self);
            clipboard.read_text_async(None::<&gio::Cancellable>, move |result| {
                if let (Some(ui), Ok(Some(text))) = (weak.upgrade(), result) {
                    match serde_json::from_str::<Value>(&text) {
                        Ok(document) if document.is_object() => {
                            ui.replace_state(selection, document)
                        }
                        _ => ui.toast("Clipboard does not contain a Deckard input"),
                    }
                }
            });
        } else {
            clipboard.set_text(&self.draft.borrow().to_string());
            if cut {
                self.clear_input();
            }
        }
    }
    pub(super) fn clear_input(self: &Rc<Self>) {
        self.replace_state(self.selection.borrow().clone(), json!({}));
    }
    fn replace_state(self: &Rc<Self>, sel: Selection, document: Value) {
        self.submit(Work {
            key: None,
            title: String::new(),
            rebuild: true,
            execute: Box::new(move |engine| {
                sel.edit(engine, |draft| {
                    *draft = document;
                    Ok(())
                })
            }),
        });
    }
}
