# vst-host

Pedalboard-based VST3 host CLI used as the subprocess backend for Engineering's `Vst3Node`.

A long-lived process that loads a VST3 effect plugin via [Pedalboard](https://spotify.github.io/pedalboard/), optionally applies a `.vstpreset`, and streams f32le interleaved audio through `Pedalboard.Plugin.process(chunk, reset=False)` over stdio so plugin state (reverb tails, filter memory, internal buffers) is preserved across chunks.

The parent project ([Engineering](https://github.com/visionsofparadise/engineering)) spawns this binary per `Vst3Node` instance during a render. The full architectural design — schema, lifecycle, streaming model, distribution shape — lives in Engineering's planner under `projects/code/engineering/buffered-audio-nodes/design-vst3.md`.

## CLI

```
vst-host \
  --plugin-path <path-to-.vst3> \
  [--preset-path <path-to-.vstpreset>] \
  --sample-rate <int> \
  --channels <int> \
  [--block-size <int, default 4096>]
```

Stdio contract:
- **stdout**: a single `READY\n` line once the plugin (and preset, if any) is loaded, followed by raw f32le interleaved audio bytes; nothing else.
- **stdin**: raw f32le interleaved audio bytes, read in chunks of `block-size * channels * 4` bytes. EOF on stdin triggers a clean exit.
- **stderr**: all logging, diagnostics, and error messages.

## Build

Releases are produced by GitHub Actions for `windows-latest` (`vst-host-win32-x64.exe`), `ubuntu-22.04` (`vst-host-linux-x64`), and `macos-14` (`vst-host-darwin-arm64`). Each artifact is a self-contained PyInstaller `--onefile` bundle of Python 3.12 + Pedalboard + numpy + the wrapper script. No system Python is required at runtime.

## Distribution

Artifacts are uploaded to the `engineering-binaries-345340320424` S3 bucket and registered in Engineering's `binaries.manifest.json` under the schema key `vst-host`, alongside the existing addons (`onnx-runtime-addon`, `vkfft-addon`, `fftw-addon`).

## License

This binary statically embeds Pedalboard (GPLv3) and JUCE (GPLv3 free tier). The wrapper script and build configuration are MIT-licensed; the produced binary as a whole is GPLv3.
