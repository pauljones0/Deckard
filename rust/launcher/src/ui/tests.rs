use super::*;

fn settle(ui: &Rc<Ui>, predicate: impl Fn() -> bool) {
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    loop {
        while glib::MainContext::default().pending() {
            glib::MainContext::default().iteration(false);
        }
        if ui.pending_count.get() == 0 && ui.pending.borrow().is_empty() && predicate() {
            break;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "GTK workflow timed out"
        );
        std::thread::sleep(Duration::from_millis(5));
    }
}
fn find(root: &impl IsA<gtk::Widget>, name: &str) -> Option<gtk::Widget> {
    if root.widget_name() == name {
        return Some(root.clone().upcast());
    }
    let mut child = root.first_child();
    while let Some(widget) = child {
        if let Some(found) = find(&widget, name) {
            return Some(found);
        }
        child = widget.next_sibling();
    }
    None
}

#[test]
fn state_edits_preserve_unknown_fields_and_target_the_captured_input() {
    let root = tempfile::tempdir().unwrap();
    let shared = deckard_core::engine::Engine::open(root.path().into()).unwrap();
    let selection = Selection {
        serial: "fake".into(),
        page: "Main".into(),
        family: "keys".into(),
        input: "0x0".into(),
        state: 0,
        sticky: false,
    };
    let mut engine = shared.lock().unwrap();
    engine.docs.put("Main",json!({"unknown-page":"keep","keys":{"0x0":{"states":{"0":{"unknown-state":"keep","labels":{"center":{"unknown-label":"keep"}}}}},"1x0":{"states":{"0":{}}}}})).unwrap();
    selection
        .edit(&mut engine, |draft| {
            set_nested(draft, &["labels", "center", "text"], json!("Native GTK"));
            Ok(())
        })
        .unwrap();
    assert_eq!(engine.docs.pages["Main"]["unknown-page"], "keep");
    let state = model::state(&engine.docs.pages["Main"], "keys", "0x0", 0);
    assert_eq!(state["unknown-state"], "keep");
    assert_eq!(state["labels"]["center"]["unknown-label"], "keep");
    assert_eq!(state["labels"]["center"]["text"], "Native GTK");
    assert!(model::state(&engine.docs.pages["Main"], "keys", "1x0", 0)["labels"].is_null());
    let selection = Selection {
        sticky: true,
        ..selection
    };
    selection
        .edit(&mut engine, |draft| {
            set_nested(draft, &["labels", "top", "text"], json!("Sticky"));
            Ok(())
        })
        .unwrap();
    assert_eq!(
        model::state(&engine.docs.sticky("fake").unwrap(), "keys", "0x0", 0)["labels"]["top"]["text"],
        "Sticky"
    );
}

