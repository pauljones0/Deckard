# Linux release builds

Release 0.4.0 supplies AppImage, DEB, RPM and portable tar.gz builds for x86_64 and aarch64. The builds use Ubuntu 22.04 with glibc 2.35; no Python/GTK runtime is included. The GUI uses egui/OpenGL and supports X11 and Wayland. GPU drivers, libc, the host C++/GCC runtime, the desktop session and system services remain supplied by the host.

## Reproduce

```sh
scripts/build-linux.sh
```

Docker installs the compiler and packaging tools, compiles the committed Cargo.lock with Rust 1.99, runs native unit/integration tests, Clippy with warnings denied, formatting checks, JPEG diagnostics, a fake-deck smoke test and GUI startup under isolated Xvfb/D-Bus. The packager then tests the relocated portable GUI, FFmpeg frame output, native plugin RPC and AppImage extract-and-run diagnostics. Artifacts appear in `dist/linux/`. The screenshot is copied out for visual inspection. Run `scripts/verify-linux-packages.sh` to install and launch the DEB/RPM on fresh Ubuntu 22.04 and Fedora 43 containers, and check the portable GUI and AppImage there. These installation checks also run on both release architectures.

The GitHub workflow uses native `ubuntu-24.04` and `ubuntu-24.04-arm` runners, with the same Ubuntu 22.04 container on each. [GitHub documents both runner architectures](https://docs.github.com/en/actions/reference/runners/github-hosted-runners). A version tag releases artifacts only after both jobs succeed. Build metadata and SHA-256 sums are published separately per architecture.

AppImage's type-2 runtime is downloaded from the official AppImage repository and checksum-pinned in `packaging/linux/appimage-runtime.json`. A changed upstream runtime intentionally fails the build until its new checksum is reviewed. The archives are relocatable; keep their bin/lib/share directories together. Build-time Python performs packaging only and is not copied into the application. The packager rejects any interpreter, `.py`, `.pyc`, `.pyo` or libpython file in runtime bundles.

## Verify a download

Download `SHA256SUMS-x86_64` or `SHA256SUMS-aarch64` from the same release, then, in that directory:

```sh
sha256sum --ignore-missing -c SHA256SUMS-x86_64
```

At least your downloaded package should report `OK`. `deckard --doctor` reports the native runtime, architecture, API version and a JPEG self-test; it explicitly reports `python: false`.

## Desktop and USB setup

DEB/RPM installers place USB/hidraw uaccess rules under `/usr/lib/udev/rules.d/`. Portable/AppImage users can click **Settings → Enable USB access** and authenticate with their system's polkit dialog. Unplug and reconnect the deck after installation. Running the application as root is unnecessary. If your desktop has no polkit agent, run the bundled `bin/install-udev.sh` with administrator privileges once.

For AppImage without FUSE, run `./deckard-0.4.0-x86_64.AppImage --appimage-extract-and-run`. If the file manager does not execute files, use the DEB/RPM installer or run the extracted `bin/deckard`. A graphical installer and executable-file settings depend on your desktop; no binary can bypass those settings.

Install your desktop's XDG portal for file dialogs. KDE automatic switching needs `kdotool`. GNOME automatic switching uses the StreamController shell extension. Text/hotkey actions use bundled `wtype` on Wayland and `xdotool` on X11. Wayland synthetic keyboard support depends on the compositor implementing the virtual keyboard protocol; unsupported desktops need an appropriate native plugin. Helpers such as `hyprctl`, `swaymsg`, `kdotool`, `busctl`, `xdg-open` and compositor IPC remain host facilities.

## Source and licenses

Deckard is GPL-3.0-or-later. Source, build scripts and the exact Cargo.lock are available through the release tag and GitHub's source downloads. `share/deckard/licenses` contains Rust dependency identifiers and their supplied license files, Roboto's Apache 2.0 license, libjpeg-turbo's license, and Ubuntu copyright files for bundled system libraries/helpers. Native libraries and FFmpeg retain their individual licenses; Deckard's license does not replace them.

The bundles copy FFmpeg and input helpers from Ubuntu 22.04, adjusting only ELF RPATHs in executables and dependency libraries. Ubuntu package copyright records identify corresponding source projects. Release source records list exact Ubuntu package/source versions and the source download location; use Ubuntu's source packages, including distribution patches, when rebuilding these dependencies. The Dockerfile supplies the compiler and installation recipe. Original source author attribution remains in the historical tree and the upstream audit.

## Verification scope

Fake-model rendering is checked for all supported layouts and rotations, including Studio's rectangular keys, Mirabox/Ulanzi wire fixtures and Plus XL's 112-pixel keys and 100×1200 wire-format strip and Neo's infobar. Process tests check a separate daemon, page/state persistence, plugin RPC and bounded failures. These checks do not replace real-device USB tests, desktop-specific focus/keyboard tests, or multi-day Rust soak testing.
