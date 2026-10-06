use super::*;
use controls::*;

const EVENTS: &[&str] = &[
    "auto",
    "lifecycle",
    "press",
    "release",
    "long-press",
    "touch",
    "long-touch",
    "turn-cw",
    "turn-ccw",
    "swipe-left",
    "swipe-right",
];

impl Ui {
    pub(super) fn action_rows(self: &Rc<Self>) {
        let (group, expander) = self.editor_group("Actions", "Actions for this key");
        let actions = self.draft.borrow()["actions"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        for (index, action) in actions.iter().enumerate() {
            let id = action["id"].as_str().unwrap_or("");
            let title = self
                .definitions
                .borrow()
                .iter()
                .find(|d| d.0 == id)
                .map(|d| d.1.clone())
                .unwrap_or_else(|| format!("Replacement needed: {id}"));
            let row = adw::ActionRow::builder()
                .title(&title)
                .subtitle(action["event"].as_str().unwrap_or("press"))
                .activatable(true)
                .build();
            let weak = Rc::downgrade(self);
            row.connect_activated(move |_| {
                if let Some(ui) = weak.upgrade() {
                    ui.configure_action(index);
                }
            });
            let actions_box = gtk::Box::new(gtk::Orientation::Horizontal, 2);
            actions_box.set_valign(gtk::Align::Center);
            for (icon, delta) in [("go-up-symbolic", -1isize), ("go-down-symbolic", 1isize)] {
                let weak = Rc::downgrade(self);
                let button = button("", icon, move || {
                    if let Some(ui) = weak.upgrade() {
                        ui.reorder_action(index, index.saturating_add_signed(delta));
                    }
                });
                button.add_css_class("flat");
                button.set_sensitive(if delta < 0 {
                    index > 0
                } else {
                    index + 1 < actions.len()
                });
                actions_box.append(&button);
            }
            let weak = Rc::downgrade(self);
            let remove = button("", "user-trash-symbolic", move || {
                if let Some(ui) = weak.upgrade() {
                    ui.remove_action(index);
                }
            });
            remove.add_css_class("flat");
            actions_box.append(&remove);
            row.add_suffix(&actions_box);
            let source = gtk::DragSource::new();
            source.set_actions(gdk::DragAction::MOVE);
            source.connect_prepare(move |_, _, _| {
                Some(gdk::ContentProvider::for_value(&(index as u32).to_value()))
            });
            row.add_controller(source);
            let drop = gtk::DropTarget::new(u32::static_type(), gdk::DragAction::MOVE);
            let weak = Rc::downgrade(self);
            drop.connect_drop(move |_, value, _, _| {
                if let (Some(ui), Ok(source)) = (weak.upgrade(), value.get::<u32>()) {
                    ui.reorder_action(source as usize, index);
                    true
                } else {
                    false
                }
            });
            row.add_controller(drop);
            expander.add_row(&row);
        }
        let weak = Rc::downgrade(self);
        let add = button("Add Action", "list-add-symbolic", move || {
            if let Some(ui) = weak.upgrade() {
                ui.choose_action(None);
            }
        });
        add.add_css_class("add-action-button");
        let row = adw::PreferencesRow::new();
        row.set_child(Some(&add));
        expander.add_row(&row);
        self.controls.append(&group);
    }
    fn action_work(
        self: &Rc<Self>,
        work: impl FnOnce(&mut Vec<Value>) -> Result<()> + Send + 'static,
    ) {
        let sel = self.selection.borrow().clone();
        self.submit(Work {
            key: None,
            title: String::new(),
            rebuild: true,
            execute: Box::new(move |engine| {
                sel.edit(engine, |draft| {
                    let mut actions = draft["actions"].as_array().cloned().unwrap_or_default();
                    work(&mut actions)?;
                    draft["actions"] = json!(actions);
                    Ok(())
                })
            }),
        });
    }
    fn reorder_action(self: &Rc<Self>, source: usize, target: usize) {
        self.action_work(move |actions| {
            anyhow::ensure!(
                source < actions.len() && target < actions.len(),
                "Action list changed; select it again"
            );
            let value = actions.remove(source);
            actions.insert(target, value);
            Ok(())
        });
    }
    fn remove_action(self: &Rc<Self>, index: usize) {
        self.action_work(move |actions| {
            anyhow::ensure!(index < actions.len(), "Action list changed");
            actions.remove(index);
            Ok(())
        });
    }
    fn sidebar_page(self: &Rc<Self>, title: &str) -> gtk::Box {
        if let Some(child) = self.sidebar.child_by_name("action-page") {
            self.sidebar.remove(&child);
        }
        let box_ = gtk::Box::new(gtk::Orientation::Vertical, 12);
        margins(&box_, 12);
        let header = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        let weak = Rc::downgrade(self);
        header.append(&button("Back", "go-previous-symbolic", move || {
            if let Some(ui) = weak.upgrade() {
                ui.sidebar.set_visible_child_name("editor");
            }
        }));
        let label = gtk::Label::new(Some(title));
        label.add_css_class("heading");
        header.append(&label);
        box_.append(&header);
        let scroll = gtk::ScrolledWindow::builder()
            .vexpand(true)
            .hscrollbar_policy(gtk::PolicyType::Never)
            .build();
        let body = gtk::Box::new(gtk::Orientation::Vertical, 12);
        scroll.set_child(Some(&body));
        box_.append(&scroll);
        self.sidebar.add_named(&box_, Some("action-page"));
        self.sidebar.set_visible_child_name("action-page");
        body
    }
    pub(super) fn choose_action(self: &Rc<Self>, replace: Option<usize>) {
        let body = self.sidebar_page("Choose an action");
        let search = gtk::SearchEntry::builder()
            .placeholder_text("Search actions")
            .build();
        body.append(&search);
        let categories = gtk::DropDown::from_strings(&[
            "All actions",
            "OS",
            "Deck",
            "Media",
            "OBS",
            "Volume Mixer",
            "Native plugins",
        ]);
        body.append(&categories);
        let list = gtk::ListBox::new();
        list.set_selection_mode(gtk::SelectionMode::None);
        list.add_css_class("boxed-list");
        body.append(&list);
        for (id, name, fields) in self.definitions.borrow().iter() {
            let row = adw::ActionRow::builder()
                .title(name)
                .subtitle(id)
                .activatable(true)
                .build();
            row.set_widget_name(&format!("{id} {name}"));
            let fields = fields.clone();
            let id = id.clone();
            let weak = Rc::downgrade(self);
            row.connect_activated(move|_|{if let Some(ui)=weak.upgrade(){
                let settings=fields.iter().map(|field|(field.key.clone(),field.default.clone())).collect::<serde_json::Map<_,_>>();
                let action=json!({"id":id,"event":if id.contains("Plugin-")||id.contains("VolumeMixer-"){"auto"}else{"press"},"settings":settings});
                ui.action_work(move|actions|{if let Some(index)=replace {anyhow::ensure!(index<actions.len(),"Action list changed");actions[index]=action;}else{actions.push(action);}Ok(())});
                ui.sidebar.set_visible_child_name("editor");
            }});
            list.append(&row);
        }
        let search_ref = search.clone();
        let category_ref = categories.clone();
        list.set_filter_func(move |row| {
            let text = row.widget_name();
            let match_search = text
                .to_lowercase()
                .contains(&search_ref.text().to_lowercase());
            let match_category = match category_ref.selected() {
                0 => true,
                1 => {
                    text.contains("OSPlugin-")
                        || text.contains("native::shell")
                        || text.contains("native::launch")
                        || text.contains("native::hotkey")
                        || text.contains("native::text")
                }
                2 => text.contains("DeckPlugin-"),
                3 => text.contains("MediaPlugin-") || text.contains("native::media"),
                4 => text.contains("OBSPlugin-") || text.contains("native::obs"),
                5 => {
                    text.contains("VolumeMixer-")
                        || text.contains("native::audio")
                        || text.contains("native::mixer")
                }
                _ => !text.starts_with("native::"),
            };
            match_search && match_category
        });
        let weak = list.downgrade();
        search.connect_search_changed(move |_| {
            if let Some(list) = weak.upgrade() {
                list.invalidate_filter();
            }
        });
        let weak = list.downgrade();
        categories.connect_selected_notify(move |_| {
            if let Some(list) = weak.upgrade() {
                list.invalidate_filter();
            }
        });
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
        let body = self.sidebar_page(
            definition
                .as_ref()
                .map(|d| d.1.as_str())
                .unwrap_or("Missing native replacement"),
        );
        let group = adw::PreferencesGroup::new();
        body.append(&group);
        let weak = Rc::downgrade(self);
        group.add(&choice(
            "Trigger",
            EVENTS,
            action["event"].as_str().unwrap_or("press"),
            move |value| {
                if let Some(ui) = weak.upgrade() {
                    ui.action_property(index, None, json!(value));
                }
            },
        ));
        if let Some((_, _, fields)) = definition {
            for field in fields {
                let current = action["settings"]
                    .get(&field.key)
                    .unwrap_or(&field.default)
                    .clone();
                let weak = Rc::downgrade(self);
                let key = field.key.clone();
                match field.kind.as_str() {
                    "bool" => group.add(&toggle(
                        &field.label,
                        current.as_bool().unwrap_or(false),
                        move |v| {
                            if let Some(ui) = weak.upgrade() {
                                ui.action_property(index, Some(key.clone()), json!(v));
                            }
                        },
                    )),
                    "number" => group.add(&number(
                        &field.label,
                        current.as_f64().unwrap_or(0.),
                        -1e9,
                        1e9,
                        if key.contains("delay") { 0.01 } else { 1. },
                        move |v| {
                            if let Some(ui) = weak.upgrade() {
                                ui.action_property(
                                    index,
                                    Some(key.clone()),
                                    if v.fract() == 0. {
                                        json!(v as i64)
                                    } else {
                                        json!(v)
                                    },
                                );
                            }
                        },
                    )),
                    "array" if key.contains("color") => {
                        group.add(&color(
                            &field.label,
                            render::color(&current, [255, 255, 255, 255]),
                            move |v| {
                                if let Some(ui) = weak.upgrade() {
                                    ui.action_property(index, Some(key.clone()), v);
                                }
                            },
                        ));
                    }
                    "array" => {
                        let row = text(&field.label, &current.to_string(), move |v| {
                            if let Some(ui) = weak.upgrade() {
                                match serde_json::from_str::<Value>(&v) {
                                    Ok(value) if value.is_array() || value.is_object() => {
                                        ui.action_property(index, Some(key.clone()), value)
                                    }
                                    _ => ui.toast("Enter a valid list or object"),
                                }
                            }
                        });
                        group.add(&row);
                    }
                    _ => {
                        let options = self.action_choices(&id, &key, &action);
                        if !options.is_empty() {
                            let refs = options.iter().map(String::as_str).collect::<Vec<_>>();
                            group.add(&choice(
                                &field.label,
                                &refs,
                                current.as_str().unwrap_or(""),
                                move |v| {
                                    if let Some(ui) = weak.upgrade() {
                                        ui.action_property(index, Some(key.clone()), json!(v));
                                    }
                                },
                            ));
                        } else if key.contains("path") || key.contains("icon") {
                            group.add(&file_row(
                                &self.window,
                                &field.label,
                                current.as_str().unwrap_or(""),
                                move |v| {
                                    if let Some(ui) = weak.upgrade() {
                                        ui.action_property(index, Some(key.clone()), json!(v));
                                    }
                                },
                            ));
                        } else {
                            group.add(&text(
                                &field.label,
                                current.as_str().unwrap_or(""),
                                move |v| {
                                    if let Some(ui) = weak.upgrade() {
                                        ui.action_property(index, Some(key.clone()), json!(v));
                                    }
                                },
                            ));
                        }
                    }
                }
            }
        } else {
            let label = gtk::Label::new(Some(
                "This Python action has no installed native replacement. Its settings are preserved until you choose a replacement.",
            ));
            label.set_wrap(true);
            body.append(&label);
        }
        let weak = Rc::downgrade(self);
        body.append(&button("Replace action", "edit-find-symbolic", move || {
            if let Some(ui) = weak.upgrade() {
                ui.choose_action(Some(index));
            }
        }));
    }
    fn action_property(self: &Rc<Self>, index: usize, key: Option<String>, value: Value) {
        let selection = self.selection.borrow().clone();
        let expected = self.draft.borrow()["actions"][index]["id"]
            .as_str()
            .unwrap_or("")
            .to_owned();
        if let Some(action) = self.draft.borrow_mut()["actions"].get_mut(index) {
            if let Some(key) = &key {
                set_nested(action, &["settings", key], value.clone());
            } else {
                action["event"] = value.clone();
            }
        }
        self.submit(Work {
            key: Some(format!("{selection:?}/actions/{index}/{key:?}")),
            title: String::new(),
            rebuild: false,
            execute: Box::new(move |engine| {
                selection.edit(engine, |draft| {
                    let action = draft["actions"]
                        .get_mut(index)
                        .context("Action list changed")?;
                    anyhow::ensure!(action["id"] == expected, "Action changed; select it again");
                    if let Some(key) = key {
                        set_nested(action, &["settings", &key], value);
                    } else {
                        action["event"] = value;
                    }
                    Ok(())
                })
            }),
        });
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
