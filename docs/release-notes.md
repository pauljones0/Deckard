Deckard 0.8.0 further reduces native rendering CPU work with exact SIMD RGB packing, shared-image opacity metadata and fewer GIF allocations.

RGB previews are packed with runtime-detected x86-64 SSSE3 or ARM64 NEON, with a scalar fallback. Every byte of the final shared buffer is written directly, avoiding a preliminary zero fill. Tests compare all channel bytes and cover short tails, unaligned buffers, native endpoint sizes, surrounding-byte guards and color spaces.

Opaque images shared across keys are checked once per composed render. The metadata cache has a 64-entry bound and weak references, so it cannot retain source pixel buffers. Transparent images retain the reference compositor. GIF playback borrows the decoder frame rather than allocating and copying a temporary full-frame buffer; transparency, disposal and source timing remain unchanged.

The release retains native firmware/model dimensions, Auto FPS, bounded USB pacing, quality-90 4:4:4 JPEG, the existing byte-budgeted single-pass/looping caches and all 56 native actions. Linux AppImage, DEB, RPM and portable builds include FFmpeg/FFprobe and the native example plugin for x86_64 and aarch64. No Python or GTK runtime is bundled. The README records matched-rate host measurements and their limits; physical LCD refresh and ARM performance require separate measurements.

Matched-rate host benchmarks report 33% less background CPU at 100 FPS on Plus and 29% less at 50 FPS on XL. The visible-editor median is 10% lower, while video is only 3% lower; those cases have overlapping trial ranges. Low-rate CPU is unchanged at the accounting floor, and RAM is effectively flat. All measured increases and ranges are retained in the README and raw results.
