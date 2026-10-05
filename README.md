# Deckard

**A native Rust application for Stream Decks and compatible controllers on Linux.** Configure keys, dials, touchscreen actions, pages, animated wallpapers and executable plugins without installing Python or GTK.

Based on [StreamController](https://github.com/StreamController/StreamController) by [Core447](https://github.com/Core447), with the extensive fixes and improvements from [nazbert/Deckard](https://github.com/nazbert/Deckard). The [upstream audit](docs/upstream-audit.md) records both source revisions and every subsequent original-upstream commit reviewed for this port.

![Native Deckard editor](docs/images/native-editor.png)

## Download and play

[**Download Deckard 0.4.0**](https://github.com/pauljones0/Deckard/releases/tag/v0.4.0). Pick your format and architecture below. Intel/AMD PCs use **x86-64**; 64-bit ARM computers use **ARM64**.

| Linux build | x86-64 | ARM64 | Start it |
| --- | --- | --- | --- |
| AppImage | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.4.0/deckard-0.4.0-x86_64.AppImage) | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.4.0/deckard-0.4.0-aarch64.AppImage) | In file properties, allow executing the file; double-click. |
| Ubuntu / Debian / Mint / Pop!_OS DEB | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.4.0/deckard-0.4.0-x86_64.deb) | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.4.0/deckard-0.4.0-aarch64.deb) | Open in your software installer, install, then launch **Deckard**. |
| Fedora / compatible RPM systems | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.4.0/deckard-0.4.0-x86_64.rpm) | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.4.0/deckard-0.4.0-aarch64.rpm) | Open in your software installer, install, then launch **Deckard**. |
| Portable archive | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.4.0/deckard-0.4.0-x86_64.tar.gz) | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.4.0/deckard-0.4.0-aarch64.tar.gz) | Extract; open `deckard/bin/deckard`. Keep the extracted directory together. |
| Arch / EndeavourOS / Manjaro | [PKGBUILD](packaging/aur/deckard-git/PKGBUILD) | Same PKGBUILD | `makepkg -si`; launch **Deckard**. This option builds from source. |

Bundles include FFmpeg, input helpers and the native example plugin. They require a Linux desktop with **glibc 2.35 or newer** and working OpenGL/EGL drivers: for example Ubuntu 22.04+, Debian 12+, Mint 21+ and current Fedora/Arch. They do not support Alpine/musl. File dialogs use your desktop's XDG portal. [Build details, checksums and troubleshooting](docs/linux-builds.md).

Connect your deck and launch Deckard. Select an input, choose an action and edit its labels or image. In **Store → Pages**, load the catalog and install **Rust Starter** for a ready-made page. Select that page in the editor; its native plugin is included. **Settings** includes autostart and an **Enable USB access** button for portable installations. DEB/RPM installers install the USB rules automatically; reconnect the deck if permission errors persist. Authentication is required only for installing system USB rules.

## What the Rust port includes

- Original, Original V2, MK.2, Mini, XL, Plus, Plus XL, Neo, Pedal, Studio, Mirabox 293S and Ulanzi D200 device definitions, with fake models for previews and tests.
- One hardware writer per deck, input polling between image writes, latest-frame coalescing, image identities, immutable byte-budgeted caches, cached glyphs and native SIMD JPEG encoding.
- Correct GIF transparency, disposal and individual frame delays; video wallpapers; slideshow order and shuffle; per-image viewport settings; calibrated Plus wallpaper continuity and rotated touchscreen layouts.
- Pages with multiple persistent input states, sticky inputs across pages, sticky indicators, page search, device names, action drag-and-drop, brightness, screensavers and session-lock handling.
- Automatic page switching on supported Hyprland, Sway, KDE, GNOME, X11 and Mango desktops; logind lock detection also supports sessions such as Niri. KDE needs `kdotool`; GNOME needs the [StreamController shell extension](https://extensions.gnome.org/extension/6871/streamcontroller-integration/).
- Atomic JSON saves, corrupt-JSON recovery, same-user single-instance control, USB reconnects, bounded subprocesses, transactional native installs and page bundles containing media and available native plugin executables.
- Native plugin/page/icon catalogs, branch filtering, source links, custom icon packs, lazy GUI startup, tray Open/Restart/Quit and an expanded CLI.
- Optional AI page proposals through a configurable endpoint. AI is off by default; proposals are reviewed before saving and command/text/hotkey actions require explicit permission in the UI.

**Plugin API changed:** Python/GTK plugins do not run. Existing page JSON and unknown plugin settings are retained, but old actions must be replaced with built-in actions or native plugins. See the [new executable plugin interface](docs/native-plugins.md) and [working Rust example](rust/plugin-example). This is a breaking application port, with a new egui interface; it does not reproduce every GTK dialog or Python plugin integration.

Tests cover native rendering, fake devices, persistence, IPC, plugin failure boundaries and isolated GUI startup. Physical USB hardware and every desktop/compositor combination still need testing on their respective machines; the old fork's multi-day Python soak results do not establish Rust soak coverage.

## Data and migration

Native data lives in `${XDG_DATA_HOME:-~/.local/share}/deckard`; `--data /path` selects another directory. Existing Deckard data at that location is reused. On first launch, previous Deckard/StreamController Flatpak data is copied if the native directory does not exist, without deleting the original. Device settings, default pages, backgrounds and brightness migrate into `settings/native.json`; page documents retain unknown fields. Symbolic links in a copied legacy tree are skipped. Keep a backup before replacing old plugin actions.

## CLI

```sh
deckard --doctor
deckard --daemon-only
deckard --list-devices
deckard --list-pages
deckard --create-page Work
deckard --export-page Work ~/Work.deckard.zip
deckard --export-all ~/deckard-pages
deckard --rpc '{"method":"change-page","params":{"serial":"YOUR_SERIAL","page":"Work"}}'
deckard --close-running
```

For a hardware-free editor: `deckard --skip-load-hardware-decks --fake-deck-model plus`. Available models: `original`, `original-v2`, `mk2`, `mini`, `xl`, `plus`, `plus-xl`, `neo`, `pedal`; hardware revisions also have presets `xl-v2`, `mk2-scissor`, `mini-mk2`, `mini-discord`, `mini-module`, `mk2-module`, `xl-module`. Use `--help` for state, action, image, label and input-emulation commands. Device commands connect to the running instance; page editing and exports also work offline.

## Build from source

Use Rust **1.99**, C/C++ build tools, CMake, NASM and the platform development libraries listed in [the Dockerfile](packaging/linux/Dockerfile). `scripts/build-rust.sh` builds the native application and runs tests. `scripts/build-linux.sh` needs only Docker on the host and builds all four Linux formats for the host architecture. GitHub Actions builds both architectures on native runners and publishes checked artifacts for version tags.

The previous Python implementation, tests and packaging remain in the repository as historical reference. They are excluded from native runtime bundles. The [old README](docs/legacy-python-readme.md) documents that retired application; its installation instructions do not apply to this Rust release.

GPL-3.0-or-later. Original project and fork attribution are retained. Bundles include dependency and font licenses; see [source and license information](docs/linux-builds.md#source-and-licenses).
