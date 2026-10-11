"""bili-note：B站链接 → 视频 + 带时间戳文字稿 + 字幕。FastAPI 应用入口。"""
from __future__ import annotations

import json
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__ as APP_VERSION
from . import config as app_config
from . import library
from . import summarizer
from . import sumjobs
from . import summary_store
from .tasks import TaskManager, OUTPUT_DIR, media_path, plan_delete_files

STATIC_DIR = Path(__file__).parent / "static"


class SubmitBody(BaseModel):
    urls: list[str] = []
    files: list[str] = []
    download: bool = True
    transcribe: bool = True
    model: str = "large-v3"


class SummarizeBody(BaseModel):
    template: str = "A"
    custom_prompt: str = ""
    with_timestamp: bool | None = None
    # 正在跑的时候再点一次「总结」= 挂上去看（幂等）；要重跑必须显式说 restart。
    restart: bool = False


class SettingsBody(BaseModel):
    llm: dict = {}
    summarize: dict = {}


manager = TaskManager()
summaries = sumjobs.manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.start()
    try:
        yield
    finally:
        # 关机时把在跑的总结收干净：它们是挂在事件循环上的独立 task，
        # 不管的话会被硬砍，分节缓存可能停在半个文件上。
        await summaries.shutdown()


app = FastAPI(title="bili-note", version=APP_VERSION, lifespan=lifespan)


@app.get("/api/version")
def get_version():
    """版本号只有 `app/__init__.py` 一处定义，前端顶栏来这儿取（避免文档/界面各写一份对不上）。"""
    return {"version": APP_VERSION}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/tasks")
async def submit(body: SubmitBody):
    if not body.urls and not body.files:
        raise HTTPException(400, "请粘贴链接或本地文件路径")
    if not body.download and not body.transcribe:
        raise HTTPException(400, "下载和转写至少开启一个")
    ids = await manager.submit(body.urls, body.files, body.download, body.transcribe, body.model)
    return {"task_ids": ids}


def _task_payload(t) -> dict:
    """任务对象 + 它自己的总结状态。

    总结状态挂在任务上（而不是单独一次请求），首页每张卡片才能各显示各的进度——
    「同时总结几个视频」要的就是这个。`has_summary` 是**文件事实**，
    所以服务重启后（作业表空了）卡片照样知道"这个已经总结过了"。
    """
    d = t.to_dict()
    state = summaries.state(t.id)
    state["has_summary"] = summary_store.exists(t)
    d["summary"] = state
    return d


@app.get("/api/tasks")
def list_tasks():
    return {"tasks": [_task_payload(t) for t in manager.list_all()]}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    t = manager.get(task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    return _task_payload(t)


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    if manager.get(task_id) is None:
        raise HTTPException(404, "任务不存在")
    ok = await manager.cancel(task_id)
    if not ok:
        raise HTTPException(400, "任务不可取消（可能已经结束了）")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str):
    """失败/取消的任务：原样重跑（不用重新粘链接）。"""
    if manager.get(task_id) is None:
        raise HTTPException(404, "任务不存在")
    ok = await manager.retry(task_id)
    if not ok:
        raise HTTPException(400, "只能重试已失败或已取消的任务")
    return {"ok": True}


@app.get("/api/tasks/{task_id}/files")
def preview_files(task_id: str):
    """只算不删：告诉前端「移除并删除文件」会删掉什么、留下什么。"""
    t = manager.get(task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    return plan_delete_files(t)


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, files: bool = False):
    """移除任务。`?files=true` 时连同产物一起删（本地文件任务的源文件永不删）。"""
    # 总结作业和任务产物共用同一批文件：作业还在跑就删，等于让两个进程抢同一个目录。
    running = summaries.active(task_id)
    if running is not None:
        raise HTTPException(400, "这个任务的总结还在跑，请先「停止」再移除")
    result = manager.remove(task_id, delete_files=files)
    if not result.get("ok"):
        raise HTTPException(400, result.get("reason") or "移除失败")
    await summaries.forget(task_id)   # 连带忘掉它的总结状态，别在内存里留孤儿
    return result


