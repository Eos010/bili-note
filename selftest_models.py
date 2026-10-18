"""V3 模型管理自检（离线；用本地假 HuggingFace 源，真下载、真续传、真校验）。

跑法：
    .venv\\Scripts\\python.exe selftest_models.py

为什么要有这套：
- 模型下载是 V3 最容易出事故的一段（路径、镜像、续传、取消、校验、磁盘）；
- 它不能靠"看代码"验证，也不能每次真下 464MB/3GB；
- 所以起一个本地 HTTP 源，把清单/文件/Range/慢速/坏文件都模拟出来，
  再让真正的 `models.DownloadManager` 跑一遍。

不会碰你的真实 `config.json` / `output/` / `models/`：进程启动前把
`BILI_NOTE_DATA_DIR` 指到 `.selftest-tmp/models-<pid>`。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
TMP = BASE / ".selftest-tmp" / f"models-{os.getpid()}"
MODELS = TMP / "models"
os.environ["BILI_NOTE_DATA_DIR"] = str(TMP)
os.environ["BILI_NOTE_DISABLE_DIALOG"] = "1"
os.environ.pop("BILI_NOTE_MODELS_DIR", None)
os.environ.pop("BILI_NOTE_ASR_ENDPOINT", None)
os.environ.pop("BILI_NOTE_ASR_PROXY", None)
shutil.rmtree(TMP, ignore_errors=True)
(TMP / "output").mkdir(parents=True, exist_ok=True)
MODELS.mkdir(parents=True, exist_ok=True)

import httpx  # noqa: E402

from app import app_paths, config as app_config, hardware, models, tasks  # noqa: E402
from app import main as app_main  # noqa: E402

# ---------------------------------------------------------------- 假模型 & 假源

REPO = "test/faster-whisper-test"
FAKE_NAME = "tiny-test"
FAKE_SIZE = 2 * 1024 * 1024
FAKE_BIN = bytes((i * 37 + 11) % 251 for i in range(FAKE_SIZE))
FAKE_SHA = hashlib.sha256(FAKE_BIN).hexdigest()
FAKE_FILES = {
    "config.json": b'{"n_mels": 80, "model": "test"}',
    "model.bin": FAKE_BIN,
    "tokenizer.json": b'{"model": {"type": "BPE"}}',
    "vocabulary.txt": b"a\nb\nc\n",
}
FAKE = models.AsrModel(FAKE_NAME, REPO, FAKE_SIZE, FAKE_NAME, "测试用假模型", 0, "cpu")

REAL_CATALOG = models.CATALOG
models.CATALOG = (FAKE,)
models.BY_NAME = {FAKE_NAME: FAKE}

MODE = {"slow": False, "corrupt": False, "no_model": False}


class FakeHF(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102
        pass

    def _json(self, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _manifest(self) -> None:
        items = []
        for name, content in FAKE_FILES.items():
            if MODE["no_model"] and name == "model.bin":
                continue
            item = {"path": name, "size": len(content)}
            if name == "model.bin":
                item["lfs"] = {"size": len(content), "oid": FAKE_SHA}
            items.append(item)
        self._json(items)

    def _file(self, name: str) -> None:
        content = FAKE_FILES.get(name)
        if content is None:
            self.send_error(404)
            return
        if MODE["corrupt"] and name == "model.bin":
            content = b"Z" * len(content)
        total = len(content)
        start = 0
        header = self.headers.get("Range")
        if header and header.startswith("bytes="):
            try:
                start = int(header.split("=", 1)[1].split("-", 1)[0])
            except ValueError:
                start = 0
        if start >= total:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = content[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
        self.end_headers()
        if MODE["slow"] and name == "model.bin":
            step = 32 * 1024
            for i in range(0, len(body), step):
                self.wfile.write(body[i:i + step])
                self.wfile.flush()
                time.sleep(0.01)
        else:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == f"/api/models/{REPO}/tree/main":
            self._manifest()
        elif path.startswith(f"/{REPO}/resolve/main/"):
            self._file(path.rsplit("/", 1)[-1])
        else:
            self.send_error(404)


SERVER = ThreadingHTTPServer(("127.0.0.1", 0), FakeHF)
SERVER.daemon_threads = True
PORT = SERVER.server_address[1]
ENDPOINT = f"http://127.0.0.1:{PORT}"
threading.Thread(target=SERVER.serve_forever, daemon=True).start()
os.environ["BILI_NOTE_ASR_ENDPOINT"] = ENDPOINT

# ---------------------------------------------------------------- 断言

ok_count = 0
fail_count = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  [OK]   {name}")
    else:
        fail_count += 1
        print(f"  [FAIL] {name}{detail}")


def wait_job(pred, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last = models.downloads.status()
    while time.time() < deadline:
        last = models.downloads.status()
        if pred(last):
            return last
        time.sleep(0.03)
    return last


def reset_model() -> None:
    for target in (MODELS / FAKE_NAME, MODELS / "hub"):
        shutil.rmtree(target, ignore_errors=True)
    models.downloads.cancel()
    deadline = time.time() + 5
    while models.downloads.status().get("state") in ("running", "verifying", "cancelling"):
        if time.time() > deadline:
            break
        time.sleep(0.05)


def make_valid(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for name, content in FAKE_FILES.items():
        (path / name).write_bytes(content)


# ---------------------------------------------------------------- 1. 路径 / 旧缓存

def test_paths() -> None:
    print("\n[1] 模型路径与旧缓存")
    reset_model()
    check("初始未安装", models.resolve_path(FAKE_NAME) is None)
    check("模型目录来自配置", str(models.models_dir()).lower() == str(MODELS).lower(),
          f" {models.models_dir()}")

    own = MODELS / FAKE_NAME
    make_valid(own)
    check("自有目录：能识别完整模型", models.is_installed(FAKE_NAME))
    check("自有目录：不是 legacy", models.is_legacy(FAKE_NAME) is False)
    check("installed_bytes > 0", models.installed_bytes(FAKE_NAME) > FAKE_SIZE)

    (own / "tokenizer.json").unlink()
    check("缺 tokenizer 不算安装", models.resolve_path(FAKE_NAME) is None)
    reset_model()

    legacy = (MODELS / "hub" / "models--test--faster-whisper-test"
              / "snapshots" / "abc123")
    make_valid(legacy)
    found = models.resolve_path(FAKE_NAME)
    check("旧 HF 缓存能直接识别", found == legacy, f" {found}")
    check("旧缓存标记 legacy", models.is_legacy(FAKE_NAME) is True)
    result = models.remove(FAKE_NAME)
    check("旧缓存不替用户删", result.get("ok") is False and "旧版" in result.get("reason", ""))
    reset_model()


# ---------------------------------------------------------------- 2. 推荐矩阵

def fake_profile(gpu=False, vram=0, cuda=False, cores=8, ram=16.0,
                 free=100.0, writable=True) -> dict:
    return {
        "models_dir": {"writable": writable, "free_gb": free, "path": str(MODELS)},
        "gpu": {"present": gpu, "name": "RTX 测试卡" if gpu else "", "vram_mb": vram},
        "cuda": {"available": cuda,
                 "reason": "" if cuda else ("CUDA 运行库不可用：缺 cublas" if gpu else "无 CUDA 设备")},
        "cpu": {"cores": cores},
        "ram_gb": ram,
    }


def test_recommend() -> None:
    print("\n[2] 硬件推荐矩阵")
    small = FAKE
    r = models.recommend(small, fake_profile())
    check("无 GPU：small 绿色", r["level"] == "green" and "推荐下载" in r["text"], f" {r}")
    check("磁盘不足：红色",
          models.recommend(small, fake_profile(free=0.001))["level"] == "red")
    check("目录不可写：红色",
          models.recommend(small, fake_profile(writable=False))["level"] == "red")

    # 用真实目录里的模型做矩阵断言（fake 模型是 CPU 类）
    large = next(m for m in REAL_CATALOG if m.name == "large-v3")
    turbo = next(m for m in REAL_CATALOG if m.name == "large-v3-turbo")
    medium = next(m for m in REAL_CATALOG if m.name == "medium")

    check("无 GPU：large-v3 红色", models.recommend(large, fake_profile())["level"] == "red")
    check("无 GPU：turbo 红色", models.recommend(turbo, fake_profile())["level"] == "red")
    check("无 GPU：medium 默认红",
          models.recommend(medium, fake_profile(cores=4, ram=8))["level"] == "red")
    check("无 GPU 但 8 核/16G：medium 黄色",
          models.recommend(medium, fake_profile(cores=8, ram=16))["level"] == "yellow")

    check("4GB：turbo 黄色",
          models.recommend(turbo, fake_profile(gpu=True, vram=4096, cuda=True))["level"] == "yellow")
    check("4GB：large-v3 红色",
          models.recommend(large, fake_profile(gpu=True, vram=4096, cuda=True))["level"] == "red")
    check("6GB：turbo 绿色",
          models.recommend(turbo, fake_profile(gpu=True, vram=6144, cuda=True))["level"] == "green")
    check("6GB：large-v3 黄色",
          models.recommend(large, fake_profile(gpu=True, vram=6144, cuda=True))["level"] == "yellow")
    check("8GB：large-v3 绿色",
          models.recommend(large, fake_profile(gpu=True, vram=8192, cuda=True))["level"] == "green")
    r = models.recommend(large, fake_profile(gpu=True, vram=12231, cuda=False))
    check("有卡但 CUDA 坏：large 红色且说明原因",
          r["level"] == "red" and "CUDA" in r["reason"], f" {r}")


# ---------------------------------------------------------------- 3. 下载源配置

def test_endpoints() -> None:
    print("\n[3] 下载源配置")
    os.environ.pop("BILI_NOTE_ASR_ENDPOINT", None)
    app_config.save({"models": {"endpoint": "official"}})
    check("official → huggingface.co", models.endpoint_url() == models.OFFICIAL_ENDPOINT)
    app_config.save({"models": {"endpoint": "mirror"}})
    check("mirror → hf-mirror.com", models.endpoint_url() == models.MIRROR_ENDPOINT)
    app_config.save({"models": {"endpoint": "custom", "custom_endpoint": "https://example.com/hf"}})
    check("custom → 自定义地址", models.endpoint_url() == "https://example.com/hf")
    app_config.save({"models": {"endpoint": "mirror"}})
    check("endpoint_label 可读", "镜像" in models.endpoint_label(), f" {models.endpoint_label()}")
    os.environ["BILI_NOTE_ASR_ENDPOINT"] = ENDPOINT


# ---------------------------------------------------------------- 4. 清单 / 下载 / 续传 / 校验

def test_manifest() -> None:
    print("\n[4] 清单")
    files = models.fetch_manifest(FAKE, ENDPOINT)
    names = {f["path"] for f in files}
    check("清单包含 model.bin", "model.bin" in names)
    check("清单包含 config/tokenizer", {"config.json", "tokenizer.json"} <= names)
    check("model.bin 带 sha256", any(f["path"] == "model.bin" and f["sha256"] == FAKE_SHA for f in files))
    check("清单总体积正确", sum(f["size"] for f in files) == sum(len(c) for c in FAKE_FILES.values()))
    MODE["no_model"] = True
    try:
        models.fetch_manifest(FAKE, ENDPOINT)
        check("清单缺 model.bin 会报错", False)
    except models.ModelDownloadError as e:
        check("清单缺 model.bin 会报错", "model.bin" in str(e))
    finally:
        MODE["no_model"] = False


def test_download_success() -> None:
    print("\n[5] 完整下载")
    reset_model()
    ok, msg = models.downloads.start(FAKE_NAME)
    check("启动下载", ok is True, f" {msg}")
    st = wait_job(lambda s: s["state"] in ("done", "error", "cancelled"))
    check("下载完成", st["state"] == "done", f" {st}")
    check("进度到 100%", st["percent"] == 100.0, f" {st['percent']}")
    check("文件落盘完整", models.is_installed(FAKE_NAME))
    path = models.resolve_path(FAKE_NAME)
    check("model.bin 大小正确", path and (path / "model.bin").stat().st_size == FAKE_SIZE)
    check("manifest.json 已写入", path is not None and (path / "manifest.json").is_file())
    ok2, msg2 = models.downloads.start(FAKE_NAME)
    check("已下载后再点下载会被拒绝", ok2 is False and "已经下载" in msg2, f" {msg2}")


def test_cancel_resume() -> None:
    print("\n[6] 取消与续传")
    reset_model()
    MODE["slow"] = True
    try:
        ok, _ = models.downloads.start(FAKE_NAME)
        check("慢速下载启动", ok)
        part = MODELS / FAKE_NAME / "model.bin.part"
        st = wait_job(
            lambda s: s["state"] == "running" and part.is_file() and part.stat().st_size > 0,
            timeout=10,
        )
        check("下载产生进度", st["state"] == "running" and part.is_file() and part.stat().st_size > 0,
              f" {st['state']} part={part.stat().st_size if part.is_file() else '-'}")
        ok = models.downloads.cancel()
        check("取消接口返回成功", ok)
        st = wait_job(lambda s: s["state"] == "cancelled", timeout=10)
        check("状态变为 cancelled", st["state"] == "cancelled", f" {st}")
        check("取消保留 .part（可续传）", part.is_file() and part.stat().st_size > 0,
              f" exists={part.is_file()} size={part.stat().st_size if part.is_file() else 0}")
    finally:
        MODE["slow"] = False

    files = models.fetch_manifest(FAKE, ENDPOINT)
    total = sum(f["size"] for f in files)
    ok, _ = models.downloads.start(FAKE_NAME)
    check("续传能再启动", ok)
    st = wait_job(lambda s: s["state"] in ("done", "error"), timeout=20)
    check("续传后完成", st["state"] == "done", f" {st}")
    check("续传进度没有从 0 重来", st["done"] == total, f" {st['done']} / {total}")
    check("续传后文件完整", models.is_installed(FAKE_NAME))


def test_checksum() -> None:
    print("\n[7] 校验失败")
    reset_model()
    MODE["corrupt"] = True
    try:
        ok, _ = models.downloads.start(FAKE_NAME)
        check("坏文件下载启动", ok)
        st = wait_job(lambda s: s["state"] in ("error", "done"), timeout=20)
        check("坏文件会失败", st["state"] == "error", f" {st}")
        check("错误说明是校验失败", "校验" in (st.get("error") or ""), f" {st.get('error')}")
        check("坏文件不会冒充已安装", not models.is_installed(FAKE_NAME))
    finally:
        MODE["corrupt"] = False
    reset_model()


def test_disk_space() -> None:
    print("\n[8] 磁盘空间")
    reset_model()
    original = app_paths.free_gb
    app_paths.free_gb = lambda path: 0.001  # noqa: ARG005 —— 2MB 模型需要约 0.0022GB
    try:
        ok, _ = models.downloads.start(FAKE_NAME)
        check("磁盘不足时仍能启动（错误在作业里）", ok)
        st = wait_job(lambda s: s["state"] in ("error", "done"), timeout=10)
        check("磁盘不足会失败", st["state"] == "error", f" {st}")
        check("错误说明磁盘空间不足", "磁盘空间不足" in (st.get("error") or ""), f" {st.get('error')}")
    finally:
        app_paths.free_gb = original
    reset_model()


# ---------------------------------------------------------------- 9. HTTP 接口

def test_api() -> None:
    print("\n[9] HTTP 接口")

    async def go():
        transport = httpx.ASGITransport(app=app_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/version")
            check("版本 3.0.0", r.status_code == 200 and r.json().get("version") == "3.0.0",
                  f" {r.text}")

            r = await client.get("/api/asr/status")
            data = r.json()
            check("状态接口 200", r.status_code == 200)
            check("状态里有机器信息", "machine" in data and "models_dir" in data)
            check("状态里列出假模型", [m["name"] for m in data["models"]] == [FAKE_NAME])
            check("默认设备是 auto", data["device"] == "auto")

            r = await client.post("/api/asr/settings", json={"dir": "relative/path"})
            check("相对模型目录被拒绝", r.status_code == 400, f" {r.text}")
            r = await client.post("/api/asr/settings", json={"endpoint": "bogus"})
            check("未知下载源被拒绝", r.status_code == 400)
            await client.post("/api/asr/settings", json={"endpoint": "mirror", "custom_endpoint": ""})
            r = await client.post("/api/asr/settings", json={"endpoint": "custom", "custom_endpoint": ""})
            check("自定义源缺地址被拒绝", r.status_code == 400, f" {r.text}")
            r = await client.post("/api/asr/settings", json={"model": "nope"})
            check("未知模型被拒绝", r.status_code == 400)
            r = await client.post("/api/asr/settings", json={"device": "tpu"})
            check("未知设备被拒绝", r.status_code == 400)

            new_dir = TMP / "another-models"
            r = await client.post("/api/asr/settings", json={"dir": str(new_dir), "onboarded": True})
            check("绝对模型目录能保存", r.status_code == 200, f" {r.text}")
            check("保存后状态用新目录",
                  str(new_dir).lower() == str(r.json()["models_dir"]["path"]).lower())
            check("onboarded 能保存", r.json()["onboarded"] is True)
            # 恢复
            await client.post("/api/asr/settings", json={"dir": "", "onboarded": False})

            r = await client.post("/api/asr/download", json={"model": "nope"})
            check("下载未知模型 400", r.status_code == 400)

            # 任务校验：缺模型时 URL 转写被拦；.txt 文稿不需要模型
            shutil.rmtree(MODELS / FAKE_NAME, ignore_errors=True)
            r = await client.post("/api/tasks", json={
                "urls": ["https://www.bilibili.com/video/BV1xx"],
                "download": True, "transcribe": True, "model": FAKE_NAME,
            })
            body = r.json()
            detail = body.get("detail") or {}
            check("缺模型时 URL 转写被拦 400", r.status_code == 400, f" {r.text}")
            check("返回结构化 code", isinstance(detail, dict) and detail.get("code") == "asr_model_missing")

            txt = TMP / "manuscript.txt"
            txt.write_text("[00:00:00] 你好\n[00:00:01] 世界\n[00:00:02] 测试\n", encoding="utf-8")
            r = await client.post("/api/tasks", json={
                "files": [str(txt)], "download": False, "transcribe": True, "model": FAKE_NAME,
            })
            check(".txt 文稿不需要模型", r.status_code == 200, f" {r.text}")
            app_main.manager.tasks.clear()

            r = await client.post("/api/tasks", json={
                "urls": ["https://www.bilibili.com/video/BV1xx"],
                "download": True, "transcribe": False, "model": FAKE_NAME,
            })
            check("仅下载视频不需要模型", r.status_code == 200, f" {r.text}")
            app_main.manager.tasks.clear()

            make_valid(MODELS / FAKE_NAME)
            r = await client.delete(f"/api/asr/models/{FAKE_NAME}")
            check("删除模型成功", r.status_code == 200, f" {r.text}")
            check("删除后不再安装", not models.is_installed(FAKE_NAME))

            r = await client.get("/api/asr/diagnostics")
            check("诊断信息是纯文本", r.status_code == 200 and "bili-note" in r.text)

    asyncio.run(go())


# ---------------------------------------------------------------- 10. 规则 / 配置

def test_rules_and_config() -> None:
    print("\n[10] 提交规则与配置白名单")
    check("URL + 转写 → 需要模型", tasks.requires_model(["https://x"], [], True))
    check("URL + 只下载 → 不需要模型", not tasks.requires_model(["https://x"], [], False))
    check("本地媒体 → 需要模型", tasks.requires_model([], ["D:\\a.mp4"], True))
    check("本地 .txt → 不需要模型", not tasks.requires_model([], ["D:\\a.txt"], True))
    check("空输入 → 不需要模型", not tasks.requires_model([], [], True))

    app_config.save({"llm": {"api_key": "sk-test-1234567890"}})
    app_config.save({"llm": {"api_key": ""}})
    check("api_key 留空不会清掉已存的 Key",
          app_config.load()["llm"]["api_key"] == "sk-test-1234567890")
    app_config.save({"models": {"dir": ""}})
    check("models.dir 空串 = 恢复默认", app_config.load()["models"]["dir"] == "")
    app_config.save({"models": {"proxy": "http://127.0.0.1:7890"}})
    app_config.save({"models": {"proxy": ""}})
    check("proxy 空串 = 清除", app_config.load()["models"]["proxy"] == "")
    app_config.save({"transcribe": {"model": FAKE_NAME, "device": "cpu", "cpu_threads": 4}})
    cfg = app_config.load()
    check("transcribe 新字段可写", cfg["transcribe"]["device"] == "cpu" and cfg["transcribe"]["cpu_threads"] == 4)
    check("未知字段被白名单挡住",
          "unknown" not in (app_config.save({"transcribe": {"unknown": 1}}).get("transcribe") or {}))


# ---------------------------------------------------------------- main

def main() -> int:
    print("bili-note V3 模型管理自检（离线假源）")
    print(f"临时数据目录：{TMP}")
    print(f"假源：{ENDPOINT}")
    app_config.save({
        "transcribe": {"model": FAKE_NAME, "device": "auto"},
        "models": {"dir": str(MODELS), "endpoint": "mirror"},
        "ui": {"onboarded": False},
    })
    try:
        test_paths()
        test_recommend()
        test_endpoints()
        test_manifest()
        test_download_success()
        test_cancel_resume()
        test_checksum()
        test_disk_space()
        test_api()
        test_rules_and_config()
    except Exception:  # noqa: BLE001
        print("\n自检自身抛错：")
        traceback.print_exc()
        return 2
    finally:
        SERVER.shutdown()
        SERVER.server_close()
        models.CATALOG = REAL_CATALOG
        models.BY_NAME = {m.name: m for m in REAL_CATALOG}
        try:
            shutil.rmtree(TMP, ignore_errors=True)
        except OSError:
            pass

    print(f"\n结果：{ok_count} 通过 / {fail_count} 失败")
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
