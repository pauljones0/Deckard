# Linux builds

The default pipeline produces distro-native packages for CachyOS rolling (x86_64), Fedora 44 stable and Ubuntu 26.04 LTS (x86_64 and aarch64). Each binary is compiled inside its target distribution. The [GTK4/libadwaita editor](ui-restoration.md) and application engine are implemented in Rust.

Packages contain Deckard, its example native plugin, desktop icon, USB rules, documentation and license records. FFmpeg, audio/input helpers and shared libraries come from the distribution's package manager. There is no bundled system-library directory or `LD_LIBRARY_PATH` wrapper. Linked dependencies are generated with `dpkg-shlibdeps` for DEB and RPM's automatic ELF dependency detection; subprocess and dynamically loaded dependencies are explicit. Packages require GTK 4.16+ and libadwaita 1.7+, including the Adwaita icon theme.

## Build and verify

```sh
scripts/build-linux.sh             # all targets available on this architecture
scripts/build-linux.sh cachyos     # or fedora44 / ubuntu26
scripts/verify-linux-packages.sh   # install and launch on fresh distro images
```

Docker supplies Rust 1.99, builds the locked workspace, runs native tests, Clippy, formatting, JPEG diagnostics, model smoke tests, a private PulseAudio fixture and GUI startup under Xvfb/D-Bus. Installation checks exercise package-manager dependency resolution, system FFmpeg/FFprobe, mixer helpers, native plugin RPC, fake models, the default GTK renderer, OpenGL and Cairo. They reject Python runtime files, copied helpers and shared-library files in the application payload.

Artifacts appear under `dist/linux/<target>/<architecture>/`. CachyOS uses `.pkg.tar.zst`, Fedora `.rpm` and Ubuntu `.deb`. Filenames, store catalogs, checksums and build metadata identify the target to prevent collisions or installing plugins built for another distribution. A source build without a target uses the Ubuntu catalog, which shares the modern Linux baseline. The GitHub workflow runs five native jobs; ARM64 does not emulate x86_64, and CachyOS has no ARM build in this pipeline. A version tag publishes artifacts only after all build/install jobs pass.

Fedora is pinned to 44, the current stable target. Advance its builder, verifier and workflow target together after checking the next stable release. Ubuntu stays pinned to 26.04 LTS; CachyOS follows its stable rolling repositories. Updating these targets changes the package compatibility baseline. These installers do not claim compatibility with older Ubuntu or Fedora releases.

## Install

Open the DEB/RPM with your desktop's package installer, or use `sudo apt install ./<download>.deb` / `sudo dnf install ./<download>.rpm`. For CachyOS, use `sudo pacman -U ./<download>.pkg.tar.zst`; dependencies are resolved from the configured repositories. The [source PKGBUILD](../packaging/aur/deckard-git/PKGBUILD) remains available for CachyOS/Arch builds with `makepkg -si`.

USB/hidraw rules install under `/usr/lib/udev/rules.d/`. Reconnect the deck after installation. The native executable and example plugin live under `/usr/lib/deckard`; `/usr/bin/deckard` starts the application. No interpreter is included in the application package. Python is used only by the build tools.

File dialogs use GTK and your desktop's available XDG portal backend. KDE automatic switching needs `kdotool`; GNOME uses the StreamController shell extension. Text/hotkey actions use system `wtype` on compatible Wayland compositors and `xdotool` on X11. Mixer actions use `pactl` with PulseAudio or PipeWire-Pulse.

Fedora's standard repositories supply [ffmpeg-free](https://packages.fedoraproject.org/pkgs/ffmpeg/ffmpeg-free/). Its codec selection differs from Ubuntu/CachyOS FFmpeg; additional formats depend on the codecs installed on that Fedora system. The native packager accepts either provider of `/usr/bin/ffmpeg` and `/usr/bin/ffprobe`.

## Older portable builds

[v0.8.0](https://github.com/pauljones0/Deckard/releases/tag/v0.8.0) retains the old egui AppImages and portable archives. They bundle their dependencies on a glibc 2.35 baseline. Check out that tag to build or verify those artifacts. The GTK release uses distro-native installers.

## Checksums, licenses and scope

Download the matching `SHA256SUMS-<target>-<architecture>` and run `sha256sum --ignore-missing -c <checksum-file>` beside the package. `deckard --doctor` reports the Rust runtime, architecture and JPEG self-test, including `python: false`.

Deckard is GPL-3.0-or-later. Source and Cargo.lock are in the release tag. Native packages include Rust dependency license records, including statically linked libjpeg-turbo; system libraries retain their distribution-managed licenses and source packages. Legacy portable bundles additionally record corresponding Ubuntu source versions and copyrights.

Container checks cover installation, rendering, startup and GTK interactions. Physical USB throughput, real desktop portal integrations and ARM performance remain separate checks. [Performance measurements](performance.md) retain their original workloads and version labels.
