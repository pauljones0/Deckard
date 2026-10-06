Deckard 0.5.0 expands the Python-free Rust port to all 56 registered actions from OSPlugin, DeckPlugin, MediaPlugin, OBSPlugin and VolumeMixer, the five plugins recommended by both upstreams.

Includes live system graphs and ping, held/repeating input, periodic command output, timed page/state returns, metadata and album artwork, tiled thumbnails, OBS counters/meters/statistics and application mixer icons. The editor provides typed settings, OBS profile testing/selection lists and media player selection. Exact private backups, unknown settings and per-action visual ownership survive migration.

CPU and memory optimizations include shared immutable configuration snapshots, change-driven live discovery, persistent media connections, cached OBS requests, a shared preview texture atlas, reuse of unchanged keys/touchscreen strips, memory-mapped font discovery and on-demand glyph rasterization with a byte budget. The GUI defaults to Vulkan with memory-focused device allocation and automatically falls back to OpenGL when Vulkan startup is unavailable. Set DECKARD_RENDERER=glow to select OpenGL explicitly.

The README reports measured CPU/RAM comparisons with both pinned upstreams and links the raw samples, workload fidelity checks and reproduction instructions. Hardware and driver differences affect results.

Linux downloads: AppImage, DEB, RPM and portable tar.gz for x86_64 and aarch64, plus Arch PKGBUILD. Requires glibc 2.35+ and functioning Linux graphics. Runtime bundles contain no Python or GTK interpreter/framework; they include FFmpeg, audio/input/ping helpers, Vulkan's loader and licenses. Both architectures are tested and installed on fresh Ubuntu/Fedora before publication.

Python/GTK plugin code cannot execute against the native API. Plugins beyond the five audited integrations need an executable native replacement. Physical USB, compositor-specific input and long-duration soak validation remain machine-dependent checks.
