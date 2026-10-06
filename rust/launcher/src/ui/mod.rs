//! Native GTK/libadwaita editor. The engine and plugin API remain toolkit independent.
mod actions;
mod assets;
mod bridge;
mod capture;
mod configuration;
mod controls;
mod dialogs;
mod editor;
mod pages;
mod preferences;
mod preview;
mod search;
mod settings;
mod store;
#[cfg(test)]
mod tests;
mod upstream_controls;
mod viewport;

use crate::editor_model::{action_definitions, set_nested};
use adw::prelude::*;
use anyhow::{Context, Result};
use bridge::{Bridge, Work};
use deckard_core::{
    engine::{InputEvent, Shared},
    model, render,
};
use gtk::{gdk, gio, glib};
use preview::PreviewImage;
use serde_json::{Value, json};
use std::{
    cell::{Cell, RefCell},
    collections::{HashMap, VecDeque},
    path::PathBuf,
    rc::Rc,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc::SyncSender,
    },
    time::Duration,
};

#[derive(Clone, Debug, PartialEq, Eq)]
struct Selection {
    serial: String,
    page: String,
    family: String,
    input: String,
    state: usize,
    sticky: bool,
}
impl Selection {
    fn params(&self) -> Value {
        json!({"serial":self.serial,"page":self.page,"family":self.family,"input":self.input,"state":self.state})
    }
    fn document(&self, engine: &deckard_core::engine::Engine) -> Value {
        if self.sticky {
            engine
                .docs
                .sticky(&self.serial)
                .unwrap_or_else(|_| json!({}))
        } else {
            engine
                .docs
                .pages
                .get(&self.page)
                .cloned()
                .unwrap_or_else(|| json!({}))
        }
    }
    fn edit(
        &self,
        engine: &mut deckard_core::engine::Engine,
        edit: impl FnOnce(&mut Value) -> Result<()>,
    ) -> Result<Value> {
        if self.sticky {
            let mut document = engine.docs.sticky(&self.serial)?;
            edit(model::state_mut(
                &mut document,
                &self.family,
                &self.input,
                self.state,
            )?)?;
            engine.docs.put_sticky(&self.serial, &document)?;
            Ok(Value::Null)
        } else {
            engine.docs.edit(&self.page, |page| {
                edit(model::state_mut(
                    page,
                    &self.family,
                    &self.input,
                    self.state,
                )?)
            })?;
            Ok(Value::Null)
        }
    }
}

type Completion = Box<dyn FnOnce(&Rc<Ui>, &Result<Value>)>;

type DeckSignature = (String, u64, u16, String, bool);

struct Preview {
    serial: String,
    physical: u8,
    input: String,
    family: String,
    frame: gtk::Frame,
    image: PreviewImage,
    identity: Cell<Option<u64>>,
}

struct Ui {
    shared: Shared,
    events: SyncSender<InputEvent>,
    signal: Arc<AtomicBool>,
    bridge: Bridge,
    pending: RefCell<VecDeque<Work>>,
    pending_count: Cell<usize>,
    completions: RefCell<HashMap<String, Completion>>,
    next_completion: Cell<u64>,
    force_reload: Cell<bool>,
    window: adw::ApplicationWindow,
    overlay: adw::ToastOverlay,
    split: adw::NavigationSplitView,
    sidebar: gtk::Stack,
    controls: gtk::Box,
    deck_stack: gtk::Stack,
    content_stack: gtk::Stack,
    deck_titles: gtk::Stack,
    main_menu: gtk::MenuButton,
    fps_banners: RefCell<HashMap<String, (adw::Banner, Cell<bool>)>>,
    page_button: gtk::MenuButton,
    page_label: gtk::Label,
    remove_state: gtk::Button,
    screen_title: gtk::Label,
    editor_scroll: gtk::ScrolledWindow,
    editor_body: gtk::Box,
    screen_clamp: adw::Clamp,
    remove_icon: gtk::Button,
    deck_settings_visible: Cell<bool>,
    deck_settings: gtk::Button,
    preview: PreviewImage,
    preview_host: gtk::Overlay,
    dial_preview: Cell<Option<(usize, u64)>>,
    state_box: gtk::Box,
    selection: RefCell<Selection>,
    loaded_selection: RefCell<Option<Selection>>,
    draft: RefCell<Value>,
    previews: RefCell<Vec<Preview>>,
    deck_signature: RefCell<Vec<DeckSignature>>,
    loading_shell: Cell<bool>,
    loaded_revision: Cell<u64>,
    expanded: RefCell<HashMap<String, bool>>,
    page_names: RefCell<Vec<String>>,
    definition_revision: Cell<u64>,
    definitions: RefCell<Vec<(String, String, Vec<deckard_core::plugin::Field>)>>,
    choices: RefCell<Value>,
    players: RefCell<Value>,
    player_refreshed: Cell<std::time::Instant>,
    catalog_dialog: RefCell<Option<gtk::Window>>,
    auxiliary_windows: RefCell<HashMap<String, gtk::Window>>,
    page_manager: RefCell<Option<Rc<pages::PageManager>>>,
    catalog_box: RefCell<Option<gtk::Box>>,
    capture_done: Cell<bool>,
}

