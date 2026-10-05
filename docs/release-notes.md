Deckard 0.4.0 replaces the application runtime with Rust and the plugin interface with native executable JSON-RPC API 1. No Python or GTK runtime is shipped.

Includes the audited StreamController updates through `0f439967`, while retaining the Deckard fork's device-writer, rendering, persistence and lifecycle fixes. Retains Studio, Mirabox 293S and Ulanzi D200 SDK support. Adds native pages/states/stickies, Neo infobar/touch inputs, Plus XL strip geometry, page bundles with media/native plugins, catalogs with branch selection, an expanded CLI, opt-in AI proposals, desktop focus/lock integration and tray controls.

Linux downloads: AppImage, DEB, RPM and portable tar.gz for x86_64 and aarch64. Bundled FFmpeg/input helpers/example plugin; glibc 2.35+ and a functioning Linux graphics session required. Download the corresponding SHA256SUMS file to verify packages. README has direct download links and first-launch instructions.

Breaking change: existing Python/GTK plugins must be replaced. Legacy page/settings data is retained and migrated; unknown plugin settings are preserved. Native automated tests and isolated GUI/package smoke checks pass. Physical-device, compositor-specific and long-duration soak verification remain machine-dependent.
