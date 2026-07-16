# vst-host

Pedalboard-based VST3 host CLI used as the subprocess backend for Buffered Audio Graph's `Vst3Node`.

The process loads an ordered chain of VST3 effect plugins via [Pedalboard](https://spotify.github.io/pedalboard/) and optionally applies a `.vstpreset` to each stage. It reads the complete f32le interleaved input from stdin, invokes the chain once in offline mode with `reset=True`, and writes the complete result to stdout. Pedalboard owns plugin state and delay compensation across the whole file.

The parent project ([Buffered Audio Graph](https://github.com/visionsofparadise/buffered-audio-graph)) spawns this binary once per `Vst3Node` render. The architectural design lives in the Buffered Audio Graph planner at `projects/code/buffered-audio-graph/buffered-audio-nodes/design-vst3.md`.

## CLI

```
vst-host \
  --stages-json <path-to-stages.json> \
  --sample-rate <int> \
  --channels <int>
```

The stages file is a non-empty JSON array. Each object contains `pluginPath`, with optional `pluginName` for shell plugins and optional `presetPath`.

Stdio contract:
- **stdout**: a single `READY\n` line once the plugin (and preset, if any) is loaded, followed by raw f32le interleaved audio bytes; nothing else.
- **stdin**: raw f32le interleaved audio bytes, read until EOF before offline processing starts.
- **stderr**: diagnostics, errors, and structured liveness records.

While the Pedalboard call is pending, the host writes one compact JSON record every 30 seconds:

```text
VST_HOST_EVENT {"type":"liveness","phase":"process","elapsedMs":30001,"processCpuDeltaMs":28754,"processCpuMs":29012,"state":"active"}
```

`active` means the host process accumulated at least 100 ms of CPU time during the interval. `idle` means it accumulated less. The state is evidence for an operator, not a hang verdict, and the host never terminates processing because of it. Vendor plugins may wait legitimately or perform work in a child process that is outside this process CPU measurement.

## Build

Releases are produced by GitHub Actions for `windows-latest` (`vst-host-win32-x64.exe`), `ubuntu-22.04` (`vst-host-linux-x64`), and `macos-14` (`vst-host-darwin-arm64`). Each artifact is a self-contained PyInstaller `--onefile` bundle of Python 3.12 + Pedalboard + numpy + the wrapper script. No system Python is required at runtime.

## Distribution

Artifacts are uploaded to the `buffered-audio-binaries-345340320424` S3 bucket and registered in Buffered Audio Graph's `binaries.manifest.json` under the schema key `vst-host`, alongside the existing addons (`onnx-runtime-addon`, `vkfft-addon`, `fftw-addon`).

## License

This binary statically embeds Pedalboard (GPLv3) and JUCE (GPLv3 free tier). The wrapper script and build configuration are MIT-licensed; the produced binary as a whole is GPLv3.
