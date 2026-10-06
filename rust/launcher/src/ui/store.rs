use super::*;
use deckard_core::store::{Entry, Network};

impl Ui {
    pub(super) fn store(self: &Rc<Self>) {
        if let Some(window) = self.catalog_dialog.borrow().as_ref() {
            window.present();
            return;
        }
        let window = gtk::ApplicationWindow::builder()
            .title("Store")
            .default_width(1050)
            .default_height(750)
            .modal(true)
            .build();
        let header = gtk::HeaderBar::new();
        header.add_css_class("flat");
        window.set_titlebar(Some(&header));
        let body = gtk::Box::new(gtk::Orientation::Vertical, 0);
        let stack = gtk::Stack::builder()
            .hexpand(true)
            .vexpand(true)
            .transition_type(gtk::StackTransitionType::SlideLeftRight)
            .build();
        let switcher = gtk::StackSwitcher::builder().stack(&stack).build();
        header.set_title_widget(Some(&switcher));
        let back = gtk::Button::from_icon_name("go-previous-symbolic");
        back.set_visible(false);
        header.pack_start(&back);
        let weak_stack = stack.downgrade();
        back.connect_clicked(move |back| {
            if let Some(stack) = weak_stack.upgrade()
                && let Some(page) = stack.visible_child().and_downcast::<gtk::Stack>()
            {
                page.set_visible_child_name("Store");
            }
            back.set_visible(false);
        });
        back.set_widget_name("store-back");
        for (kind, title) in [
            ("plugin", "Plugins"),
            ("icons", "Icons"),
            ("wallpaper", "Wallpapers"),
            ("sdplusbar", "SD Plus Bar Wallpapers"),
        ] {
            let page = gtk::Stack::builder().hexpand(true).vexpand(true).build();
            let status = adw::StatusPage::builder()
                .title("Nothing here")
                .icon_name("face-sad-symbolic")
                .build();
            page.add_named(&status, Some("Store"));
            stack.add_titled(&page, Some(kind), title);
        }
        body.append(&stack);
        window.set_child(Some(&body));
        *self.catalog_box.borrow_mut() = Some(body);
        *self.catalog_dialog.borrow_mut() = Some(window.clone().upcast());
        let weak = Rc::downgrade(self);
        window.connect_close_request(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.catalog_dialog.borrow_mut().take();
                ui.catalog_box.borrow_mut().take();
            }
            glib::Propagation::Proceed
        });
        self.keep_window("Store", &window);
        let url = format!(
            "https://github.com/pauljones0/Deckard/releases/latest/download/native-store-{}{}.json",
            option_env!("DECKARD_PACKAGE_TARGET")
                .map(|target| format!("{target}-"))
                .unwrap_or_default(),
            std::env::consts::ARCH
        );
        self.job("catalog", false, move || {
            Ok(serde_json::to_value(Network::new()?.catalog(&url)?)?)
        });
    }
    pub(super) fn show_catalog(self: &Rc<Self>, value: Value) {
        let Some(container) = self.catalog_box.borrow().clone() else {
            return;
        };
        let Some(stack) = container.first_child().and_downcast::<gtk::Stack>() else {
            return;
        };
        let entries = match serde_json::from_value::<Vec<Entry>>(value) {
            Ok(value) => value,
            Err(e) => {
                self.toast(&e.to_string());
                return;
            }
        };
        for kind in ["plugin", "icons", "wallpaper", "sdplusbar"] {
            let Some(page) = stack.child_by_name(kind).and_downcast::<gtk::Stack>() else {
                continue;
            };
            if let Some(old) = page.child_by_name("Store") {
                page.remove(&old);
            }
            let items = entries
                .iter()
                .filter(|entry| entry.kind == kind || kind == "plugin" && entry.kind == "page")
                .collect::<Vec<_>>();
            if items.is_empty() {
                let status = adw::StatusPage::builder()
                    .title("Nothing here")
                    .icon_name("face-sad-symbolic")
                    .build();
                page.add_named(&status, Some("Store"));
                continue;
            }
            let main = gtk::Box::new(gtk::Orientation::Vertical, 0);
            let search = gtk::SearchEntry::builder()
                .placeholder_text("Search")
                .hexpand(true)
                .margin_bottom(15)
                .build();
            main.append(&search);
            let flow = gtk::FlowBox::builder()
                .homogeneous(true)
                .selection_mode(gtk::SelectionMode::None)
                .build();
            let content = gtk::Box::new(gtk::Orientation::Vertical, 0);
            content.append(&flow);
            content.append(&gtk::Box::builder().hexpand(true).vexpand(true).build());
            let scroll = gtk::ScrolledWindow::builder()
                .hexpand(true)
                .vexpand(true)
                .child(&content)
                .build();
            main.append(&scroll);
            for entry in items {
                flow.append(&self.store_card(entry, &page));
            }
            let query = search.clone();
            flow.set_filter_func(move |child| {
                child
                    .widget_name()
                    .to_lowercase()
                    .contains(&query.text().to_lowercase())
            });
            let weak = flow.downgrade();
            search.connect_search_changed(move |_| {
                if let Some(flow) = weak.upgrade() {
                    flow.invalidate_filter();
                }
            });
            page.add_named(&main, Some("Store"));
            page.set_visible_child_name("Store");
        }
    }
    fn store_card(self: &Rc<Self>, entry: &Entry, page: &gtk::Stack) -> gtk::FlowBoxChild {
        let tile = gtk::FlowBoxChild::new();
        tile.set_widget_name(&format!("{} {}", entry.name, entry.description));
        let main = gtk::Box::builder()
            .orientation(gtk::Orientation::Vertical)
            .hexpand(true)
            .width_request(250)
            .height_request(250)
            .build();
        main.add_css_class("no-padding");
        tile.set_child(Some(&main));
        let button = gtk::Button::builder()
            .hexpand(true)
            .width_request(250)
            .height_request(275)
            .build();
        for class in ["no-padding", "no-round-bottom"] {
            button.add_css_class(class);
        }
        main.append(&button);
        let content = gtk::Box::new(gtk::Orientation::Vertical, 0);
        let image = gtk::Picture::builder()
            .hexpand(true)
            .vexpand(true)
            .content_fit(gtk::ContentFit::Cover)
            .width_request(250)
            .height_request(90)
            .can_shrink(true)
            .build();
        image.add_css_class("plugin-store-image");
        content.append(&image);
        let labels = gtk::Box::builder()
            .orientation(gtk::Orientation::Vertical)
            .margin_top(6)
            .margin_start(6)
            .margin_bottom(6)
            .build();
        let name = gtk::Label::builder().label(&entry.name).xalign(0.).build();
        name.add_css_class("bold");
        labels.append(&name);
        labels.append(
            &gtk::Label::builder()
                .label("Deckard contributors")
                .sensitive(false)
                .xalign(0.)
                .build(),
        );
        let description = gtk::Label::builder()
            .label(&entry.description)
            .halign(gtk::Align::Start)
            .margin_bottom(6)
            .build();
        description.add_css_class("dim-label");
        labels.append(&description);
        content.append(&labels);
        content.append(&gtk::Separator::new(gtk::Orientation::Horizontal));
        button.set_child(Some(&content));
        let footer = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        main.append(&footer);
        let weak = Rc::downgrade(self);
        let url = entry.source.clone();
        let source = controls::button("", "web-browser-symbolic", move || {
            if let Some(ui) = weak.upgrade()
                && let Err(error) =
                    gio::AppInfo::launch_default_for_uri(&url, None::<&gio::AppLaunchContext>)
            {
                ui.toast(&error.to_string());
            }
        });
        source.set_hexpand(true);
        for class in [
            "no-round-top-left",
            "no-round-top-right",
            "no-round-bottom-right",
        ] {
            source.add_css_class(class);
        }
        footer.append(&source);
        footer.append(&gtk::Separator::new(gtk::Orientation::Vertical));
        let weak = Rc::downgrade(self);
        let package = entry.clone();
        let install = controls::button("", "folder-download-symbolic", move || {
            if let Some(ui) = weak.upgrade() {
                ui.install_entry(package.clone());
            }
        });
        install.set_hexpand(true);
        for class in [
            "no-round-top-left",
            "no-round-top-right",
            "no-round-bottom-left",
        ] {
            install.add_css_class(class);
        }
        footer.append(&install);
        let weak = Rc::downgrade(self);
        let package = entry.clone();
        let page = page.downgrade();
        button.connect_clicked(move |_| {
            if let (Some(ui), Some(page)) = (weak.upgrade(), page.upgrade()) {
                ui.store_info(&page, &package);
            }
        });
        tile
    }
    fn install_entry(self: &Rc<Self>, entry: Entry) {
        let root = self.shared.lock().unwrap().docs.root.clone();
        self.job("Package installed", true, move || {
            Network::new()?.install(&entry, &root)?;
            Ok(Value::Null)
        });
    }
    fn store_info(self: &Rc<Self>, page: &gtk::Stack, entry: &Entry) {
        if let Some(old) = page.child_by_name("Info") {
            page.remove(&old);
        }
        let clamp = adw::Clamp::new();
        let content = gtk::Box::new(gtk::Orientation::Vertical, 0);
        let about = adw::PreferencesGroup::builder().title("About").build();
        for (title, value) in [
            ("Name:", entry.name.as_str()),
            ("Author:", "Deckard contributors"),
            ("Source:", entry.source.as_str()),
            ("Version:", entry.branch.as_str()),
            ("Description:", entry.description.as_str()),
        ] {
            about.add(
                &adw::ActionRow::builder()
                    .title(title)
                    .subtitle(value)
                    .build(),
            );
        }
        content.append(&about);
        let legal = adw::PreferencesGroup::builder().title("Legal").build();
        for title in [
            "License:",
            "Copyright:",
            "Original URL:",
            "License Description:",
        ] {
            legal.add(
                &adw::ActionRow::builder()
                    .title(title)
                    .subtitle("N/A")
                    .build(),
            );
        }
        content.append(&legal);
        clamp.set_child(Some(&content));
        page.add_named(&clamp, Some("Info"));
        page.set_visible_child_name("Info");
        if let Some(window) = self.catalog_dialog.borrow().as_ref() {
            fn show_back(widget: &gtk::Widget) {
                if widget.widget_name() == "store-back" {
                    widget.set_visible(true);
                }
                let mut child = widget.first_child();
                while let Some(next) = child {
                    show_back(&next);
                    child = next.next_sibling();
                }
            }
            show_back(window.upcast_ref());
        }
    }
}
