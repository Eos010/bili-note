"""预下载转写模型到本地缓存。点开 download-model.bat 时运行。

用法：
    python download_model.py [模型名 ...]
    不带参数时下载全部可用模型（large-v3 与 large-v3-turbo）。

注意：走 HuggingFace 官方源，需要梯子（Clash 代理 127.0.0.1:7890）；模型下载到项目 models 目录（D 盘），不占 C 盘。
"""
import os
import sys

# 必须在 import faster_whisper 之前设置 —— huggingface_hub 在 import 时就读取这些环境变量
# HF_HOME：模型缓存目录（项目 models 下，D 盘，不占 C 盘）
os.environ.setdefault(
    "HF_HOME",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"),
)
# 如需走代理（梯子），请在运行前设置 HTTP_PROXY / HTTPS_PROXY，或由 download-model.bat 代设

from faster_whisper.utils import download_model  # noqa: E402

AVAILABLE_MODELS = ["large-v3", "large-v3-turbo"]


def main() -> None:
    models = sys.argv[1:] or AVAILABLE_MODELS
    for name in models:
        print(f"[下载] {name} ...", flush=True)
        download_model(name)
        print(f"[完成] {name}", flush=True)
    print("\n全部模型下载完成。", flush=True)
    if sys.stdin.isatty():
        input("\n按回车退出...")


if __name__ == "__main__":
    main()
