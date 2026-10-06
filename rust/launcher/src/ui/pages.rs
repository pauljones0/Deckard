//! Upstream Page Manager: navigation split, page list and page override groups.
#![allow(deprecated)]
use super::*;
use configuration::ConfigTarget;
use controls::*;

pub(super) struct PageManager {
    window: adw::ApplicationWindow,
    list: gtk::ListBox,
    body: gtk::Box,
    stack: gtk::Stack,
    selected: RefCell<Option<String>>,
    actions: gio::SimpleActionGroup,
}
impl PageManager {
    pub(super) fn refresh(self: &Rc<Self>, ui: &Rc<Ui>) {
        let chosen = self.selected.borrow().clone();
        self.list.remove_all();
        let pages = ui
            .shared
            .lock()
            .unwrap()
            .docs
            .pages
            .keys()
            .cloned()
            .collect::<Vec<_>>();
        for page in pages {
            let row = gtk::ListBoxRow::new();
            row.set_widget_name(&page);
            let content = gtk::Box::builder()
                .orientation(gtk::Orientation::Horizontal)
                .hexpand(true)
                .build();
            content.append(
                &gtk::Label::builder()
                    .label(&page)
                    .hexpand(true)
                    .xalign(0.)
                    .ellipsize(gtk::pango::EllipsizeMode::End)
                    .build(),
            );
            let weak = Rc::downgrade(ui);
            let parent = self.window.downgrade();
            let name = page.clone();
            let remove = button("", "user-trash-symbolic", move || {
                if let (Some(ui), Some(parent)) = (weak.upgrade(), parent.upgrade()) {
                    let confirm = adw::MessageDialog::builder()
                        .transient_for(&parent)
                        .modal(true)
                        .title("Delete Page")
                        .body(format!("Are you sure you want to delete the page: {name}"))
                        .build();
                    confirm.add_response("cancel", "Cancel");
                    confirm.add_response("delete", "Delete");
                    confirm.set_response_appearance("delete", adw::ResponseAppearance::Destructive);
                    let weak = Rc::downgrade(&ui);
                    let name = name.clone();
                    confirm.connect_response(None, move |_, response| {
                        if response == "delete"
                            && let Some(ui) = weak.upgrade()
                        {
                            ui.command("delete-page", json!({"page":name}), "Page deleted", true);
                        }
                    });
                    confirm.present();
                }
            });
            remove.set_css_classes(&["flat"]);
            remove.set_halign(gtk::Align::End);
            content.append(&remove);
            row.set_child(Some(&content));
            self.list.append(&row);
            if chosen.as_ref() == Some(&page) {
                self.list.select_row(Some(&row));
            }
        }
    }
    fn select(&self, page: &str) {
        let mut child = self.list.first_child();
        while let Some(widget) = child {
            if widget.widget_name() == page
                && let Ok(row) = widget.clone().downcast::<gtk::ListBoxRow>()
            {
                self.list.select_row(Some(&row));
                return;
            }
            child = widget.next_sibling();
        }
    }
}
impl Ui {
    pub(super) fn pages(self: &Rc<Self>) {
        if let Some(manager) = self.page_manager.borrow().as_ref() {
            manager.window.present();
            return;
        }
        let window = adw::ApplicationWindow::builder()
            .title("Page Manager")
            .default_width(400)
            .default_height(600)
            .width_request(1300)
            .height_request(800)
            .build();
        let split = adw::NavigationSplitView::builder()
            .vexpand(true)
            .sidebar_width_fraction(0.4)
            .min_sidebar_width(300.)
            .build();
        window.set_content(Some(&split));
        let sidebar = gtk::Box::new(gtk::Orientation::Vertical, 0);
        let header = adw::HeaderBar::new();
        header.add_css_class("flat");
        sidebar.append(&header);
        let search = gtk::SearchEntry::builder()
            .placeholder_text("Search")
            .hexpand(true)
            .build();
        margins(&search, 7);
        header.set_title_widget(Some(&search));
        let list = gtk::ListBox::new();
        list.add_css_class("navigation-sidebar");
        list.set_selection_mode(gtk::SelectionMode::Single);
        let clamp = adw::Clamp::builder().child(&list).build();
        let scroll = gtk::ScrolledWindow::builder()
            .vexpand(true)
            .child(&clamp)
            .build();
        sidebar.append(&scroll);
        sidebar.append(&gtk::Box::new(gtk::Orientation::Horizontal, 0));
        let add = gtk::Button::with_label("Add New");
        add.add_css_class("suggested-action");
        margins(&add, 7);
        sidebar.append(&add);
        split.set_sidebar(Some(&adw::NavigationPage::new(&sidebar, "Page Selector")));
        let editor = gtk::Box::builder()
            .orientation(gtk::Orientation::Vertical)
            .hexpand(true)
            .build();
        let header = adw::HeaderBar::builder()
            .show_back_button(false)
            .show_end_title_buttons(true)
            .build();
        header.add_css_class("flat");
        editor.append(&header);
        let menu = gtk::MenuButton::builder()
            .icon_name("open-menu-symbolic")
            .build();
        header.pack_end(&menu);
        let stack = gtk::Stack::builder()
            .hexpand(true)
            .vexpand(true)
            .margin_bottom(20)
            .build();
        editor.append(&stack);
        let empty = gtk::Box::builder().hexpand(true).vexpand(true).build();
        empty.append(
            &gtk::Label::builder()
                .label("No page selected")
                .halign(gtk::Align::Center)
                .valign(gtk::Align::Center)
                .hexpand(true)
                .build(),
        );
        stack.add_named(&empty, Some("no-page"));
        let body = gtk::Box::new(gtk::Orientation::Vertical, 0);
        let clamp = adw::Clamp::builder().margin_top(40).child(&body).build();
        let scroll = gtk::ScrolledWindow::builder()
            .hexpand(true)
            .vexpand(true)
            .child(&clamp)
            .build();
        stack.add_named(&scroll, Some("editor"));
        stack.set_visible_child_name("no-page");
        split.set_content(Some(&adw::NavigationPage::new(&editor, "Page Editor")));
        let actions = gio::SimpleActionGroup::new();
        window.insert_action_group("pm", Some(&actions));
        let manager = Rc::new(PageManager {
            window: window.clone(),
            list,
            body,
            stack,
            selected: RefCell::new(None),
            actions,
        });
        let menu_model = gio::Menu::new();
        for (title, name) in [
            ("Duplicate", "duplicate"),
            ("Export Page", "export"),
            ("Export All", "export-all"),
        ] {
            menu_model.append(Some(title), Some(&format!("pm.{name}")));
        }
        let import = gio::Menu::new();
        for (title, name) in [
            ("Page", "import"),
            ("StreamController", "import-controller"),
            ("StreamDeck UI", "import-streamdeck"),
        ] {
            import.append(Some(title), Some(&format!("pm.{name}")));
        }
        menu_model.append_submenu(Some("Import"), &import);
        menu.set_menu_model(Some(&menu_model));
        for name in [
            "duplicate",
            "export",
            "export-all",
            "import",
            "import-controller",
            "import-streamdeck",
        ] {
            let action = gio::SimpleAction::new(name, None);
            action.set_enabled(!matches!(name, "duplicate" | "export"));
            let weak = Rc::downgrade(self);
            let owner = Rc::downgrade(&manager);
            action.connect_activate(move |_, _| {
                if let (Some(ui), Some(manager)) = (weak.upgrade(), owner.upgrade()) {
                    ui.page_menu(&manager, name);
                }
            });
            manager.actions.add_action(&action);
        }
        let weak = Rc::downgrade(self);
        let owner = Rc::downgrade(&manager);
        manager.list.connect_row_selected(move |_, row| {
            if let (Some(ui), Some(manager)) = (weak.upgrade(), owner.upgrade()) {
                *manager.selected.borrow_mut() = row.map(|row| row.widget_name().to_string());
                for name in ["duplicate", "export"] {
                    if let Some(action) = manager
                        .actions
                        .lookup_action(name)
                        .and_downcast::<gio::SimpleAction>()
                    {
                        action.set_enabled(row.is_some());
                    }
                }
                if let Some(row) = row {
                    ui.page_editor(&manager.body, &row.widget_name());
                    manager.stack.set_visible_child_name("editor");
                } else {
                    manager.stack.set_visible_child_name("no-page");
                }
            }
        });
        let query = search.clone();
        manager.list.set_filter_func(move |row| {
            let needle = query.text().to_lowercase();
            needle.is_empty()
                || super::search::fuzzy_ratio(&row.widget_name().to_lowercase(), &needle) > 50.
        });
        let query = search.clone();
        manager.list.set_sort_func(move |a, b| {
            let query = query.text();
            let order = if query.is_empty() {
                super::search::natural_cmp(a.widget_name().as_str(), b.widget_name().as_str())
            } else {
                super::search::fuzzy_ratio(&b.widget_name().to_lowercase(), &query.to_lowercase())
                    .total_cmp(&super::search::fuzzy_ratio(
                        &a.widget_name().to_lowercase(),
                        &query.to_lowercase(),
                    ))
            };
            order.into()
        });
        let list = manager.list.downgrade();
        search.connect_search_changed(move |_| {
            if let Some(list) = list.upgrade() {
                list.invalidate_filter();
                list.invalidate_sort();
            }
        });
        let weak = Rc::downgrade(self);
        let parent = window.downgrade();
        add.connect_clicked(move |_| {
            if let (Some(ui), Some(parent)) = (weak.upgrade(), parent.upgrade()) {
                let weak = Rc::downgrade(&ui);
                ui.page_name_dialog(&parent, "Add Page", "Add", "", move |name| {
                    if let Some(ui) = weak.upgrade() {
                        ui.command("create-page", json!({"page":name}), "Page saved", true);
                    }
                });
            }
        });
        let weak = Rc::downgrade(self);
        window.connect_close_request(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.page_manager.borrow_mut().take();
            }
            glib::Propagation::Proceed
        });
        manager.refresh(self);
        *self.page_manager.borrow_mut() = Some(manager);
        self.keep_window("Page Manager", &window);
    }
    pub(super) fn page_settings(self: &Rc<Self>) {
        self.pages();
        if let Some(manager) = self.page_manager.borrow().as_ref() {
            manager.select(&self.selection.borrow().page);
        }
    }
    fn page_name_dialog(
        self: &Rc<Self>,
        parent: &impl IsA<gtk::Window>,
        title: &str,
        confirm: &str,
        value: &str,
        save: impl Fn(String) + 'static,
    ) {
        let window = gtk::ApplicationWindow::builder()
            .title(title)
            .default_width(350)
            .default_height(150)
            .transient_for(parent)
            .modal(true)
            .build();
        let header = gtk::HeaderBar::builder().show_title_buttons(false).build();
        header.add_css_class("flat");
        window.set_titlebar(Some(&header));
        let cancel = gtk::Button::with_label("Cancel");
        header.pack_start(&cancel);
        let accept = gtk::Button::with_label(confirm);
        accept.add_css_class("confirm-button");
        header.pack_end(&accept);
        let content = gtk::Box::builder()
            .orientation(gtk::Orientation::Vertical)
            .hexpand(true)
            .vexpand(true)
            .build();
        margins(&content, 20);
        content.append(&gtk::Label::new(Some("")));
        let input = gtk::Entry::builder()
            .hexpand(true)
            .margin_top(10)
            .text(value)
            .placeholder_text("Page Name")
            .build();
        content.append(&input);
        let warning = gtk::Label::builder()
            .label("Page name cannot be empty")
            .margin_top(10)
            .build();
        warning.add_css_class("warning-label");
        content.append(&warning);
        window.set_child(Some(&content));
        let weak = Rc::downgrade(self);
        let button = accept.downgrade();
        let warning = warning.downgrade();
        let validate: Rc<dyn Fn(&gtk::Entry)> = Rc::new(move |input| {
            if let (Some(ui), Some(button), Some(warning)) =
                (weak.upgrade(), button.upgrade(), warning.upgrade())
            {
                let name = input.text();
                let exists = ui
                    .shared
                    .lock()
                    .unwrap()
                    .docs
                    .pages
                    .contains_key(name.as_str());
                let valid = model::valid_name(&name).is_ok() && !exists;
                button.set_sensitive(valid);
                warning.set_visible(!valid);
                warning.set_text(if exists {
                    "Page already exists"
                } else {
                    "Page name cannot be empty"
                });
            }
        });
        validate(&input);
        let check = validate.clone();
        input.connect_changed(move |input| check(input));
        let weak_window = window.downgrade();
        cancel.connect_clicked(move |_| {
            if let Some(window) = weak_window.upgrade() {
                window.close();
            }
        });
        let save = Rc::new(save);
        let entry = input.clone();
        let close = window.downgrade();
        let call = save.clone();
        accept.connect_clicked(move |_| {
            call(entry.text().to_string());
            if let Some(window) = close.upgrade() {
                window.close();
            }
        });
        let button = accept.downgrade();
        input.connect_activate(move |_| {
            if let Some(button) = button.upgrade()
                && button.is_sensitive()
            {
                button.emit_clicked();
            }
        });
        self.keep_window(title, &window);
        window.set_transient_for(Some(parent));
    }
    fn page_editor(self: &Rc<Self>, body: &gtk::Box, page: &str) {
        clear(body);
        let target = ConfigTarget::Page(page.into());
        let document = target.document(self);
        let name_group = adw::PreferencesGroup::new();
        body.append(&name_group);
        let name = adw::EntryRow::builder()
            .title("Name")
            .show_apply_button(true)
            .text(page)
            .build();
        name_group.add(&name);
        let weak = Rc::downgrade(self);
        let old = page.to_owned();
        name.connect_apply(move |row| {
            if let Some(ui) = weak.upgrade() {
                ui.command(
                    "rename-page",
                    json!({"page":old,"name":row.text().as_str()}),
                    "Page saved",
                    true,
                );
            }
        });
        let defaults = adw::PreferencesGroup::builder()
            .title("Default Page")
            .build();
        body.append(&defaults);
        defaults.add(&self.multi_deck_row(page, false));
        let auto = adw::PreferencesGroup::builder()
            .title("Auto change Page")
            .build();
        body.append(&auto);
        auto.add(&self.page_switch(
            "Enable",
            "",
            page,
            &["settings", "auto-change", "enable"],
            false,
        ));
        auto.add(&self.page_switch(
            "Stay on page",
            "Stay on the page until another page matches",
            page,
            &["settings", "auto-change", "stay-on-page"],
            true,
        ));
        auto.add(&self.multi_deck_row(page, true));
        for (title, key) in [("Title Regex", "title"), ("WM Class Regex", "wm-class")] {
            let entry = adw::EntryRow::builder()
                .title(title)
                .show_apply_button(true)
                .text(
                    document["settings"]["auto-change"][key]
                        .as_str()
                        .unwrap_or(""),
                )
                .build();
            let weak = Rc::downgrade(self);
            let page = page.to_owned();
            let save: Rc<dyn Fn(&adw::EntryRow)> = Rc::new(move |entry| {
                if let Some(ui) = weak.upgrade() {
                    let value = entry.text();
                    if regex_valid(&value) {
                        entry.remove_css_class("error");
                        ui.page_property(
                            page.clone(),
                            vec!["settings".into(), "auto-change".into(), key.into()],
                            json!(value.as_str()),
                        );
                    } else {
                        entry.add_css_class("error");
                    }
                }
            });
            let apply = save.clone();
            entry.connect_apply(move |entry| apply(entry));
            let focus = gtk::EventControllerFocus::new();
            let field = entry.downgrade();
            focus.connect_leave(move |_| {
                if let Some(entry) = field.upgrade() {
                    save(&entry);
                }
            });
            entry.add_controller(focus);
            auto.add(&entry);
        }
        let matching = adw::ExpanderRow::builder()
            .title("Matching Windows")
            .subtitle("Windows matching your regex")
            .build();
        let weak = Rc::downgrade(self);
        let page_owned = page.to_owned();
        let expander = matching.downgrade();
        let refresh = button("", "view-refresh-symbolic", move || {
            if let (Some(ui), Some(expander)) = (weak.upgrade(), expander.upgrade()) {
                ui.matching_windows(&expander, &page_owned);
            }
        });
        refresh.set_valign(gtk::Align::Center);
        refresh.set_css_classes(&["flat"]);
        matching.add_suffix(&refresh);
        auto.add(&matching);
        let brightness = adw::PreferencesGroup::builder()
            .title("Brightness Override")
            .build();
        let expander = self.page_override(
            "Overwrite Brightness",
            "Overrides the Deck Brightness",
            page,
            "brightness",
            &document,
        );
        expander.add_row(&self.config_scale_row(
            "Brightness",
            &target,
            &["brightness", "value"],
            75.,
            0.,
            100.,
            1.,
            0,
        ));
        brightness.add(&expander);
        body.append(&brightness);
        for (section, group_title, title, subtitle) in [
            (
                "background",
                "Background Override",
                "Overwrite Background",
                "Overrides the Deck Background",
            ),
            (
                "screensaver",
                "Screensaver Overwrite",
                "Overwrite Screensaver",
                "Overrides the Deck Screensaver",
            ),
        ] {
            let group = adw::PreferencesGroup::builder().title(group_title).build();
            let expander = self.page_override(title, subtitle, page, section, &document);
            group.add(&expander);
            body.append(&group);
            let content = gtk::Box::new(gtk::Orientation::Horizontal, 0);
            let fields = gtk::Box::builder()
                .orientation(gtk::Orientation::Vertical)
                .hexpand(true)
                .valign(gtk::Align::Center)
                .build();
            content.append(&fields);
            fields.append(&self.page_switch(
                if section == "background" {
                    "Show Background"
                } else {
                    "Enable Screensaver"
                },
                "",
                page,
                &[
                    section,
                    if section == "background" {
                        "show"
                    } else {
                        "enable"
                    },
                ],
                true,
            ));
            if section == "screensaver" {
                fields.append(&self.page_spin(
                    "Delay (min)",
                    page,
                    &[section, "time-delay"],
                    5.,
                    1.,
                    60.,
                ));
            }
            fields.append(&self.page_switch("Loop", "", page, &[section, "loop"], true));
            fields.append(&self.page_spin("FPS", page, &[section, "fps"], 0., 0., 120.));
            if section == "background" {
                fields.append(&self.page_switch(
                    "Extend to Touchscreen",
                    "Continue the background onto the touch strip (Stream Deck +)",
                    page,
                    &[section, "extend-to-touchscreen"],
                    false,
                ));
            } else {
                fields.append(&self.config_scale_row(
                    "Brightness",
                    &target,
                    &[section, "brightness"],
                    30.,
                    0.,
                    100.,
                    1.,
                    0,
                ));
            }
            let buttons = gtk::Box::builder()
                .orientation(gtk::Orientation::Vertical)
                .hexpand(true)
                .valign(gtk::Align::Center)
                .build();
            content.append(&buttons);
            let media = self.config_media_button(&target, section);
            media.set_halign(gtk::Align::Center);
            buttons.append(&media);
            if section == "background" {
                let weak = Rc::downgrade(self);
                let owner = target.clone();
                let adjust = button("Adjust View", "", move || {
                    if let Some(ui) = weak.upgrade() {
                        ui.viewport(&owner);
                    }
                });
                adjust.set_halign(gtk::Align::Center);
                adjust.set_margin_top(10);
                buttons.append(&adjust);
            }
            let row = adw::PreferencesRow::new();
            row.set_child(Some(&content));
            expander.add_row(&row);
        }
    }
    fn page_switch(
        self: &Rc<Self>,
        title: &str,
        subtitle: &str,
        page: &str,
        path: &[&str],
        default: bool,
    ) -> adw::SwitchRow {
        let value = self
            .shared
            .lock()
            .unwrap()
            .docs
            .pages
            .get(page)
            .map(|doc| get_path(doc, path).as_bool().unwrap_or(default))
            .unwrap_or(default);
        let keys = path.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let page = page.to_owned();
        let weak = Rc::downgrade(self);
        let row = toggle(title, value, move |value| {
            if let Some(ui) = weak.upgrade() {
                ui.page_property(page.clone(), keys.clone(), json!(value));
            }
        });
        row.set_subtitle(subtitle);
        row
    }
    fn page_spin(
        self: &Rc<Self>,
        title: &str,
        page: &str,
        path: &[&str],
        default: f64,
        min: f64,
        max: f64,
    ) -> adw::SpinRow {
        let row = adw::SpinRow::with_range(min, max, 1.);
        row.set_title(title);
        row.set_value(
            self.shared
                .lock()
                .unwrap()
                .docs
                .pages
                .get(page)
                .map(|doc| get_path(doc, path).as_f64().unwrap_or(default))
                .unwrap_or(default),
        );
        let keys = path.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
        let page = page.to_owned();
        let weak = Rc::downgrade(self);
        row.connect_changed(move |row| {
            if let Some(ui) = weak.upgrade() {
                ui.page_property(
                    page.clone(),
                    keys.clone(),
                    json!(row.value().round() as u64),
                );
            }
        });
        row
    }
    fn page_override(
        self: &Rc<Self>,
        title: &str,
        subtitle: &str,
        page: &str,
        section: &str,
        document: &Value,
    ) -> adw::ExpanderRow {
        let active = document[section]["overwrite"].as_bool().unwrap_or(false);
        let row = adw::ExpanderRow::builder()
            .title(title)
            .subtitle(subtitle)
            .show_enable_switch(true)
            .enable_expansion(active)
            .expanded(active)
            .build();
        let weak = Rc::downgrade(self);
        let page = page.to_owned();
        let section = section.to_owned();
        row.connect_enable_expansion_notify(move |row| {
            if let Some(ui) = weak.upgrade() {
                ui.page_property(
                    page.clone(),
                    vec![section.clone(), "overwrite".into()],
                    json!(row.enables_expansion()),
                );
            }
        });
        row
    }
    fn multi_deck_row(self: &Rc<Self>, page: &str, automatic: bool) -> adw::ActionRow {
        let row = adw::ActionRow::builder()
            .title(if automatic { "Decks" } else { "Default page" })
            .subtitle(if automatic {
                "Decks on which the page should be loaded"
            } else {
                "Select for which decks this page should be opened by default"
            })
            .activatable(true)
            .build();
        let selected = if automatic {
            self.shared.lock().unwrap().docs.pages[page]["settings"]["auto-change"]["decks"]
                .as_array()
                .cloned()
                .unwrap_or_default()
                .iter()
                .filter_map(|s| s.as_str().map(str::to_owned))
                .collect::<Vec<_>>()
        } else {
            self.shared.lock().unwrap().docs.settings["devices"]
                .as_object()
                .map(|devices| {
                    devices
                        .iter()
                        .filter(|(_, settings)| settings["page"].as_str() == Some(page))
                        .map(|(serial, _)| serial.clone())
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default()
        };
        let selected = Rc::new(RefCell::new(selected));
        let suffix = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        let count = gtk::Label::new(Some(&format!("{} selected", selected.borrow().len())));
        suffix.append(&count);
        suffix.append(&gtk::Image::from_icon_name("go-next-symbolic"));
        row.add_suffix(&suffix);
        let weak = Rc::downgrade(self);
        let page = page.to_owned();
        row.connect_activated(move |_| {
            if let Some(ui) = weak.upgrade() {
                let window = gtk::ApplicationWindow::builder()
                    .title("Select Decks")
                    .default_width(350)
                    .default_height(350)
                    .modal(true)
                    .build();
                let header = adw::HeaderBar::new();
                header.add_css_class("flat");
                window.set_titlebar(Some(&header));
                let body = gtk::Box::new(gtk::Orientation::Vertical, 0);
                let scroll = gtk::ScrolledWindow::builder()
                    .hexpand(true)
                    .vexpand(true)
                    .child(&body)
                    .build();
                margins(&scroll, 7);
                window.set_child(Some(&scroll));
                let devices = ui
                    .shared
                    .lock()
                    .unwrap()
                    .devices
                    .values()
                    .map(|device| (device.serial.clone(), format!("{:?}", device.kind)))
                    .collect::<Vec<_>>();
                for (serial, name) in devices {
                    let check = gtk::CheckButton::builder()
                        .label(&name)
                        .active(selected.borrow().contains(&serial))
                        .margin_bottom(3)
                        .build();
                    check.add_css_class("multi-deck-selector-label");
                    body.append(&check);
                    let weak = Rc::downgrade(&ui);
                    let selected = selected.clone();
                    let page = page.clone();
                    let count = count.downgrade();
                    check.connect_toggled(move |check| {
                        if let Some(ui) = weak.upgrade() {
                            selected.borrow_mut().retain(|s| s != &serial);
                            if check.is_active() {
                                selected.borrow_mut().push(serial.clone());
                            }
                            if automatic {
                                ui.page_property(
                                    page.clone(),
                                    vec!["settings".into(), "auto-change".into(), "decks".into()],
                                    json!(*selected.borrow()),
                                );
                            } else {
                                ui.settings_property(
                                    vec!["devices".into(), serial.clone(), "page".into()],
                                    if check.is_active() {
                                        json!(page)
                                    } else {
                                        Value::Null
                                    },
                                );
                            }
                            if let Some(count) = count.upgrade() {
                                count.set_text(&format!("{} selected", selected.borrow().len()));
                            }
                        }
                    });
                }
                ui.keep_window("Select Decks", &window);
            }
        });
        row
    }
    fn matching_windows(self: &Rc<Self>, expander: &adw::ExpanderRow, page: &str) {
        let document = self
            .shared
            .lock()
            .unwrap()
            .docs
            .pages
            .get(page)
            .cloned()
            .unwrap_or_default();
        let auto = document["settings"]["auto-change"].clone();
        expander.set_sensitive(false);
        let expander = expander.downgrade();
        let (send, receive) = async_channel::bounded(1);
        std::thread::spawn(move || {
            let rows = deckard_core::desktop::windows()
                .into_iter()
                .filter(|(class, title)| {
                    regex_matches(auto["title"].as_str().unwrap_or(".*"), title)
                        && regex_matches(auto["wm-class"].as_str().unwrap_or(".*"), class)
                })
                .collect::<Vec<_>>();
            let _ = send.send_blocking(rows);
        });
        glib::MainContext::default().spawn_local(async move {
            if let (Ok(rows), Some(expander)) = (receive.recv().await, expander.upgrade()) {
                fn collect(root: &gtk::Widget, rows: &mut Vec<adw::ActionRow>) {
                    if root.widget_name() == "matching-window"
                        && let Some(row) = root.downcast_ref::<adw::ActionRow>()
                    {
                        rows.push(row.clone());
                    }
                    let mut child = root.first_child();
                    while let Some(widget) = child {
                        child = widget.next_sibling();
                        collect(&widget, rows);
                    }
                }
                let mut old = Vec::new();
                collect(expander.upcast_ref(), &mut old);
                for row in old {
                    expander.remove(&row);
                }
                for (class, title) in rows {
                    let row = adw::ActionRow::builder()
                        .title(&title)
                        .subtitle(&class)
                        .use_markup(false)
                        .build();
                    row.set_widget_name("matching-window");
                    expander.add_row(&row);
                }
                expander.set_sensitive(true);
            }
        });
    }
    fn page_menu(self: &Rc<Self>, manager: &Rc<PageManager>, name: &str) {
        let chosen = manager.selected.borrow().clone();
        match name {
            "duplicate" => {
                if let Some(page) = chosen {
                    let weak = Rc::downgrade(self);
                    self.page_name_dialog(
                        &manager.window,
                        "Duplicate Page",
                        "Duplicate",
                        &format!("{page} copy"),
                        move |name| {
                            if let Some(ui) = weak.upgrade() {
                                ui.command(
                                    "duplicate-page",
                                    json!({"page":page, "name":name}),
                                    "Page saved",
                                    true,
                                );
                            }
                        },
                    );
                }
            }
            "export" | "export-all" => {
                let weak = Rc::downgrade(self);
                let all = name == "export-all";
                self.choose_file("Export Page", true, move |path| {
                    if let Some(ui) = weak.upgrade() {
                        ui.command(
                            if all { "export-all" } else { "export-page" },
                            json!({"page":chosen, "path":path}),
                            "Page exported",
                            false,
                        );
                    }
                });
            }
            _ => {
                let weak = Rc::downgrade(self);
                self.choose_file("Import Page", false, move |path| {
                    if let Some(ui) = weak.upgrade() {
                        if path.extension().is_some_and(|ext| ext == "json") {
                            match std::fs::read(&path)
                                .ok()
                                .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
                            {
                                Some(document) => {
                                    let name = path
                                        .file_stem()
                                        .unwrap_or_default()
                                        .to_string_lossy()
                                        .to_string();
                                    ui.command(
                                        "put-page",
                                        json!({"page":name, "document":document}),
                                        "Page imported",
                                        true,
                                    );
                                }
                                None => ui.toast("Invalid page document"),
                            }
                        } else {
                            let root = ui.shared.lock().unwrap().docs.root.clone();
                            ui.job("Page imported", true, move || {
                                deckard_core::store::install_archive(
                                    &std::fs::read(path)?,
                                    "page",
                                    &root,
                                )?;
                                Ok(Value::Null)
                            });
                        }
                    }
                });
            }
        }
    }
}
fn regex_valid(pattern: &str) -> bool {
    deckard_core::desktop::pattern_matches(pattern, "").is_ok()
}
fn regex_matches(pattern: &str, value: &str) -> bool {
    deckard_core::desktop::pattern_matches(pattern, value).unwrap_or(false)
}
