Deckard 0.10.0 reconstructs the direct upstream GTK4/libadwaita interface in Rust.

The original stylesheet, window proportions, headers, key/dial/touchstrip selection, state controls, layout/background/label editors, action chooser, event assignments, asset manager, page manager and seven settings pages return. Common actions use their original names and artwork. All 56 common-plugin replacements run natively, and the replacement plugin interface supplies settings, icons and colors without Python widgets.

Crop dragging previews immediately and commits through the serialized save worker. Backgrounds, slideshows, saturation, rolling labels, idle animation pause, tray visibility, fake decks and remote decks use native implementations. Unknown document fields survive edits. Regression checks cover named-page saves, pause/resume, action ownership, native resolution and GTK interactions.

Matched captures compare the real direct upstream with the Rust application under identical GTK/font/fixture conditions. Several screens are pixel-identical. Small deck-text raster differences and runtime-specific identifiers are documented; this release does not claim universal pixel identity across devices, fonts or desktop themes.

Installers target CachyOS x86-64, Fedora 44 stable and Ubuntu 26.04 LTS on x86-64 and ARM64. GTK, libadwaita, FFmpeg and helpers come from the package manager. No Python runtime or system-library bundle is included. Every target runs native tests, GTK checks, Clippy and fresh-image installation checks before publication.
