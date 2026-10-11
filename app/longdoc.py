"""长文稿流程：**先骨架、后填肉**（v1.5）。

## 为什么长稿不能一趟写完

2026-09-20 实测（万字稿：正文 8,704 字 / 25 段 / 23:08，模型 deepseek-flash）：

| 指标 | 现行单趟模板 A |
|---|---|
| 输出长度 | 9,765 字 = 原文 **115.9%**（提示词要求 40~60%、硬上限 75%） |
| 引用 | 仅 4.4%，且**3 处引号在原文里根本不存在** |
| 耗时 | 80.2 秒，其中 19,566 字是模型的"思考" |

病根**不是"塞不下"**：这个模型是百万级上下文，8.7k 字正文只占约 0.5%，
`max_tokens` 上限实测是 393,216。真正的病是——**一次生成上万字的连贯长文时，
模型会前紧后松、越写越薄，并且系统性地无视篇幅约束**。

## 为什么不是 map-reduce（分块总结再合并）

- 合并那一步本身又是一次**超长生成**，退化原封不动地回来；
- 每块先压一遍 = 细节被压两次，不可逆；
- 按固定字数切块会切在句子中间。

## 这里的做法：切**输出**，不切输入

1. **骨架**（1 次调用，输出几百字）：只让模型给全文分段——每节的标题、起止时间戳、一句主旨。
   输出短 → 不会退化 → 拿到的结构可靠。顺带白送一份"速览"。
2. **填肉**（每节 1 次调用，输出几百字）：带着**完整骨架 +（默认）全文**，只写这一节。
   每节都是短生成 → 不存在"越写越薄"。
3. **装配**：按顺序拼起来，最终文件格式与单趟模板 A **完全一致**
   （所以前端的时间戳跳转、引用高亮一行都不用改）。

副产品：
- 每节单独落盘 → **断点续传**（失败只重跑那一节，不用从头再花一次钱）；
- 进度是真实的"第 3/9 节"，不是转圈的光标；
- 结构由骨架定死 → 不会塌；时间戳来自骨架 → 可本地校验（真实存在、单调、不重复）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from typing import AsyncIterator, Iterable

from . import quality, summarizer

# 提示词/流程版本：进 manifest，改了它旧的缓存分节就作废（避免用旧提示词拼出新结果）
PROMPT_VERSION = "v1.5-outline-1"

# 超过这个字数就走"骨架→逐节"。低于它的稿子单趟就够，多趟只是多花钱多等待。
DEFAULT_THRESHOLD = 4_000

# 超过这个字数，逐节展开时就不再把**全文**塞给每一次调用（否则 N 节 × 全文，输入费会失控）。
FULL_CONTEXT_MAX = 40_000

_TS_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}$")
_FENCE_RE = re.compile(r"^\s*```")


# ---------------------------------------------------------------- 长度评估


@dataclass
class Plan:
    """开工前对这份稿子的评估结论。既给流程用，也直接给用户看。"""

    chars: int
    tokens: int
    minutes: float
    segments: int
    strategy: str            # single | outline
    tier: str                # 短稿 / 中稿 / 长稿 / 超长稿
    sections_lo: int
    sections_hi: int
    target_lo: int
    target_hi: int
    target_cap: int
    eta_seconds: int
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


def _to_sec(ts: str) -> int:
    h, m, s = (int(x) for x in ts.split(":"))
    return h * 3600 + m * 60 + s


def assess(
    body: str,
    segments: int,
    duration: str,
    threshold: int = DEFAULT_THRESHOLD,
) -> Plan:
    """本地长度评估：**不花钱、不联网**，在发第一个请求之前就把策略定下来。

    注意判据是**质量阈值**（一次能写好多长而不塌），不是容量阈值——
    百万上下文 + 39 万输出上限，容量在可见的未来用不完（万字稿只占 0.5%）。
    """
    chars = len(body)
    tokens = quality.estimate_tokens(body)
    minutes = _to_sec(duration) / 60 if _TS_RE.match(duration or "") else 0.0
    lo, hi, cap = summarizer.length_target(chars)
    sec_lo, sec_hi = summarizer.section_range(chars)

    if chars < threshold:
        strategy, tier = "single", "短稿"
        eta = 20 + int(chars / 120)
        note = (
            f"{tier} {chars:,} 字（约 {tokens:,} token）· 一趟写完 · "
            f"目标 {lo:,}~{hi:,} 字 · 预计 {max(10, eta)} 秒"
        )
    else:
        strategy = "outline"
        tier = "长稿" if chars <= 25_000 else "超长稿"
        # 骨架一趟 + 每节一趟；每节按 12 秒估（思考关掉后的量级，实测 15.7s/6k 字输出）
        eta = 35 + sec_hi * 12
        note = (
            f"{tier} {chars:,} 字（约 {tokens:,} token）· 分 {sec_lo}~{sec_hi} 节逐节生成 · "
            f"目标 {lo:,}~{hi:,} 字 · 预计 {eta // 60} 分 {eta % 60} 秒"
        )

    return Plan(
        chars=chars, tokens=tokens, minutes=round(minutes, 1), segments=segments,
        strategy=strategy, tier=tier,
        sections_lo=sec_lo, sections_hi=sec_hi,
        target_lo=lo, target_hi=hi, target_cap=cap,
        eta_seconds=eta, note=note,
    )


# ---------------------------------------------------------------- 骨架解析


@dataclass
class Section:
    index: int
    title: str
    start: str
    end: str
    gist: str
    start_sec: int = 0
    # 分到这一节的原文段（由 assign_segments 填）
    segs: list[tuple[str, str]] = field(default_factory=list)

    @property
    def body(self) -> str:
        return "\n".join(f"[{ts}] {t}" for ts, t in self.segs)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("segs", None)
        return d


def parse_outline(text: str) -> list[Section]:
    """解析骨架。**容错优先**：只认能认的行，认不出的行跳过，不抛异常。

    模型偶尔会加代码块围栏、全角竖线、把时间戳写成 `[00:00:00]`。这些都容忍。
    """
    out: list[Section] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _FENCE_RE.match(line):
            continue
        parts = [p.strip() for p in re.split(r"[|｜]", line)]
        if len(parts) < 5:
            continue
        _sid, start, end = parts[0], parts[1].strip("[]"), parts[2].strip("[]")
        title = parts[3]
        gist = "|".join(parts[4:]).strip()
        if not (_TS_RE.match(start) and _TS_RE.match(end)):
            continue
        if not title:
            continue
        out.append(
            Section(index=len(out) + 1, title=title[:40], start=start, end=end, gist=gist)
        )

    out.sort(key=lambda s: _to_sec(s.start))
    for i, s in enumerate(out, 1):
        s.index = i
        s.start_sec = _to_sec(s.start)
    return out


def outline_problems(
    sections: list[Section], segs: list[tuple[str, str]], plan: Plan
) -> tuple[list[str], list[str]]:
    """骨架自检。返回 `(hard, soft)` 两组问题，都是本地校验、不花钱。

    **只有 hard 才触发机械兜底**——兜底会把模型识别出的语义边界换成等长切分，
    是明确的降级，不能因为一点小瑕疵就动用。所以判据只看"真的会坏事"的：

    hard：
      - 节数 < 2（等于又变回一趟写完，整条流程的意义就没了）
      - 有两节起点相同（`assign_segments` 会切出一个空节 → 白花一次调用 + 空小节）
      - 某一节超过 15,000 字（这一节又会变成长生成，退化照旧）
      - 节数 > 60（多半是模型在胡言乱语）

    soft（只提醒，不降级）：
      - 节数比建议值少/多；首节没从文稿开头起；节与节之间有缝；时间戳倒序（会被排序修正）。

    ⚠️ 教训：第一版把"节数偏少"当成了 hard，结果一份语义切得很好的 6 节骨架被丢掉，
    换成了机械切分——**那比不切还糟**。校验要盯着"会不会坏事"，不是盯着"符不符合建议"。
    """
    hard: list[str] = []
    soft: list[str] = []
    if not sections:
        return ["骨架解析后一节都没有"], []

    if len(sections) < 2:
        hard.append("骨架只切出 1 节（等于没分段）")
    if len(sections) > 60:
        hard.append(f"节数异常多（{len(sections)}）")

    starts = [s.start for s in sections]
    if len(set(starts)) != len(starts):
        hard.append("有两节用了同一个起始时间戳（会切出空节）")

    big = [s.index for s in sections if len(s.body) > 15_000]
    if big:
        hard.append(f"第 {big} 节超过 15,000 字，这一节仍会退化成超长生成")

    if len(sections) < max(2, plan.sections_lo - 2):
        soft.append(f"节数偏少（{len(sections)} < 建议 {plan.sections_lo}）")
    if len(sections) > plan.sections_hi + 4:
        soft.append(f"节数偏多（{len(sections)} > 建议 {plan.sections_hi}）")

    first_ts = segs[0][0] if segs else "00:00:00"
    if sections[0].start_sec > _to_sec(first_ts):
        soft.append(f"首节从 {sections[0].start} 开始，晚于文稿开头 {first_ts}")

    if [s.index for a, s in zip(sections, sections[1:]) if s.start_sec < a.start_sec]:
        soft.append("时间戳有倒序（已按时间重排）")

    gaps = [f"{a.end}→{b.start}" for a, b in zip(sections, sections[1:]) if a.end != b.start]
    if gaps:
        soft.append(f"节与节之间有缝/重叠：{gaps[:3]}")

    return hard, soft


def assign_segments(segs: list[tuple[str, str]], sections: list[Section]) -> None:
    """把原文各段分给各节（就地写入 `Section.segs`）。

    用"段的时间戳落在哪一节的起点之后"来判，**不用模型给的结束时间**——
    这样即使骨架的边界略有偏差，也不会有任何一段丢掉。
    """
    if not sections:
        return
    starts = [s.start_sec for s in sections]
    for s in sections:
        s.segs = []
    for ts, text in segs:
        i = bisect_right(starts, _to_sec(ts)) - 1
        sections[max(0, i)].segs.append((ts, text))


def mechanical_outline(
    segs: list[tuple[str, str]], duration: str, n: int
) -> list[Section]:
    """兜底：骨架没用时，按**段边界**机械切成 n 节。

    宁可要一份结构平庸但内容完整的总结，也不要因为骨架解析失败就整个任务失败。
    按段边界切（不是按字数）保证不会把一句话切开。
    """
    if not segs:
        return []
    n = max(1, min(n, len(segs)))
    size = max(1, len(segs) // n)
    out: list[Section] = []
    for i in range(0, len(segs), size):
        chunk = segs[i:i + size]
        out.append(
            Section(
                index=len(out) + 1,
                title=f"第 {len(out) + 1} 部分",
                start=chunk[0][0],
                end=chunk[-1][0],
                gist="（骨架生成失败，按原文段边界机械切分）",
                start_sec=_to_sec(chunk[0][0]),
                segs=list(chunk),
            )
        )
    if len(out) > 1:
        out[-1].end = duration or out[-1].end
    return out


def render_outline(sections: list[Section]) -> str:
    """骨架 → 给人看的速览（也是喂给每一节的"全局上下文"）。"""
    return "\n".join(
        f"- [{s.start}] **{s.title}** —— {s.gist}" if s.gist else f"- [{s.start}] **{s.title}**"
        for s in sections
    )


# ---------------------------------------------------------------- 断点续传


def source_hash(body: str) -> str:
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


class Parts:
    """分节落盘目录：`<总结路径>.parts/`。

    存在意义只有一个——**长任务失败不用从头再来**。多节 × 每次几十秒，
    跑到第 7 节断网就全丢，用户要重新花一遍钱和时间，那是不能接受的。
    """

    def __init__(self, root: str) -> None:
        self.root = root

    @property
    def manifest_path(self) -> str:
        return os.path.join(self.root, "manifest.json")

    def outline_path(self) -> str:
        return os.path.join(self.root, "outline.txt")

    def section_path(self, index: int) -> str:
        return os.path.join(self.root, f"sec{index:02d}.md")

    def load_manifest(self, key: dict) -> dict | None:
        """读 manifest 并校验是否与本次任务一致；不一致就清空重来。

        `finished=True` 的缓存一律不复用——那是"上一次已经成功跑完、但目录没删掉"的残留
        （Windows 上杀软/索引器偶尔会短暂占用文件，`rmtree` 会失败）。
        用户点「重新生成」时要的是重跑，不是拿旧结果糊弄他。
        """
        try:
            with open(self.manifest_path, encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            return None
        if m.get("finished"):
            self.reset()
            return None
        if any(m.get(k) != v for k, v in key.items()):
            self.reset()
            return None
        return m

    def save_manifest(self, key: dict, extra: dict | None = None) -> None:
        os.makedirs(self.root, exist_ok=True)
        data = dict(key)
        data.update(extra or {})
        try:
            with open(self.manifest_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass    # 缓存写不进去不该让整篇总结失败

    def read_outline(self) -> str:
        try:
            with open(self.outline_path(), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def write_outline(self, text: str) -> None:
        os.makedirs(self.root, exist_ok=True)
        with open(self.outline_path(), "w", encoding="utf-8") as f:
            f.write(text)

    def read_section(self, index: int) -> str:
        try:
            with open(self.section_path(index), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def write_section(self, index: int, text: str) -> None:
        os.makedirs(self.root, exist_ok=True)
        with open(self.section_path(index), "w", encoding="utf-8") as f:
            f.write(text)

    def drop_section(self, index: int) -> None:
        try:
            os.remove(self.section_path(index))
        except OSError:
            pass

    def reset(self) -> bool:
        """删掉分节缓存。返回是否删干净。

        Windows 上刚写完的文件可能被杀软/搜索索引器短暂占用，`rmtree` 会失败——
        而 `ignore_errors=True` 会把它变成**静默失败**（第一版就是这么留下一个
        "看起来完成了、其实缓存还在"的目录，下一次点「重新生成」会拿旧分节糊弄用户）。
        所以这里重试几次，并且把失败如实返回给调用方。
        """
        for attempt in range(3):
            if not os.path.isdir(self.root):
                return True
            try:
                shutil.rmtree(self.root)
                return True
            except OSError:
                if attempt == 2:
                    return False
                time.sleep(0.05 * (attempt + 1))
        return not os.path.isdir(self.root)


# ---------------------------------------------------------------- 主流程


@dataclass
class Job:
    cfg: dict
    body: str
    segs: list[tuple[str, str]]
    duration: str
    summary_path: str
    template: str = "A"
    with_timestamp: bool = True
    custom_prompt: str = ""
    opts: dict = field(default_factory=dict)


async def run(job: Job) -> AsyncIterator[tuple[str, dict]]:
    """跑完整条总结流程，产出 SSE 事件 `(event, data)`。

    单趟与多趟在这里统一，调用方（`main.py`）不用关心用的是哪条路。
    """
    cfg = job.cfg
    opts = job.opts or {}
    plan = assess(
        job.body, len(job.segs), job.duration,
        threshold=int(opts.get("long_doc_threshold", DEFAULT_THRESHOLD)),
    )
    yield "plan", plan.to_dict()

    if job.template.upper() != "A" or plan.strategy == "single":
        async for ev in _run_single(job, plan):
            yield ev
        return

    async for ev in _run_outline(job, plan):
        yield ev


async def _stream_llm(
    cfg: dict, system: str, user: str, *, thinking: bool | None
) -> AsyncIterator[tuple[str, str]]:
    """把 `stream_chat` 的事件转成 `(kind, text)`，`kind` ∈ reasoning/delta，最后给 finish。"""
    async for ev in summarizer.stream_chat(cfg, system, user, thinking=thinking):
        if ev["type"] == "reasoning":
            yield "reasoning", ev["text"]
        elif ev["type"] == "delta":
            yield "delta", ev["text"]
        elif ev["type"] == "done":
            yield "finish", ev.get("finish_reason") or ""


async def _run_single(job: Job, plan: Plan) -> AsyncIterator[tuple[str, dict]]:
    """短稿：一趟写完（提示词已修掉"宁可多写"和硬上限打架的问题）。"""
    yield "stage", {"stage": "single", "index": 1, "total": 1, "title": "整篇"}
    system, user = summarizer.build_prompt(
        job.template, job.body, len(job.segs), job.duration,
        custom_prompt=job.custom_prompt, with_timestamp=job.with_timestamp,
    )
    chunks: list[str] = []
    finish = ""
    think_chars = 0
    async for kind, text in _stream_llm(job.cfg, system, user, thinking=None):
        if kind == "reasoning":
            think_chars += len(text)
            yield "reasoning", {"text": text}
        elif kind == "delta":
            chunks.append(text)
            yield "delta", {"text": text}
        else:
            finish = text

    text = "".join(chunks)
    yield "done", {
        "text": text, "outline": "", "finish_reason": finish,
        "truncated": finish == "length", "reasoning_chars": think_chars,
        "plan": plan.to_dict(),
        "quality": quality.measure(
            job.body, text, [t for _ts, t in job.segs], [ts for ts, _t in job.segs]
        ),
    }


async def _run_outline(job: Job, plan: Plan) -> AsyncIterator[tuple[str, dict]]:
    """长稿：骨架 → 逐节展开 → 装配。"""
    opts = job.opts or {}
    parts = Parts(job.summary_path + ".parts")
    key = {
        "prompt_version": PROMPT_VERSION,
        "source_sha1": source_hash(job.body),
        "model": (job.cfg.get("llm") or {}).get("model") or "",
        "with_timestamp": bool(job.with_timestamp),
    }
    manifest = parts.load_manifest(key)
    resumed = bool(manifest)
    if resumed:
        yield "stage", {"stage": "resume", "index": 0, "total": 0,
                        "title": f"发现上次未完成的分节，接着跑（已完成 {len(manifest.get('done') or [])} 节）"}

    # ---- 第一趟：骨架 ----
    # 续跑时从 manifest 里**结构化**地恢复骨架，而不是去重新解析那份给人看的 markdown——
    # 机械兜底产出的 markdown 是解析不回来的（第一版就是这么错的：续跑时骨架"消失"，
    # 于是又重跑一遍骨架 + 全部小节，续传等于没有）。
    sections: list[Section] = []
    outline_text = ""
    if resumed and manifest.get("sections"):
        try:
            sections = [Section(**d) for d in manifest["sections"]]
        except TypeError:
            sections = []
    if sections:
        outline_text = parts.read_outline() or render_outline(sections)
        yield "stage", {"stage": "outline", "index": 0, "total": 0, "title": "复用上次的骨架"}

    if not sections:
        yield "stage", {"stage": "outline", "index": 0, "total": 0, "title": "生成骨架（分段）"}
        system, user = summarizer.build_outline_prompt(
            job.body, len(job.segs), job.duration, plan.sections_lo, plan.sections_hi
        )
        buf: list[str] = []
        async for kind, text in _stream_llm(job.cfg, system, user, thinking=True):
            if kind == "reasoning":
                yield "reasoning", {"text": text}
            elif kind == "delta":
                buf.append(text)
                yield "outline_delta", {"text": text}
        outline_text = "".join(buf)
        sections = parse_outline(outline_text)

        hard, soft = outline_problems(sections, job.segs, plan)
        if soft:
            yield "warn", {"message": "骨架提醒（不影响继续）：" + "；".join(soft)}
        if hard:
            # 骨架真的坏了 → 不重试（再花一次钱未必更好），直接机械兜底，保证内容完整
            yield "warn", {"message": "骨架不可用，已改为按原文段边界机械分节：" + "；".join(hard)}
            sections = mechanical_outline(
                job.segs, job.duration, max(plan.sections_lo, 3)
            )
            outline_text = render_outline(sections)
        parts.write_outline(outline_text)

    assign_segments(job.segs, sections)
    outline_md = render_outline(sections)
    total = len(sections)
    # 先把骨架写进 manifest：这样即使第一节就失败，续跑也不用重新生成骨架
    done: list[int] = list(manifest.get("done") or []) if resumed else []
    parts.save_manifest(key, {"done": done, "total": total,
                              "sections": [s.to_dict() for s in sections]})
    yield "outline", {
        "sections": [s.to_dict() for s in sections],
        "markdown": outline_md,
        "reused": bool(resumed),
    }

    # ---- 第二趟：逐节展开 ----
    ratios = (
        plan.target_lo / max(1, plan.chars),
        plan.target_hi / max(1, plan.chars),
        plan.target_cap / max(1, plan.chars),
    )
    use_full = plan.chars <= FULL_CONTEXT_MAX
    pieces: list[str] = []
    think_chars = 0

    for s in sections:
        cached = parts.read_section(s.index) if s.index in done else ""
        if cached:
            pieces.append(cached)
            # 复用的一节也要发给前端，否则用户看到的文档是缺节的
            yield "delta", {"text": cached}
            yield "stage", {"stage": "section", "index": s.index, "total": total,
                            "title": s.title, "reused": True}
            continue

        yield "stage", {"stage": "section", "index": s.index, "total": total,
                        "title": s.title, "reused": False}
        sec_chars = len(s.body)
        system, user = summarizer.build_section_prompt(
            index=s.index, total=total, title=s.title, outline_md=outline_md,
            section_body=s.body, full_body=job.body if use_full else None,
            with_timestamp=job.with_timestamp,
            lo=max(80, int(sec_chars * ratios[0])),
            hi=max(150, int(sec_chars * ratios[1])),
            cap=max(200, int(sec_chars * ratios[2])),
        )
        buf: list[str] = []
        async for kind, text in _stream_llm(job.cfg, system, user, thinking=False):
            if kind == "reasoning":
                think_chars += len(text)
                yield "reasoning", {"text": text}
            elif kind == "delta":
                buf.append(text)
                yield "delta", {"text": text}
            else:
                pass
        piece = "".join(buf).strip()
        if piece:
            parts.write_section(s.index, piece)
            if s.index not in done:
                done.append(s.index)
            parts.save_manifest(key, {"done": done, "total": total,
                                      "sections": [x.to_dict() for x in sections]})
            pieces.append(piece)

    assembled = "\n\n".join(pieces).strip() + "\n"
    # 成功了就把分节缓存清掉，别在 output/ 里留一堆临时文件；
    # 万一删不掉（文件被占用），就打一个 finished 标记，防止下次误当成"没跑完"而复用。
    if not parts.reset():
        parts.save_manifest(key, {"finished": True})

    yield "done", {
        "text": assembled,
        "outline": outline_md,
        "finish_reason": "stop",
        "truncated": False,
        "reasoning_chars": think_chars,
        "plan": plan.to_dict(),
        "sections": total,
        "quality": quality.measure(
            job.body, assembled, [t for _ts, t in job.segs], [ts for ts, _t in job.segs]
        ),
    }


# ---------------------------------------------------------------- 自检入口


def _smoke() -> None:  # pragma: no cover - 手动跑
    """python -m app.longdoc <文稿.txt>：只打印长度评估，不调模型。"""
    import sys

    from .summarizer import read_and_merge

    _segs, body, duration = read_and_merge(sys.argv[1])
    p = assess(body, len(_segs), duration)
    print(json.dumps(p.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover
    _smoke()
