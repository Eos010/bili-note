"""转写模型中心：目录清单、路径解析、旧缓存兼容、下载与删除。

V3 的核心约束（都是为了让「模型下到哪」不再靠猜）：

1. **程序自己拥有目录布局**：`<models_dir>/<模型名>/model.bin` 等。
   推理时把**绝对路径**交给 `WhisperModel`，不再让 huggingface_hub 决定缓存位置。
2. **不静默下载**：只有用户点「下载」才联网；转写前模型不存在就报明确的
   `ModelNotReady`，不会在请求线程里拉几个 G。
3. **旧缓存只读兼容**：v2.x 的 `models/hub/models--.../snapshots/...` 直接识别、
   不移动、不删除——作者本机那 4.39GB 不能白下。
4. **可续传、可取消、可校验**：下载写 `.part`，取消保留，继续时用 HTTP Range；
   LFS 文件按清单里的 sha256 校验。

下载清单来自 HF 的 tree API：
    GET {endpoint}/api/models/{repo}/tree/main?recursive=true
文件：
    GET {endpoint}/{repo}/resolve/main/{path}
默认走 `hf-mirror.com`（本机实测官方直连超时），可切官方/自定义。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

import httpx

from . import app_paths, config as app_config, hardware
from .errors import ModelNotReady

MIRROR_ENDPOINT = "https://hf-mirror.com"
OFFICIAL_ENDPOINT = "https://huggingface.co"

# 只下载推理需要的文件；README/演示文件不要。
ALLOW_PATTERNS = (
    "model.bin",
    "config.json",
    "preprocessor_config.json",
    "tokenizer.json",
    "vocabulary.txt",
    "vocabulary.json",
)

_BYTES_PER_MB = 1024 * 1024
_BYTES_PER_GB = 1024 ** 3


@dataclass(frozen=True)
class AsrModel:
    name: str
    repo: str
    size: int          # model.bin 的精确字节数（用于判断"下完了没有"）
    label: str
    desc: str
    need_vram_mb: int  # 绿色推荐所需显存；0 = CPU 也能推荐
    kind: str          # cpu / gpu / any


# 体积是 2026-10-18 从 hf-mirror 的 tree API 实测的，不是估算。
CATALOG: tuple[AsrModel, ...] = (
    AsrModel("tiny", "Systran/faster-whisper-tiny", 75_538_270,
             "tiny", "极快 · 质量低 · 低配兜底", 0, "cpu"),
    AsrModel("base", "Systran/faster-whisper-base", 145_217_532,
             "base", "快 · 质量一般 · 低配备选", 0, "cpu"),
    AsrModel("small", "Systran/faster-whisper-small", 483_546_902,
             "small", "CPU 默认 · 质量和速度平衡", 0, "cpu"),
    AsrModel("medium", "Systran/faster-whisper-medium", 1_527_906_378,
             "medium", "质量较好 · CPU 偏慢", 4096, "any"),
    AsrModel("large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo", 1_617_884_929,
             "large-v3-turbo", "GPU 推荐 · 质量约 99% · 更快", 6144, "gpu"),
    AsrModel("large-v3", "Systran/faster-whisper-large-v3", 3_087_284_237,
             "large-v3", "质量最高 · GPU 推荐", 8192, "gpu"),
)
BY_NAME: dict[str, AsrModel] = {m.name: m for m in CATALOG}
DEFAULT_MODEL = "small"


class ModelDownloadError(Exception):
    """下载失败（网络 / 清单 / 校验 / 磁盘）。消息是给用户看的中文。"""


class DownloadCancelled(Exception):
    """用户取消了下载。"""


# ---------------------------------------------------------------- 目录与路径

def models_dir() -> Path:
    return app_paths.models_dir()


def default_models_dir() -> Path:
    return app_paths.default_models_dir()


def model_dir(name: str) -> Path:
    return models_dir() / name


def _repo_dir_name(repo: str) -> str:
    return "models--" + repo.replace("/", "--")


def _valid_model_dir(path: Path, size: int) -> bool:
    """一个目录算不算「完整可用的模型」。

    只用文件事实判断，不加载模型（加载要占显存/内存，状态页不能这么干）。
    """
    try:
        if not path.is_dir():
            return False
        model_bin = path / "model.bin"
        if not model_bin.is_file() or model_bin.stat().st_size != size:
            return False
        return (path / "config.json").is_file() and (path / "tokenizer.json").is_file()
    except OSError:
        return False


def legacy_dir(name: str) -> Optional[Path]:
    """v2.x 的 HuggingFace 缓存布局，只读识别。"""
    model = BY_NAME.get(name)
    if model is None:
        return None
    repo_dir = _repo_dir_name(model.repo)
    candidates: list[Path] = []
    for base in (models_dir(), models_dir() / "hub"):
        snapshots = base / repo_dir / "snapshots"
        if not snapshots.is_dir():
            continue
        try:
            for snap in snapshots.iterdir():
                if _valid_model_dir(snap, model.size):
                    candidates.append(snap)
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def resolve_path(name: str) -> Optional[Path]:
    """返回可直接交给 WhisperModel 的绝对目录；不存在/不完整返回 None。"""
    model = BY_NAME.get(name)
    if model is None:
        return None
    own = model_dir(name)
    if _valid_model_dir(own, model.size):
        return own
    legacy = legacy_dir(name)
    if legacy is not None:
        return legacy
    return None


def is_installed(name: str) -> bool:
    return resolve_path(name) is not None


def is_legacy(name: str) -> bool:
    own = model_dir(name)
    model = BY_NAME.get(name)
    return model is not None and not _valid_model_dir(own, model.size) and legacy_dir(name) is not None


def installed_bytes(name: str) -> int:
    path = resolve_path(name)
    if path is None:
        return 0
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file() and not p.name.endswith(".part"):
                total += p.stat().st_size
    except OSError:
        pass
    return total


# ---------------------------------------------------------------- 配置读取

def endpoint_url() -> str:
    env = os.environ.get("BILI_NOTE_ASR_ENDPOINT")
    if env:
        return env.rstrip("/")
    cfg = app_config.load().get("models") or {}
    choice = (cfg.get("endpoint") or "mirror").lower()
    if choice == "official":
        return OFFICIAL_ENDPOINT
    if choice == "custom":
        return (cfg.get("custom_endpoint") or MIRROR_ENDPOINT).rstrip("/")
    return MIRROR_ENDPOINT


def proxy_url() -> str:
    env = os.environ.get("BILI_NOTE_ASR_PROXY")
    if env:
        return env.strip()
    cfg = app_config.load().get("models") or {}
    return str(cfg.get("proxy") or "").strip()


def endpoint_label(url: str | None = None) -> str:
    url = url or endpoint_url()
    if url == MIRROR_ENDPOINT:
        return "国内镜像 hf-mirror.com"
    if url == OFFICIAL_ENDPOINT:
        return "HuggingFace 官方"
    return url


def _client(proxy: str | None = None) -> httpx.Client:
    kwargs = {
        "follow_redirects": True,
        "timeout": httpx.Timeout(60.0, connect=15.0, read=90.0, write=90.0),
    }
    if proxy:
        kwargs["proxy"] = proxy
        kwargs["trust_env"] = False
    try:
        return httpx.Client(**kwargs)
    except TypeError:  # httpx < 0.28 的旧参数名
        if proxy:
            kwargs.pop("proxy", None)
            kwargs["proxies"] = proxy
        return httpx.Client(**kwargs)


# ---------------------------------------------------------------- 推荐

def recommend(model: AsrModel, prof: dict | None = None) -> dict:
    """按硬件给「绿 / 黄 / 红」建议。

    绿色 = 推荐下载；黄色 = 可以跑但偏慢；红色 = 配置不足，不推荐。
    红色仍允许用户强行下载（前端二次确认），这里只负责给判断和理由。
    """
    p = prof or hardware.profile()
    md = p.get("models_dir") or {}
    gpu = p.get("gpu") or {}
    cuda = p.get("cuda") or {}
    vram = int(gpu.get("vram_mb") or 0)
    cores = int((p.get("cpu") or {}).get("cores") or 0)
    ram = float(p.get("ram_gb") or 0)
    free_gb = float(md.get("free_gb") or 0)
    need_gb = model.size / _BYTES_PER_GB * 1.15

    def result(level: str, text: str, reason: str) -> dict:
        return {"level": level, "text": text, "reason": reason}

    if not md.get("writable", True):
        return result("red", "配置不足，不推荐",
                      f"模型目录不可写：{md.get('path') or models_dir()}")
    if free_gb and free_gb < need_gb:
        return result("red", "配置不足，不推荐",
                      f"磁盘只剩 {free_gb:.1f}GB，这个模型至少需要 {need_gb:.1f}GB")

    cuda_ok = bool(cuda.get("available"))
    if not cuda_ok:
        gpu_note = ""
        if gpu.get("present"):
            gpu_note = f"检测到 {gpu.get('name') or 'NVIDIA 显卡'}，但 CUDA 不可用（{cuda.get('reason') or '原因未知'}）；"
        elif cuda.get("reason"):
            gpu_note = f"{cuda['reason']}；"

    if model.kind == "cpu" or model.need_vram_mb <= 0:
        if cuda_ok:
            return result("green", "推荐下载", f"GPU 可用（{gpu.get('name') or 'NVIDIA'} {vram}MB），CPU 也能跑")
        return result("green", "推荐下载", f"CPU 也能跑（{cores} 核 / {ram:.0f}GB 内存）")

    if not cuda_ok:
        if model.name == "medium" and cores >= 8 and ram >= 16:
            return result("yellow", "可以运行，但偏慢", gpu_note + "大模型在 CPU 上会很慢")
        return result("red", "配置不足，不推荐", gpu_note + "没有可用的 GPU 加速")

    if model.name == "medium":
        if vram >= 4096:
            return result("green", "推荐下载", f"显存 {vram}MB，够跑")
        if vram >= 2048:
            return result("yellow", "可以运行，但偏慢", f"显存 {vram}MB 偏紧，可能频繁换出")
        return result("red", "配置不足，不推荐", f"显存 {vram}MB 不足")

    if model.name == "large-v3-turbo":
        if vram >= 6144:
            return result("green", "推荐下载", f"显存 {vram}MB，够跑")
        if vram >= 4096:
            return result("yellow", "可以运行，但偏慢", f"显存 {vram}MB 偏紧，长视频可能 OOM")
        return result("red", "配置不足，不推荐", f"显存 {vram}MB 不足")

    if model.name == "large-v3":
        if vram >= 8192:
            return result("green", "推荐下载", f"显存 {vram}MB，够跑")
        if vram >= 6144:
            return result("yellow", "可以运行，但偏慢", f"显存 {vram}MB 偏紧，长视频可能 OOM")
        return result("red", "配置不足，不推荐", f"显存 {vram}MB 不足（建议 8GB 以上）")

    return result("green", "推荐下载", "配置满足")


def status_rows(prof: dict | None = None) -> list[dict]:
    p = prof or hardware.profile()
    rows = []
    for model in CATALOG:
        path = resolve_path(model.name)
        rec = recommend(model, p)
        rows.append({
            "name": model.name,
            "label": model.label,
            "desc": model.desc,
            "size": model.size,
            "size_gb": round(model.size / _BYTES_PER_GB, 2),
            "repo": model.repo,
            "need_vram_mb": model.need_vram_mb,
            "kind": model.kind,
            "downloaded": path is not None,
            "legacy": bool(path) and is_legacy(model.name),
            "path": str(path) if path else "",
            "downloaded_bytes": installed_bytes(model.name) if path else 0,
            "recommend": rec,
        })
    return rows


def current_model() -> str:
    name = (app_config.load().get("transcribe") or {}).get("model") or DEFAULT_MODEL
    return name if name in BY_NAME else DEFAULT_MODEL


# ---------------------------------------------------------------- 清单与下载

def _manifest_url(repo: str, endpoint: str) -> str:
    return f"{endpoint}/api/models/{repo}/tree/main?recursive=true"


def fetch_manifest(model: AsrModel, endpoint: str, proxy: str | None = None) -> list[dict]:
    url = _manifest_url(model.repo, endpoint)
    try:
        with _client(proxy) as client:
            resp = client.get(url)
    except httpx.HTTPError as e:
        raise ModelDownloadError(f"连不上 {endpoint}：{e}") from e
    if resp.status_code != 200:
        snippet = (resp.text or "")[:200].replace("\n", " ")
        raise ModelDownloadError(f"获取清单失败（HTTP {resp.status_code}）：{snippet}")
    try:
        data = resp.json()
    except ValueError as e:
        raise ModelDownloadError(f"清单不是合法 JSON：{e}") from e
    if not isinstance(data, list):
        raise ModelDownloadError("清单格式不对（不是文件列表）")
    files = []
    for item in data:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if path not in ALLOW_PATTERNS:
            continue
        lfs = item.get("lfs") or {}
        size = item.get("size") or lfs.get("size") or 0
        sha = lfs.get("oid") or ""
        files.append({"path": path, "size": int(size), "sha256": str(sha)})
    if not any(f["path"] == "model.bin" for f in files):
        raise ModelDownloadError("清单里没有 model.bin，源可能不对")
    return files


def _manifest_with_fallback(model: AsrModel, proxy: str | None) -> tuple[list[dict], str]:
    primary = endpoint_url()
    endpoints = [primary]
    if primary != OFFICIAL_ENDPOINT:
        endpoints.append(OFFICIAL_ENDPOINT)
    errors = []
    for ep in endpoints:
        try:
            return fetch_manifest(model, ep, proxy), ep
        except ModelDownloadError as e:
            errors.append(f"{ep}：{e}")
    raise ModelDownloadError(
        "拿不到模型文件清单。\n" + "\n".join(errors)
        + "\n→ 检查网络；或在设置里切换下载源（国内镜像 / 官方）/ 填代理。"
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class DownloadJob:
    model: str = ""
    state: str = "idle"          # idle/running/verifying/done/error/cancelled
    total: int = 0
    done: int = 0
    speed: float = 0.0
    error: str = ""
    endpoint: str = ""
    started: float = 0.0
    updated: float = 0.0
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_t: float = field(default=0.0, repr=False)
    _last_done: int = field(default=0, repr=False)

    def snapshot(self) -> dict:
        with self.lock:
            pct = (self.done / self.total * 100) if self.total else 0.0
            return {
                "model": self.model,
                "state": self.state,
                "total": self.total,
                "done": self.done,
                "percent": round(max(0.0, min(100.0, pct)), 1),
                "speed": round(self.speed, 1),
                "error": self.error,
                "endpoint": self.endpoint,
                "endpoint_label": endpoint_label(self.endpoint) if self.endpoint else "",
                "started": self.started,
                "updated": self.updated,
            }

    def set(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)
            self.updated = time.time()

    def bump(self, n: int) -> None:
        with self.lock:
            self.done += n
            now = time.time()
            if self._last_t <= 0:
                self._last_t, self._last_done = now, self.done
                return
            dt = now - self._last_t
            if dt >= 0.3:
                self.speed = (self.done - self._last_done) / dt
                self._last_t, self._last_done = now, self.done
            self.updated = now


def _download_file(
    client: httpx.Client,
    url: str,
    dest: Path,
    size: int,
    sha256: str,
    job: DownloadJob,
) -> None:
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)

    existing = part.stat().st_size if part.is_file() else 0
    if existing > size:
        part.unlink(missing_ok=True)
        existing = 0
    if existing == size and size > 0:
        # 已经下完但没改名（上次进程在改名附近被杀）
        if not sha256 or _sha256(part) == sha256:
            os.replace(part, dest)
            return

    last_error: Exception | None = None
    for attempt in range(4):
        if job.cancel.is_set():
            raise DownloadCancelled()
        headers = {"Range": f"bytes={existing}-"} if existing else {}
        try:
            with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 416 and existing >= size:
                    break
                if resp.status_code not in (200, 206):
                    body = b""
                    try:
                        body = resp.read()[:200]
                    except Exception:  # noqa: BLE001
                        pass
                    raise ModelDownloadError(
                        f"下载 {dest.name} 失败（HTTP {resp.status_code}）："
                        f"{body.decode('utf-8', 'ignore')[:160]}"
                    )
                if existing and resp.status_code == 200:
                    existing = 0  # 服务端不支持 Range，只能从头来
                mode = "ab" if existing else "wb"
                with open(part, mode) as f:
                    # 256KB 一块：取消能更快生效，进度条也更顺；1MB 时小文件几乎没反馈。
                    for chunk in resp.iter_bytes(256 * 1024):
                        if job.cancel.is_set():
                            raise DownloadCancelled()
                        f.write(chunk)
                        job.bump(len(chunk))
            last_error = None
            break
        except DownloadCancelled:
            raise
        except (httpx.HTTPError, OSError) as e:
            last_error = e
            try:
                existing = part.stat().st_size
            except OSError:
                existing = 0
            if attempt >= 3:
                break
            time.sleep(1.2 * (attempt + 1))

    if last_error is not None:
        raise ModelDownloadError(f"下载 {dest.name} 失败：{last_error}")
    if not part.is_file():
        raise ModelDownloadError(f"下载 {dest.name} 失败：没有生成文件")
    actual = part.stat().st_size
    if actual != size:
        raise ModelDownloadError(f"{dest.name} 大小不对（{actual} / 期望 {size}），可能是源的问题")
    if sha256:
        job.set(state="verifying")
        got = _sha256(part)
        if got != sha256:
            part.unlink(missing_ok=True)
            raise ModelDownloadError(f"{dest.name} 校验失败（sha256 不符），文件已删除，请重试")
        job.set(state="running")
    if dest.exists():
        dest.unlink()
    os.replace(part, dest)


class DownloadManager:
    """同一时间只允许一个下载作业。状态放在内存里，进程重启就回到 idle（.part 还在）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: DownloadJob | None = None
        self._thread: threading.Thread | None = None

    def status(self) -> dict:
        with self._lock:
            job = self._job
        return job.snapshot() if job else {
            "model": "", "state": "idle", "total": 0, "done": 0, "percent": 0.0,
            "speed": 0.0, "error": "", "endpoint": "", "endpoint_label": "",
            "started": 0.0, "updated": 0.0,
        }

    def start(self, name: str) -> tuple[bool, str]:
        model = BY_NAME.get(name)
        if model is None:
            return False, "没有这个转写模型"
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False, "已经有一个模型在下载，等它结束或先取消"
            if is_installed(name):
                return False, "这个模型已经下载好了"
            job = DownloadJob(
                model=name, state="running", started=time.time(), updated=time.time(),
                endpoint=endpoint_url(),
            )
            self._job = job
            thread = threading.Thread(target=self._run, args=(model, job), daemon=True,
                                      name=f"asr-download-{name}")
            self._thread = thread
            thread.start()
        return True, ""

    def cancel(self) -> bool:
        with self._lock:
            job = self._job
        if job is None or job.state not in ("running", "verifying"):
            return False
        job.cancel.set()
        job.set(state="cancelling")
        return True

    def _run(self, model: AsrModel, job: DownloadJob) -> None:
        try:
            self._download(model, job)
            job.set(state="done", speed=0.0, updated=time.time())
        except DownloadCancelled:
            job.set(state="cancelled", speed=0.0, updated=time.time())
        except Exception as e:  # noqa: BLE001 —— 下载作业不能把服务带崩
            job.set(state="error", error=str(e), speed=0.0, updated=time.time())

    def _download(self, model: AsrModel, job: DownloadJob) -> None:
        target = model_dir(model.name)
        app_paths.ensure_dir(target)
        proxy = proxy_url()
        files, endpoint = _manifest_with_fallback(model, proxy)
        job.set(endpoint=endpoint, total=sum(f["size"] for f in files))

        free = app_paths.free_gb(target)
        need = job.total / _BYTES_PER_GB * 1.15
        if free and free < need:
            raise ModelDownloadError(
                f"磁盘空间不足：{target} 所在盘只剩 {free:.1f}GB，至少需要 {need:.1f}GB"
            )

        # 统计已下载的部分（续传时进度条不能从 0 开始）
        done = 0
        for f in files:
            dest = target / f["path"]
            if dest.is_file() and dest.stat().st_size == f["size"]:
                done += f["size"]
                continue
            part = dest.with_name(dest.name + ".part")
            if part.is_file():
                size = part.stat().st_size
                if size <= f["size"]:
                    done += size
                else:
                    part.unlink(missing_ok=True)
        job.set(done=done, _last_done=done, _last_t=time.time())

        with _client(proxy) as client:
            for f in files:
                if job.cancel.is_set():
                    raise DownloadCancelled()
                dest = target / f["path"]
                if dest.is_file() and dest.stat().st_size == f["size"]:
                    continue
                url = f"{endpoint}/{model.repo}/resolve/main/{quote(f['path'], safe='/')}"
                _download_file(client, url, dest, f["size"], f["sha256"], job)

        manifest = {
            "name": model.name,
            "repo": model.repo,
            "endpoint": endpoint,
            "downloaded_at": time.time(),
            "files": [{"path": f["path"], "size": f["size"], "sha256": f["sha256"]} for f in files],
        }
        (target / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )


downloads = DownloadManager()


def remove(name: str) -> dict:
    """删除**我们自己目录里**的模型；旧 HF 缓存不在这里删（那是用户的历史资产）。"""
    if name not in BY_NAME:
        return {"ok": False, "reason": "没有这个转写模型"}
    with downloads._lock:  # noqa: SLF001 —— 同一个模块，复用它的作业锁
        job = downloads._job
        if job is not None and job.model == name and job.state in ("running", "verifying", "cancelling"):
            return {"ok": False, "reason": "这个模型正在下载，先取消再删"}
    target = model_dir(name)
    if target.is_dir():
        shutil.rmtree(target)
        return {"ok": True, "removed": str(target)}
    legacy = legacy_dir(name)
    if legacy is not None:
        return {
            "ok": False,
            "reason": "这是旧版 HuggingFace 缓存，程序不替你删；要清理请到文件管理器打开：",
            "path": str(legacy.parent.parent),
        }
    return {"ok": False, "reason": "没有找到这个模型的目录"}


def download_sync(name: str, on_progress: Callable[[dict], None] | None = None) -> dict:
    """给 CLI（download_model.py）用的同步下载。"""
    model = BY_NAME.get(name)
    if model is None:
        raise ModelDownloadError(f"没有这个模型：{name}")
    if is_installed(name):
        return {"ok": True, "message": "已经下载过了", "path": str(resolve_path(name))}
    job = DownloadJob(model=name, state="running", started=time.time(), updated=time.time(),
                      endpoint=endpoint_url())
    stop = threading.Event()

    def report() -> None:
        last = -1.0
        while not stop.wait(1.0):
            snap = job.snapshot()
            if on_progress and (snap["percent"] - last >= 5 or snap["state"] != "running"):
                last = snap["percent"]
                on_progress(snap)

    reporter = threading.Thread(target=report, daemon=True) if on_progress else None
    if reporter:
        reporter.start()
    try:
        with _client(proxy_url()) as client:
            target = model_dir(name)
            app_paths.ensure_dir(target)
            files, endpoint = _manifest_with_fallback(model, proxy_url())
            job.set(endpoint=endpoint, total=sum(f["size"] for f in files))
            done = 0
            for f in files:
                dest = target / f["path"]
                if dest.is_file() and dest.stat().st_size == f["size"]:
                    done += f["size"]
            job.set(done=done, _last_done=done, _last_t=time.time())
            for f in files:
                dest = target / f["path"]
                if dest.is_file() and dest.stat().st_size == f["size"]:
                    continue
                url = f"{endpoint}/{model.repo}/resolve/main/{quote(f['path'], safe='/')}"
                _download_file(client, url, dest, f["size"], f["sha256"], job)
        (target / "manifest.json").write_text(
            json.dumps({"name": name, "repo": model.repo, "endpoint": endpoint,
                        "downloaded_at": time.time(),
                        "files": [{"path": f["path"], "size": f["size"], "sha256": f["sha256"]}
                                  for f in files]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    finally:
        stop.set()
        if reporter:
            reporter.join(timeout=2)
    if on_progress:
        on_progress(job.snapshot())
    return {"ok": True, "path": str(target)}


def require(name: str) -> Path:
    """取模型路径；没有就给一句能照做的错误。"""
    path = resolve_path(name)
    if path is not None:
        return path
    model = BY_NAME.get(name)
    if model is None:
        raise ModelNotReady(f"没有这个转写模型：{name}")
    label = model.label
    if name == "small":
        hint = "→ 也可以先用 tiny（约 0.08GB）应急，或到「设置 → 转写模型」下载小模型。"
    else:
        hint = "→ 想先用小模型，可以换成 small（约 0.46GB，CPU 也能跑）。"
    raise ModelNotReady(
        f"转写模型 {label} 还没下载。\n"
        f"→ 打开右上角「设置 → 转写模型」下载它（约 {model.size / _BYTES_PER_GB:.1f}GB）。\n"
        f"{hint}\n"
        f"→ 模型目录：{models_dir()}"
    )
