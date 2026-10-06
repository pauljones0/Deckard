# Python plugins → native Rust actions

Deckard 0.5.0 implements all 56 registered actions from the five plugins recommended by **both** StreamController and nazbert/Deckard. These replacements are built into Rust; no Python plugin installation or interpreter is required. The old plugin's Python classes and GTK settings panels cannot run against the executable plugin API. Migration translates saved actions/settings into native controls. Readouts, artwork, background jobs and held/repeating inputs run in Rust. Arbitrary plugins outside the five audited integrations still need an executable native replacement.

## Migrate saved pages

Run `deckard --inspect-legacy-actions`. The report identifies supported conversions and remaining actions by document, input and state. Run `deckard --migrate-legacy-actions` to apply supported conversions. Migration scans `pages/*.json` and `sticky/*.json`, including every saved state and all input families. Existing native actions and installed native executable plugins continue working.

Equivalent CLI, using the same data directory as your running application:

```sh
deckard --inspect-legacy-actions
deckard --migrate-legacy-actions
# For a separate legacy installation:
deckard --data /path/to/data --inspect-legacy-actions
```

Inspection does not change page JSON. Before changing a document, migration saves its **exact original bytes** under `backups/native-actions/`, with a SHA-256 suffix and mode `0600`. Repeating migration does not convert actions twice. Unknown settings and unsupported actions remain intact; their original Python actions still require replacement before they can execute. To restore a document, close Deckard and copy its recorded backup over the corresponding page/sticky JSON.

OBS connection profiles import into `settings/native.json` under `obs.connections`, without overwriting an existing native profile. That file and migration backups are private to your user. Original Python plugin settings are retained. Pages may still reference old plugin icons: keep those assets when moving your data directory.

## Coverage

| Upstream plugin | Actions | Native implementations |
| --- | ---: | --- |
| **OSPlugin** | 16 | `RunCommand`, `EasyCommand`, `OpenInBrowser`, `Hotkey`, `EasyHotkey`, `Delay`, `Launch`, `CPU_Graph`, `RAM_Graph`, `MoveXY`, `Click`, `CPU`, `RAM`, `WriteText`, `CPUTemp`, `Ping` |
| **DeckPlugin** | 6 | `ChangePage`, `GoToSleep`, `GoToPreviousPage`, `ChangeBrightness`, `AdjustBrightness`, `ChangeState` |
| **MediaPlugin** | 8 | `Play`, `Pause`, `PlayPause`, `Next`, `Previous`, `Info`, `Thumbnail`, `MediaDial` |
| **OBSPlugin** | 18 | `ToggleStream`, `ToggleRecord`, `RecPlayPause`, `ToggleReplayBuffer`, `SaveReplayBuffer`, `ToggleVirtualCamera`, `ToggleStudioMode`, `TriggerTransition`, `ToggleInputMute`, `SetInputMute`, `InputDial`, `SwitchScene`, `ToggleSceneItemEnabled`, `SetSceneItemEnabled`, `SwitchSceneCollection`, `ToggleSceneFilter`, `SetSceneFilter`, `OBSStats` |
| **VolumeMixer** | 8 | `Open`, `Exit`, `VolumeMute`, `VolumeUp`, `VolumeDown`, `MoveRight`, `MoveLeft`, `Dial` |

All 56 actions appear in the editor with typed settings. Native replacements also preserve the saved per-action label, image and background ownership selectors; explicit user labels and images take priority. Unknown fields are retained in page JSON and exact original backups.