pub fn run(shared: Shared, events: SyncSender<InputEvent>, signal: Arc<AtomicBool>) -> Result<()> {
    gtk::init().context("Cannot open the GTK display; check DISPLAY or WAYLAND_DISPLAY")?;
    adw::init()?;
    let settings = shared.lock().unwrap().docs.settings.clone();
    let scheme = settings["appearance"].as_str().unwrap_or("dark").to_owned();
    adw::StyleManager::default().set_color_scheme(match scheme.as_str() {
        "light" => adw::ColorScheme::ForceLight,
        "system" => adw::ColorScheme::Default,
        _ if settings["ui"]["allow-white-mode"] == true => adw::ColorScheme::Default,
        _ => adw::ColorScheme::ForceDark,
    });
    eprintln!("Deckard editor: GTK 4 / libadwaita · Rust");
    let provider = gtk::CssProvider::new();
    let icon_theme = gtk::IconTheme::for_display(&gdk::Display::default().context("No display")?);
    for path in [
        "/usr/share/deckard/icons",
        concat!(env!("CARGO_MANIFEST_DIR"), "/../../Assets/icons"),
    ] {
        if std::path::Path::new(path).is_dir() {
            icon_theme.add_search_path(path);
        }
    }
    provider.load_from_string(concat!(
        include_str!("../../../../style.css"),
        include_str!("native.css")
    ));
    gtk::style_context_add_provider_for_display(
        &gdk::Display::default().context("No display")?,
        &provider,
        gtk::STYLE_PROVIDER_PRIORITY_APPLICATION,
    );
    let app = adw::Application::builder()
        .application_id("io.github.nazbert.Deckard")
        .flags(gio::ApplicationFlags::NON_UNIQUE)
        .build();
    let active = Rc::new(RefCell::new(None::<Rc<Ui>>));
    let hold = active.clone();
    app.connect_activate(move |app| {
        if let Some(ui) = hold.borrow().as_ref() {
            ui.window.present();
            return;
        }
        let ui = Ui::build(app, shared.clone(), events.clone(), signal.clone());
        *hold.borrow_mut() = Some(ui);
    });
    let code = app.run_with_args::<&str>(&[]);
    active.borrow_mut().take();
    anyhow::ensure!(code == glib::ExitCode::SUCCESS, "GTK application failed");
    Ok(())
}

