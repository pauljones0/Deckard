# Deckard

Stream Deck controller for Linux. Written in Rust. No Python or GTK required.

![Deckard editor](docs/images/native-editor.png)

## Download

**[Deckard 0.8.0](https://github.com/pauljones0/Deckard/releases/tag/v0.8.0)** · x86-64 for Intel/AMD, ARM64 for ARM.

| Format | x86-64 | ARM64 | Run it |
| --- | --- | --- | --- |
| AppImage | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.8.0/deckard-0.8.0-x86_64.AppImage) | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.8.0/deckard-0.8.0-aarch64.AppImage) | Allow execution in file properties, then double-click. |
| DEB · Debian / Ubuntu / Mint | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.8.0/deckard-0.8.0-x86_64.deb) | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.8.0/deckard-0.8.0-aarch64.deb) | Install with your software installer. |
| RPM · Fedora | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.8.0/deckard-0.8.0-x86_64.rpm) | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.8.0/deckard-0.8.0-aarch64.rpm) | Install with your software installer. |
| Portable | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.8.0/deckard-0.8.0-x86_64.tar.gz) | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.8.0/deckard-0.8.0-aarch64.tar.gz) | Extract and open `deckard/bin/deckard`. |
| Arch | [PKGBUILD](packaging/aur/deckard-git/PKGBUILD) | Same | Build with `makepkg -si`. |

Requires glibc 2.35+ and Vulkan or OpenGL drivers. FFmpeg and helpers are bundled. [Troubleshooting](docs/linux-builds.md).

Connect your deck, launch Deckard, and choose an action. **Store → Pages → Rust Starter** installs a ready-made page. Portable/AppImage users: **Settings → Enable USB access**, then reconnect the deck.

## Features

- Keys, dials, touchscreens, pages, labels and animated wallpapers.
- OS, media, OBS and volume controls: 56 built-in actions replacing the common upstream plugins.
- Native device resolution. Auto FPS follows the source, up to 120 FPS, adapting to USB throughput.

Supports Elgato models plus Mirabox and Ulanzi. [Models and resolutions](docs/rendering-endpoints.md).

Python plugins need native replacements. Saved pages can be migrated through **Settings → Inspect legacy actions**. [Migration](docs/plugin-migration.md) · [Plugin API](docs/native-plugins.md).

## Less CPU. Less RAM.

Measured upstream comparison with eight animated Plus keys. **Rust 0.5.0** results; newer releases are measured separately below.

| Mode | StreamController CPU / RAM | nazbert/Deckard CPU / RAM | Rust 0.5.0 CPU / RAM |
| --- | ---: | ---: | ---: |
| Editor | 6.33% / 339 MiB | 3.80% / 303 MiB | **1.83% / 176 MiB** |
| Background | 4.07% / 197 MiB | 2.23% / 195 MiB | **0.80% / 16 MiB** |

**0.8.0: under 1% of a CPU core at 100 FPS.** Compared with 0.7.0 at the same frame rates:

| Background animation | CPU · 0.7 → 0.8 | CPU saved | RAM MiB · 0.7 → 0.8 |
| --- | ---: | ---: | ---: |
| Plus · 8 keys · 100 FPS | 1.40% → **0.93%** | **33%** | 17.7 → 17.8 |
| XL · 32 keys · 50 FPS | 2.33% → **1.67%** | **29%** | 28.0 → 28.1 |

CPU is percent of one core; RAM is process-tree PSS. Three trials per case on a Ryzen 9800X3D / RTX 5070, using fake devices. USB/LCD and ARM performance remain unmeasured. Latest RAM use is effectively unchanged. [Full results and method](docs/performance.md).

## Build

`scripts/build-rust.sh` builds and tests with Rust 1.99, CMake, NASM and the required development libraries. `scripts/build-linux.sh` builds Linux packages using Docker. [Build details](docs/linux-builds.md).

[Usage and CLI](docs/user-guide.md) · [All docs](docs/README.md)

Based on [StreamController](https://github.com/StreamController/StreamController) by Core447 and [nazbert/Deckard](https://github.com/nazbert/Deckard). [Upstream audit](docs/upstream-audit.md). Licensed [GPL-3.0-or-later](LICENSE).
