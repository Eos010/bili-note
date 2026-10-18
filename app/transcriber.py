"""转写模块：faster-whisper 封装。

V3 的两条硬规则：

1. **只从模型目录加载**。`models.require()` 给绝对路径，模型不存在就抛
   `ModelNotReady`——绝不让 faster-whisper 在请求线程里静默联网拉几个 G。
2. **设备策略集中在这里**。`auto` = CUDA 探测通过才用 GPU，加载失败自动回退 CPU，
   并把原因带回前端；用户显式选 `cuda` 时失败则明确报错，不偷偷降级。
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from faster_whisper import WhisperModel

from . import config as app_config
from . import hardware, models
from .errors import CancelledError, ModelNotReady

DEFAULT_MODEL = models.DEFAULT_MODEL
AVAILABLE_MODELS = [m.name for m in models.CATALOG]

# 缓存键必须带 path：模型目录改了以后，旧路径的缓存不能继续冒充新模型。
_models: dict[tuple[str, str, str], tuple[WhisperModel, str, str]] = {}


def _cuda_compute_type(prof: dict | None = None) -> str:
    """挑一个显卡支持的精度。ctranslate2 的返回值是字符串集合。"""
    prof = prof or hardware.profile()
    supported = set((prof.get("cuda") or {}).get("compute_types") or [])
    for candidate in ("float16", "int8_float16", "float32"):
        if candidate in supported:
            return candidate
    return "float16"


def _cpu_threads() -> int:
    try:
        return max(0, int((app_config.load().get("transcribe") or {}).get("cpu_threads") or 0))
    except (TypeError, ValueError):
        return 0


def _load(name: str, device_mode: str, path: str) -> tuple[WhisperModel, str, str]:
    prof = hardware.profile()
    cuda_ok = bool((prof.get("cuda") or {}).get("available"))
    note = ""

    if device_mode in ("auto", "cuda"):
        if not cuda_ok:
            reason = (prof.get("cuda") or {}).get("reason") or "CUDA 不可用"
            if device_mode == "cuda":
                raise ModelNotReady(
                    f"你选择了 GPU 转写，但这台机器现在用不了 CUDA：{reason}\n"
                    "→ 到「设置 → 转写模型」改成「自动」或「CPU」，或者重新安装 GPU 运行库。"
                )
            note = reason
        else:
            try:
                model = WhisperModel(
                    path, device="cuda", compute_type=_cuda_compute_type(prof),
                    cpu_threads=_cpu_threads(),
                )
                return model, "cuda", ""
            except Exception as e:  # noqa: BLE001 —— 驱动/OOM/缺 DLL 都算，回退 CPU
                if device_mode == "cuda":
                    raise ModelNotReady(
                        f"GPU 加载失败：{e}\n→ 改成「自动」或「CPU」可以继续转写（速度会慢）。"
                    ) from e
                note = f"CUDA 加载失败，已回退 CPU：{str(e)[:240]}"

    model = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=_cpu_threads())
    return model, "cpu", note


def get_model(
    name: Optional[str] = None,
    device_mode: Optional[str] = None,
) -> tuple[WhisperModel, str, str]:
    """懒加载指定模型，返回 `(model, device, note)`。已加载则复用。"""
    name = name or models.current_model()
    cfg = app_config.load().get("transcribe") or {}
    device_mode = (device_mode or cfg.get("device") or "auto").lower()
    if device_mode not in ("auto", "cpu", "cuda"):
        device_mode = "auto"

    path = models.require(name)  # 不存在 → ModelNotReady（中文、可照做）
    key = (name, device_mode, str(path))
    if key in _models:
        return _models[key]

    # 同一个模型换了目录：把旧路径的缓存丢掉，避免两张模型同时占显存。
    for old_key in [k for k in _models if k[0] == name and k != key]:
        _models.pop(old_key, None)

    model, device, note = _load(name, device_mode, str(path))
    _models[key] = (model, device, note)
    return model, device, note


def _fmt_srt(seconds: float) -> str:
    """SRT 时间戳：HH:MM:SS,mmm"""
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_txt(seconds: float) -> str:
    """文本稿时间戳：[HH:MM:SS]"""
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _srt_block(index: int, start: float, end: float, text: str) -> str:
    return f"{index}\n{_fmt_srt(start)} --> {_fmt_srt(end)}\n{text.strip()}\n"


def transcribe(
    media_path: str,
    output_dir: str,
    base_name: str,
    progress_cb: Callable[[dict], None],
    cancelled: Callable[[], bool] = lambda: False,
    language: Optional[str] = None,
    model_name: Optional[str] = None,
) -> tuple[str, str]:
    """转写媒体文件，写入 <base_name>.txt 和 <base_name>.srt。

    progress_cb 接收 {"phase": "load_model"|"transcribe", "progress": float,
    "device": "cuda"|"cpu", "device_note": str}
    返回 (txt_path, srt_path)。
    """
    model_name = model_name or models.current_model()
    cfg = app_config.load().get("transcribe") or {}
    device_mode = (cfg.get("device") or "auto").lower()
    beam_size = int(cfg.get("beam_size") or 5)

    progress_cb({"phase": "load_model", "progress": 0.0, "device": "",
                 "device_note": "加载模型…"})
    model, device, note = get_model(model_name, device_mode)
    progress_cb({"phase": "transcribe", "progress": 0.0, "device": device,
                 "device_note": note})

    try:
        duration, segs = _collect_segments(
            model, media_path, language, device, progress_cb, cancelled, beam_size, note
        )
    except ModelNotReady:
        raise
    except Exception as e:  # noqa: BLE001
        # 运行中 CUDA 崩（OOM / 驱动重置）：auto 模式下换 CPU 重跑一遍。
        if device == "cuda" and device_mode == "auto":
            note = f"GPU 转写中断，已回退 CPU 重试：{str(e)[:200]}"
            model, device, _ = _load(model_name, "cpu", str(models.require(model_name)))
            progress_cb({"phase": "transcribe", "progress": 0.0, "device": device,
                         "device_note": note})
            duration, segs = _collect_segments(
                model, media_path, language, device, progress_cb, cancelled, beam_size, note
            )
        else:
            raise

    srt_lines: list[str] = []
    txt_lines: list[str] = []
    for i, seg in enumerate(segs, start=1):
        text = seg.text.strip()
        srt_lines.append(_srt_block(i, seg.start, seg.end, text))
        txt_lines.append(f"[{_fmt_txt(seg.start)}] {text}")

    txt_path = os.path.join(output_dir, base_name + ".txt")
    srt_path = os.path.join(output_dir, base_name + ".srt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(txt_lines))
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))
    progress_cb({"phase": "transcribe", "progress": 100.0, "device": device,
                 "device_note": note})
    return txt_path, srt_path


def _collect_segments(model, media_path, language, device, progress_cb, cancelled,
                      beam_size: int = 5, device_note: str = ""):
    """先开 VAD 过滤静音（长视频更快）；若全空或覆盖率过低（如歌曲/音乐场景
    VAD 误判为无语音），自动降级为无 VAD 重跑。返回 (duration, [segments])。"""
    last = (0.0, [])
    for vad in (True, False):
        kwargs = dict(language=language, beam_size=beam_size, vad_filter=vad)
        if vad:
            kwargs["vad_parameters"] = {"min_silence_duration_ms": 500}
        segments, info = model.transcribe(media_path, **kwargs)
        duration = info.duration or 0.0
        segs = []
        for seg in segments:
            if cancelled():
                raise CancelledError("转写已取消")
            segs.append(seg)
            if duration > 0:
                progress_cb({
                    "phase": "transcribe",
                    "progress": min(99.0, seg.end / duration * 100),
                    "device": device,
                    "device_note": device_note,
                })
        last = (duration, segs)
        covered = sum(s.end - s.start for s in segs)
        if segs and (duration == 0 or covered / duration > 0.3):
            return duration, segs
    return last
