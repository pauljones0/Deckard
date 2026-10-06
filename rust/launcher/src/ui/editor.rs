use super::*;
use controls::*;

fn device_model_name(kind: deckard_core::Kind) -> &'static str {
    use deckard_core::Kind::*;
    match kind {
        Original | OriginalV2 => "Stream Deck",
        Mini | MiniMk2 | MiniDiscord | MiniMk2Module => "Stream Deck Mini",
        Xl | XlV2 | XlV2Module => "Stream Deck XL",
        Mk2 | Mk2Scissor | Mk2Module => "Stream Deck MK.2",
        Plus => "Stream Deck +",
        PlusXl => "Stream Deck + XL",
        Neo => "Stream Deck Neo",
        Studio => "Stream Deck Studio",
        Pedal => "Stream Deck Pedal",
        Mirabox293s => "Stream Dock 293S",
        UlanziD200 => "Ulanzi D200",
    }
}

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
        for device in &devices {
            if let Some((banner, previous)) = self.fps_banners.borrow().get(&device.serial) {
                let enabled = settings["warnings"]["enable-fps-warnings"]
                    .as_bool()
                    .or_else(|| settings["ui"]["enable-fps-warnings"].as_bool())
                    .unwrap_or(true);
                let shown = enabled && device.low_fps;
                if previous.replace(shown) != shown {
                    banner.set_revealed(shown);
                }
            }
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
        if *self.deck_signature.borrow() != signature
            || (devices.is_empty()
                && self.content_stack.visible_child_name().as_deref() != Some("empty"))
        {
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
            if let Some(manager) = self.page_manager.borrow().clone() {
                manager.refresh(self);
            }
            *self.page_names.borrow_mut() = pages;
        }
        self.page_label.set_text(&self.selection.borrow().page);
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
        let search = gtk::SearchEntry::builder()
            .placeholder_text("Search pages")
            .build();
        content.append(&search);
        let list = gtk::ListBox::new();
        list.set_selection_mode(gtk::SelectionMode::Single);
        list.add_css_class("navigation-sidebar");
        let hint = gtk::Label::builder()
            .label("No pages found")
            .margin_top(12)
            .margin_bottom(12)
            .build();
        hint.add_css_class("dim-label");
        list.set_placeholder(Some(&hint));
        let scroll = gtk::ScrolledWindow::builder()
            .max_content_height(300)
            .propagate_natural_height(true)
            .hscrollbar_policy(gtk::PolicyType::Never)
            .focusable(false)
            .child(&list)
            .build();
        content.append(&scroll);
        for page in pages {
            let row = gtk::ListBoxRow::new();
            row.set_widget_name(page);
            row.set_child(Some(
                &gtk::Label::builder()
                    .label(page)
                    .xalign(0.)
                    .hexpand(true)
                    .ellipsize(gtk::pango::EllipsizeMode::End)
                    .max_width_chars(30)
                    .build(),
            ));
            list.append(&row);
            if page == &self.selection.borrow().page {
                list.select_row(Some(&row));
            }
        }
        let query = Rc::new(RefCell::new(String::new()));
        let scores = Rc::new(RefCell::new(HashMap::<String, (u8, i32)>::new()));
        let rank = {
            let query = query.clone();
            let scores = scores.clone();
            move |name: &str| {
                if let Some(rank) = scores.borrow().get(name) {
                    return *rank;
                }
                let lower = name.to_lowercase();
                let query = query.borrow();
                let score = search::fuzzy_ratio(&lower, &query).round() as i32;
                let tier = if query.is_empty() || lower.starts_with(&*query) {
                    3
                } else if lower.contains(&*query) {
                    2
                } else if score > 50 {
                    1
                } else {
                    0
                };
                let rank = (tier, score);
                scores.borrow_mut().insert(name.into(), rank);
                rank
            }
        };
        list.set_filter_func({
            let rank = rank.clone();
            move |row| rank(&row.widget_name()).0 > 0
        });
        list.set_sort_func({
            let rank = rank.clone();
            let query = query.clone();
            move |a, b| {
                let names = (a.widget_name(), b.widget_name());
                let order = if query.borrow().is_empty() {
                    search::natural_cmp(&names.0, &names.1)
                } else {
                    rank(&names.1)
                        .cmp(&rank(&names.0))
                        .then_with(|| search::natural_cmp(&names.0, &names.1))
                };
                order.into()
            }
        });
        let weak_list = list.downgrade();
        search.connect_search_changed(move |search| {
            *query.borrow_mut() = search.text().to_lowercase();
            scores.borrow_mut().clear();
            if let Some(list) = weak_list.upgrade() {
                list.invalidate_filter();
                list.invalidate_sort();
                if let Some(row) = first_visible_row(&list) {
                    list.select_row(Some(&row));
                }
            }
        });
        let weak = Rc::downgrade(self);
        let pop = popover.downgrade();
        list.connect_row_activated(move |_, row| {
            if let Some(ui) = weak.upgrade() {
                ui.change_page(&row.widget_name());
            }
            if let Some(pop) = pop.upgrade() {
                pop.popdown();
            }
        });
        let keys = gtk::EventControllerKey::new();
        let weak_list = list.downgrade();
        keys.connect_key_pressed(move |_, key, _, _| {
            let Some(list) = weak_list.upgrade() else {
                return glib::Propagation::Proceed;
            };
            if key == gdk::Key::Return {
                if let Some(row) = list.selected_row() {
                    row.emit_by_name::<()>("activate", &[]);
                }
                return glib::Propagation::Stop;
            }
            if key == gdk::Key::Up || key == gdk::Key::Down {
                let mut rows = Vec::new();
                let mut child = list.first_child();
                while let Some(widget) = child {
                    child = widget.next_sibling();
                    if let Ok(row) = widget.downcast::<gtk::ListBoxRow>()
                        && row.is_visible()
                    {
                        rows.push(row);
                    }
                }
                if !rows.is_empty() {
                    let current = rows
                        .iter()
                        .position(|row| Some(row) == list.selected_row().as_ref())
                        .unwrap_or(0);
                    let next = if key == gdk::Key::Down {
                        (current + 1).min(rows.len() - 1)
                    } else {
                        current.saturating_sub(1)
                    };
                    list.select_row(Some(&rows[next]));
                }
                return glib::Propagation::Stop;
            }
            glib::Propagation::Proceed
        });
        search.add_controller(keys);
        let weak = Rc::downgrade(self);
        let weak_list = list.downgrade();
        let weak_search = search.downgrade();
        let weak_scroll = scroll.downgrade();
        popover.connect_show(move |_| {
            if let (Some(ui), Some(list)) = (weak.upgrade(), weak_list.upgrade()) {
                let active = ui.selection.borrow().page.clone();
                let mut child = list.first_child();
                while let Some(widget) = child {
                    child = widget.next_sibling();
                    if let Ok(row) = widget.downcast::<gtk::ListBoxRow>()
                        && row.widget_name() == active
                    {
                        list.select_row(Some(&row));
                        let weak_row = row.downgrade();
                        let scroll = weak_scroll.clone();
                        glib::idle_add_local_once(move || {
                            if let (Some(row), Some(scroll)) =
                                (weak_row.upgrade(), scroll.upgrade())
                            {
                                let adjustment = scroll.vadjustment();
                                adjustment.set_value(
                                    row.compute_bounds(scroll.child().as_ref().unwrap())
                                        .map_or(0., |r| r.y() as f64)
                                        .min(adjustment.upper() - adjustment.page_size())
                                        .max(0.),
                                );
                            }
                        });
                        break;
                    }
                }
            }
            if let Some(search) = weak_search.upgrade() {
                search.grab_focus();
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
        let empty = devices.is_empty();
        self.content_stack
            .set_visible_child_name(if empty { "empty" } else { "decks" });
        self.deck_titles
            .set_visible_child_name(if empty { "empty" } else { "decks" });
        self.split.set_collapsed(empty);
        if empty {
            self.split.set_show_content(true);
        }
        self.deck_settings.set_visible(!empty);
        self.main_menu.set_visible(true);
        for name in ["store", "settings"] {
            if let Some(action) = self
                .window
                .lookup_action(name)
                .and_downcast::<gio::SimpleAction>()
            {
                action.set_enabled(!empty);
            }
        }
        if devices.is_empty() {
            self.preview.set_paintable(None::<&gdk::Paintable>);
        }
        self.fps_banners.borrow_mut().clear();
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
                image.set_hexpand(true);
                image.set_vexpand(true);
                image.add_css_class("key-image");
                image.add_css_class("key-button");
                image.set_overflow(gtk::Overflow::Hidden);
                image.set_widget_name(&format!("key-{serial}-{input}"));
                let frame = gtk::Frame::new(None);
                frame.set_child(Some(&image));
                frame.add_css_class("key-button-frame-hidden");
                grid.attach(
                    &frame,
                    i32::from(index % cols),
                    i32::from(index / cols),
                    1,
                    1,
                );
                self.connect_input(&image, serial, "keys", &input);
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
                let image = if rotation % 180 == 90 {
                    PreviewImage::new(48, 384)
                } else {
                    PreviewImage::new(384, 48)
                };
                image.add_css_class("plus-screenbar-image");
                image.set_overflow(gtk::Overflow::Hidden);
                self.connect_input(
                    &image,
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
                frame.set_child(Some(&image));
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
                dials.set_hexpand(true);
                dials.set_homogeneous(true);
                body.append(&dials);
                for index in 0..device.kind.encoder_count() {
                    let button = gtk::Image::new();
                    button.add_css_class("dial");
                    button.set_widget_name(&format!("dial-{serial}-{index}"));
                    button.set_tooltip_text(Some(&format!("Dial {}", index + 1)));
                    let frame = gtk::Frame::new(None);
                    frame.set_halign(gtk::Align::Center);
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
                .filter(|name| !name.trim().is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| {
                    let model = device_model_name(device.kind);
                    if serial == "remote-deck-1" {
                        "Remote Deck 1".into()
                    } else if device.fake {
                        let index = serial
                            .rsplit('-')
                            .next()
                            .and_then(|s| s.parse::<usize>().ok())
                            .unwrap_or(0)
                            + 1;
                        format!("Fake Deck {index} ({model})")
                    } else {
                        model.into()
                    }
                });
            let view = gtk::Stack::builder().hexpand(true).vexpand(true).build();
            view.add_named(&scroll, Some("key-grid"));
            if self.deck_settings_visible.get() {
                view.add_named(&self.deck_settings_view(serial), Some("deck-settings"));
                view.set_visible_child_name("deck-settings");
            }
            let main = gtk::Box::new(gtk::Orientation::Vertical, 0);
            let banner = adw::Banner::builder().title("Low FPS detected. This might be caused by your background video. This may dissapear after the caching is finished.").button_label("Dismiss").revealed(device.low_fps).build();
            banner.connect_button_clicked(|banner| banner.set_revealed(false));
            self.fps_banners
                .borrow_mut()
                .insert(serial.clone(), (banner.clone(), Cell::new(device.low_fps)));
            main.append(&banner);
            main.append(&view);
            self.deck_stack.add_titled(&main, Some(serial), &title);
        }
        self.deck_stack
            .set_visible_child_name(&self.selection.borrow().serial);
        self.loading_shell.set(false);
    }
    fn connect_input(
        self: &Rc<Self>,
        button: &impl IsA<gtk::Widget>,
        serial: &str,
        family: &str,
        input: &str,
    ) {
        let weak = Rc::downgrade(self);
        let serial = serial.to_owned();
        let family = family.to_owned();
        let input = input.to_owned();
        let owner = (serial.clone(), family.clone(), input.clone());
        button.set_focusable(true);
        let primary = gtk::GestureClick::new();
        primary.set_button(1);
        let widget = button.clone().upcast::<gtk::Widget>().downgrade();
        primary.connect_pressed(move |_, count, _, _| {
            if let Some(ui) = weak.upgrade() {
                ui.select_input(&owner.0, &owner.1, &owner.2);
                if let Some(widget) = widget.upgrade() {
                    widget.grab_focus();
                }
                if count == 2
                    && ui.shared.lock().unwrap().docs.settings["ui"]["emulate-at-double-click"]
                        .as_bool()
                        .unwrap_or(true)
                {
                    ui.send_event(&owner.0, &owner.1, &owner.2, "press", 1);
                    ui.send_event(&owner.0, &owner.1, &owner.2, "release", 0);
                }
            }
        });
        button.add_controller(primary);
        let menu_click = gtk::GestureClick::new();
        menu_click.set_button(3);
        let weak = Rc::downgrade(self);
        let weak_button = button.clone().upcast::<gtk::Widget>().downgrade();
        let owner = (serial.clone(), family.clone(), input.clone());
        menu_click.connect_pressed(move |_, _, x, y| {
            let (Some(ui), Some(button)) = (weak.upgrade(), weak_button.upgrade()) else {
                return;
            };
            ui.select_input(&owner.0, &owner.1, &owner.2);
            let menu = gio::Menu::new();
            for (title, action) in [
                ("Copy", "win.copy-input"),
                ("Cut", "win.cut-input"),
                ("Paste", "win.paste-input"),
                ("Remove", "win.clear-input"),
                ("Update", "win.update-input"),
            ] {
                menu.append(Some(title), Some(action));
            }
            let popover = gtk::PopoverMenu::from_model(Some(&menu));
            popover.set_has_arrow(false);
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
                    // An unconfigured touch LCD is transparent in upstream's
                    // editor. Its physical device frame still remains black.
                    let blank_strip = preview.physical == 255
                        && tile.rgb.iter().all(|channel| *channel == 0)
                        && self
                            .shared
                            .lock()
                            .unwrap()
                            .config(&device.serial)
                            .is_some_and(|config| {
                                let state = render::effective_input(
                                    &config,
                                    &preview.family,
                                    &preview.input,
                                )["states"]["0"]
                                    .clone();
                                config.page["background"]["media-path"]
                                    .as_str()
                                    .unwrap_or("")
                                    .is_empty()
                                    && config.page["background"]["media-paths"]
                                        .as_array()
                                        .is_none_or(Vec::is_empty)
                                    && state["media"]["path"].as_str().unwrap_or("").is_empty()
                                    && state["background"]["media-path"]
                                        .as_str()
                                        .unwrap_or("")
                                        .is_empty()
                                    && state["background"]["color"].is_null()
                            });
                    if blank_strip {
                        preview.image.set_paintable(None::<&gdk::Paintable>);
                        preview.identity.set(Some(tile.identity));
                        continue;
                    }
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
                if strip.rgb.iter().all(|channel| *channel == 0)
                    && self.previews.borrow().iter().any(|preview| {
                        preview.physical == 255 && preview.image.paintable().is_none()
                    })
                {
                    self.preview.set_paintable(None::<&gdk::Paintable>);
                    self.dial_preview.set(Some((index, strip.identity)));
                    return;
                }
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
    pub(super) fn rebuild_editor(self: &Rc<Self>, document: &Value) {
        clear(&self.state_box);
        let selection = self.selection.borrow().clone();
        let changed_input = self.loaded_selection.borrow().as_ref() != Some(&selection);
        *self.loaded_selection.borrow_mut() = Some(selection.clone());
        let touchscreen = matches!(selection.family.as_str(), "touchscreens" | "infobar");
        self.state_box
            .parent()
            .and_then(|viewport| viewport.parent())
            .unwrap()
            .set_visible(!touchscreen);
        if touchscreen && self.controls.parent().as_ref() != Some(self.screen_clamp.upcast_ref()) {
            self.editor_scroll.set_child(None::<&gtk::Widget>);
            self.screen_clamp.set_child(Some(&self.controls));
            self.editor_scroll.set_child(Some(&self.screen_clamp));
        } else if !touchscreen
            && self.controls.parent().as_ref() == Some(self.screen_clamp.upcast_ref())
        {
            self.screen_clamp.set_child(None::<&gtk::Widget>);
            self.editor_scroll.set_child(Some(&self.controls));
        }
        if let Some(parent) = self.remove_state.parent().and_downcast::<gtk::Box>() {
            parent.remove(&self.remove_state);
        }
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
        let stack = gtk::Stack::new();
        for state in &states {
            stack.add_titled(
                &gtk::Box::new(gtk::Orientation::Horizontal, 0),
                Some(&state.to_string()),
                &format!("State {}", state + 1),
            );
        }
        stack.set_visible_child_name(&selection.state.to_string());
        let switcher = gtk::StackSwitcher::builder().stack(&stack).build();
        switcher.add_css_class("state-switcher");
        let weak = Rc::downgrade(self);
        let owner = selection.clone();
        stack.connect_visible_child_name_notify(move |stack| {
            if let (Some(ui), Some(state)) = (weak.upgrade(), stack.visible_child_name())
                && let Ok(number) = state.parse::<usize>()
            {
                ui.selection.borrow_mut().state = number;
                ui.force_reload.set(true);
                let mut params = owner.params();
                params["state"] = json!(number);
                if !owner.serial.is_empty() {
                    ui.command("change-state", params, "", true);
                }
                ui.refresh();
            }
        });
        self.state_box.append(&switcher);
        let weak = Rc::downgrade(self);
        let plus = button("", "list-add-symbolic", move || {
            if let Some(ui) = weak.upgrade() {
                ui.change_states(true);
            }
        });
        plus.set_widget_name("state-add");
        self.state_box.append(&plus);
        self.remove_state.set_visible(states.len() > 1);
        while let Some(child) = self.controls.last_child() {
            if child == self.preview_host.clone().upcast::<gtk::Widget>() {
                break;
            }
            self.controls.remove(&child);
        }
        self.screen_title.set_visible(touchscreen);
        self.preview_host.set_visible(!touchscreen);
        self.preview.set_logical_size(
            if selection.family == "dials" {
                200
            } else {
                175
            },
            if selection.family == "dials" {
                100
            } else {
                175
            },
        );
        self.preview.remove_css_class("icon-selector-image-key");
        self.preview.remove_css_class("icon-selector-image-dial");
        self.preview.add_css_class(if selection.family == "dials" {
            "icon-selector-image-dial"
        } else {
            "icon-selector-image-key"
        });
        self.preview.remove_css_class("icon-selector-image-key");
        self.preview.remove_css_class("icon-selector-image-dial");
        self.preview.add_css_class(if selection.family == "dials" {
            "icon-selector-image-dial"
        } else {
            "icon-selector-image-key"
        });
        self.remove_icon.set_visible(
            self.draft.borrow()["media"]["path"]
                .as_str()
                .is_some_and(|p| !p.is_empty()),
        );
        if !touchscreen {
            let (layout, expander) = self.editor_group("Layout", "Layout for this key");
            expander.add_row(&self.upstream_spin_row(
                "Size (%)",
                &["media", "size"],
                1.,
                0.,
                200.,
                1.,
                100.,
            ));
            expander.add_row(&self.upstream_spin_row(
                "VAlign",
                &["media", "valign"],
                0.,
                -1.,
                1.,
                0.1,
                1.,
            ));
            expander.add_row(&self.upstream_spin_row(
                "HAlign",
                &["media", "halign"],
                0.,
                -1.,
                1.,
                0.1,
                1.,
            ));
            self.append_editor_panel(&layout, 90);
        }
        let (background, expander) = self.editor_group("Background", "Background for this key");
        expander.add_row(&self.upstream_color_row("Color", &["background", "color"], [0, 0, 0, 0]));
        if touchscreen {
            expander.add_row(&self.upstream_media_row());
        }
        let animated = self.draft.borrow()["media"]["path"]
            .as_str()
            .is_some_and(|path| {
                let ext = std::path::Path::new(path)
                    .extension()
                    .and_then(|s| s.to_str())
                    .unwrap_or("")
                    .to_ascii_lowercase();
                matches!(ext.as_str(), "gif" | "mp4" | "webm" | "mov" | "mkv" | "avi")
            });
        if animated {
            if touchscreen {
                expander.add_row(&self.upstream_loop_row());
            }
            expander.add_row(&self.upstream_spin_row(
                "FPS",
                &["media", "fps"],
                0.,
                0.,
                120.,
                1.,
                1.,
            ));
        }
        self.append_editor_panel(&background, 25);
        if !touchscreen {
            let (labels, expander) = self.editor_group("Labels", "Labels for this key");
            for (position, title) in [("top", "Top"), ("center", "Center"), ("bottom", "Bottom")] {
                expander.add_row(&self.upstream_label_row(position, title));
            }
            self.append_editor_panel(&labels, 25);
        }
        self.action_rows();
        if changed_input {
            self.editor_scroll.vadjustment().set_value(0.);
        }
        if touchscreen {
            self.controls.append(&self.remove_state);
        } else {
            self.editor_body.append(&self.remove_state);
        }
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

fn first_visible_row(list: &gtk::ListBox) -> Option<gtk::ListBoxRow> {
    let mut child = list.first_child();
    while let Some(widget) = child {
        child = widget.next_sibling();
        if let Ok(row) = widget.downcast::<gtk::ListBoxRow>()
            && row.is_visible()
        {
            return Some(row);
        }
    }
    None
}
