# Native plugin API 1

Plugins are native Linux ELF executables, written in Rust, C, C++ or another compiled language. There is no Python interpreter, GTK object interface, embedded script engine or plugin-owned USB handle. Plugins run as your user and are **not sandboxed**. Install code you trust.

## Package

Place `manifest.json` and your executable in one directory under `plugins-native/` inside the Deckard data directory. A distributable ZIP contains those files at its root, with the executable permission preserved. Executables must be 64-bit ELF files for the host architecture; paths must remain inside the plugin directory. Scripts and symlink archive members are rejected.

```json
{
  "api": 1,
  "id": "example",
  "name": "Rust example",
  "version": "0.4.1",
  "executable": "deckard-plugin-example",
  "source": "https://github.com/pauljones0/Deckard",
  "actions": [{
    "id": "hello", "name": "Hello Rust",
    "fields": [{"key":"label","label":"Label","kind":"string","default":"Hello Rust"}]
  }]
}
```

Action identifiers in pages are `example::hello`. IDs within the manifest must be unique. Fields render as native settings controls; kinds are `string`, `number`, `bool`, `array`. Arrays use a JSON editor. Unknown legacy Python actions remain in documents and are shown for replacement rather than executed.

## Protocol

Deckard starts a plugin on its first action, with its own directory as the working directory. Standard input and output carry UTF-8, newline-delimited JSON-RPC 2.0 messages. Keep stdout exclusively for protocol messages. The host currently discards plugin stderr; debugging can use a plugin-owned log file.

Request:

```json
{"jsonrpc":"2.0","id":1,"method":"event","params":{"action":"hello","event":"press","value":1,"serial":"DEVICE_SERIAL","input":{"family":"keys","id":"0x0"},"settings":{"label":"Hello Rust"}}}
```

Response:

```json
{"jsonrpc":"2.0","id":1,"result":{"label":"Hello Rust","color":[40,100,180,255]}}
```

Return the same numeric ID. A `label` changes the bottom label and a `color` changes the background of the active input state, including sticky inputs. These are runtime overlays; the host does not rewrite the page or silently overwrite state zero. Page changes/reloads clear overlays. An empty result is also valid. JSON-RPC `error` responses are reported as action failures.

Families are `keys`, `dials`, `touchscreens`, `infobar`. Key IDs are `0x0` coordinates, with Neo touch keys `touch-0` and `touch-1`; dial IDs are decimal strings. Events include `press`, `release`, `long-press`, `turn-cw`, `turn-ccw`, `touch`, `long-touch`, `swipe-left`, `swipe-right`. Dial turn `value` carries the signed step count. Key hold time defaults to 500 ms and can be changed through native settings `hold_ms`.

Messages are limited to 128 KiB. Writing to an unresponsive plugin and waiting for a response each have a two-second deadline. Invalid IDs, malformed/oversized responses, disconnection and timeouts stop that process; a later action starts a new instance. The host retains at most sixteen live plugin processes and terminates each process group during shutdown. Reload/install replaces cached plugin processes on the next action. Plugins cannot instantiate native UI widgets; settings are declared in the manifest.

## Build the example

```sh
cargo build --release -p deckard-plugin-example
mkdir -p ~/.local/share/deckard/plugins-native/example
cp examples/plugin/manifest.json ~/.local/share/deckard/plugins-native/example/
cp target/release/deckard-plugin-example ~/.local/share/deckard/plugins-native/example/
deckard --rpc '{"method":"reload"}'
```

The release bundles already include this example. User-installed plugins take precedence over a bundled plugin with the same ID. For distributable plugins, build against the documented Linux baseline and include your own non-system dependencies, using relative ELF RPATHs. Architectures in a catalog entry use `x86_64` or `aarch64`.

[Example manifest](../examples/plugin/manifest.json), [example implementation](../rust/plugin-example/src/main.rs), [host implementation](../rust/core/src/plugin.rs).

## Catalogs and bundles

A native store catalog is an HTTPS JSON array. Each entry has `kind` (`plugin`, `page`, `icons`), `name`, `description`, HTTPS `url`, `sha256`, optional `source`, `branch`, and `architectures`. Branches select **published, checksum-pinned packages**, not arbitrary Git checkouts or install scripts. See the [catalog template](../examples/native-store.json). Releases generate architecture-specific catalogs with the actual package checksums.

Page ZIPs contain `package.json` (`api`, `id`, `name`, `required_plugins`), `page.json`, optional `assets/`, and optional `plugins/ID/`. Export includes referenced existing media and installed native plugins. Imports validate paths, size limits, plugin manifests and asset references before committing. Existing plugins are retained rather than overwritten by a page import. Missing legacy plugins remain listed as requirements; they cannot be bundled into a native executable package.
