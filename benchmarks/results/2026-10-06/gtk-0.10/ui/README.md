# Paired GTK captures

The source is the running nazbert/Deckard application at `a3609c7d`. Each pair uses identical GTK Cairo, fonts, window size and fake-device fixtures. The main pair is from the final release executable; the other pairs use the recorded debug candidate, whose UI code is unchanged by the later portability and smoke-test fixes. Widget JSON files accompany every image.

| Screen | Direct upstream | Rust | Pixels differing by >4 / 255 |
| --- | --- | --- | ---: |
| action | [Image](action/direct/editor.png) | [Image](action/rust/editor.png) | 0.2720% |
| assets | [Image](assets/direct/editor.png) | [Image](assets/rust/editor.png) | 0.0000% |
| assets-icon-packs | [Image](assets-icon-packs/direct/editor.png) | [Image](assets-icon-packs/rust/editor.png) | 0.0000% |
| chooser-populated | [Image](chooser-populated/direct/editor.png) | [Image](chooser-populated/rust/editor.png) | 0.2132% |
| deck-settings | [Image](deck-settings/direct/editor.png) | [Image](deck-settings/rust/editor.png) | 0.1652% |
| dial | [Image](dial/direct/editor.png) | [Image](dial/rust/editor.png) | 0.2132% |
| labels-detail | [Image](labels-detail/direct/editor.png) | [Image](labels-detail/rust/editor.png) | 0.2132% |
| main | [Image](main/direct/editor.png) | [Image](main/rust/editor.png) | 0.2962% |
| no-devices | [Image](no-devices/direct/editor.png) | [Image](no-devices/rust/editor.png) | 0.0000% |
| pages | [Image](pages/direct/editor.png) | [Image](pages/rust/editor.png) | 0.0000% |
| settings-developer | [Image](settings-developer/direct/editor.png) | [Image](settings-developer/rust/editor.png) | 0.0423% |
| settings-performance | [Image](settings-performance/direct/editor.png) | [Image](settings-performance/rust/editor.png) | 0.0000% |
| settings-ui | [Image](settings-ui/direct/editor.png) | [Image](settings-ui/rust/editor.png) | 0.0000% |
| touchscreen | [Image](touchscreen/direct/editor.png) | [Image](touchscreen/rust/editor.png) | 0.2132% |

Zero mean channel error establishes pixel identity for that recorded fixture. Nonzero cases include different deck-text rasterization, fake-device serials and data paths. Native plugin field schemas replace Python widget factories, so these captures do not assert universal identity for every plugin form, font or theme.

[Pixel reports and executable hashes](../ui-comparison.json) · [GTK restoration notes](../../../../../docs/ui-restoration.md)
