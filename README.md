# bili-note

把 B 站视频变成可检索的笔记：**链接 → 视频 + 带时间戳文字稿 + 字幕 + AI 总结**。本地 Web 应用，一次点击完成下载、转写与总结。

## 功能

- 粘贴 B 站链接（支持批量，每行一个），两个独立开关：
  - **下载视频**：默认 1080P，无则取该视频最高可用
  - **转写文字稿**：模型在应用内选择/下载（默认 `small`，CPU 可跑），GPU 自动检测、不可用时回退 CPU
- **一开始不下载任何模型**：安装/首次启动都不会偷偷拉几个 G。
  首次向导先检测显卡/CUDA/ffmpeg/磁盘，再按配置给每个模型标**绿色「推荐下载」/ 红色「配置不足，不推荐」**；
  只有你点「下载」才联网。模型目录可在设置里改，下载、加载、删除只认这一个绝对路径
- **模型中心**：`small`（464MB，CPU 默认）/ `base` / `tiny` / `medium` / `large-v3-turbo` / `large-v3`；
  支持国内镜像（默认 `hf-mirror.com`）/ 官方源 / 自定义源 / 代理；断点续传、取消、sha256 校验；
  旧版 `models/hub` 缓存会被识别为「旧缓存」直接可用，不要求重下
- **本地视频/音频转写**：切到「本地转写」粘贴文件路径（每行一个），跳过下载直接转写，`.txt`/`.srt` 保存在原视频同目录
  - 粘 **`.txt` 文稿路径**会跳过转写、直接生成一个可总结的任务（对已有文稿做总结不用重新转写）
- 每个视频一个目录，输出 `.mp4` + `.txt`（带时间戳全文稿）+ `.srt`（可挂字幕）
- 任务列表 + 实时进度（分阶段：解析 / 下载 / 转写）
- 完成后一键打开输出目录、网页预览文字稿
- 顶栏 **「打开本地输出目录」**：一键在资源管理器里打开 `output/`（目录不存在会自动创建）。
  注意口径：**只有下载类任务**的产物落在 `output/`；本地文件任务的 `.txt`/`.srt` 写在**源文件旁边**，
  那用任务卡片上的「打开目录」
- **取消 / 重试 / 删除**：
  - 运行中点「取消」会立刻停下并标记「已取消」；已下载的半成品（`.part` / DASH 分片）自动清掉
  - 失败或取消的任务有「重试」按钮，不用重新粘链接；**失败时保留 `.part` 以便断点续传**
  - 「移除」会先弹出确认框，**列出"将删除 / 将保留"清单再动手**
    - URL 任务：整个 `output/<标题>/` 目录删掉
    - 本地文件任务：只删生成的 `.txt`/`.srt`/`.summary.md`，**原视频和用户目录永不触碰**
    - 粘 `.txt` 文稿建的任务：那个 `.txt` 是**你的原始资料**，一律不删
- **已有文稿自动检索**：打开页面就扫一遍 `output/`，把**已经转写好的文稿**列在任务面板下面的
  「已有文稿」区块里，直接预览 / 看总结，**不用再手动粘 `.txt` 路径**
  - **为什么和任务列表分开**：任务表是**内存态**（服务一重启就空），而 `output/` 下的 `.txt` 是**文件事实**、一直都在
  - **只认自己写的格式**：每行 `[HH:MM:SS] 正文`，且要求非空行 ≥3、命中率 ≥80%
    —— `output/.tmp/` 里 AB 试跑的 `.stats.txt`（内容是「文稿正文 6523 字…」）就是这么被挡在外面的；
    目录名以 `.` / `_` 开头的一律不扫
  - **前端只拿 id、不拿路径**：每份文稿 id = 相对路径的 sha1 前 12 位，读取时服务端重新扫描比对
    ⇒ "请求里带路径"这件事根本不成立（见 `app/library.py`）
  - 每行显示：字数 · 时长 · 日期 · 有视频/字幕 · **已总结**（有 `<标题>.summary.md` 时给「查看总结」）
  - 看已有文稿的总结时面板是**只读态**：模板 / 时间戳开关 /「重新生成」/「停止」全部收起；
    有本地视频时，点总结里的时间戳照样跳回视频（走 `/api/library/{id}/media`）
  - 接口只读：`GET /api/library`、`/{id}/text`、`/{id}/summary`、`/{id}/media`
