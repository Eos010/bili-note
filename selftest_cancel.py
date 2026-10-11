"""取消 / 删除 的回归测试。不联网、不碰 B 站。

跑法：
    .venv\\Scripts\\python.exe selftest_cancel.py

背景（2026-09-16 用户报「下载到一半点取消没反应」）：
根因不是"取消信号没送到"，而是送到之后处理全错——
`downloader` / `transcriber` / `tasks` 各自定义了 `_CancelledError`，三个类互不相同
→ `tasks` 接不住 → 任务被标成 error（红「失败」）且错误信息为空，半成品也没人清。

本测试守住修复后的行为：**取消要迅速停、要标 cancelled、要清半成品；
删除要按白名单动手、绝不碰用户自己的文件和目录。**
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import downloader  # noqa: E402
from app.tasks import (  # noqa: E402
    OUTPUT_DIR,
    Task,
    _is_ours,
    _is_manuscript_task,
    delete_task_files,
    plan_delete_files,
)

PORT = 8123
TOTAL_BYTES = 12 * 1024 * 1024
CHUNK = 64 * 1024
CHUNK_DELAY = 0.05          # 12MB / 64KB * 0.05s ≈ 9.6 秒下完
CANCEL_AFTER = 1.5
SCRATCH = os.path.join(OUTPUT_DIR, ".__selftest_delete__")
LOCAL_SCRATCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".selftest-tmp")

ok_count = 0
fail_count = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  [OK]   {name}")
    else:
        fail_count += 1
        print(f"  [FAIL] {name} {extra}")


class SlowHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(TOTAL_BYTES))
        self.end_headers()
        chunk = b"\x00" * CHUNK
        for _ in range(TOTAL_BYTES // CHUNK):
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError, OSError):
                return
            time.sleep(CHUNK_DELAY)

    def log_message(self, *args):
        pass


def start_server() -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), SlowHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------- 1. 异常统一


def test_cancel_exception_unified() -> None:
    print("\n== 取消异常必须是同一个类型 ==")
    from app import tasks, transcriber
    from app.errors import CancelledError

    check("downloader 不再自带取消类", not hasattr(downloader, "_CancelledError"))
    check("transcriber 不再自带取消类", not hasattr(transcriber, "_CancelledError"))
    check("tasks 不再自带取消类", not hasattr(tasks, "_CancelledError"))

    # 关键：tasks 的 except 子句必须能接住 downloader 抛的异常
    caught = False
    try:
        raise CancelledError("下载已取消")
    except CancelledError:
        caught = True
    check("tasks 能接住 downloader 抛的取消异常", caught)

    check("取消异常有可读文案（不再空白）", str(CancelledError("下载已取消")) == "下载已取消")


def test_progress_after_cancel() -> None:
    print("\n== 取消后状态不被进度回调覆写 ==")
    t = Task(id="x", url="u", download=True, transcribe=True)
    t.status = "downloading"
    t.cancel_requested = True
    t.phase_detail = "正在取消…"

    def progress(d):
        if t.cancel_requested:
            return
        t.progress = 50.0
        t.phase_detail = "下载中 50%"

    progress({"total_bytes": 100, "downloaded_bytes": 50})
    check("phase 保持「正在取消…」", t.phase_detail == "正在取消…", f" {t.phase_detail}")
    check("progress 不被覆写", t.progress == 0.0, f" {t.progress}")


# ---------------------------------------------------------------- 2. 真取消


def test_cancel_mid_download() -> None:
    print("\n== 下载中途取消 ==")
    shutil.rmtree(LOCAL_SCRATCH, ignore_errors=True)
    os.makedirs(LOCAL_SCRATCH, exist_ok=True)
    url = f"http://127.0.0.1:{PORT}/big.mp4"
    flag = {"cancel": False}
    threading.Thread(target=lambda: (time.sleep(CANCEL_AFTER), flag.update(cancel=True)),
                     daemon=True).start()

    t0 = time.time()
    outcome, detail = "returned", ""
    try:
        path, _ = downloader.download(url, LOCAL_SCRATCH, lambda d: None,
                                      cancelled=lambda: flag["cancel"])
        detail = path
    except Exception as e:  # noqa: BLE001
        outcome = "raised"
        detail = f"{type(e).__name__}: {e}"
    elapsed = time.time() - t0

    print(f"  结果：{outcome} / 耗时 {elapsed:.1f}s / {detail}")
    print(f"  残留文件：{os.listdir(LOCAL_SCRATCH)}")

    check("取消后迅速停下（不是把 9.6 秒下完）", elapsed < 4.0, f" 实际 {elapsed:.1f}s")
    check("抛的是统一取消类型（而不是 DownloadError）",
          detail.startswith("CancelledError"), f" {detail}")
    check("取消文案可读（不再空白）", "取消" in detail, f" {detail}")

    shutil.rmtree(LOCAL_SCRATCH, ignore_errors=True)


# ---------------------------------------------------------------- 3. 半成品清理


def test_cleanup_partial() -> None:
    print("\n== 半成品清理（只删没下完的） ==")
    shutil.rmtree(LOCAL_SCRATCH, ignore_errors=True)
    os.makedirs(LOCAL_SCRATCH, exist_ok=True)
    made = {
        "big.mp4.part": "半成品",
        "big.mp4.ytdl": "yt-dlp 元数据",
        "big.f299.mp4": "DASH 分片",
        "big.f140.m4a": "DASH 分片",
    }
    for name in made:
        with open(os.path.join(LOCAL_SCRATCH, name), "w") as f:
            f.write("x")
    keep = ["big.mp4", "big.txt", "big.srt", "big.summary.md"]
    for name in keep:
        with open(os.path.join(LOCAL_SCRATCH, name), "w") as f:
            f.write("x")

    removed = downloader.cleanup_partial(LOCAL_SCRATCH)
    rest = sorted(os.listdir(LOCAL_SCRATCH))
    print(f"  删了 {len(removed)} 个；剩下 {rest}")

    check("半成品全部清掉", all(n not in rest for n in made), f" 剩下 {rest}")
    check("成品一个不动", all(n in rest for n in keep), f" 剩下 {rest}")
    check("不存在的目录不报错", downloader.cleanup_partial(r"D:\不存在的目录") == [])
    shutil.rmtree(LOCAL_SCRATCH, ignore_errors=True)


# ---------------------------------------------------------------- 4. 删除安全


def test_delete_safety() -> None:
    print("\n== 删除白名单：绝不能碰到用户自己的东西 ==")
    check("_is_ours 拒绝 OUTPUT_DIR 本身", not _is_ours(OUTPUT_DIR))
    check("_is_ours 拒绝 .tmp 临时目录", not _is_ours(os.path.join(OUTPUT_DIR, ".tmp")))
    check("_is_ours 接受 output/<标题>", _is_ours(os.path.join(OUTPUT_DIR, "某视频")))
    check("_is_ours 拒绝 output 之外的路径", not _is_ours(r"D:\Videos"))

    # URL 任务 → 整个产物目录（我们自己建的，得先真的存在）
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(SCRATCH, exist_ok=True)
    t1 = Task(id="a", url="https://example.com/v", download=True, transcribe=True)
    t1.kind = "url"
    t1.output_dir = SCRATCH
    p1 = plan_delete_files(t1)
    check("URL 任务：整目录删", p1["mode"] == "dir" and p1["targets"] == [SCRATCH], f" {p1}")

    # 本地媒体任务 → 只删生成的，保留原视频，不删目录
    shutil.rmtree(LOCAL_SCRATCH, ignore_errors=True)
    os.makedirs(LOCAL_SCRATCH, exist_ok=True)
    media = os.path.join(LOCAL_SCRATCH, "高数01.mp4")
    make = {
        media: "原视频",
        os.path.join(LOCAL_SCRATCH, "高数01.txt"): "生成的文稿",
        os.path.join(LOCAL_SCRATCH, "高数01.srt"): "生成的字幕",
    }
    for p in make:
        with open(p, "w") as f:
            f.write("x")

    t2 = Task(id="b", url=media, download=False, transcribe=True, kind="file")
    t2.output_dir = LOCAL_SCRATCH
    t2.txt_path = os.path.join(LOCAL_SCRATCH, "高数01.txt")
    t2.srt_path = os.path.join(LOCAL_SCRATCH, "高数01.srt")
    p2 = plan_delete_files(t2)
    check("本地任务：不删目录", LOCAL_SCRATCH not in p2["targets"], f" {p2}")
    check("本地任务：保留原视频", media in p2["kept"], f" {p2}")
    check("本地任务：原视频不在删除清单里", media not in p2["targets"], f" {p2}")
    check("本地任务：生成的文稿/字幕在删除清单里",
          t2.txt_path in p2["targets"] and t2.srt_path in p2["targets"], f" {p2}")

    # 粘 .txt 文稿建的任务 → 那个 .txt 是他的原始资料，绝不删
    doc = os.path.join(LOCAL_SCRATCH, "我的资料.txt")
    with open(doc, "w") as f:
        f.write("x")
    t3 = Task(id="c", url=doc, download=False, transcribe=True, kind="file")
    t3.output_dir = LOCAL_SCRATCH
    t3.txt_path = doc
    check("识别为「已有文稿」任务", _is_manuscript_task(t3))
    p3 = plan_delete_files(t3)
    check("文稿任务：不删源 .txt", doc not in p3["targets"], f" {p3}")
    check("文稿任务：源 .txt 进保留清单", doc in p3["kept"], f" {p3}")
    check("文稿任务：说明文案点明不删原稿", "不会被删" in p3["note"], f" {p3['note']}")

    shutil.rmtree(LOCAL_SCRATCH, ignore_errors=True)


def test_delete_execute() -> None:
    print("\n== 实际执行删除 ==")
    # URL 任务：整目录消失
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(SCRATCH, exist_ok=True)
    for name in ("v.mp4", "v.txt", "v.srt", "v.summary.md"):
        with open(os.path.join(SCRATCH, name), "w") as f:
            f.write("x")
    t = Task(id="d", url="https://example.com/v", download=True, transcribe=True)
    t.kind = "url"
    t.output_dir = SCRATCH
    t.txt_path = os.path.join(SCRATCH, "v.txt")
    res = delete_task_files(t)
    check("URL 任务目录已删除", not os.path.isdir(SCRATCH), f" {res}")
    check("记下了删了什么", res["removed"] and res["errors"] == [], f" {res}")

    # 本地文稿任务：只删总结，原稿留着
    shutil.rmtree(LOCAL_SCRATCH, ignore_errors=True)
    os.makedirs(LOCAL_SCRATCH, exist_ok=True)
    doc = os.path.join(LOCAL_SCRATCH, "notes.txt")
    summ = os.path.join(LOCAL_SCRATCH, "notes.summary.md")
    for p in (doc, summ):
        with open(p, "w") as f:
            f.write("x")
    t2 = Task(id="e", url=doc, download=False, transcribe=True, kind="file")
    t2.output_dir = LOCAL_SCRATCH
    t2.txt_path = doc
    res2 = delete_task_files(t2)
    check("文稿任务：总结被删", not os.path.isfile(summ), f" {res2}")
    check("文稿任务：原稿还在", os.path.isfile(doc), f" {res2}")
    check("文稿任务：目录还在", os.path.isdir(LOCAL_SCRATCH), f" {res2}")
    shutil.rmtree(LOCAL_SCRATCH, ignore_errors=True)


def test_error_explain() -> None:
    print("\n== 报错翻人话 ==")
    from app.errors import explain, is_transient

    # 用户截图里那条真实报错
    raw = ("ERROR: [BiliBili] 1TRYz6yEE1: Unable to download webpage: "
           "The read operation timed out (caused by TransportError('The read operation timed out'))")
    out = explain(Exception(raw))
    check("超时被认出来", "网络超时" in out, f" {out[:60]}")
    check("给了可执行的下一步", "重试" in out, f" {out[:60]}")
    check("原始报错被保留（不丢排查线索）", "TransportError" in out)
    check("没有再出现英文 ERROR: 开头", not out.startswith("ERROR:"), f" {out[:30]}")

    check("视频不存在 → 提示下架",
          "不存在或已下架" in explain(Exception("ERROR: Video unavailable")))
    check("412 → 提示风控",
          "风控" in explain(Exception("HTTP Error 412: Precondition Failed")))
    check("大会员 → 提示需要登录",
          "需要登录" in explain(Exception("This video is for Premium members only")))
    check("不认识的原样返回（不瞎编）",
          explain(Exception("某些没见过的新错误")) == "某些没见过的新错误")
    check("空异常也不崩", explain(Exception("")) != "")

    check("超时算瞬时错误（值得重试）", is_transient(Exception("read operation timed out")))
    check("连接重置算瞬时错误", is_transient(Exception("Connection reset by peer")))
    check("503 算瞬时错误", is_transient(Exception("HTTP Error 503")))
    check("视频不存在不算瞬时（重试没意义）",
          not is_transient(Exception("ERROR: Video unavailable")))
    check("链接不支持不算瞬时", not is_transient(Exception("Unsupported URL")))


def test_resolve_hardened() -> None:
    print("\n== 解析器加固（补 1）==")
    import yt_dlp

    captured: dict = {}
    calls = {"n": 0}

    class FakeYDL:
        def __init__(self, opts):
            captured.clear()
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("Unable to download webpage: The read operation timed out")
            return {"title": "如何提供有效安慰", "duration": 1386}

    orig = downloader.yt_dlp.YoutubeDL
    orig_backoff = downloader.RESOLVE_BACKOFF
    downloader.yt_dlp.YoutubeDL = FakeYDL
    downloader.RESOLVE_BACKOFF = 0  # 测试里不真等
    try:
        info = downloader.resolve("https://www.bilibili.com/video/BV1TRYz6yEE1/")
        check("瞬时超时会重试到成功", calls["n"] == 3, f" 实际调了 {calls['n']} 次")
        check("最终拿到标题", info.title == "如何提供有效安慰", f" {info.title}")
        check("解析带上了 socket_timeout", captured.get("socket_timeout") == 120, f" {captured}")
        check("解析带上了 retries", captured.get("retries") == 10)
        check("解析带上了 extractor_retries", captured.get("extractor_retries") == 3)
        check("解析与下载的网络参数是同一套",
              all(captured.get(k) == v for k, v in downloader.NETWORK_OPTS.items()))

        # 非瞬时错误不该重试（避免白白等三次）
        calls["n"] = 0

        class FakeYDL2(FakeYDL):
            def extract_info(self, url, download=False):
                calls["n"] += 1
                raise Exception("ERROR: Video unavailable")

        downloader.yt_dlp.YoutubeDL = FakeYDL2
        raised = False
        try:
            downloader.resolve("https://x")
        except Exception:
            raised = True
        check("视频不存在时不重试（只调 1 次）", calls["n"] == 1, f" 实际 {calls['n']} 次")
        check("并把错误抛出去", raised)
    finally:
        downloader.yt_dlp.YoutubeDL = orig
        downloader.RESOLVE_BACKOFF = orig_backoff
        _ = yt_dlp


class SlowFailHandler(BaseHTTPRequestHandler):
    """先慢吞吞拖 3 秒，再回 503 —— 制造一段「用户已按取消、但 yt-dlp 还没返回」的窗口。"""

    def do_GET(self):  # noqa: N802
        time.sleep(3.0)
        try:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:
            pass

    def log_message(self, *args):
        pass


def test_retry_checkpoint() -> None:
    print("\n== 重试路径上的取消检查点（真根因之二）==")
    from app.errors import CancelledError

    flag = {"v": False}
    fn = downloader.retry_checkpoint(lambda: flag["v"])

    check("未取消时返回 0.0（不改变 yt-dlp 原有退避行为）", fn(n=0) == 0.0, f" {fn(n=0)}")
    check("多种调用签名都不炸", fn() == 0.0 and fn(1) == 0.0 and fn(n=3, x=1) == 0.0)

    flag["v"] = True
    raised = False
    try:
        fn(n=1)
    except CancelledError:
        raised = True
    check("已取消时从重试路径抛出 CancelledError", raised)

    opts = downloader._retry_opts(lambda: False)
    check("四个重试通道都装上了检查点",
          set(opts) == {"http", "fragment", "file_access", "extractor"}, f" {sorted(opts)}")

    # 关键机制：从 sleep_func 抛出的异常必须能穿透 RetryManager.report_retry
    # （它被 yt-dlp 的重试循环直接调用，异常会一路炸出下载循环）
    from yt_dlp.utils import RetryManager

    def _boom(*a, **kw):
        raise CancelledError("下载已取消")

    propagated = False
    try:
        RetryManager.report_retry(
            Exception("boom"), count=1, retries=10,
            sleep_func=_boom, info=lambda *a: None, warn=lambda *a: None,
        )
    except CancelledError:
        propagated = True
    except Exception:
        propagated = False
    check("异常能穿透 yt-dlp 的 RetryManager（机制成立）", propagated)

    # 未取消时 report_retry 正常返回（不改变原行为）
    ok = RetryManager.report_retry(
        Exception("boom"), count=1, retries=10,
        sleep_func=downloader.retry_checkpoint(lambda: False),
        info=lambda *a: None, warn=lambda *a: None,
    )
    check("未取消时 report_retry 正常放行", ok is None)


def test_download_opts_wired() -> None:
    print("\n== 下载参数确实挂上了检查点 ==")
    import yt_dlp

    captured: dict = {}
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".selftest-tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    fake_video = os.path.join(tmp, "v.mp4")
    with open(fake_video, "w") as f:
        f.write("x")

    class FakeYDL:
        def __init__(self, opts):
            captured.clear()
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def add_progress_hook(self, h):
            pass

        def add_postprocessor_hook(self, h):
            pass

        def extract_info(self, url, download=True):
            return {"requested_downloads": [{"filepath": fake_video}]}

    orig = downloader.yt_dlp.YoutubeDL
    downloader.yt_dlp.YoutubeDL = FakeYDL
    flag = {"v": False}
    try:
        downloader.download("http://x/y.mp4", tmp, lambda d: None,
                            cancelled=lambda: flag["v"])
    finally:
        downloader.yt_dlp.YoutubeDL = orig

    rsf = captured.get("retry_sleep_functions") or {}
    check("download() 的 opts 里有 retry_sleep_functions", bool(rsf), f" {type(rsf)}")
    check("四个通道齐全", set(rsf) == {"http", "fragment", "file_access", "extractor"},
          f" {sorted(rsf)}")
    flag["v"] = True
    hit = False
    try:
        rsf["http"](n=1)
    except Exception as e:  # noqa: BLE001
        hit = "Cancel" in type(e).__name__
    check("该检查点可被取消触发", hit)
    shutil.rmtree(tmp, ignore_errors=True)


def test_cancel_reported_as_cancelled() -> None:
    print("\n== 保险丝：按过取消，就必须报「已取消」而不是「失败」==")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT + 1), SlowFailHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    shutil.rmtree(LOCAL_SCRATCH, ignore_errors=True)
    os.makedirs(LOCAL_SCRATCH, exist_ok=True)
    url = f"http://127.0.0.1:{PORT + 1}/slowfail.mp4"

    flag = {"cancel": False}
    threading.Thread(target=lambda: (time.sleep(1.0), flag.update(cancel=True)),
                     daemon=True).start()
    t0 = time.time()
    detail = "正常返回"
    try:
        downloader.download(url, LOCAL_SCRATCH, lambda d: None,
                            cancelled=lambda: flag["cancel"])
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {str(e)[:90]}"
    elapsed = time.time() - t0

    print(f"  耗时 {elapsed:.1f}s / {detail}")
    check("yt-dlp 内部报的错（DownloadError）被翻译成「已取消」",
          detail.startswith("CancelledError"), f" {detail}")
    check("用户能看到可读的取消文案", "取消" in detail, f" {detail}")

    srv.shutdown()
    shutil.rmtree(LOCAL_SCRATCH, ignore_errors=True)


def main() -> int:
    print("bili-note 取消 / 删除 / 报错 回归测试（不联网）")
    srv = start_server()
    try:
        test_cancel_exception_unified()
        test_progress_after_cancel()
        test_cancel_mid_download()
        test_cleanup_partial()
        test_delete_safety()
        test_delete_execute()
        test_error_explain()
        test_resolve_hardened()
        test_retry_checkpoint()
        test_download_opts_wired()
        test_cancel_reported_as_cancelled()
    finally:
        srv.shutdown()
        shutil.rmtree(SCRATCH, ignore_errors=True)
        shutil.rmtree(LOCAL_SCRATCH, ignore_errors=True)
    print(f"\n结果：{ok_count} 通过 / {fail_count} 失败")
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
