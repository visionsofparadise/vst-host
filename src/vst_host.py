"""vst-host: Pedalboard-based VST3 host CLI.

Long-lived subprocess that loads a VST3 effect plugin, optionally applies a
.vstpreset, and streams f32le interleaved audio between stdin and stdout
while preserving plugin state across chunks via Pedalboard's
``Plugin.process(reset=False)``.

Stdio contract:
- stdout: a single ``READY\\n`` line on startup once the plugin (and preset, if
  any) is loaded, followed by raw f32le interleaved audio bytes; nothing else
  ever goes to stdout.
- stdin: raw f32le interleaved audio bytes, read in chunks of
  ``block_size * channels * 4`` bytes. EOF on stdin triggers a clean exit.
- stderr: all logging, diagnostics, and error messages.

See ``projects/code/engineering/buffered-audio-nodes/design-vst3.md`` in the
Engineering planner for the full architectural design.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pedalboard


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vst-host",
        description=(
            "Stream f32le interleaved audio through a VST3 effect plugin via "
            "Pedalboard, preserving plugin state across chunks."
        ),
    )
    parser.add_argument(
        "--plugin-path",
        required=True,
        help="Path to the .vst3 plugin file or bundle.",
    )
    parser.add_argument(
        "--preset-path",
        default=None,
        help="Optional path to a .vstpreset state file to apply after load.",
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
        help="Number of interleaved audio channels (e.g. 2 for stereo).",
    )
    parser.add_argument(
        "--block-size",
        type=int,
        default=4096,
        help="Frames per chunk read from stdin (default: 4096).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.channels <= 0:
        print(f"vst-host: --channels must be positive, got {args.channels}", file=sys.stderr)
        return 2
    if args.sample_rate <= 0:
        print(f"vst-host: --sample-rate must be positive, got {args.sample_rate}", file=sys.stderr)
        return 2
    if args.block_size <= 0:
        print(f"vst-host: --block-size must be positive, got {args.block_size}", file=sys.stderr)
        return 2

    try:
        plugin = pedalboard.load_plugin(args.plugin_path)
    except Exception as exc:  # noqa: BLE001 - surface any plugin-load failure.
        print(f"vst-host: failed to load plugin '{args.plugin_path}': {exc}", file=sys.stderr)
        return 1

    if args.preset_path:
        try:
            plugin.load_preset(args.preset_path)
        except Exception as exc:  # noqa: BLE001 - surface any preset-load failure.
            print(
                f"vst-host: failed to load preset '{args.preset_path}': {exc}",
                file=sys.stderr,
            )
            return 1

    # Signal readiness exactly once on stdout, then never write anything to
    # stdout again except processed audio bytes.
    sys.stdout.buffer.write(b"READY\n")
    sys.stdout.buffer.flush()

    bytes_per_block = args.block_size * args.channels * 4
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    while True:
        # Use an explicit byte count -- ``read()`` with no arg blocks until EOF
        # which would deadlock the parent process on the very first chunk.
        raw = stdin.read(bytes_per_block)
        if not raw:
            break

        # The parent may flush a partial trailing block at EOF; handle it by
        # taking the largest whole-frame multiple available.
        frame_bytes = args.channels * 4
        usable = (len(raw) // frame_bytes) * frame_bytes
        if usable == 0:
            # Not even one full frame; drop and continue draining stdin.
            continue
        if usable != len(raw):
            print(
                f"vst-host: discarded {len(raw) - usable} trailing bytes (not aligned to a frame)",
                file=sys.stderr,
            )
            raw = raw[:usable]

        try:
            interleaved = np.frombuffer(raw, dtype=np.float32)
            # ``Plugin.process`` accepts shape (channels, samples) or
            # (samples, channels). Use (channels, samples) consistently.
            samples = interleaved.reshape(-1, args.channels).T
            out = plugin(samples, sample_rate=args.sample_rate, reset=False)
            # ``out`` matches ``samples`` shape; re-interleave by transpose +
            # contiguous f32 cast before writing.
            stdout.write(np.ascontiguousarray(out.T, dtype=np.float32).tobytes())
            stdout.flush()
        except Exception as exc:  # noqa: BLE001 - any in-process failure ends the run.
            print(f"vst-host: error during processing: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