- 网络健壮性：解析与下载共用同一套超时/重试参数；报错会翻成中文并给出下一步（原始报错附在最后）
- **AI 总结（v1.3；模板 A 于 v1.4 重写；v1.5 加长稿流程；v2.1 改为后台作业）**：转写完成后点任务卡片上的「总结」，把文稿发给 LLM，在面板里**流式**呈现
  - **总结是后台作业，不是那根连接**（v2.1）：开始之后**关掉面板、切走页面、甚至关掉浏览器，
    总结都继续跑**。首页每张任务卡片各显示各的进度（第几节 / 百分比 / 字数），所以可以**同时总结多个视频**
  - **随时接回来看**：再点「查看」就把已经生成的部分接上，文字继续往下打——服务端会把没赶上的事件补发一遍
  - **同一任务同时只跑一个**：重复点「总结」= 挂上去看（不会白烧第二次钱）；要重跑点「重新生成」
  - **「停止」会保留已完成的节**（长稿是逐节落盘的），下次生成接着跑，不从头再来
  - **同时跑几个**在设置里调（「同时总结数」，默认 3）；超出的自动排队，改完立刻生效、不用重启
  - 内置模板：精读全录（事无巨细 + 挑着引用原话）/ 行动项提取 / 骨架速览 / 自定义
  - **开工前先做长度评估**（纯本地、不花钱）：字数 / 段数 / 时长 / 估算 token → 决定走哪条路，
    并在面板上告诉你"分几节、目标多少字、大概等多久"
  - **长稿走「骨架 → 逐节展开」**（v1.5，见 `app/longdoc.py`）：先让模型只给全文分段
    （每节标题 + 起止时间戳 + 一句主旨，几十秒），再带着完整骨架**逐节**写正文。
    每节都是短生成，所以不会"越写越薄"；实测万字稿（8,704 字 / 23 分钟）11~13 节，每节 1.4~3.4 秒
  - **骨架速览是白送的**：单独存一份 `<标题>.summary.outline.md`，面板顶部可展开、时间戳可点跳转
    —— 1~2 分钟读完，用来判断这个视频值不值得细读
  - **断点续传**：每节独立落盘（`<标题>.summary.md.parts/`）。跑到一半断网或关页面，
    再点一次「重新生成」只补跑缺的那几节，不用从头再花一遍钱；整篇跑完自动清掉缓存
  - **质量自检**（`app/quality.py`，纯本地、不用 LLM 裁判）：完成后报告**锚点覆盖率**
    （原文里的数字/年份/书名有多少落进了总结——这才是"细节有没有丢"的硬指标）、段落召回、引用保真度。
    引号里只要有一句不是原文就会被标出来
  - 模板 A 的配方是**实测对比定下的**：以"自己的话完整转述"为主，只在关键处引用原话。
    篇幅目标按文稿长度**分档**（不是固定比例），且字数要求是**硬性要求第 1 条**。
    改动前请读 `app/summarizer.py` 里 `length_target()` 与 `_requirements()` 上方的注释
    （记着每一轮改坏过什么）
  - **模板 A 的定位是「忠实记录」，不是「压缩」**：实测落点约原文 85%，用来替代"再听一遍"，
    不是用来"快速了解"——**省时间那条路走「骨架速览」**。想更短就调 `length_target()` 的系数，
    但先想清楚愿意丢多少细节
  - **点总结里的时间戳直接跳到视频对应位置**（本地视频用内置播放器，B 站链接开原页面 `?t=`）
  - 结果与文稿同源归档为 `<标题>.summary.md`；重开面板读缓存，点「重新生成」才重跑
  - 支持推理模型：**思考过程单独显示**（deepseek-flash 会先想 40 秒以上，不显示的话界面会空白），可在设置里关掉。
    长稿流程里**逐节展开那几趟关掉思考**（实测思考把耗时拉长约 5 倍：75~95s → 15.7s，而每一节本来就短）
  - **不会用背景知识把原文改对**：识别错的字照原样保留并标注〔疑为「XX」〕，
    这条受质量自检的"引号保真"约束（v1.4 的输出里有过 3 处凭空出现的引号）
  - 输出撞上限时明确提示（`finish_reason=length`），不会静默腰斩

