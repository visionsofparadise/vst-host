"""vst-host: Pedalboard-based VST3 host CLI.

Hosts a chain of one or more VST3 effect plugins via
[Pedalboard](https://spotify.github.io/pedalboard/) and processes a single
audio buffer end-to-end in offline mode (``Pedalboard.process(reset=True)``).
Plugin delay compensation is handled internally by Pedalboard, so the output
buffer is sample-aligned with the input — no leading silence, no tail loss,
no per-plugin PDC management on the caller side.

The chain is specified at startup as a JSON file; the buffer flows in via
stdin and out via stdout as f32le interleaved bytes. Designed as the
subprocess backend for Engineering's `Vst3Node` (whole-file mode).

Stdio contract:
- stdout: a single ``READY\\n`` line on startup once all plugins (and presets,
  if any) are loaded, followed by raw f32le interleaved audio bytes; nothing
  else ever goes to stdout.
- stdin: raw f32le interleaved audio bytes, read until EOF.
- stderr: all logging, diagnostics, and error messages.

See ``projects/code/engineering/buffered-audio-nodes/design-vst3.md`` in the
Engineering planner for the full architectural design.
"""

from __future__ import annotations

# IMPORTANT: import pedalboard BEFORE numpy. pedalboard 0.9.7+ on Windows
# can crash with access violation 0xC0000005 if another native library
# (numpy, PyQt6, etc.) is imported first; the order here is the workaround
# tracked in https://github.com/spotify/pedalboard/issues/372 and #365.
import pedalboard

import argparse
import json
import sys
from typing import Any

import numpy as np


def _settle_state(plugin: pedalboard.Plugin) -> None:
    """Round-trip a VST3 plugin's ``raw_state`` through itself.

    Some VST3 plugins (notably iZotope's RX/Neutron line, Surge XT, Diva)
    keep a chunk of internal state behind their UI/event thread. Pedalboard
    runs everything on a single thread without a message loop, so directly
    setting parameters or calling ``load_preset`` doesn't always cause those
    plugins to actually internalize the change — the audio comes out as if
    nothing happened.

    Reading ``raw_state`` and writing it back forces pedalboard to call
    ``getState``/``setState`` on the plugin, which most implementations use
    as their canonical "commit pending changes" path. Doing this both before
    and after a state-changing operation is the workaround documented in
    https://github.com/spotify/pedalboard/issues/394.

    No-op for plugin types that don't expose ``raw_state`` (e.g. AU on
    non-Apple platforms).
    """
    if not hasattr(plugin, "raw_state"):
        return
    plugin.raw_state = plugin.raw_state


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vst-host",
        description=(
            "Process an audio buffer through a chain of VST3 effect plugins "
            "via Pedalboard, in offline mode with internal PDC."
        ),
    )
    parser.add_argument(
        "--stages-json",
        required=True,
        help=(
            "Path to a JSON file describing the plugin chain. The file must "
            "contain an array of objects, each with: pluginPath (required, "
            "string), pluginName (optional, string — for shell plugins like "
            "WaveShell), presetPath (optional, string)."
        ),
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        required=True,
        help="Audio sample rate in Hz (e.g. 48000).",
    )
    parser.add_argument(
        "--channels",
        type=int,
        required=True,
        help="Number of interleaved audio channels (e.g. 1 or 2).",
    )
    parser.add_argument(
        "--list-plugin-names",
        metavar="PLUGIN_PATH",
        default=None,
        help=(
            "Enumerate sub-plugin names contained in the given .vst3 file "
            "(e.g. for WaveShell) and exit. Prints one name per line on "
            "stdout. No audio I/O happens in this mode; --stages-json, "
            "--sample-rate, --channels are not required."
        ),
    )
    return parser


