Deckard 0.9.1 restores the previous GTK4/libadwaita editor, implemented entirely in Rust.

The original window proportions, header bars, state selector, selected-input preview, key grid, touch strip, dials and grouped settings return. GTK dialogs cover pages, assets, store, device settings, OBS connections, automatic switching and migration. All 56 native replacements for the common upstream plugin actions remain available; executable native plugins receive GTK forms from their field schemas.

The selected preview opens the asset picker, with hover hints and image removal. Right-click input menus provide copy, cut, paste and clear. Closing to the tray releases the window surface and renderer, which are recreated on reopening.

Saved edits retain unknown JSON and the selected input at the time of editing. Pending writes drain on exit. Preview textures share engine RGB buffers, update only when pixels change and are released while hidden. Searchable asset pages keep 40 scaled thumbnails at a time. Touchscreen state backgrounds and animations now reach the native strip.

Installers target CachyOS x86-64, Fedora 44 stable and Ubuntu 26.04 LTS on x86-64 and ARM64. GTK, libadwaita, FFmpeg and desktop helpers come from the package manager. No Python runtime or system-library bundle is included. The old AppImage/portable releases remain available under v0.8.0.

Each target runs native tests, GTK interaction checks, Clippy, formatting, rendering/audio diagnostics and installation/startup checks on a fresh distribution image before publication. Fake-device throughput does not establish physical USB/LCD performance.
