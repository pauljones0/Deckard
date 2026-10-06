Deckard 0.10.0 reconstructs the direct upstream GTK4/libadwaita interface in Rust.

The original stylesheet, window proportions, headers, key/dial/touchstrip selection, state controls, layout/background/label editors, action chooser, event assignments, asset manager, page manager and seven settings pages return. Common actions use their original names and artwork. All 56 common-plugin replacements run natively, and the replacement plugin interface supplies settings, icons and colors without Python widgets.

Crop dragging previews immediately and commits through the serialized save worker. Backgrounds, slideshows, saturation, rolling labels, idle animation pause, tray visibility, fake decks and remote decks use native implementations. Unknown document fields survive edits. Regression checks cover named-page saves, pause/resume, action ownership, native resolution and GTK interactions.

Matched captures compare the real direct upstream with the Rust application under identical GTK/font/fixture conditions. Several screens are pixel-identical. Small deck-text raster differences and runtime-specific identifiers are documented; this release does not claim universal pixel identity across devices, fonts or desktop themes.

Installers target CachyOS x86-64, Fedora 44 stable and Ubuntu 26.04 LTS on x86-64 and ARM64. GTK, libadwaita, FFmpeg and helpers come from the package manager. No Python runtime or system-library bundle is included. Every target runs native tests, GTK checks, Clippy and fresh-image installation checks before publication.

Fresh three-trial comparisons measure 52% less editor CPU and 31% less RAM than the direct upstream with eight animated Plus keys. Rust uses 1.83% of one CPU core / 214 MiB PSS with the editor open, and 0.93% / 56 MiB in daemon mode. Separate checks deliver about 100 FPS on all eight native 120×120 keys. Measurements use fake devices on a 9800X3D / RTX 5070 under GPU load; USB/LCD and ARM performance are unmeasured.