def _open_in_explorer(path: str) -> None:
    """用系统文件管理器打开一个目录。

    本地工具、只跑在用户自己机器上，所以直接 `startfile`；
    但失败必须转成 400 —— 否则前端看到一个没头没尾的 500，
    症状正是项目档里记过的「点按钮没反应」。
    """
    if not hasattr(os, "startfile"):  # 非 Windows 平台
        raise HTTPException(400, "当前平台不支持打开目录")
    try:
        os.startfile(path)  # noqa: S606 —— 本地工具，打开用户自己机器的目录
    except OSError as e:
        raise HTTPException(400, f"打开目录失败：{e}") from e


@app.post("/api/tasks/{task_id}/open")
def open_dir(task_id: str):
    t = manager.get(task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    target = t.output_dir or (os.path.dirname(t.txt_path) if t.txt_path else None)
    if not target or not os.path.isdir(target):
        raise HTTPException(400, "输出目录不存在")
    _open_in_explorer(target)
    return {"ok": True}


@app.post("/api/open-output")
def open_output_dir():
    """打开全局产物目录 `output/`（顶栏那个按钮）。

    注意口径：**只有下载类任务**的产物落在 output/；本地文件任务的 .txt/.srt
    写在源文件旁边，那是任务卡片上「打开目录」的活。
    """
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)  # 目录不存在就先建，否则 Explorer 会弹错误框
    except OSError as e:
        raise HTTPException(400, f"输出目录不可用：{e}") from e
    _open_in_explorer(OUTPUT_DIR)
    return {"ok": True, "path": OUTPUT_DIR}


@app.get("/api/tasks/{task_id}/text")
def preview_text(task_id: str):
    t = manager.get(task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    if not t.txt_path or not os.path.exists(t.txt_path):
        raise HTTPException(400, "文字稿不存在")
    with open(t.txt_path, "r", encoding="utf-8") as f:
        return PlainTextResponse(f.read())


# ---------------------------------------------------------------- 已有文稿（自动扫描 output/）
#
# 为什么单开一组端点、而不是塞进任务表：
# ① 任务表是内存态（重启即空），而 output/ 里的文稿是**文件事实**——两者生命周期不同，
#    混在一起就会出现"重启后一堆历史任务冒出来、没法区分哪些是这轮跑的"；
# ② 前端**只发 id 不发路径**（见 `library.resolve`），路径穿越那类问题没有入口；
# ③ 这一组是只读的：能看、能读总结，不改任何文件。


def _library_item(item_id: str):
    item = library.resolve(item_id)
    if item is None:
        raise HTTPException(404, "文稿不存在（可能已被移动或删除）")
    return item


@app.get("/api/library")
def library_list():
    """扫一遍 `output/`，列出符合转写格式的文稿（新的在前）。"""
    # 根目录取 `library.root()`（它读的就是 `tasks.OUTPUT_DIR`）——不要在接口里
    # 再引一份自己的 OUTPUT_DIR，那会变成同一个事实的第二个来源。
    return {"root": library.root(), "items": [library.public(i) for i in library.scan()]}


@app.get("/api/library/{item_id}/text")
def library_text(item_id: str):
    item = _library_item(item_id)
    try:
        return PlainTextResponse(library.read_text(item))
    except OSError as e:
        raise HTTPException(400, f"文稿读不出来：{e}") from e


@app.get("/api/library/{item_id}/summary")
def library_summary(item_id: str):
    item = _library_item(item_id)
    content, outline = library.read_summary(item)
    return {"exists": bool(content), "content": content, "outline": outline}


@app.get("/api/library/{item_id}/media")
def library_media(item_id: str):
    """给 `#sum-video` 用：点总结里的时间戳可以跳回本地视频。

    媒体文件是从**同一目录**里挑的（与文稿同名的优先），不是前端指定的路径。
    """
    item = _library_item(item_id)
    path = item["_media_path"]
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "这个文稿旁边没有可播放的媒体文件")
    media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


# ---------------------------------------------------------------- 总结（v1.3 / v2.1 后台化）


