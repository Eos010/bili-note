# bili-note

把 B 站视频变成可检索的笔记：**链接 / 本地文件 → 视频 + 带时间戳文字稿 + 字幕 + AI 总结**。

本地 Web 应用，数据都在自己电脑上。普通用户下载便携包解压即用，不需要 Python、Git 或 pip。

## 能做什么

- **B 站链接 → 视频 + 文稿 + 字幕**：批量粘贴链接，可单独开关「下载视频」「转写文字稿」；默认 1080P，无则取最高可用。
- **本地视频/音频转写**：粘贴文件路径直接转写，`.txt` / `.srt` 写在源文件旁边。
  粘已有的 `.txt` 文稿会跳过转写，直接生成可总结的任务。
- **转写模型中心**：`tiny` / `base` / `small` / `medium` / `large-v3-turbo` / `large-v3`，
  应用内下载、切换、删除；默认 `small`（约 464MB，CPU 可跑），**安装和首次启动都不会自动下载模型**。
- **首次向导**：自动检测系统、内存、NVIDIA 显卡/显存、CUDA、ffmpeg、磁盘和模型目录；
  每个模型给出绿色「推荐下载」或红色「配置不足，不推荐」，由你决定下不下。
- **模型目录可配置**：默认在程序目录的 `models/`，设置里可以改成任意可写磁盘；
  下载、加载、删除都只认这个绝对路径，不会再出现“模型下到别处找不到”的情况。
- **下载源/代理**：默认国内镜像 `hf-mirror.com`，可切 HuggingFace 官方、自定义源或填 HTTP 代理；
  支持断点续传、取消、sha256 校验和磁盘空间预检。
- **AI 总结**：转写完成后一键生成，流式显示；支持精读全录 / 行动项 / 骨架速览 / 自定义模板。
  长稿走「骨架 → 逐节展开」，不会越写越薄；每节独立落盘，断了可以接着跑。
- **后台总结作业**：开始之后关掉面板、切走页面、甚至关掉浏览器，总结都继续跑；
  首页每张任务卡片各显示各的进度，可以同时总结多个视频。
- **点时间戳跳回视频**：本地视频用内置播放器，B 站链接打开原页面 `?t=秒数`。
- **已有文稿自动检索**：打开页面自动扫描 `output/`，直接预览或查看总结，不用再粘路径。
- **任务管理**：实时进度、取消、重试、移除；删除前先列出「将删除 / 将保留」清单。
  本地文件的源视频和用户目录永不触碰。
- **网络报错说人话**：超时、风控、下架、磁盘满、ffmpeg/CUDA 缺失都会翻成中文并给出下一步。

## 安装

### 普通用户：便携版（推荐）

1. 下载 `bili-note-v3-portable-win64.zip`（GitHub Releases）；
2. **解压到可写目录**，例如 `D:\bili-note`。
   不要放 `C:\Program Files`——程序要把 `config.json` / `output/` / `models/` 写在 exe 旁边；
3. 双击 `bili-note.exe`；
4. 首次向导里选模型：想先体验就下载 `small`（约 464MB），或点「先不下载，仅下载视频」。

> 为什么用便携包而不是传统安装器：模型、视频、文稿都在程序旁边，装到 Program Files 反而会因为没有写权限而失败。
> 解压即用、删除即卸载，路径最不容易出错。
>
> 完整包约 796MB，含 ffmpeg + Python 运行时 + NVIDIA cublas，支持 GPU；
> `-cpu-` 轻量包约 269MB，不含 cublas，只能用 CPU。

### 开发者：源码

环境：Windows 10/11、Python 3.10–3.12、ffmpeg（没有也能由脚本准备）、可选 NVIDIA GPU。

```bash
git clone https://github.com/Eos010/bili-note.git
cd bili-note
install.bat    # 建 venv、装依赖（清华镜像）、准备 ffmpeg 和 GPU 运行库
start.bat      # 启动并自动打开浏览器
```

