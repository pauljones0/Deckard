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
}
