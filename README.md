# vst-host

A command-line host for VST3 effect plugins. It runs raw audio through an ordered chain of plugins, optionally applying a `.vstpreset` to each, and writes the processed audio back out. It is the plugin backend for [Buffered Audio Graph](https://github.com/visionsofparadise/buffered-audio-graph).

Each run reads the whole input, processes it offline with plugin delay compensation applied across the chain, and writes the whole result. No system Python is needed to run the released binaries.

## Usage

```sh
vst-host \
  --stages-json <path-to-stages.json> \
  --sample-rate <int> \
  --channels <int>
```

The stages file is a non-empty JSON array. Each object contains `pluginPath`, with optional `pluginName` for shell plugins and optional `presetPath`.

- **stdin**: raw f32le interleaved audio, read until EOF before processing starts.
- **stdout**: a single `READY\n` line once the plugins and presets are loaded, followed by the raw f32le interleaved result.
- **stderr**: logging, diagnostics and errors.

## Releases

Binaries are built for Windows (`vst-host-win32-x64.exe`), Linux (`vst-host-linux-x64`) and macOS (`vst-host-darwin-arm64`).

## License

The produced binary embeds Pedalboard and JUCE, both GPLv3, so the binary as a whole is GPLv3. The wrapper script and build configuration are MIT-licensed.
