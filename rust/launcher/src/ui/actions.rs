use super::*;
use controls::*;

fn action_title(name: &str) -> (&str, &str) {
    name.split_once(" · ")
        .map(|(category, title)| {
            (
                title,
                if category == "VolumeMixer" {
                    "Volume Mixer"
                } else {
                    category
                },
            )
        })
        .unwrap_or((name, "Native plugins"))
}

// Permissions refer to action positions. Keep them attached to the same action
// when upstream's move buttons or drag/drop change that order.
fn remap_controls(state: &mut Value, order: &[usize]) {
    let remap = |value: &mut Value| {
        if let Some(old) = value.as_u64() {
            *value = order
                .iter()
                .position(|i| *i == old as usize)
                .map_or(Value::Null, |i| json!(i));
        }
    };
    for key in ["image-control-action", "background-control-action"] {
        if let Some(value) = state.get_mut(key) {
            remap(value);
        }
    }
    if let Some(values) = state["label-control-actions"].as_array_mut() {
        for value in values {
            remap(value);
        }
    }
}
impl Ui {
    pub(super) fn action_rows(self: &Rc<Self>) {
        let (group, expander) = self.editor_group("Actions", "Actions for this key");
        let state = self.draft.borrow().clone();
        let actions = state["actions"].as_array().cloned().unwrap_or_default();
        for (index, action) in actions.iter().enumerate() {
            let id = action["id"].as_str().unwrap_or("");
            let definition = self
                .definitions
                .borrow()
                .iter()
                .find(|d| d.0 == id)
                .cloned();
            let row = adw::ActionRow::builder().activatable(true).build();
            row.add_css_class("no-padding");
            let main = gtk::Box::new(gtk::Orientation::Horizontal, 0);
            margins(&main, 15);
            main.set_hexpand(true);
            row.set_child(Some(&main));
            let selection = self.selection.borrow().clone();
            let weak = Rc::downgrade(self);
            let owner = selection.clone();
            row.connect_activated(move |_| {
                if let Some(ui) = weak.upgrade()
                    && *ui.selection.borrow() == owner
                {
                    ui.configure_action(index);
                }
            });
            let allow = gtk::Box::builder()
                .orientation(gtk::Orientation::Horizontal)
                .margin_end(15)
                .build();
            allow.add_css_class("linked");
            main.append(&allow);
            for (key, icon, tooltip) in [
                (
                    "image-control-action",
                    "image-x-generic-symbolic",
                    "Allow action to control the media",
                ),
                (
                    "background-control-action",
                    "color-select-symbolic",
                    "Allow action to control the background color",
                ),
            ] {
                let active = state
                    .get(key)
                    .map_or(index == 0, |v| v.as_u64() == Some(index as u64));
                let toggle = gtk::ToggleButton::builder()
                    .icon_name(icon)
                    .active(active)
                    .tooltip_text(tooltip)
                    .build();
                toggle.add_css_class("blue-toggle-button");
                let weak = Rc::downgrade(self);
                let owner = selection.clone();
                toggle.connect_toggled(move |button| {
                    if let Some(ui) = weak.upgrade() {
                        ui.edit(
                            owner.clone(),
                            vec![key.into()],
                            if button.is_active() {
                                json!(index)
                            } else {
                                Value::Null
                            },
                        );
                        ui.force_reload.set(true);
                    }
                });
                allow.append(&toggle);
            }
            let label_toggle = gtk::Button::new();
            label_toggle.add_css_class("blue-toggle-button");
            let label_box = gtk::Box::new(gtk::Orientation::Horizontal, 6);
            let indicators = gtk::Box::new(gtk::Orientation::Vertical, 0);
            label_box.append(&gtk::Image::from_icon_name("insert-text-symbolic"));
            label_box.append(&indicators);
            label_toggle.set_child(Some(&label_box));
            let popover = gtk::Popover::new();
            popover.set_parent(&label_toggle);
            let checks = gtk::Box::new(gtk::Orientation::Vertical, 0);
            popover.set_child(Some(&checks));
            for (i, title) in ["Top", "Center", "Bottom"].into_iter().enumerate() {
                let active = state["label-control-actions"]
                    .get(i)
                    .map_or(index == 0, |v| v.as_u64() == Some(index as u64));
                let indicator = gtk::Box::new(gtk::Orientation::Vertical, 0);
                indicator.add_css_class(if active {
                    "action-row-label-toggle-active"
                } else {
                    "action-row-label-toggle-inactive"
                });
                indicators.append(&indicator);
                let check = gtk::CheckButton::builder()
                    .label(title)
                    .active(active)
                    .build();
                let weak = Rc::downgrade(self);
                let owner = selection.clone();
                check.connect_toggled(move |check| {
                    indicator.set_css_classes(&[if check.is_active() {
                        "action-row-label-toggle-active"
                    } else {
                        "action-row-label-toggle-inactive"
                    }]);
                    if let Some(ui) = weak.upgrade() {
                        let selection = owner.clone();
                        let active = check.is_active();
                        ui.submit(Work {
                            key: None,
                            title: String::new(),
                            rebuild: true,
                            execute: Box::new(move |engine| {
                                selection.edit(engine, |draft| {
                                    let mut values = draft["label-control-actions"]
                                        .as_array()
                                        .cloned()
                                        .unwrap_or_else(|| vec![json!(0); 3]);
                                    values.resize(3, Value::Null);
                                    values[i] = if active { json!(index) } else { Value::Null };
                                    draft["label-control-actions"] = json!(values);
                                    Ok(())
                                })
                            }),
                        });
                        ui.force_reload.set(true);
                    }
                });
                checks.append(&check);
            }
            // Attach only while open, and always detach before the button is
            // disposed during a state/page rebuild.
            popover.unparent();
            let popup = popover.clone();
            label_toggle.connect_clicked(move |button| {
                if popup.parent().is_none() {
                    popup.set_parent(button);
                }
                popup.popup();
            });
            let popup = popover.downgrade();
            label_toggle.connect_unrealize(move |_| {
                if let Some(popup) = popup.upgrade() {
                    popup.unparent();
                }
            });
            popover.connect_closed(|popover| popover.unparent());
            allow.append(&label_toggle);
            let left = gtk::Box::builder()
                .orientation(gtk::Orientation::Vertical)
                .hexpand(true)
                .valign(gtk::Align::Center)
                .build();
            main.append(&left);
            let name = definition.as_ref().map(|d| d.1.as_str()).unwrap_or(id);
            let (title, category) = action_title(name);
            let label = gtk::Label::builder()
                .use_markup(true)
                .xalign(0.)
                .margin_end(5)
                .wrap(true)
                .wrap_mode(gtk::pango::WrapMode::WordChar)
                .build();
            label.set_markup(&format!(
                "<b>{}</b> <span color=\"#979797\">({})</span>",
                glib::markup_escape_text(title),
                glib::markup_escape_text(category)
            ));
            left.append(&label);
            if let Some(comment) = action["comment"].as_str().filter(|s| !s.is_empty()) {
                left.append(
                    &gtk::Label::builder()
                        .label(comment)
                        .xalign(0.)
                        .sensitive(false)
                        .ellipsize(gtk::pango::EllipsizeMode::End)
                        .margin_end(60)
                        .build(),
                );
            }
            let buttons = gtk::Box::builder()
                .orientation(gtk::Orientation::Horizontal)
                .halign(gtk::Align::End)
                .valign(gtk::Align::Center)
                .build();
            buttons.add_css_class("linked");
            main.append(&buttons);
            for (icon, delta) in [("go-up-symbolic", -1isize), ("go-down-symbolic", 1isize)] {
                let weak = Rc::downgrade(self);
                let owner = selection.clone();
                let button = button("", icon, move || {
                    if let Some(ui) = weak.upgrade()
                        && *ui.selection.borrow() == owner
                    {
                        ui.reorder_action(index, index.saturating_add_signed(delta));
                    }
                });
                button.set_sensitive(if delta < 0 {
                    index > 0
                } else {
                    index + 1 < actions.len()
                });
                buttons.append(&button);
            }
            let source = gtk::DragSource::new();
            source.set_actions(gdk::DragAction::MOVE);
            source.connect_prepare(move |_, _, _| {
                Some(gdk::ContentProvider::for_value(&(index as u32).to_value()))
            });
            let weak_row = row.downgrade();
            source.connect_drag_begin(move |_, _| {
                if let Some(row) = weak_row.upgrade() {
                    row.add_css_class("action-row-dragged");
                }
            });
            let weak_row = row.downgrade();
            source.connect_drag_end(move |_, _, _| {
                if let Some(row) = weak_row.upgrade() {
                    row.remove_css_class("action-row-dragged");
                }
            });
            row.add_controller(source);
            let drop = gtk::DropTarget::new(u32::static_type(), gdk::DragAction::MOVE);
            let weak_row = row.downgrade();
            drop.connect_motion(move |_, _, y| {
                if let Some(row) = weak_row.upgrade() {
                    row.remove_css_class("action-row-drop-above");
                    row.remove_css_class("action-row-drop-below");
                    row.add_css_class(if y > row.height() as f64 / 2. {
                        "action-row-drop-below"
                    } else {
                        "action-row-drop-above"
                    });
                }
                gdk::DragAction::MOVE
            });
            let weak_row = row.downgrade();
            drop.connect_leave(move |_| {
                if let Some(row) = weak_row.upgrade() {
                    row.remove_css_class("action-row-drop-above");
                    row.remove_css_class("action-row-drop-below");
                }
            });
            let weak = Rc::downgrade(self);
            let weak_row = row.downgrade();
            let count = actions.len();
            drop.connect_drop(move |_, value, _, y| {
                if let (Some(ui), Some(row), Ok(source)) =
                    (weak.upgrade(), weak_row.upgrade(), value.get::<u32>())
                    && *ui.selection.borrow() == selection
                {
                    row.remove_css_class("action-row-drop-above");
                    row.remove_css_class("action-row-drop-below");
                    let source = source as usize;
                    let boundary = index + usize::from(y > row.height() as f64 / 2.);
                    let target = boundary.saturating_sub(usize::from(source < boundary));
                    if target < count && target != source {
                        ui.reorder_action(source, target);
                    }
                    true
                } else {
                    false
                }
            });
            row.add_controller(drop);
            expander.add_row(&row);
        }
        let weak = Rc::downgrade(self);
        let add = adw::ButtonRow::builder().title("Add Action").build();
        add.add_css_class("suggested-action");
        add.add_css_class("add-action-button");
        add.set_widget_name("Add Action");
        add.connect_activated(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.choose_action(None);
            }
        });
        expander.add_row(&add);
        group.set_margin_bottom(50);
        if matches!(
            self.selection.borrow().family.as_str(),
            "touchscreens" | "infobar"
        ) {
            let outer = adw::PreferencesGroup::builder().title("Actions").build();
            let clamp = adw::Clamp::new();
            group.set_margin_bottom(50);
            clamp.set_child(Some(&group));
            outer.add(&clamp);
            self.controls.append(&outer);
        } else {
            self.append_editor_panel(&group, 25);
        }
    }
    fn reorder_action(self: &Rc<Self>, source: usize, target: usize) {
        let sel = self.selection.borrow().clone();
        let expected = {
            let mut state = self.draft.borrow_mut();
            let Some(actions) = state["actions"].as_array_mut() else {
                return;
            };
            if source >= actions.len() || target >= actions.len() || source == target {
                return;
            }
            let expected = actions.iter().map(|a| a["id"].clone()).collect::<Vec<_>>();
            let action = actions.remove(source);
            actions.insert(target, action);
            let mut order = (0..actions.len()).collect::<Vec<_>>();
            let index = order.remove(source);
            order.insert(target, index);
            remap_controls(&mut state, &order);
            expected
        };
        let document = sel.document(&self.shared.lock().unwrap());
        self.rebuild_editor(&document);
        self.submit(Work {
            key: None,
            title: String::new(),
            rebuild: true,
            execute: Box::new(move |engine| {
                sel.edit(engine, |draft| {
                    let actions = draft["actions"]
                        .as_array_mut()
                        .context("Action list changed")?;
                    anyhow::ensure!(
                        actions.iter().map(|a| a["id"].clone()).collect::<Vec<_>>() == expected,
                        "Action order changed; reload before moving"
                    );
                    anyhow::ensure!(
                        source < actions.len() && target < actions.len(),
                        "Action list changed"
                    );
                    let value = actions.remove(source);
                    actions.insert(target, value);
                    let mut order = (0..actions.len()).collect::<Vec<_>>();
                    let old = order.remove(source);
                    order.insert(target, old);
                    remap_controls(draft, &order);
                    Ok(())
                })
            }),
        });
    }
    fn remove_action(self: &Rc<Self>, index: usize) {
        let sel = self.selection.borrow().clone();
        let expected = self.draft.borrow()["actions"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        if index >= expected.len() {
            return;
        }
        let mut order = (0..expected.len()).collect::<Vec<_>>();
        order.remove(index);
        self.draft.borrow_mut()["actions"]
            .as_array_mut()
            .unwrap()
            .remove(index);
        remap_controls(&mut self.draft.borrow_mut(), &order);
        self.submit(Work {
            key: None,
            title: String::new(),
            rebuild: true,
            execute: Box::new(move |engine| {
                sel.edit(engine, |draft| {
                    let actions = draft["actions"]
                        .as_array_mut()
                        .context("Action list changed")?;
                    anyhow::ensure!(index < actions.len(), "Action list changed");
                    anyhow::ensure!(*actions == expected, "Action list changed; select it again");
                    let mut order = (0..actions.len()).collect::<Vec<_>>();
                    actions.remove(index);
                    order.remove(index);
                    remap_controls(draft, &order);
                    Ok(())
                })
            }),
        });
    }
    fn sidebar_page(self: &Rc<Self>, title: &str) -> gtk::Box {
        if let Some(child) = self.sidebar.child_by_name("action-page") {
            self.sidebar.remove(&child);
        }
        let scroll = gtk::ScrolledWindow::builder()
            .hexpand(true)
            .vexpand(true)
            .margin_end(if title == "Configure Action" { 4 } else { 0 })
            .build();
        scroll.set_widget_name(title);
        let clamp = adw::Clamp::new();
        scroll.set_child(Some(&clamp));
        let body = gtk::Box::builder()
            .orientation(gtk::Orientation::Vertical)
            .hexpand(true)
            .vexpand(true)
            .margin_top(4)
            .build();
        clamp.set_child(Some(&body));
        let nav = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        let weak = Rc::downgrade(self);
        let back = gtk::Button::new();
        let content = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        content.append(&gtk::Image::from_icon_name("go-previous-symbolic"));
        content.append(&gtk::Label::new(Some("Back")));
        back.set_child(Some(&content));
        back.connect_clicked(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.sidebar.set_visible_child_name("editor");
            }
        });
        nav.append(&back);
        body.append(&nav);
        let label = gtk::Label::builder()
            .label(title)
            .xalign(0.)
            .margin_top(30)
            .margin_start(if title == "Configure Action" { 20 } else { 0 })
            .build();
        label.add_css_class("page-header");
        body.append(&label);
        self.sidebar.add_named(&scroll, Some("action-page"));
        self.sidebar.set_visible_child_name("action-page");
        body
    }
    pub(super) fn choose_action(self: &Rc<Self>, replace: Option<usize>) {
        let body = self.sidebar_page("Choose An Action");
        let search = gtk::SearchEntry::builder()
            .placeholder_text("Search for actions and plugins")
            .hexpand(true)
            .margin_top(10)
            .build();
        body.append(&search);
        let group = adw::PreferencesGroup::builder().margin_top(40).build();
        body.append(&group);
        let mut categories = std::collections::BTreeMap::<
            String,
            Vec<(String, String, Vec<deckard_core::plugin::Field>)>,
        >::new();
        for (id, name, fields) in self.definitions.borrow().iter() {
            // Built-in low-level aliases remain valid in saved pages; the chooser
            // offers their named common-plugin replacements without duplicates.
            if id.starts_with("native::") && !id.contains("Plugin-") && !id.contains("VolumeMixer-")
            {
                continue;
            }
            let (title, category) = action_title(name);
            categories.entry(category.to_owned()).or_default().push((
                id.clone(),
                title.to_owned(),
                fields.clone(),
            ));
        }
        let mut filter_rows = Vec::new();
        let selection = self.selection.borrow().clone();
        for (category, mut definitions) in categories {
            definitions.sort_by(|a, b| search::natural_cmp(&a.1, &b.1));
            let plugin_id = match category.as_str() {
                "OS" => "com_core447_OSPlugin",
                "Deck" => "com_core447_DeckPlugin",
                "Media" => "com_core447_MediaPlugin",
                "OBS" => "com_core447_OBSPlugin",
                "Volume Mixer" => "com_core447_VolumeMixer",
                _ => "native",
            };
            let expander = adw::ExpanderRow::builder()
                .title(&category)
                .subtitle(plugin_id)
                .build();
            expander.add_prefix(&gtk::Image::from_icon_name("view-paged"));
            group.add(&expander);
            let mut rows = Vec::new();
            for (id, name, fields) in definitions {
                let row = adw::ActionRow::new();
                row.add_css_class("action-chooser-item");
                let choose = gtk::Button::builder()
                    .hexpand(true)
                    .vexpand(true)
                    .overflow(gtk::Overflow::Hidden)
                    .build();
                choose.add_css_class("no-margin");
                choose.add_css_class("invisible");
                row.set_child(Some(&choose));
                let content = gtk::Box::builder()
                    .orientation(gtk::Orientation::Horizontal)
                    .hexpand(true)
                    .vexpand(true)
                    .margin_top(10)
                    .margin_bottom(10)
                    .build();
                choose.set_child(Some(&content));
                content.append(&gtk::Image::from_icon_name(
                    if id == "native::DeckPlugin-GoToPreviousPage" {
                        "mail-reply-sender-symbolic"
                    } else {
                        "insert-image-symbolic"
                    },
                ));
                let label = gtk::Label::builder().label(&name).margin_start(10).build();
                label.add_css_class("bold");
                label.add_css_class("large-text");
                content.append(&label);
                choose.set_widget_name(&id);
                let weak = Rc::downgrade(self);
                let owner = selection.clone();
                choose.connect_clicked(move |_| { if let Some(ui) = weak.upgrade() && *ui.selection.borrow() == owner {
                    let settings = fields.iter().map(|field| (field.key.clone(), field.default.clone())).collect::<serde_json::Map<_,_>>(); let action = json!({"id":id,"event":if id.contains("Plugin-") || id.contains("VolumeMixer-") { "auto" } else { "press" },"settings":settings});
                    let selection = owner.clone(); let selected = owner.clone();
                    ui.sidebar.set_visible_child_name("editor");
                    ui.submit_then(Work { key: None, title: String::new(), rebuild: true, execute: Box::new(move |engine| {
                        let mut added = 0;
                        selection.edit(engine, |state| {
                            let mut actions = state["actions"].as_array().cloned().unwrap_or_default();
                            added = replace.unwrap_or(actions.len());
                            if replace.is_some() { anyhow::ensure!(added < actions.len(), "Action list changed"); actions[added] = action; }
                            else { actions.push(action); }
                            state["actions"] = json!(actions); Ok(())
                        })?; Ok(json!(added))
                    })}, move |ui, result| {
                        if *ui.selection.borrow() == selected && let Ok(index) = result {
                            ui.force_reload.set(true); ui.refresh();
                            if ui.shared.lock().unwrap().docs.settings["ui"]["auto-open-action-config"].as_bool().unwrap_or(true) {
                                ui.configure_action(index.as_u64().unwrap_or(0) as usize);
                            }
                        }
                    });
                }});
                expander.add_row(&row);
                rows.push((row, name.to_lowercase()));
            }
            filter_rows.push((expander, category.to_lowercase(), rows));
        }
        search.connect_search_changed(move |search| {
            let query = search.text().to_lowercase();
            for (expander, category, rows) in &filter_rows {
                let category_match =
                    query.is_empty() || search::fuzzy_ratio(category, &query).round() > 20.;
                let mut any = false;
                for (row, title) in rows {
                    let visible =
                        category_match || search::fuzzy_ratio(title, &query).round() > 20.;
                    row.set_visible(visible);
                    any |= visible;
                }
                expander.set_visible(any);
                expander.set_expanded(!query.is_empty() && any);
            }
        });
        let weak = Rc::downgrade(self);
        let store = gtk::Button::builder()
            .label("Add More")
            .margin_top(40)
            .margin_bottom(40)
            .build();
        store.add_css_class("suggested-action");
        store.connect_clicked(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.store();
            }
        });
        body.append(&store);
    }
    fn action_writer(
        self: &Rc<Self>,
        index: usize,
        selection: Selection,
        expected: String,
    ) -> Rc<dyn Fn(Vec<String>, Value)> {
        let weak = Rc::downgrade(self);
        Rc::new(move |path, value| {
            if let Some(ui) = weak.upgrade() {
                if *ui.selection.borrow() == selection
                    && let Some(action) = ui.draft.borrow_mut()["actions"].get_mut(index)
                {
                    let refs = path.iter().map(String::as_str).collect::<Vec<_>>();
                    set_nested(action, &refs, value.clone());
                }
                let owner = selection.clone();
                let id = expected.clone();
                let key = format!("{owner:?}/actions/{index}/{path:?}");
                ui.submit(Work {
                    key: Some(key),
                    title: String::new(),
                    rebuild: false,
                    execute: Box::new(move |engine| {
                        owner.edit(engine, |draft| {
                            let action = draft["actions"]
                                .get_mut(index)
                                .context("Action list changed")?;
                            anyhow::ensure!(action["id"] == id, "Action changed; select it again");
                            let refs = path.iter().map(String::as_str).collect::<Vec<_>>();
                            set_nested(action, &refs, value);
                            Ok(())
                        })
                    }),
                });
            }
        })
    }
    pub(super) fn configure_action(self: &Rc<Self>, index: usize) {
        let action = self.draft.borrow()["actions"]
            .get(index)
            .cloned()
            .unwrap_or(Value::Null);
        let id = action["id"].as_str().unwrap_or("").to_owned();
        let definition = self
            .definitions
            .borrow()
            .iter()
            .find(|d| d.0 == id)
            .cloned();
        let selection = self.selection.borrow().clone();
        if id.starts_with("native::MediaPlugin-")
            && self.player_refreshed.get().elapsed() >= Duration::from_secs(5)
        {
            self.player_refreshed.set(std::time::Instant::now());
            let (send, receive) = async_channel::bounded(1);
            let weak = Rc::downgrade(self);
            let owner = selection.clone();
            let expected = id.clone();
            std::thread::spawn(move || {
                let mut players = deckard_core::mpris::snapshot()
                    .unwrap_or_default()
                    .into_iter()
                    .map(|player| {
                        player
                            .bus
                            .trim_start_matches("org.mpris.MediaPlayer2.")
                            .to_owned()
                    })
                    .collect::<Vec<_>>();
                players.sort_unstable();
                players.dedup();
                let _ = send.send_blocking(json!(players));
            });
            glib::MainContext::default().spawn_local(async move {
                if let (Ok(players), Some(ui)) = (receive.recv().await, weak.upgrade()) {
                    let changed = *ui.players.borrow() != players;
                    *ui.players.borrow_mut() = players;
                    if changed
                        && *ui.selection.borrow() == owner
                        && ui.draft.borrow()["actions"][index]["id"] == expected
                        && ui
                            .sidebar
                            .visible_child()
                            .is_some_and(|child| child.widget_name() == "Configure Action")
                    {
                        ui.configure_action(index);
                    }
                }
            });
        }
        if id.starts_with("native::OBSPlugin-") {
            let connection = action["settings"]["connection"]
                .as_str()
                .or_else(|| action["settings"]["connection_id"].as_str())
                .unwrap_or("default")
                .to_owned();
            let scene = action["settings"]["scene"]
                .as_str()
                .unwrap_or("")
                .to_owned();
            let cached = self.choices.borrow()["connection"].as_str() == Some(&connection)
                && self.choices.borrow()["selected_scene"].as_str() == Some(&scene);
            if !cached {
                *self.choices.borrow_mut() =
                    json!({"connection":connection,"selected_scene":scene,"loading":true});
                let profile = self.shared.lock().unwrap().obs_profile(&connection);
                let (send, receive) = async_channel::bounded(1);
                let owner = selection.clone();
                let expected = id.clone();
                let weak = Rc::downgrade(self);
                std::thread::spawn(move || {
                    let mut result = deckard_core::obs::choices(&profile, &connection, &scene).unwrap_or_else(|error| json!({"connection":connection,"error":deckard_core::engine::redact(&error.to_string())}));
                    result["selected_scene"] = json!(scene);
                    let _ = send.send_blocking(result);
                });
                glib::MainContext::default().spawn_local(async move {
                    if let (Ok(result), Some(ui)) = (receive.recv().await, weak.upgrade()) {
                        if ui.choices.borrow()["connection"] != result["connection"]
                            || ui.choices.borrow()["selected_scene"] != result["selected_scene"]
                        {
                            return;
                        }
                        *ui.choices.borrow_mut() = result;
                        if *ui.selection.borrow() == owner
                            && ui.draft.borrow()["actions"][index]["id"] == expected
                            && ui
                                .sidebar
                                .visible_child()
                                .is_some_and(|child| child.widget_name() == "Configure Action")
                        {
                            ui.configure_action(index);
                        }
                    }
                });
            }
        }
        let writer = self.action_writer(index, selection.clone(), id.clone());
        let body = self.sidebar_page("Configure Action");
        let comments = adw::PreferencesGroup::builder().margin_top(20).build();
        let write = writer.clone();
        comments.add(&text(
            "Comment",
            action["comment"].as_str().unwrap_or(""),
            move |value| write(vec!["comment".into()], json!(value)),
        ));
        body.append(&comments);
        let assignments = adw::PreferencesGroup::builder().margin_top(20).build();
        let expander = adw::ExpanderRow::builder()
            .title("Event Assigner")
            .subtitle("Configure event assignments")
            .build();
        assignments.add(&expander);
        body.append(&assignments);
        let events = deckard_core::builtins::input_events(&selection.family);
        let setting_events = Rc::new(Cell::new(false));
        let controls = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        controls.add_css_class("linked");
        expander.add_suffix(&controls);
        let mut event_rows = Vec::new();
        for &(title, event) in events {
            let default = deckard_core::builtins::event_matches(&action, event);
            let current = action["event-assignments"]
                .as_object()
                .map_or(default, |map| {
                    map.get(title).map_or(default, |v| !v.is_null())
                });
            let write = writer.clone();
            let guarded = setting_events.clone();
            let row = choice(
                title,
                &["None", "Activate"],
                if current { "Activate" } else { "None" },
                move |value| {
                    if guarded.get() {
                        return;
                    }
                    write(
                        vec!["event-assignments".into(), title.into()],
                        if value == "None" {
                            Value::Null
                        } else {
                            json!("activate")
                        },
                    )
                },
            );
            expander.add_row(&row);
            event_rows.push((row, title, default));
        }
        let rows = Rc::new(event_rows);
        for (icon, tooltip, reset) in [
            ("edit-undo-symbolic", "Reset to default", true),
            ("edit-clear-all-symbolic", "Clear all", false),
        ] {
            let write = writer.clone();
            let rows = rows.clone();
            let guarded = setting_events.clone();
            let button = gtk::Button::builder()
                .icon_name(icon)
                .tooltip_text(tooltip)
                .valign(gtk::Align::Center)
                .build();
            button.connect_clicked(move |_| {
                guarded.set(true);
                let mut map = serde_json::Map::new();
                for (row, title, default) in rows.iter() {
                    let active = reset && *default;
                    row.set_selected(u32::from(active));
                    map.insert(
                        (*title).into(),
                        if active {
                            json!("activate")
                        } else {
                            Value::Null
                        },
                    );
                }
                guarded.set(false);
                write(vec!["event-assignments".into()], Value::Object(map));
            });
            controls.append(&button);
        }
        let separator = gtk::Separator::builder()
            .orientation(gtk::Orientation::Horizontal)
            .margin_top(20)
            .margin_bottom(20)
            .build();
        body.append(&separator);
        let group = adw::PreferencesGroup::new();
        body.append(&group);
        if let Some((_, name, fields)) = definition {
            let (title, category) = action_title(&name);
            group.set_title(title);
            group.set_description(Some(category));
            group.set_visible(!fields.is_empty());
            separator.set_visible(!fields.is_empty());
            for field in fields {
                if id == "native::OSPlugin-OpenInBrowser" && field.key == "new_window" {
                    continue;
                }
                let mut field = field;
                if id == "native::OSPlugin-OpenInBrowser" && field.key == "url" {
                    field.label = "URL:".into();
                }
                let current = action["settings"]
                    .get(&field.key)
                    .unwrap_or(&field.default)
                    .clone();
                let write = writer.clone();
                let key = field.key.clone();
                match field.kind.as_str() {
                    "bool" => group.add(&toggle(
                        &field.label,
                        current.as_bool().unwrap_or(false),
                        move |v| write(vec!["settings".into(), key.clone()], json!(v)),
                    )),
                    "number" => group.add(&number(
                        &field.label,
                        current.as_f64().unwrap_or(0.),
                        -1e9,
                        1e9,
                        if key.contains("delay") { 0.01 } else { 1. },
                        move |v| {
                            write(
                                vec!["settings".into(), key.clone()],
                                if v.fract() == 0. {
                                    json!(v as i64)
                                } else {
                                    json!(v)
                                },
                            )
                        },
                    )),
                    "array" if key.contains("color") => group.add(&color(
                        &field.label,
                        render::color(&current, [255, 255, 255, 255]),
                        move |v| write(vec!["settings".into(), key.clone()], v),
                    )),
                    "array" => {
                        let weak = Rc::downgrade(self);
                        group.add(&text(&field.label, &current.to_string(), move |v| {
                            match serde_json::from_str::<Value>(&v) {
                                Ok(value) if value.is_array() || value.is_object() => {
                                    write(vec!["settings".into(), key.clone()], value)
                                }
                                _ => {
                                    if let Some(ui) = weak.upgrade() {
                                        ui.toast("Enter a valid list or object");
                                    }
                                }
                            }
                        }));
                    }
                    _ => {
                        let options = self.action_choices(&id, &key, &action);
                        if !options.is_empty() {
                            let refs = options.iter().map(String::as_str).collect::<Vec<_>>();
                            group.add(&choice(
                                &field.label,
                                &refs,
                                current.as_str().unwrap_or(""),
                                move |v| write(vec!["settings".into(), key.clone()], json!(v)),
                            ));
                        } else if key.contains("path") || key.contains("icon") {
                            group.add(&file_row(
                                &self.window,
                                &field.label,
                                current.as_str().unwrap_or(""),
                                move |v| write(vec!["settings".into(), key.clone()], json!(v)),
                            ));
                        } else {
                            group.add(&text(
                                &field.label,
                                current.as_str().unwrap_or(""),
                                move |v| write(vec!["settings".into(), key.clone()], json!(v)),
                            ));
                        }
                    }
                }
            }
        } else {
            let label = gtk::Label::new(Some("This action needs an installed native replacement."));
            label.set_wrap(true);
            body.append(&label);
            let weak = Rc::downgrade(self);
            body.append(&button("Replace action", "edit-find-symbolic", move || {
                if let Some(ui) = weak.upgrade() {
                    ui.choose_action(Some(index));
                }
            }));
        }
        let weak = Rc::downgrade(self);
        let remove = gtk::Button::builder()
            .label("Remove Action")
            .margin_top(12)
            .margin_bottom(100)
            .build();
        remove.add_css_class("remove-action-button");
        remove.connect_clicked(move |_| {
            if let Some(ui) = weak.upgrade()
                && *ui.selection.borrow() == selection
            {
                ui.remove_action(index);
                ui.sidebar.set_visible_child_name("editor");
            }
        });
        body.append(&remove);
    }
    fn action_choices(&self, id: &str, key: &str, action: &Value) -> Vec<String> {
        let mut options = Vec::new();
        if ["player", "player_name"].contains(&key) {
            options.push(String::new());
            if let Some(players) = self.players.borrow().as_array() {
                options.extend(players.iter().filter_map(|p| p.as_str().map(str::to_owned)));
            }
        }
        if ["page", "selected_page"].contains(&key) {
            options.extend(self.page_names.borrow().iter().cloned());
        }
        if ["connection", "connection_id"].contains(&key)
            && let Some(profiles) =
                self.shared.lock().unwrap().docs.settings["obs"]["connections"].as_object()
        {
            options.extend(profiles.keys().cloned());
        }
        let choices = self.choices.borrow();
        let data = match key {
            "scene" => Some((&choices["scenes"]["scenes"], "sceneName")),
            "input" => Some((&choices["inputs"]["inputs"], "inputName")),
            "scene_collection" => Some((
                &choices["collections"]["sceneCollections"],
                "sceneCollectionName",
            )),
            "item" => Some((
                &choices["items"][action["settings"]["scene"].as_str().unwrap_or("")]["sceneItems"],
                "sourceName",
            )),
            "filter" => Some((
                &choices["filters"][action["settings"]["scene"].as_str().unwrap_or("")]["filters"],
                "filterName",
            )),
            _ => None,
        };
        if let Some((data, key)) = data
            && let Some(rows) = data.as_array()
        {
            options.extend(
                rows.iter()
                    .filter_map(|row| row[key].as_str().map(str::to_owned)),
            );
        }
        let constants: &[&str] = match (id, key) {
            ("native::media", "method") => {
                &["PlayPause", "Play", "Pause", "Next", "Previous", "Stop"]
            }
            ("native::audio", "operation") => &["toggle-mute", "set-volume", "adjust-volume"],
            ("native::mixer", "operation") => &["open", "exit", "left", "right"],
            ("native::input", "operation") => &["keys", "click", "move"],
            ("native::input", "button") => &["left", "middle", "right"],
            _ => &[],
        };
        options.extend(constants.iter().map(|s| (*s).into()));
        options
    }
}