## 环境要求

**便携版（推荐给普通用户）**：Windows 10/11 x64，**不需要 Python / Git / pip**。
解压后双击 `bili-note.exe` 即可；ffmpeg 已随包，模型按需下载。

**源码/开发路径**：
- Windows 10/11
- Python 3.10–3.12（开发环境为 3.12）
- ffmpeg（`install.bat` 会尝试从 PATH / WinGet / gyan.dev 准备好）
- 转写推荐 NVIDIA GPU；没有 GPU 时自动走 CPU（`small` 可用，`large-v3` 不推荐）

## 安装

### 普通用户：便携版（无需命令行）

1. 下载 `bili-note-v3-portable-win64.zip`（GitHub Releases）；
2. **解压到可写目录**，例如 `D:\bili-note`（不要放 `C:\Program Files`，
   程序要把 `config.json` / `output/` / `models/` 写在 exe 旁边）；
3. 双击 `bili-note.exe`；
4. 首次向导里选模型：想先体验就下载 `small`（约 464MB），或者点「先不下载，仅下载视频」。

> 为什么是便携包而不是传统安装器：这个程序的数据（模型、视频、文稿）就在程序旁边，
> 装到 Program Files 反而会因为没有写权限而失败。解压即用、删除即卸载，路径最不容易出错。

### 开发者/源码

```bash
git clone https://github.com/Eos010/bili-note.git
cd bili-note
install.bat          # 一键：Python 检测/安装、venv、pip（清华镜像）、ffmpeg、GPU 运行库
start.bat            # 启动（自动开浏览器）
```

`install.bat` **不会下载模型**：模型在界面里由你自己选、自己点。
更新：`update.bat`（工作区有改动会拒绝 pull，避免覆盖你的修改）。

## 使用

双击 `start.bat` / `bili-note.exe` 后，浏览器会打开 `http://127.0.0.1:8001`
（8001 被别的程序占用时自动换 8002…，并打开正确地址）。粘贴链接 → 选开关 → 点「开始」。

- **首次向导**：环境检查 + 模型选择；跳过也不影响「仅下载视频」。
- **设置 → 转写模型**：模型目录（可改绝对路径）、下载源、代理、CPU/GPU/自动、模型下载/删除、诊断信息。
- 如果由别的常驻入口托管，可设 `BILI_NOTE_NO_BROWSER=1`（或传 `--no-browser`）禁止自动弹浏览器；
  `BILI_NOTE_PORT` 可指定端口。

输出目录：`output/<视频标题>/`。仅转写时临时音频用完即删。

## 模型与磁盘

| 模型 | 体积 | 建议 | 说明 |
|---|---|---|---|
| tiny | 76MB | 低配兜底 | 极快，质量低 |
| base | 142MB | 低配备选 | 快，质量一般 |
| **small** | **464MB** | **CPU 默认** | 质量和速度平衡 |
| medium | 1.46GB | 4GB+ 显存 | CPU 偏慢 |
| large-v3-turbo | 1.55GB | 6GB+ 显存 | 质量约 large-v3 的 99%，更快 |
| large-v3 | 2.95GB | 8GB+ 显存 | 质量最高 |

- 默认模型目录：便携版 = `<解压目录>\models`；源码版 = `<仓库>\models`。
  设置里可改成任意可写磁盘，保存后下载/加载/删除都只认这个目录（不再依赖 `HF_HOME`）。
- 默认下载源：`hf-mirror.com`（国内直连友好）；可切官方或自定义，支持 HTTP 代理。
- 下载中断会保留 `.part`，再点下载续传；LFS 文件按清单里的 sha256 校验。
- 旧版 `models/hub/models--.../snapshots/...` 会被识别为「旧缓存」直接使用，不会自动移动/删除。

## 配置 AI 总结（只需一次）

点页面右上角 **⚙**，填：