impl Ui {
    #[allow(deprecated)]
    fn build(
        app: &adw::Application,
        shared: Shared,
        events: SyncSender<InputEvent>,
        signal: Arc<AtomicBool>,
    ) -> Rc<Self> {
        let window = adw::ApplicationWindow::builder()
            .application(app)
            .title("Deckard")
            .default_width(1400)
            .default_height(900)
            .width_request(800)
            .height_request(700)
            .build();
        let overlay = adw::ToastOverlay::new();
        let split = adw::NavigationSplitView::builder()
            .sidebar_width_fraction(0.4)
            .min_sidebar_width(450.)
            .max_sidebar_width(600.)
            .build();
        overlay.set_child(Some(&split));
        window.set_content(Some(&overlay));
        let content = gtk::Box::new(gtk::Orientation::Vertical, 0);
        let header = adw::HeaderBar::builder().show_back_button(false).build();
        header.add_css_class("flat");
        let deck_stack = gtk::Stack::builder().hexpand(true).vexpand(true).build();
        let switcher = gtk::StackSwitcher::builder().stack(&deck_stack).build();
        switcher.set_margin_start(75);
        switcher.set_margin_end(75);
        let deck_titles = gtk::Stack::builder().hhomogeneous(false).build();
        deck_titles.add_named(&switcher, Some("decks"));
        let no_decks_title = gtk::Label::new(Some("No Decks Detected"));
        no_decks_title.add_css_class("bold");
        deck_titles.add_named(&no_decks_title, Some("empty"));
        header.set_title_widget(Some(&deck_titles));
        let deck_settings =
            controls::button("Deck Settings", "drive-removable-media-symbolic", || {});
        header.pack_start(&deck_settings);
        if let Some(box_) = deck_settings.child().and_downcast::<gtk::Box>() {
            box_.set_spacing(10);
        }
        let menu = gtk::MenuButton::builder()
            .icon_name("open-menu-symbolic")
            .build();
        let model = gio::Menu::new();
        for (label, action) in [
            ("Open Store", "win.store"),
            ("Open Settings", "win.settings"),
            ("Quit", "win.quit"),
            ("About", "win.about"),
            ("Support Core447 (original author)", "win.support"),
        ] {
            model.append(Some(label), Some(action));
        }
        menu.set_menu_model(Some(&model));
        header.pack_end(&menu);
        content.append(&header);
        deck_stack.set_margin_end(3);
        deck_stack.set_margin_bottom(10);
        deck_stack.set_size_request(500, -1);
        let content_stack = gtk::Stack::builder().vexpand(true).hexpand(true).build();
        content_stack.add_named(&deck_stack, Some("decks"));
        let no_decks = gtk::Box::builder()
            .orientation(gtk::Orientation::Vertical)
            .halign(gtk::Align::Center)
            .valign(gtk::Align::Center)
            .hexpand(true)
            .vexpand(true)
            .build();
        let caption = gtk::Label::new(Some("No Decks Available"));
        caption.add_css_class("error-label");
        no_decks.append(&caption);
        let add_fake = gtk::Button::builder()
            .label("Add A Fake Deck")
            .margin_top(60)
            .margin_start(60)
            .margin_end(60)
            .halign(gtk::Align::Center)
            .build();
        for class in ["text-button", "suggested-action", "pill"] {
            add_fake.add_css_class(class);
        }
        no_decks.append(&add_fake);
        no_decks.append(&gtk::Label::builder().label("<a href=\"https://core447.com/streamcontroller/docs/latest/common_problems/#1-no-decks-found\">Checkout Common Problems</a>")
            .use_markup(true).margin_top(12).build());
        content_stack.add_named(&no_decks, Some("empty"));
        content.append(&content_stack);
        split.set_content(Some(&adw::NavigationPage::new(&content, "Deckard")));
        let side = gtk::Box::new(gtk::Orientation::Vertical, 0);
        let side_header = adw::HeaderBar::builder().show_back_button(false).build();
        side_header.add_css_class("flat");
        let page_button = gtk::MenuButton::new();
        page_button.add_css_class("header-page-dropdown");
        page_button.set_tooltip_text(Some("Select page"));
        let page_label = gtk::Label::builder()
            .label("Main")
            .xalign(0.)
            .hexpand(true)
            .ellipsize(gtk::pango::EllipsizeMode::End)
            .max_width_chars(20)
            .build();
        let page_content = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        page_content.append(&page_label);
        page_content.append(&gtk::Image::from_icon_name("pan-down-symbolic"));
        page_button.set_child(Some(&page_content));
        let header_box = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        let caption = gtk::Label::new(Some("Page:"));
        caption.add_css_class("bold");
        caption.set_margin_start(3);
        caption.set_margin_end(7);
        header_box.append(&caption);
        let linked = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        linked.add_css_class("linked");
        header_box.append(&linked);
        linked.append(&page_button);
        let page_settings = gtk::Button::from_icon_name("folder-documents-symbolic");
        page_settings.set_tooltip_text(Some("Page settings"));
        linked.append(&page_settings);
        let pages = gtk::Button::from_icon_name("folder-open-symbolic");
        pages.set_tooltip_text(Some("Page manager"));
        linked.append(&pages);
        side_header.set_title_widget(Some(&header_box));
        side.append(&side_header);
        let sidebar = gtk::Stack::builder()
            .vexpand(true)
            .transition_type(gtk::StackTransitionType::SlideLeftRight)
            .build();
        let scroll = gtk::ScrolledWindow::builder()
            .vexpand(true)
            .hscrollbar_policy(gtk::PolicyType::Never)
            .build();
        let editor = gtk::Box::new(gtk::Orientation::Vertical, 0);
        let controls = gtk::Box::new(gtk::Orientation::Vertical, 0);
        let screen_title = gtk::Label::new(Some("Touch Bar"));
        screen_title.add_css_class("large-title");
        screen_title.add_css_class("bold");
        screen_title.set_margin_top(15);
        screen_title.set_margin_bottom(30);
        screen_title.set_visible(false);
        controls.append(&screen_title);
        let state_scroll = gtk::ScrolledWindow::new();
        state_scroll.set_hexpand(true);
        state_scroll.set_margin_start(20);
        state_scroll.set_margin_end(20);
        state_scroll.set_margin_top(10);
        state_scroll.set_margin_bottom(10);
        let state_box = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        state_box.set_halign(gtk::Align::Center);
        state_box.set_valign(gtk::Align::Center);
        state_box.set_overflow(gtk::Overflow::Hidden);
        state_box.add_css_class("state-switcher-box");
        state_box.add_css_class("linked");
        state_scroll.set_child(Some(&state_box));
        controls.append(&state_scroll);
        let preview = PreviewImage::new(175, 175);
        preview.set_halign(gtk::Align::Center);
        preview.add_css_class("key-image");
        preview.set_overflow(gtk::Overflow::Hidden);
        preview.add_css_class("icon-selector-image-base");
        preview.add_css_class("icon-selector-image-key");
        let picture_overlay = gtk::Overlay::builder().child(&preview).build();
        let hint = gtk::Label::builder()
            .label("Click to change")
            .halign(gtk::Align::Center)
            .valign(gtk::Align::Center)
            .can_target(false)
            .build();
        hint.add_css_class("icon-selector-hint-label-hidden");
        picture_overlay.add_overlay(&hint);
        let preview_button = gtk::Button::builder().child(&picture_overlay).build();
        preview_button.add_css_class("icon-selectorkey-image");
        preview_button.add_css_class("no-padding");
        preview_button.set_overflow(gtk::Overflow::Hidden);
        controls::margins(&preview_button, 10);
        preview_button.set_widget_name("selected-icon");
        let preview_host = gtk::Overlay::builder()
            .child(&preview_button)
            .halign(gtk::Align::Center)
            .margin_top(30)
            .build();
        let remove_icon = gtk::Button::from_icon_name("user-trash-symbolic");
        remove_icon.set_widget_name("remove-selected-image");
        for class in ["icon-selector-remove-button", "no-padding", "remove-button"] {
            remove_icon.add_css_class(class);
        }
        remove_icon.set_halign(gtk::Align::End);
        remove_icon.set_valign(gtk::Align::End);
        remove_icon.set_visible(false);
        preview_host.add_overlay(&remove_icon);
        preview_host.set_clip_overlay(&remove_icon, true);
        controls.append(&preview_host);
        scroll.set_child(Some(&controls));
        editor.append(&scroll);
        let remove_state = gtk::Button::with_label("Remove State");
        remove_state.add_css_class("destructive-action");
        controls::margins(&remove_state, 15);
        remove_state.set_visible(false);
        editor.append(&remove_state);
        sidebar.set_transition_duration(200);
        sidebar.add_named(&editor, Some("editor"));
        side.append(&sidebar);
        let sidebar_page = adw::NavigationPage::new(&side, "Sidebar");
        sidebar_page.set_margin_start(4);
        sidebar_page.set_margin_end(4);
        sidebar_page.set_size_request(300, -1);
        sidebar_page.set_hexpand(true);
        split.set_sidebar(Some(&sidebar_page));
        split.set_show_content(true);
        let engine = shared.lock().unwrap();
        let mut devices: Vec<_> = engine.devices.values().collect();
        devices.sort_by_key(|d| &d.serial);
        let serial = devices
            .first()
            .map(|d| d.serial.clone())
            .unwrap_or_default();
        let page = devices
            .first()
            .map(|d| d.page.clone())
            .or_else(|| engine.docs.pages.keys().next().cloned())
            .unwrap_or_else(|| "Main".into());
        let definitions = action_definitions(&engine.plugins);
        let revision = engine.plugin_revision;
        drop(engine);
        let bridge = Bridge::new(shared.clone(), signal.clone());
        let ui = Rc::new(Self {
            shared,
            events,
            signal,
            bridge,
            pending: RefCell::new(VecDeque::new()),
            pending_count: Cell::new(0),
            completions: RefCell::new(HashMap::new()),
            next_completion: Cell::new(0),
            force_reload: Cell::new(true),
            window,
            overlay,
            split,
            sidebar,
            controls,
            deck_stack,
            content_stack,
            deck_titles,
            main_menu: menu,
            fps_banners: RefCell::new(HashMap::new()),
            page_button,
            page_label,
            remove_state,
            screen_title,
            editor_scroll: scroll,
            editor_body: editor,
            screen_clamp: adw::Clamp::new(),
            remove_icon: remove_icon.clone(),
            deck_settings_visible: Cell::new(false),
            deck_settings,
            preview,
            preview_host,
            dial_preview: Cell::new(None),
            state_box,
            selection: RefCell::new(Selection {
                serial,
                page,
                family: "keys".into(),
                input: "0x0".into(),
                state: 0,
                sticky: false,
            }),
            loaded_selection: RefCell::new(None),
            draft: RefCell::new(json!({})),
            previews: RefCell::new(Vec::new()),
            deck_signature: RefCell::new(Vec::new()),
            loading_shell: Cell::new(false),
            loaded_revision: Cell::new(u64::MAX),
            expanded: RefCell::new(HashMap::new()),
            page_names: RefCell::new(Vec::new()),
            definition_revision: Cell::new(revision),
            definitions: RefCell::new(definitions),
            choices: RefCell::new(json!({})),
            players: RefCell::new(json!([])),
            player_refreshed: Cell::new(std::time::Instant::now() - Duration::from_secs(60)),
            catalog_dialog: RefCell::new(None),
            auxiliary_windows: RefCell::new(HashMap::new()),
            page_manager: RefCell::new(None),
            catalog_box: RefCell::new(None),
            capture_done: Cell::new(false),
        });
        let weak = Rc::downgrade(&ui);
        preview_button.connect_clicked(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.assets();
            }
        });
        let weak = Rc::downgrade(&ui);
        remove_icon.connect_clicked(move |button| {
            if let Some(ui) = weak.upgrade() {
                ui.edit(
                    ui.selection.borrow().clone(),
                    vec!["media".into(), "path".into()],
                    Value::Null,
                );
                button.set_visible(false);
            }
        });
        let motion = gtk::EventControllerMotion::new();
        let weak = Rc::downgrade(&ui);
        let caption = hint.clone();
        motion.connect_enter(move |_, _, _| {
            if let Some(ui) = weak.upgrade() {
                ui.preview.remove_css_class("icon-selector-image-base");
                ui.preview.add_css_class("icon-selector-image-hover");
                caption.remove_css_class("icon-selector-hint-label-hidden");
                caption.add_css_class("icon-selector-hint-label-visible");
            }
        });
        let weak = Rc::downgrade(&ui);
        motion.connect_leave(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.preview.remove_css_class("icon-selector-image-hover");
                ui.preview.add_css_class("icon-selector-image-base");
                hint.remove_css_class("icon-selector-hint-label-visible");
                hint.add_css_class("icon-selector-hint-label-hidden");
            }
        });
        let weak = Rc::downgrade(&ui);
        ui.remove_state.connect_clicked(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.change_states(false);
            }
        });
        ui.preview_host.add_controller(motion);
        ui.install_actions(app);
        let weak = Rc::downgrade(&ui);
        add_fake.connect_clicked(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.settings();
                if let Some(window) = ui
                    .auxiliary_windows
                    .borrow()
                    .get("Settings")
                    .and_then(|w| w.downcast_ref::<adw::PreferencesWindow>())
                {
                    window.set_visible_page_name("Developer");
                }
            }
        });
        ui.bridge.listen(&ui);
        let weak = Rc::downgrade(&ui);
        pages.connect_clicked(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.pages();
            }
        });
        let weak = Rc::downgrade(&ui);
        page_settings.connect_clicked(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.page_settings();
            }
        });
        let weak = Rc::downgrade(&ui);
        ui.deck_settings.connect_clicked(move |_| {
            if let Some(ui) = weak.upgrade() {
                ui.device_settings();
            }
        });
        let weak = Rc::downgrade(&ui);
        ui.window.connect_close_request(move |_| {
            if let Some(ui) = weak.upgrade() {
                let keep = ui.shared.lock().unwrap().docs.settings["keep_running"]
                    .as_bool()
                    .unwrap_or(true);
                if keep {
                    ui.bridge.hidden.store(true, Ordering::Relaxed);
                    ui.clear_textures();
                    ui.window.set_visible(false);
                    gtk::prelude::WidgetExt::unrealize(&ui.window);
                } else {
                    ui.command("quit", json!({}), "", false);
                }
            }
            glib::Propagation::Stop
        });
        let weak = Rc::downgrade(&ui);
        ui.deck_stack
            .connect_visible_child_name_notify(move |stack| {
                if let (Some(ui), Some(serial)) = (weak.upgrade(), stack.visible_child_name())
                    && !ui.loading_shell.get()
                    && ui.selection.borrow().serial != serial.as_str()
                {
                    ui.select_device(serial.as_str());
                }
            });
        ui.refresh();
        ui.window.present();
        let weak = Rc::downgrade(&ui);
        glib::idle_add_local_once(move || {
            if let Some(ui) = weak.upgrade() {
                ui.refresh();
            }
        });
        if std::env::var_os("DECKARD_UI_CAPTURE").is_some() {
            let weak = Rc::downgrade(&ui);
            glib::timeout_add_local_once(Duration::from_millis(1800), move || {
                if let Some(ui) = weak.upgrade() {
                    ui.capture_scene();
                }
            });
            let weak = Rc::downgrade(&ui);
            glib::timeout_add_local_once(Duration::from_millis(5200), move || {
                if let Some(ui) = weak.upgrade() {
                    ui.capture();
                }
            });
        }
        // Reproducible close-to-tray measurements use the real window lifecycle.
        if let Some(delay) = std::env::var("DECKARD_UI_HIDE_AFTER_MS")
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
        {
            let weak = Rc::downgrade(&ui);
            glib::timeout_add_local_once(Duration::from_millis(delay), move || {
                if let Some(ui) = weak.upgrade() {
                    ui.window.close();
                }
            });
        }
        ui
    }

    fn install_actions(self: &Rc<Self>, app: &adw::Application) {
        for name in [
            "store",
            "pages",
            "assets",
            "settings",
            "about",
            "quit",
            "copy-input",
            "cut-input",
            "paste-input",
            "clear-input",
            "update-input",
            "support",
        ] {
            let action = gio::SimpleAction::new(name, None);
            let weak = Rc::downgrade(self);
            action.connect_activate(move |_, _| {
                if let Some(ui) = weak.upgrade() {
                    match name {
                        "store" => ui.store(),
                        "pages" => ui.pages(),
                        "assets" => ui.assets(),
                        "settings" => ui.settings(),
                        "about" => ui.about(),
                        "copy-input" => ui.clipboard(false, false),
                        "cut-input" => ui.clipboard(false, true),
                        "paste-input" => ui.clipboard(true, false),
                        "clear-input" => ui.clear_input(),
                        "update-input" => {
                            ui.force_reload.set(true);
                            ui.refresh();
                        }
                        "support" => {
                            gtk::UriLauncher::new("https://ko-fi.com/core447").launch(
                                Some(&ui.window),
                                gio::Cancellable::NONE,
                                |_| {},
                            );
                        }
                        _ => ui.command("quit", json!({}), "", false),
                    }
                }
            });
            self.window.add_action(&action);
        }
        app.set_accels_for_action("win.settings", &["<Primary>comma"]);
        app.set_accels_for_action("win.quit", &["<Primary>q"]);
    }
    fn toast(&self, text: &str) {
        self.overlay.add_toast(adw::Toast::new(text));
    }
    fn submit(self: &Rc<Self>, work: Work) {
        let mut pending = self.pending.borrow_mut();
        if work.key.is_some() && pending.back().is_some_and(|last| last.key == work.key) {
            *pending.back_mut().unwrap() = work;
        } else {
            pending.push_back(work);
        }
        drop(pending);
        self.pump();
    }
    fn pump(&self) {
        let mut pending = self.pending.borrow_mut();
        while let Some(work) = pending.pop_front() {
            match self.bridge.send.try_send(work) {
                Ok(()) => self.pending_count.set(self.pending_count.get() + 1),
                Err(std::sync::mpsc::TrySendError::Full(work)) => {
                    pending.push_front(work);
                    break;
                }
                Err(std::sync::mpsc::TrySendError::Disconnected(_)) => {
                    self.toast("Editor worker stopped");
                    break;
                }
            }
        }
    }
    fn submit_then(
        self: &Rc<Self>,
        mut work: Work,
        done: impl FnOnce(&Rc<Ui>, &Result<Value>) + 'static,
    ) {
        let id = self.next_completion.get();
        self.next_completion.set(id.wrapping_add(1));
        work.title = format!("completion-{id}");
        self.completions
            .borrow_mut()
            .insert(work.title.clone(), Box::new(done));
        self.submit(work);
    }
    fn command(self: &Rc<Self>, method: &str, params: Value, title: &str, rebuild: bool) {
        let method = method.to_owned();
        self.submit(Work {
            key: None,
            title: title.into(),
            rebuild,
            execute: Box::new(move |engine| {
                engine.command(&json!({"method":method,"params":params}))
            }),
        });
    }
    fn job(
        self: &Rc<Self>,
        title: &str,
        rebuild: bool,
        work: impl FnOnce() -> Result<Value> + Send + 'static,
    ) {
        // Network/media work never holds the engine mutex. It reports through the
        // same main-thread channel and cannot retain GTK widgets across threads.
        let weak = Rc::downgrade(self);
        let (send, receive) = async_channel::bounded(1);
        let title = title.to_owned();
        std::thread::spawn(move || {
            let _ = send.send_blocking(work());
        });
        glib::MainContext::default().spawn_local(async move {
            if let (Ok(result), Some(ui)) = (receive.recv().await, weak.upgrade()) {
                match result {
                    Ok(value) => match title.as_str() {
                        "catalog" => ui.show_catalog(value),
                        "obs-choices" => *ui.choices.borrow_mut() = value,
                        "media-players" => *ui.players.borrow_mut() = value,
                        "ai-proposal" => ui.review_ai(value),
                        _ => ui.toast(&title),
                    },
                    Err(e) => ui.toast(&deckard_core::engine::redact(&format!("{e:#}"))),
                }
                if rebuild {
                    ui.force_reload.set(true);
                    ui.command("reload", json!({}), "", true);
                }
                ui.refresh();
            }
        });
    }
    fn edit(self: &Rc<Self>, selection: Selection, path: Vec<String>, value: Value) {
        if *self.selection.borrow() == selection {
            set_nested(
                &mut self.draft.borrow_mut(),
                &path.iter().map(String::as_str).collect::<Vec<_>>(),
                value.clone(),
            );
        }
        let key = Some(format!("{selection:?}/{path:?}"));
        self.submit(Work {
            key,
            title: String::new(),
            rebuild: false,
            execute: Box::new(move |engine| {
                selection.edit(engine, |draft| {
                    set_nested(
                        draft,
                        &path.iter().map(String::as_str).collect::<Vec<_>>(),
                        value,
                    );
                    Ok(())
                })
            }),
        });
    }
}
