"""把 CUDA 运行库目录加进 Windows 的 DLL 搜索路径。

**为什么必须有这一步**：`ctranslate2` 的 wheel 不带 `cublas64_12.dll` / `cublasLt64_12.dll`，
`pip install faster-whisper` 之后 GPU 推理会报 `Could not locate cublas64_12.dll`。
本机旧方案是把 DLL 手工复制进 `site-packages/ctranslate2/`（项目档里有记录），
但那是一次性手工操作，换一台机器就复现不了。

现在 `install.ps1` 会装 `nvidia-cublas-cu12`，这里在 **import ctranslate2 之前**
把 `nvidia/cublas/bin` 等目录交给 `os.add_dll_directory`，于是：
- 源码安装：直接用 pip 装出来的 DLL；
- 便携包：用打包进 `_internal/ctranslate2` 的 DLL。

`os.add_dll_directory` 只在 Windows 3.8+ 存在；其他平台安全跳过。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_APPLIED = False

# 依赖 DLL 可能在的位置（相对 site-packages / _MEIPASS / 程序根）
_RELATIVE_DIRS = (
    "ctranslate2",
    "nvidia/cublas/bin",
    "nvidia/cudnn/bin",
    "nvidia/cuda_nvrtc/bin",
    "cuda",
    "cuda/bin",
)


def _roots() -> list[Path]:
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            roots.append(Path(meipass))
        roots.append(Path(sys.executable).resolve().parent)
    else:
        try:
            import site

            for p in site.getsitepackages():
                roots.append(Path(p))
            try:
                roots.append(Path(site.getusersitepackages()))
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
        roots.append(Path(sys.prefix) / "Lib" / "site-packages")
    # 去重，保留顺序
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def candidate_dirs() -> list[str]:
    dirs: list[str] = []
    for root in _roots():
        for rel in _RELATIVE_DIRS:
            path = root / rel
            if path.is_dir():
                dirs.append(str(path))
    return dirs


def apply() -> None:
    """幂等：可以反复调用。非 Windows 直接返回。"""
    global _APPLIED
    if _APPLIED or os.name != "nt":
        return
    _APPLIED = True
    dirs = candidate_dirs()
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except (AttributeError, OSError):
            pass
    if dirs:
        # 有些库用 LoadLibrary("cublas64_12.dll") 而不走 add_dll_directory，PATH 兜底。
        old = os.environ.get("PATH", "")
        prefix = ";".join(dirs)
        if prefix.lower() not in old.lower():
            os.environ["PATH"] = prefix + (";" + old if old else "")


def has_cublas() -> bool:
    """运行库里有没有真正的 cublas。

    `ctranslate2.get_cuda_device_count()` 只问驱动，**不检查 cublas 是否存在**：
    CPU 轻量包（刻意不含 cublas）也会报 1 张卡。所以推荐矩阵要再问一次这里，
    否则会给用户推荐一个"看起来有 GPU、一加载就失败"的模型。
    """
    if os.name != "nt":
        return True  # 非 Windows 的 CUDA 运行库不由这套 DLL 名字判断
    names = ("cublas64_12.dll", "cublasLt64_12.dll")
    for d in candidate_dirs():
        for name in names:
            if (Path(d) / name).is_file():
                return True
    for name in names:
        try:
            if shutil.which(name):
                return True
        except OSError:
            pass
    return False
