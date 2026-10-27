"""Request-to-response logic of ``vst-host params``, over plugin-like objects.

This module imports nothing from pedalboard or ctypes, so its tests run on any
platform with fake plugins. ``vst_host`` (VST3, through pedalboard) and
``vst2_host`` (VST2, through ctypes) supply adapters with this shape:

- plugin: ``info`` (dict of name, vendor, version, identifier),
  ``parameters()`` (list of parameter adapters), ``load_state(inner)``,
  ``save_state()`` and ``flush()``, which makes the plugin's saved state take
  the parameter changes made since the last flush;
- parameter: ``index``, ``name``, ``default``, ``raw`` (read and write),
  ``display``, ``text_for(raw)`` and ``meta()``, which returns catalog
  metadata from the host or ``None`` when the probe supplies it.

State framing follows FL Studio's Fruity Wrapper sub-event 53:

- VST3: u32 version 1, then chunks of u32 id, u64 length and data. Chunk 1 is
  FL's own data, chunk 3 is the plugin's IComponent state byte for byte, and
  chunk 4 is FL's ParamID map. Only chunk 3 is ever replaced.
- VST2: a 21-byte header (i32 -9, u8 13, i32 -2, u32 chunk length, 8 zero
  bytes), then the plugin's ``effGetChunk`` bytes.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
import struct
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_EPSILON = 1e-6
STALE_NUDGE = 0.01
PROBE_STEPS = 100
PROBE_MAX_CHOICES = 32

VST3_COMPONENT_CHUNK = 3
VST3_FL_DATA_CHUNK = 1
VST2_HEADER_LENGTH = 21
VST2_LENGTH_OFFSET = 9


class RequestError(ValueError):
    """A malformed request: the command exits 2."""


class PluginError(RuntimeError):
    """A request the plugin cannot serve: the command exits 1."""


# --- JUCE MemoryBlock base64 ("<byte count>.<chars>", 6 bits per character,
# little-endian bit order), the encoding of pedalboard's VST3 raw_state XML.

JUCE_ALPHABET = ".ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+"
_JUCE_INDEX = {c: i for i, c in enumerate(JUCE_ALPHABET)}


def juce_b64_encode(data: bytes) -> str:
    # Every 3 bytes are 24 little-endian bits, so 4 characters.
    chars = []
    for k in range(0, len(data), 3):
        group = data[k : k + 3]
        bits = int.from_bytes(group, "little")
        for b in range(0, len(group) * 8, 6):
            chars.append(JUCE_ALPHABET[(bits >> b) & 0x3F])
    return "%d.%s" % (len(data), "".join(chars))


def juce_b64_decode(text: str) -> bytes:
    size_text, body = text.split(".", 1)
    size = int(size_text)
    out = bytearray()
    for k in range(0, len(body), 4):
        bits = 0
        for b, ch in enumerate(body[k : k + 4]):
            bits |= _JUCE_INDEX[ch] << (6 * b)
        out += bits.to_bytes(3, "little")
    return bytes(out[:size]) + bytes(max(0, size - len(out)))


# --- VST3 framing

def vst3_chunks(state: bytes) -> tuple[int, list[tuple[int, bytes]]]:
    """Split FL's VST3 sub-event 53 into its version and (id, data) chunks."""
    if len(state) < 4:
        raise RequestError("vst3 state is shorter than its 4-byte version")
    version = struct.unpack_from("<I", state, 0)[0]
    chunks: list[tuple[int, bytes]] = []
    q = 4
    while q < len(state):
        if q + 12 > len(state):
            raise RequestError("vst3 state has a truncated chunk header at byte %d" % q)
        cid, n = struct.unpack_from("<IQ", state, q)
        if q + 12 + n > len(state):
            raise RequestError("vst3 state chunk %d at byte %d overruns the state" % (cid, q))
        chunks.append((cid, state[q + 12 : q + 12 + n]))
        q += 12 + n
    if not any(cid == VST3_COMPONENT_CHUNK for cid, _ in chunks):
        raise RequestError("vst3 state has no chunk 3 (IComponent state)")
    return version, chunks


def vst3_frame(version: int, chunks: list[tuple[int, bytes]]) -> bytes:
    return struct.pack("<I", version) + b"".join(
        struct.pack("<IQ", cid, len(data)) + data for cid, data in chunks
    )


def vst3_component(state: bytes) -> bytes:
    return dict(vst3_chunks(state)[1])[VST3_COMPONENT_CHUNK]