#[test]
#[ignore = "requires GTK display: dbus-run-session -- xvfb-run -a cargo test -p deckard gtk_editor_workflows -- --ignored --test-threads=1"]
fn gtk_editor_workflows() {
    gtk::init().unwrap();
    adw::init().unwrap();
    adw::StyleManager::default().set_color_scheme(adw::ColorScheme::ForceDark);
    let css = gtk::CssProvider::new();
    css.load_from_string(concat!(
        include_str!("../../../../style.css"),
        include_str!("native.css")
    ));
    gtk::style_context_add_provider_for_display(
        &gdk::Display::default().unwrap(),
        &css,
        gtk::STYLE_PROVIDER_PRIORITY_APPLICATION,
    );
    let root = tempfile::tempdir().unwrap();
    let shared = deckard_core::engine::Engine::open(root.path().into()).unwrap();
    shared.lock().unwrap().docs.put("Main",json!({"unknown-page":"keep","keys":{"0x0":{"states":{"0":{"unknown-state":"keep","labels":{"center":{"text":"First","unknown-label":"keep"}}}}},"1x0":{"states":{"0":{"labels":{"center":{"text":"Second"}}}}}}})).unwrap();
    let runtime = deckard_core::engine::Runtime::start(
        shared.clone(),
        vec![
            deckard_core::engine::fake_kind("plus").unwrap(),
            deckard_core::engine::fake_kind("xl").unwrap(),
            deckard_core::engine::fake_kind("neo").unwrap(),
        ],
        false,
    )
    .unwrap();
    let app = adw::Application::builder()
        .application_id("io.github.nazbert.Deckard.EditorTests")
        .flags(gio::ApplicationFlags::NON_UNIQUE)
        .build();
    app.register(None::<&gio::Cancellable>).unwrap();
    let signal = Arc::new(AtomicBool::new(false));
    let ui = Ui::build(&app, shared.clone(), runtime.events.clone(), signal);
    settle(&ui, || shared.lock().unwrap().devices.len() == 3);
    ui.select_device("FAKE-PLUS-0");
    settle(&ui, || {
        ui.previews
            .borrow()
            .iter()
            .filter(|p| p.image.is_mapped() && p.family == "keys")
            .count()
            == 8
    });
    ui.refresh();
    settle(&ui, || ui.preview.paintable().is_some());
    assert_eq!(ui.split.sidebar_width_fraction(), 0.4);
    assert_eq!(ui.preview.width_request(), 175);
    settle(&ui, || {
        ui.previews
            .borrow()
            .iter()
            .filter(|p| p.serial == "FAKE-PLUS-0" && p.physical != 254)
            .all(|p| p.image.height() > 0)
    });
    for preview in ui
        .previews
        .borrow()
        .iter()
        .filter(|p| p.serial == "FAKE-PLUS-0" && p.physical != 254)
    {
        assert!(
            preview.image.paintable().is_some(),
            "every visible key/strip has a native frame"
        );
        assert_eq!(
            preview.image.height(),
            if preview.family == "keys" { 75 } else { 50 }
        );
    }
    find(&ui.deck_stack, "dial-FAKE-PLUS-0-2")
        .unwrap()
        .downcast::<gtk::Button>()
        .unwrap()
        .emit_clicked();
    settle(&ui, || {
        ui.selection.borrow().family == "dials" && ui.preview.paintable().is_some()
    });
    let dial = ui.preview.paintable().unwrap();
    assert_eq!(
        (dial.intrinsic_width(), dial.intrinsic_height()),
        (200, 100)
    );
    find(&ui.deck_stack, "key-FAKE-PLUS-0-0x0")
        .unwrap()
        .downcast::<gtk::Button>()
        .unwrap()
        .emit_clicked();
    settle(&ui, || ui.selection.borrow().family == "keys");
    let row = find(&ui.controls, "labels/center/text")
        .unwrap()
        .downcast::<adw::EntryRow>()
        .unwrap();
    row.set_text("Edited in Rust GTK");
    // Rapid key changes must not redirect queued edits to the new selection.
    find(&ui.deck_stack, "key-FAKE-PLUS-0-1x0")
        .unwrap()
        .downcast::<gtk::Button>()
        .unwrap()
        .emit_clicked();
    settle(&ui, || ui.selection.borrow().input == "1x0");
    let engine = shared.lock().unwrap();
    assert_eq!(
        model::state(&engine.docs.pages["Main"], "keys", "0x0", 0)["labels"]["center"]["text"],
        "Edited in Rust GTK"
    );
    assert_eq!(
        model::state(&engine.docs.pages["Main"], "keys", "1x0", 0)["labels"]["center"]["text"],
        "Second"
    );
    assert_eq!(engine.docs.pages["Main"]["unknown-page"], "keep");
    assert_eq!(
        model::state(&engine.docs.pages["Main"], "keys", "0x0", 0)["labels"]["center"]["unknown-label"],
        "keep"
    );
    drop(engine);
    find(&ui.state_box, "state-add")
        .unwrap()
        .downcast::<gtk::Button>()
        .unwrap()
        .emit_clicked();
    settle(&ui, || {
        shared.lock().unwrap().docs.pages["Main"]["keys"]["1x0"]["states"]
            .get("1")
            .is_some()
    });
    find(&ui.state_box, "state-1")
        .unwrap()
        .downcast::<gtk::ToggleButton>()
        .unwrap()
        .emit_clicked();
    settle(&ui, || ui.selection.borrow().state == 1);
    ui.choose_action(None);
    let mut list = Vec::new();
    fn walk(widget: &gtk::Widget, list: &mut Vec<adw::ActionRow>) {
        if let Some(row) = widget.downcast_ref::<adw::ActionRow>() {
            list.push(row.clone());
        }
        let mut child = widget.first_child();
        while let Some(w) = child {
            walk(&w, list);
            child = w.next_sibling();
        }
    }
    walk(&ui.sidebar.clone().upcast(), &mut list);
    let row = list
        .iter()
        .find(|r| {
            r.widget_name()
                .starts_with("native::OSPlugin-OpenInBrowser ")
        })
        .unwrap();
    row.emit_by_name::<()>("activated", &[]);
    settle(&ui, || {
        model::state(&shared.lock().unwrap().docs.pages["Main"], "keys", "1x0", 1)["actions"]
            .as_array()
            .is_some_and(|a| a.len() == 1)
    });
    ui.configure_action(0);
    assert_eq!(
        ui.sidebar.visible_child_name().as_deref(),
        Some("action-page")
    );
    ui.change_states(false);
    settle(&ui, || ui.selection.borrow().state == 0);
    assert!(
        shared.lock().unwrap().docs.pages["Main"]["keys"]["1x0"]["states"]
            .get("1")
            .is_none()
    );
    // Every device keeps its own geometry and frames when switching tabs.
    ui.select_device("FAKE-XL-1");
    ui.deck_stack.set_visible_child_name("FAKE-XL-1");
    settle(&ui, || {
        ui.previews
            .borrow()
            .iter()
            .filter(|p| p.image.is_mapped() && p.family == "keys")
            .count()
            == 32
    });
    ui.select_device("FAKE-NEO-2");
    ui.deck_stack.set_visible_child_name("FAKE-NEO-2");
    settle(&ui, || {
        ui.previews
            .borrow()
            .iter()
            .filter(|p| p.image.is_mapped() && p.family == "keys")
            .count()
            == 8
    });
    ui.select_device("FAKE-PLUS-0");
    ui.deck_stack.set_visible_child_name("FAKE-PLUS-0");
    settle(&ui, || ui.preview.paintable().is_some());
    // GTK dialogs and structural edits must update their lists without reopening.
    ui.settings();
    let dialog = ui.window.visible_dialog().unwrap();
    let root_widget = dialog.child().unwrap();
    for count in [1, 2] {
        find(&root_widget, "add-rule")
            .unwrap()
            .downcast::<adw::ActionRow>()
            .unwrap()
            .emit_by_name::<()>("activated", &[]);
        settle(&ui, || {
            shared.lock().unwrap().docs.settings["rules"]
                .as_array()
                .is_some_and(|r| r.len() == count)
        });
        assert!(find(&root_widget, &format!("remove-rule-{}", count - 1)).is_some());
    }
    find(&root_widget, "remove-rule-0")
        .unwrap()
        .downcast::<adw::ActionRow>()
        .unwrap()
        .emit_by_name::<()>("activated", &[]);
    settle(&ui, || {
        shared.lock().unwrap().docs.settings["rules"]
            .as_array()
            .is_some_and(|r| r.len() == 1)
    });
    assert!(find(&root_widget, "remove-rule-1").is_none());
    dialog.close();
    settle(&ui, || ui.window.visible_dialog().is_none());
    ui.obs_profiles();
    let dialog = ui.window.visible_dialog().unwrap();
    let root_widget = dialog.child().unwrap();
    find(&root_widget, "Save as new")
        .unwrap()
        .downcast::<gtk::Button>()
        .unwrap()
        .emit_clicked();
    settle(&ui, || {
        shared.lock().unwrap().docs.settings["obs"]["connections"]
            .as_object()
            .is_some_and(|p| p.len() == 1)
    });
    let combo = find(&root_widget, "obs-profile")
        .unwrap()
        .downcast::<adw::ComboRow>()
        .unwrap();
    assert!(
        combo
            .selected_item()
            .and_downcast::<gtk::StringObject>()
            .unwrap()
            .string()
            .starts_with("profile-")
    );
    find(&root_widget, "Delete")
        .unwrap()
        .downcast::<gtk::Button>()
        .unwrap()
        .emit_clicked();
    settle(&ui, || {
        shared.lock().unwrap().docs.settings["obs"]["connections"]
            .as_object()
            .is_some_and(|p| p.is_empty())
    });
    assert_eq!(
        combo
            .selected_item()
            .and_downcast::<gtk::StringObject>()
            .unwrap()
            .string(),
        "default"
    );
    dialog.close();
    settle(&ui, || ui.window.visible_dialog().is_none());
    shared
        .lock()
        .unwrap()
        .docs
        .edit("Main", |document| {
            set_nested(
                document,
                &["background", "media-paths"],
                json!(["first.png", "second.png"]),
            );
            Ok(())
        })
        .unwrap();
    ui.page_settings();
    let dialog = ui.window.visible_dialog().unwrap();
    let root_widget = dialog.child().unwrap();
    find(&root_widget, "remove-slide-0")
        .unwrap()
        .downcast::<gtk::Button>()
        .unwrap()
        .emit_clicked();
    settle(&ui, || {
        shared.lock().unwrap().docs.pages["Main"]["background"]["media-paths"]
            == json!(["second.png"])
    });
    assert!(find(&root_widget, "remove-slide-0").is_some());
    assert!(find(&root_widget, "remove-slide-1").is_none());
    dialog.close();
    settle(&ui, || ui.window.visible_dialog().is_none());
    ui.assets();
    ui.window.visible_dialog().unwrap().close();
    settle(&ui, || ui.window.visible_dialog().is_none());
    ui.device_settings();
    ui.window.visible_dialog().unwrap().close();
    settle(&ui, || ui.window.visible_dialog().is_none());
    ui.store();
    ui.show_catalog(json!({"plugins":[],"pages":[],"icons":[]}));
    ui.window.visible_dialog().unwrap().close();
    settle(&ui, || ui.catalog_dialog.borrow().is_none());
    ui.window.close();
    settle(&ui, || !ui.window.is_visible());
    assert!(ui.bridge.hidden.load(Ordering::Relaxed));
    assert!(ui.preview.paintable().is_none());
    assert!(
        ui.previews
            .borrow()
            .iter()
            .all(|p| p.image.paintable().is_none())
    );
    shared.lock().unwrap().show_window = true;
    settle(&ui, || {
        ui.window.is_visible() && ui.preview.paintable().is_some()
    });
    let maps = std::fs::read_to_string("/proc/self/maps").unwrap();
    assert!(maps.contains("libgtk-4.so"));
    assert!(maps.contains("libadwaita-1.so"));
    assert!(!maps.contains("libpython"));
    let bytes = std::fs::read(root.path().join("pages/Main.json")).unwrap();
    let saved: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(saved["unknown-page"], "keep");
    ui.window.destroy();
    drop(ui);
    drop(runtime);
}
