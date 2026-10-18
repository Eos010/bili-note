"""硬件 / 运行环境探测：给首次向导和「转写模型」设置页提供一份**可解释**的现状。

这里回答的问题是「这台机器能不能跑某个模型」，而不是「装没装显卡」。
所以除了 `nvidia-smi`，还要在子进程里真正问一次 ctranslate2——显卡在、驱动旧、
缺 cublas DLL 这三种情况，用户看到的都该是不同的话。

结果缓存 30 秒：设置页会轮询，不能让每 1.5 秒一次轮询都去拉 `nvidia-smi`。
"""
from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import app_paths

_CACHE: dict | None = None
_CACHE_AT = 0.0
_CACHE_TTL = 30.0

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd: list[str], timeout: float, cwd: str | None = None,
         env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env=env,
        creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


# ---------------------------------------------------------------- 内存 / 系统

class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _ram_gb() -> float:
    if os.name == "nt":
        try:
            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return round(status.ullTotalPhys / (1024 ** 3), 1)
        except (AttributeError, OSError):
            pass
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        return round(total / (1024 ** 3), 1)
    except (AttributeError, ValueError, OSError):
        return 0.0


def _os_label() -> str:
    if os.name == "nt":
        try:
            v = sys.getwindowsversion()
            return f"Windows {v.major}.{v.minor} (build {v.build})"
        except AttributeError:
            pass
    return f"{platform.system()} {platform.release()}"


# ---------------------------------------------------------------- GPU / CUDA

def gpu_info() -> dict:
    """用 nvidia-smi 拿型号/显存/驱动。没有 nvidia-smi 不算失败，交给 CUDA 探针判断。"""
    info = {"present": False, "name": "", "vram_mb": 0, "driver": "", "error": ""}
    if os.name != "nt":
        return info
    try:
        p = _run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            timeout=6,
        )
    except FileNotFoundError:
        info["error"] = "没有找到 nvidia-smi（通常表示没有 NVIDIA 显卡或驱动）"
        return info
    except subprocess.TimeoutExpired:
        info["error"] = "nvidia-smi 超时"
        return info
    except OSError as e:
        info["error"] = f"nvidia-smi 启动失败：{e}"
        return info
    if p.returncode != 0:
        info["error"] = (p.stderr or p.stdout or "").strip()[:200] or "nvidia-smi 返回非 0"
        return info

    best: tuple[int, str, str] = (0, "", "")
    for line in (p.stdout or "").splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 3:
            continue
        name, vram, driver = parts[0], parts[1], parts[2]
        try:
            vram_mb = int(float(vram))
        except ValueError:
            vram_mb = 0
        if vram_mb >= best[0]:
            best = (vram_mb, name, driver)
    if best[0]:
        info.update(present=True, name=best[1], vram_mb=best[0], driver=best[2])
    else:
        info["error"] = "nvidia-smi 没有返回可解析的显卡信息"
    return info