def _require_transcript(task_id: str):
    t = manager.get(task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    if not t.txt_path or not os.path.isfile(t.txt_path):
        raise HTTPException(400, "这个任务还没有文字稿，无法总结")
    return t


@app.get("/api/tasks/{task_id}/summary")
def get_summary(task_id: str):
    t = _require_transcript(task_id)
    content, outline = summary_store.read(t)
    return {"exists": bool(content), "content": content, "outline": outline}


@app.get("/api/tasks/{task_id}/summary/status")
def summary_status(task_id: str):
    """轻量轮询：只回总结状态，不碰文稿和正文。

    任务列表里已经带了同样的信息（`task.summary`），这个口子给
    "只想盯一个任务"的场景用，也方便手动排查。
    """
    t = manager.get(task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    state = summaries.state(task_id)
    state["has_summary"] = summary_store.exists(t)
    return state


@app.post("/api/tasks/{task_id}/summary/cancel")
async def cancel_summary(task_id: str):
    """停止正在跑的总结。**已完成的节会保留**，下次生成接着跑，不从头再来。"""
    if manager.get(task_id) is None:
        raise HTTPException(404, "任务不存在")
    ok = await summaries.stop(task_id)
    if not ok:
        raise HTTPException(400, "这个任务当前没有正在跑的总结")
    return {"ok": True}


@app.delete("/api/tasks/{task_id}/summary")
def delete_summary(task_id: str):
    t = _require_transcript(task_id)
    summary_store.remove(t)
    return {"ok": True}


@app.get("/api/tasks/{task_id}/media")
def stream_media(task_id: str):
    """给前端 <video> 用。Starlette 的 FileResponse 支持 Range，所以能拖动跳转。"""
    t = manager.get(task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    path = media_path(t)
    if not path:
        raise HTTPException(404, "没有可播放的媒体文件")
    media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _outline_path(t) -> str:
    """（已迁到 `summary_store.outline_path`，这里只留给自检/排查用的别名。）"""
    return summary_store.outline_path(t)


@app.post("/api/tasks/{task_id}/summarize")
async def summarize(task_id: str, body: SummarizeBody):
    """开始（或接上）一个**后台总结作业**，并把这根连接作为它的观看窗口。

    ⚠️ 这条流**不是**总结本身（v2.0 以前是，于是关掉弹窗就把总结掐了）。
    真正的生成跑在 `sumjobs` 起的独立 task 上，连接断开只影响这一个人看不看得见，
    作业照跑、任务卡上的进度照更新。所以：

    - 已经有一个在跑 → 直接挂上去看（幂等，重复点「总结」不会起第二个作业）；
    - 要重跑 → 显式 `restart=true`（前端「重新生成」按钮）。
    """
    t = _require_transcript(task_id)          # 先校验：不通过就别起作业，免留孤儿
    opts = app_config.load().get("summarize") or {}
    with_ts = body.with_timestamp
    if with_ts is None:
        with_ts = bool(opts.get("with_timestamp", True))
    template = (body.template or opts.get("template") or "A").upper()

    job = summaries.active(task_id)
    if job is not None and body.restart:
        await summaries.stop(task_id)
        job = None
    if job is None:
        job = summaries.start(
            t, template=template, with_timestamp=bool(with_ts),
            custom_prompt=body.custom_prompt,
        )

    async def gen():
        async for event, data in summaries.subscribe(job):
            yield _sse(event, data)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------- 设置


@app.get("/api/settings")
def get_settings():
    return app_config.public()


@app.post("/api/settings")
def save_settings(body: SettingsBody):
    app_config.save({"llm": body.llm, "summarize": body.summarize})
    return app_config.public()


@app.get("/api/templates")
def get_templates():
    return {"templates": summarizer.list_templates()}


@app.get("/api/models")
async def get_models():
    """测试连接并列出可用模型（设置面板用）。"""
    cfg = app_config.load()
    try:
        models = await summarizer.fetch_models(cfg)
    except summarizer.SummarizeError as e:
        raise HTTPException(400, str(e)) from e
    return {"models": models}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
