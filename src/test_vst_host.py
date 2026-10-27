"""Tests of ``vst_host``: params I/O, VST3 bundle paths and option checks.

Each params case runs ``vst_host._params_main`` in a subprocess with real
file descriptors, with ``_serve_params`` replaced so no plugin loads. The
other cases load no plugin either.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr
from io import StringIO

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


class ResolveVst3PathTest(unittest.TestCase):
    def test_bundle_folder_resolves_to_its_windows_binary(self):
        import vst_host

        with tempfile.TemporaryDirectory() as root:
            bundle = os.path.join(root, "Fake.vst3")
            binaries = os.path.join(bundle, "Contents", "x86_64-win")
            os.makedirs(binaries)
            inner = os.path.join(binaries, "Fake.vst3")
            open(inner, "wb").close()
            for given in (bundle, bundle + os.sep):
                with self.subTest(given):
                    self.assertEqual(vst_host.resolve_vst3_path(given, "win32"), os.path.join(given, "Contents", "x86_64-win", "Fake.vst3"))
            self.assertEqual(vst_host.resolve_vst3_path(inner, "win32"), inner)
            self.assertEqual(vst_host.resolve_vst3_path(bundle, "darwin"), bundle)

    def test_other_paths_pass_through(self):
        import vst_host

        with tempfile.TemporaryDirectory() as root:
            empty_bundle = os.path.join(root, "Empty.vst3")
            os.makedirs(os.path.join(empty_bundle, "Contents", "x86_64-win"))
            plain_folder = os.path.join(root, "Plugins")
            os.makedirs(plain_folder)
            for given in (empty_bundle, plain_folder, os.path.join(root, "Missing.vst3")):
                with self.subTest(given):
                    self.assertEqual(vst_host.resolve_vst3_path(given, "win32"), given)


class ListPluginNamesTest(unittest.TestCase):
    def test_extra_options_exit_2(self):
        import vst_host

        cases = [
            ["--stages-json", "stages.json"],
            ["--sample-rate", "48000"],
            ["--channels", "2"],
            ["params"],
        ]
        for extra in cases:
            with self.subTest(extra):
                stderr = StringIO()
                with redirect_stderr(stderr):
                    code = vst_host.main(["--list-plugin-names", "missing.vst3", *extra])
                self.assertEqual(code, 2)
                self.assertIn("takes no other option", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
