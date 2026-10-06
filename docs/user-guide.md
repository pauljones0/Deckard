# Using Deckard

Connect your deck and launch Deckard. Select an input, choose an action and edit its labels or image. In **Store → Pages**, load the catalog and install **Rust Starter** for a ready-made page. Select that page in the editor; its native plugin is included. **Settings** includes autostart and an **Enable USB access** button for portable installations. DEB/RPM installers install the USB rules automatically; reconnect the deck if permission errors persist. The portable access helper requires administrator authentication.

## What the Rust port includes

- Original, Original V2, MK.2, Mini, XL, Plus, Plus XL, Neo, Pedal, Studio, Mirabox 293S and Ulanzi D200 device definitions, with fake models for previews and tests.
- One hardware writer per deck, input polling between image writes, latest-frame coalescing, image identities, immutable byte-budgeted caches, bounded on-demand glyphs, shared configuration snapshots, reuse of unchanged keys/strips and native SIMD JPEG encoding.
- Correct GIF transparency, disposal and individual frame delays; video wallpapers; slideshow order and shuffle; per-image viewport settings; calibrated Plus wallpaper continuity and rotated touchscreen layouts.
- Pages with multiple persistent input states, sticky inputs across pages, sticky indicators, page search, device names, action drag-and-drop, brightness, screensavers and session-lock handling.
- Automatic page switching on supported Hyprland, Sway, KDE, GNOME, X11 and Mango desktops; logind lock detection also supports sessions such as Niri. KDE needs `kdotool`; GNOME needs the [StreamController shell extension](https://extensions.gnome.org/extension/6871/streamcontroller-integration/).
- Atomic JSON saves, corrupt-JSON recovery, same-user single-instance control, USB reconnects, bounded subprocesses, transactional native installs and page bundles containing media and available native plugin executables.
- Native plugin/page/icon catalogs, branch filtering, source links, custom icon packs, lazy GUI startup, tray Open/Restart/Quit and an expanded CLI.
- Optional AI page proposals through a configurable endpoint. AI is off by default; proposals are reviewed before saving and command/text/hotkey actions require explicit permission in the UI.

**All 56 actions in the five recommended plugins have native implementations.** OSPlugin, DeckPlugin, MediaPlugin, OBSPlugin and VolumeMixer now include live readouts, graphs, artwork, OBS meters/status, repeating input, periodic commands and timed returns. These run in Rust. Open **Settings → Inspect legacy actions → Migrate supported actions** to translate saved actions with exact backups. [Action coverage, setup and migration](plugin-migration.md).

**Plugin API changed:** Python/GTK plugins do not run. Existing page JSON and unknown plugin settings are retained. See the [new executable plugin interface](native-plugins.md) and [working Rust example](../rust/plugin-example). This is a breaking application port, with a new egui interface; it does not reproduce every GTK dialog or Python plugin integration.

Tests cover native rendering, fake devices, persistence, IPC, plugin failure boundaries and isolated GUI startup. Physical USB hardware and every desktop/compositor combination still need testing on their respective machines; the old fork's multi-day Python soak results do not establish Rust soak coverage.

## Data and migration

Native data lives in `${XDG_DATA_HOME:-~/.local/share}/deckard`; `--data /path` selects another directory. Existing Deckard data at that location is reused. On first launch, previous Deckard/StreamController Flatpak data is copied if the native directory does not exist, without deleting the original. Device settings, default pages, backgrounds and brightness migrate into `settings/native.json`; page documents retain unknown fields. Symbolic links in a copied legacy tree are skipped. Supported actions can be converted with the reversible migration described above; unsupported actions remain intact.

## CLI

```sh
deckard --doctor
deckard --daemon-only
deckard --list-devices
deckard --list-pages
deckard --inspect-legacy-actions
deckard --migrate-legacy-actions
deckard --create-page Work
deckard --export-page Work ~/Work.deckard.zip
deckard --export-all ~/deckard-pages
deckard --rpc '{"method":"change-page","params":{"serial":"YOUR_SERIAL","page":"Work"}}'
deckard --close-running
```

For a hardware-free editor: `deckard --skip-load-hardware-decks --fake-deck-model plus`. Available models: `original`, `original-v2`, `mk2`, `mini`, `xl`, `plus`, `plus-xl`, `neo`, `pedal`, `studio`, `mirabox-293s`, `ulanzi-d200`; hardware revisions also have presets `xl-v2`, `mk2-scissor`, `mini-mk2`, `mini-discord`, `mini-module`, `mk2-module`, `xl-module`. Use `--help` for state, action, image, label and input-emulation commands. Device commands connect to the running instance; page editing and exports also work offline.

## Build from source

Use Rust **1.99**, C/C++ build tools, CMake, NASM and the platform development libraries listed in [the Dockerfile](../packaging/linux/Dockerfile). `scripts/build-rust.sh` builds the native application and runs tests. `scripts/build-linux.sh` needs only Docker on the host and builds all four Linux formats for the host architecture. GitHub Actions builds both architectures on native runners and publishes checked artifacts for version tags.