- **OS:** CPU/RAM/temperature readouts, history graphs, ping, absolute pointer positioning, held/repeating evdev hotkeys, configurable delays, `.desktop` application launchers, custom URL schemes and new browser windows. Commands support output labels, interactive shells, intervals, long-hold pause and opt-in background execution.
- **Deck:** timed page/state returns, cross-input/cross-device targets, manual-navigation cancellation, brightness bounds and live status decorations.
- **Media:** persistent native MPRIS transport, live playback state, title/artist, artwork, idle images, tiled/grid thumbnails including physical key gaps, dial progress and timestamps. An empty player selection controls all players; the display prefers a playing player.
- **OBS:** authenticated WebSocket v5 profiles, server events, recording/stream counters, scene/item/filter state, input mute/volume, VU meters with decay, configurable statistics and custom status icons. The original logarithmic dial volume curve is preserved. State queries are shared and cached; invalid selections do not force connection churn. Enable OBS's current built-in WebSocket server (port 4455).
- **Mixer:** application icons, refreshed names/volumes, muted indicators, dial bars, configurable increments, layout/rotation and return to the previous page. Requires PulseAudio or PipeWire's PulseAudio-compatible server.

Long commands and delays execute outside the input dispatcher. Page changes, disconnects and shutdown cancel their workers; held keys are released. Detached launches and explicitly enabled background commands follow their configured lifecycle. Evdev sequences are bounded to 256 events and a maximum delay of 60 seconds per event. Media downloads, decoded artwork, glyphs, JPEG caches, worker queues and remote IO have explicit bounds.

## Input and OBS setup

DEB/RPM install the deck and `uinput` rules. Current distro-native packages install both rule sets. Log out/in if your session has not acquired `/dev/uinput` access. Evdev hotkeys and mouse controls work through Linux uinput. Text/easy hotkeys use system `wtype` on Wayland or `xdotool` on X11; Wayland virtual-keyboard support depends on your compositor.

For OBS, enable **Tools → WebSocket Server Settings**. Use **Settings → Plugins → OBS → Open Settings** to add/test profiles. OBS action forms fetch scenes, inputs, collections and the selected scene’s items/filters in the background. Advanced profiles can be edited in Settings JSON:

```json
{
  "obs": {
    "connections": {
      "default": {"host": "localhost", "port": 4455, "password": "YOUR_PASSWORD"}
    }
  }
}
```

Merge this into your existing settings rather than replacing the whole document. Select an OBS action's connection profile and operation; its request fields are JSON, for example `{"sceneName":"Desktop"}` for `SetCurrentProgramScene`. An unreachable server or player is reported in Deckard's error list.

## Audited sources and verification

Both application onboarding lists recommend these five plugins at the application revisions recorded in the [upstream audit](upstream-audit.md). Replacement schemas were checked against these plugin revisions:

| Plugin | Source revision |
| --- | --- |
| OSPlugin | [b7c719b](https://github.com/StreamController/OSPlugin/tree/b7c719b2ec7e9424cf182d7682c3189772f4dbd2) |
| DeckPlugin | [14924f3](https://github.com/StreamController/DeckPlugin/tree/14924f3b54e4f68117f26b8ae8d55d83956fb805) |
| MediaPlugin | [e77ea62](https://github.com/StreamController/MediaPlugin/tree/e77ea626dc744fda41f43b0a78d11f099aa6d80a) |
| OBSPlugin | [242afda](https://github.com/StreamController/OBSPlugin/tree/242afda6bec75c568dfe71d2ce47a9a196d5a430) |
| VolumeMixer | [c5f72f7](https://github.com/StreamController/VolumeMixer/tree/c5f72f7897abccbd6d8811fbe118089902d3c8b8) |

Tests exercise live metadata changes on a private MPRIS D-Bus service, authenticated local OBS WebSocket requests, cached readouts/event invalidation and rejected selections. They verify held/repeating key release, cancellation of long commands/delays, timed page/state returns, periodic command output, ownership selectors, thumbnail geometry and bounded glyph memory. `benchmarks/validate_common_actions.py` verifies all five migration schemas, exact backups, sticky actions, private imported OBS credentials, unknown-action retention and repeated migration. Against a private PulseAudio server it exercises actual Rust mixer open/dial/mute/label-refresh/return behavior, brightness and a detached command. Tests avoid injecting input into the user's active desktop. Physical controller, real OBS UI and compositor-specific input testing remain separate hardware/session checks.

For integrations beyond these built-ins, use the [native executable plugin interface](native-plugins.md). The host owns the UI; native plugins declare settings fields and handle JSON-RPC events.
