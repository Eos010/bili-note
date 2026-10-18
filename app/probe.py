"""CUDA 能力探针：在**子进程**里 import ctranslate2，避免坏驱动把主服务带崩。

跑法：
    python -m app.probe            # 源码
    bili-note.exe --probe-cuda     # 冻结态（由 run.py 分派）

输出最后一行是 JSON：
    {"cuda_devices": 1, "compute_types": ["float16", ...], "error": ""}
"""
from __future__ import annotations

import json

from . import cuda_bootstrap


def probe_cuda() -> dict:
    cuda_bootstrap.apply()
    try:
        import ctranslate2

        devices = int(ctranslate2.get_cuda_device_count())
        types: list[str] = []
        if devices > 0:
            types = sorted(ctranslate2.get_supported_compute_types("cuda"))
        return {"cuda_devices": devices, "compute_types": types, "error": ""}
    except Exception as e:  # noqa: BLE001 —— 任何异常都要变成可读原因，不能炸掉调用方
        return {
            "cuda_devices": 0,
            "compute_types": [],
            "error": f"{type(e).__name__}: {e}",
        }


if __name__ == "__main__":
    print(json.dumps(probe_cuda(), ensure_ascii=False))
