# Direct-upstream GTK interface, in Rust

The reference is [nazbert/Deckard `a3609c7d`](https://github.com/nazbert/Deckard/tree/a3609c7de63347dbc7e031926826898950600c83). Deckard uses its stylesheet and reconstructs its GTK4/libadwaita widgets in Rust. The engine, editor and shipped common-plugin replacements run without Python. GTK, GLib, Pango, Cairo and libadwaita are system libraries.

![Rust editor](images/native-editor.png)

The paired headers, 40% sidebar, state selector, selected-input preview, key grid, dials, touchstrip, grouped controls, action chooser and event assignments follow the reference. The asset manager, page manager, deck settings and seven preferences pages use the same layout and captions. Native plugin settings use declared fields, icons and colors instead of Python widget factories.

## What was checked

`benchmarks/ui_parity.py` launches the actual reference and Rust applications with isolated data, identical fonts, GTK renderer, window sizes and fake Plus fixtures. It records screenshots, mapped widget bounds and pixel differences. The populated chooser loads all five reference plugins, using a private null-sink audio server and the reference OBS dependencies; a failed plugin startup cannot silently pass that comparison.

UI settings, performance settings, custom assets, icon-pack chooser, page manager and the no-device screen match pixel-for-pixel in the recorded fixture. The main screen differs in about 0.30% of pixels, concentrated in deck-text rendering. Python/Pillow and Rust/ab_glyph rasterize those labels differently. Fake serials and isolated data paths also differ. These checks establish the recorded layouts, not universal pixel identity for every font, plugin panel, theme or system library version. [Paired screenshots and widget bounds](../benchmarks/results/2026-10-06/gtk-0.10/ui) · [Pixel reports](../benchmarks/results/2026-10-06/gtk-0.10/ui-comparison.json) · [Reference screenshot](images/gtk-editor-reference.png).

The GTK workflow test covers actual selection, edits, state changes, action selection, device switching, settings, OBS profile creation/deletion, slideshows, dialogs and close/reopen. It checks native frame dimensions, unknown-field preservation and GTK/libadwaita mappings without libpython.

```sh
dbus-run-session -- xvfb-run -a -s '-screen 0 1800x1000x24' \
  env GTK_A11Y=none GSK_RENDERER=cairo G_DEBUG=fatal-criticals \
  cargo test -p deckard gtk_editor_workflows --locked -- --ignored --test-threads=1
```

## Resource use

GTK stays on the main thread. Saved edits use a serialized worker that captures the document/input/state being edited. Crop dragging previews in memory; committed edits retain unknown fields. Native frame buffers are shared with GTK textures, unchanged previews are reused and only the selected device is painted. Closing to the tray releases textures and the window surface. Asset pages retain at most 50 scaled thumbnails. Static label rasters, rolling labels, media and encoded frames have bounded caches. Idle animation pause stops GIF, video and slideshow updates without a resume burst.

[Packages](linux-builds.md) · [Measurements](performance.md) · [Source](../rust/launcher/src/ui/mod.rs)