| 字段 | 说明 |
|------|------|
| 接口地址 | OpenAI 兼容的 base_url，默认 `https://api.deepseek.com/v1` |
| API Key | 存在 `config.json`，**已在 `.gitignore` 里**。留空 = 不修改 |
| 模型 | 点「测试连接」会拉取可用模型列表填充候选 |

- 「测试连接」= 拉 `GET {base_url}/models`，能验证 Key 和地址是否可用。
- 也可用环境变量覆盖（优先级更高）：`BILI_NOTE_API_KEY`、`BILI_NOTE_BASE_URL`。
- **API Key 永远不会回传给浏览器**，设置接口只返回打码形式。

## 自检

不需要 API Key、不联网、不花钱：

```bash
.venv\Scripts\python.exe selftest.py         # 合并保真性 + 提示词 + 边界 + 版本号/打开输出目录接口（57 项）
.venv\Scripts\python.exe selftest_library.py # 已有文稿自动检索：扫描边界 + 只读接口安全边界（42 项）
.venv\Scripts\python.exe selftest_e2e.py     # 端到端（内置假 LLM，跑完整条流式链路，58 项）
.venv\Scripts\python.exe selftest_cancel.py  # 取消/删除/报错（含慢速本地源，64 项）
.venv\Scripts\python.exe selftest_longdoc.py # 长稿流程（假 LLM：长度评估/骨架/分节/断点续传，76 项）
.venv\Scripts\python.exe selftest_bgsum.py   # 后台总结作业（假 LLM：断连/重连/并发/停止，68 项）
.venv\Scripts\python.exe selftest_models.py  # V3 模型中心（本地假 HF 源：清单/下载/续传/取消/校验/接口，85 项）
```

合计 **450 项**。`selftest_e2e.py`、`selftest_longdoc.py`、`selftest_bgsum.py` 会临时移走你的
`config.json`（旁边留 `.selftest-backup` 可恢复），跑完自动放回；
`selftest_models.py` 用 `.selftest-tmp/` 作数据目录，不碰真实配置/输出/模型。

界面交互另有一个**真浏览器**量具（Node 原生 + CDP，零 npm 依赖，只点「预览文稿」「查看总结」这类
只读按钮，不写盘、不起总结作业）：

```bash
node .verify\library_ui_check.mjs      # 16 项；截图落 .verify\shots\
node .verify\model_ui_check.mjs        # 22 项；首次向导/模型下拉/转写模型设置（不下载模型）
```

> 为什么 Python 自检之外还要它：Python 打的是 HTTP 层，而**"点按钮有没有反应"只有真浏览器能证**。
> 这个项目被这类 bug 咬过两次（`/api/tasks/{id}/cancel`、`/api/tasks/{id}/open`），
> 所以新增的交互一律走一遍真点击。

> `selftest_bgsum.py` 会把被测应用起在**真端口 8096** 上（假 LLM 在 8097）。
> 这不是多此一举：`httpx.ASGITransport` 会把整个响应缓冲完再交出来，
> 用它测"客户端中途断开"测不到任何东西——**断言会在错误的前提下假通过**。

要**真跑一次**看质量，用这两个（会花钱）：

```bash
.venv\Scripts\python.exe _ab_test.py    <文稿.txt> <输出前缀>   # 单趟模板 A，出质量报告
.venv\Scripts\python.exe _ab_longdoc.py <文稿.txt> <输出前缀>   # 走完整长稿流程，出质量报告
```

