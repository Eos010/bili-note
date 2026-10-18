"""下载模块：yt-dlp 封装。解析链接 → 选格式 → 下载到 output/<标题>/"""
from __future__ import annotations

import glob
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import yt_dlp

from .errors import CancelledError, is_transient

# B站 1080P 及以下优先，无则取该视频最高可用；单文件格式优先（免合并）
FORMAT_SELECTION = "b/bv*[height<=1080]+ba/b"

# 下载中断时会留下的半成品；取消/失败后按这些模式清理（只在 output/ 自己的目录里动手）
PARTIAL_GLOBS = ("*.part", "*.part-*", "*.ytdl", "*.ytdl-*")

# 网络健壮性参数：**解析和下载共用同一套**。
# 2026-09-17 教训：resolve() 原来只有 quiet/no_warnings/skip_download，
# 于是同样的网络抖动，下载能扛、解析直接死。两边必须同配置。
NETWORK_OPTS = {
    "socket_timeout": 120,
    "retries": 10,
    "fragment_retries": 10,
    "extractor_retries": 3,
}

# 解析失败的额外重试（yt-dlp 内部的 extractor_retries 覆盖不到网页读取超时）
RESOLVE_ATTEMPTS = 3
RESOLVE_BACKOFF = 1.5


def retry_checkpoint(cancelled: Callable[[], bool]):
    """给 yt-dlp 的**重试路径**装一个取消检查点。

    ⚠️ 2026-09-17 根因（用户报「下载一半点取消没反应」的第二层）：
    把取消检查挂在 `progress_hooks` 上是**不够的**——yt-dlp 的 `report_retry`
    不会调用 `_hook_progress`，所以「DNS 失败 → 重试 10 次」那整段里
    一个钩子都不响，取消信号永远读不到，任务要等自己超时才结束。

    yt-dlp 每次重试前都会调 `retry_sleep_functions[...]` 算休眠时长
    （`RetryManager.report_retry` 里的 `sleep_func(n=count-1)`），
    这是重试路径上唯一的注入点。从这里抛异常能直接炸掉重试循环。

    返回值固定 0.0：yt-dlp 默认就是 `None`（**不退避**），我们只加检查点，
    不改变原有退避行为。
    """

    def _fn(*args, n=None, **kwargs):  # noqa: ARG001 —— yt-dlp 以 n= 关键字调用
        if cancelled():
            raise CancelledError("下载已取消")
        return 0.0

    return _fn


def _retry_opts(cancelled: Callable[[], bool]) -> dict:
    """四个重试通道全装上检查点（http/fragment 是下载，extractor 是解析）。"""
    fn = retry_checkpoint(cancelled)
    return {k: fn for k in ("http", "fragment", "file_access", "extractor")}


@dataclass
class VideoInfo:
    url: str
    title: str
    duration: Optional[float] = None
    uploader: Optional[str] = None


def find_ffmpeg_bin() -> Optional[str]:
    """定位 ffmpeg 所在目录：随包/项目 tools → PATH → WinGet 包位置。

    便携包会把 ffmpeg 放在 `<程序根>/tools/ffmpeg/bin`（PyInstaller 下是
    `_internal/tools/ffmpeg/bin`），这时不依赖用户机器上装没装 ffmpeg。
    """
    from . import app_paths

    for candidate in (
        app_paths.TOOLS_DIR / "ffmpeg" / "bin",
        app_paths.TOOLS_DIR / "ffmpeg",
    ):
        exe = candidate / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if exe.is_file():
            return str(candidate)

    p = shutil.which("ffmpeg")
    if p:
        return os.path.dirname(p)
    local = os.environ.get("LOCALAPPDATA", "")
    pattern = os.path.join(
        local, "Microsoft", "WinGet", "Packages",
        "Gyan.FFmpeg*", "ffmpeg-*", "bin",
    )
    for hit in glob.glob(pattern):
        if os.path.exists(os.path.join(hit, "ffmpeg.exe")):
            return hit
    return None


def resolve(url: str, cancelled: Callable[[], bool] = lambda: False) -> VideoInfo:
    """仅解析，不下载：取标题/时长/清晰度判断。

    和 `download()` 用同一套网络参数，对**瞬时网络错误**额外重试——
    yt-dlp 自带的 `extractor_retries` 覆盖不到"网页读取超时"这一类。
    重试之间也会检查取消（用户点了取消就不该继续耗）。
    """
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        **NETWORK_OPTS,
        "retry_sleep_functions": _retry_opts(cancelled),
    }
    last: Optional[Exception] = None

    for attempt in range(RESOLVE_ATTEMPTS):
        if cancelled():
            raise CancelledError("解析已取消")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            title = info.get("title") or info.get("id", url)
            return VideoInfo(
                url=url,
                title=sanitize_filename(title),
                duration=info.get("duration"),
                uploader=info.get("uploader"),
            )
        except CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— 判断是否值得重试
            last = e
            if attempt < RESOLVE_ATTEMPTS - 1 and is_transient(e):
                time.sleep(RESOLVE_BACKOFF * (attempt + 1))
                continue
            raise
    raise last if last else RuntimeError("解析失败")


