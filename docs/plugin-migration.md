# Common Python plugins → native actions

Deckard 0.4.1 implements the common controls from the five plugins recommended by **both** StreamController and nazbert/Deckard. These replacements are built into Rust; no Python plugin installation or interpreter is required. The old plugin's Python classes and GTK settings panels cannot run against the executable plugin API. Migration translates supported saved actions/settings to native controls, rather than loading that code.

## Migrate saved pages

Open **Settings → Inspect legacy actions**. The report identifies each supported conversion and each remaining action by document, input and state. Click **Migrate supported actions** to apply those conversions. Migration scans `pages/*.json` and `sticky/*.json`, including every saved state and all input families. Existing native actions and installed native executable plugins continue working.

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

| Upstream plugin | Native replacements included | What remains outside this replacement |
| --- | --- | --- |
| **OSPlugin** | Launch applications; one-shot evdev hotkeys; mouse click and relative movement; HTTP(S) links; text; easy hotkeys; user-configured shell commands; short delays. | Held/repeating hotkeys, automatic/periodic command execution, command-output displays, interactive terminal modes, non-HTTP links and separate browser-window modes. |
| **DeckPlugin** | Change/previous page, sleep, set/adjust brightness, change input state including another input. | Timed page/state returns and the original live status/icon decorations. |
| **MediaPlugin** | MPRIS Play, Pause, PlayPause, Stop, Next and Previous. Media dial: press to play/pause, turn to change track. Select a player by identity or D-Bus name; an empty selection controls all available players. | Album artwork, track metadata and continuously updated playback-state icons. Player-specific behavior depends on that player's MPRIS implementation. |
| **OBSPlugin** | OBS WebSocket **v5** authentication and multiple connection profiles; stream, recording/pause, replay buffer/save, virtual camera, studio mode, transition, scenes/collections, input mute/volume, scene-item visibility and filters. | WebSocket v4, live status subscriptions/icons, elapsed recording counters and other actions not listed in the migration report. |
| **VolumeMixer** | Open/exit, previous/next streams, per-application mute and volume, dial control and refreshed stream names/volume labels. The generated page follows device layout and rotation. | Application artwork and the original plugin's exact page styling. Requires PulseAudio or PipeWire's PulseAudio-compatible server. |

Native actions appear in the editor's action chooser. You can also create them on new pages. The volume mixer creates a `Native Mixer SERIAL` page and returns to the page it opened from. Volume is clamped to 0–100%; the step is configurable. Additional native audio actions control the default output and microphone directly.

Delays are limited to five seconds. Evdev sequences are limited to 256 events, with a maximum of one second per event and five seconds in total. Migrated shell commands retain intentional shell syntax and default to detached execution; foreground execution has a five-second deadline. OBS requests have bounded frames and connection/read/write timeouts. These bounds prevent a stalled helper or remote service from indefinitely blocking input handling.

## Input and OBS setup

DEB/RPM install the deck and `uinput` rules. Portable builds provide **Settings → Enable USB access**, which also enables native input access. Log out/in if your session has not acquired `/dev/uinput` access. One-shot evdev hotkeys and mouse controls work through Linux uinput. Text/easy hotkeys use bundled `wtype` on Wayland or `xdotool` on X11; Wayland virtual-keyboard support depends on your compositor.

For OBS, enable **Tools → WebSocket Server Settings**. Native profiles can be edited in Deckard's Settings JSON:

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

Tests exercise a private MPRIS D-Bus service and an authenticated local OBS WebSocket server. `benchmarks/validate_common_actions.py` verifies all five migration schemas, exact backups, sticky actions, private imported OBS credentials, unknown-action retention and repeated migration. Against a private PulseAudio server it exercises actual Rust mixer open/dial/mute/label-refresh/return behavior, brightness and a detached command. Tests avoid injecting input into the user's active desktop. Physical controller, real OBS UI and compositor-specific input testing remain separate hardware/session checks.

For integrations beyond these built-ins, use the [native executable plugin interface](native-plugins.md). The host owns the UI; native plugins declare settings fields and handle JSON-RPC events.
