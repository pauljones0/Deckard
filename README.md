# Deckard

Stream Deck controller for Linux. Rust engine. GTK4/libadwaita editor. No Python runtime.

![Rust GTK editor](docs/images/native-editor.png)

## Download

**[Deckard 0.10.0](https://github.com/pauljones0/Deckard/releases/tag/v0.10.0)**

| Linux | x86-64 · Intel/AMD | ARM64 |
| --- | --- | --- |
| CachyOS | [pacman package](https://github.com/pauljones0/Deckard/releases/download/v0.10.0/deckard-0.10.0-cachyos-x86_64.pkg.tar.zst) | — |
| Fedora 44 stable | [RPM](https://github.com/pauljones0/Deckard/releases/download/v0.10.0/deckard-0.10.0-fedora44-x86_64.rpm) | [RPM](https://github.com/pauljones0/Deckard/releases/download/v0.10.0/deckard-0.10.0-fedora44-aarch64.rpm) |
| Ubuntu 26.04 LTS | [DEB](https://github.com/pauljones0/Deckard/releases/download/v0.10.0/deckard-0.10.0-ubuntu26-x86_64.deb) | [DEB](https://github.com/pauljones0/Deckard/releases/download/v0.10.0/deckard-0.10.0-ubuntu26-aarch64.deb) |

Open the DEB/RPM in your software installer. CachyOS: `sudo pacman -U ./deckard-0.10.0-cachyos-x86_64.pkg.tar.zst`. Your package manager installs the libraries and helpers. Reconnect your deck, then launch Deckard. [Builds and setup](docs/linux-builds.md).

**Store → Plugins → Rust Starter** installs a ready-made page.

## Features

- The direct upstream’s GTK layout, widgets and stylesheet, rebuilt in Rust.
- OS, media, OBS and volume controls: 56 built-in actions replacing the common upstream plugins.
- Native device resolution. Auto FPS follows the source, up to 120 FPS, adapting to USB throughput.
- Animated wallpapers, asset browsing, automatic page switching and close-to-tray.

Supports Elgato, Mirabox and Ulanzi. [Models](docs/rendering-endpoints.md) · [Usage](docs/user-guide.md).

Python plugins use a new native interface. `deckard --migrate-legacy-actions` converts supported saved actions. [Migration](docs/plugin-migration.md) · [Plugin API](docs/native-plugins.md).

## Less CPU. Less RAM.

Eight animated Plus keys, 10 FPS source. **52% less editor CPU and 31% less RAM than the direct upstream.**

| Mode | StreamController CPU / RAM | nazbert/Deckard CPU / RAM | Rust GTK CPU / RAM |
| --- | ---: | ---: | ---: |
| Editor | 7.27% / 345 MiB | 3.80% / 311 MiB | **1.83% / 214 MiB** |
| Daemon only | 4.53% / 203 MiB | 2.50% / 200 MiB | **0.93% / 56 MiB** |

Rust 0.10.0; medians of three trials on a 9800X3D / RTX 5070 under GPU load. CPU is percent of one core; RAM is process-tree PSS. Fake devices; USB/LCD and ARM performance unmeasured. [Method, ranges and 100 FPS results](docs/performance.md).

## Build

`scripts/build-linux.sh` builds distro-native packages. [Build details](docs/linux-builds.md) · [GTK editor](docs/ui-restoration.md) · [All docs](docs/README.md).

Based on [StreamController](https://github.com/StreamController/StreamController) by Core447 and [nazbert/Deckard](https://github.com/nazbert/Deckard). [Upstream audit](docs/upstream-audit.md). [GPL-3.0-or-later](LICENSE).
