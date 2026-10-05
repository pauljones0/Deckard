Build the native Rust application on Arch or Omarchy with `makepkg -si` from this directory. No Python interpreter, venv, GTK or Python plugin loader is installed. `cargo test` and the native codec self-test run in `check()`.

The source package follows pauljones0/Deckard. Its USB/hidraw rules are installed automatically. Native file selection uses your desktop portal backend; automatic page switching uses the desktop's own helpers, with kdotool needed on KDE and the StreamController extension needed on GNOME.

The portable/AppImage release builds are alternatives that do not require compiling Rust.
