"""后台总结作业自检（v2.1）：**不联网、不花钱**（本地起一个假 LLM）。

跑法：`.venv\\Scripts\\python.exe selftest_bgsum.py`

这套测试存在的唯一理由：v2.0 以前"总结"和"那根 HTTP 连接"是同一个东西，
于是关掉弹窗就把总结掐了、也没法同时总结多个视频。要保证这类退化**不会再悄悄回来**，
就必须把下面几条钉成断言：

  1. **没有订阅者，作业照样跑完**（不是"有人看着才动"）
  2. **订阅者中途断开，作业不受影响**（关弹窗 = 断观看窗口，不是停作业）
  3. **断线重连能拿回完整回放**（连"已经发生过的"一起补上，一个字不差）
  4. **重复点「总结」不会开第二个作业**（幂等；要重跑必须显式 restart）
  5. **多任务并发 + 上限闸门**（超出的排队，且最终都能跑完，不能死锁）
  6. **停止**保留已完成的节、**失败**落到任务状态上、**跑着的时候不许删任务**
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
TMP_DIR = os.path.join(BASE, "output", ".tmp", "bgsum_selftest")
MOCK_PORT = 8097
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}/v1"

# 被测应用跑在**真端口**上，而不是 httpx 的 ASGITransport。
# 原因（2026-09-20 踩的）：ASGITransport 会把整个响应缓冲完再交出来，
# 于是"读到一半就断开"这类测试根本跑不到一半——作业早就结束了，
# 断言看着像通过、其实什么都没测。断连语义必须走真 socket 才算数。
APP_PORT = 8096
APP_BASE = f"http://127.0.0.1:{APP_PORT}"

ok_count = 0
fail_count = 0

# 假 LLM 的可控状态
mock_state: dict = {
    "delay": 0.02,      # 每个 chunk 之间睡多久（用来制造"跑到一半"的时刻）
    "fail": False,
    "calls": 0,
    "outline_calls": 0,
    "section_calls": 0,
    "single_calls": 0,
}

CHUNK = 50


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  [OK]   {name}")
    else:
        fail_count += 1
        print(f"  [FAIL] {name} {extra}")


# ---------------------------------------------------------------- 假 LLM

mock = FastAPI()

_OUTLINE_SPLIT = 5


def _timestamps(text: str) -> list[str]:
    return re.findall(r"\[(\d{2}:\d{2}:\d{2})\]", text)


@mock.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    msgs = body.get("messages") or []
    user = msgs[-1]["content"] if msgs else ""
    mock_state["calls"] += 1
    if mock_state["fail"]:
        return JSONResponse({"error": {"message": "假 LLM 故意失败"}}, status_code=500)

    # ---- 骨架 ----
    if "严格每行一节" in user:
        mock_state["outline_calls"] += 1
        src = user.split("【文稿正文】")[-1]
        ts = _timestamps(src)
        n = min(_OUTLINE_SPLIT, max(1, len(ts)))
        idx = [round(i * (len(ts) - 1) / n) for i in range(n)] + [len(ts) - 1]
        lines = [
            f"S{k + 1:02d} | {ts[idx[k]]} | {ts[idx[k + 1]]} | 小节{k + 1} | 第{k + 1}节主旨"
            for k in range(n)
        ]
        return await _stream("\n".join(lines) + "\n")

    # ---- 某一节 ----
    if "这是一份视频文字稿的**第" in user:
        mock_state["section_calls"] += 1
        m = re.search(r"### (.+?)\n", user)
        title = m.group(1).strip() if m else "小节"
        src = user.split("【本节原文】")[-1].split("【任务】")[0]
        ts = _timestamps(src)
        start = ts[0] if ts else "00:00:00"
        body_text = re.sub(r"\[\d{2}:\d{2}:\d{2}\]\s*", "", src).strip()[:600]
        return await _stream(f"### {title}\n[{start}]\n{body_text}\n")

    # ---- 单趟 ----
    mock_state["single_calls"] += 1
    m = re.search(r"压到 (\d+)~(\d+) 字", user)
    want = int(m.group(1)) if m else 1200
    filler = ("总结正文内容甲乙丙丁戊己庚辛壬癸" * 400)[: max(200, want)]
    return await _stream(f"### 全篇\n[00:00:00]\n{filler}\n")


async def _stream(text: str) -> StreamingResponse:
    async def gen():
        for i in range(0, len(text), CHUNK):
            if mock_state["delay"]:
                await asyncio.sleep(mock_state["delay"])
            yield "data: " + json.dumps(
                {"choices": [{"delta": {"content": text[i:i + CHUNK]}, "finish_reason": None}]},
                ensure_ascii=False,
            ) + "\n\n"
        yield "data: " + json.dumps(
            {"choices": [{"delta": {}, "finish_reason": "stop"}]}, ensure_ascii=False
        ) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@mock.get("/v1/models")
async def models():
    return {"data": [{"id": "mock-chat"}]}


def start_mock() -> uvicorn.Server:
    cfg = uvicorn.Config(mock, host="127.0.0.1", port=MOCK_PORT, log_level="error")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{MOCK_PORT}/v1/models", timeout=1)
            return server
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError("假 LLM 没起来")


async def start_app() -> tuple[asyncio.Task, uvicorn.Server]:
    """把被测应用起在**当前事件循环**里。

    必须在同一个循环里：测试要对 `summaries` 直接下断言，
    如果应用跑在别的线程/循环，"作业"就会挂在另一个循环上，测的根本不是同一个东西。
    """
    from app.main import app

    cfg = uvicorn.Config(app, host="127.0.0.1", port=APP_PORT, log_level="error")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        try:
            async with httpx.AsyncClient() as c:
                await c.get(f"{APP_BASE}/api/version", timeout=1)
            return task, server
        except httpx.HTTPError:
            await asyncio.sleep(0.05)
    raise RuntimeError("被测服务没起来")


# ---------------------------------------------------------------- 工具


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        event, data = None, None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if event and data:
            try:
                events.append((event, json.loads(data)))
            except json.JSONDecodeError:
                pass
    return events


def body_of(events: list[tuple[str, dict]]) -> str:
    return "".join(d.get("text", "") for e, d in events if e == "delta")


def norm(s: str) -> str:
    """去掉所有空白再比。

    长稿是**逐节**生成再拼起来的，落盘时用 `\\n\\n` 连接；
    而流里的 delta 是各节**原样的输出块**首尾相接。两者只有空白差异——
    比"字"才是真的在比内容，比空白差异没有意义。
    """
    return re.sub(r"\s+", "", s or "")


async def wait_until(pred, timeout: float = 20.0, interval: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()


def make_transcript(path: str, n_lines: int) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = []
    for i in range(n_lines):
        sec = i * 3
        ts = f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
        lines.append(f"[{ts}] 第{i:03d}段测试文本甲乙丙丁戊己庚辛壬癸子丑寅卯")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def cleanup_paths(txt: str) -> None:
    base = os.path.splitext(txt)[0]
    for p in (base + ".summary.md", base + ".summary.outline.md"):
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(base + ".summary.md.parts", ignore_errors=True)


# ---------------------------------------------------------------- 主流程


async def run_e2e() -> None:
    from app.main import manager, summaries
    from app.tasks import Task

    app_task, app_server = await start_app()   # 真端口 + 真 lifespan（manager.start 在里面）

    short_txt = make_transcript(os.path.join(TMP_DIR, "短文稿.txt"), 60)     # 走单趟
    long_txt = make_transcript(os.path.join(TMP_DIR, "长文稿.txt"), 220)    # 走骨架→逐节
    for p in (short_txt, long_txt):
        cleanup_paths(p)

    def add_task(tid: str, txt: str, title: str) -> Task:
        t = Task(id=tid, url=txt, download=False, transcribe=True, kind="file")
        t.status = "done"
        t.title = title
        t.txt_path = txt
        t.output_dir = os.path.dirname(txt)
        manager.tasks[tid] = t
        return t

    add_task("bg0000000001", short_txt, "后台短稿")
    add_task("bg0000000002", long_txt, "后台长稿")

    async def new_task(tid: str, title: str) -> Task:
        txt = make_transcript(os.path.join(TMP_DIR, f"{tid}.txt"), 60)
        cleanup_paths(txt)
        return add_task(tid, txt, title)

    async with httpx.AsyncClient(base_url=APP_BASE, timeout=120) as client:
        # ============================================================
        print("\n== 1. 没有任何订阅者，作业照样跑完 ==")
        # ============================================================
        mock_state.update({"delay": 0.01, "fail": False})
        t1 = manager.get("bg0000000001")
        job = summaries.start(t1, template="A", with_timestamp=True, custom_prompt="")
        check("作业立刻处于活跃状态（不等任何连接）", job.active, f" {job.status}")
        check("作业已挂到事件循环上", job.runner is not None and not job.runner.done())

        done_ok = await wait_until(lambda: job.status in ("done", "error"), timeout=30)
        check("无人观看也跑到了终态", done_ok, f" {job.status} {job.error}")
        check("终态是 done", job.status == "done", f" {job.status} {job.error}")
        check("总结文件已落盘（没人看着也写了）",
              os.path.isfile(os.path.splitext(short_txt)[0] + ".summary.md"))
        check("作业状态带完整正文长度", len(job.text) > 100, f" {len(job.text)}")

        # ============================================================
        print("\n== 2. 订阅者中途断开 → 作业不受影响 ==")
        # ============================================================
        t2 = manager.get("bg0000000002")
        mock_state.update({"delay": 0.02, "fail": False})
        job2 = summaries.start(t2, template="A", with_timestamp=True, custom_prompt="")

        seen = 0
        gen = summaries.subscribe(job2)
        async for _ev, _d in gen:
            seen += 1
            if seen >= 3:
                break
        await gen.aclose()          # ← 等价于"客户端把连接掐了"
        check("断开前确实收到过事件", seen >= 3, f" {seen}")
        check("断开后作业仍在跑", job2.active, f" {job2.status}")
        check("断开后订阅者已摘掉（不留悬挂队列）", len(job2.subscribers) == 0,
              f" {len(job2.subscribers)}")

        still = await wait_until(lambda: job2.status in ("done", "error"), timeout=40)
        check("断开后作业自己跑完了", still and job2.status == "done",
              f" {job2.status} {job2.error}")

        # ---- 2b. 走真实 HTTP：连上 → 读一点 → 关掉连接 ----
        # 这是"用户点了弹窗右上角的 ×"的真实形态，必须走真 socket 才测得到
        # （ASGITransport 会把响应缓冲完，等于什么都没测）。
        print("\n== 2b. 真实 HTTP 断开（= 用户点了弹窗的关闭） ==")
        t2b = await new_task("bg0000000003", "断开测试")
        mock_state.update({"delay": 0.04, "fail": False})
        got = 0
        async with client.stream("POST", f"/api/tasks/{t2b.id}/summarize",
                                 json={"template": "A"}) as r:
            async for chunk in r.aiter_text():
                if chunk:
                    got += 1
                    if got >= 2:
                        break
        # 出了 with = 连接关了
        job2b = summaries.get(t2b.id)
        check("HTTP 断开前确实收到了数据（说明真的在流式）", got >= 2, f" {got}")
        check("HTTP 断开后作业仍在跑（这就是「关掉页面也继续」）",
              job2b is not None and job2b.active, f" {job2b.status if job2b else 'None'}")
        # 断开是异步的：TCP 关掉之后服务端要过一小会儿才收到 http.disconnect。
        # 所以这里**等**它收干净——收不干净就是真的漏了一个悬挂队列（每关一个标签页漏一个）。
        cleaned = await wait_until(lambda: len(job2b.subscribers) == 0, timeout=5)
        check("HTTP 断开后服务端订阅者会被清掉", cleaned,
              f" 还剩 {len(job2b.subscribers)} 个")
        finished = await wait_until(lambda: not job2b.active, timeout=30)
        check("HTTP 断开后作业自己跑完并存了盘",
              finished and job2b.status == "done"
              and os.path.isfile(os.path.splitext(t2b.url)[0] + ".summary.md"),
              f" {job2b.status} {job2b.error}")

        # ============================================================
        print("\n== 3. 断线重连：回放要一字不差 ==")
        # ============================================================
        replay = [ev for ev in job2.replay()]
        kinds = [e for e, _ in replay]
        check("回放里有 start", "start" in kinds)
        check("回放里有骨架", "outline" in kinds)
        check("回放里有终态 done", "done" in kinds)
        check("回放里的正文与作业正文一字不差（忽略空白）",
              norm(body_of(replay)) == norm(job2.text),
              f" {len(norm(body_of(replay)))} vs {len(norm(job2.text))}")
        check("回放里有全部小节标题",
              body_of(replay).count("### ") == job2.sections,
              f" {body_of(replay).count('### ')} vs {job2.sections} 节")

        # 落盘文件里也要能读到同一份正文（换个进程来读也一样）
        saved = open(os.path.splitext(long_txt)[0] + ".summary.md", encoding="utf-8").read()
        check("文件里的正文 = 作业内存里的正文", job2.text.strip() in saved)
        check("文件正文与回放一字不差", norm(job2.text) in norm(saved))

        # 迟到的订阅者：连上就该立刻拿到全过程 + 终态，然后结束
        late = [ev async for ev in summaries.subscribe(job2)]
        check("终态之后再订阅也能立刻拿到完整回放",
              norm(body_of(late)) == norm(job2.text) and any(e == "done" for e, _ in late),
              f" {len(late)} 个事件")
        check("终态之后订阅会立即结束（不挂着等）", len(job2.subscribers) == 0)

        # ============================================================
        print("\n== 4. 幂等：重复点「总结」不会开第二个作业 ==")
        # ============================================================
        t4 = await new_task("bg0000000004", "幂等测试")
        mock_state.update({"delay": 0.06, "fail": False, "calls": 0})
        async with client.stream("POST", f"/api/tasks/{t4.id}/summarize",
                                 json={"template": "A"}) as r:
            first = summaries.get(t4.id)
            check("POST 后有了作业", first is not None)
            await r.aiter_text().__anext__()      # 只读一点点，作业还在跑

        async with client.stream("POST", f"/api/tasks/{t4.id}/summarize",
                                 json={"template": "A"}) as r:
            second = summaries.get(t4.id)
            check("再次 POST 挂的是同一个作业（没有新开）", second is first,
                  f" {first is not None} {second is first}")
            raw = "".join([c async for c in r.aiter_text()])
        ev = parse_sse(raw)
        check("第二个观看窗口也能看到完整过程", any(e == "done" for e, _ in ev),
              f" {[d for e, d in ev if e == 'error']}")
        check("第二个窗口看到的是重放 + 实时（正文非空）", len(body_of(ev)) > 100)
        await wait_until(lambda: not summaries.get(t4.id).active, timeout=30)

        # ============================================================
        print("\n== 5. restart=true 才换新作业 ==")
        # ============================================================
        t5 = await new_task("bg0000000005", "重跑测试")
        mock_state.update({"delay": 0.06, "fail": False})
        async with client.stream("POST", f"/api/tasks/{t5.id}/summarize",
                                 json={"template": "A"}) as r:
            j_old = summaries.get(t5.id)
            await r.aiter_text().__anext__()
        async with client.stream("POST", f"/api/tasks/{t5.id}/summarize",
                                 json={"template": "A", "restart": True}) as r:
            j_new = summaries.get(t5.id)
            check("restart=true 换成了新作业", j_new is not j_old)
            check("旧的作业被标成已取消", j_old.status == "cancelled", f" {j_old.status}")
            raw = "".join([c async for c in r.aiter_text()])
        check("新作业正常跑完", any(e == "done" for e, _ in parse_sse(raw)))
        await wait_until(lambda: not summaries.get(t5.id).active, timeout=30)

        # ============================================================
        print("\n== 6. 多任务并发 + 上限闸门 ==")
        # ============================================================
        print("   （配置 max_concurrent = 2，起 3 个）")
        mock_state.update({"delay": 0.03, "fail": False})
        tids = ["bg0000000006", "bg0000000007", "bg0000000008"]
        tasks = []
        for tid in tids:
            tasks.append(await new_task(tid, f"并发测试{tid[-1]}"))
        jobs = [
            summaries.start(t, template="A", with_timestamp=True, custom_prompt="")
            for t in tasks
        ]
        await asyncio.sleep(0.25)
        statuses = [j.status for j in jobs]
        running = statuses.count("running")
        queued = statuses.count("queued")
        check("同时在跑的没超过上限 2", running <= 2, f" 状态 {statuses}")
        check("超出的那个在排队（而不是被丢掉）", queued >= 1, f" 状态 {statuses}")
        check("闸门统计的在跑数 = 实际 running 数", summaries._gate.active == running,
              f" gate={summaries._gate.active} running={running}")

        all_done = await wait_until(
            lambda: all(j.status in ("done", "error") for j in jobs), timeout=60
        )
        check("排队的最后也跑完了（闸门不会死锁）", all_done,
              f" {[j.status for j in jobs]}")
        check("三个都是 done", all(j.status == "done" for j in jobs),
              f" {[j.error for j in jobs]}")
        check("闸门全部释放", summaries._gate.active == 0, f" {summaries._gate.active}")

        # ============================================================
        print("\n== 7. 状态接口 / 任务列表 ==")
        # ============================================================
        r = await client.get(f"/api/tasks/{tids[0]}/summary/status")
        st = r.json()
        check("状态接口 200", r.status_code == 200, f" {r.status_code}")
        check("状态带终态", st.get("status") == "done", f" {st.get('status')}")
        check("状态带 has_summary", st.get("has_summary") is True, f" {st}")
        check("状态带正文字数", st.get("chars", 0) > 0, f" {st.get('chars')}")

        r = await client.get("/api/tasks/根本没有这个/summary/status")
        check("未知任务的状态接口 404", r.status_code == 404, f" {r.status_code}")

        # 存在但从没总结过的任务 → idle + has_summary=False（**不能误报成 done**）
        t13 = await new_task("bg0000000013", "从未总结")
        r = await client.get(f"/api/tasks/{t13.id}/summary/status")
        st13 = r.json()
        check("从没总结过的任务是 idle", st13.get("status") == "idle", f" {st13.get('status')}")
        check("从没总结过的 has_summary=False", st13.get("has_summary") is False, f" {st13}")

        r = await client.get("/api/tasks")
        listing = r.json()["tasks"]
        row = next((x for x in listing if x["id"] == tids[0]), {})
        check("任务列表每项都带 summary 字段", "summary" in row, f" {sorted(row)[:8]}")
        check("列表里也能看出有没有总结", row.get("summary", {}).get("has_summary") is True)
        never = next((x for x in listing if x["id"] == "bg0000000002"), {})
        check("跑过的任务在列表里状态可读",
              never.get("summary", {}).get("status") in ("done", "idle"),
              f" {never.get('summary', {}).get('status')}")

        # ============================================================
        print("\n== 8. 停止：保留已完成的分节（可以接着跑） ==")
        # ============================================================
        t9 = manager.get("bg0000000002")
        cleanup_paths(long_txt)
        mock_state.update({"delay": 0.05, "fail": False})
        job9 = summaries.start(t9, template="A", with_timestamp=True, custom_prompt="")
        got_section = await wait_until(
            lambda: job9.stage == "section" and job9.total > 0, timeout=30
        )
        check("长稿进入了逐节阶段", got_section, f" stage={job9.stage} total={job9.total}")

        r = await client.post(f"/api/tasks/{t9.id}/summary/cancel")
        check("停止接口 200", r.status_code == 200, f" {r.status_code}")
        stopped = await wait_until(lambda: job9.status == "cancelled", timeout=15)
        check("作业变成 cancelled", stopped, f" {job9.status}")
        check("没有写出总结文件（没跑完就不算数）",
              not os.path.isfile(os.path.splitext(long_txt)[0] + ".summary.md"))
        parts = os.path.splitext(long_txt)[0] + ".summary.md.parts"
        check("分节缓存保留（下次能接着跑）", os.path.isdir(parts),
              f" {sorted(os.listdir(parts)) if os.path.isdir(parts) else '无'}")

        r = await client.post(f"/api/tasks/{t9.id}/summary/cancel")
        check("停已经停了的 → 400（而不是静默成功）", r.status_code == 400, f" {r.status_code}")

        # 停止后还能接着跑完
        mock_state.update({"delay": 0.005})
        async with client.stream("POST", f"/api/tasks/{t9.id}/summarize",
                                 json={"template": "A"}) as r:
            raw = "".join([c async for c in r.aiter_text()])
        ev9 = parse_sse(raw)
        check("停止后重新挂上能跑完", any(e == "done" for e, _ in ev9),
              f" {[d for e, d in ev9 if e == 'error']}")
        check("续跑后文件写出来了", os.path.isfile(os.path.splitext(long_txt)[0] + ".summary.md"))

        # ============================================================
        print("\n== 9. 失败落到任务状态上（不是只在流里报一下） ==")
        # ============================================================
        t10 = await new_task("bg0000000010", "失败测试")
        mock_state.update({"delay": 0.0, "fail": True})
        async with client.stream("POST", f"/api/tasks/{t10.id}/summarize",
                                 json={"template": "A"}) as r:
            raw = "".join([c async for c in r.aiter_text()])
        ev10 = parse_sse(raw)
        check("流里报了 error", any(e == "error" for e, _ in ev10))
        st10 = summaries.state(t10.id)
        check("作业状态是 error", st10["status"] == "error", f" {st10['status']}")
        check("状态里带着错误信息（任务卡要用）", bool(st10["error"]), f" {st10['error'][:60]}")
        mock_state.update({"fail": False})
        async with client.stream("POST", f"/api/tasks/{t10.id}/summarize",
                                 json={"template": "A"}) as r:
            raw = "".join([c async for c in r.aiter_text()])
        check("失败后重试能成功（错误状态不算活跃作业）",
              any(e == "done" for e, _ in parse_sse(raw)))

        # ============================================================
        print("\n== 10. 跑着的时候不许删任务 ==")
        # ============================================================
        t11 = await new_task("bg0000000011", "删除守卫")
        mock_state.update({"delay": 0.08, "fail": False})
        async with client.stream("POST", f"/api/tasks/{t11.id}/summarize",
                                 json={"template": "A"}) as r:
            await r.aiter_text().__anext__()
            rr = await client.delete(f"/api/tasks/{t11.id}?files=true")
            check("总结跑着时删除被拒（400）", rr.status_code == 400, f" {rr.status_code}")
            check("拒绝理由说的清楚", "总结" in rr.json().get("detail", ""),
                  f" {rr.json().get('detail')}")
            check("任务还在（没被删掉）", manager.get(t11.id) is not None)

        await client.post(f"/api/tasks/{t11.id}/summary/cancel")
        await wait_until(lambda: not summaries.active(t11.id), timeout=15)
        rr = await client.delete(f"/api/tasks/{t11.id}?files=true")
        check("停掉之后可以正常删除", rr.status_code == 200, f" {rr.status_code} {rr.text[:80]}")
        check("删除后作业状态也被忘掉了", summaries.get(t11.id) is None)

        # ============================================================
        print("\n== 11. 进度真的在动（任务卡上那条进度条的数据源） ==")
        # ============================================================
        t12 = await new_task("bg0000000012", "进度测试")
        mock_state.update({"delay": 0.03, "fail": False})
        job12 = summaries.start(t12, template="A", with_timestamp=True, custom_prompt="")
        check("刚开跑时进度是 0 附近", summaries.state(t12.id)["progress"] <= 5,
              f" {summaries.state(t12.id)['progress']}")
        await wait_until(lambda: summaries.state(t12.id)["progress"] > 20, timeout=15)
        mid = summaries.state(t12.id)["progress"]
        check("跑起来之后进度涨上去了", mid > 20, f" {mid}")
        check("进行中状态是 running（卡片照着显示）",
              summaries.state(t12.id)["status"] == "running",
              f" {summaries.state(t12.id)['status']}")
        await wait_until(lambda: not summaries.get(t12.id).active, timeout=30)
        check("跑完进度是 100", summaries.state(t12.id)["progress"] == 100.0,
              f" {summaries.state(t12.id)['progress']}")
        check("跑完标签是「完成」", summaries.state(t12.id)["label"] == "完成",
              f" {summaries.state(t12.id)['label']}")

    # ---------------------------------------------------------------- 收尾
    await summaries.shutdown()
    for tid in ("bg0000000001", "bg0000000002", "bg0000000003", "bg0000000004",
                "bg0000000005", "bg0000000006", "bg0000000007", "bg0000000008",
                "bg0000000010", "bg0000000011", "bg0000000012", "bg0000000013"):
        manager.tasks.pop(tid, None)
    for p in (short_txt, long_txt):
        cleanup_paths(p)
    shutil.rmtree(TMP_DIR, ignore_errors=True)

    # 停掉被测服务，把端口还回去（走优雅退出，别让 lifespan 抛 CancelledError 糊一屏）
    app_server.should_exit = True
    try:
        await asyncio.wait_for(app_task, timeout=10)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
        app_task.cancel()


def main() -> int:
    print("bili-note 后台总结作业自检（本地假 LLM，不联网不花钱）")

    backup = CONFIG_PATH + ".selftest-backup"
    had_config = os.path.exists(CONFIG_PATH)
    if had_config:
        shutil.move(CONFIG_PATH, backup)
        print(f"\n（已把你的 config.json 暂存为 {os.path.basename(backup)}，跑完自动放回）")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "llm": {"base_url": MOCK_BASE, "api_key": "sk-selftest-fake",
                    "model": "mock-chat", "max_tokens": 4000},
            "summarize": {"long_doc_threshold": 4000, "max_concurrent": 2},
        }, f, ensure_ascii=False, indent=2)

    server = start_mock()
    try:
        asyncio.run(run_e2e())
    finally:
        server.should_exit = True
        time.sleep(0.3)
        if had_config:
            shutil.move(backup, CONFIG_PATH)
        elif os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)

    print(f"\n结果：{ok_count} 通过 / {fail_count} 失败")
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
