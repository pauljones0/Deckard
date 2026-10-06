Deckard 0.7.0 reduces CPU work in the native rendering pipeline while retaining source timing, native endpoint dimensions and JPEG quality 90 with 4:4:4 chroma.

RGBA composition copies opaque rows and keeps reference alpha blending for partial transparency. Preview RGB pixels are allocated directly in their shared immutable buffer, avoiding a conversion dispatcher and an extra allocation/copy. Pixel identities use single-pass XXH3 with encoding/geometry information; download/signature verification retains cryptographic hashes. Font lookup caches missing families without blocking valid fonts.

Single-pass GIF/video playback uses a short encoded-image reuse window, up to 1 MiB, within the configured total cache budget. Looping animations retain the larger cache; finished single-pass playback and disconnected devices release unused encoded bytes. The release retains Auto FPS, firmware/model native resolution, bounded USB pacing and all 56 native actions.

AppImage, DEB, RPM and portable Linux builds are available for x86_64 and aarch64, with bundled FFmpeg/FFprobe and the native example plugin. No Python/GTK runtime is included. CPU/RAM measurements and their workload limits are recorded in the README and benchmark results; physical LCD refresh still requires connected hardware.
