"""启动入口：选端口 → 起 FastAPI 服务 → 自动打开浏览器。

V3 的四件事：
1. 端口不再写死：8001 被别的程序占了就自动往后找，不会打开一个错误页面；
2. 单实例：8001 上已经是 bili-note 就只开浏览器，不再起第二个服务；
3. `--probe-cuda`：给硬件检测的子进程用（冻结态也能跑）；
4. instance.json：`stop.bat` 靠它找到 PID，不再靠端口猜。
"""
from __future__ import annotations

import atexit
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

HOST = "127.0.0.1"
DEFAULT_PORT = 8001
PORT_SPAN = 10


def _candidate_ports() -> list[int]:
    env = os.environ.get("BILI_NOTE_PORT")
    if env:
        try:
            return [int(env)]
        except ValueError:
            pass
    return list(range(DEFAULT_PORT, DEFAULT_PORT + PORT_SPAN))


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def _is_bili_note(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/version", timeout=0.8) as resp:
            data = json.loads(resp.read(4096).decode("utf-8", "ignore"))
        return data.get("name") == "bili-note"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _find_port() -> tuple[int, bool]:
    """返回 (端口, 是否已有本程序在跑)。"""
    for port in _candidate_ports():
        if _is_bili_note(port):
            return port, True
        if _port_free(port):
            return port, False
    # 全部被占：交给系统分配一个，至少能启动
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1], False


def _open_browser(url: str) -> None:
    time.sleep(1.2)
    webbrowser.open(url)


def _probe_cuda() -> int:
    from app.probe import probe_cuda

    print(json.dumps(probe_cuda(), ensure_ascii=False))
    return 0


def _write_instance(port: int) -> str:
    from app import app_paths

    app_paths.ensure_dir(app_paths.DATA_DIR)
    data = {
        "pid": os.getpid(),
        "port": port,
        "url": f"http://{HOST}:{port}",
        "started": time.time(),
        "version": _version(),
    }
    app_paths.INSTANCE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return str(app_paths.INSTANCE_PATH)


def _remove_instance() -> None:
    from app import app_paths

    try:
        data = json.loads(app_paths.INSTANCE_PATH.read_text(encoding="utf-8"))
        if int(data.get("pid") or 0) == os.getpid():
            app_paths.INSTANCE_PATH.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def _version() -> str:
    try:
        from app import __version__

        return __version__
    except Exception:  # noqa: BLE001
        return ""


def _check_data_dir(path) -> tuple[bool, str]:
    """确认数据目录可写。

    Windows 上刚创建的文件可能被 Defender/索引短暂占用，一次写失败就退出会让用户
    看到一个假故障。这里写 5 次（每次换文件名），只要有一次成功就认为可写。
    """
    last_error = ""
    for attempt in range(5):
        probe = path / f".bili-note-write-test-{os.getpid()}-{attempt}"
        try:
            probe.write_text("ok", encoding="utf-8")
        except OSError as e:
            last_error = str(e)
            time.sleep(0.2)
            continue
        # 写成功就够了；删除失败不算不可写（前面项目档里踩过这个坑）
        for _ in range(3):
            try:
                probe.unlink()
                break
            except OSError:
                time.sleep(0.1)
        return True, ""
    return False, last_error


def main() -> int:
    if "--probe-cuda" in sys.argv:
        return _probe_cuda()

    from app import app_paths, cuda_bootstrap

    cuda_bootstrap.apply()

    # 数据目录必须可写：便携版被解压到 Program Files 时，要给出能照做的错误。
    try:
        app_paths.ensure_dir(app_paths.DATA_DIR)
    except OSError as e:
        print(f"[错误] 数据目录不可用：{app_paths.DATA_DIR}\n       {e}")
        print("→ 请把 bili-note 解压/移动到可写目录（例如 D:\\bili-note），不要放在 Program Files 下。")
        return 2
    writable, why = _check_data_dir(app_paths.DATA_DIR)
    if not writable:
        print(f"[错误] 数据目录不可写：{app_paths.DATA_DIR}\n       {why}")
        print("→ 请把 bili-note 解压/移动到可写目录（例如 D:\\bili-note），不要放在 Program Files 下。")
        return 2

    port, already = _find_port()
    url = f"http://{HOST}:{port}"
    no_browser = os.environ.get("BILI_NOTE_NO_BROWSER") == "1" or "--no-browser" in sys.argv

    if already:
        print(f"bili-note 已经在运行 → {url}")
        if not no_browser:
            webbrowser.open(url)
        return 0

    instance = _write_instance(port)
    atexit.register(_remove_instance)

    print("=" * 58)
    print(f" bili-note V{_version() or '?'}  →  {url}")
    print(f" 数据目录：{app_paths.DATA_DIR}")
    print(f" 模型目录：{app_paths.models_dir()}")
    print(" 关闭这个窗口或按 Ctrl+C 停止服务。")
    print("=" * 58, flush=True)

    if not no_browser:
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()

    try:
        import uvicorn

        from app.main import app as fastapi_app

        uvicorn.run(fastapi_app, host=HOST, port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"\n[错误] 启动失败：{type(e).__name__}: {e}")
        print(f"日志/实例文件：{instance}")
        if getattr(sys, "frozen", False) and sys.stdin and sys.stdin.isatty():
            input("按回车退出…")
        return 1
    finally:
        _remove_instance()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
