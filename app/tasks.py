"""任务管理器：单 worker 串行队列。解析 → 下载(可选) → 转写(可选)。"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from . import downloader, summary_store, transcriber
from .errors import CancelledError, explain

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")

# 本地文件转写支持的媒体扩展名（交给 faster-whisper/PyAV 解码）
_MEDIA_EXTS = {
    ".mp4", ".mkv", ".mov", ".webm", ".flv", ".avi", ".ts", ".m4v", ".mpg", ".mpeg",
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".mka",
}

# 能被浏览器 <video> 播放的扩展名。
# 看着和 `_MEDIA_EXTS` 一样，但**语义不同**，不要合并：一个是"PyAV 能不能解码"，
# 一个是"浏览器能不能播"。哪天加了 PyAV 支持而浏览器不支持的格式（或反过来），
# 合并就会变成"把 .txt 当视频发出去"那类事故（v1.3 踩过）。
_PLAYABLE_EXTS = set(_MEDIA_EXTS)


def is_playable(path: str) -> bool:
    """浏览器能不能播这个文件。**这条规则只在这里定义一次**——
    `media_path()` 和 `library.py` 的媒体挑选都走它。"""
    return os.path.splitext(path or "")[1].lower() in _PLAYABLE_EXTS


def media_path(t: "Task") -> str:
    """任务当前可播放的媒体文件：下载产物优先，本地任务回退到源文件。"""
    candidates = []
    if t.video_path:
        candidates.append(t.video_path)
    if t.kind == "file":
        candidates.append(t.url)
    for path in candidates:
        if path and is_playable(path) and os.path.isfile(path):
            return path
    return ""


def _clean(s: str) -> str:
    return s.strip().strip('"').strip("'")


def _is_local_path(s: str) -> bool:
    """判断是否为本地路径：盘符（D:\\）、UNC（\\\\server）、绝对路径（反斜杠/斜杠开头）。"""
    if len(s) >= 2 and s[0].isalpha() and s[1] == ":":
        return True
    return s.startswith("\\\\") or s.startswith("/") or s.startswith("\\")


# ---------------------------------------------------------------- 删除产物
#
# 安全铁律（2026-09-16 加）：
#  1. 只有 `output/<标题>/` 这种**我们自己建的**目录才允许整目录删；
#  2. 本地文件任务的目录是**用户的目录**，只删我们生成的文件，绝不删目录；
#  3. 如果任务是「粘 .txt 文稿」建的，那个 .txt 是**他的原始资料**（不是我们生成的），
#     一律不删——只删我们产出的 `.summary.md`。
# 删除前必须先出清单（plan_delete_files）给用户看。


def _is_ours(path: str) -> bool:
    """路径是否严格位于 OUTPUT_DIR 之内（排除 OUTPUT_DIR 本身和临时目录）。"""
    if not path:
        return False
    root = os.path.abspath(OUTPUT_DIR)
    target = os.path.abspath(path)
    if target == root or not target.startswith(root + os.sep):
        return False
    return os.path.basename(target) != ".tmp"


def _summary_of(t: "Task") -> str:
    """总结正文路径。规则在 `summary_store` 里，全项目只有那一处定义。"""
    return summary_store.summary_path(t)


def _summary_extras(t: "Task") -> list[str]:
    """总结的附属产物：骨架速览文件 + 长稿流程的分节缓存目录。"""
    return summary_store.extras(t)


def _is_manuscript_task(t: "Task") -> bool:
    """任务是不是「粘 .txt 文稿」建的 —— 那 .txt 是源资料，不是产物。"""
    return t.kind == "file" and os.path.splitext(t.url or "")[1].lower() == ".txt"


def plan_delete_files(t: "Task") -> dict:
    """只算不删：返回 {targets, kept, mode, note}。前端拿它给用户确认。"""
    summary = _summary_of(t)
    extras = _summary_extras(t)

    if t.kind == "url":
        d = t.output_dir
        if d and _is_ours(d) and os.path.isdir(d):
            return {
                "mode": "dir",
                "targets": [d],
                "kept": [],
                "note": "整个任务目录都会被删除（视频、文稿、字幕、总结都在里面）。",
            }
        # 兜底：目录不在 output/ 内（异常情况）→ 退化成只删成果文件
        targets = [
            p for p in (t.txt_path, t.srt_path, summary, *extras)
            if p and os.path.exists(p)
        ]
        return {
            "mode": "files",
            "targets": targets,
            "kept": [p for p in (d,) if p and os.path.isdir(p)],
            "note": "任务目录不在 output/ 内，只删除生成的文稿/字幕/总结，目录保留。",
        }

    # 本地文件任务
    if _is_manuscript_task(t):
        targets = [p for p in (summary, *extras) if p and os.path.exists(p)]
        return {
            "mode": "files",
            "targets": targets,
            "kept": [p for p in (t.txt_path, t.srt_path) if p],
            "note": "这是你自己的 .txt 文稿，不会被删；只删我们生成的总结。",
        }
    targets = [
        p for p in (t.txt_path, t.srt_path, summary, *extras) if p and os.path.exists(p)
    ]
    return {
        "mode": "files",
        "targets": targets,
        "kept": [p for p in (t.url,) if p and os.path.isfile(p)],
        "note": "只删我们生成的文稿/字幕/总结；原视频文件保留。",
    }


def delete_task_files(t: "Task") -> dict:
    """按 plan 执行删除。返回 {removed, kept, errors}。"""
    import shutil

    plan = plan_delete_files(t)
    removed: list[str] = []
    errors: list[str] = []

    for target in plan["targets"]:
        try:
            if os.path.isdir(target):
                shutil.rmtree(target)
            elif os.path.isfile(target):
                os.remove(target)
            removed.append(target)
        except OSError as e:
            errors.append(f"{target}：{e}")

    # 顺手清掉可能残留的半成品（只在 output/ 内）
    if t.kind == "url":
        if t.output_dir and _is_ours(t.output_dir):
            downloader.cleanup_partial(t.output_dir)
        downloader.cleanup_partial(os.path.join(OUTPUT_DIR, ".tmp"))

    return {"removed": removed, "kept": plan["kept"], "errors": errors}


@dataclass
class Task:
    id: str
    url: str
    download: bool
    transcribe: bool
    kind: str = "url"              # url / file
    model: str = transcriber.DEFAULT_MODEL
    status: str = "queued"          # queued/resolving/downloading/transcribing/done/error/cancelled
    title: str = ""
    progress: float = 0.0
    phase_detail: str = ""
    output_dir: str = ""
    video_path: str = ""
    txt_path: str = ""
    srt_path: str = ""
    error: str = ""
    cancel_requested: bool = False
    created_at: float = field(default_factory=time.time)

    @property
    def cancellable(self) -> bool:
        """还在跑的阶段才能取消（前端按钮的显隐和这里共用同一个判定）。

        ⚠️ 2026-09-17 教训：这个判定原来**只写在 `to_dict()` 的字典里**（给前端看），
        而 `TaskManager.cancel()` 却写成 `t.cancellable` 去读**对象属性** ——
        属性根本不存在，于是每次点取消都 AttributeError → 接口 500 → 前端毫无反应。
        「点击取消没反应」的真根因就是这一行。判定必须只有一处定义。
        """
        return self.status in ("queued", "resolving", "downloading", "transcribing")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title or self.url,
            "kind": self.kind,
            "model": self.model,
            "status": self.status,
            "progress": round(self.progress, 1),
            "phase_detail": self.phase_detail,
            "download": self.download,
            "transcribe": self.transcribe,
            "video_path": self.video_path,
            "txt_path": self.txt_path,
            "srt_path": self.srt_path,
            "error": self.error,
            "cancellable": self.cancellable,
        }


class TaskManager:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    async def submit(self, urls: list[str], files: list[str], download: bool, transcribe: bool, model: str) -> list[str]:
        ids: list[str] = []
        for u in urls:
            u = _clean(u)
            if not u:
                continue
            kind = "file" if _is_local_path(u) else "url"
            t = Task(
                id=uuid.uuid4().hex[:12], url=u, kind=kind, model=model,
                download=download and kind == "url",
                transcribe=True if kind == "file" else transcribe,
            )
            self.tasks[t.id] = t
            await self._queue.put(t.id)
            ids.append(t.id)
        for f in files:
            f = _clean(f)
            if not f:
                continue
            t = Task(id=uuid.uuid4().hex[:12], url=f, kind="file", model=model, download=False, transcribe=True)
            self.tasks[t.id] = t
            await self._queue.put(t.id)
            ids.append(t.id)
        return ids

    def get(self, task_id: str) -> Optional[Task]:
        return self.tasks.get(task_id)

    def list_all(self) -> list[Task]:
        return sorted(self.tasks.values(), key=lambda t: t.created_at)

    async def cancel(self, task_id: str) -> bool:
        t = self.tasks.get(task_id)
        if t and t.cancellable:
            t.cancel_requested = True
            t.phase_detail = "正在取消…"
            return True
        return False

    async def retry(self, task_id: str) -> bool:
        """把失败/取消的任务重置后重新排队（沿用原来的链接、开关、模型）。"""
        t = self.tasks.get(task_id)
        if t is None or t.status not in ("error", "cancelled"):
            return False
        t.status = "queued"
        t.progress = 0.0
        t.phase_detail = "重新排队…"
        t.error = ""
        t.cancel_requested = False
        t.video_path = ""
        t.txt_path = ""
        t.srt_path = ""
        await self._queue.put(t.id)
        return True

    def remove(self, task_id: str, delete_files: bool = False) -> dict:
        """移除任务。delete_files=True 时连同产物一起删。

        有文件删不掉时**不把任务从列表抹掉**——否则用户看不到失败、也没法重试。
        """
        t = self.tasks.get(task_id)
        if t is None:
            return {"ok": False, "reason": "任务不存在"}
        if t.status not in ("done", "error", "cancelled"):
            return {"ok": False, "reason": "任务还在跑，请先取消再删除"}

        removed: list[str] = []
        kept: list[str] = []
        if delete_files:
            result = delete_task_files(t)
            if result["errors"]:
                return {
                    "ok": False,
                    "reason": "部分文件删除失败（可能被占用）：" + "；".join(result["errors"]),
                    "removed": result["removed"],
                    "kept": result["kept"],
                }
            removed, kept = result["removed"], result["kept"]

        del self.tasks[task_id]
        return {"ok": True, "removed": removed, "kept": kept}

    # ---- 内部 ----

    async def _run(self) -> None:
        while True:
            tid = await self._queue.get()
            t = self.tasks.get(tid)
            if t is not None:
                await self._process(t)
            self._queue.task_done()

    async def _process(self, t: Task) -> None:
        try:
            if t.kind == "file":
                await self._process_file(t)
            else:
                await self._process_url(t)

            if t.cancel_requested:
                raise CancelledError()

            t.status = "done"
            t.progress = 100.0
            t.phase_detail = "完成"
        except CancelledError:
            t.status = "cancelled"
            t.phase_detail = "已取消"
            t.error = ""
            self._cleanup_temp(t)
            # 取消是"我不要了"→ 清掉半成品
            self._cleanup_partial(t)
        except Exception as e:  # noqa: BLE001 —— 任务级兜底，错误返回给前端
            t.status = "error"
            t.error = explain(e)
            t.phase_detail = "失败"
            self._cleanup_temp(t)
            # 失败是"没成，还想再试"→ **保留 .part 让重试能断点续传**，不清

    async def _process_url(self, t: Task) -> None:
        # --- 1. 解析 ---
        t.status = "resolving"
        t.phase_detail = "解析链接…"
        info = await asyncio.to_thread(
            downloader.resolve, t.url, cancelled=lambda: t.cancel_requested
        )
        t.title = info.title
        t.phase_detail = f"已解析：{t.title}"

        if t.cancel_requested:
            raise CancelledError()

        # --- 2. 确定媒体源与目录 ---
        # 下载+转写 / 仅下载 → 保存视频到 output/<标题>/
        # 仅转写 → 拉音频到临时目录，转写完删
        t.output_dir = os.path.join(OUTPUT_DIR, info.title)
        os.makedirs(t.output_dir, exist_ok=True)
        if t.download:
            media_path = await self._do_download(t, t.url, t.output_dir, t.title)
            audio_is_temp = False
        else:
            temp_dir = os.path.join(OUTPUT_DIR, ".tmp")
            os.makedirs(temp_dir, exist_ok=True)
            media_path = await self._do_download_audio(t, t.url, temp_dir, t.title)
            audio_is_temp = True

        if t.cancel_requested:
            raise CancelledError()

        # --- 3. 转写 ---
        if t.transcribe:
            await self._do_transcribe(t, media_path, audio_is_temp)

    async def _process_file(self, t: Task) -> None:
        """本地文件：不下载、不解析，直接转写，结果写在源文件同目录。

        特例：`.txt` 视为「已有文稿」，跳过转写直接标记完成——这样可以直接
        对上一次就跑好的文稿做总结，不必重新转写。
        """
        t.status = "resolving"
        t.phase_detail = "检查文件…"
        path = os.path.abspath(t.url)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"文件不存在：{path}")
        ext = os.path.splitext(path)[1].lower()
        if ext != ".txt" and ext not in _MEDIA_EXTS:
            raise ValueError(f"不支持的文件类型：{ext or '无扩展名'}（支持视频/音频文件，或已有的 .txt 文稿）")

        base = os.path.basename(path)
        t.title = os.path.splitext(base)[0]
        t.output_dir = os.path.dirname(path)

        if ext == ".txt":
            srt = os.path.splitext(path)[0] + ".srt"
            t.txt_path = path
            t.srt_path = srt if os.path.isfile(srt) else ""
            t.progress = 100.0
            t.phase_detail = "已有文稿（跳过转写）"
            return

        t.phase_detail = f"转写本地文件：{base}"

        if t.cancel_requested:
            raise CancelledError()

        await self._do_transcribe(t, path, audio_is_temp=False)

    def _cleanup_temp(self, t: Task) -> None:
        """仅转写模式的临时音频，清理。"""
        if not t.download and t.video_path and os.path.exists(t.video_path):
            try:
                os.remove(t.video_path)
            except OSError:
                pass

    def _cleanup_partial(self, t: Task) -> None:
        """清掉下载中断留下的半成品（.part / .ytdl / DASH 分片）。

        **只在 output/ 自己的目录里动手**：
        - URL 任务：产物目录 `output/<标题>/` 是我们建的，清；
        - 仅转写任务：临时音频目录 `output/.tmp/`，清；
        - **本地文件任务：绝对不碰**——它的 output_dir 是用户自己的目录。
        """
        if t.kind != "url":
            return
        targets = []
        if t.output_dir and os.path.abspath(t.output_dir).startswith(os.path.abspath(OUTPUT_DIR)):
            targets.append(t.output_dir)
        if not t.download:
            targets.append(os.path.join(OUTPUT_DIR, ".tmp"))
        for directory in targets:
            downloader.cleanup_partial(directory)

    async def _do_download(self, t: Task, url: str, out_dir: str, base: str) -> str:
        t.status = "downloading"
        t.phase_detail = "准备下载…"

        def _progress(d: dict) -> None:
            if t.cancel_requested:
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0.0
            t.progress = pct * 0.45
            speed = d.get("speed") or 0
            spd = f"{speed / 1e6:.1f} MB/s" if speed else "?"
            t.phase_detail = f"下载中 {pct:.0f}%（{spd}）"

        video_path, _ = await asyncio.to_thread(
            downloader.download, url, out_dir, _progress,
            cancelled=lambda: t.cancel_requested,
        )
        t.video_path = video_path
        t.progress = 45.0
        t.phase_detail = "下载完成"
        return video_path

    async def _do_download_audio(self, t: Task, url: str, temp_dir: str, base: str) -> str:
        t.status = "downloading"
        t.phase_detail = "准备拉取音频…"

        def _progress(d: dict) -> None:
            if t.cancel_requested:
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0.0
            t.progress = pct * 0.45
            t.phase_detail = f"拉取音频 {pct:.0f}%"

        audio_path = await asyncio.to_thread(
            downloader.download_audio, url, temp_dir, _progress,
            cancelled=lambda: t.cancel_requested,
        )
        t.video_path = audio_path
        t.progress = 45.0
        t.phase_detail = "音频就绪"
        return audio_path

    async def _do_transcribe(self, t: Task, media_path: str, audio_is_temp: bool) -> None:
        t.status = "transcribing"
        t.phase_detail = "加载模型…"
        base = os.path.splitext(os.path.basename(media_path))[0]
        out_dir = t.output_dir or os.path.dirname(media_path)

        def _progress(d: dict) -> None:
            if t.cancel_requested:
                return
            pct = d.get("progress", 0.0)
            t.progress = 45.0 + pct * 0.55
            device = d.get("device", "")
            t.phase_detail = f"转写中 {pct:.0f}%（{'GPU' if device == 'cuda' else 'CPU'}）"

        txt_path, srt_path = await asyncio.to_thread(
            transcriber.transcribe, media_path, out_dir, base,
            progress_cb=_progress, cancelled=lambda: t.cancel_requested,
            model_name=t.model,
        )
        t.txt_path = txt_path
        t.srt_path = srt_path
        if audio_is_temp:
            self._cleanup_temp(t)
            t.video_path = ""
        t.progress = 100.0
        t.phase_detail = "转写完成"