## 构建便携包（维护者）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_portable.ps1
# 可选：CPU 轻量包（不含 cublas，体积小很多）
powershell -ExecutionPolicy Bypass -File scripts\build_portable.ps1 -NoCuda
```

产出：`release\bili-note-v3-portable-win64.zip`（或 `...-cpu-win64.zip`）。
构建脚本会：装 PyInstaller → 准备 `tools\ffmpeg` → 收集 `app/static` 与原生依赖 →
**用构建出来的 exe 自己跑一次 `--probe-cuda` 和真实 HTTP 健康检查** → 打 zip。
不会把 `config.json` / `output/` / `models/` 打进包。

## 项目结构

```
bili-note/
├── run.py               # 启动入口：端口选择 / 单实例 / --probe-cuda / 自动开浏览器
├── install.bat          # 源码一键安装（Python/venv/pip/ffmpeg/GPU 运行库；不下模型）
├── update.bat           # 源码更新（工作区脏就拒绝）
├── start.bat / stop.bat # 源码启动/停止（stop 走 instance.json，不再按端口猜）
├── download-model.bat   # 命令行预下载模型（和界面同一条下载路径）
├── bili-note.spec       # PyInstaller onedir 规格
├── PORTABLE-README.txt  # 便携包里的使用说明
├── config.example.json  # 无 Key 的配置模板
├── requirements-gpu.txt # NVIDIA cublas（可选）
├── requirements-build.txt
├── app/
│   ├── main.py          # FastAPI 应用 + API + SSE 接线
│   ├── app_paths.py     # 程序根/数据目录/模型目录的唯一解析处（冻结态也认）
│   ├── cuda_bootstrap.py# import ctranslate2 前修好 CUDA DLL 搜索路径
│   ├── hardware.py      # 显卡/CUDA/ffmpeg/磁盘探测 + 人话结论
│   ├── models.py        # 模型清单/路径解析/旧缓存兼容/下载与校验
│   ├── probe.py         # CUDA 子进程探针（坏驱动不会带崩主服务）
│   ├── tasks.py         # 任务管理器（单 worker 串行队列）
│   ├── downloader.py    # yt-dlp 封装：解析/下载/音频
│   ├── transcriber.py   # faster-whisper 封装：只从模型目录加载，设备策略
│   ├── summarizer.py    # 文稿合并 + 提示词模板 + LLM 流式调用 + 长度策略
│   ├── longdoc.py       # 长稿流程
│   ├── sumjobs.py       # 后台总结作业
│   ├── summary_store.py # 总结产物路径与读写
│   ├── library.py       # 已有文稿自动检索
│   ├── quality.py       # 质量量具
│   ├── config.py        # 配置读写（Key 打码、白名单、环境变量优先）
│   └── static/          # 原生 HTML/CSS/JS 前端
├── scripts/
│   ├── install.ps1      # install.bat 的真实逻辑
│   ├── update.ps1
│   ├── stop.ps1         # 校验 PID 后再停，避免 instance.json 过期误杀
│   └── build_portable.ps1 # 打便携 zip（-NoCuda 出 CPU 轻量包）
├── selftest*.py         # 7 套自检（含新增 selftest_models.py）
└── output/              # 产物（git 忽略）
```

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | FastAPI + uvicorn |
| 下载 | yt-dlp（`bv*[height<=1080]+ba/b`） |
| 转写 | faster-whisper + large-v3 / large-v3-turbo（CTranslate2，可切换） |
| 总结 | httpx 流式调用 OpenAI 兼容接口（默认 DeepSeek 官方）；**作业跑在后台 task 上**（`app/sumjobs.py`），SSE 只是它的观看窗口 |
| 前端 | 原生 HTML/CSS/JS（零构建） |

## 版本线

- **V3（当前）· 安装方式 + 转写模型管理重构** —— 一句话：**让没有 Python 基础的人也能装上，
  并且模型默认不下载、下到哪由程序唯一决定。**
  - **安装**：新增便携包（PyInstaller onedir，内置 ffmpeg + Python 运行时，免 Python/Git/pip），
    解压即用；源码路径补 `install.bat` / `update.bat` / 新的 `start.bat` / `stop.bat`。
  - **首次向导**：环境检查（OS/CPU/内存/GPU/显存/CUDA/ffmpeg/磁盘/模型目录）+ 模型卡片；
    绿色「推荐下载」/ 红色「配置不足，不推荐」；模型**只有点下载才联网**。
  - **模型中心**：统一目录（`app/app_paths.py` 唯一解析）、自有布局 `<models_dir>/<模型名>/`、
    绝对路径直接交给 `WhisperModel`；默认源 `hf-mirror.com`，可切官方/自定义/代理；
    `.part` 续传、取消、sha256 校验、磁盘预检；旧 `models/hub` 缓存只读兼容。
  - **默认模型 `small`（464MB，CPU 可跑）**；`device=auto`（CUDA 探测通过才用 GPU，失败回退 CPU 并显示原因）。
    **运行期不再静默下载模型**：模型缺失 → `ModelNotReady` + 可照做的中文提示。
  - **启动器**：端口 8001–8010 自动选择、单实例检测、`instance.json` 给 stop 用、
    `--probe-cuda` 子进程探针；数据/模型路径在界面上都可见。
  - 新增 `app/app_paths.py`、`app/hardware.py`、`app/models.py`、`app/probe.py`、
    `app/cuda_bootstrap.py`、`selftest_models.py`、`.verify/model_ui_check.mjs`、
    `build_portable.ps1` / `bili-note.spec`。
  - **修掉的旧问题**：`HF_HOME` 只在 `start.bat` 设置导致模型下到 C 盘缓存；首次转写静默拉 3GB；
    端口占用误判「已运行」；全新 venv 缺 cublas 导致 GPU 静默退化。
- **v2.1 · 后台总结作业** —— **相对 v2.0 只多了这一件事：总结从"那根连接"里拆出来变成后台作业。**
  其余（界面、长稿流程、模板、产物格式）与 v2.0 一致。
  - **病根**：v1.3 起，生成循环就跑在 `POST /summarize` 的响应生成器里，于是"关掉弹窗"= "取消任务"；
    前端的 `sumStreaming` 是全局单标志，第二个视频开不了；卡片上没有进度。**三件事一个根因**
  - **改法**：作业 = `asyncio.create_task` 的独立 task（不属于请求那棵任务树，断线取消不到）；
    SSE 端点退化成**订阅者**（先回放事件流水，再跟随实时事件）；作业状态挂到任务列表上
    （`task.summary`），首页每张卡片各显示各的进度
  - **幂等**：同一任务同时只有一个作业，重复点「总结」= 挂上去看，重跑要显式 `restart`
  - **并发闸门** `summarize.max_concurrent`（默认 3）：不用 `asyncio.Semaphore`——
    信号量额度构造时定死、改设置要重启；这里每次 `acquire` 现读配置，已在跑的不打断
  - **边界**：停止保留已完成的节；失败落到作业状态上（卡片可见 + 可重试）；跑着时不许删任务
  - 新增 `app/sumjobs.py`、`app/summary_store.py`、`selftest_bgsum.py`
  - 顺手消掉一处**写了两份的规则**：`tasks.py` 与 `main.py` 各有一套"总结文件放哪儿"，
    后台作业需要第三处用到它 → 提成 `summary_store.py`（同一条规则写三份，早晚有一份会漂）
- **v2.0 · 前端风格重构 + 「打开本地输出目录」**：
  - 设计 token 收敛：全文件只引用 `:root` 变量（旧版散着 20 多处硬编码色值，改一处色调得满文件抠）；
    口径是**浅色 · 中性灰蓝为底，哔哩粉弱化成小面积点缀**（开关 / 进度条 / 时间戳 / 焦点环 / 光标），
    主按钮改深板岩底——因为白字压 `#fb7299` 对比度只有约 2.6:1，够不着 WCAG AA
  - 顶部居中大标题 → **工具条**（左品牌、右「打开本地输出目录」+ ⚙），品牌旁显示版本徽章
  - 总结正文恢复字号层级（此前 h2~h6 被统一压成 15.5px，11~13 节的长稿看起来是一堵墙）
  - 补 `:focus-visible` 焦点环、`prefers-reduced-motion`、窄屏换行（任务卡按钮原先会被挤爆）
  - 新接口 `POST /api/open-output` → 打开 `output/`（不存在则先创建）；
    顺带给 `os.startfile` 失败兜底成 400，并把任务卡「打开目录」的失败反馈补上（原先不看 `res.ok`）
  - **版本号单一来源**：`app/__init__.py:__version__` 一处定义，`GET /api/version` 吐给前端顶栏徽章——
    此前代码里写的是 `1.0.0`、文档里写 v1.5，两处各说各话