def _load_chain(stages: list[dict[str, Any]]) -> pedalboard.Pedalboard:
    plugins: list[pedalboard.Plugin] = []
    for i, stage in enumerate(stages):
        plugin_path = stage.get("pluginPath")
        if not plugin_path or not isinstance(plugin_path, str):
            raise ValueError(f"stage {i}: 'pluginPath' is required and must be a string")
        plugin_name = stage.get("pluginName")
        preset_path = stage.get("presetPath")
        parameters = stage.get("parameters")

        if parameters is not None and not isinstance(parameters, dict):
            raise ValueError(f"stage {i}: 'parameters' must be an object if provided")

        which = (
            f"'{plugin_path}' (plugin_name='{plugin_name}')"
            if plugin_name
            else f"'{plugin_path}'"
        )
        try:
            plugin = pedalboard.load_plugin(plugin_path, plugin_name=plugin_name)
        except Exception as exc:  # noqa: BLE001 - surface any plugin-load failure.
            raise RuntimeError(f"stage {i}: failed to load plugin {which}: {exc}") from exc

        # Pre-cycle: prime the plugin's state machinery before we touch it.
        # See _settle_state() docstring.
        _settle_state(plugin)

        if preset_path:
            try:
                plugin.load_preset(preset_path)
            except Exception as exc:  # noqa: BLE001 - surface any preset-load failure.
                raise RuntimeError(
                    f"stage {i}: failed to load preset '{preset_path}' for plugin {which}: {exc}"
                ) from exc
            # Post-preset cycle: forces plugins like Surge XT / iZotope to
            # actually apply the loaded preset rather than silently keeping
            # default state.
            _settle_state(plugin)

        # `parameters` is applied after `presetPath` — same precedence the
        # desktop-app would surface if a user picked a preset and then nudged
        # a knob. Useful for plugins where Pedalboard's preset-loading path
        # rejects the on-disk preset format (e.g. Waves XPst inside a VST3
        # wrapper) but the parameter surface is reachable directly.
        if parameters:
            for key, value in parameters.items():
                if not hasattr(plugin, key):
                    raise RuntimeError(
                        f"stage {i}: parameter '{key}' not exposed by plugin {which}"
                    )
                try:
                    setattr(plugin, key, value)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"stage {i}: failed to set parameter '{key}'={value!r} on plugin {which}: {exc}"
                    ) from exc
            # Post-parameter cycle: same reasoning as the post-preset cycle.
            # iZotope plugins in particular keep hidden state that setattr
            # alone doesn't flush; this forces a getState/setState round-trip.
            _settle_state(plugin)

        plugins.append(plugin)

    return pedalboard.Pedalboard(plugins)


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.list_plugin_names is not None:
        try:
            names = pedalboard.VST3Plugin.get_plugin_names_for_file(args.list_plugin_names)
        except Exception as exc:  # noqa: BLE001
            print(
                f"vst-host: failed to enumerate plugin names in '{args.list_plugin_names}': {exc}",
                file=sys.stderr,
            )
            return 1
        for name in names:
            print(name)
        return 0

    if args.channels <= 0:
        print(f"vst-host: --channels must be positive, got {args.channels}", file=sys.stderr)
        return 2
    if args.sample_rate <= 0:
        print(f"vst-host: --sample-rate must be positive, got {args.sample_rate}", file=sys.stderr)
        return 2

    try:
        with open(args.stages_json, "rb") as f:
            stages_data = json.load(f)
    except Exception as exc:  # noqa: BLE001
        print(f"vst-host: failed to read stages JSON '{args.stages_json}': {exc}", file=sys.stderr)
        return 1

    if not isinstance(stages_data, list) or len(stages_data) == 0:
        print("vst-host: stages JSON must be a non-empty array", file=sys.stderr)
        return 2

    try:
        board = _load_chain(stages_data)
    except Exception as exc:  # noqa: BLE001
        print(f"vst-host: {exc}", file=sys.stderr)
        return 1

    # Signal readiness exactly once on stdout, then never write anything to
    # stdout again except processed audio bytes.
    sys.stdout.buffer.write(b"READY\n")
    sys.stdout.buffer.flush()

    # Read all of stdin into a single f32le buffer.
    raw = sys.stdin.buffer.read()
    frame_bytes = args.channels * 4
    usable = (len(raw) // frame_bytes) * frame_bytes
    if usable != len(raw):
        print(
            f"vst-host: discarded {len(raw) - usable} trailing bytes (not aligned to a frame)",
            file=sys.stderr,
        )
        raw = raw[:usable]
    if usable == 0:
        # No audio — clean exit.
        return 0

    interleaved = np.frombuffer(raw, dtype=np.float32)
    # ``Pedalboard.process`` accepts shape (channels, samples).
    samples = interleaved.reshape(-1, args.channels).T

    try:
        # Offline mode: reset=True. Pedalboard handles plugin delay
        # compensation internally for the whole chain; the returned buffer
        # has the same number of frames as the input and is sample-aligned.
        out = board(samples, sample_rate=args.sample_rate, reset=True)
    except Exception as exc:  # noqa: BLE001
        print(f"vst-host: error during processing: {exc}", file=sys.stderr)
        return 1

    if out.shape[1] != samples.shape[1]:
        print(
            f"vst-host: WARNING — Pedalboard returned {out.shape[1]} frames "
            f"for {samples.shape[1]} input frames (offline mode normally "
            "returns equal length)",
            file=sys.stderr,
        )

    sys.stdout.buffer.write(np.ascontiguousarray(out.T, dtype=np.float32).tobytes())
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
