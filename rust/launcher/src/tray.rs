use deckard_core::engine::Shared;
use ksni::{Tray, blocking::TrayMethods, menu::StandardItem};
struct DeckardTray {
    shared: Shared,
    visible: bool,
}
static SERVICE: std::sync::OnceLock<ksni::blocking::Handle<DeckardTray>> =
    std::sync::OnceLock::new();
pub fn set_visible(visible: bool) {
    if let Some(handle) = SERVICE.get() {
        handle.update(|tray| tray.visible = visible);
    }
}
impl Tray for DeckardTray {
    fn status(&self) -> ksni::Status {
        if self.visible {
            ksni::Status::Active
        } else {
            ksni::Status::Passive
        }
    }
    fn id(&self) -> String {
        "deckard".into()
    }
    fn title(&self) -> String {
        "Deckard".into()
    }
    fn icon_name(&self) -> String {
        "io.github.nazbert.Deckard".into()
    }
    fn icon_pixmap(&self) -> Vec<ksni::Icon> {
        let Ok(image) = image::load_from_memory(include_bytes!(
            "../../../Assets/icons/hicolor/512x512/apps/io.github.nazbert.Deckard.png"
        )) else {
            return vec![];
        };
        let image = image
            .resize_exact(32, 32, image::imageops::FilterType::Triangle)
            .to_rgba8();
        let data = image
            .pixels()
            .flat_map(|p| [p[3], p[0], p[1], p[2]])
            .collect();
        vec![ksni::Icon {
            width: 32,
            height: 32,
            data,
        }]
    }
    fn activate(&mut self, _x: i32, _y: i32) {
        self.shared.lock().unwrap().show_window = true;
    }
    fn menu(&self) -> Vec<ksni::MenuItem<Self>> {
        vec![
            StandardItem {
                label: "Open Deckard".into(),
                activate: Box::new(|tray: &mut Self| {
                    tray.shared.lock().unwrap().show_window = true
                }),
                ..Default::default()
            }
            .into(),
            StandardItem {
                label: "Open data folder".into(),
                activate: Box::new(|tray: &mut Self| {
                    let path = tray.shared.lock().unwrap().docs.root.clone();
                    let _ = std::process::Command::new("xdg-open").arg(path).spawn();
                }),
                ..Default::default()
            }
            .into(),
            StandardItem {
                label: "Restart".into(),
                activate: Box::new(|tray: &mut Self| {
                    let mut engine = tray.shared.lock().unwrap();
                    engine.restart = true;
                    engine.quit = true;
                }),
                ..Default::default()
            }
            .into(),
            StandardItem {
                label: "Quit".into(),
                activate: Box::new(|tray: &mut Self| tray.shared.lock().unwrap().quit = true),
                ..Default::default()
            }
            .into(),
        ]
    }
}
pub struct Handle(ksni::blocking::Handle<DeckardTray>);
impl Drop for Handle {
    fn drop(&mut self) {
        self.0.shutdown().wait();
    }
}
pub fn start(shared: Shared) -> Option<Handle> {
    let visible = shared.lock().unwrap().docs.settings["ui"]["tray-icon"]
        .as_bool()
        .unwrap_or(true);
    let handle = DeckardTray { shared, visible }.spawn().ok()?;
    let _ = SERVICE.set(handle.clone());
    Some(Handle(handle))
}
