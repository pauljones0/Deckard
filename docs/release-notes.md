Deckard 0.4.1 adds entirely native replacements for the common controls from all five plugins recommended by both upstreams: OSPlugin, DeckPlugin, MediaPlugin, OBSPlugin and VolumeMixer. No Python or GTK runtime is shipped.

Open **Settings → Inspect legacy actions → Migrate supported actions** to convert saved pages and sticky inputs. Migration saves exact private backups, preserves unknown settings/unsupported actions, reports remaining integrations and imports OBS connection profiles without replacing native profiles. It is safe to repeat. The CLI exposes the same inspect/migrate commands. See `docs/plugin-migration.md` for the exact coverage and limitations.

Includes native MPRIS media control, authenticated OBS WebSocket v5 controls, per-application volume mixing with refreshed labels, output/microphone mute and volume, evdev key/mouse actions, application launches, shell commands and deck/page/state controls. Audio/input helpers are bundled. The installation helper and package rules enable uinput access.

The renderer now reuses unchanged animated frames and shared image resizing; the editor repaints on state/pixel changes and updates existing GPU textures. The README contains measured CPU/RAM comparisons against both pinned upstreams, with raw samples and reproducible methodology. It reports CPU regressions as well as improvements; static CPU differences near the accounting floor are not promoted as speedups.

Linux downloads: AppImage, DEB, RPM and portable tar.gz for x86_64 and aarch64. Requires glibc 2.35+ and a functioning Linux graphics session. Verify downloads with the architecture-specific SHA256SUMS file. Both architectures undergo native tests, Clippy, formatting and installation/GUI checks on fresh Ubuntu 22.04 and Fedora 43 before publication.

Existing Python/GTK plugin code cannot execute in this application. Common controls are implemented in Rust; album artwork, arbitrary third-party plugins, advanced held/repeating actions and other unsupported modes require further native implementations. Original saved data is preserved. Physical USB, compositor-specific input and long-duration soak checks remain separate machine-dependent validation.
