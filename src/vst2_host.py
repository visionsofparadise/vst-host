"""A minimal VST2 host over ctypes for ``vst-host params`` (Windows only).

Every plugin call runs on the calling (main) thread, and pending window
messages are dispatched after each call, since VST2 plugins expect one thread
with a Windows message pump.

``Vst2Plugin`` and ``Vst2Parameter`` are the adapters ``params_core`` serves
requests on. A request uses two instances of the plugin: one carries the
request's state, and a fresh one supplies defaults and the displays for
previews and the probe, since VST2 shows a display only for the current value.
"""

from __future__ import annotations

import ctypes as C
import ctypes.wintypes as W
import time
from typing import Any

from params_core import PluginError, probe

intptr = C.c_ssize_t

effOpen, effClose, effGetParamLabel, effGetParamDisplay, effGetParamName = 0, 1, 6, 7, 8
effSetSampleRate, effSetBlockSize, effMainsChanged = 10, 11, 12
effEditOpen, effEditClose, effEditIdle = 14, 15, 19
effGetChunk, effSetChunk, effCanBeAutomated = 23, 24, 26
effGetEffectName, effGetVendorString, effGetProductString, effGetVendorVersion = 45, 47, 48, 49

effFlagsHasEditor, effFlagsProgramChunks = 1 << 0, 1 << 5
kEffectMagic = 0x56737450

AUDIOMASTER = C.CFUNCTYPE(intptr, C.c_void_p, C.c_int32, C.c_int32, intptr, C.c_void_p, C.c_float)


class AEffect(C.Structure):
    pass


DISPATCHER = C.CFUNCTYPE(intptr, C.POINTER(AEffect), C.c_int32, C.c_int32, intptr, C.c_void_p, C.c_float)
PROCESS = C.CFUNCTYPE(None, C.POINTER(AEffect), C.POINTER(C.POINTER(C.c_float)), C.POINTER(C.POINTER(C.c_float)), C.c_int32)
SETPARAM = C.CFUNCTYPE(None, C.POINTER(AEffect), C.c_int32, C.c_float)
GETPARAM = C.CFUNCTYPE(C.c_float, C.POINTER(AEffect), C.c_int32)

AEffect._fields_ = [
    ("magic", C.c_int32), ("dispatcher", DISPATCHER), ("process", PROCESS),
    ("setParameter", SETPARAM), ("getParameter", GETPARAM),
    ("numPrograms", C.c_int32), ("numParams", C.c_int32), ("numInputs", C.c_int32),
    ("numOutputs", C.c_int32), ("flags", C.c_int32),
    ("resvd1", intptr), ("resvd2", intptr),
    ("initialDelay", C.c_int32), ("realQualities", C.c_int32), ("offQualities", C.c_int32),
    ("ioRatio", C.c_float), ("object", C.c_void_p), ("user", C.c_void_p),
    ("uniqueID", C.c_int32), ("version", C.c_int32),
    ("processReplacing", PROCESS), ("processDoubleReplacing", C.c_void_p), ("future", C.c_char * 56),
]


class VstTimeInfo(C.Structure):
    _fields_ = [(n, C.c_double) for n in ("samplePos", "sampleRate", "nanoSeconds", "ppqPos", "tempo",
                                          "barStartPos", "cycleStartPos", "cycleEndPos")] + \
               [(n, C.c_int32) for n in ("timeSigNumerator", "timeSigDenominator", "smpteOffset",
                                         "smpteFrameRate", "samplesToNextClock", "flags")]


_user32 = C.WinDLL("user32", use_last_error=True)
_user32.CreateWindowExW.restype = C.c_void_p
_user32.CreateWindowExW.argtypes = [C.c_uint32, C.c_wchar_p, C.c_wchar_p, C.c_uint32, C.c_int, C.c_int, C.c_int,
                                    C.c_int, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p]
_user32.DestroyWindow.argtypes = [C.c_void_p]

WS_OVERLAPPEDWINDOW = 0x00CF0000  # without WS_VISIBLE: the window is never shown


