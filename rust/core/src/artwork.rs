//! Audited upstream artwork, embedded in the native application.
pub(crate) fn bytes(path: &str) -> Option<&'static [u8]> {
    Some(match path {
        "builtin://OSPlugin/click.png" => {
            include_bytes!("../../../Assets/NativeActions/OSPlugin/click.png")
        }
        "builtin://OSPlugin/controller.png" => {
            include_bytes!("../../../Assets/NativeActions/OSPlugin/controller.png")
        }
        "builtin://OSPlugin/hourglass_empty-inv.png" => {
            include_bytes!("../../../Assets/NativeActions/OSPlugin/hourglass_empty-inv.png")
        }
        "builtin://OSPlugin/joystick.png" => {
            include_bytes!("../../../Assets/NativeActions/OSPlugin/joystick.png")
        }
        "builtin://OSPlugin/keyboard.png" => {
            include_bytes!("../../../Assets/NativeActions/OSPlugin/keyboard.png")
        }
        "builtin://OSPlugin/mouse.png" => {
            include_bytes!("../../../Assets/NativeActions/OSPlugin/mouse.png")
        }
        "builtin://OSPlugin/terminal.png" => {
            include_bytes!("../../../Assets/NativeActions/OSPlugin/terminal.png")
        }
        "builtin://OSPlugin/web.png" => {
            include_bytes!("../../../Assets/NativeActions/OSPlugin/web.png")
        }
        "builtin://DeckPlugin/decrease_brightness.png" => {
            include_bytes!("../../../Assets/NativeActions/DeckPlugin/decrease_brightness.png")
        }
        "builtin://DeckPlugin/folder.png" => {
            include_bytes!("../../../Assets/NativeActions/DeckPlugin/folder.png")
        }
        "builtin://DeckPlugin/go_to_previous_page.png" => {
            include_bytes!("../../../Assets/NativeActions/DeckPlugin/go_to_previous_page.png")
        }
        "builtin://DeckPlugin/increase_brightness.png" => {
            include_bytes!("../../../Assets/NativeActions/DeckPlugin/increase_brightness.png")
        }
        "builtin://DeckPlugin/light.png" => {
            include_bytes!("../../../Assets/NativeActions/DeckPlugin/light.png")
        }
        "builtin://DeckPlugin/sidebar.png" => {
            include_bytes!("../../../Assets/NativeActions/DeckPlugin/sidebar.png")
        }
        "builtin://DeckPlugin/sleep.png" => {
            include_bytes!("../../../Assets/NativeActions/DeckPlugin/sleep.png")
        }
        "builtin://MediaPlugin/idle.png" => {
            include_bytes!("../../../Assets/NativeActions/MediaPlugin/idle.png")
        }
        "builtin://MediaPlugin/next.png" => {
            include_bytes!("../../../Assets/NativeActions/MediaPlugin/next.png")
        }
        "builtin://MediaPlugin/pause.png" => {
            include_bytes!("../../../Assets/NativeActions/MediaPlugin/pause.png")
        }
        "builtin://MediaPlugin/play.png" => {
            include_bytes!("../../../Assets/NativeActions/MediaPlugin/play.png")
        }
        "builtin://MediaPlugin/previous.png" => {
            include_bytes!("../../../Assets/NativeActions/MediaPlugin/previous.png")
        }
        "builtin://MediaPlugin/stop.png" => {
            include_bytes!("../../../Assets/NativeActions/MediaPlugin/stop.png")
        }
        _ => return None,
    })
}