def vst3_replace_component(state: bytes, component: bytes) -> bytes:
    version, chunks = vst3_chunks(state)
    return vst3_frame(
        version, [(cid, component if cid == VST3_COMPONENT_CHUNK else data) for cid, data in chunks]
    )


def vst3_new_state(component: bytes) -> bytes:
    """A sub-event 53 for a fresh instance: FL's 64-byte data chunk and chunk 3.

    The ParamID map (chunk 4) is left out, since pedalboard does not expose
    ParamIDs.
    """
    fl_data = struct.pack("<I", 1) + bytes(60)
    return vst3_frame(1, [(VST3_FL_DATA_CHUNK, fl_data), (VST3_COMPONENT_CHUNK, component)])


_ICOMPONENT = re.compile(rb"<IComponent>([^<]*)</IComponent>")


def vst3_raw_state(component: bytes) -> bytes:
    """pedalboard's VST3 ``raw_state`` for an IComponent state."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?> <VST3PluginState><IComponent>%s</IComponent></VST3PluginState>'
        % juce_b64_encode(component)
    )
    body = xml.encode("ascii") + b"\0"
    return b"VC2!" + struct.pack("<I", len(body)) + body


def vst3_component_from_raw_state(raw_state: bytes) -> bytes:
    if raw_state[:4] != b"VC2!":
        raise PluginError("pedalboard's raw_state lacks the VC2! header")
    n = struct.unpack_from("<I", raw_state, 4)[0]
    match = _ICOMPONENT.search(raw_state[8 : 8 + n])
    if match is None:
        raise PluginError("pedalboard's raw_state has no IComponent element")
    return juce_b64_decode(match.group(1).decode("ascii"))


# --- VST2 framing

def vst2_split(state: bytes) -> tuple[bytes, bytes]:
    """Split FL's VST2 sub-event 53 into its 21-byte header and the plugin chunk."""
    if len(state) < VST2_HEADER_LENGTH:
        raise RequestError("vst2 state is shorter than FL's 21-byte header")
    header, chunk = state[:VST2_HEADER_LENGTH], state[VST2_HEADER_LENGTH:]
    declared = struct.unpack_from("<I", header, VST2_LENGTH_OFFSET)[0]
    if declared != len(chunk):
        raise RequestError(
            "vst2 state header declares a %d-byte chunk, but %d bytes follow" % (declared, len(chunk))
        )
    return header, chunk


def vst2_frame(header: bytes, chunk: bytes) -> bytes:
    return header[:VST2_LENGTH_OFFSET] + struct.pack("<I", len(chunk)) + header[VST2_LENGTH_OFFSET + 4 :] + chunk


def vst2_new_state(chunk: bytes) -> bytes:
    return struct.pack("<ibiI", -9, 13, -2, len(chunk)) + bytes(8) + chunk


def inner_state(fmt: str, state: bytes) -> bytes:
    return vst3_component(state) if fmt == "vst3" else vst2_split(state)[1]


def outer_state(fmt: str, base: bytes | None, inner: bytes) -> bytes:
    if fmt == "vst3":
        return vst3_new_state(inner) if base is None else vst3_replace_component(base, inner)
    return vst2_new_state(inner) if base is None else vst2_frame(vst2_split(base)[0], inner)


# --- Requests

@dataclass
class Request:
    format: str
    plugin_path: str
    plugin_name: str | None = None
    state: bytes | None = None
    state_text: str | None = None
    set: dict[str, float] = field(default_factory=dict)
    preview: list[tuple[str, float]] = field(default_factory=list)
    catalog: bool = False


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError("%s must be a finite number" % where)
    try:
        number = float(value)  # an integer beyond the float range raises OverflowError
    except OverflowError:
        number = math.inf
    if not math.isfinite(number):
        raise RequestError("%s must be a finite number" % where)
    if not 0.0 <= number <= 1.0:
        raise RequestError("%s must lie in [0, 1], got %r" % (where, value))
    return number


def _no_unknown_fields(obj: dict[str, Any], known: set[str], where: str) -> None:
    unknown = sorted(set(obj) - known)
    if unknown:
        raise RequestError("unknown %s field(s): %s" % (where, ", ".join(unknown)))


