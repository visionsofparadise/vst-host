# vst-host

A command-line host for VST3 effect plugins and plugin state. It runs raw audio through an ordered chain of plugins, optionally applying a `.vstpreset` to each, and writes the processed audio back out. It is the plugin backend for [Buffered Audio Graph](https://github.com/visionsofparadise/buffered-audio-graph). Its `params` command reads and writes the parameters held in a VST3 or VST2 plugin's saved state.

Each run reads the whole input, processes it offline with plugin delay compensation applied across the chain, and writes the whole result. No system Python is needed to run the released binaries.

## Usage

```sh
vst-host \
  --stages-json <path-to-stages.json> \
  --sample-rate <int> \
  --channels <int>
```

The stages file is a non-empty JSON array. Each object contains `pluginPath`, a `.vst3` file or bundle folder, with optional `pluginName` for shell plugins and optional `presetPath`.

- **stdin**: raw f32le interleaved audio, read until EOF before processing starts.
- **stdout**: a single `READY\n` line once the plugins and presets are loaded, followed by the raw f32le interleaved result.
- **stderr**: logging, diagnostics and errors.

`vst-host --list-plugin-names <plugin.vst3>` prints the sub-plugin names in a shell plugin file, one per line, and takes no other option.

## Parameters

```sh
vst-host params < request.json > response.json
```

`params` loads one plugin, reads one JSON request on stdin and writes one JSON response line on stdout. Errors go to stderr with exit 1, and a malformed request exits 2. Output the plugin itself prints goes to stderr.

The request:

| Field | Meaning |
|---|---|
| `format` | `"vst3"` or `"vst2"` (VST2 on Windows only). |
| `pluginPath` | The `.vst3` file or bundle folder, or the VST2 `.dll`. |
| `pluginName` | Optional sub-plugin name for VST3 shell plugins; a `vst2` request carrying it exits 2. |
| `state` | Optional base64 plugin state: a VST3 plugin's component state, or a VST2 plugin's chunk. Without it the plugin starts from its defaults. |
| `set` | Optional object of parameter name to raw value in [0, 1], applied to the state. |
| `preview` | Optional array of `{ name, raw }` whose displays are returned. |
| `catalog` | `true` lists every parameter with its metadata; otherwise only the parameters whose raw value differs from the default. |

The response:

| Field | Meaning |
|---|---|
| `plugin` | `name`, `vendor`, `version` and `identifier`. |
| `state` | The plugin's state with `set` applied. Without `set` it is the request's state as sent. A request without `state` gets the state of a fresh instance. |
| `parameters` | Rows of `name`, `index`, `raw`, `display` and `default`. Catalog rows add whichever of `label`, `units`, `min`, `max`, `minText`, `maxText`, `step`, `choices` (`{ label, raw }`), `discrete`, `boolean` and `automatable` the plugin supplies. |
| `previews` | `{ name, raw, display }` per preview. |
| `stale` | `true` when a VST3 plugin's controller ignores the loaded state, so its reported values are its defaults rather than the state's. `parameters` is then empty, and `set` still applies to the state: each value goes to its target ± 0.01 and then to the target, and a value the saved state does not take exits 1. |

Parameter names are the plugin's own. A VST2 plugin's defaults come from a fresh instance, and its catalog metadata comes from its displays at 101 raw values: two displays mark it `boolean`, 3 to 32 give `choices`, and more give `minText` and `maxText`, the displays at raw 0 and 1. VST3 catalog metadata follows the same rule over pedalboard's display sweep, with numeric displays giving `min`, `max` and `step`. A VST2 `set` opens the plugin's editor in a hidden window, so a VST2 plugin without an editor cannot be written.

## Releases

Each release holds a folder build per platform: `vst-host-win32-x64.zip`, `vst-host-linux-x64.tar.gz` and `vst-host-darwin-arm64.tar.gz`. Each archive unpacks to a `vst-host-<platform>-<arch>` folder holding the `vst-host` executable (`vst-host.exe` on Windows) beside its `_internal` libraries, which stay together.

## License

[GPL-3.0-or-later](LICENSE). The produced binary embeds Pedalboard and JUCE, both GPLv3, so the whole project is GPL-3.0-or-later.
