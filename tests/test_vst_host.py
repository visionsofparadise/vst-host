from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import vst_host  # noqa: E402


class _BinaryStream:
    def __init__(self, data: bytes = b"") -> None:
        self.buffer = io.BytesIO(data)


class _TelemetryStderr(io.StringIO):
    def __init__(self, telemetry_seen: threading.Event) -> None:
        super().__init__()
        self._telemetry_seen = telemetry_seen

    def write(self, text: str) -> int:
        written = super().write(text)

        if text.startswith(vst_host._TELEMETRY_PREFIX):
            self._telemetry_seen.set()

        return written


class _JoinRecordingThread(threading.Thread):
    join_calls: list[tuple[str, float | None]] = []

    def join(self, timeout: float | None = None) -> None:
        self.join_calls.append((self.name, timeout))
        super().join(timeout)


class _ScriptedStop:
    def __init__(self, waits_before_stop: int) -> None:
        self._waits_before_stop = waits_before_stop
        self.intervals: list[float] = []

    def wait(self, interval_seconds: float) -> bool:
        self.intervals.append(interval_seconds)

        return len(self.intervals) > self._waits_before_stop


class MonitorLivenessTests(unittest.TestCase):
    def test_emits_repeated_active_and_idle_json_events(self) -> None:
        stop = _ScriptedStop(waits_before_stop=2)
        stderr = io.StringIO()
        started_ns = 5_000_000_000
        initial_cpu_ns = 1_000_000_000

        with (
            patch.object(
                vst_host.time,
                "monotonic_ns",
                side_effect=[35_000_000_000, 65_000_000_000],
            ),
            patch.object(
                vst_host.time,
                "process_time_ns",
                side_effect=[1_200_000_000, 1_250_000_000],
            ),
            patch.object(vst_host.sys, "stderr", stderr),
        ):
            vst_host._monitor_liveness(
                stop,
                started_ns,
                initial_cpu_ns,
                interval_seconds=0.001,
            )

        lines = stderr.getvalue().splitlines()

        self.assertEqual(stop.intervals, [0.001, 0.001, 0.001])
        self.assertEqual(len(lines), 2)

        events = []
        for line in lines:
            self.assertTrue(line.startswith(vst_host._TELEMETRY_PREFIX))
            events.append(json.loads(line.removeprefix(vst_host._TELEMETRY_PREFIX)))

        self.assertEqual(
            events,
            [
                {
                    "type": "liveness",
                    "phase": "process",
                    "elapsedMs": 30_000,
                    "processCpuDeltaMs": 200,
                    "processCpuMs": 1_200,
                    "state": "active",
                },
                {
                    "type": "liveness",
                    "phase": "process",
                    "elapsedMs": 60_000,
                    "processCpuDeltaMs": 50,
                    "processCpuMs": 1_250,
                    "state": "idle",
                },
            ],
        )

    def test_stop_interrupts_a_long_wait_promptly(self) -> None:
        stop = threading.Event()
        stderr = io.StringIO()
        monitor = threading.Thread(
            target=vst_host._monitor_liveness,
            args=(stop, time.monotonic_ns(), time.process_time_ns(), 60.0),
        )

        with patch.object(vst_host.sys, "stderr", stderr):
            monitor.start()
            stop_started_at = time.perf_counter()
            stop.set()
            monitor.join(timeout=0.25)
            stop_elapsed = time.perf_counter() - stop_started_at

        self.assertFalse(monitor.is_alive())
        self.assertLess(stop_elapsed, 0.25)
        self.assertEqual(stderr.getvalue(), "")


class MainMonitorLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        _JoinRecordingThread.join_calls.clear()

    def test_main_joins_monitor_after_success_and_emits_nothing_after_return(self) -> None:
        monitor_started = threading.Event()
        monitor_finished = threading.Event()
        telemetry_seen = threading.Event()
        stderr = _TelemetryStderr(telemetry_seen)
        stdin_bytes = vst_host.np.asarray([0.25, -0.5], dtype=vst_host.np.float32).tobytes()
        stdin = _BinaryStream(stdin_bytes)
        stdout = _BinaryStream()
        original_monitor = vst_host._monitor_liveness

        def monitored_liveness(
            stop: threading.Event,
            started_ns: int,
            initial_cpu_ns: int,
        ) -> None:
            monitor_started.set()
            try:
                original_monitor(stop, started_ns, initial_cpu_ns, interval_seconds=0.001)
            finally:
                monitor_finished.set()

        def board(samples: vst_host.np.ndarray, sample_rate: int, reset: bool) -> vst_host.np.ndarray:
            self.assertTrue(monitor_started.wait(timeout=0.25))
            self.assertTrue(telemetry_seen.wait(timeout=0.25))
            self.assertEqual(sample_rate, 48_000)
            self.assertTrue(reset)

            return samples.copy()

        with tempfile.TemporaryDirectory() as directory:
            stages_path = Path(directory) / "stages.json"

            stages_path.write_text('[{"pluginPath":"fake.vst3"}]', encoding="utf-8")

            with (
                patch.object(vst_host, "_load_chain", return_value=board),
                patch.object(vst_host, "_monitor_liveness", side_effect=monitored_liveness),
                patch.object(vst_host.threading, "Thread", _JoinRecordingThread),
                patch.object(vst_host.sys, "stdin", stdin),
                patch.object(vst_host.sys, "stdout", stdout),
                patch.object(vst_host.sys, "stderr", stderr),
            ):
                result = vst_host.main(
                    [
                        "--stages-json",
                        str(stages_path),
                        "--sample-rate",
                        "48000",
                        "--channels",
                        "1",
                    ]
                )

        stderr_after_return = stderr.getvalue()

        time.sleep(0.01)

        self.assertEqual(result, 0)
        self.assertTrue(monitor_started.is_set())
        self.assertTrue(monitor_finished.is_set())
        self.assertEqual(_JoinRecordingThread.join_calls, [("vst-host-liveness", None)])
        self.assertFalse(any(thread.name == "vst-host-liveness" for thread in threading.enumerate()))
        self.assertTrue(telemetry_seen.is_set())
        self.assertEqual(stderr.getvalue(), stderr_after_return)
        self.assertEqual(stdout.buffer.getvalue(), b"READY\n" + stdin_bytes)

    def test_main_joins_monitor_after_board_error_and_emits_nothing_after_return(self) -> None:
        monitor_started = threading.Event()
        monitor_finished = threading.Event()
        telemetry_seen = threading.Event()
        stderr = _TelemetryStderr(telemetry_seen)
        stdin_bytes = vst_host.np.asarray([0.25, -0.5], dtype=vst_host.np.float32).tobytes()
        stdin = _BinaryStream(stdin_bytes)
        stdout = _BinaryStream()
        original_monitor = vst_host._monitor_liveness

        def monitored_liveness(
            stop: threading.Event,
            started_ns: int,
            initial_cpu_ns: int,
        ) -> None:
            monitor_started.set()
            try:
                original_monitor(stop, started_ns, initial_cpu_ns, interval_seconds=0.001)
            finally:
                monitor_finished.set()

        def board(_samples: vst_host.np.ndarray, sample_rate: int, reset: bool) -> vst_host.np.ndarray:
            self.assertTrue(monitor_started.wait(timeout=0.25))
            self.assertEqual(sample_rate, 48_000)
            self.assertTrue(reset)

            raise RuntimeError("controlled board failure")

        with tempfile.TemporaryDirectory() as directory:
            stages_path = Path(directory) / "stages.json"

            stages_path.write_text('[{"pluginPath":"fake.vst3"}]', encoding="utf-8")

            with (
                patch.object(vst_host, "_load_chain", return_value=board),
                patch.object(vst_host, "_monitor_liveness", side_effect=monitored_liveness),
                patch.object(vst_host.threading, "Thread", _JoinRecordingThread),
                patch.object(vst_host.sys, "stdin", stdin),
                patch.object(vst_host.sys, "stdout", stdout),
                patch.object(vst_host.sys, "stderr", stderr),
            ):
                result = vst_host.main(
                    [
                        "--stages-json",
                        str(stages_path),
                        "--sample-rate",
                        "48000",
                        "--channels",
                        "1",
                    ]
                )

        stderr_after_return = stderr.getvalue()

        time.sleep(0.01)

        self.assertEqual(result, 1)
        self.assertTrue(monitor_started.is_set())
        self.assertTrue(monitor_finished.is_set())
        self.assertEqual(_JoinRecordingThread.join_calls, [("vst-host-liveness", None)])
        self.assertFalse(any(thread.name == "vst-host-liveness" for thread in threading.enumerate()))
        self.assertEqual(stderr.getvalue(), stderr_after_return)
        self.assertIn("vst-host: error during processing: controlled board failure", stderr_after_return)
        self.assertEqual(stdout.buffer.getvalue(), b"READY\n")


if __name__ == "__main__":
    unittest.main()
