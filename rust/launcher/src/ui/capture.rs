//! Opt-in deterministic captures for comparing both implementations under GTK.
#![allow(deprecated)]
use super::*;
fn walk(widget: &gtk::Widget, apply: &impl Fn(&gtk::Widget)) {
    apply(widget);
    let mut child = widget.first_child();
    while let Some(widget) = child {
        child = widget.next_sibling();
        walk(&widget, apply);
    }
}
impl Ui {
    pub(super) fn capture_scene(self: &Rc<Self>) {
        match std::env::var("DECKARD_UI_CAPTURE_CASE")
            .unwrap_or_default()
            .as_str()
        {
            case if case.starts_with("settings") => {
                self.settings();
                if let Some((_, title)) = case.split_once('-')
                    && let Some(window) = self.auxiliary_windows.borrow()["Settings"]
                        .downcast_ref::<adw::PreferencesWindow>()
                {
                    let name = if title == "ui" {
                        "UI".into()
                    } else {
                        format!("{}{}", title[..1].to_uppercase(), &title[1..])
                    };
                    window.set_visible_page_name(&name);
                }
            }
            case if case.starts_with("assets") => {
                self.asset_manager(Rc::new(|_| {}));
                if let Some((_, name)) = case.split_once('-') {
                    let window = self.auxiliary_windows.borrow()["Asset Manager"].clone();
                    walk(window.upcast_ref(), &|w| {
                        if let Some(stack) = w.downcast_ref::<gtk::Stack>()
                            && stack.child_by_name(name).is_some()
                        {
                            stack.set_visible_child_name(name);
                        }
                    });
                }
            }
            "pages" => {
                self.page_settings();
            }
            "deck-settings" => self.device_settings(),
            "chooser" | "chooser-populated" => self.choose_action(None),
            "action" => self.configure_action(0),
            "dial" => {
                let serial = self.selection.borrow().serial.clone();
                self.select_input(&serial, "dials", "0");
            }
            "touchscreen" => {
                let serial = self.selection.borrow().serial.clone();
                self.select_input(&serial, "touchscreens", "0");
            }
            "page-selector" => self.page_button.popup(),
            "labels" => walk(self.controls.upcast_ref(), &|widget| {
                if let Some(row) = widget.downcast_ref::<adw::ExpanderRow>()
                    && ["Labels", "Layout", "Background"].contains(&row.title().as_str())
                {
                    row.set_expanded(true);
                }
            }),
            "labels-detail" => {
                walk(self.controls.upcast_ref(), &|widget| {
                    if let Some(row) = widget.downcast_ref::<adw::ExpanderRow>()
                        && row.title() == "Labels"
                    {
                        row.set_expanded(true);
                    }
                });
                let scroll = self.editor_scroll.downgrade();
                glib::timeout_add_local_once(Duration::from_millis(250), move || {
                    if let Some(scroll) = scroll.upgrade() {
                        scroll.vadjustment().set_value(400.);
                    }
                });
            }
            _ => {}
        }
    }
    pub(super) fn capture(&self) {
        let Some(path) = std::env::var_os("DECKARD_UI_CAPTURE") else {
            return;
        };
        if self.capture_done.get() || self.window.width() == 0 {
            return;
        }
        let case = std::env::var("DECKARD_UI_CAPTURE_CASE").unwrap_or_default();
        let window = self
            .auxiliary_windows
            .borrow()
            .get(match case.as_str() {
                case if case.starts_with("settings") => "Settings",
                case if case.starts_with("assets") => "Asset Manager",
                "pages" => "Page Manager",
                _ => "",
            })
            .cloned()
            .unwrap_or_else(|| self.window.clone().upcast());
        let paintable = gtk::WidgetPaintable::new(Some(&window));
        let snapshot = gtk::Snapshot::new();
        paintable.snapshot(&snapshot, window.width() as f64, window.height() as f64);
        let node = snapshot.to_node().or_else(|| {
            let child = window.first_child()?;
            let snapshot = gtk::Snapshot::new();
            gtk::WidgetPaintable::new(Some(&child)).snapshot(
                &snapshot,
                child.width() as f64,
                child.height() as f64,
            );
            snapshot.to_node()
        });
        if let (Some(node), Some(renderer)) = (node, window.renderer()) {
            match renderer
                .render_texture(&node, None)
                .save_to_png(PathBuf::from(&path))
            {
                Ok(()) => self.capture_done.set(true),
                Err(e) => eprintln!("Editor screenshot: {e}"),
            }
        }
        let mut rows = Vec::new();
        fn dump(widget: &gtk::Widget, root: &gtk::Widget, rows: &mut Vec<Value>) {
            if widget.is_mapped() {
                let bounds = widget.compute_bounds(root);
                let title = widget
                    .downcast_ref::<gtk::Label>()
                    .map(|w| w.label().to_string())
                    .or_else(|| {
                        widget
                            .downcast_ref::<adw::PreferencesRow>()
                            .map(|w| w.title().to_string())
                    })
                    .or_else(|| {
                        widget
                            .downcast_ref::<gtk::Button>()
                            .and_then(|w| w.label().map(|s| s.to_string()))
                    });
                rows.push(json!({"type":widget.type_().name(),"title":title,"css":widget.css_classes().iter().map(|s|s.as_str()).collect::<Vec<_>>(),"bounds":bounds.map(|r| [r.x(),r.y(),r.width(),r.height()])}));
            }
            let mut child = widget.first_child();
            while let Some(widget) = child {
                child = widget.next_sibling();
                dump(&widget, root, rows);
            }
        }
        dump(window.upcast_ref(), window.upcast_ref(), &mut rows);
        let _ = std::fs::write(
            PathBuf::from(path).with_extension("json"),
            serde_json::to_vec_pretty(&rows).unwrap(),
        );
    }
}
