"""Unit tests of params_core over fake plugins (stdlib unittest, any platform)."""

from __future__ import annotations

import base64
import json
import struct
import unittest

import params_core as core


class FakeParam:
    def __init__(self, plugin, index, name, default, texts=None, meta=None):
        self.plugin, self.index, self.name, self.default = plugin, index, name, default
        self._texts = texts or (lambda raw: "%.2f" % raw)
        self._meta = meta
        plugin.controller[index] = default

    @property
    def raw(self):
        return self.plugin.controller[self.index]

    @raw.setter
    def raw(self, value):
        self.plugin.controller[self.index] = value
        self.plugin.pending.add(self.index)

    @property
    def display(self):
        return self._texts(self.raw)

    def text_for(self, raw):
        return self._texts(raw)

    def meta(self):
        return self._meta


class FakePlugin:
    """A controller (what parameters report) and a processor (what the state saves).

    ``flush`` hands the changed controller values to the processor. A stale
    plugin's ``load_state`` sets the processor only, as Serum does.
    """

    def __init__(self, stale=False):
        self.stale = stale
        self.controller: dict[int, float] = {}
        self.processor: dict[int, float] = {}
        self.pending: set[int] = set()
        self.flushes = 0
        self.info = {"name": "Fake", "vendor": "Test", "version": "1.0", "identifier": "FAKE"}
        self.params = [
            FakeParam(self, 0, "Gain", 0.5),
            FakeParam(self, 1, "Mode", 0.0, texts=lambda r: ["A", "B", "C"][min(int(r * 3), 2)]),
            FakeParam(self, 2, "Mix", 1.0, meta={"min": 0.0, "max": 100.0, "units": "%"}),
        ]
        self.processor = dict(self.controller)

    def parameters(self):
        return self.params

    def load_state(self, inner):
        values = dict(enumerate(struct.unpack("<%df" % (len(inner) // 4), inner)))
        self.processor = dict(values)
        if not self.stale:
            self.controller.update(values)
        self.pending.clear()

    def save_state(self):
        return struct.pack("<%df" % len(self.processor), *(self.processor[i] for i in sorted(self.processor)))

    def flush(self):
        self.flushes += 1
        for i in self.pending:
            self.processor[i] = self.controller[i]
        self.pending.clear()


def floats(*values):
    return struct.pack("<%df" % len(values), *values)


def vst3_state(component, param_ids=(0, 1, 2)):
    fl_data = struct.pack("<I", 1) + bytes(60)
    id_map = struct.pack("<I%dI" % len(param_ids), len(param_ids), *param_ids)
    return core.vst3_frame(1, [(1, fl_data), (3, component), (4, id_map)])


def vst2_state(chunk):
    return struct.pack("<ibiI", -9, 13, -2, len(chunk)) + bytes(8) + chunk


def b64(data):
    return base64.b64encode(data).decode("ascii")


def request(**fields):
    return core.parse_request({"format": "vst3", "pluginPath": "fake.vst3", **fields})


class JuceBase64Test(unittest.TestCase):
    def test_known_encoding(self):
        # bits of 0x01 0x02 read 6 at a time from the least significant end are 1, 8, 0, and the alphabet starts ".AB..."
        self.assertEqual(core.juce_b64_encode(b"\x01\x02"), "2.AH.")

    def test_round_trip_every_length(self):
        for n in range(0, 20):
            data = bytes((k * 37 + 11) & 0xFF for k in range(n))
            self.assertEqual(core.juce_b64_decode(core.juce_b64_encode(data)), data)


class Vst3FramingTest(unittest.TestCase):
    def test_chunks_and_component(self):
        state = vst3_state(b"COMPONENT")
        version, chunks = core.vst3_chunks(state)
        self.assertEqual(version, 1)
        self.assertEqual([(cid, len(data)) for cid, data in chunks], [(1, 64), (3, 9), (4, 16)])
        self.assertEqual(core.vst3_component(state), b"COMPONENT")

    def test_replace_keeps_chunks_1_and_4(self):
        state = vst3_state(b"OLD")
        new = core.vst3_replace_component(state, b"NEWER STATE")
        old_chunks, new_chunks = dict(core.vst3_chunks(state)[1]), dict(core.vst3_chunks(new)[1])
        self.assertEqual(new_chunks[3], b"NEWER STATE")
        self.assertEqual(new_chunks[1], old_chunks[1])
        self.assertEqual(new_chunks[4], old_chunks[4])
        self.assertEqual(core.vst3_replace_component(state, b"OLD"), state)

    def test_raw_state_round_trip(self):
        raw = core.vst3_raw_state(b"\x00\xffcomponent")
        self.assertEqual(raw[:4], b"VC2!")
        self.assertEqual(struct.unpack_from("<I", raw, 4)[0], len(raw) - 8)
        self.assertEqual(raw[-1:], b"\0")
        self.assertIn(b"<VST3PluginState><IComponent>", raw)
        self.assertEqual(core.vst3_component_from_raw_state(raw), b"\x00\xffcomponent")

    def test_malformed_states(self):
        with self.assertRaises(core.RequestError):
            core.vst3_chunks(b"\x01\x00")
        with self.assertRaises(core.RequestError):
            core.vst3_chunks(struct.pack("<IIQ", 1, 3, 100) + b"short")
        with self.assertRaises(core.RequestError):
            core.vst3_chunks(core.vst3_frame(1, [(1, b"x")]))

    def test_new_state_has_fl_data_and_component(self):
        chunks = dict(core.vst3_chunks(core.vst3_new_state(b"C"))[1])
        self.assertEqual(chunks[1], struct.pack("<I", 1) + bytes(60))
        self.assertEqual(chunks[3], b"C")


class Vst2FramingTest(unittest.TestCase):
    def test_split_and_frame(self):
        chunk = bytes(range(84))
        state = vst2_state(chunk)
        self.assertEqual(len(state), 105)
        header, inner = core.vst2_split(state)
        self.assertEqual(inner, chunk)
        self.assertEqual(core.vst2_frame(header, chunk), state)
        self.assertEqual(core.vst2_new_state(chunk), state)

    def test_frame_rewrites_the_length(self):
        header, _ = core.vst2_split(vst2_state(b"abcd"))
        framed = core.vst2_frame(header, b"longer chunk")
        self.assertEqual(struct.unpack_from("<I", framed, 9)[0], 12)
        self.assertEqual(framed[:9], header[:9])
        self.assertEqual(framed[13:21], header[13:21])
        self.assertEqual(core.vst2_split(framed)[1], b"longer chunk")

    def test_length_mismatch(self):
        with self.assertRaises(core.RequestError):
            core.vst2_split(vst2_state(b"abcd") + b"extra")
        with self.assertRaises(core.RequestError):
            core.vst2_split(b"short")


class ProbeTest(unittest.TestCase):
    def test_two_displays_mark_boolean(self):
        self.assertEqual(core.probe(lambda r: "On" if r >= 0.5 else "Off"), {"boolean": True})

    def test_choices_take_the_midpoint_of_their_run(self):
        meta = core.probe(lambda r: ["Sine", "Saw", "Square", "Noise"][min(int(r * 4), 3)])
        self.assertTrue(meta["discrete"])
        self.assertEqual(
            meta["choices"],
            [
                {"label": "Sine", "raw": 0.12},  # 0.00..0.24
                {"label": "Saw", "raw": 0.37},  # 0.25..0.49
                {"label": "Square", "raw": 0.62},  # 0.50..0.74
                {"label": "Noise", "raw": 0.875},  # 0.75..1.00
            ],
        )

    def test_choice_limit(self):
        self.assertTrue(core.probe(lambda r: str(min(int(r * 32), 31)))["discrete"])
        meta = core.probe(lambda r: str(min(int(r * 33), 32)))
        self.assertEqual(meta, {"minText": "0", "maxText": "32"})

    def test_continuous(self):
        self.assertEqual(core.probe(lambda r: "%.0f Hz" % (20 + r * 19980)), {"minText": "20 Hz", "maxText": "20000 Hz"})

    def test_single_display(self):
        self.assertEqual(core.probe(lambda r: "--"), {})


class RequestTest(unittest.TestCase):
    def test_parses_a_full_request(self):
        state = vst3_state(floats(0.5, 0.0, 1.0))
        r = request(pluginName="Sub", state=b64(state), set={"Gain": 0.25}, preview=[{"name": "Mix", "raw": 0.5}], catalog=True)
        self.assertEqual((r.format, r.plugin_path, r.plugin_name), ("vst3", "fake.vst3", "Sub"))
        self.assertEqual(r.state, state)
        self.assertEqual(r.set, {"Gain": 0.25})
        self.assertEqual(r.preview, [("Mix", 0.5)])
        self.assertTrue(r.catalog)

    def test_rejects_malformed_requests(self):
        bad = [
            [],
            {"format": "au", "pluginPath": "x"},
            {"format": "vst3"},
            {"format": "vst3", "pluginPath": "x", "extra": 1},
            {"format": "vst3", "pluginPath": "x", "state": "not base64!"},
            {"format": "vst3", "pluginPath": "x", "state": b64(b"\x01\x00\x00\x00")},
            {"format": "vst2", "pluginPath": "x", "state": b64(b"short")},
            {"format": "vst3", "pluginPath": "x", "set": {"Gain": 1.5}},
            {"format": "vst3", "pluginPath": "x", "set": {"Gain": True}},
            {"format": "vst3", "pluginPath": "x", "preview": [{"name": "Gain"}]},
            {"format": "vst3", "pluginPath": "x", "catalog": "yes"},
        ]
        for obj in bad:
            with self.subTest(obj=obj), self.assertRaises(core.RequestError):
                core.parse_request(obj)


class RespondTest(unittest.TestCase):
    def test_lists_only_non_default_parameters(self):
        state = vst3_state(floats(0.75, 0.0, 1.0 - 1e-7))
        response = core.respond(request(state=b64(state)), FakePlugin())
        self.assertEqual(response["parameters"], [{"name": "Gain", "index": 0, "raw": 0.75, "display": "0.75", "default": 0.5}])
        self.assertEqual(response["state"], b64(state))
        self.assertNotIn("stale", response)
        json.dumps(response, allow_nan=False)

    def test_set_flushes_and_replaces_chunk_3(self):
        state = vst3_state(floats(0.5, 0.0, 1.0))
        plugin = FakePlugin()
        response = core.respond(request(state=b64(state), set={"Mix": 0.25}), plugin)
        out = base64.b64decode(response["state"])
        self.assertEqual(core.vst3_component(out), floats(0.5, 0.0, 0.25))
        self.assertEqual(dict(core.vst3_chunks(out)[1])[4], dict(core.vst3_chunks(state)[1])[4])
        self.assertEqual([p["name"] for p in response["parameters"]], ["Mix"])

    def test_vst2_set_rewrites_the_header_length(self):
        state = vst2_state(floats(0.5, 0.0))
        plugin = FakePlugin()
        plugin.params = plugin.params[:2]
        plugin.processor = {0: 0.5, 1: 0.0}
        r = core.parse_request({"format": "vst2", "pluginPath": "fake.dll", "state": b64(state), "set": {"Mode": 1.0}})
        out = base64.b64decode(core.respond(r, plugin)["state"])
        self.assertEqual(core.vst2_split(out)[1], floats(0.5, 1.0))

    def test_without_state_frames_the_fresh_state(self):
        response = core.respond(request(), FakePlugin())
        self.assertEqual(core.vst3_component(base64.b64decode(response["state"])), floats(0.5, 0.0, 1.0))
        self.assertEqual(response["parameters"], [])

    def test_catalog_uses_host_metadata_or_the_probe(self):
        response = core.respond(request(catalog=True), FakePlugin())
        rows = {p["name"]: p for p in response["parameters"]}
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows["Mix"]["units"], "%")
        self.assertEqual(rows["Mix"]["max"], 100.0)
        self.assertEqual([c["label"] for c in rows["Mode"]["choices"]], ["A", "B", "C"])
        self.assertEqual(rows["Mode"]["choices"][1]["raw"], 0.5)
        self.assertEqual(rows["Gain"]["minText"], "0.00")

    def test_catalog_reads_displays_from_preview_params(self):
        plugin, fresh = FakePlugin(), FakePlugin()
        fresh.params[0]._meta = {"label": "dB"}
        response = core.respond(request(state=b64(vst3_state(floats(0.875, 0.0, 1.0))), catalog=True), plugin, fresh.params)
        gain = response["parameters"][0]
        self.assertEqual((gain["raw"], gain["label"]), (0.875, "dB"))

    def test_preview(self):
        response = core.respond(request(preview=[{"name": "Mode", "raw": 0.9}, {"name": "Gain", "raw": 0.1}]), FakePlugin())
        self.assertEqual(response["previews"], [{"name": "Mode", "raw": 0.9, "display": "C"}, {"name": "Gain", "raw": 0.1, "display": "0.10"}])

    def test_unknown_names(self):
        with self.assertRaises(core.PluginError):
            core.respond(request(set={"Nope": 0.5}), FakePlugin())
        with self.assertRaises(core.PluginError):
            core.respond(request(preview=[{"name": "Nope", "raw": 0.5}]), FakePlugin())


class StaleControllerTest(unittest.TestCase):
    def test_following_controller_is_not_stale(self):
        plugin = FakePlugin()
        plugin.load_state(floats(0.8, 0.3, 0.2))
        self.assertFalse(core.controller_is_stale(plugin, plugin.params))
        self.assertEqual(plugin.save_state(), floats(0.8, 0.3, 0.2))
        self.assertEqual(plugin.flushes, 2)

    def test_stale_controller_is_detected(self):
        plugin = FakePlugin(stale=True)
        plugin.load_state(floats(0.8, 0.3, 0.2))
        self.assertTrue(core.controller_is_stale(plugin, plugin.params))

    def test_stale_response_passes_state_through(self):
        state = vst3_state(floats(0.8, 0.3, 0.2))
        response = core.respond(request(state=b64(state), preview=[{"name": "Gain", "raw": 0.25}], catalog=True), FakePlugin(stale=True))
        self.assertTrue(response["stale"])
        self.assertEqual(response["state"], b64(state))
        self.assertEqual(response["parameters"], [])
        self.assertEqual(response["previews"][0]["display"], "0.25")

    def test_stale_set_keeps_the_loaded_values(self):
        state = vst3_state(floats(0.8, 0.3, 0.2))
        response = core.respond(request(state=b64(state), set={"Gain": 0.1}), FakePlugin(stale=True))
        self.assertTrue(response["stale"])
        self.assertEqual(response["parameters"], [])
        self.assertEqual(core.vst3_component(base64.b64decode(response["state"])), floats(0.1, 0.3, 0.2))

    def test_vst2_and_stateless_requests_skip_the_check(self):
        plugin = FakePlugin(stale=True)
        self.assertNotIn("stale", core.respond(request(), plugin))
        self.assertEqual(plugin.flushes, 0)
        plugin = FakePlugin(stale=True)
        plugin.params = plugin.params[:2]
        plugin.processor = {0: 0.5, 1: 0.0}
        r = core.parse_request({"format": "vst2", "pluginPath": "fake.dll", "state": b64(vst2_state(floats(0.5, 0.0)))})
        self.assertNotIn("stale", core.respond(r, plugin))


if __name__ == "__main__":
    unittest.main()
