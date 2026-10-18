"""应用根目录 / 数据目录的**唯一解析处**。

V3 之前，路径散在三处：
- `config.py` 自己算 `BASE_DIR`；
- `tasks.py` 自己算 `OUTPUT_DIR`；
- `start.bat` 用 `HF_HOME` 把模型缓存指到项目 `models/`（而 `run.py` 不知道这件事）。

结果就是用户直接 `python run.py` 时模型下到 `C:\\Users\\...\\cache\\huggingface`，
程序再按 `models/` 去找就找不到。这里把「程序放哪、数据写哪、模型放哪」收成一组函数，
所有模块只从这里取，不再各自拼路径。

两种运行形态：
- 源码：根目录 = 仓库根（`app/` 的上一级）；
- 冻结（PyInstaller onedir）：根目录 = `bili-note.exe` 所在目录，数据也写在这里（便携）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否是 PyInstaller 打出来的可执行文件。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """程序根目录：冻结态是可执行文件所在目录，源码态是仓库根。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """可写数据目录：`config.json` / `output/` / `instance.json` 都放这里。

    `BILI_NOTE_DATA_DIR` 只用于测试和高级用户；默认就是程序根目录。
    """
    env = os.environ.get("BILI_NOTE_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return app_root()


def _static_dir() -> Path:
    if is_frozen():
        # onedir 模式下静态资源被 PyInstaller 放到 `_internal/app/static`
        return Path(getattr(sys, "_MEIPASS", app_root())) / "app" / "static"
    return Path(__file__).resolve().parent / "static"


def _tools_dir() -> Path:
    # 冻结态下随包工具在 `_internal/tools`（即 sys._MEIPASS/tools），不是 exe 同级。
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_root())) / "tools"
    return app_root() / "tools"


def default_models_dir() -> Path:
    """没有配置 `models.dir` 时的默认模型目录。"""
    return data_dir() / "models"


def models_dir() -> Path:
    """当前生效的模型目录（环境变量 > 配置 > 默认）。

    只接受绝对路径；相对路径按数据目录解析（配置接口会拒绝相对路径，这里只兜底）。
    """
    env = os.environ.get("BILI_NOTE_MODELS_DIR")
    if env:
        return Path(os.path.expandvars(env)).expanduser().resolve()
    raw = ""
    try:
        from . import config as app_config  # 延迟导入，避免配置模块在导入期反向依赖
        raw = (app_config.load().get("models") or {}).get("dir") or ""
    except Exception:  # noqa: BLE001 —— 配置坏了也要能启动，回退默认目录
        raw = ""
    raw = os.path.expandvars(str(raw).strip()) if raw else ""
    if not raw:
        return default_models_dir()
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = data_dir() / path
    return path


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


APP_ROOT = app_root()
DATA_DIR = data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
OUTPUT_DIR = DATA_DIR / "output"
TOOLS_DIR = _tools_dir()
INSTANCE_PATH = DATA_DIR / "instance.json"
SERVER_LOG_PATH = DATA_DIR / "server.log"
STATIC_DIR = _static_dir()


def free_gb(path: Path) -> float:
    """目标目录所在卷的剩余空间（GB）。目录还不存在时向上找最近的已存在父级。"""
    import shutil

    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(str(probe)).free / (1024 ** 3)
    except OSError:
        return 0.0
