# GTK editor, in Rust

Deckard uses GTK4 and libadwaita through their Rust bindings. The previous layout, stylesheet and controls are restored; the application and executable plugins use Rust. No Python interpreter is loaded or packaged.

![Rust GTK editor](images/native-editor.png)

The editor retains the paired header bars, 40% left sidebar, state switcher, selected-input preview, grouped controls, action chooser and dialogs. Keys display at 75 logical pixels; the selected preview is 175. Device frames retain their native resolution independently of those display sizes.

Pages, assets, store, device settings, OBS connections, automatic switching, migration and optional AI review use GTK controls. Layout, background, labels and action properties save through the Rust engine. Unknown JSON fields survive edits. The common upstream actions and native plugin field schemas supply their settings forms.

## Rendering and lifecycle

GTK stays on the main thread. A bounded worker serializes saved edits, carrying the selected device, page, input and state with each command. Network requests run separately. Structural settings changes update their lists after persistence; closing the application drains pending edits.

Previews wrap the engine's shared RGB buffers in `glib::Bytes` and `gdk::MemoryTexture`. They do not decode device JPEGs. Unchanged textures are reused, only the selected device receives preview updates, and closing to the tray releases textures. Reopening restores frames. The preview cadence is capped around 60 Hz; device rendering and USB pacing retain their independent source/model rates.

Asset browsing uses search and pages of 40 scaled thumbnails. GTK, GLib, Pango, Cairo and libadwaita are supplied by the distribution. egui, eframe, wgpu and rfd were removed from the launcher.

## Verification

The GTK workflow test exercises key/dial selection, native frame dimensions, saved labels, unknown-field preservation, state creation/removal, action selection, device switching, settings lists, OBS profile creation/deletion, slideshow deletion, dialogs and close/reopen behavior. It verifies GTK/libadwaita mappings and the absence of libpython.

```sh
dbus-run-session -- xvfb-run -a env GTK_A11Y=none GSK_RENDERER=cairo G_DEBUG=fatal-criticals \
  cargo test -p deckard gtk_editor_workflows --locked -- --ignored --test-threads=1
```

This runs on X11 in every distro builder; a private Wayland session covers local interactions and screenshots. Existing engine, IPC, plugin and rendering tests remain. Physical USB devices and every desktop portal backend require testing on their respective systems.

[Source](../rust/launcher/src/ui/mod.rs) · [Previous GTK reference](images/gtk-editor-reference.png) · [Packages](linux-builds.md) · [Measurements](performance.md)
