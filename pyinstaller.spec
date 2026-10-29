# PyInstaller spec for the vst-host CLI.
#
# Bundles src/vst_host.py + Python + pedalboard + numpy into a self-contained
# folder (onedir): the ``vst-host`` executable beside its ``_internal``
# libraries. A onefile build unpacks itself on every start, which added
# 7-9 s per run. Pedalboard ships a JUCE-statically-
# linked native extension; PyInstaller's analyser sometimes misses parts of
# it, so we explicitly --collect-all the package to pull in every submodule,
# data file, and dynamic library.
#
# Output is ``dist/vst-host/`` holding ``vst-host`` (or ``vst-host.exe`` on
# Windows). CI archives the folder under the per-platform-arch name.

# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# Pull every submodule, datafile, and binary dependency from pedalboard.
# Pedalboard's wheel ships a compiled extension with statically-linked JUCE,
# but the analyser does not always pick up the helper modules under
# pedalboard.io / pedalboard_native, hence collect_all.
pedalboard_datas, pedalboard_binaries, pedalboard_hiddenimports = collect_all("pedalboard")

# numpy is reliably picked up but we collect it too as belt-and-braces; the
# size cost of duplicates is zero (PyInstaller dedupes).
numpy_datas, numpy_binaries, numpy_hiddenimports = collect_all("numpy")

a = Analysis(
    ["src/vst_host.py"],
    pathex=[],
    binaries=pedalboard_binaries + numpy_binaries,
    datas=pedalboard_datas + numpy_datas,
    hiddenimports=pedalboard_hiddenimports + numpy_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="vst-host",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="vst-host",
)