def download(
    url: str,
    output_dir: str,
    progress_cb: Callable[[dict], None],
    cancelled: Callable[[], bool] = lambda: False,
) -> tuple[str, str]:
    """下载视频到 output_dir，返回 (视频文件路径, 输出目录)。

    progress_cb 接收 yt-dlp 的 progress dict。
    """
    ffmpeg_bin = find_ffmpeg_bin()
    if ffmpeg_bin is None:
        raise RuntimeError("未找到 ffmpeg。请安装 ffmpeg 后重试（yt-dlp 合并音视频需要）。")

    opts = {
        "format": FORMAT_SELECTION,
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "ffmpeg_location": ffmpeg_bin,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_cb],
        "noplaylist": True,
        # 健壮性：B 站 CDN 弱网下常见超时/断流（与 resolve() 共用同一套）
        **NETWORK_OPTS,
        # 取消检查点：进度钩子在重试期间不触发，必须额外挂这里
        "retry_sleep_functions": _retry_opts(cancelled),
        "concurrent_fragment_downloads": 1,
    }

    def _maybe_cancel(d):
        if cancelled():
            raise CancelledError("下载已取消")

    def _maybe_cancel_pp(d):
        # 合并（ffmpeg）阶段不会触发 progress 回调，用 postprocessor 钩子在阶段边界补一道检查
        if cancelled():
            raise CancelledError("下载已取消")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.add_progress_hook(_maybe_cancel)
            ydl.add_postprocessor_hook(_maybe_cancel_pp)
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        # 保险丝：yt-dlp 会把内部异常包装成 DownloadError，可能把我们的取消信号一起吞掉。
        # 只要用户已经按过取消，就一律按「已取消」上报，不再冒充"失败"。
        if cancelled():
            raise CancelledError("下载已取消") from e
        raise

    # 定位实际产物（可能是 .mp4 或 .m4s 等扩展名）
    candidates = []
    requested = info.get("requested_downloads") or []
    for d in requested:
        p = d.get("filepath")
        if p and os.path.exists(p):
            candidates.append(p)
    if not candidates:
        for f in os.listdir(output_dir):
            if f != "audio.wav":
                candidates.append(os.path.join(output_dir, f))
    if not candidates:
        raise RuntimeError("下载完成但未找到视频文件")
    # 优先 mp4/mkv，其次任意
    candidates.sort(key=lambda p: (not p.lower().endswith((".mp4", ".mkv")), len(p)))
    return candidates[0], output_dir


def download_audio(
    url: str,
    temp_dir: str,
    progress_cb: Callable[[dict], None],
    cancelled: Callable[[], bool] = lambda: False,
) -> str:
    """仅转写模式：拉音频流到临时目录，返回音频文件路径。"""
    ffmpeg_bin = find_ffmpeg_bin()
    opts = {
        "format": "bestaudio/b",
        "outtmpl": os.path.join(temp_dir, "%(title)s.%(ext)s"),
        "ffmpeg_location": ffmpeg_bin,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_cb],
        "noplaylist": True,
        **NETWORK_OPTS,
        # 取消检查点：进度钩子在重试期间不触发，必须额外挂这里
        "retry_sleep_functions": _retry_opts(cancelled),
        "concurrent_fragment_downloads": 1,
    }

    def _maybe_cancel(d):
        if cancelled():
            raise CancelledError("下载已取消")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.add_progress_hook(_maybe_cancel)
            ydl.add_postprocessor_hook(_maybe_cancel)
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        # 同上：只要按过取消，就一律按「已取消」上报
        if cancelled():
            raise CancelledError("下载已取消") from e
        raise

    candidates = []
    requested = info.get("requested_downloads") or []
    for d in requested:
        p = d.get("filepath")
        if p and os.path.exists(p):
            candidates.append(p)
    if not candidates:
        for f in os.listdir(temp_dir):
            if f.startswith("."):
                continue
            candidates.append(os.path.join(temp_dir, f))
    if not candidates:
        raise RuntimeError("音频下载完成但未找到文件")
    candidates.sort(key=lambda p: len(p))
    return candidates[0]


def sanitize_filename(name: str) -> str:
    """去掉 Windows 非法字符，截断过长标题。"""
    name = name.replace("/", "·").replace("\\", "·")
    for ch in '<>:"|?*':
        name = name.replace(ch, "")
    name = name.strip().strip(".")
    return name[:120] or "video"


def cleanup_partial(directory: str) -> list[str]:
    """删掉目录里 yt-dlp 留下的**半成品**（.part / .ytdl / DASH 分片）。

    只删明确属于"下载到一半"的文件，**绝不碰已完成的成品**（.mp4/.txt/.srt/.summary.md）。
    返回被删掉的路径列表。
    """
    if not directory or not os.path.isdir(directory):
        return []
    removed: list[str] = []
    for pattern in PARTIAL_GLOBS:
        for path in glob.glob(os.path.join(directory, pattern)):
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass
    # DASH 分片：形如 <title>.f299.mp4（合并成功后 yt-dlp 会自己删，中断时才会残留）
    for name in os.listdir(directory):
        if re.search(r"\.f\d+\.[A-Za-z0-9]+$", name):
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    removed.append(path)
                except OSError:
                    pass
    return removed
