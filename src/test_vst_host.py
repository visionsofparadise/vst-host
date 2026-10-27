"""Tests of ``vst-host params`` I/O: the fd 1 redirect and the exit codes.

Each case runs ``vst_host._params_main`` in a subprocess with real file
descriptors, with ``_serve_params`` replaced so no plugin loads.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unittest

SRC = os.path.dirname(os.path.abspath(__file__))

DRIVER = """
import os, sys
import params_core, vst_host
def serve(request):
{body}
vst_host._serve_params = serve
sys.exit(vst_host._params_main())
"""

GOOD = {"format": "vst3", "pluginPath": "fake.vst3"}


def run(body: str, stdin: bytes) -> subprocess.CompletedProcess:
    code = DRIVER.format(body="\n".join("    " + line for line in textwrap.dedent(body).strip().splitlines()))
    return subprocess.run([sys.executable, "-c", code], input=stdin, capture_output=True, cwd=SRC, timeout=120)


def request(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


class ParamsMainTest(unittest.TestCase):
    def test_plugin_output_goes_to_stderr(self):
        result = run(
            """
            os.write(1, b"result 1\\n")
            print("printed by the plugin")
            return {"plugin": {"name": "Fake"}, "state": "", "parameters": []}
            """,
            request(GOOD),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'{"plugin": {"name": "Fake"}, "state": "", "parameters": []}\n')
        self.assertIn(b"result 1", result.stderr)
        self.assertIn(b"printed by the plugin", result.stderr)

    def test_exit_codes(self):
        cases = [
            ("bad JSON", "return {}", b"{not json", 2),
            ("non-UTF-8 input", "return {}", b"\xff\xfe", 2),
            ("unknown field", "return {}", request({**GOOD, "extra": 1}), 2),
            ("huge integer", "return {}", b'{"format": "vst3", "pluginPath": "x", "set": {"Gain": 1' + b"0" * 400 + b"}}", 2),
            ("request error while serving", "raise params_core.RequestError('bad state')", request(GOOD), 2),
            ("plugin error", "raise params_core.PluginError('no such parameter')", request(GOOD), 1),
            ("plugin crash", "raise RuntimeError('boom')", request(GOOD), 1),
            ("non-finite response", "return {'raw': float('nan')}", request(GOOD), 1),
        ]
        for name, body, stdin, code in cases:
            with self.subTest(name):
                result = run(body, stdin)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"vst-host params:", result.stderr)


if __name__ == "__main__":
    unittest.main()
