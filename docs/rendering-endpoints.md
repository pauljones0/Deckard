# Native resolution and source-timed animation

Deckard controls Elgato Stream Deck macro controllers and the supported Mirabox/Ulanzi controllers. Valve's Steam Deck is a Linux host computer; its 1280×800 display and GPU do not determine an attached controller's key resolution or USB throughput.

## Physical image endpoints

The renderer uses each model's native key and strip dimensions, including firmware-reported key geometry when available. It preserves user rotation separately from the device's required wire rotation. JPEG output remains quality 90 with 4:4:4 chroma; raising FPS does not reduce resolution or JPEG quality.

| Controller | Keys | Key image | Additional display | Key encoding |
| --- | ---: | --- | --- | --- |
| Original Stream Deck (2017) | 15 | 72×72 | — | BMP |
| Original V2 / MK.2 / MK.2 scissor / 15-key module | 15 | 72×72 | — | JPEG |
| Mini / Mini 2022 / Discord / 6-key module | 6 | **80×80** | — | BMP |
| XL / XL V2 / 32-key module | 32 | 96×96 | — | JPEG |
| Neo | 8 | 96×96 | 248×58 infobar | JPEG |
| Plus | 8 | 120×120 | 800×100 touch strip | JPEG |
| Plus XL | 36 | **112×112** | **1200×100** logical strip; 100×1200 JPEG after wire rotation | JPEG |
| Studio | 32 | **144×112** fallback; firmware query preferred | Encoder LEDs | JPEG |
| Mirabox 293S | 18 | 85×85 | Side keys included | JPEG |
| Ulanzi D200 | 14 active + 1 inert grid position | 196×196 | — | PNG / grouped ZIP |
| Pedal | 3 | No display | — | No image writes |

Sources: Elgato's [Classic](https://docs.elgato.com/streamdeck/hid/module-15_32/), [Mini](https://docs.elgato.com/streamdeck/hid/mini/), [XL](https://docs.elgato.com/streamdeck/hid/stream-deck-xl/), [Neo](https://docs.elgato.com/streamdeck/hid/stream-deck-neo/), [Plus](https://docs.elgato.com/streamdeck/hid/stream-deck-plus/) and [Plus XL](https://docs.elgato.com/streamdeck/hid/stream-deck-plus-xl/) HID specifications. Studio's rectangular fallback comes from the [native model definitions in node-elgato-stream-deck](https://github.com/Julusian/node-elgato-stream-deck/blob/77c379f483546753fb7eb74508a5454d21fc79a1/packages/core/src/models/definitions.ts); the old Python SDK's 80×120 fallback was inconsistent with that implementation. Compatible-device geometry remains based on the audited upstream protocols. Elgato's [unit-information report](https://docs.elgato.com/streamdeck/hid/general/#get-unit-information) supplies key geometry; Deckard validates the report ID, expected layout and bounded dimensions, and uses the model fallback on unsupported or inconsistent firmware.

144×144 artwork is a source-icon recommendation, rather than an extra MK.2 display mode. Elgato explicitly says the desktop software [scales high-DPI raster icons down](https://docs.elgato.com/guidelines/stream-deck/plugins/#key-icons). SVGs are rasterized for the actual destination; using one SVG on both a key and a larger strip does not reuse the smaller rasterization.

## FPS controls

In the editor, **Animation FPS cap → 0** selects **Auto · source rate**. A positive value caps playback between 1 and 120 FPS. Existing pages retain their saved limits; select Auto to remove an old 15/30 FPS cap. This setting caps delivered samples and does not speed up the animation's timeline.

- GIFs retain their individual frame delays, including valid 10 ms frames (100 FPS). Unspecified zero delays get a 20 ms fallback. Transparency and disposal remain streamed, without preloading a decoded frame history.
- Videos use their probed average frame rate, including fractional rates such as 30000/1001, capped by the requested limit. Variable-rate sources are normalized to their average rate. Missing/invalid rate metadata falls back to 60 FPS. FFprobe ships with FFmpeg in the Linux bundles.
- Device-wide `max_fps` in Settings is another animation cap: `0` or absent means Auto (up to the 120 FPS software limit); a positive value means 1–120 FPS. Per-media and device caps combine using the smaller limit. Input/configuration changes paint promptly.
- Slideshows wait for their next slide deadline. Static images and unchanged frames reuse their composition and encoded bytes.

Example device setting (replace the serial with your controller's serial):

```json
{"devices":{"YOUR_SERIAL":{"max_fps":0}}}
```

Auto preserves the source's rate when rendering and transport can keep up. It does not invent a model-wide 30/60 FPS rating, upscale output beyond the native endpoint, or generate 120 unique frames from a 24 FPS source.

## USB pacing and diagnostics

A single writer owns each controller. One prepared frame may overlap one in-flight frame, keeping memory bounded while allowing decoding/rendering to run alongside USB transfer. When that slot is occupied, ordinary animation rendering waits. The next render samples the current source timeline; missed frames are skipped rather than queued for delayed replay. New configuration revisions replace obsolete output. The physical-key cursor survives replacements and completes a full sweep, preventing later keys from starving during frequent live changes.

The writer polls input between keys without a blocking read or a fixed sleep on the active path. Idle waits are interruptible by newly rendered output; input remains polled at least every 8 ms between idle waits. Elgato image writes borrow encoded bytes directly, and HID packet buffers are reused. Ulanzi retains its required grouped ZIP transaction. Unchanged pixels never trigger another USB image write.

`status` exposes `native_key_size`, `native_strip_size`, `tile_updates` (actual pixel changes), `frame_clock_us` (monotonic elapsed time), `written_tiles`, `written_bytes` and `write_time_us`. Calculate changed-tile FPS from the difference between two counter snapshots divided by the elapsed clock difference. The byte counter counts encoded payload bytes, excluding HID headers/padding; successful write counters describe completed host writes, not LCD scanout. Fake devices report zero USB writes.

Elgato publishes no universal guaranteed LCD animation rate in these HID specifications. Its [SDK Marketplace guidance](https://docs.elgato.com/guidelines/stream-deck/plugins/#key-icons) recommends at most ten programmatic updates per second; that guidance is not a measured raw-HID transport ceiling. Sustainable visible FPS depends on firmware, image complexity/format, number of changing keys, strip updates, USB sharing and host performance. A software-only/fake-device benchmark cannot certify a physical panel's refresh rate. Real-device write counters let you check the particular endpoint without mistaking IPC polling or editor preview refresh for device output.
