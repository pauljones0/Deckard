//! Direct upstream's crop editor, sharing the native renderer's crop geometry.
use super::*;
use configuration::ConfigTarget;

struct Crop {
    owner: ConfigTarget,
    entries: RefCell<Vec<(String, Value)>>,
    index: Cell<usize>,
    image: RefCell<Option<gtk::gdk_pixbuf::Pixbuf>>,
    area: gtk::DrawingArea,
    spin: gtk::SpinButton,
    loading: Cell<bool>,
    anchor: Cell<Option<(f64, f64)>>,
    last_push: Cell<std::time::Instant>,
    pending: RefCell<Option<glib::SourceId>>,
    canvas: (u32, u32),
    epoch: Cell<u64>,
    dirty: Cell<bool>,
}
impl Crop {
    fn owner_key(&self) -> String {
        match &self.owner {
            ConfigTarget::Device(serial) => format!("device:{serial}"),
            ConfigTarget::Page(page) => format!("page:{page}"),
        }
    }
    fn view(&self) -> Value {
        self.entries.borrow()[self.index.get()].1.clone()
    }
    fn persist(&self, ui: &Rc<Ui>) {
        if !self.dirty.replace(false) {
            return;
        }
        let owner = self.owner.clone();
        let owner_key = self.owner_key();
        let entries = self.entries.borrow().clone();
        let view = self.view();
        ui.submit(Work {
            key: None,
            title: String::new(),
            rebuild: false,
            execute: Box::new(move |engine| {
                let mut document = match &owner {
                    ConfigTarget::Device(_) => engine.docs.settings.clone(),
                    ConfigTarget::Page(page) => engine
                        .docs
                        .pages
                        .get(page)
                        .cloned()
                        .context("Page disappeared")?,
                };
                let background = match &owner {
                    ConfigTarget::Device(serial) => &mut document["devices"][serial]["background"],
                    ConfigTarget::Page(_) => &mut document["background"],
                };
                if let Some(slides) = background["media-paths"]
                    .as_array_mut()
                    .filter(|s| !s.is_empty())
                {
                    for item in slides {
                        let path = item
                            .as_str()
                            .or_else(|| item["path"].as_str())
                            .unwrap_or("");
                        if let Some((_, view)) = entries.iter().find(|(p, _)| p == path) {
                            if item.is_string() {
                                *item = json!({"path":path});
                            }
                            item["view"] = view.clone();
                        }
                    }
                } else {
                    background["view"] = view;
                }
                let command = match owner {
                    ConfigTarget::Device(_) => json!({"method":"put-settings", "params":document}),
                    ConfigTarget::Page(page) => {
                        json!({"method":"put-page", "params":{"name":page, "document":document}})
                    }
                };
                let result = engine.command(&command);
                engine.clear_background_view_preview(&owner_key);
                result
            }),
        });
    }
    fn apply(&self, ui: &Rc<Ui>, mut view: Value, commit: bool) {
        if let Some(image) = self.image.borrow().as_ref() {
            let source = (image.width() as u32, image.height() as u32);
            let (l, t, r, b) = render::viewport_rect(source, self.canvas, &view);
            view["x"] = json!(((l + r) / 2. / source.0 as f64).clamp(0., 1.));
            view["y"] = json!(((t + b) / 2. / source.1 as f64).clamp(0., 1.));
        }
        self.entries.borrow_mut()[self.index.get()].1 = view;
        self.dirty.set(true);
        self.area.queue_draw();
        if commit || self.last_push.get().elapsed() >= Duration::from_millis(100) {
            self.last_push.set(std::time::Instant::now());
            if commit {
                self.persist(ui);
            } else {
                let owner = self.owner_key();
                let (path, view) = self.entries.borrow()[self.index.get()].clone();
                ui.submit(Work {
                    key: Some(format!("crop-preview/{owner}")),
                    title: String::new(),
                    rebuild: false,
                    execute: Box::new(move |engine| {
                        engine.preview_background_view(&owner, &path, view);
                        Ok(Value::Null)
                    }),
                });
            }
        }
    }
    fn load(self: &Rc<Self>, index: usize) {
        self.index.set(index);
        self.loading.set(true);
        self.spin
            .set_value(self.view()["scale"].as_f64().unwrap_or(1.));
        self.loading.set(false);
        self.image.borrow_mut().take();
        self.epoch.set(self.epoch.get() + 1);
        let epoch = self.epoch.get();
        let path = self.entries.borrow()[index].0.clone();
        let (send, receive) = async_channel::bounded(1);
        std::thread::spawn(move || {
            let _ = send.send_blocking(super::assets::decode_image(
                &std::path::PathBuf::from(path),
                1120,
                680,
            ));
        });
        let weak = Rc::downgrade(self);
        glib::MainContext::default().spawn_local(async move {
            if let (Ok(Some(image)), Some(crop)) = (receive.recv().await, weak.upgrade())
                && crop.epoch.get() == epoch
            {
                let bytes = glib::Bytes::from_owned(image.pixels);
                let pixbuf = gtk::gdk_pixbuf::Pixbuf::from_bytes(
                    &bytes,
                    gtk::gdk_pixbuf::Colorspace::Rgb,
                    image.alpha,
                    8,
                    image.width,
                    image.height,
                    image.stride as i32,
                );
                *crop.image.borrow_mut() = Some(pixbuf);
                crop.area.queue_draw();
            }
        });
    }
    fn flush(&self, ui: &Rc<Ui>) {
        if let Some(timer) = self.pending.borrow_mut().take() {
            timer.remove();
        }
        self.persist(ui);
    }
}
impl Ui {
    pub(super) fn viewport(self: &Rc<Self>, target: &ConfigTarget) {
        let document = target.document(self);
        let mut entries = document["background"]["media-paths"]
            .as_array()
            .map(|slides| {
                slides
                    .iter()
                    .filter_map(|item| {
                        let path = item.as_str().or_else(|| item["path"].as_str())?;
                        Some((
                            path.to_owned(),
                            if item["view"].is_object() {
                                item["view"].clone()
                            } else {
                                json!({"x":0.5,"y":0.5,"scale":1.})
                            },
                        ))
                    })
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        if entries.is_empty()
            && let Some(path) = document["background"]["media-path"]
                .as_str()
                .filter(|s| !s.is_empty())
        {
            entries.push((
                path.into(),
                if document["background"]["view"].is_object() {
                    document["background"]["view"].clone()
                } else {
                    json!({"x":0.5,"y":0.5,"scale":1.})
                },
            ));
        }
        if entries.is_empty() {
            self.toast("Choose background media first");
            return;
        }
        let dialog = adw::Dialog::builder().title("Background View").build();
        let toolbar = adw::ToolbarView::new();
        toolbar.add_top_bar(&adw::HeaderBar::new());
        dialog.set_child(Some(&toolbar));
        let body = gtk::Box::builder()
            .orientation(gtk::Orientation::Vertical)
            .spacing(10)
            .margin_top(10)
            .margin_bottom(15)
            .margin_start(15)
            .margin_end(15)
            .build();
        toolbar.set_content(Some(&body));
        let area = gtk::DrawingArea::builder()
            .content_width(560)
            .content_height(340)
            .halign(gtk::Align::Center)
            .build();
        let spin = gtk::SpinButton::with_range(0.25, 8., 0.05);
        spin.set_digits(2);
        let engine = self.shared.lock().unwrap();
        let serial = match target {
            ConfigTarget::Device(s) => s,
            ConfigTarget::Page(_) => &self.selection.borrow().serial.clone(),
        };
        let kind = engine.devices.get(serial).map(|d| {
            (
                d.kind,
                d.key_size,
                engine.docs.settings["devices"][serial]["rotation"]
                    .as_u64()
                    .unwrap_or(0) as u16,
            )
        });
        drop(engine);
        let canvas = kind
            .map(|(kind, size, rotation)| {
                let (rows, cols) = render::layout(kind, rotation);
                let (sx, sy) = render::key_spacing(kind, rotation);
                let grid = (
                    cols as u32 * size.0 as u32 + sx * u32::from(cols - 1),
                    rows as u32 * size.1 as u32 + sy * u32::from(rows - 1),
                );
                let strip = if document["background"]["extend-to-touchscreen"]
                    .as_bool()
                    .unwrap_or(false)
                {
                    kind.lcd_strip_size().map(|(w, h)| (w as u32, h as u32))
                } else {
                    None
                };
                let plus = kind == deckard_core::Kind::Plus;
                deckard_core::geometry::background_band(
                    grid,
                    strip,
                    rotation,
                    plus,
                    if plus { 34 } else { 36 },
                )
                .canvas
            })
            .unwrap_or((360, 240));
        let crop = Rc::new(Crop {
            owner: target.clone(),
            entries: RefCell::new(entries),
            index: Cell::new(0),
            image: RefCell::new(None),
            area: area.clone(),
            spin: spin.clone(),
            loading: Cell::new(false),
            anchor: Cell::new(None),
            last_push: Cell::new(std::time::Instant::now() - Duration::from_secs(1)),
            pending: RefCell::new(None),
            canvas,
            epoch: Cell::new(0),
            dirty: Cell::new(false),
        });
        if crop.entries.borrow().len() > 1 {
            let line = gtk::Box::new(gtk::Orientation::Horizontal, 10);
            line.append(
                &gtk::Label::builder()
                    .label("Image")
                    .hexpand(true)
                    .xalign(0.)
                    .build(),
            );
            let names = crop
                .entries
                .borrow()
                .iter()
                .map(|(p, _)| {
                    std::path::Path::new(p)
                        .file_name()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .into_owned()
                })
                .collect::<Vec<_>>();
            let names = names.iter().map(String::as_str).collect::<Vec<_>>();
            let picker = gtk::DropDown::from_strings(&names);
            line.append(&picker);
            body.append(&line);
            let weak = Rc::downgrade(&crop);
            let ui = Rc::downgrade(self);
            picker.connect_selected_notify(move |picker| {
                if let (Some(crop), Some(ui)) = (weak.upgrade(), ui.upgrade()) {
                    crop.flush(&ui);
                    crop.load(picker.selected() as usize);
                }
            });
        }
        body.append(&area);
        let hint = gtk::Label::builder()
            .label("Drag the white box to choose the visible area")
            .halign(gtk::Align::Center)
            .build();
        hint.add_css_class("dim-label");
        body.append(&hint);
        let line = gtk::Box::new(gtk::Orientation::Horizontal, 10);
        line.append(
            &gtk::Label::builder()
                .label("Zoom factor")
                .hexpand(true)
                .xalign(0.)
                .build(),
        );
        line.append(&spin);
        body.append(&line);
        let reset = gtk::Button::builder()
            .label("Reset view")
            .halign(gtk::Align::End)
            .build();
        body.append(&reset);
        let weak = Rc::downgrade(&crop);
        area.set_draw_func(move |_, cr, width, height| {
            let Some(crop) = weak.upgrade() else {
                return;
            };
            let image = crop.image.borrow();
            let Some(image) = image.as_ref() else {
                return;
            };
            let scale =
                (width as f64 / image.width() as f64).min(height as f64 / image.height() as f64);
            let ox = (width as f64 - image.width() as f64 * scale) / 2.;
            let oy = (height as f64 - image.height() as f64 * scale) / 2.;
            let _ = cr.save();
            cr.translate(ox, oy);
            cr.scale(scale, scale);
            cr.set_source_pixbuf(image, 0., 0.);
            let _ = cr.paint();
            let _ = cr.restore();
            let (l, t, r, b) = render::viewport_rect(
                (image.width() as u32, image.height() as u32),
                crop.canvas,
                &crop.view(),
            );
            let (l, t, r, b) = (
                ox + l * scale,
                oy + t * scale,
                ox + r * scale,
                oy + b * scale,
            );
            cr.set_source_rgba(0., 0., 0., 0.45);
            cr.rectangle(0., 0., width as f64, t.max(0.));
            cr.rectangle(0., b, width as f64, (height as f64 - b).max(0.));
            cr.rectangle(0., t.max(0.), l.max(0.), b - t);
            cr.rectangle(r, t.max(0.), (width as f64 - r).max(0.), b - t);
            let _ = cr.fill();
            cr.set_source_rgba(1., 1., 1., 1.);
            cr.set_line_width(2.);
            cr.rectangle(l, t, r - l, b - t);
            let _ = cr.stroke();
        });
        let drag = gtk::GestureDrag::new();
        let weak = Rc::downgrade(&crop);
        drag.connect_drag_begin(move |_, _, _| {
            if let Some(crop) = weak.upgrade() {
                let view = crop.view();
                crop.anchor.set(Some((
                    view["x"].as_f64().unwrap_or(0.5),
                    view["y"].as_f64().unwrap_or(0.5),
                )));
            }
        });
        let weak = Rc::downgrade(&crop);
        let ui = Rc::downgrade(self);
        drag.connect_drag_update(move |_, dx, dy| {
            if let (Some(crop), Some(ui)) = (weak.upgrade(), ui.upgrade())
                && let Some((x, y)) = crop.anchor.get()
            {
                let image = crop.image.borrow();
                let Some(image) = image.as_ref() else {
                    return;
                };
                let scale = (crop.area.width() as f64 / image.width() as f64)
                    .min(crop.area.height() as f64 / image.height() as f64);
                let mut view = crop.view();
                view["x"] = json!((x + dx / scale / image.width() as f64).clamp(0., 1.));
                view["y"] = json!((y + dy / scale / image.height() as f64).clamp(0., 1.));
                crop.apply(&ui, view, false);
            }
        });
        let weak = Rc::downgrade(&crop);
        let ui = Rc::downgrade(self);
        drag.connect_drag_end(move |_, dx, dy| {
            if let (Some(crop), Some(ui)) = (weak.upgrade(), ui.upgrade()) {
                crop.anchor.set(None);
                if dx != 0. || dy != 0. {
                    crop.apply(&ui, crop.view(), true);
                }
            }
        });
        area.add_controller(drag);
        let weak = Rc::downgrade(&crop);
        let ui = Rc::downgrade(self);
        spin.connect_value_changed(move |spin| {
            if let (Some(crop), Some(ui)) = (weak.upgrade(), ui.upgrade())
                && !crop.loading.get()
            {
                if let Some(timer) = crop.pending.borrow_mut().take() {
                    timer.remove();
                }
                let mut view = crop.view();
                view["scale"] = json!(spin.value());
                crop.apply(&ui, view, false);
                let weak = Rc::downgrade(&crop);
                let ui = Rc::downgrade(&ui);
                *crop.pending.borrow_mut() = Some(glib::timeout_add_local_once(
                    Duration::from_millis(400),
                    move || {
                        if let (Some(crop), Some(ui)) = (weak.upgrade(), ui.upgrade()) {
                            crop.pending.borrow_mut().take();
                            crop.persist(&ui);
                        }
                    },
                ));
            }
        });
        let weak = Rc::downgrade(&crop);
        let ui = Rc::downgrade(self);
        reset.connect_clicked(move |_| {
            if let (Some(crop), Some(ui)) = (weak.upgrade(), ui.upgrade()) {
                crop.flush(&ui);
                crop.loading.set(true);
                crop.spin.set_value(1.);
                crop.loading.set(false);
                crop.apply(&ui, json!({"x":0.5,"y":0.5,"scale":1.}), true);
            }
        });
        crop.load(0);
        let ui = Rc::downgrade(self);
        dialog.connect_closed(move |_| {
            if let Some(ui) = ui.upgrade() {
                crop.flush(&ui);
            }
        });
        dialog.present(Some(&self.window));
    }
}
