"""转写模块：faster-whisper 封装。large-v3-turbo，GPU 优先 CPU 兜底，产 txt + srt。"""
from __future__ import annotations

import os
from typing import Callable, Optional

from faster_whisper import WhisperModel

from .errors import CancelledError

# 默认转写模型；可选项供前端下拉展示
DEFAULT_MODEL = os.environ.get("BILI_NOTE_MODEL", "large-v3")
AVAILABLE_MODELS = ["large-v3", "large-v3-turbo"]

# 按名称缓存已加载的模型（GPU 显存充足时两个都常驻，切换零开销）
_models: dict[str, tuple[WhisperModel, str]] = {}


def get_model(name: str = DEFAULT_MODEL) -> tuple[WhisperModel, str]:
    """懒加载指定模型：先试 CUDA，失败回退 CPU。已加载则直接复用。"""
    if name in _models:
        return _models[name]
    try:
        model = WhisperModel(name, device="cuda", compute_type="float16")
        device = "cuda"
    except Exception:
        model = WhisperModel(name, device="cpu", compute_type="int8")
        device = "cpu"
    _models[name] = (model, device)
    return model, device


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
    model_name: str = DEFAULT_MODEL,
) -> tuple[str, str]:
    """转写媒体文件，写入 <base_name>.txt 和 <base_name>.srt。

    progress_cb 接收 {"phase": "load_model"|"transcribe", "progress": float, ...}
    返回 (txt_path, srt_path)。
    """
    model, device = get_model(model_name)
    progress_cb({"phase": "transcribe", "progress": 0.0, "device": device})

    duration, segs = _collect_segments(model, media_path, language, device, progress_cb, cancelled)

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
    progress_cb({"phase": "transcribe", "progress": 100.0, "device": device})
    return txt_path, srt_path


def _collect_segments(model, media_path, language, device, progress_cb, cancelled):
    """先开 VAD 过滤静音（长视频更快）；若全空或覆盖率过低（如歌曲/音乐场景
    VAD 误判为无语音），自动降级为无 VAD 重跑。返回 (duration, [segments])。"""
    last = (0.0, [])
    for vad in (True, False):
        kwargs = dict(language=language, beam_size=5, vad_filter=vad)
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
                })
        last = (duration, segs)
        covered = sum(s.end - s.start for s in segs)
        if segs and (duration == 0 or covered / duration > 0.3):
            return duration, segs
    return last