def cuda_probe() -> dict:
    """在子进程里问 ctranslate2：能不能真的用 CUDA。"""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--probe-cuda"]
    else:
        cmd = [sys.executable, "-m", "app.probe"]
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        # cwd 固定到程序根：从别的目录启动 run.py 时，`python -m app.probe` 才找得到包
        p = _run(cmd, timeout=25, cwd=str(app_paths.APP_ROOT), env=env)
    except subprocess.TimeoutExpired:
        return {"cuda_devices": 0, "compute_types": [], "error": "CUDA 探测超时（25 秒）"}
    except OSError as e:
        return {"cuda_devices": 0, "compute_types": [], "error": f"CUDA 探测启动失败：{e}"}

    # 子进程可能先打印警告，最后一行才是 JSON；倒着找第一行能解析的。
    for line in reversed((p.stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "cuda_devices" in data:
            return {
                "cuda_devices": int(data.get("cuda_devices") or 0),
                "compute_types": list(data.get("compute_types") or []),
                "error": str(data.get("error") or ""),
            }
    err = (p.stderr or p.stdout or "").strip()
    return {
        "cuda_devices": 0,
        "compute_types": [],
        "error": (err[-300:] or "CUDA 探测没有返回结果"),
    }


def _cuda_summary(gpu: dict, probe: dict) -> dict:
    from . import cuda_bootstrap  # 延迟导入，避免循环

    available = bool(probe.get("cuda_devices")) and not probe.get("error")
    reason = ""
    if available and not cuda_bootstrap.has_cublas():
        available = False
        reason = "检测到 NVIDIA 显卡，但运行库里缺 cublas（CPU 轻量包就是这样）；要用 GPU 请使用完整包或源码安装"
    if not available and not reason:
        if probe.get("error"):
            reason = f"CUDA 运行库不可用：{probe['error']}"
        elif gpu.get("present"):
            reason = "检测到 NVIDIA 显卡，但 CUDA 设备数为 0（驱动或运行库不完整）"
        else:
            reason = "没有检测到可用的 NVIDIA CUDA 设备"
    return {
        "available": available,
        "compute_types": list(probe.get("compute_types") or []) if available else [],
        "reason": reason,
    }


# ---------------------------------------------------------------- ffmpeg

def ffmpeg_info() -> dict:
    """ffmpeg 是否可用。先看随包/项目目录，再看 PATH。"""
    from . import downloader  # 延迟导入：downloader 会 import yt_dlp

    bin_dir = downloader.find_ffmpeg_bin()
    if not bin_dir:
        return {"available": False, "path": "", "version": "", "error": "没有找到 ffmpeg"}
    exe = Path(bin_dir) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not exe.is_file():
        exe = Path(bin_dir) / "ffmpeg"
    try:
        p = _run([str(exe), "-version"], timeout=6)
        first = ((p.stdout or "").splitlines() or [""])[0].strip()
        return {"available": p.returncode == 0, "path": str(exe), "version": first[:120],
                "error": "" if p.returncode == 0 else first[:200]}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"available": False, "path": str(exe), "version": "", "error": str(e)[:200]}


# ---------------------------------------------------------------- 模型目录

def _writable(path: Path) -> bool:
    try:
        app_paths.ensure_dir(path)
        probe = path / f".bili-note-write-test-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
    except OSError:
        return False
    # Windows 上刚写完的文件偶尔会被杀软/索引短暂占用，删不掉不等于不能写。
    # 写成功就算可写；清理失败留个无害的小文件，下次扫描顺手清掉。
    for _ in range(3):
        try:
            probe.unlink()
            break
        except OSError:
            time.sleep(0.1)
    return True


def _cleanup_write_probes(path: Path) -> None:
    try:
        for old in path.glob(".bili-note-write-test-*"):
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


def models_dir_info() -> dict:
    path = app_paths.models_dir()
    exists = path.is_dir()
    _cleanup_write_probes(path)
    return {
        "path": str(path),
        "exists": exists,
        "writable": _writable(path) if exists or not path.exists() else False,
        "free_gb": round(app_paths.free_gb(path), 1),
        "default": str(app_paths.default_models_dir()),
        "is_default": str(path).lower() == str(app_paths.default_models_dir()).lower(),
    }


# ---------------------------------------------------------------- 汇总

def profile(refresh: bool = False) -> dict:
    global _CACHE, _CACHE_AT
    now = time.time()
    if not refresh and _CACHE is not None and now - _CACHE_AT < _CACHE_TTL:
        return _CACHE

    gpu = gpu_info()
    probe = cuda_probe()
    data = {
        "platform": {
            "os": _os_label(),
            "python": platform.python_version(),
            "frozen": app_paths.is_frozen(),
            "app_root": str(app_paths.APP_ROOT),
            "data_dir": str(app_paths.DATA_DIR),
        },
        "cpu": {"cores": os.cpu_count() or 0},
        "ram_gb": _ram_gb(),
        "gpu": gpu,
        "cuda": _cuda_summary(gpu, probe),
        "ffmpeg": ffmpeg_info(),
        "models_dir": models_dir_info(),
    }
    _CACHE, _CACHE_AT = data, now
    return data


def invalidate() -> None:
    global _CACHE, _CACHE_AT
    _CACHE, _CACHE_AT = None, 0.0


def machine_note(profile_data: dict | None = None) -> str:
    """给前端看的一句人话总结。"""
    p = profile_data or profile()
    gpu, cuda = p["gpu"], p["cuda"]
    if cuda["available"]:
        return f"GPU 可用 · {gpu.get('name') or 'NVIDIA'} {gpu.get('vram_mb', 0)}MB"
    if gpu.get("present"):
        return f"检测到 {gpu.get('name')}，但 CUDA 不可用，将用 CPU"
    return "CPU 模式"