def drain() -> None:
    """Dispatch every pending message on this thread's queue."""
    msg = W.MSG()
    while _user32.PeekMessageW(C.byref(msg), None, 0, 0, 1):
        _user32.TranslateMessage(C.byref(msg))
        _user32.DispatchMessageW(C.byref(msg))


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while True:
        drain()
        if time.monotonic() >= end:
            return
        time.sleep(0.01)


class Host:
    """One VST2 instance: the AEffect from the DLL's entry point, opened."""

    def __init__(self, path: str, sample_rate: float = 44100.0, block: int = 512):
        self.sr, self.block = sample_rate, block
        self.time = VstTimeInfo(sampleRate=sample_rate, tempo=120.0, timeSigNumerator=4, timeSigDenominator=4,
                                flags=(1 << 1) | (1 << 9) | (1 << 10) | (1 << 13))
        self._cb = AUDIOMASTER(self._audiomaster)
        self._chunk = None
        self.dll = C.WinDLL(path)
        main = getattr(self.dll, "VSTPluginMain", None) or getattr(self.dll, "main", None)
        if main is None:
            raise RuntimeError("'%s' exports neither VSTPluginMain nor main" % path)
        main.restype = C.POINTER(AEffect)
        main.argtypes = [AUDIOMASTER]
        self.fx = main(self._cb)
        if not self.fx or self.fx.contents.magic != kEffectMagic:
            raise RuntimeError("'%s' is not a VST2 plugin" % path)
        self.e = self.fx.contents
        self.d(effOpen)
        self.d(effSetSampleRate, opt=self.sr)
        self.d(effSetBlockSize, val=self.block)

    def _audiomaster(self, eff, op, idx, val, ptr, opt):
        if op == 1:
            return 2400  # audioMasterVersion
        if op == 7:
            return C.addressof(self.time)  # audioMasterGetTime
        if op == 16:
            return int(self.sr)
        if op == 17:
            return self.block
        if op == 23:
            return 1  # audioMasterGetCurrentProcessLevel: user
        if op in (32, 33):  # vendor and product strings
            C.memmove(ptr, b"vst-host\x00", 9)
            return 1
        if op == 34:
            return 1  # vendor version
        if op == 37:  # audioMasterCanDo
            s = C.string_at(ptr).decode(errors="replace")
            return 1 if s in ("sendVstEvents", "sendVstMidiEvent", "sendVstTimeInfo", "receiveVstEvents",
                              "receiveVstMidiEvent", "sizeWindow", "supplyIdle") else 0
        return 0

    def d(self, op, idx=0, val=0, ptr=None, opt=0.0):
        result = self.e.dispatcher(self.fx, op, idx, val, ptr, opt)
        drain()
        return result

    def string(self, op, idx=0) -> str:
        buf = C.create_string_buffer(256)
        self.d(op, idx, 0, buf)
        return buf.value.decode("latin-1")

    def get(self, index: int) -> float:
        value = float(self.e.getParameter(self.fx, index))
        drain()
        return value

    def set(self, index: int, value: float) -> None:
        self.e.setParameter(self.fx, index, C.c_float(value))
        drain()

    def get_chunk(self) -> bytes:
        p = C.c_void_p()
        n = self.d(effGetChunk, 0, 0, C.byref(p))
        return C.string_at(p, n) if n > 0 and p.value else b""

    def set_chunk(self, data: bytes) -> None:
        self._chunk = C.create_string_buffer(data, len(data))
        self.d(effSetChunk, 0, len(data), self._chunk)

    def sync_through_editor(self, idles: int = 5, seconds: float = 0.05) -> None:
        """Open the editor in a window that is never shown, idle it, and close it.

        Sausage Fattener's effGetChunk keeps returning the chunk from before a
        setParameter until its editor has opened and idled; rendering,
        effBeginSetProgram/effEndSetProgram and effSetProgram leave it stale.
        A write therefore fails when the editor cannot open, rather than
        returning the old chunk.
        """
        if not self.e.flags & effFlagsHasEditor:
            raise PluginError("cannot write the state: the plugin has no editor (effFlagsHasEditor unset), "
                              "which the host opens so the plugin's chunk takes a set")
        hwnd = _user32.CreateWindowExW(0, "STATIC", "vst-host", WS_OVERLAPPEDWINDOW, 0, 0, 600, 400,
                                       None, None, None, None)
        if not hwnd:
            raise PluginError("cannot write the state: CreateWindowExW failed for the editor's hidden window "
                              "(Windows error %d)" % C.get_last_error())
        try:
            if not self.d(effEditOpen, ptr=C.c_void_p(hwnd)):
                raise PluginError("cannot write the state: the plugin's editor did not open (effEditOpen returned 0)")
            try:
                for _ in range(idles):
                    self.d(effEditIdle)
                    pump(seconds)
            finally:
                self.d(effEditClose)
        finally:
            _user32.DestroyWindow(C.c_void_p(hwnd))
            drain()

    def close(self) -> None:
        self.d(effClose)


