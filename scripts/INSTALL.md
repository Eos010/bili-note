# 一键安装（源码/开发路径）

**普通用户不需要这个文件**：直接下载 `bili-note-v3-portable-win64.zip`，解压到可写目录，
双击 `bili-note.exe` 即可（免 Python、免 Git、免 pip）。

这个脚本给想用源码/想自己调代码的人：

1. 找 Python 3.10–3.12（没有就用 winget 装 3.12，再没有就打开 python.org）；
2. 建 `.venv`，用清华镜像装 `requirements.txt`（失败自动回退官方 PyPI）；
3. 检测到 NVIDIA 显卡时装 `requirements-gpu.txt`（cublas），并把 DLL 放进 ctranslate2 目录；
4. 找 ffmpeg：PATH → winget → 下载到 `tools\ffmpeg`；
5. 没有 `config.json` 时从 `config.example.json` 复制；
6. **不下载任何模型**（模型在界面里由你自己选、自己点）。

重复运行是安全的：已存在的 venv 和依赖会被复用/补齐。

跑完双击 `start.bat`。