- **v1.5 · 长稿流程**：长度评估 → 骨架先行 → 逐节展开 → 质量自检 → 断点续传。
  病根是"一趟写完长文会越写越薄、且无视篇幅约束"（实测输出 = 原文 116%）；
  修法不是分块（容量根本不是瓶颈：万字稿只占上下文 0.5%），而是**切输出**。
  新增 `app/longdoc.py`、`app/quality.py`、`selftest_longdoc.py`、`_ab_longdoc.py`。
  同时修掉提示词三处硬伤：与篇幅上限打架的"宁可多写"、写死的"6~14 节"、
  挂在硬性要求列表外因而被无视的字数要求
- **v1.4** 模板 A 提示词重写：输出从"逐条引用原话的卡片式摘录"改成"自己的话转述为主、关键处引原话"的成篇笔记
  （实测：引用占比 46%→约 15%，列表行 145→0，小节标题不再摘原文半句话）；给模板 A 加了篇幅上下限与收尾自检
- **v1.3.2** 取消真根因修复（`Task.cancellable` 缺失导致接口 500）+ 重试路径取消检查点 + 失败重试按钮
- **v1.3.1** 取消/删除修复：统一取消异常、清半成品、删除文件安全白名单、报错翻中文
- **v1.3** AI 总结：文稿合并 + 流式总结面板 + 时间戳跳转 + 设置页
- **v1.2** 双模型切换 + 模型迁 D 盘
- **v1.1** 本地视频/音频转写
- **v1.0** 核心管道