def parse_request(obj: Any) -> Request:
    if not isinstance(obj, dict):
        raise RequestError("the request must be a JSON object")
    _no_unknown_fields(obj, {"format", "pluginPath", "pluginName", "state", "set", "preview", "catalog"}, "request")
    fmt = obj.get("format")
    if fmt not in ("vst3", "vst2"):
        raise RequestError('format must be "vst3" or "vst2"')
    path = obj.get("pluginPath")
    if not isinstance(path, str) or not path:
        raise RequestError("pluginPath must be a non-empty string")
    name = obj.get("pluginName")
    if name is not None and not isinstance(name, str):
        raise RequestError("pluginName must be a string")
    if name is not None and fmt == "vst2":
        raise RequestError("pluginName selects a plugin inside a VST3 bundle; the vst2 host loads one plugin per DLL")
    request = Request(format=fmt, plugin_path=path, plugin_name=name)

    state_text = obj.get("state")
    if state_text is not None:
        if not isinstance(state_text, str):
            raise RequestError("state must be a base64 string")
        try:
            request.state = base64.b64decode(state_text, validate=True)
        except binascii.Error as exc:
            raise RequestError("state is not valid base64: %s" % exc) from exc
        request.state_text = state_text
        inner_state(fmt, request.state)  # validates the framing before the plugin loads

    sets = obj.get("set")
    if sets is not None:
        if not isinstance(sets, dict):
            raise RequestError("set must be an object of parameter name to raw value")
        request.set = {k: _number(v, "set[%r]" % k) for k, v in sets.items()}

    previews = obj.get("preview")
    if previews is not None:
        if not isinstance(previews, list):
            raise RequestError("preview must be an array of { name, raw }")
        for k, item in enumerate(previews):
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise RequestError("preview[%d] must be an object with a string name" % k)
            _no_unknown_fields(item, {"name", "raw"}, "preview[%d]" % k)
            request.preview.append((item["name"], _number(item.get("raw"), "preview[%d].raw" % k)))

    catalog = obj.get("catalog", False)
    if not isinstance(catalog, bool):
        raise RequestError("catalog must be a boolean")
    request.catalog = catalog
    return request


# --- Probe and parameter rows

def probe(text_for: Callable[[float], str]) -> dict[str, Any]:
    """Classify a parameter from its displays at 101 evenly spaced raw values.

    Exactly 2 distinct displays mark it boolean. 3 to 32 mark it discrete, with
    one choice per display whose raw value is the midpoint of the raw run that
    shows it (the first run when a display repeats). More than 32 give the
    displays at 0 and 1 as ``minText`` and ``maxText``.
    """
    raws = [k / PROBE_STEPS for k in range(PROBE_STEPS + 1)]
    texts = [text_for(r) for r in raws]
    distinct = list(dict.fromkeys(texts))
    if len(distinct) == 2:
        return {"boolean": True}
    if 3 <= len(distinct) <= PROBE_MAX_CHOICES:
        choices: dict[str, float] = {}
        start = 0
        for k in range(1, len(texts) + 1):
            if k == len(texts) or texts[k] != texts[start]:
                if texts[start] not in choices:
                    choices[texts[start]] = round((raws[start] + raws[k - 1]) / 2, 6)
                start = k
        return {"discrete": True, "choices": [{"label": t, "raw": choices[t]} for t in distinct]}
    if len(distinct) > PROBE_MAX_CHOICES:
        return {"minText": texts[0], "maxText": texts[-1]}
    return {}


def _finite(value: Any) -> bool:
    return not isinstance(value, float) or math.isfinite(value)


def _reported(param: Any, what: str, value: Any) -> float:
    """A number the plugin reports, which the JSON response can carry."""
    number = float(value)
    if not math.isfinite(number):
        raise PluginError("the plugin reports a non-finite %s for parameter %r: %r" % (what, param.name, number))
    return number


def row(param: Any) -> dict[str, Any]:
    return {
        "name": param.name,
        "index": param.index,
        "raw": _reported(param, "raw value", param.raw),
        "display": param.display,
        "default": _reported(param, "default", param.default),
    }


def catalog_row(param: Any) -> dict[str, Any]:
    out = row(param)
    meta = param.meta()
    if meta is None:
        meta = probe(param.text_for)
    for key, value in meta.items():
        if value is not None and value != "" and _finite(value):
            out[key] = value
    return out


def differs(param: Any) -> bool:
    return abs(float(param.raw) - float(param.default)) > DEFAULT_EPSILON


# --- Stale controller

def _nudged(value: float) -> float:
    """``value`` moved by 0.01 toward the middle, so it stays in [0, 1]."""
    return value - STALE_NUDGE if value > 0.5 else value + STALE_NUDGE


