# -*- mode: python ; coding: utf-8 -*-
"""One-folder Windows portable build for AutoMarker Re-ID.

The executable and its ``_internal`` runtime can be copied together to another
Windows PC.  ``queries`` and ``output`` intentionally live beside the EXE so
they remain writable and can travel with the portable folder.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs


datas = [
    ("assets", "assets"),
    ("ui_template.png", "."),
    ("reid.xml", "."),
    ("reid.bin", "."),
]
binaries = []
hiddenimports = [
    "pystray._win32",
    "PIL.ImageTk",
    "rapidocr.inference_engine.base",
    "rapidocr.inference_engine.openvino.main",
    "rapidocr.inference_engine.openvino.device",
    "rapidocr.ch_ppocr_det.main",
    "rapidocr.ch_ppocr_cls.main",
    "rapidocr.ch_ppocr_rec.main",
    "rapidocr.utils.typings",
]

# OpenVINO and RapidOCR load data files and native DLLs lazily.  Do not use
# ``collect_all`` here: it asks PyInstaller to import every optional backend
# (including unavailable Torch/TensorRT) during the build.  The explicit
# modules above are the OpenVINO OCR path used by this app.
for package in ("openvino", "rapidocr", "onnxruntime"):
    datas += collect_data_files(package)
    binaries += collect_dynamic_libs(package)


a = Analysis(
    ["app_gui.pyw"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # RapidOCR advertises several optional engines.  This application uses
    # OpenVINO only; excluding unavailable GPU/training stacks keeps PyInstaller
    # from loading their native DLLs while building the portable release.
    excludes=["torch", "torchvision", "tensorflow", "tensorrt", "paddle"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AutoMarker-ReID",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/app_icon.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AutoMarker-ReID-Portable",
)