def _fourcc(value: int) -> str:
    raw = (value & 0xFFFFFFFF).to_bytes(4, "big")
    return raw.decode("latin-1") if all(32 <= b < 127 for b in raw) else "%08X" % (value & 0xFFFFFFFF)


class Vst2Parameter:
    def __init__(self, host: Host, index: int, default: float):
        self.host, self.index, self.default = host, index, default
        self.name = host.string(effGetParamName, index)

    @property
    def raw(self) -> float:
        return self.host.get(self.index)

    @raw.setter
    def raw(self, value: float) -> None:
        self.host.set(self.index, value)

    @property
    def display(self) -> str:
        return self.host.string(effGetParamDisplay, self.index).strip()

    def text_for(self, raw: float) -> str:
        """The display at ``raw``, read by setting it and restoring the value."""
        before = self.host.get(self.index)
        try:
            self.host.set(self.index, raw)
            return self.display
        finally:
            self.host.set(self.index, before)

    def meta(self) -> dict[str, Any] | None:
        label = self.host.string(effGetParamLabel, self.index).strip()
        out = probe(self.text_for)
        out["automatable"] = bool(self.host.d(effCanBeAutomated, self.index))
        if label:
            out["label"] = label
            out["units"] = label
        return out


class Vst2Plugin:
    """The request's instance, with ``fresh`` beside it for defaults and displays."""

    def __init__(self, path: str):
        self._hosts: list[Host] = []
        try:
            self.fresh = self._open(path)
            self.host = self._open(path)
            e = self.host.e
            if not e.flags & effFlagsProgramChunks:
                raise RuntimeError("the plugin keeps no state chunk (effFlagsProgramChunks unset)")
            name = self.host.string(effGetEffectName) or self.host.string(effGetProductString)
            self.info = {
                "name": name,
                "vendor": self.host.string(effGetVendorString),
                "version": str(self.host.d(effGetVendorVersion) or e.version),
                "identifier": _fourcc(e.uniqueID),
            }
            self.defaults = [self.fresh.get(i) for i in range(self.fresh.e.numParams)]
        except BaseException:
            self.close()
            raise
        self._params: list[Vst2Parameter] | None = None
        self._fresh_params: list[Vst2Parameter] | None = None

    def parameters(self) -> list[Vst2Parameter]:
        if self._params is None:
            self._params = [Vst2Parameter(self.host, i, self.defaults[i] if i < len(self.defaults) else 0.0)
                            for i in range(self.host.e.numParams)]
        return self._params

    def fresh_parameters(self) -> list[Vst2Parameter]:
        if self._fresh_params is None:
            self._fresh_params = [Vst2Parameter(self.fresh, i, d) for i, d in enumerate(self.defaults)]
        return self._fresh_params

    def load_state(self, inner: bytes) -> None:
        self.host.set_chunk(inner)

    def save_state(self) -> bytes:
        return self.host.get_chunk()

    def flush(self) -> None:
        self.host.sync_through_editor()

    def _open(self, path: str) -> Host:
        host = Host(path)
        self._hosts.append(host)
        return host

    def close(self) -> None:
        """effClose every opened instance, once."""
        while self._hosts:
            self._hosts.pop().close()
