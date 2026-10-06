use super::*;
use controls::*;
use deckard_core::store::{Entry, Network};

impl Ui {
    pub(super) fn store(self: &Rc<Self>) {
        if let Some(dialog) = self.catalog_dialog.borrow().as_ref() {
            dialog.present(Some(&self.window));
            return;
        }
        let (dialog, body) = self.dialog("Store");
        let source = adw::PreferencesGroup::new();
        source.set_title("Native plugins, pages and icons");
        body.append(&source);
        let url = format!(
            "https://github.com/pauljones0/Deckard/releases/latest/download/native-store-{}{}.json",
            option_env!("DECKARD_PACKAGE_TARGET")
                .map(|target| format!("{target}-"))
                .unwrap_or_default(),
            std::env::consts::ARCH
        );
        let entry = adw::EntryRow::builder()
            .title("Catalog URL")
            .text(&url)
            .build();
        source.add(&entry);
        let weak = Rc::downgrade(self);
        body.append(&button(
            "Refresh catalog",
            "view-refresh-symbolic",
            move || {
                if let Some(ui) = weak.upgrade() {
                    let url = entry.text().to_string();
                    ui.job("catalog", false, move || {
                        Ok(serde_json::to_value(Network::new()?.catalog(&url)?)?)
                    });
                }
            },
        ));
        let catalog = gtk::Box::new(gtk::Orientation::Vertical, 12);
        body.append(&catalog);
        let local = adw::PreferencesGroup::new();
        local.set_title("Local packages");
        body.append(&local);
        for (kind, title) in [
            ("plugin", "Install native plugin ZIP"),
            ("page", "Import page bundle"),
            ("icons", "Install icon pack ZIP"),
        ] {
            let row = adw::ActionRow::builder()
                .title(title)
                .activatable(true)
                .build();
            let weak = Rc::downgrade(self);
            row.connect_activated(move |_| {
                if let Some(ui) = weak.upgrade() {
                    ui.install_local(kind);
                }
            });
            local.add(&row);
        }
        let installed = adw::PreferencesGroup::new();
        installed.set_title("Installed native plugins");
        body.append(&installed);
        for plugin in &self.shared.lock().unwrap().plugins {
            installed.add(
                &adw::ActionRow::builder()
                    .title(&plugin.manifest.name)
                    .subtitle(format!(
                        "{} · {}",
                        plugin.manifest.id, plugin.manifest.version
                    ))
                    .build(),
            );
        }
        let weak = Rc::downgrade(self);
        dialog.connect_closed(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.catalog_dialog.borrow_mut().take();
                ui.catalog_box.borrow_mut().take();
            }
        });
        *self.catalog_box.borrow_mut() = Some(catalog);
        *self.catalog_dialog.borrow_mut() = Some(dialog.clone());
        dialog.present(Some(&self.window));
        self.job("catalog", false, move || {
            Ok(serde_json::to_value(Network::new()?.catalog(&url)?)?)
        });
    }
    pub(super) fn show_catalog(self: &Rc<Self>, value: Value) {
        let Some(container) = self.catalog_box.borrow().clone() else {
            return;
        };
        let entries = match serde_json::from_value::<Vec<Entry>>(value) {
            Ok(value) => value,
            Err(e) => {
                self.toast(&e.to_string());
                return;
            }
        };
        clear(&container);
        let stack = gtk::Stack::new();
        let switcher = gtk::StackSwitcher::builder()
            .stack(&stack)
            .halign(gtk::Align::Center)
            .build();
        container.append(&switcher);
        container.append(&stack);
        for (kind, title) in [("plugin", "Plugins"), ("page", "Pages"), ("icons", "Icons")] {
            let list = gtk::Box::new(gtk::Orientation::Vertical, 12);
            for entry in entries.iter().filter(|entry| entry.kind == kind) {
                let group = adw::PreferencesGroup::new();
                let row = adw::ActionRow::builder()
                    .title(&entry.name)
                    .subtitle(&entry.description)
                    .build();
                let install_entry = entry.clone();
                let weak = Rc::downgrade(self);
                let install = button("Install", "folder-download-symbolic", move || {
                    if let Some(ui) = weak.upgrade() {
                        let root = ui.shared.lock().unwrap().docs.root.clone();
                        let entry = install_entry.clone();
                        ui.job("Package installed", true, move || {
                            Network::new()?.install(&entry, &root)?;
                            Ok(Value::Null)
                        });
                    }
                });
                install.set_valign(gtk::Align::Center);
                row.add_suffix(&install);
                group.add(&row);
                if !entry.branch.is_empty() {
                    group.add(
                        &adw::ActionRow::builder()
                            .title("Branch")
                            .subtitle(&entry.branch)
                            .build(),
                    );
                }
                if entry.source.starts_with("https://") {
                    let link = gtk::LinkButton::with_label(&entry.source, "Source");
                    group.set_header_suffix(Some(&link));
                }
                list.append(&group);
            }
            stack.add_titled(&list, Some(kind), title);
        }
    }
    pub(super) fn install_local(self: &Rc<Self>, kind: &str) {
        let weak = Rc::downgrade(self);
        let kind = kind.to_owned();
        let root = self.shared.lock().unwrap().docs.root.clone();
        self.choose_file("Install native package", false, move |path| {
            if let Some(ui) = weak.upgrade() {
                let root = root.clone();
                let kind = kind.clone();
                ui.job("Package installed", true, move || {
                    deckard_core::store::install_archive(&std::fs::read(path)?, &kind, &root)?;
                    Ok(Value::Null)
                });
            }
        });
    }
}
