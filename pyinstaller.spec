# PyInstaller spec for the vst-host CLI.
#
# Bundles src/vst_host.py + Python + pedalboard + numpy into a single
# self-contained --onefile executable. Pedalboard ships a JUCE-statically-
# linked native extension; PyInstaller's analyser sometimes misses parts of
# it, so we explicitly --collect-all the package to pull in every submodule,
# data file, and dynamic library.
#
# Output filename is ``vst-host`` (or ``vst-host.exe`` on Windows). CI renames
# the artifact to the per-platform-arch convention afterwards.

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
    a.binaries,
    a.datas,
    [],
    name="vst-host",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
