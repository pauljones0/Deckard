Build the native Rust application on CachyOS or Arch with `makepkg -si` from this directory. Dependencies, including FFmpeg and audio/input helpers, come from pacman. No Python interpreter, venv or Python plugin loader is installed. The GTK4/libadwaita editor uses system libraries. `cargo test` and the native codec self-test run in `check()`.

The source package follows pauljones0/Deckard. Its USB/hidraw rules are installed automatically. Native file selection uses your desktop portal backend; automatic page switching uses the desktop's own helpers, with kdotool needed on KDE and the StreamController extension needed on GNOME.

The new distro build pipeline also produces a CachyOS x86_64 binary package, installable with `sudo pacman -U <download>.pkg.tar.zst`. Ubuntu/Fedora have their own DEB/RPM builds. Published 0.8.0 portable/AppImage builds remain available.
