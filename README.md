# Deckard

**A native Rust application for Stream Decks and compatible controllers on Linux.** Configure keys, dials, touchscreen actions, pages, animated wallpapers and executable plugins without installing Python or GTK.

Based on [StreamController](https://github.com/StreamController/StreamController) by [Core447](https://github.com/Core447), with the extensive fixes and improvements from [nazbert/Deckard](https://github.com/nazbert/Deckard). The [upstream audit](docs/upstream-audit.md) records both source revisions and every subsequent original-upstream commit reviewed for this port.

![Native Deckard editor](docs/images/native-editor.png)

## Download and play

[**Download Deckard 0.7.0**](https://github.com/pauljones0/Deckard/releases/tag/v0.7.0). Pick your format and architecture below. Intel/AMD PCs use **x86-64**; 64-bit ARM computers use **ARM64**.

| Linux build | x86-64 | ARM64 | Start it |
| --- | --- | --- | --- |
| AppImage | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.7.0/deckard-0.7.0-x86_64.AppImage) | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.7.0/deckard-0.7.0-aarch64.AppImage) | In file properties, allow executing the file; double-click. |
| Ubuntu / Debian / Mint / Pop!_OS DEB | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.7.0/deckard-0.7.0-x86_64.deb) | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.7.0/deckard-0.7.0-aarch64.deb) | Open in your software installer, install, then launch **Deckard**. |
| Fedora / compatible RPM systems | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.7.0/deckard-0.7.0-x86_64.rpm) | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.7.0/deckard-0.7.0-aarch64.rpm) | Open in your software installer, install, then launch **Deckard**. |
| Portable archive | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.7.0/deckard-0.7.0-x86_64.tar.gz) | [Download](https://github.com/pauljones0/Deckard/releases/download/v0.7.0/deckard-0.7.0-aarch64.tar.gz) | Extract; open `deckard/bin/deckard`. Keep the extracted directory together. |
| Arch / EndeavourOS / Manjaro | [PKGBUILD](packaging/aur/deckard-git/PKGBUILD) | Same PKGBUILD | `makepkg -si`; launch **Deckard**. This option builds from source. |

Bundles include FFmpeg/FFprobe, audio/input helpers and the native example plugin. They require a Linux desktop with **glibc 2.35 or newer** and working Vulkan or OpenGL/EGL drivers: for example Ubuntu 22.04+, Debian 12+, Mint 21+ and current Fedora/Arch. They do not support Alpine/musl. File dialogs use your desktop's XDG portal. [Build details, checksums and troubleshooting](docs/linux-builds.md).

Connect your deck and launch Deckard. Select an input, choose an action and edit its labels or image. In **Store → Pages**, load the catalog and install **Rust Starter** for a ready-made page. Select that page in the editor; its native plugin is included. **Settings** includes autostart and an **Enable USB access** button for portable installations. DEB/RPM installers install the USB rules automatically; reconnect the deck if permission errors persist. The portable access helper requires administrator authentication.

## Every pixel, every useful frame

**0.6.0 removes the fixed 30 FPS render loop.** Auto follows the source animation rate (up to 120 FPS), renders at the controller's native resolution and applies bounded USB backpressure. The editor's **Animation FPS cap → 0** selects Auto; saved page limits remain configurable.

| Model | Native key image | Extra display |
| --- | --- | --- |
| Original / V2 / MK.2 | 72×72 | — |
| Mini family | **80×80** | — |
| XL / Neo | 96×96 | Neo: 248×58 infobar |
| Plus | 120×120 | 800×100 touch strip |
| Plus XL | **112×112** | 1200×100 touch strip |
| Studio | **144×112** fallback; queried from firmware when available | Encoder LEDs |
| Mirabox / Ulanzi | 85×85 / 196×196 | Model-specific key layouts |

These are controller image endpoints, rather than Valve Steam Deck screen dimensions or 144×144 source-icon artwork. [Verified specifications, FPS settings, transport behavior and diagnostics](docs/rendering-endpoints.md). Maximum visible hardware FPS needs a connected controller; the software limit is not a guaranteed LCD refresh rate.

## Faster animation, less work per frame

**100 FPS at about 17 MiB: 3.4× the frames, with 34% less CPU work per device frame. At the same 10 FPS, background CPU falls 20%.** These are measured Rust 0.5.0 → 0.6.0 results, with every key animated at its native resolution.

| Workload · all keys | FPS · 0.5 → 0.6 | CPU · 0.5 → 0.6 | RAM MiB · 0.5 → 0.6 | CPU work per device frame |
| --- | ---: | ---: | ---: | ---: |
| Plus · 8 × 120² · 10 FPS GIF · background | 10.0 → **10.0** | 0.67% → **0.53%** | 17.8 → **16.9** | **−20%** |
| Plus · 8 × 120² · 100 FPS GIF · background | 29.7 → **100.1** | 1.47% → 3.27% | 17.9 → **17.1** | **−34%** |
| Plus · 8 × 120² · 60 FPS video · background | 29.7 → **60.1** | 2.40% → 2.93% | 66.9 → 66.5 | **−40%** |
| XL · 32 × 96² · 50 FPS GIF · background | 29.2 → **49.9** | 3.20% → 4.80% | 28.0 → 27.7 | **−12%** |
| Plus XL · 36 × 112² · 50 FPS GIF · background | 28.7 → **49.9** | 4.47% → 6.53% | 32.8 → 32.4 | **−16%** |
| Plus · 8 × 120² · 100 FPS GIF · visible editor | 29.6 → **100.0** | 3.07% → 5.33% | 178.3 → 177.7 | **−48%** |

Higher-rate sources use more total CPU because more frames are rendered; the final column compares CPU cost for a complete frame across all keys. CPU is percent of one logical core; RAM is application-plus-descendants PSS. Results are medians of three trials, each with 15 seconds warmup and 15 seconds sampling, on the Ryzen 7 9800X3D / RTX 5070 host. Separate pixel checks validate every key after timing; the visible editor uses Vulkan in a fresh private 1280×800 Wayland session. GIFs have 200 distinct 120×120 source frames; video is a 60 FPS FFV1 clip. These fake-device measurements establish rendering throughput, rather than physical USB/LCD limits or ARM performance. [Methodology](benchmarks/README.md#native-endpoint-optimization-060), [raw samples, per-key FPS and executable hashes](benchmarks/results/2026-10-06/endpoints).

## More room for your stream 🦀

**Published 0.5.0 comparison: editor animation uses 52% less CPU and 42% less RAM than the direct upstream. Background animation uses 80% less CPU and 92% less RAM than the original.**

The upstream table records the published **0.5.0** comparison. Each application cell shows **CPU / RAM**. CPU is percent of one logical core; RAM is proportional resident memory (PSS). Negative changes mean lower resource use.

| Workload | Original StreamController | Direct upstream Deckard | Rust 0.5.0 | vs original | vs direct |
| --- | ---: | ---: | ---: | --- | --- |
| Editor visible · static | 0.10% / 297 MiB | 0.07% / 266 MiB | **0.10% / 171 MiB** | CPU: idle floor; RAM -42% | CPU: idle floor; RAM -36% |
| Editor visible · 8 animated keys | 6.33% / 339 MiB | 3.80% / 303 MiB | **1.83% / 176 MiB** | CPU -71%; RAM -48% | CPU -52%; RAM -42% |
| Background · static | 0.10% / 167 MiB | 0.03% / 149 MiB | **0.10% / 13 MiB** | CPU: idle floor; RAM -92% | CPU: idle floor; RAM -91% |
| Background · 8 animated keys | 4.07% / 197 MiB | 2.23% / 195 MiB | **0.80% / 16 MiB** | CPU -80%; RAM -92% | CPU -64%; RAM -92% |

The Rust port itself also got leaner:

| Animated workload | Rust 0.4.1 | Rust 0.5.0 | Improvement |
| --- | ---: | ---: | --- |
| Editor visible · 8 keys | 4.90% / 187 MiB | **1.83% / 176 MiB** | **63% less CPU; 6% less RAM** |
| Background · 8 keys | 1.07% / 64 MiB | **0.80% / 16 MiB** | **25% less CPU; 75% less RAM** |

Static CPU values are near the accounting floor, so their tiny differences are not treated as speedups. CPU savings are resource savings; input-response latency was not measured.

Measured on a Ryzen 7 9800X3D / NVIDIA RTX 5070, using three trials per case, 30 seconds warmup and 30 seconds sampling, a fresh private 1280×800 GPU-backed Wayland session, one fake Plus and eight labelled keys. The GIF requests 10 FPS; separate frame checks observed **8.9–10 FPS upstream and 9.95 FPS in Rust**, on all eight keys in both modes. No installed plugins or physical USB hardware were used. The Python baselines share one dependency environment; applications retain their default renderers/encoders. Rust 0.5.0 uses Vulkan with memory-focused allocation; 0.4.1 used OpenGL. Results depend on hardware, drivers and workload. [Methodology and reproduction](benchmarks/README.md), [raw samples and revisions](benchmarks/results/2026-10-06), [frame validation](benchmarks/results/2026-10-06/validation), [archived 0.4.1 baseline](benchmarks/results/2026-10-05).

## What the Rust port includes

- Original, Original V2, MK.2, Mini, XL, Plus, Plus XL, Neo, Pedal, Studio, Mirabox 293S and Ulanzi D200 device definitions, with fake models for previews and tests.
- One hardware writer per deck, input polling between image writes, latest-frame coalescing, image identities, immutable byte-budgeted caches, bounded on-demand glyphs, shared configuration snapshots, reuse of unchanged keys/strips and native SIMD JPEG encoding.
- Correct GIF transparency, disposal and individual frame delays; video wallpapers; slideshow order and shuffle; per-image viewport settings; calibrated Plus wallpaper continuity and rotated touchscreen layouts.
- Pages with multiple persistent input states, sticky inputs across pages, sticky indicators, page search, device names, action drag-and-drop, brightness, screensavers and session-lock handling.
- Automatic page switching on supported Hyprland, Sway, KDE, GNOME, X11 and Mango desktops; logind lock detection also supports sessions such as Niri. KDE needs `kdotool`; GNOME needs the [StreamController shell extension](https://extensions.gnome.org/extension/6871/streamcontroller-integration/).
- Atomic JSON saves, corrupt-JSON recovery, same-user single-instance control, USB reconnects, bounded subprocesses, transactional native installs and page bundles containing media and available native plugin executables.
- Native plugin/page/icon catalogs, branch filtering, source links, custom icon packs, lazy GUI startup, tray Open/Restart/Quit and an expanded CLI.
- Optional AI page proposals through a configurable endpoint. AI is off by default; proposals are reviewed before saving and command/text/hotkey actions require explicit permission in the UI.

**All 56 actions in the five recommended plugins have native implementations.** OSPlugin, DeckPlugin, MediaPlugin, OBSPlugin and VolumeMixer now include live readouts, graphs, artwork, OBS meters/status, repeating input, periodic commands and timed returns. These run in Rust. Open **Settings → Inspect legacy actions → Migrate supported actions** to translate saved actions with exact backups. [Action coverage, setup and migration](docs/plugin-migration.md).

**Plugin API changed:** Python/GTK plugins do not run. Existing page JSON and unknown plugin settings are retained. See the [new executable plugin interface](docs/native-plugins.md) and [working Rust example](rust/plugin-example). This is a breaking application port, with a new egui interface; it does not reproduce every GTK dialog or Python plugin integration.

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

Use Rust **1.99**, C/C++ build tools, CMake, NASM and the platform development libraries listed in [the Dockerfile](packaging/linux/Dockerfile). `scripts/build-rust.sh` builds the native application and runs tests. `scripts/build-linux.sh` needs only Docker on the host and builds all four Linux formats for the host architecture. GitHub Actions builds both architectures on native runners and publishes checked artifacts for version tags.

The previous Python implementation, tests and packaging remain in the repository as historical reference. They are excluded from native runtime bundles. The [old README](docs/legacy-python-readme.md) documents that retired application; its installation instructions do not apply to this Rust release.

GPL-3.0-or-later. Original project and fork attribution are retained. Bundles include dependency and font licenses; see [source and license information](docs/linux-builds.md#source-and-licenses).