- 更新：`update.bat`（工作区有改动会拒绝 pull）。
- 停止：`stop.bat`（按 `instance.json` 校验 PID，不会误杀别的程序）。
- `install.bat` **不会下载模型**：模型在界面里由你自己选、自己点。

## 使用

双击 `start.bat` / `bili-note.exe` 后，浏览器会打开 `http://127.0.0.1:8001`
（8001 被占用时自动换 8002…，并打开正确地址）。

- 粘 B 站链接：选「下载视频 / 转写文字稿」→ 开始。
- 切到「本地转写」：粘本地视频/音频路径；粘 `.txt` 则直接生成可总结任务。
- 完成后：任务卡可打开目录、预览文稿、生成总结。
- 设置 → **转写模型**：模型目录、下载源、代理、CPU/GPU/自动、模型下载/删除、诊断信息。
- 设置 → **AI 总结**：接口地址、API Key、模型、最大输出 tokens、同时总结数。

输出目录：`output/<视频标题>/`（下载类任务）；本地文件任务的 `.txt`/`.srt` 在源文件旁边。

## 模型与磁盘

| 模型 | 体积 | 建议 |
|---|---|---|
| tiny | 76MB | 低配兜底，质量低 |
| base | 142MB | 低配备选 |
| **small** | **464MB** | **默认，CPU 可跑** |
| medium | 1.46GB | 4GB+ 显存；CPU 偏慢 |
| large-v3-turbo | 1.55GB | 6GB+ 显存，质量接近 large-v3、更快 |
| large-v3 | 2.95GB | 8GB+ 显存，质量最高 |

- 默认模型目录：便携版 `<解压目录>\models`；源码版 `<仓库>\models`。设置里可改。
- 默认下载源：`hf-mirror.com`；可切官方/自定义，支持 HTTP 代理。
- 下载中断会保留 `.part`，再点下载续传；LFS 文件按 sha256 校验。
- 旧版 `models/hub/...` 缓存会识别为「旧缓存」直接使用，不要求重下、不会自动移动或删除。

## 配置 AI 总结（只需一次）

点右上角 **⚙ → AI 总结**：

| 字段 | 说明 |
|---|---|
| 接口地址 | OpenAI 兼容的 base_url，默认 `https://api.deepseek.com/v1` |
| API Key | 存在 `config.json`（已在 `.gitignore`）。留空 = 不修改 |
| 模型 | 点「测试连接」会拉取可用模型列表 |

也可以用环境变量覆盖（优先级更高）：`BILI_NOTE_API_KEY`、`BILI_NOTE_BASE_URL`。
**API Key 永远不会回传给浏览器**，设置接口只返回打码形式。

## 关键功能迭代

- **核心管道**：B 站链接 / 本地文件 → 下载 → 转写 → 带时间戳 `.txt` + `.srt`。
- **本地转写与文稿复用**：本地视频/音频直接转写；粘 `.txt` 跳过转写，直接做总结。
- **AI 总结**：流式输出 → 长稿「骨架 + 逐节」→ 后台作业（关页面继续跑、多任务并行）。
- **已有文稿自动检索**：扫描 `output/`，直接预览/看总结，与内存态任务列表分开。
- **任务稳定性**：取消/重试/删除的边界、失败保留断点、删除前列清单、报错翻中文。
- **安装与模型（当前 V3）**：便携包 + 源码一键安装；首次向导；GPU/CUDA 检测与绿色/红色推荐；
  模型默认不下载、目录唯一、镜像/续传/校验；旧模型缓存兼容。

## 自检

不需要 API Key、不联网、不花钱：

