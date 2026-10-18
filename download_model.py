"""预下载转写模型（命令行版）。

统一走 `app.models` 的下载器——和界面里「设置 → 转写模型」完全同一条路径：
同样的模型目录、同样的镜像/代理配置、同样的 `.part` 续传与 sha256 校验。

用法：
    .venv\\Scripts\\python.exe download_model.py            # 默认下载 small（CPU 可跑）
    .venv\\Scripts\\python.exe download_model.py small large-v3
    .venv\\Scripts\\python.exe download_model.py --list

注意：模型目录由配置 `models.dir` 决定（默认是程序目录下的 models/），
不再依赖 `HF_HOME`，所以下完一定能被程序找到。
"""
from __future__ import annotations

import sys

from app import models


def main() -> int:
    args = [a for a in sys.argv[1:] if a.strip()]
    if any(a in ("--list", "-l") for a in args):
        print("可用模型：")
        for m in models.CATALOG:
            mark = "已下载" if models.is_installed(m.name) else "未下载"
            print(f"  {m.name:<16} {m.size / 1024**2:>8.1f} MB  {mark}  {m.desc}")
        print(f"\n模型目录：{models.models_dir()}")
        return 0

    names = args or ["small"]
    for name in names:
        if name not in models.BY_NAME:
            print(f"[跳过] 没有这个模型：{name}")
            continue
        if models.is_installed(name):
            print(f"[已有] {name} → {models.resolve_path(name)}")
            continue
        print(f"[下载] {name}（约 {models.BY_NAME[name].size / 1024**2:.0f} MB）")
        def on_progress(s: dict) -> None:
            if s.get("state") == "running":
                speed = s.get("speed") or 0
                print(
                    f"\r  {s.get('percent', 0):5.1f}%  "
                    f"{s.get('done', 0) / 1024**2:7.1f} / {s.get('total', 0) / 1024**2:.1f} MB"
                    + (f"  {speed / 1024**2:5.1f} MB/s" if speed else ""),
                    end="", flush=True,
                )
        try:
            result = models.download_sync(name, on_progress)
        except Exception as e:  # noqa: BLE001 —— CLI 要给人看原因
            print(f"\n[失败] {name}：{e}")
            continue
        print(f"\n[完成] {name} → {result.get('path')}")

    print(f"\n模型目录：{models.models_dir()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