> **版本号口径（2026-09-20 定）：不再做版本号规划。**
> 上面这串是**已发布状态的记录**，不是路线图——不发版就不动它。
> 没有"计划中"的号，也不预留号给某个功能；要做什么直接做，做完了才决定这版叫什么。
> （背景：v2.0 这个号原本留给弹幕导出、v2.1 留给登录态 cookie，结果两次都被别的东西占了。
> 预留号 = 必然漂移，所以这个做法本身取消了。）

## 常见问题（V3 安装/模型）

| 现象 | 处理 |
|---|---|
| 解压后双击没反应 / 提示没有写权限 | 不要放 `Program Files`；解压到 `D:\bili-note` 这类可写目录 |
| 想用 GPU，但向导显示红色 / CPU | 「设置 → 转写模型」看 CUDA 原因；源码版重跑 `install.bat`（会装 cublas）；便携包若提示缺运行库，用 CPU 或换 GPU 完整包 |
| 模型下载很慢 | 设置里把下载源切到「国内镜像 hf-mirror.com」；仍慢可填代理（如 `http://127.0.0.1:7890`） |
| 下载到一半断了 | 已下载部分保留，再点「下载」会续传；不用从头来 |
| 模型明明下过却显示未下载 | 看「设置 → 转写模型」里的模型目录；改过目录后旧目录的模型不会自动搬过去。旧版 `models/hub` 会显示「旧缓存」 |
| 端口 8001 被占用 | 程序会自动用 8002… 并打开正确地址；不要手动改浏览器地址 |
| 杀毒/SmartScreen 拦截 | 未签名 exe 的正常提示：更多信息 → 仍要运行；不要用 onefile 版（本项目用 onedir） |
| 只想先存视频 | 首次向导点「先不下载，仅下载视频」，或关掉「转写文字稿」开关 |
| 粘 `.txt` 文稿做总结 | 不需要转写模型（跳过转写，直接生成可总结任务） |
| 想换模型目录 | 设置里填绝对路径 → 保存；之后下载/加载/删除都只认新目录 |
| 想彻底卸载 | 删除整个程序文件夹即可；`models/` 和 `output/` 都在里面，想保留先拷走 |

## 说明

- **完整便携包**内含 ffmpeg 与 NVIDIA cublas/cuDNN 运行库（只用于本地推理）；
  **CPU 轻量包**不含 cublas，检测到显卡也会明确显示「CUDA 不可用，将用 CPU」。
- 下载与转写的产物仅供**个人学习与研究**使用，请尊重视频作者版权，勿用于商业用途或二次分发。
- 总结默认对接 DeepSeek 官方接口（OpenAI 兼容），可在设置里换成任意兼容服务。
- `config.json` 含 API Key，已在 `.gitignore` 中，**提交前请确认它没有被纳入版本控制**。

## 许可

[MIT License](LICENSE) © 2026 Eos010
