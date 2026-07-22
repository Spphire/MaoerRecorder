# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).resolve()
FFMPEG = Path(os.environ.get("MAOER_BUILD_FFMPEG", ""))
FFPROBE = Path(os.environ.get("MAOER_BUILD_FFPROBE", ""))

if not FFMPEG.is_file() or not FFPROBE.is_file():
    raise SystemExit(
        "MAOER_BUILD_FFMPEG and MAOER_BUILD_FFPROBE must point to existing executables. "
        "Run build_exe.bat instead of invoking this spec directly."
    )

datas = collect_data_files("playwright")
datas += [
    (str(ROOT / "maoer" / "templates"), "maoer/templates"),
    (str(ROOT / "maoer" / "static"), "maoer/static"),
    (str(ROOT / "assets"), "assets"),
]

binaries = [
    (str(FFMPEG), "vendor/ffmpeg"),
    (str(FFPROBE), "vendor/ffmpeg"),
]

hiddenimports = collect_submodules("pystray")

a = Analysis(
    [str(ROOT / "dashboard.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MaoerRecorder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "assets" / "MaoerRecorder.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MaoerRecorder",
)
