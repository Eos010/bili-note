"""`output/` 里「已经转写好的文稿」的自动检索 —— 只读清单。

**为什么要有这个模块**：任务表是内存态（服务一重启就空了），而 `output/` 下的
`.txt` 文稿是**文件事实**、一直都在。以前要总结一份旧文稿，只能把它的路径手动粘到
「本地转写」框里。这个模块就是把那一步变成"打开页面自动扫出来"。

两条设计约束，都是防事故的：

1. **前端只拿 id，不拿路径。** `scan()` 给每份文稿算一个稳定 id（相对路径的 sha1 前 12 位），
   之后所有读取都走 `resolve(id)` —— 重新扫描一遍再比对 id。
   于是"请求里带路径"这件事根本不成立，路径穿越那一类问题不是被过滤掉，而是**没有入口**。
2. **只认自己写的格式。** 转写产物每行形如 `[HH:MM:SS] 正文`（见 `transcriber._fmt_txt`）。
   判定要求非空行 ≥ 3 行、且命中率 ≥ 80%。`output/.tmp/` 里那些 AB 试跑的 `.stats.txt`
   （内容是「文稿正文 6523 字…」）就是这么被挡在外面的——它们也确实是 `.txt`。
"""
from __future__ import annotations

import hashlib
import os
import re
from types import SimpleNamespace

from . import summary_store
from . import tasks

# 转写产物的行格式：`[HH:MM:SS] 正文`
TS_LINE_RE = re.compile(r"^\[(\d{1,2}):(\d{2}):(\d{2})\]")

MIN_LINES = 3          # 少于 3 行的不算文稿（碎片/说明文件）
MIN_TS_RATIO = 0.8     # 带时间戳的行占比下限
MAX_BYTES = 4 * 1024 * 1024   # 超过这个体积的不是 whisper 文稿，别整份读进内存

# 扫描时跳过的目录前缀：`.`（.tmp 试跑区）、`_`（预留的内部目录）
_SKIP_DIR_PREFIX = (".", "_")


def root() -> str:
    """扫描的根目录。**只有 `tasks.OUTPUT_DIR` 一处定义**，这里不另写一份。"""
    return tasks.OUTPUT_DIR


def _item_id(rel: str) -> str:
    return hashlib.sha1(rel.replace("\\", "/").encode("utf-8")).hexdigest()[:12]


def _secs(match: re.Match) -> int:
    h, m, s = (int(x) for x in match.groups())
    return h * 3600 + m * 60 + s


def _fmt(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _pick_media(directory: str, stem: str) -> str:
    """同目录里能播的媒体文件：优先与文稿同名的那份，其次取最大的。"""
    best, best_key = "", None
    try:
        names = os.listdir(directory)
    except OSError:
        return ""
    for name in names:
        full = os.path.join(directory, name)
        if not os.path.isfile(full) or not tasks.is_playable(full):
            continue
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        key = (os.path.splitext(name)[0] == stem, size)
        if best_key is None or key > best_key:
            best, best_key = full, key
    return best


def _describe(base: str, txt_path: str) -> dict | None:
    """读一份候选 .txt，够格就返回条目，不够格（不是转写产物）返回 None。"""
    try:
        if os.path.getsize(txt_path) > MAX_BYTES:
            return None
        with open(txt_path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    hits = [ln for ln in lines if TS_LINE_RE.match(ln)]
    if len(lines) < MIN_LINES or len(hits) / len(lines) < MIN_TS_RATIO:
        return None

    chars = 0
    last = 0
    for ln in lines:
        m = TS_LINE_RE.match(ln)
        if m:
            last = _secs(m)
            chars += len(ln[m.end():].strip())
        else:
            chars += len(ln)

    rel = os.path.relpath(txt_path, base)
    stem = os.path.splitext(os.path.basename(txt_path))[0]
    srt = os.path.splitext(txt_path)[0] + ".srt"
    # 总结产物的路径规则**只有 summary_store 一处定义**，这里借一个只带 txt_path 的壳复用它
    shim = SimpleNamespace(txt_path=txt_path)
    summary = summary_store.summary_path(shim)
    outline = summary_store.outline_path(shim)
    media = _pick_media(os.path.dirname(txt_path), stem)

    summary_chars = 0
    if summary and os.path.isfile(summary):
        try:
            summary_chars = os.path.getsize(summary)
        except OSError:
            summary_chars = 0

    return {
        "id": _item_id(rel),
        "title": stem,
        "dir_name": os.path.basename(os.path.dirname(txt_path)),
        "name": os.path.basename(txt_path),
        "chars": chars,
        "lines": len(lines),
        "duration": _fmt(last),
        "duration_sec": last,
        "mtime": os.path.getmtime(txt_path),
        "has_srt": os.path.isfile(srt),
        "has_summary": bool(summary) and os.path.isfile(summary),
        "has_outline": bool(outline) and os.path.isfile(outline),
        "summary_bytes": summary_chars,
        "has_media": bool(media),
        "media_name": os.path.basename(media) if media else "",
        # 下面两个带下划线 = 内部字段，`public()` 不会发给前端
        "_txt_path": txt_path,
        "_media_path": media,
    }


def scan(base: str | None = None) -> list[dict]:
    """扫一遍产物目录，按修改时间倒序返回文稿条目（新的在前）。"""
    base = os.path.abspath(base or root())
    if not os.path.isdir(base):
        return []
    items: list[dict] = []
    try:
        names = os.listdir(base)
    except OSError:
        return []
    for name in names:
        if name.startswith(_SKIP_DIR_PREFIX):
            continue
        directory = os.path.join(base, name)
        if not os.path.isdir(directory):
            continue
        for fn in sorted(os.listdir(directory)):
            low = fn.lower()
            if not low.endswith(".txt") or low.endswith(".stats.txt"):
                continue
            item = _describe(base, os.path.join(directory, fn))
            if item:
                items.append(item)
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return items


def resolve(item_id: str, base: str | None = None) -> dict | None:
    """按 id 找回条目。**前端永远只发 id，不发路径** —— 这里重新扫一遍再比对。"""
    if not item_id:
        return None
    for item in scan(base):
        if item["id"] == item_id:
            return item
    return None


def public(item: dict) -> dict:
    """去掉内部字段（`_txt_path` / `_media_path`），只留能发给前端的。"""
    return {k: v for k, v in item.items() if not k.startswith("_")}


def read_text(item: dict) -> str:
    with open(item["_txt_path"], "r", encoding="utf-8") as f:
        return f.read()


def read_summary(item: dict) -> tuple[str, str]:
    """返回 `(总结正文, 骨架速览)`；没有的文件给空串。"""
    shim = SimpleNamespace(txt_path=item["_txt_path"])
    return summary_store.read(shim)
