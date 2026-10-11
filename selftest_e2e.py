"""bili-note 端到端自检：用本地假 LLM 端点跑通整条总结链路。

不需要 API Key、不联网、不花钱。跑法：
    .venv\\Scripts\\python.exe selftest_e2e.py

覆盖：合并 → 提示词 → httpx 流式 → SSE 解析 → .md 落盘 → 错误/截断分支。
跑完会清理掉自己生成的 .summary.md 和临时 config.json。
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
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
MOCK_PORT = 8099
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}/v1"

ok_count = 0
fail_count = 0
seen_prompt = {"user": "", "system": ""}
seen_body: dict = {}


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


@mock.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    model = body.get("model", "")
    msgs = body.get("messages") or []
    seen_prompt["system"] = msgs[0]["content"] if msgs else ""
    seen_prompt["user"] = msgs[-1]["content"] if msgs else ""
    seen_body.clear()
    seen_body.update(body)

    if model == "mock-401":
        return JSONResponse(
            {"error": {"message": "Authentication Fails, Your api key is invalid"}},
            status_code=401,
        )

    finish = "length" if model == "mock-length" else "stop"
    pieces = [
        "### 开场自述\n",
        "[00:00:00]\n",
        "- 他先交代背景，原话「上学期快结束的时候呢」\n",
        "- 接着说到了记点与排名，原话「我好像从来都不知道」\n",
        "### 方法\n",
        "[00:04:04]\n",
        "- 建立信心 + 找到方法（大意）\n",
    ]

    async def gen():
        # 推理模型会先吐 reasoning_content（真实 deepseek-flash 会持续几十秒）
        for p in ["用户要一份事无巨细的总结，", "我需要逐段核对原话……", "先看开头交代的背景。"]:
            yield "data: " + json.dumps(
                {"choices": [{"delta": {"reasoning_content": p}, "finish_reason": None}]},
                ensure_ascii=False,
            ) + "\n\n"
            await asyncio.sleep(0.005)
        for p in pieces:
            yield "data: " + json.dumps(
                {"choices": [{"delta": {"content": p}, "finish_reason": None}]},
                ensure_ascii=False,
            ) + "\n\n"
            await asyncio.sleep(0.005)
        yield ": keep-alive\n\n"  # 混入注释行，验证解析器能跳过
        yield "data: " + json.dumps(
            {"choices": [{"delta": {}, "finish_reason": finish}]}, ensure_ascii=False
        ) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@mock.get("/v1/models")
async def models():
    return {"data": [{"id": "mock-chat"}, {"id": "mock-length"}, {"id": "mock-401"}]}


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


# ---------------------------------------------------------------- SSE 解析


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


# ---------------------------------------------------------------- 主流程


async def run() -> None:
    from app import config as app_config
    from app import summarizer
    from app.main import app, manager
    from app.tasks import Task

    # ASGITransport 不会跑 lifespan，手动把队列 worker 起起来
    await manager.start()

    # 找一份真实文稿
    files = sorted(glob.glob(os.path.join(BASE, "output", "**", "*.txt"), recursive=True),
                   key=os.path.getsize)
    if not files:
        print("output/ 下没有 .txt，无法做端到端测试")
        return
    txt = files[0]
    print(f"\n用文稿：{os.path.basename(txt)}（{os.path.getsize(txt)} 字节）")

    # 造一个「已完成」的任务塞进 manager（绕开真实转写）
    tid = "e2etest0001"
    t = Task(id=tid, url="D:\\fake\\video.mp4", download=False, transcribe=True, kind="file")
    t.status = "done"
    t.progress = 100.0
    t.title = "端到端测试"
    t.txt_path = txt
    t.output_dir = os.path.dirname(txt)
    manager.tasks[tid] = t

    summary_path = os.path.splitext(txt)[0] + ".summary.md"
    if os.path.exists(summary_path):
        os.remove(summary_path)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as client:
        # ---- 1. 正常流 ----
        print("\n== 1. 正常流式总结 ==")
        async with client.stream("POST", f"/api/tasks/{tid}/summarize",
                                 json={"template": "A", "with_timestamp": True}) as r:
            check("HTTP 200", r.status_code == 200)
            check("Content-Type 是 event-stream",
                  "text/event-stream" in r.headers.get("content-type", ""))
            raw = "".join([chunk async for chunk in r.aiter_text()])

        events = parse_sse(raw)
        kinds = [e for e, _ in events]
        print(f"  收到事件：{kinds}")
        check("有 start 事件", "start" in kinds)
        check("有 meta 事件", "meta" in kinds)
        check("有多个 delta 事件", kinds.count("delta") >= 5)
        check("有 done 事件", "done" in kinds)
        check("思考过程被转发（reasoning 事件）", kinds.count("reasoning") >= 3,
              f" 实际 {kinds.count('reasoning')} 个")
        check("思考内容非空",
              len("".join(d.get("text", "") for e, d in events if e == "reasoning")) > 10)

        meta = next((d for e, d in events if e == "meta"), {})
        check("meta 带段数与字数", meta.get("segments", 0) > 0 and meta.get("chars", 0) > 0,
              f" {meta}")

        text = "".join(d.get("text", "") for e, d in events if e == "delta")
        check("拼接出的总结非空", len(text) > 50)
        check("思考内容没混进正文",
              "reasoning" not in text and "先看开头交代的背景" not in text)

        done = next((d for e, d in events if e == "done"), {})
        check("done 未标记截断", done.get("truncated") is False)
        check("done 带保存路径", bool(done.get("path")))
        check("done 报告思考字数", done.get("reasoning_chars", 0) > 0, f" {done}")

        # 落盘检查
        check(".summary.md 已生成", os.path.isfile(summary_path))
        content = open(summary_path, "r", encoding="utf-8").read() if os.path.isfile(summary_path) else ""
        check("含 front-matter", content.startswith("---\n"))
        check("front-matter 记录模板与模型",
              "template: A" in content and "model: mock-chat" in content)
        check("正文写入完整", text.strip() in content)

        # 提示词检查（模型这一侧真的收到了合并后的文稿）
        check("提示词含【文稿正文】", "【文稿正文】" in seen_prompt["user"])
        check("提示词含时间戳要求", "标注位置" in seen_prompt["user"])
        check("文稿已合并（行数远少于原始碎片）",
              seen_prompt["user"].count("\n") < 200, f" 实际 {seen_prompt['user'].count(chr(10))} 行")
        check("system 提示词非空", len(seen_prompt["system"]) > 10)

        # ---- 2. 缓存读取 ----
        print("\n== 2. 读回已保存的总结 ==")
        r = await client.get(f"/api/tasks/{tid}/summary")
        data = r.json()
        check("GET /summary 返回 exists=true", data.get("exists") is True)
        check("GET /summary 内容与文件一致", data.get("content", "").startswith("---"))

        # ---- 3. 无时间戳 ----
        print("\n== 3. 关掉时间戳 ==")
        async with client.stream("POST", f"/api/tasks/{tid}/summarize",
                                 json={"template": "A", "with_timestamp": False}) as r:
            raw = "".join([chunk async for chunk in r.aiter_text()])
        check("关掉后提示词不再要求时间戳", "标注位置" not in seen_prompt["user"])
        check("关掉后仍是 200 流程", "done" in [e for e, _ in parse_sse(raw)])

        # ---- 4. 截断分支 ----
        print("\n== 4. 输出被截断（finish_reason=length）==")
        cfg = app_config.load()
        app_config.save({"llm": {"model": "mock-length"}})
        async with client.stream("POST", f"/api/tasks/{tid}/summarize", json={"template": "C"}) as r:
            raw = "".join([chunk async for chunk in r.aiter_text()])
        done = next((d for e, d in parse_sse(raw) if e == "done"), {})
        check("done 标记 truncated=true", done.get("truncated") is True)
        check("finish_reason=length 被透传", done.get("finish_reason") == "length")
        app_config.save({"llm": {"model": cfg["llm"]["model"]}})

        # ---- 5. 错误分支 ----
        print("\n== 5. API 报错（401）==")
        app_config.save({"llm": {"model": "mock-401"}})
        async with client.stream("POST", f"/api/tasks/{tid}/summarize", json={"template": "A"}) as r:
            raw = "".join([chunk async for chunk in r.aiter_text()])
        err = next((d for e, d in parse_sse(raw) if e == "error"), {})
        check("返回 error 事件", bool(err))
        check("401 错误信息可读", "401" in err.get("message", "") and "Key" in err.get("message", ""),
              f" {err.get('message','')[:80]}")
        app_config.save({"llm": {"model": "mock-chat"}})

        # ---- 6. 没有文稿的任务 ----
        print("\n== 6. 无文稿任务 ==")
        t2 = Task(id="e2etest0002", url="D:\\fake\\nope.mp4", download=False, transcribe=True, kind="file")
        t2.status = "done"
        manager.tasks[t2.id] = t2
        r = await client.post(f"/api/tasks/{t2.id}/summarize", json={"template": "A"})
        check("无文稿任务返回 400", r.status_code == 400, f" 实际 {r.status_code}")
        r = await client.post("/api/tasks/不存在/summarize", json={"template": "A"})
        check("不存在的任务返回 404", r.status_code == 404, f" 实际 {r.status_code}")

        # ---- 7. 视频端点 ----
        print("\n== 7. 媒体端点 ==")
        r = await client.get(f"/api/tasks/{tid}/media")
        check("无媒体文件时返回 404", r.status_code == 404, f" 实际 {r.status_code}")

        # ---- 8. 模型列表 ----
        print("\n== 8. 模型列表（测试连接）==")
        r = await client.get("/api/models")
        check("能列出模型", r.status_code == 200 and len(r.json().get("models", [])) == 3,
              f" {r.status_code} {r.text[:120]}")

        # ---- 9. 粘 .txt 路径 → 跳过转写（对已有文稿做总结的入口）----
        print("\n== 9. 粘 .txt 路径直接进任务 ==")
        ids = await manager.submit([], [txt], download=False, transcribe=True, model="large-v3")
        t3 = manager.get(ids[0])
        for _ in range(100):
            await asyncio.sleep(0.05)
            if t3.status in ("done", "error"):
                break
        check("任务完成（跳过转写）", t3.status == "done", f" 实际 {t3.status} {t3.error}")
        check("txt_path 指向原文稿", t3.txt_path == os.path.abspath(txt))
        check("没有产生 video_path（说明没走转写）", not t3.video_path, f" {t3.video_path}")
        # .txt 不在 _MEDIA_EXTS 里：没报「不支持的文件类型」就证明走的是 .txt 专用分支
        check("没有报不支持的文件类型（走的 .txt 分支）", t3.error == "", f" {t3.error}")
        r = await client.get(f"/api/tasks/{t3.id}/media")
        check("media 端点拒绝把 .txt 当视频发（404）", r.status_code == 404, f" 实际 {r.status_code}")
        async with client.stream("POST", f"/api/tasks/{t3.id}/summarize",
                                 json={"template": "A"}) as r:
            raw = "".join([chunk async for chunk in r.aiter_text()])
        check("能直接对它跑总结", "done" in [e for e, _ in parse_sse(raw)])
        if os.path.isfile(summary_path):
            os.remove(summary_path)
        manager.tasks.pop(t3.id, None)

        # ---- 10. 思考开关 ----
        print("\n== 10. 思考（推理）开关 ==")
        app_config.save({"llm": {"thinking_enabled": False}})
        async with client.stream("POST", f"/api/tasks/{tid}/summarize",
                                 json={"template": "A"}) as r:
            raw = "".join([chunk async for chunk in r.aiter_text()])
        check("关闭时请求体带 thinking.type=disabled",
              seen_body.get("thinking") == {"type": "disabled"},
              f" 实际 {seen_body.get('thinking')}")
        check("关闭时仍能正常完成", "done" in [e for e, _ in parse_sse(raw)])

        app_config.save({"llm": {"thinking_enabled": True}})
        async with client.stream("POST", f"/api/tasks/{tid}/summarize",
                                 json={"template": "A"}) as r:
            raw = "".join([chunk async for chunk in r.aiter_text()])
        check("开启时请求体不带 thinking 字段（用服务端默认）",
              "thinking" not in seen_body, f" 实际 {seen_body.get('thinking')}")
        check("开启时仍能正常完成", "done" in [e for e, _ in parse_sse(raw)])

        # ---- 11. 失败重试 ----
        print("\n== 11. 失败重试 ==")
        t4 = Task(id="e2etest0003", url=txt, download=False, transcribe=True, kind="file")
        t4.status = "error"
        t4.error = "假的失败信息"
        t4.phase_detail = "失败"
        manager.tasks[t4.id] = t4

        r = await client.post(f"/api/tasks/{t4.id}/retry")
        check("重试返回 200", r.status_code == 200, f" 实际 {r.status_code}")
        for _ in range(120):
            await asyncio.sleep(0.05)
            if t4.status in ("done", "error"):
                break
        check("重试后跑完", t4.status == "done", f" {t4.status} {t4.error}")
        check("旧的错误信息被清空", t4.error == "", f" {t4.error}")

        r = await client.post(f"/api/tasks/{t4.id}/retry")
        check("已成功的任务不能重试（400）", r.status_code == 400, f" 实际 {r.status_code}")
        r = await client.post("/api/tasks/不存在的id/retry")
        check("不存在的任务重试返回 404", r.status_code == 404, f" 实际 {r.status_code}")
        manager.tasks.pop(t4.id, None)

        # ---- 12. HTTP 层的取消接口 ----
        # ⚠️ 2026-09-17：上一轮修「取消」时我只测了 downloader 那一层，
        # **没测 HTTP 这一层**，于是漏掉了真根因——`t.cancellable` 读的是不存在的
        # 对象属性（那个名字只作为 to_dict() 的字典 key 存在），cancel 每次都 500。
        # 这一段就是补那个洞，永远别再漏。
        print("\n== 12. HTTP 取消接口（真根因所在层）==")
        t5 = Task(id="e2etest0004", url="https://example.com/v", download=True, transcribe=True)
        t5.status = "downloading"
        t5.progress = 30.0
        t5.phase_detail = "下载中 30%"
        manager.tasks[t5.id] = t5

        r = await client.post(f"/api/tasks/{t5.id}/cancel")
        check("取消返回 200（不是 500）", r.status_code == 200,
              f" 实际 {r.status_code} {r.text[:150]}")
        check("cancel_requested 被置位", t5.cancel_requested is True)
        check("phase 变成「正在取消…」", t5.phase_detail == "正在取消…", f" {t5.phase_detail}")

        r = await client.post("/api/tasks/不存在的id/cancel")
        check("不存在的任务取消 → 404", r.status_code == 404, f" 实际 {r.status_code}")

        t6 = Task(id="e2etest0005", url="https://example.com/v", download=True, transcribe=True)
        t6.status = "done"
        manager.tasks[t6.id] = t6
        r = await client.post(f"/api/tasks/{t6.id}/cancel")
        check("已完成的任务取消 → 400（不是 500）", r.status_code == 400, f" 实际 {r.status_code}")
        check("错误信息是人话", "不可取消" in r.text, f" {r.text[:120]}")

        # 列表接口的 cancellable 字段（前端按钮显隐靠它）必须和对象属性一致
        r = await client.get("/api/tasks")
        byid = {x["id"]: x for x in r.json()["tasks"]}
        check("下载中任务 cancellable=true", byid[t5.id]["cancellable"] is True)
        check("已完成任务 cancellable=false", byid[t6.id]["cancellable"] is False)
        check("cancellable 与对象属性同源",
              byid[t5.id]["cancellable"] == t5.cancellable
              and byid[t6.id]["cancellable"] == t6.cancellable)
        manager.tasks.pop(t5.id, None)
        manager.tasks.pop(t6.id, None)

    # 清理
    manager.tasks.pop(tid, None)
    manager.tasks.pop("e2etest0002", None)
    if os.path.exists(summary_path):
        os.remove(summary_path)
        print(f"\n（已清理测试产物 {os.path.basename(summary_path)}）")


def main() -> int:
    print("bili-note 端到端自检（本地假 LLM，不联网不花钱）")

    backup = CONFIG_PATH + ".selftest-backup"
    had_config = os.path.exists(CONFIG_PATH)
    if had_config:
        # 用 move 而不是 copy：万一本测试中途被打断，备份文件仍在磁盘上，
        # 用户还能手动把 config.json.selftest-backup 改回来（含 API Key）。
        shutil.move(CONFIG_PATH, backup)
        print(f"（已把你的 config.json 暂存为 {os.path.basename(backup)}，跑完自动放回）")
    # 指向假端点
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "llm": {"base_url": MOCK_BASE, "api_key": "sk-selftest-fake", "model": "mock-chat",
                    "max_tokens": 4000},
            "summarize": {},
        }, f, ensure_ascii=False, indent=2)

    server = start_mock()
    try:
        asyncio.run(run())
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
