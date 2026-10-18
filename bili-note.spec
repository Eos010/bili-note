# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 规格：bili-note V3 便携版（onedir，console 模式）。

为什么是 onedir 不是 onefile：
- onefile 每次启动都要把 ~1GB 解压到临时目录，慢，而且更容易被杀毒误报；
- onedir 解压一次，之后双击即用，配置/模型/output 就在 exe 同级。

构建由 `build_portable.ps1` 驱动。它负责：
- 安装 PyInstaller；
- 准备 tools\ffmpeg；
- 调本 spec；
- 构建后跑 `bili-note.exe --probe-cuda` 与健康检查；
- 打 zip。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)
datas = []
binaries = []
hiddenimports = []

# 这些包带原生 DLL / 数据文件（VAD onnx、ctranslate2 CUDA DLL、av、yt-dlp 提取器…），
# 必须整包收集，否则打出来的 exe 会在运行到某一刻才炸。
for pkg in (
    "ctranslate2",
    "faster_whisper",
    "av",
    "onnxruntime",
    "yt_dlp",
    "huggingface_hub",
    "tokenizers",
):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# 静态前端 + 说明书 + 示例配置
datas += [
    (str(ROOT / "app" / "static"), "app/static"),
    (str(ROOT / "config.example.json"), "."),
    (str(ROOT / "README.md"), "."),
    (str(ROOT / "LICENSE"), "."),
]

# 随包 ffmpeg（如果 build 脚本准备好了）
ffmpeg_dir = ROOT / "tools" / "ffmpeg"
if ffmpeg_dir.is_dir():
    datas += [(str(ffmpeg_dir), "tools/ffmpeg")]

hiddenimports += [
    "app.main",
    "app.probe",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
]

a = Analysis(
    ["run.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "torch", "torchaudio", "matplotlib", "pandas", "PIL", "scipy"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="bili-note",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
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
    name="bili-note",
)
