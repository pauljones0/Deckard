//! Asset Manager: upstream window, pack drill-in, pooled 50-card grids and info.
use super::*;
use controls::*;
use std::path::Path;

type ChooseAsset = Rc<dyn Fn(String)>;
#[derive(Clone)]
struct Asset {
    path: PathBuf,
    name: String,
    pack: bool,
    origin: Option<String>,
}
struct Card {
    widget: gtk::FlowBoxChild,
    picture: gtk::Picture,
    label: gtk::Label,
    subtitle: gtk::Label,
    binding: RefCell<Option<Asset>>,
    generation: Cell<u64>,
    grid: RefCell<std::rc::Weak<Grid>>,
}
struct Grid {
    flow: gtk::FlowBox,
    cards: RefCell<Vec<Rc<Card>>>,
    card_factory: Rc<dyn Fn() -> Rc<Card>>,
    items: RefCell<Vec<Asset>>,
    search: gtk::SearchEntry,
    images: gtk::ToggleButton,
    videos: gtk::ToggleButton,
    previous: gtk::Button,
    next: gtk::Button,
    page: Cell<usize>,
    navigation: gtk::Box,
    packs: bool,
    leaf: glib::WeakRef<gtk::Stack>,
    back: glib::WeakRef<gtk::Button>,
    epoch: Arc<std::sync::atomic::AtomicU64>,
    query_epoch: Arc<std::sync::atomic::AtomicU64>,
}
pub(super) struct Thumbnail {
    pub(super) pixels: Vec<u8>,
    pub(super) width: i32,
    pub(super) height: i32,
    pub(super) stride: usize,
    pub(super) alpha: bool,
}
fn decode(path: &Path) -> Option<Thumbnail> {
    decode_image(path, 250, 180)
}
pub(super) fn decode_image(path: &Path, width: i32, height: i32) -> Option<Thumbnail> {
    let pixbuf = match gtk::gdk_pixbuf::Pixbuf::from_file_at_scale(path, width, height, true) {
        Ok(pixbuf) => pixbuf,
        Err(_) if video(path) => {
            let temporary = tempfile::tempdir().ok()?;
            let output = temporary.path().join("thumbnail.png");
            deckard_core::desktop::run_command(
                &[
                    "ffmpeg".into(),
                    "-v".into(),
                    "error".into(),
                    "-nostdin".into(),
                    "-i".into(),
                    path.to_string_lossy().into_owned(),
                    "-frames:v".into(),
                    "1".into(),
                    "-vf".into(),
                    format!("scale={width}:{height}:force_original_aspect_ratio=decrease"),
                    output.to_string_lossy().into_owned(),
                ],
                Duration::from_secs(4),
            )
            .ok()?;
            gtk::gdk_pixbuf::Pixbuf::from_file(&output).ok()?
        }
        Err(_) => return None,
    };
    Some(Thumbnail {
        pixels: pixbuf.read_pixel_bytes().as_ref().to_vec(),
        width: pixbuf.width(),
        height: pixbuf.height(),
        stride: pixbuf.rowstride() as usize,
        alpha: pixbuf.has_alpha(),
    })
}
fn read_json(path: &Path) -> Value {
    std::fs::read(path)
        .ok()
        .filter(|b| b.len() <= 1024 * 1024)
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or(Value::Null)
}
fn pack_manifest(path: &Path) -> Value {
    let manifest = read_json(&path.join("manifest.json"));
    if manifest.is_object() {
        manifest
    } else {
        read_json(&path.join("package.json"))
    }
}
fn pack_assets(path: &Path) -> Vec<Asset> {
    let manifest = pack_manifest(path);
    let directory = manifest["icons"]
        .as_str()
        .or_else(|| manifest["images"].as_str());
    let root = directory
        .map(|d| path.join(d))
        .unwrap_or_else(|| path.to_owned());
    let mut items = Vec::new();
    scan(&root, &mut items, 5);
    if let Some(banner) = manifest["thumbnail"]
        .as_str()
        .or_else(|| manifest["banner"].as_str())
    {
        items.retain(|a| a.path != path.join(banner));
    }
    items
}
fn pack_thumbnail(path: &Path) -> PathBuf {
    let manifest = pack_manifest(path);
    if let Some(banner) = manifest["thumbnail"]
        .as_str()
        .or_else(|| manifest["banner"].as_str())
    {
        return path.join(banner);
    }
    PathBuf::new()
}
fn attribution(asset: &Asset) -> Value {
    for ancestor in asset.path.ancestors().take(7) {
        if ancestor.join("manifest.json").is_file() || ancestor.join("package.json").is_file() {
            let data = read_json(&ancestor.join("attribution.json"));
            let name = asset.path.file_name().unwrap_or_default().to_string_lossy();
            let stem = asset.path.file_stem().unwrap_or_default().to_string_lossy();
            return data
                .get(name.as_ref())
                .or_else(|| data.get(stem.as_ref()))
                .or_else(|| data.get("default"))
                .or_else(|| data.get("general"))
                .or_else(|| data.get("generic"))
                .cloned()
                .unwrap_or_else(|| pack_manifest(ancestor)["license"].clone());
        }
        let data = read_json(&ancestor.join("Assets.json"));
        if let Some(entries) = data.as_array()
            && let Some(entry) = entries
                .iter()
                .find(|v| v["internal-path"].as_str() == asset.path.to_str())
        {
            let mut license = entry["license"].clone();
            if license.is_object() && entry["original-url"].is_string() {
                license["original-url"] = entry["original-url"].clone();
            }
            return license;
        }
    }
    Value::Null
}
fn scan(directory: &Path, result: &mut Vec<Asset>, depth: usize) {
    if depth == 0 {
        return;
    }
    if let Ok(entries) = std::fs::read_dir(directory) {
        for entry in entries.flatten() {
            let Ok(kind) = entry.file_type() else {
                continue;
            };
            if kind.is_dir() {
                scan(&entry.path(), result, depth - 1);
            } else if kind.is_file() && media_file(&entry.path()) {
                result.push(Asset {
                    path: entry.path(),
                    name: entry
                        .path()
                        .file_stem()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .into_owned(),
                    pack: false,
                    origin: None,
                });
            }
        }
    }
}
fn media_file(path: &Path) -> bool {
    path.extension().and_then(|s| s.to_str()).is_some_and(|s| {
        matches!(
            s.to_ascii_lowercase().as_str(),
            "png"
                | "jpg"
                | "jpeg"
                | "webp"
                | "svg"
                | "gif"
                | "bmp"
                | "mp4"
                | "webm"
                | "mov"
                | "mkv"
                | "avi"
        )
    })
}
fn video(path: &Path) -> bool {
    path.extension().and_then(|s| s.to_str()).is_some_and(|s| {
        matches!(
            s.to_ascii_lowercase().as_str(),
            "gif" | "mp4" | "webm" | "mov" | "mkv" | "avi"
        )
    })
}
impl Grid {
    fn refresh(self: &Rc<Self>) {
        let query = self.search.text().to_string();
        let mut ranked = self
            .items
            .borrow()
            .iter()
            .filter(|item| {
                item.pack
                    || if video(&item.path) {
                        self.videos.is_active()
                    } else {
                        self.images.is_active()
                    }
            })
            .filter_map(|item| {
                let rank = search::asset_rank(&item.name, &query);
                (rank.0 <= -65).then(|| (rank, item.clone()))
            })
            .collect::<Vec<_>>();
        ranked.sort_by(|a, b| a.0.cmp(&b.0));
        let items = ranked.into_iter().map(|(_, item)| item).collect::<Vec<_>>();
        let page_size = if self.packs { items.len().max(1) } else { 50 };
        let pages = items.len().div_ceil(page_size).max(1);
        self.page.set(self.page.get().min(pages - 1));
        self.navigation.set_visible(!self.packs);
        while self.cards.borrow().len() < if self.packs { items.len() } else { 50 } {
            let card = (self.card_factory)();
            *card.grid.borrow_mut() = Rc::downgrade(self);
            self.flow.append(&card.widget);
            self.cards.borrow_mut().push(card);
        }
        self.previous.set_sensitive(self.page.get() > 0);
        self.next.set_sensitive(self.page.get() + 1 < pages);
        self.flow.unselect_all();
        let epoch_value = self.epoch.fetch_add(1, Ordering::Relaxed) + 1;
        let epoch = self.epoch.clone();
        let (send, receive) = async_channel::bounded(50);
        let mut loads = Vec::new();
        for (index, card) in self.cards.borrow().iter().enumerate() {
            card.generation.set(card.generation.get() + 1);
            card.picture.set_paintable(None::<&gdk::Paintable>);
            let item = items.get(self.page.get() * page_size + index).cloned();
            card.widget.set_visible(item.is_some());
            if let Some(item) = &item {
                card.label.set_text(&item.name);
                card.subtitle.set_text(item.origin.as_deref().unwrap_or(""));
                card.subtitle.set_visible(item.origin.is_some());
                if self.flow.is_mapped() {
                    loads.push((index, card.generation.get(), item.clone()));
                }
            }
            *card.binding.borrow_mut() = item;
        }
        // One worker per grid pass, bounded to the visible 50 cards. The result
        // channel carries pixels only; GTK objects stay on the main thread.
        std::thread::spawn(move || {
            for (index, generation, item) in loads {
                let path = if item.pack {
                    pack_thumbnail(&item.path)
                } else {
                    item.path
                };
                if send.is_closed() || epoch.load(Ordering::Relaxed) != epoch_value {
                    break;
                }
                if send
                    .send_blocking((index, generation, decode(&path)))
                    .is_err()
                {
                    break;
                }
            }
        });
        let weak = Rc::downgrade(self);
        glib::MainContext::default().spawn_local(async move {
            while let Ok((index, generation, thumbnail)) = receive.recv().await {
                let Some(grid) = weak.upgrade() else {
                    break;
                };
                let Some(card) = grid.cards.borrow().get(index).cloned() else {
                    continue;
                };
                if card.generation.get() != generation {
                    continue;
                }
                if let Some(image) = thumbnail {
                    let bytes = glib::Bytes::from_owned(image.pixels);
                    let texture = gdk::MemoryTexture::new(
                        image.width,
                        image.height,
                        if image.alpha {
                            gdk::MemoryFormat::R8g8b8a8
                        } else {
                            gdk::MemoryFormat::R8g8b8
                        },
                        &bytes,
                        image.stride,
                    );
                    card.picture.set_paintable(Some(&texture));
                }
            }
        });
    }
}
impl Ui {
    pub(super) fn assets(self: &Rc<Self>) {
        let owner = self.selection.borrow().clone();
        let weak = Rc::downgrade(self);
        self.asset_manager(Rc::new(move |path| {
            if let Some(ui) = weak.upgrade() {
                ui.edit(
                    owner.clone(),
                    vec!["media".into(), "path".into()],
                    json!(path),
                );
                ui.force_reload.set(true);
            }
        }));
    }
    pub(super) fn asset_manager(self: &Rc<Self>, choose: ChooseAsset) {
        // A new session drops every old callback and resets search/drill-in.
        let old = self.auxiliary_windows.borrow_mut().remove("Asset Manager");
        if let Some(old) = old {
            old.close();
        }
        let window = gtk::ApplicationWindow::builder()
            .title("Asset Manager")
            .default_width(1050)
            .default_height(750)
            .build();
        let header = gtk::HeaderBar::new();
        header.add_css_class("flat");
        window.set_titlebar(Some(&header));
        let main = gtk::Stack::builder()
            .hexpand(true)
            .vexpand(true)
            .transition_type(gtk::StackTransitionType::SlideLeftRight)
            .transition_duration(200)
            .build();
        let chooser = gtk::Stack::new();
        main.add_named(&chooser, Some("Asset Chooser"));
        window.set_child(Some(&main));
        header.set_title_widget(Some(&gtk::StackSwitcher::builder().stack(&chooser).build()));
        let back = gtk::Button::from_icon_name("go-previous-symbolic");
        back.set_visible(false);
        header.pack_start(&back);
        let weak_main = main.downgrade();
        let weak_chooser = chooser.downgrade();
        back.connect_clicked(move |back| {
            if let Some(main) = weak_main.upgrade() {
                main.set_visible_child_name("Asset Chooser");
            }
            if let Some(chooser) = weak_chooser.upgrade()
                && let Some(stack) = chooser.visible_child().and_downcast::<gtk::Stack>()
            {
                stack.set_visible_child_name("pack-chooser");
            }
            back.set_visible(false);
        });
        let root = self.shared.lock().unwrap().docs.root.clone();
        for (key, title, directories, custom) in [
            (
                "custom-assets",
                "Custom Assets",
                vec![root.join("Assets/AssetManager/Assets"), root.join("assets")],
                true,
            ),
            (
                "icon-packs",
                "Icon Packs",
                vec![root.join("icons-native"), root.join("icons")],
                false,
            ),
            (
                "wallpaper-packs",
                "Wallpaper Packs",
                vec![root.join("wallpapers-native"), root.join("wallpapers")],
                false,
            ),
            (
                "sd-plus-bar-wallpaper-packs",
                "SD+ Bar Wallpapers",
                vec![
                    root.join("sdplusbar-native"),
                    root.join("sd-plus-bar-wallpapers"),
                ],
                false,
            ),
        ] {
            let family = gtk::Stack::new();
            let (body, grid) = self.asset_grid(
                &family,
                &main,
                &back,
                &window,
                choose.clone(),
                custom,
                !custom,
            );
            family.add_named(&body, Some("pack-chooser"));
            chooser.add_titled(&family, Some(key), title);
            let weak_grid = Rc::downgrade(&grid);
            let (send, receive) = async_channel::bounded(1);
            std::thread::spawn(move || {
                let mut items = Vec::new();
                for directory in directories {
                    if custom {
                        scan(&directory, &mut items, 4);
                    } else if let Ok(entries) = std::fs::read_dir(directory) {
                        for entry in entries
                            .flatten()
                            .filter(|entry| entry.file_type().is_ok_and(|kind| kind.is_dir()))
                        {
                            items.push(Asset {
                                path: entry.path(),
                                name: pack_manifest(&entry.path())["name"]
                                    .as_str()
                                    .map(str::to_owned)
                                    .unwrap_or_else(|| {
                                        entry.file_name().to_string_lossy().into_owned()
                                    }),
                                pack: true,
                                origin: None,
                            });
                        }
                    }
                }
                let _ = send.send_blocking(items);
            });
            glib::MainContext::default().spawn_local(async move {
                if let (Ok(items), Some(grid)) = (receive.recv().await, weak_grid.upgrade()) {
                    *grid.items.borrow_mut() = items;
                    grid.refresh();
                }
            });
            if custom {
                let weak = Rc::downgrade(self);
                let weak_grid = Rc::downgrade(&grid);
                let browse = button("Browse Files", "", move || {
                    if let Some(ui) = weak.upgrade() {
                        let weak = Rc::downgrade(&ui);
                        let grid = weak_grid.clone();
                        let dialog = gtk::FileDialog::builder()
                            .title("Select Files")
                            .modal(true)
                            .build();
                        let parent = ui.auxiliary_windows.borrow().get("Asset Manager").cloned();
                        dialog.open_multiple(
                            parent.as_ref(),
                            gio::Cancellable::NONE,
                            move |result| {
                                if let (Ok(files), Some(ui), Some(grid)) =
                                    (result, weak.upgrade(), grid.upgrade())
                                {
                                    for i in 0..files.n_items() {
                                        if let Some(file) =
                                            files.item(i).and_downcast::<gio::File>()
                                            && let Some(path) = file.path()
                                        {
                                            ui.import_custom_asset(&path, &grid);
                                        }
                                    }
                                }
                            },
                        );
                    }
                });
                browse.set_margin_top(15);
                body.append(&browse);
                let drop =
                    gtk::DropTarget::new(gdk::FileList::static_type(), gdk::DragAction::COPY);
                let weak = Rc::downgrade(self);
                let weak_grid = Rc::downgrade(&grid);
                drop.connect_drop(move |_, value, _, _| {
                    if let (Ok(files), Some(ui), Some(grid)) = (
                        value.get::<gdk::FileList>(),
                        weak.upgrade(),
                        weak_grid.upgrade(),
                    ) {
                        for file in files.files() {
                            if let Some(path) = file.path() {
                                ui.import_custom_asset(&path, &grid);
                            }
                        }
                        true
                    } else {
                        false
                    }
                });
                body.add_controller(drop);
            } else if key == "icon-packs" {
                let weak = Rc::downgrade(self);
                let target = Rc::downgrade(&grid);
                let import = button("Import Pack", "", move || {
                    if let (Some(ui), Some(grid)) = (weak.upgrade(), target.upgrade()) {
                        ui.import_pack("icons", &grid);
                    }
                });
                import.set_margin_start(15);
                import.set_valign(gtk::Align::Center);
                if let Some(nav) = body.first_child().and_downcast::<gtk::Box>() {
                    nav.append(&import);
                }
            }
            // Signals own the grid while its GTK page is alive; no Ui/window
            // strong reference is captured, so closing releases textures and data.
            let keep = grid.clone();
            body.connect_unmap(move |_| {
                keep.epoch.fetch_add(1, Ordering::Relaxed);
                for card in keep.cards.borrow().iter() {
                    card.picture.set_paintable(None::<&gdk::Paintable>);
                    card.generation.set(card.generation.get() + 1);
                }
            });
            let keep = grid.clone();
            body.connect_map(move |_| {
                if keep.packs {
                    keep.search.set_text("");
                }
                keep.refresh();
            });
        }
        self.keep_window("Asset Manager", &window);
        // Upstream leaves the search entry unfocused when opening this window.
        gtk::prelude::GtkWindowExt::set_focus(&window, None::<&gtk::Widget>);
    }
    #[allow(clippy::too_many_arguments)]
    fn asset_grid(
        self: &Rc<Self>,
        family: &gtk::Stack,
        main: &gtk::Stack,
        back: &gtk::Button,
        window: &gtk::ApplicationWindow,
        choose: ChooseAsset,
        custom: bool,
        packs: bool,
    ) -> (gtk::Box, Rc<Grid>) {
        let body = gtk::Box::builder()
            .orientation(gtk::Orientation::Vertical)
            .hexpand(true)
            .vexpand(true)
            .build();
        margins(&body, 15);
        let nav = gtk::Box::builder()
            .orientation(gtk::Orientation::Horizontal)
            .hexpand(true)
            .margin_bottom(15)
            .build();
        body.append(&nav);
        let search = gtk::SearchEntry::builder()
            .placeholder_text("Search")
            .hexpand(true)
            .build();
        nav.append(&search);
        let types = gtk::Box::builder()
            .orientation(gtk::Orientation::Horizontal)
            .margin_start(15)
            .visible(custom)
            .build();
        types.add_css_class("linked");
        nav.append(&types);
        let videos = gtk::ToggleButton::builder()
            .icon_name("camera-video-symbolic")
            .active(true)
            .build();
        videos.set_css_classes(&["blue-toggle-button"]);
        types.append(&videos);
        let images = gtk::ToggleButton::builder()
            .icon_name("camera-photo-symbolic")
            .active(true)
            .build();
        images.set_css_classes(&["blue-toggle-button"]);
        types.append(&images);
        let scroll = gtk::ScrolledWindow::builder()
            .hexpand(true)
            .vexpand(true)
            .build();
        body.append(&scroll);
        let content = gtk::Box::new(gtk::Orientation::Vertical, 0);
        scroll.set_child(Some(&content));
        let flow = gtk::FlowBox::builder()
            .hexpand(true)
            .selection_mode(if packs {
                gtk::SelectionMode::None
            } else {
                gtk::SelectionMode::Single
            })
            .build();
        content.append(&flow);
        content.append(&gtk::Box::builder().vexpand(true).build());
        let nav = gtk::Box::builder()
            .orientation(gtk::Orientation::Horizontal)
            .hexpand(true)
            .margin_top(15)
            .margin_bottom(15)
            .margin_start(15)
            .margin_end(15)
            .build();
        body.append(&nav);
        let previous = gtk::Button::builder()
            .icon_name("go-previous-symbolic")
            .sensitive(false)
            .build();
        nav.append(&previous);
        nav.append(&gtk::Box::builder().hexpand(true).build());
        let next = gtk::Button::builder()
            .icon_name("go-next-symbolic")
            .sensitive(false)
            .build();
        nav.append(&next);
        let card_ui = Rc::downgrade(self);
        let card_main = main.downgrade();
        let card_back = back.downgrade();
        let card_factory: Rc<dyn Fn() -> Rc<Card>> = Rc::new(move || {
            let widget = gtk::FlowBoxChild::new();
            widget.add_css_class("asset-preview");
            margins(&widget, 5);
            widget.set_visible(false);
            let overlay = gtk::Overlay::new();
            widget.set_child(Some(&overlay));
            let content = gtk::Box::builder()
                .orientation(gtk::Orientation::Vertical)
                .width_request(250)
                .height_request(180)
                .build();
            overlay.set_child(Some(&content));
            let picture = gtk::Picture::builder()
                .width_request(250)
                .height_request(180)
                .overflow(gtk::Overflow::Hidden)
                .content_fit(gtk::ContentFit::Cover)
                .build();
            content.append(&picture);
            let label = gtk::Label::builder()
                .xalign(0.5)
                .ellipsize(gtk::pango::EllipsizeMode::End)
                .max_width_chars(20)
                .margin_start(20)
                .margin_end(20)
                .build();
            content.append(&label);
            let subtitle = gtk::Label::builder()
                .xalign(0.5)
                .ellipsize(gtk::pango::EllipsizeMode::End)
                .max_width_chars(20)
                .margin_start(20)
                .margin_end(20)
                .visible(false)
                .build();
            subtitle.add_css_class("dim-label");
            subtitle.add_css_class("caption");
            content.append(&subtitle);
            let card = Rc::new(Card {
                widget: widget.clone(),
                picture,
                label,
                subtitle,
                binding: RefCell::new(None),
                generation: Cell::new(0),
                grid: RefCell::new(std::rc::Weak::new()),
            });
            let info = gtk::Button::builder()
                .icon_name("help-about-symbolic")
                .halign(gtk::Align::Start)
                .valign(gtk::Align::End)
                .margin_start(5)
                .margin_bottom(5)
                .build();
            overlay.add_overlay(&info);
            let weak = card_ui.clone();
            let owner = Rc::downgrade(&card);
            let main = card_main.clone();
            let back = card_back.clone();
            info.connect_clicked(move |_| {
                if let (Some(ui), Some(card), Some(main), Some(back)) = (
                    weak.upgrade(),
                    owner.upgrade(),
                    main.upgrade(),
                    back.upgrade(),
                ) && let Some(asset) = card.binding.borrow().clone()
                {
                    ui.asset_info(&main, &back, &asset);
                }
            });
            let remove = gtk::Button::builder()
                .icon_name("user-trash-symbolic")
                .valign(gtk::Align::End)
                .halign(gtk::Align::End)
                .margin_end(5)
                .margin_bottom(5)
                .visible(custom)
                .build();
            overlay.add_overlay(&remove);
            let weak = card_ui.clone();
            let owner = Rc::downgrade(&card);
            remove.connect_clicked(move |_| {
                if let (Some(ui), Some(card)) = (weak.upgrade(), owner.upgrade()) {
                    let asset = card.binding.borrow().clone();
                    if let Some(asset) = asset
                        && let Some(grid) = card.grid.borrow().upgrade()
                    {
                        ui.delete_custom_asset(&asset.path, &grid);
                    }
                }
            });
            card
        });
        let grid = Rc::new(Grid {
            flow,
            cards: RefCell::new(Vec::new()),
            card_factory,
            items: RefCell::new(Vec::new()),
            search,
            images,
            videos,
            previous,
            next,
            page: Cell::new(0),
            navigation: nav,
            packs,
            leaf: family.downgrade(),
            back: back.downgrade(),
            epoch: Arc::new(std::sync::atomic::AtomicU64::new(0)),
            query_epoch: Arc::new(std::sync::atomic::AtomicU64::new(0)),
        });
        let search_choose = choose.clone();
        let search_main = main.downgrade();
        let weak = Rc::downgrade(self);
        let owner = Rc::downgrade(&grid);
        let main = main.downgrade();
        let window = window.downgrade();
        grid.flow.connect_child_activated(move |_, child| {
            if let (Some(ui), Some(grid)) = (weak.upgrade(), owner.upgrade()) {
                let Some(card) = grid.cards.borrow().get(child.index() as usize).cloned() else {
                    return;
                };
                let Some(asset) = card.binding.borrow().clone() else {
                    return;
                };
                if asset.pack {
                    if let (Some(main), Some(window), Some(family), Some(back)) = (
                        main.upgrade(),
                        window.upgrade(),
                        grid.leaf.upgrade(),
                        grid.back.upgrade(),
                    ) {
                        if let Some(old) = family.child_by_name("leaf") {
                            family.remove(&old);
                        }
                        let (body, leaf) = ui.asset_grid(
                            &family,
                            &main,
                            &back,
                            &window,
                            choose.clone(),
                            false,
                            false,
                        );
                        let (send, receive) = async_channel::bounded(1);
                        std::thread::spawn(move || {
                            let _ = send.send_blocking(pack_assets(&asset.path));
                        });
                        let weak_leaf = Rc::downgrade(&leaf);
                        glib::MainContext::default().spawn_local(async move {
                            if let (Ok(items), Some(leaf)) =
                                (receive.recv().await, weak_leaf.upgrade())
                            {
                                *leaf.items.borrow_mut() = items;
                                leaf.refresh();
                            }
                        });
                        let keep = leaf.clone();
                        body.connect_map(move |_| keep.refresh());
                        let keep = leaf.clone();
                        body.connect_unmap(move |_| {
                            for card in keep.cards.borrow().iter() {
                                card.picture.set_paintable(None::<&gdk::Paintable>);
                            }
                        });
                        family.add_named(&body, Some("leaf"));
                        family.set_visible_child_name("leaf");
                        back.set_visible(true);
                        leaf.refresh();
                    }
                } else {
                    choose(asset.path.to_string_lossy().into_owned());
                    if let Some(window) = window.upgrade() {
                        window.close();
                    }
                }
            }
        });
        for (button, forward) in [(&grid.previous, false), (&grid.next, true)] {
            let weak = Rc::downgrade(&grid);
            button.connect_clicked(move |_| {
                if let Some(grid) = weak.upgrade() {
                    grid.page.set(if forward {
                        grid.page.get() + 1
                    } else {
                        grid.page.get().saturating_sub(1)
                    });
                    grid.refresh();
                }
            });
        }
        let weak = Rc::downgrade(&grid);
        let weak_ui = Rc::downgrade(self);
        grid.search.connect_search_changed(move |_| {
            let Some(grid) = weak.upgrade() else {
                return;
            };
            grid.page.set(0);
            grid.refresh();
            let query = grid.search.text().to_string();
            let epoch_value = grid.query_epoch.fetch_add(1, Ordering::Relaxed) + 1;
            if !grid.packs || query.trim().is_empty() {
                return;
            }
            let packs = grid.items.borrow().clone();
            let epoch = grid.query_epoch.clone();
            let (send, receive) = async_channel::bounded(1);
            let needle = query.clone();
            std::thread::spawn(move || {
                let mut result = Vec::new();
                for pack in packs {
                    if send.is_closed() || epoch.load(Ordering::Relaxed) != epoch_value {
                        return;
                    }
                    for mut asset in pack_assets(&pack.path) {
                        if search::asset_rank(&asset.name, &needle).0 <= -65 {
                            asset.origin = Some(pack.name.clone());
                            result.push(asset);
                        }
                    }
                }
                let _ = send.send_blocking(result);
            });
            let weak = Rc::downgrade(&grid);
            let weak_ui = weak_ui.clone();
            let main = search_main.clone();
            let choose = search_choose.clone();
            glib::MainContext::default().spawn_local(async move {
                if let (Ok(items), Some(grid), Some(ui), Some(main)) = (
                    receive.recv().await,
                    weak.upgrade(),
                    weak_ui.upgrade(),
                    main.upgrade(),
                ) && grid.query_epoch.load(Ordering::Relaxed) == epoch_value
                    && grid.search.text() == query
                    && grid.flow.is_mapped()
                    && let (Some(family), Some(back), Some(window)) = (
                        grid.leaf.upgrade(),
                        grid.back.upgrade(),
                        ui.auxiliary_windows
                            .borrow()
                            .get("Asset Manager")
                            .cloned()
                            .and_then(|window| window.downcast::<gtk::ApplicationWindow>().ok()),
                    )
                {
                    if let Some(old) = family.child_by_name("leaf") {
                        family.remove(&old);
                    }
                    let (body, leaf) =
                        ui.asset_grid(&family, &main, &back, &window, choose, false, false);
                    *leaf.items.borrow_mut() = items;
                    leaf.search.set_text(&query);
                    let keep = leaf.clone();
                    body.connect_map(move |_| keep.refresh());
                    family.add_named(&body, Some("leaf"));
                    family.set_visible_child_name("leaf");
                    back.set_visible(true);
                    leaf.refresh();
                }
            });
        });
        for button in [&grid.images, &grid.videos] {
            let weak = Rc::downgrade(&grid);
            button.connect_toggled(move |_| {
                if let Some(grid) = weak.upgrade() {
                    grid.page.set(0);
                    grid.refresh();
                }
            });
        }
        (body, grid)
    }
    #[allow(deprecated)]
    fn import_pack(self: &Rc<Self>, kind: &str, grid: &Rc<Grid>) {
        let parent = self
            .auxiliary_windows
            .borrow()
            .get("Asset Manager")
            .cloned()
            .unwrap_or_else(|| self.window.clone().upcast());
        let dialog = adw::MessageDialog::builder()
            .transient_for(&parent)
            .modal(true)
            .heading("Import an icon pack")
            .body("Choose a zip archive or a folder of pictures")
            .build();
        dialog.add_response("cancel", "Cancel");
        dialog.add_response("import", "Import");
        dialog.set_response_appearance("import", adw::ResponseAppearance::Suggested);
        dialog.set_default_response(Some("cancel"));
        dialog.set_close_response("cancel");
        dialog.set_response_enabled("import", false);
        let group = adw::PreferencesGroup::builder().margin_top(10).build();
        dialog.set_extra_child(Some(&group));
        let name = adw::EntryRow::builder().title("Name").build();
        let description = adw::EntryRow::builder().title("Description").build();
        group.add(&name);
        group.add(&description);
        let source = Rc::new(RefCell::new(None::<PathBuf>));
        let banner = Rc::new(RefCell::new(None::<PathBuf>));
        let source_row = adw::ActionRow::builder()
            .title("Pictures")
            .subtitle("Nothing chosen")
            .build();
        group.add(&source_row);
        for (title, folder) in [("Archive", false), ("Folder", true)] {
            let pick = gtk::Button::builder()
                .label(title)
                .valign(gtk::Align::Center)
                .build();
            source_row.add_suffix(&pick);
            let parent = parent.downgrade();
            let target = source.clone();
            let row = source_row.downgrade();
            let name = name.downgrade();
            let dialog = dialog.downgrade();
            pick.connect_clicked(move |_| {
                let chooser = gtk::FileDialog::new();
                let parent = parent.upgrade();
                let target = target.clone();
                let row = row.clone();
                let name = name.clone();
                let dialog = dialog.clone();
                let selected = move |result: std::result::Result<gio::File, glib::Error>| {
                    if let Ok(file) = result
                        && let Some(path) = file.path()
                    {
                        *target.borrow_mut() = Some(path.clone());
                        if let Some(row) = row.upgrade() {
                            row.set_subtitle(&path.to_string_lossy());
                        }
                        if let Some(name) = name.upgrade() {
                            if name.text().trim().is_empty() {
                                name.set_text(
                                    &path.file_stem().unwrap_or_default().to_string_lossy(),
                                );
                            }
                            if let Some(dialog) = dialog.upgrade() {
                                dialog
                                    .set_response_enabled("import", !name.text().trim().is_empty());
                            }
                        }
                    }
                };
                if folder {
                    chooser.select_folder(parent.as_ref(), gio::Cancellable::NONE, selected);
                } else {
                    chooser.open(parent.as_ref(), gio::Cancellable::NONE, selected);
                }
            });
        }
        let row = adw::ActionRow::builder()
            .title("Banner")
            .subtitle("Use the first picture")
            .build();
        group.add(&row);
        let pick = gtk::Button::builder()
            .label("Choose")
            .valign(gtk::Align::Center)
            .build();
        row.add_suffix(&pick);
        let weak = Rc::downgrade(self);
        let target = banner.clone();
        pick.connect_clicked(move |_| {
            if let Some(ui) = weak.upgrade() {
                let row = row.downgrade();
                let target = target.clone();
                ui.choose_file("Banner", false, move |path| {
                    if let Some(row) = row.upgrade() {
                        row.set_subtitle(&path.to_string_lossy());
                    }
                    *target.borrow_mut() = Some(path);
                });
            }
        });
        let weak_dialog = dialog.downgrade();
        let target = source.clone();
        name.connect_changed(move |name| {
            if let Some(dialog) = weak_dialog.upgrade() {
                dialog.set_response_enabled(
                    "import",
                    !name.text().trim().is_empty() && target.borrow().is_some(),
                );
            }
        });
        let weak = Rc::downgrade(self);
        let grid = Rc::downgrade(grid);
        let kind = kind.to_owned();
        dialog.connect_response(None, move |_, response| {
            if response == "import"
                && let Some(ui) = weak.upgrade()
                && let Some(source) = source.borrow().clone()
            {
                let name = name.text().trim().to_owned();
                let description = description.text().trim().to_owned();
                let banner = banner.borrow().clone();
                let root = ui.shared.lock().unwrap().docs.root.clone();
                let kind = kind.clone();
                let (send, receive) = async_channel::bounded(1);
                let title = name.clone();
                std::thread::spawn(move || {
                    let _ = send.send_blocking(crate::editor_model::create_asset_pack(
                        &source,
                        &root,
                        &name,
                        &description,
                        banner.as_deref(),
                        &kind,
                    ));
                });
                let grid = grid.clone();
                let weak = Rc::downgrade(&ui);
                glib::MainContext::default().spawn_local(async move {
                    if let (Ok(result), Some(ui)) = (receive.recv().await, weak.upgrade()) {
                        match result {
                            Ok(path) => {
                                if let Some(grid) = grid.upgrade() {
                                    grid.items.borrow_mut().push(Asset {
                                        path,
                                        name: title,
                                        pack: true,
                                        origin: None,
                                    });
                                    grid.refresh();
                                }
                                ui.toast("Pack imported");
                            }
                            Err(error) => ui.toast(&error.to_string()),
                        }
                    }
                });
            }
        });
        dialog.present();
    }
    fn import_custom_asset(self: &Rc<Self>, path: &Path, grid: &Rc<Grid>) {
        let directory = self.shared.lock().unwrap().docs.root.join("assets");
        let source = path.to_owned();
        let (send, receive) = async_channel::bounded(1);
        std::thread::spawn(move || {
            let result = (|| -> Result<Asset> {
                anyhow::ensure!(media_file(&source), "Choose an image or video");
                anyhow::ensure!(
                    std::fs::metadata(&source)?.len() <= 256 * 1024 * 1024,
                    "Asset exceeds 256 MiB"
                );
                std::fs::create_dir_all(&directory)?;
                let name = source
                    .file_stem()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .into_owned();
                let extension = source.extension().unwrap_or_default().to_string_lossy();
                let temporary = tempfile::NamedTempFile::new_in(&directory)?;
                std::fs::copy(&source, temporary.path())?;
                let mut number = 0;
                loop {
                    let file_name = if number == 0 {
                        source.file_name().unwrap_or_default().to_os_string()
                    } else {
                        format!("{name} ({number}).{extension}").into()
                    };
                    let destination = directory.join(file_name);
                    match std::fs::hard_link(temporary.path(), &destination) {
                        Ok(()) => {
                            return Ok(Asset {
                                path: destination,
                                name,
                                pack: false,
                                origin: None,
                            });
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                            number += 1;
                        }
                        Err(error) => return Err(error.into()),
                    }
                }
            })();
            let _ = send.send_blocking(result);
        });
        let weak = Rc::downgrade(self);
        let grid = Rc::downgrade(grid);
        glib::MainContext::default().spawn_local(async move {
            if let (Ok(result), Some(ui), Some(grid)) =
                (receive.recv().await, weak.upgrade(), grid.upgrade())
            {
                match result {
                    Ok(asset) => {
                        grid.items.borrow_mut().push(asset);
                        grid.refresh();
                    }
                    Err(e) => ui.toast(&e.to_string()),
                }
            }
        });
    }
    fn delete_custom_asset(self: &Rc<Self>, path: &Path, grid: &Rc<Grid>) {
        let root = self.shared.lock().unwrap().docs.root.clone();
        let path = path.to_owned();
        let worker_path = path.clone();
        let (send, receive) = async_channel::bounded(1);
        std::thread::spawn(move || {
            let result = (|| -> Result<()> {
                let canonical = worker_path.canonicalize()?;
                anyhow::ensure!(
                    [root.join("assets"), root.join("Assets/AssetManager/Assets")]
                        .iter()
                        .filter_map(|root| root.canonicalize().ok())
                        .any(|root| canonical.starts_with(root)),
                    "Only custom assets can be deleted"
                );
                std::fs::remove_file(&worker_path)?;
                Ok(())
            })();
            let _ = send.send_blocking(result);
        });
        let weak = Rc::downgrade(self);
        let grid = Rc::downgrade(grid);
        glib::MainContext::default().spawn_local(async move {
            if let (Ok(result), Some(ui), Some(grid)) =
                (receive.recv().await, weak.upgrade(), grid.upgrade())
            {
                match result {
                    Ok(()) => {
                        grid.items.borrow_mut().retain(|a| a.path != path);
                        grid.refresh();
                    }
                    Err(e) => ui.toast(&e.to_string()),
                }
            }
        });
    }
    fn asset_info(&self, main: &gtk::Stack, back: &gtk::Button, asset: &Asset) {
        fn attribute(group: &adw::PreferencesGroup, title: &str, value: &str) -> gtk::Label {
            let row = adw::PreferencesRow::new();
            let content = gtk::Box::builder()
                .orientation(gtk::Orientation::Horizontal)
                .hexpand(true)
                .margin_top(15)
                .margin_bottom(15)
                .build();
            content.append(
                &gtk::Label::builder()
                    .label(title)
                    .xalign(0.)
                    .hexpand(true)
                    .margin_start(15)
                    .build(),
            );
            let label = gtk::Label::builder()
                .label(value)
                .halign(gtk::Align::Fill)
                .margin_end(15)
                .build();
            content.append(&label);
            row.set_child(Some(&content));
            group.add(&row);
            label
        }
        if let Some(old) = main.child_by_name("Asset Info") {
            main.remove(&old);
        }
        let clamp = adw::Clamp::builder().hexpand(true).margin_top(15).build();
        let content = gtk::Box::new(gtk::Orientation::Vertical, 0);
        clamp.set_child(Some(&content));
        let mut dimensions = Vec::new();
        if !asset.pack {
            let image = adw::PreferencesGroup::builder()
                .title(if video(&asset.path) { "Video" } else { "Image" })
                .build();
            dimensions.push(attribute(&image, "Resolution:", "unknown").downgrade());
            dimensions.push(attribute(&image, "Aspect Ratio:", "unknown").downgrade());
            if video(&asset.path) {
                dimensions.push(attribute(&image, "Framerate:", "unknown").downgrade());
            }
            content.append(&image);
        }
        let license = adw::PreferencesGroup::builder().title("License").build();
        let mut labels = Vec::new();
        for (title, key) in [
            ("License:", "license"),
            ("Author:", "author"),
            ("URL:", "url"),
        ] {
            labels.push((key, attribute(&license, title, "N/A").downgrade()));
        }
        let original = adw::ActionRow::builder()
            .title("Original URL:")
            .subtitle("N/A")
            .build();
        license.add(&original);
        labels.push((
            "comment",
            attribute(&license, "Comment:", "N/A").downgrade(),
        ));
        content.append(&license);
        main.add_named(&clamp, Some("Asset Info"));
        main.set_visible_child_name("Asset Info");
        back.set_visible(true);
        let (send, receive) = async_channel::bounded(1);
        let asset = asset.clone();
        std::thread::spawn(move || {
            let info = gtk::gdk_pixbuf::Pixbuf::file_info(&asset.path).map(|(_, w, h)| (w, h));
            let mut details = vec![
                "unknown".to_owned(),
                "unknown".to_owned(),
                "unknown".to_owned(),
            ];
            if let Some((w, h)) = info {
                fn gcd(a: i32, b: i32) -> i32 {
                    if b == 0 { a } else { gcd(b, a % b) }
                }
                let divisor = gcd(w, h).max(1);
                details[0] = format!("{w}x{h}");
                details[1] = format!("{}:{}", w / divisor, h / divisor);
            }
            if video(&asset.path)
                && let Ok(output) = deckard_core::desktop::run_command(
                    &[
                        "ffprobe".into(),
                        "-v".into(),
                        "error".into(),
                        "-select_streams".into(),
                        "v:0".into(),
                        "-show_entries".into(),
                        "stream=width,height,r_frame_rate".into(),
                        "-of".into(),
                        "json".into(),
                        asset.path.to_string_lossy().into_owned(),
                    ],
                    Duration::from_secs(4),
                )
                && let Ok(value) = serde_json::from_str::<Value>(&output)
            {
                let stream = &value["streams"][0];
                if let (Some(w), Some(h)) = (stream["width"].as_i64(), stream["height"].as_i64()) {
                    details[0] = format!("{w}x{h}");
                    let mut a = w;
                    let mut b = h;
                    while b != 0 {
                        (a, b) = (b, a % b);
                    }
                    details[1] = format!("{}:{}", w / a.max(1), h / a.max(1));
                }
                let rate = stream["r_frame_rate"].as_str().unwrap_or("unknown");
                details[2] = rate
                    .split_once('/')
                    .and_then(|(n, d)| Some(n.parse::<f64>().ok()? / d.parse::<f64>().ok()?))
                    .filter(|n| n.is_finite())
                    .map(|n| format!("{n:.1}"))
                    .unwrap_or_else(|| rate.to_owned());
            }
            let _ = send.send_blocking((details, attribution(&asset)));
        });
        let original = original.downgrade();
        glib::MainContext::default().spawn_local(async move {
            if let Ok((details, data)) = receive.recv().await {
                for (label, detail) in dimensions.iter().zip(details) {
                    if let Some(label) = label.upgrade() {
                        label.set_label(&detail);
                    }
                }
                for (key, label) in labels {
                    if let Some(label) = label.upgrade() {
                        label.set_label(if key == "license" {
                            data["license"]
                                .as_str()
                                .or_else(|| data["name"].as_str())
                                .unwrap_or("N/A")
                        } else {
                            data[key]
                                .as_str()
                                .or_else(|| match key {
                                    "author" => data["copyright"].as_str(),
                                    "url" => data["license-url"].as_str(),
                                    _ => None,
                                })
                                .unwrap_or("N/A")
                        });
                    }
                }
                if let Some(row) = original.upgrade() {
                    let url = data["original-url"]
                        .as_str()
                        .or_else(|| data["original_url"].as_str())
                        .unwrap_or("N/A");
                    row.set_subtitle(url);
                    if url != "N/A" {
                        let url = url.to_owned();
                        let open = gtk::Button::from_icon_name("web-browser-symbolic");
                        open.set_valign(gtk::Align::Center);
                        open.connect_clicked(move |_| {
                            gtk::UriLauncher::new(&url).launch(
                                None::<&gtk::Window>,
                                gio::Cancellable::NONE,
                                |_| {},
                            )
                        });
                        row.add_suffix(&open);
                    }
                }
            }
        });
    }
}
