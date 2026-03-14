# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for compress_pdf.

Bundles:
  - compress_pdf.py + pikepdf + Pillow (with their delocated dylibs)
  - cjpegli   (jpegli encoder + libjxl dylibs from build dir)
  - cjpeg     (mozjpeg encoder + libjpeg from Homebrew)

External dylib dependencies are collected transitively by macholib.
"""

import sys
from pathlib import Path

# ── external binary paths ──────────────────────────────────────────────────────
CJPEGLI_SRC  = "/tmp/jpegli/build-codex-fixed/tools/cjpegli"
CJPEG_SRC    = "/opt/homebrew/opt/mozjpeg/bin/cjpeg"

external_binaries = []
if Path(CJPEGLI_SRC).exists():
    external_binaries.append((CJPEGLI_SRC, "."))
if Path(CJPEG_SRC).exists():
    external_binaries.append((CJPEG_SRC, "."))

a = Analysis(
    ["scripts/compress_pdf.py"],
    pathex=[],
    binaries=external_binaries,
    datas=[],
    hiddenimports=[
        "pikepdf._core",
        "pikepdf.models",
        "pikepdf.objects",
        "PIL._imaging",
        "PIL.Image",
        "PIL.JpegImagePlugin",
        "PIL.PngImagePlugin",
    ],
    hookspath=["hooks"],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="compress-pdf",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="compress-pdf",
)