def _far(value: float) -> float:
    """The end of [0, 1] farther from ``value``."""
    return 0.0 if value > 0.5 else 1.0


def controller_is_stale(plugin: Any, params: list[Any]) -> bool:
    """Whether a VST3 controller ignored the state just loaded.

    Three blocks run. The first comes before the baseline is taken, so a plugin
    that rewrites its state on its first block is not taken for stale. Then
    every parameter is pushed by ±0.01, a block runs, every parameter goes back
    to the value the controller reports, and a block runs. A controller that
    follows its state rewrites the loaded values; a stale one writes its own
    values over the processor's. The nudge matters: a set to the value a
    parameter already holds sends no change.
    """
    plugin.flush()
    loaded = plugin.save_state()
    values = [float(p.raw) for p in params]
    for p, v in zip(params, values):
        p.raw = _nudged(v)
    plugin.flush()
    for p, v in zip(params, values):
        p.raw = v
    plugin.flush()
    return plugin.save_state() != loaded


def set_stale(plugin: Any, named: dict[str, Any], sets: dict[str, float]) -> None:
    """Apply ``sets`` to a plugin whose controller ignored its loaded state.

    The controller still holds its own values, so a target equal to one of them
    would send no change. Each parameter therefore moves to its target ±0.01
    (toward the middle), a block runs, it moves to the target, and a block
    runs. The controller cannot report the loaded values, so the saved state
    is the evidence that the target took: it must differ from the nudged
    state. When it does not (a stepped parameter can store the nudged and the
    target value as one step), the parameter goes to the far end of [0, 1] and
    back, and the state must change across that pass.
    """
    for name, raw in sets.items():
        param = named[name]
        param.raw = _nudged(raw)
        plugin.flush()
        nudged = plugin.save_state()
        param.raw = raw
        plugin.flush()
        if plugin.save_state() != nudged:
            continue
        param.raw = _far(raw)
        plugin.flush()
        far = plugin.save_state()
        param.raw = raw
        plugin.flush()
        if plugin.save_state() == far:
            raise PluginError(
                "the plugin's state did not take %r = %r: its controller ignores the loaded state, "
                "and the saved state stayed the same across the set" % (name, raw)
            )


# --- Requests to responses

def _by_name(params: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in params:
        out.setdefault(p.name, p)
    return out


def respond(request: Request, plugin: Any, preview_params: list[Any] | None = None) -> dict[str, Any]:
    """Serve a parsed request on a loaded plugin.

    ``preview_params`` are the parameters previews and the probe read
    displays from, when the host keeps a separate fresh instance for that
    (VST2, where a display needs a set); by default the plugin's own.
    """
    if request.state is not None:
        plugin.load_state(inner_state(request.format, request.state))
    params = plugin.parameters()
    named = _by_name(params)
    shown = _by_name(preview_params) if preview_params is not None else named

    missing = [n for n in request.set if n not in named] + [n for n, _ in request.preview if n not in shown]
    if missing:
        raise PluginError("the plugin has no parameter named %s" % ", ".join(repr(n) for n in missing))

    previews = [
        {"name": name, "raw": raw, "display": shown[name].text_for(raw)} for name, raw in request.preview
    ]

    response: dict[str, Any] = {"plugin": plugin.info}
    stale = request.format == "vst3" and request.state is not None and controller_is_stale(plugin, params)
    if stale:
        # The check wrote the controller's values over the processor's, so the
        # state is loaded again, and a set then changes only the parameters it
        # names: the processor takes a controller change at its next block.
        plugin.load_state(inner_state(request.format, request.state))

    if request.set:
        if stale:
            set_stale(plugin, named, request.set)
        else:
            for name, raw in request.set.items():
                named[name].raw = raw
            plugin.flush()
        response["state"] = _b64(outer_state(request.format, request.state, plugin.save_state()))
    elif request.state is not None:
        response["state"] = request.state_text
    else:
        response["state"] = _b64(outer_state(request.format, None, plugin.save_state()))

    if stale:
        response["parameters"] = []
    elif request.catalog:
        if preview_params is not None:
            fresh = {p.index: p for p in preview_params}
            response["parameters"] = [{**catalog_row(fresh.get(p.index, p)), **row(p)} for p in params]
        else:
            response["parameters"] = [catalog_row(p) for p in params]
    else:
        response["parameters"] = [row(p) for p in params if differs(p)]
    if previews:
        response["previews"] = previews
    if stale:
        response["stale"] = True
    return response


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
