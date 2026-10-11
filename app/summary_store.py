"""总结产物的路径与读写 —— **全项目只在这里定义一次**。

为什么单独提一个模块（2026-09-20，做后台总结时）：

在加后台作业之前，同一套"总结文件放哪儿"的规则已经写了两份——
`tasks.py` 的 `_summary_of()` / `_summary_extras()`（给删除用）和
`main.py` 的 `_summary_path()` / `_outline_path()`（给读写用）。
后台作业需要第三次用到它。**同一条规则写三份，早晚有一份会漂**，
而这一漂就是"删任务删不掉总结"或者"总结写到了别处"这种查半天的 bug。

产物一共三样，全部与 `.txt` 同目录同基名：

| 文件 | 是什么 |
|---|---|
| `<基名>.summary.md` | 总结正文（带 front matter） |
| `<基名>.summary.outline.md` | 长稿流程的「骨架速览」 |
| `<基名>.summary.md.parts/` | 长稿流程的分节缓存（断点续传用，跑完自动清） |
"""
from __future__ import annotations

import os
from datetime import datetime

SUMMARY_SUFFIX = ".summary.md"
OUTLINE_SUFFIX = ".summary.outline.md"
PARTS_SUFFIX = ".summary.md.parts"

# 本地文件任务的 `.txt` 就是**用户自己的原始资料**，不是我们的产物——
# 删任务时永远不碰它（判定在 tasks.plan_delete_files 里）。
_FRONT_MATTER = "---\n"


def _base(t) -> str:
    return os.path.splitext(t.txt_path)[0] if getattr(t, "txt_path", "") else ""


def summary_path(t) -> str:
    base = _base(t)
    return base + SUMMARY_SUFFIX if base else ""


def outline_path(t) -> str:
    base = _base(t)
    return base + OUTLINE_SUFFIX if base else ""


def parts_dir(t) -> str:
    base = _base(t)
    return base + PARTS_SUFFIX if base else ""


def extras(t) -> list[str]:
    """总结的附属产物（骨架速览 + 分节缓存目录）。

    两者都是我们生成的，删任务时要一起清掉；漏掉会在 `output/` 里留下
    `xxx.summary.md.parts/` 这种垃圾（v1.5 踩过，别再漏）。
    """
    if not _base(t):
        return []
    return [outline_path(t), parts_dir(t)]


def exists(t) -> bool:
    p = summary_path(t)
    return bool(p) and os.path.isfile(p)


def read(t) -> tuple[str, str]:
    """返回 `(总结正文, 骨架速览)`；没有的文件给空串。"""
    content = outline = ""
    p = summary_path(t)
    if p and os.path.isfile(p):
        with open(p, "r", encoding="utf-8") as f:
            content = f.read()
    op = outline_path(t)
    if op and os.path.isfile(op):
        with open(op, "r", encoding="utf-8") as f:
            outline = f.read().strip()
    return content, outline


def write(
    t, text: str, template: str, model: str, seg_count: int, duration: str,
    outline: str = "", plan: dict | None = None,
) -> str:
    """落盘总结（+ 可选的骨架速览）。返回总结文件路径。"""
    path = summary_path(t)
    if not path:
        raise ValueError("这个任务没有文字稿，无法写入总结")
    front = (
        _FRONT_MATTER
        + f"source: {os.path.basename(t.txt_path)}\n"
        + f"template: {template}\n"
        + f"model: {model}\n"
        + f"merged_segments: {seg_count}\n"
        + f"duration: {duration}\n"
        + f"created: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    )
    if plan:
        front += (
            f"chars: {plan.get('chars')}\n"
            f"strategy: {plan.get('strategy')}\n"
            f"target: {plan.get('target_lo')}-{plan.get('target_hi')}\n"
        )
    if outline:
        front += f"outline: {os.path.basename(outline_path(t))}\n"
    front += "---\n\n"

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(front + text)
    if outline:
        with open(outline_path(t), "w", encoding="utf-8") as f:
            f.write(outline + "\n")
    return path


def remove(t) -> list[str]:
    """删掉总结正文 + 骨架速览，返回实际删掉的文件。分节缓存不在这里删（跑完自己清）。"""
    removed: list[str] = []
    for p in (summary_path(t), outline_path(t)):
        if p and os.path.isfile(p):
            try:
                os.remove(p)
                removed.append(p)
            except OSError:
                pass
    return removed
