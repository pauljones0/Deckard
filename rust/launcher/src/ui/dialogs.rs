use super::*;
use controls::*;

impl Ui {
    pub(super) fn dialog(&self, title: &str) -> (adw::Dialog, gtk::Box) {
        let dialog = adw::Dialog::builder()
            .title(title)
            .content_width(700)
            .content_height(650)
            .build();
        let toolbar = adw::ToolbarView::new();
        let header = adw::HeaderBar::new();
        toolbar.add_top_bar(&header);
        let scroll = gtk::ScrolledWindow::builder()
            .vexpand(true)
            .hscrollbar_policy(gtk::PolicyType::Never)
            .build();
        let body = gtk::Box::new(gtk::Orientation::Vertical, 24);
        margins(&body, 24);
        scroll.set_child(Some(&body));
        toolbar.set_content(Some(&scroll));
        dialog.set_child(Some(&toolbar));
        (dialog, body)
    }
    pub(super) fn about(&self) {
        let dialog=adw::AboutDialog::builder().application_name("Deckard").application_icon("io.github.nazbert.Deckard")
            .version(env!("CARGO_PKG_VERSION")).developer_name("Deckard contributors")
            .website("https://github.com/pauljones0/Deckard").issue_url("https://github.com/pauljones0/Deckard/issues")
            .license_type(gtk::License::Gpl30).comments("Stream Deck controller for Linux. Rust engine and GTK editor. Based on StreamController by Core447 and Deckard by nazbert.").build();
        dialog.present(Some(&self.window));
    }
    pub(super) fn show_report(&self, title: &str, value: &Value) {
        let (dialog, body) = self.dialog(title);
        if let Some(documents) = value["documents"].as_array() {
            let converted: usize = documents
                .iter()
                .map(|d| d["converted"].as_array().map_or(0, Vec::len))
                .sum();
            let remaining: usize = documents
                .iter()
                .map(|d| d["remaining"].as_array().map_or(0, Vec::len))
                .sum();
            let summary = gtk::Label::new(Some(&format!(
                "{converted} supported actions · {remaining} need a replacement"
            )));
            summary.set_wrap(true);
            body.append(&summary);
            for doc in documents {
                if let Some(items) = doc["remaining"].as_array() {
                    for item in items {
                        let row = adw::ActionRow::builder()
                            .title(item["id"].as_str().unwrap_or("Unknown action"))
                            .subtitle(format!(
                                "{} · {} {} · {}",
                                item["document"].as_str().unwrap_or(""),
                                item["family"].as_str().unwrap_or(""),
                                item["input"].as_str().unwrap_or(""),
                                item["reason"].as_str().unwrap_or("")
                            ))
                            .build();
                        body.append(&row);
                    }
                }
            }
        } else {
            let view = gtk::TextView::builder()
                .editable(false)
                .monospace(true)
                .wrap_mode(gtk::WrapMode::WordChar)
                .build();
            view.buffer()
                .set_text(&serde_json::to_string_pretty(value).unwrap_or_default());
            body.append(&view);
        }
        dialog.present(Some(&self.window));
    }
    pub(super) fn advanced_state(self: &Rc<Self>) {
        let selection = self.selection.borrow().clone();
        let original = self.draft.borrow().clone();
        let value = original.clone();
        let weak = Rc::downgrade(self);
        self.json_dialog("Advanced state", value, move |document| {
            if let Some(ui) = weak.upgrade() {
                let sel = selection.clone();
                let original = original.clone();
                ui.submit(Work {
                    key: None,
                    title: "State saved".into(),
                    rebuild: true,
                    execute: Box::new(move |engine| {
                        sel.edit(engine, |state| {
                            anyhow::ensure!(
                                *state == original,
                                "State changed while editing; reopen the editor before saving"
                            );
                            *state = document;
                            Ok(())
                        })
                    }),
                });
            }
        });
    }
    pub(super) fn json_dialog(&self, title: &str, value: Value, save: impl Fn(Value) + 'static) {
        let (dialog, body) = self.dialog(title);
        let view = gtk::TextView::builder()
            .monospace(true)
            .wrap_mode(gtk::WrapMode::WordChar)
            .height_request(400)
            .build();
        view.buffer()
            .set_text(&serde_json::to_string_pretty(&value).unwrap_or_default());
        body.append(&view);
        let error = gtk::Label::new(None);
        error.add_css_class("error");
        error.set_wrap(true);
        body.append(&error);
        let weak = dialog.downgrade();
        body.append(&button("Save", "document-save-symbolic", move || {
            let buffer = view.buffer();
            let text = buffer.text(&buffer.start_iter(), &buffer.end_iter(), true);
            match serde_json::from_str::<Value>(&text) {
                Ok(value) if value.is_object() => {
                    save(value);
                    if let Some(dialog) = weak.upgrade() {
                        dialog.close();
                    }
                }
                _ => error.set_text("Enter a valid JSON object."),
            }
        }));
        dialog.present(Some(&self.window));
    }
    pub(super) fn pages(self: &Rc<Self>) {
        let (dialog, body) = self.dialog("Pages");
        let group = adw::PreferencesGroup::new();
        group.set_title("Manage pages");
        body.append(&group);
        let names = self
            .shared
            .lock()
            .unwrap()
            .docs
            .pages
            .keys()
            .cloned()
            .collect::<Vec<_>>();
        let refs = names.iter().map(String::as_str).collect::<Vec<_>>();
        let chosen = Rc::new(RefCell::new(self.selection.borrow().page.clone()));
        let selected = chosen.clone();
        group.add(&choice(
            "Page",
            &refs,
            &chosen.borrow().clone(),
            move |page| *selected.borrow_mut() = page,
        ));
        let name = adw::EntryRow::builder().title("New name").build();
        group.add(&name);
        let actions = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        body.append(&actions);
        for (label, method) in [
            ("Create", "create-page"),
            ("Duplicate", "duplicate-page"),
            ("Rename", "rename-page"),
        ] {
            let weak = Rc::downgrade(self);
            let chosen = chosen.clone();
            let entry = name.clone();
            let weak_dialog = dialog.downgrade();
            actions.append(&button(label, "document-new-symbolic", move || {
                if let Some(ui) = weak.upgrade() {
                    let params = if method == "create-page" {
                        json!({"page":entry.text().as_str()})
                    } else {
                        json!({"page":*chosen.borrow(),"name":entry.text().as_str()})
                    };
                    ui.command(method, params, "Page saved", true);
                    if let Some(dialog) = weak_dialog.upgrade() {
                        dialog.close();
                    }
                }
            }));
        }
        let weak = Rc::downgrade(self);
        let selected = chosen.clone();
        let weak_dialog = dialog.downgrade();
        actions.append(&button("Delete", "user-trash-symbolic", move || {
            if let Some(ui) = weak.upgrade() {
                let page = selected.borrow().clone();
                let confirm = adw::AlertDialog::new(
                    Some("Delete page?"),
                    Some(&format!("Delete “{page}”? This cannot be undone.")),
                );
                confirm.add_responses(&[("cancel", "Cancel"), ("delete", "Delete")]);
                confirm.set_response_appearance("delete", adw::ResponseAppearance::Destructive);
                confirm.set_default_response(Some("cancel"));
                confirm.set_close_response("cancel");
                let weak = Rc::downgrade(&ui);
                confirm.connect_response(Some("delete"), move |_, _| {
                    if let Some(ui) = weak.upgrade() {
                        ui.command("delete-page", json!({"page":page}), "Page deleted", true);
                    }
                });
                confirm.present(Some(&ui.window));
                if let Some(dialog) = weak_dialog.upgrade() {
                    dialog.close();
                }
            }
        }));
        let bundles = adw::PreferencesGroup::new();
        bundles.set_title("Page bundles");
        body.append(&bundles);
        let weak = Rc::downgrade(self);
        let selected = chosen.clone();
        let export = adw::ActionRow::builder()
            .title("Export page with assets and native plugins")
            .activatable(true)
            .build();
        export.connect_activated(move |_| {
            if let Some(ui) = weak.upgrade() {
                let page = selected.borrow().clone();
                let weak = Rc::downgrade(&ui);
                ui.choose_file("Export page", true, move |path| {
                    if let Some(ui) = weak.upgrade() {
                        ui.command(
                            "export-page",
                            json!({"page":page,"path":path}),
                            "Page exported",
                            false,
                        );
                    }
                });
            }
        });
        bundles.add(&export);
        let weak = Rc::downgrade(self);
        let import = adw::ActionRow::builder()
            .title("Import page bundle")
            .subtitle(".deckardpage or .zip")
            .activatable(true)
            .build();
        import.connect_activated(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.install_local("page");
            }
        });
        bundles.add(&import);
        let weak = Rc::downgrade(self);
        let selected = chosen.clone();
        let advanced = adw::ActionRow::builder()
            .title("Advanced page document")
            .activatable(true)
            .build();
        advanced.connect_activated(move |_| {
            if let Some(ui) = weak.upgrade() {
                let page = selected.borrow().clone();
                let original = ui
                    .shared
                    .lock()
                    .unwrap()
                    .docs
                    .pages
                    .get(&page)
                    .cloned()
                    .unwrap_or_else(|| json!({}));
                let weak = Rc::downgrade(&ui);
                let saved = original.clone();
                ui.json_dialog("Advanced page", original, move |document| {
                    if let Some(ui) = weak.upgrade() {
                        let page = page.clone();
                        let saved = saved.clone();
                        ui.submit(Work {
                            key: None,
                            title: "Page saved".into(),
                            rebuild: true,
                            execute: Box::new(move |engine| {
                                anyhow::ensure!(
                                    engine.docs.pages.get(&page) == Some(&saved),
                                    "Page changed while editing; reopen it before saving"
                                );
                                engine.docs.put(&page, document)?;
                                Ok(Value::Null)
                            }),
                        });
                    }
                });
            }
        });
        bundles.add(&advanced);
        dialog.present(Some(&self.window));
    }
    pub(super) fn choose_file(
        &self,
        title: &str,
        save: bool,
        selected: impl FnOnce(PathBuf) + 'static,
    ) {
        let dialog = gtk::FileDialog::builder().title(title).modal(true).build();
        let callback = move |result: std::result::Result<gio::File, glib::Error>| {
            if let Ok(file) = result
                && let Some(path) = file.path()
            {
                selected(path);
            }
        };
        if save {
            dialog.set_initial_name(Some("Page.deckardpage"));
            dialog.save(Some(&self.window), None::<&gio::Cancellable>, callback);
        } else {
            dialog.open(Some(&self.window), None::<&gio::Cancellable>, callback);
        }
    }
    pub(super) fn page_property(self: &Rc<Self>, page: String, path: Vec<String>, value: Value) {
        self.submit(Work {
            key: Some(format!("page/{page}/{path:?}")),
            title: String::new(),
            rebuild: false,
            execute: Box::new(move |engine| {
                engine.docs.edit(&page, |document| {
                    set_nested(
                        document,
                        &path.iter().map(String::as_str).collect::<Vec<_>>(),
                        value,
                    );
                    Ok(())
                })?;
                Ok(Value::Null)
            }),
        });
    }
    pub(super) fn page_settings(self: &Rc<Self>) {
        let page = self.selection.borrow().page.clone();
        let document = self
            .shared
            .lock()
            .unwrap()
            .docs
            .pages
            .get(&page)
            .cloned()
            .unwrap_or_else(|| json!({}));
        let (dialog, body) = self.dialog(&format!("Page Settings · {page}"));
        let background = adw::PreferencesGroup::new();
        background.set_title("Wallpaper");
        body.append(&background);
        for (title, key, default) in [
            ("Show wallpaper", "show", true),
            ("Override deck wallpaper", "overwrite", true),
            ("Extend to touchscreen", "extend-to-touchscreen", false),
        ] {
            let weak = Rc::downgrade(self);
            let page = page.clone();
            background.add(&toggle(
                title,
                document["background"][key].as_bool().unwrap_or(default),
                move |v| {
                    if let Some(ui) = weak.upgrade() {
                        ui.page_property(
                            page.clone(),
                            vec!["background".into(), key.into()],
                            json!(v),
                        );
                    }
                },
            ));
        }
        let weak = Rc::downgrade(self);
        let target = page.clone();
        background.add(&file_row(
            &self.window,
            "Image, GIF or video",
            document["background"]["media-path"].as_str().unwrap_or(""),
            move |v| {
                if let Some(ui) = weak.upgrade() {
                    ui.page_property(
                        target.clone(),
                        vec!["background".into(), "media-path".into()],
                        json!(v),
                    );
                }
            },
        ));
        let weak = Rc::downgrade(self);
        let target = page.clone();
        background.add(&number(
            "Slideshow interval (seconds)",
            document["background"]["slideshow-interval"]
                .as_f64()
                .unwrap_or(10.),
            1.,
            86400.,
            1.,
            move |v| {
                if let Some(ui) = weak.upgrade() {
                    ui.page_property(
                        target.clone(),
                        vec!["background".into(), "slideshow-interval".into()],
                        json!(v as u64),
                    );
                }
            },
        ));
        let weak = Rc::downgrade(self);
        let target = page.clone();
        background.add(&choice(
            "Slideshow order",
            &["ordered", "shuffle"],
            document["background"]["slideshow-order"]
                .as_str()
                .unwrap_or("ordered"),
            move |v| {
                if let Some(ui) = weak.upgrade() {
                    ui.page_property(
                        target.clone(),
                        vec!["background".into(), "slideshow-order".into()],
                        json!(v),
                    );
                }
            },
        ));
        let list = gtk::Box::new(gtk::Orientation::Vertical, 4);
        body.append(&list);
        list.set_widget_name("slideshow-images");
        self.populate_slideshow(&list, &page);
        let weak = Rc::downgrade(self);
        let target = page.clone();
        let weak_list = list.downgrade();
        body.append(&button(
            "Add slideshow image",
            "list-add-symbolic",
            move || {
                if let Some(ui) = weak.upgrade() {
                    let page = target.clone();
                    let weak = Rc::downgrade(&ui);
                    let weak_list = weak_list.clone();
                    ui.choose_file("Slideshow image", false, move |path| {
                        if let (Some(ui), Some(list)) = (weak.upgrade(), weak_list.upgrade()) {
                            ui.slideshow_edit(&list, page, Some(path), None);
                        }
                    });
                }
            },
        ));
        dialog.present(Some(&self.window));
    }
    fn populate_slideshow(self: &Rc<Self>, list: &gtk::Box, page: &str) {
        clear(list);
        list.set_sensitive(true);
        let document = self
            .shared
            .lock()
            .unwrap()
            .docs
            .pages
            .get(page)
            .cloned()
            .unwrap_or_default();
        if let Some(paths) = document["background"]["media-paths"].as_array() {
            for (index, path) in paths.iter().enumerate() {
                let row = adw::ActionRow::builder()
                    .title(
                        path.as_str()
                            .or_else(|| path["path"].as_str())
                            .unwrap_or(""),
                    )
                    .build();
                let weak = Rc::downgrade(self);
                let weak_list = list.downgrade();
                let page = page.to_owned();
                let remove = button("", "user-trash-symbolic", move || {
                    if let (Some(ui), Some(list)) = (weak.upgrade(), weak_list.upgrade()) {
                        ui.slideshow_edit(&list, page.clone(), None, Some(index));
                    }
                });
                remove.set_widget_name(&format!("remove-slide-{index}"));
                row.add_suffix(&remove);
                list.append(&row);
            }
        }
    }
    fn slideshow_edit(
        self: &Rc<Self>,
        list: &gtk::Box,
        page: String,
        add: Option<PathBuf>,
        remove: Option<usize>,
    ) {
        list.set_sensitive(false);
        let weak_list = list.downgrade();
        let target = page.clone();
        self.submit_then(
            Work {
                key: None,
                title: String::new(),
                rebuild: false,
                execute: Box::new(move |engine| {
                    engine.docs.edit(&page, |document| {
                        let mut paths = document["background"]["media-paths"]
                            .as_array()
                            .cloned()
                            .unwrap_or_default();
                        if let Some(index) = remove {
                            anyhow::ensure!(index < paths.len(), "Slideshow changed");
                            paths.remove(index);
                        }
                        if let Some(path) = add {
                            paths.push(json!(path));
                        }
                        set_nested(document, &["background", "media-paths"], json!(paths));
                        Ok(())
                    })?;
                    Ok(Value::Null)
                }),
            },
            move |ui, _| {
                if let Some(list) = weak_list.upgrade() {
                    ui.populate_slideshow(&list, &target);
                }
            },
        );
    }
    pub(super) fn assets(self: &Rc<Self>) {
        let (dialog, body) = self.dialog("Assets");
        let root = self.shared.lock().unwrap().docs.root.clone();
        let select = self.selection.borrow().clone();
        let weak = Rc::downgrade(self);
        let weak_dialog = dialog.downgrade();
        body.append(&button(
            "Choose image, GIF or video…",
            "folder-open-symbolic",
            move || {
                if let Some(ui) = weak.upgrade() {
                    let sel = select.clone();
                    let weak = Rc::downgrade(&ui);
                    ui.choose_file("Choose media", false, move |path| {
                        if let Some(ui) = weak.upgrade() {
                            ui.edit(sel, vec!["media".into(), "path".into()], json!(path));
                        }
                    });
                    if let Some(dialog) = weak_dialog.upgrade() {
                        dialog.close();
                    }
                }
            },
        ));
        let flow = gtk::FlowBox::builder()
            .max_children_per_line(5)
            .min_children_per_line(2)
            .selection_mode(gtk::SelectionMode::None)
            .build();
        body.append(&flow);
        let mut files = Vec::new();
        for directory in [
            root.join("icons-native"),
            root.join("Assets/AssetManager/Assets"),
            root.join("assets"),
        ] {
            asset_files(&directory, &mut files, 3);
        }
        files.sort();
        files.dedup();
        let search = gtk::SearchEntry::builder()
            .placeholder_text("Search assets")
            .build();
        body.insert_child_after(&search, None::<&gtk::Widget>);
        let navigation = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        let previous = gtk::Button::with_label("Previous");
        let next = gtk::Button::with_label("Next");
        let status = gtk::Label::new(None);
        navigation.append(&previous);
        navigation.append(&status);
        navigation.append(&next);
        body.append(&navigation);
        let page = Rc::new(Cell::new(0usize));
        let weak_flow = flow.downgrade();
        let weak_search = search.downgrade();
        let weak_previous = previous.downgrade();
        let weak_next = next.downgrade();
        let weak_status = status.downgrade();
        let weak_ui = Rc::downgrade(self);
        let close = dialog.downgrade();
        let selection = self.selection.borrow().clone();
        let current = page.clone();
        let refresh: Rc<dyn Fn()> = Rc::new(move || {
            let (Some(flow), Some(search), Some(previous), Some(next), Some(status)) = (
                weak_flow.upgrade(),
                weak_search.upgrade(),
                weak_previous.upgrade(),
                weak_next.upgrade(),
                weak_status.upgrade(),
            ) else {
                return;
            };
            flow.remove_all();
            let needle = search.text().to_lowercase();
            let filtered = files
                .iter()
                .filter(|p| p.to_string_lossy().to_lowercase().contains(&needle))
                .collect::<Vec<_>>();
            let count = filtered.len();
            let pages = count.div_ceil(40).max(1);
            current.set(current.get().min(pages - 1));
            previous.set_sensitive(current.get() > 0);
            next.set_sensitive(current.get() + 1 < pages);
            status.set_text(&format!("{} / {pages} · {count} assets", current.get() + 1));
            for path in filtered.into_iter().skip(current.get() * 40).take(40) {
                let picture = PreviewImage::new(96, 96);
                let load_path = path.clone();
                picture.connect_map(move |picture| {
                    if let Ok(pixbuf) =
                        gtk::gdk_pixbuf::Pixbuf::from_file_at_scale(&load_path, 96, 96, true)
                    {
                        picture.set_paintable(Some(&gdk::Texture::for_pixbuf(&pixbuf)));
                    }
                });
                picture.connect_unmap(|picture| picture.set_paintable(None::<&gdk::Paintable>));
                let box_ = gtk::Box::new(gtk::Orientation::Vertical, 4);
                box_.append(&picture);
                let label = gtk::Label::new(path.file_name().and_then(|n| n.to_str()));
                label.set_ellipsize(gtk::pango::EllipsizeMode::End);
                label.set_max_width_chars(14);
                box_.append(&label);
                let button = gtk::Button::builder()
                    .child(&box_)
                    .tooltip_text(path.to_string_lossy())
                    .build();
                button.add_css_class("deckard-asset");
                let weak = weak_ui.clone();
                let selection = selection.clone();
                let close = close.clone();
                let path = path.clone();
                button.connect_clicked(move |_| {
                    if let Some(ui) = weak.upgrade() {
                        ui.edit(
                            selection.clone(),
                            vec!["media".into(), "path".into()],
                            json!(path),
                        );
                    }
                    if let Some(dialog) = close.upgrade() {
                        dialog.close();
                    }
                });
                flow.insert(&button, -1);
            }
        });
        let draw = refresh.clone();
        let current = page.clone();
        search.connect_search_changed(move |_| {
            current.set(0);
            draw();
        });
        let draw = refresh.clone();
        let current = page.clone();
        previous.connect_clicked(move |_| {
            current.set(current.get().saturating_sub(1));
            draw();
        });
        let draw = refresh.clone();
        let current = page.clone();
        next.connect_clicked(move |_| {
            current.set(current.get() + 1);
            draw();
        });
        refresh();
        let group = adw::PreferencesGroup::new();
        group.set_title("Create an icon pack");
        body.append(&group);
        let name = adw::EntryRow::builder().title("Pack name").build();
        group.add(&name);
        let weak = Rc::downgrade(self);
        body.append(&button(
            "Choose folder and create pack",
            "folder-new-symbolic",
            move || {
                if let Some(ui) = weak.upgrade() {
                    let dialog = gtk::FileDialog::builder().title("Icon folder").build();
                    let weak = Rc::downgrade(&ui);
                    let name = name.text().to_string();
                    let root = root.clone();
                    dialog.select_folder(
                        Some(&ui.window),
                        None::<&gio::Cancellable>,
                        move |result| {
                            if let (Some(ui), Ok(file)) = (weak.upgrade(), result)
                                && let Some(path) = file.path()
                            {
                                ui.job("Icon pack created", false, move || {
                                    crate::editor_model::create_icon_pack(&path, &root, &name)?;
                                    Ok(Value::Null)
                                });
                            }
                        },
                    );
                }
            },
        ));
        dialog.present(Some(&self.window));
    }
}
fn asset_files(directory: &std::path::Path, files: &mut Vec<PathBuf>, depth: usize) {
    if depth == 0 || files.len() >= 2000 {
        return;
    }
    if let Ok(entries) = std::fs::read_dir(directory) {
        for entry in entries.flatten() {
            if entry.file_type().is_ok_and(|t| t.is_dir()) {
                asset_files(&entry.path(), files, depth - 1);
            } else if entry
                .path()
                .extension()
                .and_then(|e| e.to_str())
                .is_some_and(|ext| ["png", "jpg", "jpeg", "webp", "svg", "gif"].contains(&ext))
            {
                files.push(entry.path());
            }
            if files.len() >= 2000 {
                break;
            }
        }
    }
}
