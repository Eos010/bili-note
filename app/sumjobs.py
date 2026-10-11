"""后台总结作业（v2.1）：**总结是一个服务端作业，HTTP 连接只是它的一个观看窗口**。

## 为什么要有这一层

在 v2.0 之前，`POST /api/tasks/{id}/summarize` 返回的 SSE 流**就是**总结本身：
生成循环由客户端连接驱动。于是有一串连锁后果——

- 关掉弹窗 / 切走页面 → 连接断 → Starlette 取消生成器 → **总结当场停掉**；
- 想同时总结几个视频 → 前端只有一个 `sumStreaming` 全局标志，第二个被挡住；
- 首页任务卡上**看不到任何总结进度**，只能盯着那个弹窗。

根因是把"**作业**"和"**观看作业的那根管子**"设计成了同一个东西。

## 这里的做法

```
POST /summarize ──► SummaryManager.start() ──► asyncio.create_task(_execute)   ← 作业（独立生命周期）
                            │
        SSE 端点 ──► subscribe(job) ──► 重放已有事件 + 跟随后续事件          ← 观看窗口（可随时断开/重连）
```

三条硬规矩：

1. **作业不是请求的子任务**。`asyncio.create_task` 出来的 task 挂在事件循环上，
   归属跟请求那棵任务树无关——客户端断开时 Starlette 取消的是**它的**生成器，
   动不到作业。
2. **重连要能接上**。作业保留一份事件流水（journal），新订阅者先把已有的补发一遍，
   再挂到实时队列上；两者在同一段**没有 await** 的代码里完成，所以不会漏事件、也不会重事件。
   看门狗看到的是"总结正在打字"，而不是一块空白。
3. **同一任务同时只有一个作业**。重复点「总结」= 挂到现有作业上看（幂等），
   要重跑得显式传 `restart`。

副产品：`GET /api/tasks` 的每个任务都带一份总结状态，
所以首页每张卡片能各显示各的进度——这正是"同时总结多个视频"需要的。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from . import config as app_config
from . import longdoc, summarizer, summary_store, tasks as tasks_mod

# 同时跑几个总结。默认 3：既够"同时开几个视频"，又不会把并发连接和 token 速率打爆。
DEFAULT_MAX_CONCURRENT = 3

# 事件流水上限。超过就退化成"快照重放"（见 `SummaryJob.replay`）——
# 这里的目的只是**兜住内存**，正常一份总结的流水只有几十 KB。
JOURNAL_MAX_EVENTS = 8_000
JOURNAL_MAX_CHARS = 1_000_000

# 思考过程重放上限：推理模型单次能"想"出几万字，全留着看也看不完，只留开头一段。
REASONING_REPLAY_MAX = 8_000

# 终态
TERMINAL = ("done", "error", "cancelled")
ACTIVE = ("queued", "running")

_RESUME_HINT = "已完成的节都留着，下次生成会接着跑，不会从头再来。"


def _max_concurrent() -> int:
    opts = app_config.load().get("summarize") or {}
    try:
        return max(1, int(opts.get("max_concurrent", DEFAULT_MAX_CONCURRENT)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CONCURRENT


class _Gate:
    """并发闸门。

    不用预建的 `asyncio.Semaphore`：那是构造时定死额度的，改设置得重启才生效。
    这里 `limit` 每次在 `acquire` 时现读，所以用户把「同时总结数」从 3 改成 1，
    下一个作业立刻按新额度排队；已经在跑的**不打断**（改配置不该腰斩正在跑的活）。
    """

    def __init__(self) -> None:
        self._active = 0
        self._waiters: list[asyncio.Future] = []

    @property
    def active(self) -> int:
        return self._active

    async def acquire(self, limit: int) -> None:
        while self._active >= limit:
            fut = asyncio.get_running_loop().create_future()
            self._waiters.append(fut)
            try:
                await fut
            finally:
                if fut in self._waiters:
                    self._waiters.remove(fut)
        self._active += 1

    def release(self) -> None:
        if self._active > 0:
            self._active -= 1
        # 全部唤醒、各自重判（等待者只有个位数，省得算"该叫醒几个"算错）
        for fut in list(self._waiters):
            if not fut.done():
                fut.set_result(None)


@dataclass
class SummaryJob:
    task_id: str
    task: object
    template: str
    custom_prompt: str
    with_timestamp: bool
    cfg: dict
    opts: dict

    status: str = "queued"          # queued / running / done / error / cancelled
    stage: str = ""                 # preparing / outline / section / single / resume
    label: str = "排队中…"
    progress: float = 0.0
    index: int = 0
    total: int = 0
    section_title: str = ""
    error: str = ""
    text: str = ""                  # 已生成的正文（单趟流式累积 / 长稿各节拼装）
    outline: str = ""               # 骨架速览 markdown
    outline_sections: list[dict] = field(default_factory=list)
    sections: int = 0
    model: str = ""
    strategy: str = ""
    target: int = 0                 # 单趟模式的篇幅目标（用来估算进度）
    reasoning_chars: int = 0
    reasoning_replay: str = ""      # 只留开头一段给重连的人看
    meta: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    # 事件流水（非终态事件）+ 终态事件分开存，重放时拼起来
    journal: list[tuple[str, dict]] = field(default_factory=list)
    journal_chars: int = 0
    collapsed: bool = False
    done_payload: Optional[tuple[str, dict]] = None

    subscribers: set[asyncio.Queue] = field(default_factory=set)
    runner: Optional[asyncio.Task] = None

    # ---- 状态 ----

    @property
    def terminal(self) -> bool:
        return self.done_payload is not None

    @property
    def active(self) -> bool:
        return self.status in ACTIVE and not self.terminal

    def to_state(self) -> dict:
        started = self.started_at or self.created_at
        elapsed = (self.finished_at or time.time()) - started if self.started_at else 0.0
        return {
            "status": self.status,
            "stage": self.stage,
            "label": self.label,
            "progress": round(self.progress, 1),
            "index": self.index,
            "total": self.total,
            "title": self.section_title,
            "error": self.error,
            "model": self.model,
            "template": self.template,
            "strategy": self.strategy,
            "sections": self.sections,
            "chars": len(self.text),
            "reasoning_chars": self.reasoning_chars,
            "elapsed": round(elapsed, 1) if self.started_at else 0.0,
        }

    # ---- 事件 ----

    def journal_event(self, event: str, data: dict) -> None:
        if self.collapsed:
            return
        cost = len(event) + len(str(data.get("text", ""))) + 64
        if len(self.journal) >= JOURNAL_MAX_EVENTS or self.journal_chars + cost > JOURNAL_MAX_CHARS:
            # 兜住内存：丢掉流水，改成"快照重放"。
            # 正文不会丢——`self.text` 一直单独在累积。
            self.collapsed = True
            self.journal.clear()
            self.journal_chars = 0
            return
        self.journal.append((event, data))
        self.journal_chars += cost

    def replay(self) -> list[tuple[str, dict]]:
        """给新订阅者的"回放"：先把它没赶上的一切补上。

        没被截断时原样重发流水——**保住了流式的颗粒度**，重连的人看到的是
        文字在继续打字，而不是"啪"地蹦出半篇。
        """
        if not self.collapsed:
            out = list(self.journal)
        else:
            # 流水被截断（>8000 事件 / >1MB，正常跑不到）→ 退化成快照。
            # 正文不会丢：`self.text` 一直单独在累积。
            out = [("start", {"model": self.model, "template": self.template})]
            if self.meta:
                out.append(("meta", dict(self.meta)))
            if self.plan:
                out.append(("plan", dict(self.plan)))
            if self.outline:
                out.append(("outline", {"sections": list(self.outline_sections),
                                        "markdown": self.outline, "reused": False}))
            if self.reasoning_replay:
                out.append(("reasoning", {"text": self.reasoning_replay}))
            for w in self.warnings:
                out.append(("warn", {"message": w}))
            if self.text:
                out.append(("delta", {"text": self.text}))
            if self.stage:
                out.append(("stage", {
                    "stage": self.stage, "index": self.index,
                    "total": self.total, "title": self.section_title,
                }))
        if self.done_payload:
            out.append(self.done_payload)
        return out


class SummaryManager:
    """总结作业表。整个进程一个实例（`main.summaries`）。"""

    def __init__(self) -> None:
        self.jobs: dict[str, SummaryJob] = {}
        self._gate = _Gate()

    # ---- 查询 ----

    def get(self, task_id: str) -> Optional[SummaryJob]:
        return self.jobs.get(task_id)

    def active(self, task_id: str) -> Optional[SummaryJob]:
        """还在排队/跑着的作业（终态的不算——所以「重试」会开新作业而不是挂到旧的上）。"""
        job = self.jobs.get(task_id)
        return job if (job is not None and job.active) else None

    def state(self, task_id: str) -> dict:
        """给任务卡/轮询用的总结状态。没有作业 = idle（可能是从没跑过，也可能上次跑完服务重启了）。"""
        job = self.jobs.get(task_id)
        if job is None:
            return {
                "status": "idle", "stage": "", "label": "", "progress": 0.0,
                "index": 0, "total": 0, "title": "", "error": "", "model": "",
                "template": "", "strategy": "", "sections": 0, "chars": 0,
                "reasoning_chars": 0, "elapsed": 0.0,
            }
        return job.to_state()

    # ---- 生命周期 ----

    def start(self, t, *, template: str, with_timestamp: bool, custom_prompt: str) -> SummaryJob:
        """建作业并**立刻**挂到事件循环上——不等任何客户端连接。

        调用方负责保证同一任务没有别的活跃作业（`active()` 为 None）。
        """
        cfg = app_config.load()
        opts = cfg.get("summarize") or {}
        job = SummaryJob(
            task_id=t.id, task=t,
            template=template, with_timestamp=with_timestamp,
            custom_prompt=custom_prompt, cfg=cfg, opts=dict(opts),
            model=(cfg.get("llm") or {}).get("model") or "",
        )
        self.jobs[t.id] = job
        job.runner = asyncio.create_task(self._execute(job))
        return job

    async def stop(self, task_id: str) -> bool:
        """停掉正在跑/排队的作业（分节缓存保留，下次可以接着跑）。"""
        job = self.active(task_id)
        if job is None:
            return False
        task = job.runner
        if task is not None and not task.done():
            task.cancel()
            # 用 wait 而不是 await task：作业被取消是**预期结果**，
            # 不该以异常的形式冒到调用方（更不该和调用方自己的取消混在一起）。
            await asyncio.wait([task])
        return True

    async def forget(self, task_id: str) -> None:
        """任务被移除时连带忘掉作业（先停，免得它继续往已删的目录里写）。"""
        await self.stop(task_id)
        self.jobs.pop(task_id, None)

    async def shutdown(self) -> None:
        for job in list(self.jobs.values()):
            if job.active and job.runner is not None and not job.runner.done():
                job.runner.cancel()
        pending = [j.runner for j in self.jobs.values() if j.runner is not None and not j.runner.done()]
        if pending:
            await asyncio.wait(pending)

    # ---- 订阅 ----

    async def subscribe(self, job: SummaryJob) -> AsyncIterator[tuple[str, dict]]:
        """SSE 视图：先回放，再跟随。客户端随时可以走，作业不受影响。"""
        q: asyncio.Queue = asyncio.Queue()
        # ⚠️ 注册 + 快照必须连续、中间不能有 await，否则会漏事件或重事件。
        job.subscribers.add(q)
        terminal = job.terminal
        replay = job.replay()

        try:
            for ev in replay:
                yield ev
            if terminal:
                return
            while True:
                item = await q.get()
                if item is None:
                    return
                yield item
        finally:
            job.subscribers.discard(q)

    # ---- 内部 ----

    def _emit(self, job: SummaryJob, event: str, data: dict) -> None:
        self._apply(job, event, data)
        job.journal_event(event, data)
        for q in list(job.subscribers):
            q.put_nowait((event, data))

    def _terminate(self, job: SummaryJob, event: str, data: dict) -> None:
        job.done_payload = (event, data)
        if not job.finished_at:
            job.finished_at = time.time()
        for q in list(job.subscribers):
            q.put_nowait((event, data))
            q.put_nowait(None)      # 哨兵：告诉订阅者"没有后续了"
        job.subscribers.clear()

    def _apply(self, job: SummaryJob, event: str, data: dict) -> None:
        """把事件折进作业状态——任务卡上的进度就是从这儿来的。"""
        if event == "meta":
            job.meta = dict(data)
            job.progress = max(job.progress, 2.0)
        elif event == "plan":
            job.plan = dict(data)
            job.strategy = data.get("strategy") or ""
            job.target = int(data.get("target_hi") or 0)
            job.label = data.get("note") or job.label
            job.progress = max(job.progress, 3.0)
        elif event == "stage":
            stage = data.get("stage") or ""
            job.stage = stage
            total = int(data.get("total") or 0)
            if total:
                job.total = total
            job.index = int(data.get("index") or 0)
            title = data.get("title") or ""
            if stage == "outline":
                job.label = title or "生成骨架（分段）…"
                job.progress = max(job.progress, 8.0)
            elif stage == "resume":
                job.label = title or "接着上次继续…"
            elif stage == "section":
                job.section_title = title
                job.label = (f"第 {job.index}/{job.total} 节 · {title}"
                             + ("（复用上次的）" if data.get("reused") else ""))
                if job.total:
                    job.progress = max(
                        job.progress, 12.0 + 84.0 * max(0, job.index - 1) / job.total
                    )
            elif stage == "single":
                job.section_title = ""
                job.label = "整篇生成中…"
                job.progress = max(job.progress, 4.0)
        elif event == "outline":
            job.outline = data.get("markdown") or ""
            job.outline_sections = list(data.get("sections") or [])
            job.sections = len(job.outline_sections)
            if job.sections:
                job.total = job.sections
            job.progress = max(job.progress, 12.0)
        elif event == "delta":
            job.text += data.get("text") or ""
            if job.stage == "single" and job.target:
                # 单趟模式没有"第几节"可分，就用**已出字数 / 目标字数**估——
                # 比一个转圈的菊花有用得多。
                job.progress = max(
                    job.progress, min(96.0, 4.0 + 92.0 * len(job.text) / job.target)
                )
        elif event == "reasoning":
            text = data.get("text") or ""
            job.reasoning_chars += len(text)
            room = REASONING_REPLAY_MAX - len(job.reasoning_replay)
            if room > 0:
                job.reasoning_replay += text[:room]
        elif event == "warn":
            msg = data.get("message") or ""
            if msg:
                job.warnings.append(msg)

    async def _execute(self, job: SummaryJob) -> None:
        t = job.task
        acquired = False
        try:
            limit = _max_concurrent()
            if self._gate.active >= limit:
                job.label = "排队中（等前面的总结跑完）…"
            await self._gate.acquire(limit)
            acquired = True

            job.status = "running"
            job.started_at = time.time()
            job.label = "合并文稿…"
            self._emit(job, "start", {"model": job.model, "template": job.template})

            opts = job.opts
            segs, doc, duration = await asyncio.to_thread(
                summarizer.read_and_merge,
                t.txt_path,
                seg_chars=int(opts.get("seg_chars", 400)),
                seg_seconds=int(opts.get("seg_seconds", 180)),
                gap_seconds=int(opts.get("gap_seconds", 3)),
            )
            self._emit(job, "meta", {
                "segments": len(segs), "duration": duration, "chars": len(doc),
            })

            work = longdoc.Job(
                cfg=job.cfg, body=doc, segs=segs, duration=duration,
                summary_path=summary_store.summary_path(t), template=job.template,
                with_timestamp=job.with_timestamp, custom_prompt=job.custom_prompt,
                opts=opts,
            )

            final: dict = {}
            async for event, data in longdoc.run(work):
                if event == "done":
                    final = data          # 落盘要等整篇拼完，先攒着
                    continue
                self._emit(job, event, data)

            text = (final.get("text") or "").strip()
            if not text:
                raise summarizer.SummarizeError("模型没有返回任何内容，请重试。")

            plan = final.get("plan") or {}
            path = await asyncio.to_thread(
                summary_store.write, t, text, job.template, job.model,
                len(segs), duration, final.get("outline") or "", plan,
            )

            job.text = text
            job.outline = final.get("outline") or job.outline
            job.plan = plan or job.plan
            job.quality = final.get("quality") or {}
            job.reasoning_chars = final.get("reasoning_chars") or job.reasoning_chars
            job.status = "done"
            job.progress = 100.0
            job.label = "完成"
            self._terminate(job, "done", {
                "finish_reason": final.get("finish_reason") or "stop",
                "truncated": bool(final.get("truncated")),
                "path": path,
                "has_media": bool(tasks_mod.media_path(t)),
                "reasoning_chars": job.reasoning_chars,
                "outline": job.outline,
                "quality": job.quality,
                "plan": job.plan,
            })
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.label = "已停止"
            job.error = ""
            job.finished_at = time.time()
            self._terminate(job, "cancelled", {"message": "已停止。" + _RESUME_HINT})
            raise
        except summarizer.SummarizeError as e:
            job.status = "error"
            job.label = "失败"
            job.error = str(e)
            self._terminate(job, "error", {"message": str(e)})
        except Exception as e:  # noqa: BLE001 —— 作业级兜底，错误要送到界面/任务卡
            job.status = "error"
            job.label = "失败"
            job.error = f"{type(e).__name__}: {e}"
            self._terminate(job, "error", {"message": job.error})
        finally:
            if acquired:
                self._gate.release()


# 全局单例：整个进程一张作业表
manager = SummaryManager()