```bash
.venv\Scripts\python.exe selftest.py           # 合并/提示词/边界/版本号/打开输出目录（57）
.venv\Scripts\python.exe selftest_library.py   # 已有文稿扫描与只读接口（42）
.venv\Scripts\python.exe selftest_e2e.py       # 端到端（假 LLM，58）
.venv\Scripts\python.exe selftest_cancel.py    # 取消/删除/报错（64）
.venv\Scripts\python.exe selftest_longdoc.py   # 长稿流程（76）
.venv\Scripts\python.exe selftest_bgsum.py     # 后台总结作业（68）
.venv\Scripts\python.exe selftest_models.py    # 模型中心（本地假 HF 源，85）
```

合计 **450 项**。另有真浏览器量具（Node 原生 + CDP，零 npm 依赖）：

```bash
node .verify\library_ui_check.mjs   # 已有文稿界面（16）
node .verify\model_ui_check.mjs     # 首次向导 / 模型下拉 / 转写模型设置（22）
```

界面量具只点只读按钮、不写盘、不下载模型；截图落 `.verify\shots\`。

## 项目结构

```
bili-note/
├── run.py                # 启动入口：端口选择、单实例、--probe-cuda
├── install.bat / update.bat / start.bat / stop.bat
├── download_model.py     # 命令行预下载模型（和界面同一条路径）
├── bili-note.spec        # PyInstaller onedir 规格
├── config.example.json   # 无 Key 的配置模板
├── app/
│   ├── main.py           # FastAPI + API + SSE
│   ├── app_paths.py      # 程序根/数据/模型目录唯一解析
│   ├── models.py         # 模型清单/路径/旧缓存/下载校验
│   ├── hardware.py       # 显卡/CUDA/ffmpeg/磁盘探测
│   ├── cuda_bootstrap.py # import ctranslate2 前修 CUDA DLL 路径
│   ├── probe.py          # CUDA 子进程探针
│   ├── tasks.py          # 任务管理器（单 worker 串行）
│   ├── transcriber.py    # faster-whisper 封装
│   ├── summarizer.py / longdoc.py / sumjobs.py / quality.py
│   ├── library.py        # 已有文稿扫描
│   └── static/           # 原生 HTML/CSS/JS
├── scripts/              # install/update/stop/build_portable 的 PowerShell 逻辑
└── output/               # 产物（git 忽略）
```

技术栈：FastAPI + uvicorn、yt-dlp、faster-whisper（CTranslate2）、httpx 流式调用 OpenAI 兼容接口、原生前端（零构建）。

## 常见问题

| 现象 | 处理 |
|---|---|
| 双击没反应 / 提示没有写权限 | 不要放 `Program Files`，解压到 `D:\bili-note` 这类可写目录 |
| 想用 GPU，但显示 CPU | 「设置 → 转写模型」看 CUDA 原因；源码版重跑 `install.bat`；CPU 轻量包请换完整包 |
| 模型下载慢 | 设置里切「国内镜像 hf-mirror.com」，或填代理（如 `http://127.0.0.1:7890`） |
| 下载到一半断了 | 已下载部分保留，再点「下载」续传 |
| 模型下过却显示未下载 | 看设置里的模型目录是否改过；旧版 `models/hub` 会显示「旧缓存」 |
| 端口 8001 被占用 | 程序会自动换端口并打开正确地址 |
| SmartScreen / 杀毒拦截 | 未签名 exe 的正常提示：更多信息 → 仍要运行 |
| 只想先存视频 | 向导里点「先不下载」，或关掉「转写文字稿」 |
| 想彻底卸载 | 删除程序文件夹即可；`models/` 和 `output/` 想保留先拷走 |

## 说明

- 下载与转写产物仅供**个人学习与研究**使用，请尊重视频作者版权，勿用于商业用途或二次分发。
- 总结默认对接 DeepSeek 官方接口（OpenAI 兼容），可在设置里换任意兼容服务。
- `config.json` 含 API Key，已在 `.gitignore` 中；提交前请确认它没有被纳入版本控制。
- 便携包内含 ffmpeg；完整包另含 NVIDIA cublas/cuDNN 运行库（仅用于本地推理）。

## 许可

[MIT License](LICENSE) © 2026 Eos010
