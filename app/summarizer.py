"""总结模块：文稿合并 + LLM 流式调用。

两件事：
1. **合并**（`merge_transcript`）：把 whisper 的碎片行拼成段，段首保留一个时间戳，
   **不改一个字**——保证"引用可逐字对回原稿"。
2. **调用**（`stream_chat`）：OpenAI 兼容 /chat/completions，流式返回增量文本。

设计依据见 `项目档案\\bili-note\\v1.3-总结功能设计稿.md`。
"""
from __future__ import annotations

import json
import os
import re
from typing import AsyncIterator, Iterable

import httpx

# ---------------------------------------------------------------- 文稿合并

TS_RE = re.compile(r"^\[(\d{1,2}):(\d{2}):(\d{2})\]\s*(.*)$")
_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
# 中文片段之间不留空格（中文本来无空格）；英文/混排保留空格
_CJK_SPACE_RE = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")


class SummarizeError(Exception):
    """给前端看的、说人话的错误。"""


def _fmt(secs: int) -> str:
    return f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"


def merge_transcript(
    lines: Iterable[str],
    seg_chars: int = 400,
    seg_seconds: int = 180,
    gap_seconds: int = 3,
) -> list[tuple[str, str]]:
    """碎片行 → 段落列表 [(时间戳, 合并正文), ...]。

    断段条件（任一满足）：段正文达 seg_chars / 段跨度达 seg_seconds /
    与上一行的间隔超过 gap_seconds。
    """
    segs: list[tuple[str, str]] = []
    parts: list[str] = []
    ts = ""
    start_secs = 0
    prev_secs: int | None = None
    cur_len = 0

    def flush() -> None:
        if parts:
            segs.append((ts, _join(parts)))

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        m = TS_RE.match(line)
        if m:
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            body = m.group(4).strip()
        else:
            # 没有时间戳的行（少数格式）：归入当前段，沿用上一个时间
            if prev_secs is None:
                continue
            secs, body = prev_secs, line
        if not body:
            continue

        if parts:
            too_long = cur_len >= seg_chars
            too_old = (secs - start_secs) >= seg_seconds
            big_gap = prev_secs is not None and (secs - prev_secs) > gap_seconds
            if too_long or too_old or big_gap:
                flush()
                parts, cur_len = [], 0

        if not parts:
            ts, start_secs = _fmt(secs), secs
        parts.append(body)
        cur_len += len(body)
        prev_secs = secs

    flush()
    return segs


def _join(parts: list[str]) -> str:
    """空格连接后，去掉中文字符之间的空格（中文原文没有空格）。"""
    return _CJK_SPACE_RE.sub("", " ".join(parts))


def build_document(segs: list[tuple[str, str]]) -> tuple[str, int, str]:
    """段落列表 → 发给模型的文稿正文。返回 (正文, 段数, 时长字符串)。"""
    body = "\n".join(f"[{ts}] {text}" for ts, text in segs)
    last = segs[-1][0] if segs else "00:00:00"
    return body, len(segs), last


def read_and_merge(txt_path: str, **opts) -> tuple[list[tuple[str, str]], str, str]:
    """读 .txt 文稿并合并。返回 (段落, 正文, 时长)。"""
    if not os.path.isfile(txt_path):
        raise SummarizeError(f"文字稿不存在：{txt_path}")
    with open(txt_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    segs = merge_transcript(lines, **opts)
    if not segs:
        raise SummarizeError("文字稿是空的，无法总结。")
    body, _, duration = build_document(segs)
    return segs, body, duration


# ---------------------------------------------------------------- 提示词模板
#
# 模板 A 的配方（v1.4 起）是在**实测对比**上改出来的，改之前请先读这段：
#
# 2026-09-19 实测：v1.3 的模板 A 拿一份 13518 字的文稿，吐出 12228 字的总结——
# 原文的 90%，其中 46% 是「」里的逐字引用，203 行里有 158 行是项目符号。
# 逐条「- 引用原话：…」的格式把"事无巨细"落实成了"把文稿抄一遍"：覆盖没丢，
# 但读起来是卡片不是文章，用户的实际评价是"不如豆包"。
# 病根不在"要详细"，而在三处写坏了的措辞：
#   1) 「凡属核心论点…必须引用原话」→ 句句引用 → 半篇是原文；
#   2) 「宁可啰嗦，绝不遗漏」被读成"多抄原话"；
#   3) 「不要按重要性重排」被读成"逐段映射" → 小节标题直接摘取该段第一句，
#      出现「就是假设对方之前自己的处理方式是错的」这种半句话标题。
# 所以现在的配方是：覆盖度用**明确的下限**（字数目标 + 自检）保住，
# 同时用**明确的上限**（引用占比 ≤20%、正文 ≤ 原文）压掉照抄。

_SYSTEM = (
    "你是知识类视频的精读助手：把一段口播录像的文字稿，整理成一篇能替代"
    "「逐字稿精读」的详细笔记。你的读者看过视频、想省掉再听一遍的时间，"
    "又不接受漏掉任何一处细节——所以你的底线是覆盖，不是精炼。"
)


# ---------------------------------------------------------------- 长度策略
#
# 2026-09-20 实测（万字稿 8,704 字）：模板 A 的「40~60%、硬上限 75%」**完全没被执行**，
# 一趟吐出 9,765 字 = 原文 115.9%。原因不是"模型不听话"那么简单——是提示词里有两句话在打架：
# 【任务】说"宁可多写，也不要为了篇幅而合并、跳过或一笔带过"，
# 【篇幅目标】说"75% 是硬上限"。模型选了前者。v1.5 把前者删了。
#
# 另一件事：**压缩率不能是固定比例**。固定 45% 放在 2,000 字短稿上太狠，
# 放在 60,000 字长稿上又要求模型一次写 27,000 字（写不完 → 后段必然变薄）。
# 所以给一条随长度下弯的曲线，并且永远带一个绝对上限。


def length_target(chars: int) -> tuple[int, int, int]:
    """按文稿长度给出 (下限, 上限, 硬上限) 字数。随长度下弯，不是固定比例。

    ⚠️ **这是"要求值"，不是"实际落点"**。2026-09-20 实测：模型系统性超出目标——
    要求 35~45%（上限 55%）时，实际落在 63~86%；要求 60~70% 时，实际 82%。
    也就是说**模型把"硬上限"当软建议，落点大约是要求值的 1.5~1.9 倍**。
    所以这一栏写着 35~45% 的稿子，交出来是一份约 85% 的**忠实记录**。

    这个落点是对的，不是 bug：作者在 v1.4 就定过口径——
    「别概括得太精炼、别丢细节和原味」，字数是他最后才动的旋钮。
    模板 A 的定位因此是**「精读全录」：忠实、可读、可跳转的完整记录**，
    用来替代"再听一遍"，不是用来"快速了解"。
    **"省时间"那条路由「骨架速览」承担**（`longdoc.py` 的骨架层，万字稿约 500~800 字，
    1~2 分钟读完）——那才是给人做取舍用的。

    想让它更短，就把这里的系数再往下调；调之前先想清楚愿意丢多少细节——
    实测锚点覆盖率在压到 62% 时仍能保住 100%，但再往下就开始掉了。
    """
    if chars <= 3_000:
        lo_r, hi_r, cap_r = 0.45, 0.60, 0.70
    elif chars <= 8_000:
        lo_r, hi_r, cap_r = 0.40, 0.55, 0.65
    elif chars <= 25_000:
        lo_r, hi_r, cap_r = 0.35, 0.45, 0.55
    elif chars <= 60_000:
        lo_r, hi_r, cap_r = 0.25, 0.35, 0.42
    else:
        lo_r, hi_r, cap_r = 0.18, 0.26, 0.32
    lo, hi, cap = int(chars * lo_r), int(chars * hi_r), int(chars * cap_r)
    # 绝对上限：再长的稿子，一篇笔记也不该超过 2 万字——超过了就是没在总结。
    return lo, hi, min(cap, 20_000)


def section_range(chars: int) -> tuple[int, int]:
    """按文稿长度给出建议的**小节数**区间。

    v1.4 把「6~14 节」写死在提示词里，跟稿子多长无关：万字稿只有 14 节时每节摊 600 多字，
    直接顶爆"每段 150~300 字"那条——这就是长稿"顾不到细节"的具体形态。
    """
    lo = max(3, min(20, round(chars / 1_200)))
    hi = max(lo + 2, min(28, round(chars / 650)))
    return lo, hi


def _requirements(
    with_ts: bool,
    *,
    scope: str = "full",
    sec_range: tuple[int, int] | None = None,
    length: tuple[int, int, int] | None = None,
    source_chars: int = 0,
    source_desc: str = "文稿",
) -> str:
    """模板 A 的硬性要求，返回带连续编号的文本。

    编号由这里统一生成（正文里不要自己写「1.」），新增/条件性插入条目时才不会断号。

    `scope`：
      - ``"full"``    单趟总结整篇（模板 A 的老路径）
      - ``"section"`` 长稿流程里写**其中一节**（`longdoc.py`）——
        节数、标题、时间戳都由骨架给定，所以那几条要换掉，不能再让模型自己分节。
    """
    in_section = scope == "section"
    reqs: list[str] = []

    # 1 字数 —— **必须排第一条**。
    #   2026-09-20 实测：字数要求原来写在【篇幅目标】里、排在【硬性要求】之后，
    #   模型当它不存在——单趟输出 = 原文 116%，分节流程里每节也稳定在 65~106%。
    #   把正文和原文并排一看就明白了：**它做的是"加标点 + 轻度改写"，不是总结**。
    #   病根是措辞给了它台阶：①「覆盖优先」+②「不许抽象压缩」+「压缩掉的只允许是口语重复」
    #   三条加起来 = "所有信息、同等具体度、只能删语气词" → 那当然压不动。
    #   现在：字数是第 1 条，覆盖明确从属于它，并且**直说可以从哪里省**（见第 3 条）。
    if length:
        lo, hi, cap = length
        reqs.append(
            f"**字数是最硬的一条**：{source_desc} {source_chars} 字，你的正文"
            f"（不含小节标题、不含时间戳那一行）要写到 **{lo}~{hi} 字**，"
            f"**{cap} 字是硬上限**。写完自己数一遍，超了上限就回头砍，砍进区间再交。"
            "超字数的含义不是「写得详细」，而是**没在总结**——"
            "只加标点的照搬也算。自检办法：随便挑一段你的正文和原文对照，"
            "如果两边的句子几乎一一对应，就是抄，重写。"
        )

    # 2 覆盖（信息都在 ≠ 句子都在）
    reqs.append(
        "信息一个都不能少：讲话人提到的每一个论点、每一个例子、每一组数字、每一条因果推论、"
        "每一处转折和让步，都要落到总结里。判断标准只有一条——读者只看总结，"
        "就知道他讲过的每一件事。**但「都在」指的是信息都在，不是句子都在**："
        "原话只能出现在引号里（见下条），引号之外一律用自己的话写。"
    )

    # 3 怎么省字数 —— 只喊"不许照抄"是没用的，模型不知道**该砍什么**，于是只加标点。
    reqs.append(
        "字数要从这些地方省出来（是必须省，不是可选）："
        "同一个意思讲了两三遍的，只留最清楚的那一遍；"
        "「你可能会问」「说白了」「也就是说」这类与听众互动的插入语、铺垫和过渡，"
        "只留信息、不留过程；举例前的冗长铺陈砍掉，但**例子本身和它的关键细节"
        "（谁、做了什么、结果如何）要留**；把「先说什么、又叫什么、说白了就是」"
        "这种层层解释合并成一句定义。省掉的是**说话的方式**，不是**说的内容**。"
    )

    # 4 具体度（保留信息，不保留句法）
    reqs.append(
        "保留具体度：例子、数字、书名、人名、场景、比喻、「反对什么、赞成什么」的对立关系，"
        "都要保留到具体，不要提炼成「方法论」「三步走」这类原文里根本没有的说法。"
        "但具体 ≠ 照搬句子：一句话能说清的事不要压成四个字；"
        "同样，三句绕来绕去才说清的事，要写成一句紧凑的书面中文。"
    )

    # 5 引用（原：「凡属核心论点…必须引用原话」，是"抄稿"的主因；但压过头会丢原味。
    #   实测记录：写"不超过两成"→ 77% 掉到 7%；写"至少一两处"→ 4%；
    #   写"每三五句带一处引号"→ 27%，连引号一起把正文撑爆（整篇被 max_tokens 腰斩）。
    #   所以配比要写成一个**中间量**，并且强调引号里只放关键短句、不要整段搬。）
    reqs.append(
        "保留讲话人的原味：不要用你自己的书面语把他讲的东西全部重说一遍。他讲得形象的比喻、"
        "给概念下的定义、下判断下结论的那句话，都摘原话放进引号里，不要转述——"
        "平均下来大约每段带出一处引号就够了。但引号里只放关键的那一句半句，"
        "不要把整段话搬进引号充数。引号**只用中文直角引号「」**"
        "（全篇统一，不要用弯引号或英文引号），前端靠它把原话标出来。"
        "引用必须逐字取自文字稿，不得改写后冒充原话；明显的识别错字照原样保留（见下一条）。"
    )

    # 6 顺序
    reqs.append(
        "按讲话人的顺序组织：就是讲话人讲述的先后顺序，不要按你自己判断的重要性重排，"
        "也不要合并进「按主题分类」的框子里。"
    )

    # 7 标点/错字
    #   注意：whisper 原文**零标点**，所以"引号里一个字都不许增删改"和"补标点"是打架的。
    #   实测（2026-09-20）：基线输出里 3 处引号在原文中根本不存在——模型给 ASR 错字
    #   「埃里克埃里克·埃里克」补成了正确的「埃里克·埃里克森」，还凭空写了「弥散性」。
    #   所以这里的措辞要堵死"用背景知识顺手改对"这条路。
    reqs.append(
        "补标点、不改词：转述和引用都补上必要标点让它可读。但引号里**除标点外一个字都不许"
        "增删改**——错别字、人名说错的、明显识别错的，一律照原样保留，"
        "后面紧跟一个方括号注明〔疑为「XX」〕即可（只写这几个字，不要展开解释）。"
        "**不许用你的背景知识把原文改对**，那属于脑补。"
    )

    # 8 行文（实测：不写死段数，模型会把一整节写成一块 800 字的黑砖）
    reqs.append(
        "读得出「一篇东西」：不要写成卡片或词条，每个小节用连贯的叙述写。"
        "但必须分段：一节按话题的推进分成 2~4 个自然段，每段 2~4 句、约 150~300 字；"
        "400 字以上不分段的大块文字不合格。只有讲话人自己就在列举"
        "（如「第一种…第二种…」）时才用列表。"
    )

    # 9 标题
    reqs.append(
        "小节标题要像标题：2~12 个字，用讲话人自己的说法命名这一节在讲什么，"
        "不要直接摘取节内第一句话。"
    )
    if in_section:
        # 分节模式：标题/时间戳/顺序/节数都由骨架给定，把上面两条换掉
        reqs[5] = (
            "只写这一节：本节之外的任何内容都不要写，不要写导语、不要写小结、"
            "不要提「下一节」「前面说过」。"
        )
        reqs[8] = "标题原样使用给定的小节标题，一个字都不要改，也不要另起标题。"
    elif sec_range:
        lo_sec, hi_sec = sec_range
        reqs[8] = (
            "小节标题要像标题：2~12 个字，用讲话人自己的说法命名这一节在讲什么，"
            f"不要直接摘取节内第一句话。整篇 {lo_sec}~{hi_sec} 节为宜。"
        )

    if with_ts:
        reqs.append(
            "标注位置：每节标题的下一行单独写该节第一句话的时间戳，"
            "格式 [HH:MM:SS]，只写文稿里真实出现过的时间戳，不要两节用同一个时间戳。"
            "前端靠它点击跳到视频对应位置，务必准确。"
        )
    reqs.append(
        "不得脑补：文字稿没有的信息一律不写，包括你的背景知识、你的评价，"
        "以及「值得学习」「非常重要」这类套话。"
    )
    reqs.append(
        "不确定的地方写出来：听不清或语义不通的片段，不要猜、不要硬凑通顺。"
        "标注要短，就写〔识别不清〕或〔疑为「XX」〕，不要写成一句解释。"
    )
    # 收尾自检：把"覆盖"和"字数"都变成一次可执行的核对，而不是一句口号
    scope_word = "本节原文" if in_section else "文稿"
    reqs.append(
        f"收尾自检（不要输出自检过程）：写成后对着{scope_word}快速核一遍——"
        "① 有没有哪一段话、哪一个例子在总结里找不到对应？有没有两节之间漏了内容？"
        "② 有没有哪句引号里的话其实不是原文（或者是你顺手改「对」的）？"
        "③ **数一遍正文字数，超上限就砍。** 发现就补上、改掉，再交稿。"
    )
    return "\n".join(f"{i}. {r}" for i, r in enumerate(reqs, 1))


def _doc_block(body: str, seg_count: int, duration: str) -> str:
    return f"【文稿信息】共 {seg_count} 段，覆盖时长 {duration}。\n\n【文稿正文】\n{body}"


_TEMPLATES: dict[str, dict] = {
    "A": {
        "name": "精读全录",
        "desc": "事无巨细 + 挑着引用原话（默认）",
    },
    "B": {
        "name": "行动项提取",
        "desc": "只要能落地做的事",
    },
    "C": {
        "name": "骨架速览",
        "desc": "先判断值不值得细读",
    },
    "D": {
        "name": "自定义",
        "desc": "用你自己的提示词",
    },
}


def list_templates() -> list[dict]:
    return [{"id": k, **v} for k, v in _TEMPLATES.items()]


def build_prompt(
    template: str,
    body: str,
    seg_count: int,
    duration: str,
    custom_prompt: str = "",
    with_timestamp: bool = True,
) -> tuple[str, str]:
    """返回 (system, user)。"""
    doc = _doc_block(body, seg_count, duration)
    tpl = (template or "A").upper()

    if tpl == "B":
        ts_req = (
            "每条行动项后面标注它在视频里的时间戳，格式 [HH:MM:SS]。"
            if with_timestamp else ""
        )
        user = f"""下面是一份视频文字稿，按时间顺序排列，每段开头是起始时间 [HH:MM:SS]。
文稿由语音识别生成：可能完全不带标点、句子在任意位置断开，也可能已带标点——以实际内容为准。

【任务】
提取这份文稿里**所有可以落地执行的行动项**，不要写概括性的读后感。

【硬性要求】
1. 只输出"能做的事"：方法、步骤、清单、判断标准、避坑建议。纯观点/抒情不需要。
2. 每条必须引用讲话人的原话作为依据，用「」标出，不得改写后冒充原话。
3. 不要脑补：文稿里没说的做法不要添加。
4. 不确定的地方写「此处识别不清」，不要猜。
{ts_req}

【输出格式】
Markdown 列表。每条格式：
- **<做什么>** —— 依据原话「……」
{ts_req and "  [HH:MM:SS]" or ""}

{doc}"""
        return _SYSTEM, user

    if tpl == "C":
        user = f"""下面是一份视频文字稿，按时间顺序排列，每段开头是起始时间 [HH:MM:SS]。
文稿由语音识别生成：可能完全不带标点、句子在任意位置断开，也可能已带标点——以实际内容为准。

【任务】
快速判断这个视频值不值得细看，输出**骨架**，不要展开细节。

【硬性要求】
1. 总长度控制在 600 字以内，超出即视为不合格。
2. 只输出：(a) 一句话说清这个视频在讲什么；(b) 3~6 个章节标题（用讲话人自己的说法）；
   (c) 这个视频最值得看的一处亮点，引用原话「」。
3. 不展开论证过程，不列举例子细节。
4. 不要脑补。

【输出格式】
Markdown。

{doc}"""
        return _SYSTEM, user

    if tpl == "D":
        head = custom_prompt.strip() or (
            "请总结以下视频文稿：事无巨细，不要漏掉任何一个例子、数字和因果推论；"
            "自己的话完整转述为主，只在关键处引用讲话人的原话，用「」标出。"
        )
        user = f"""{head}

下面是一份视频文字稿：一整段口播音频的语音识别结果，按时间顺序排列，
每段开头是该段的起始时间 [HH:MM:SS]。
文稿由语音识别生成：可能完全不带标点、句子在任意位置断开、专有名词可能识别错。
以实际内容为准，不要假设。

{doc}"""
        return _SYSTEM, user

    # 模板 A（默认）：= 实测验证过的配方（事无巨细 + 挑着引用原话）
    # 篇幅目标由文稿长度算（见 length_target）：下限防"过度概括"，硬上限防"照抄文稿"。
    #
    # ⚠️ v1.5 修掉的三处硬伤（2026-09-20，万字稿实测）：
    #  1) 【任务】里原来有"宁可多写，也不要为了篇幅而合并、跳过或一笔带过"——
    #     它和篇幅上限**正面打架**，模型选了前者，输出 = 原文 115.9%。这句已删。
    #     凡是"宁可…"这种方向性的话，都会盖过数字约束，不要再加回来。
    #  2) 「整篇 6~14 节」写死，与稿子长度无关；长稿上每节被迫摊到 600+ 字，
    #     顶爆"每段 150~300 字"。现在由 section_range() 按长度算。
    #  3) **篇幅目标原来挂在【硬性要求】之外的【篇幅目标】段里，模型当它不存在**
    #     （分节流程里每节仍稳定在 65~106%）。现在字数是硬性要求第 1 条，
    #     并且写清"可以从哪里省"——只喊"不许照抄"，模型不知道砍什么，就只会加标点。
    lo, hi, cap = length_target(len(body))
    sec_lo, sec_hi = section_range(len(body))
    user = f"""下面是一份视频文字稿：一整段口播音频的语音识别结果，按时间顺序排列，
每段开头是该段的起始时间 [HH:MM:SS]。注意两点：
- 这是**一篇连续的口头讲述**，分段只是我们的技术切分，不是讲话人的章节；请按话题自己划分小节。
- 文稿由语音识别生成：可能完全不带标点、句子在任意位置断开、专有名词可能识别错。
  以实际内容为准，不要假设。

【任务】
通读全文，输出一份事无巨细的内容总结。目标是：读者只看这份总结，
就等于把视频完整看了一遍，只是省了时间——细节、例子、数字、原话的味道都还在。
**但这是一篇总结，不是把原文重打一遍**：文稿 {len(body)} 字，
你的正文要压到 {lo}~{hi} 字（硬上限 {cap} 字）。做不到这个字数就是不达标。
省字数的具体办法见下面硬性要求第 3 条。

【硬性要求】
{_requirements(with_timestamp, sec_range=(sec_lo, sec_hi),
               length=(lo, hi, cap), source_chars=len(body))}

【输出格式】
Markdown。按内容自然分成若干小节，每节格式：

### 小节标题
[HH:MM:SS]
正文（连贯的自然段；引用的原话用「」标出）

不要写「讲话人说」「引用：」「依据原话」这类前缀——正文直接写内容，引号本身就说明那是原话。
不要在开头或结尾加「总结如下」「以上就是全部内容」这类空话。

{_doc_block(body, seg_count, duration)}"""
    return _SYSTEM, user


# ---------------------------------------------------------------- 长稿流程的提示词
#
# 为什么要拆成两趟，见 `longdoc.py` 顶部的说明。这里只放措辞。


def build_outline_prompt(
    body: str, seg_count: int, duration: str, sec_lo: int, sec_hi: int
) -> tuple[str, str]:
    """第一趟：只要骨架（分段 + 标题 + 起止时间戳 + 一句话主旨），不要正文。

    输出刻意做得很短——短输出的任务几乎不会退化，所以这一趟拿到的结构是可靠的，
    而结构一旦定下来，后面每一节就都短了。
    """
    system = (
        "你是知识类视频的编辑：拿到一段口播录像的文字稿，先只做一件事——"
        "按话题的推进把它切成若干节，并给出每节的位置。你不写总结。"
    )
    user = f"""下面是一份视频文字稿，按时间顺序排列，每段开头是该段的起始时间 [HH:MM:SS]。
文稿由语音识别生成：可能完全不带标点、句子在任意位置断开、专有名词可能识别错。

【任务】
通读全文，把它按**话题的推进**切成 {sec_lo}~{sec_hi} 节。**不要写总结、不要写正文**，
只要每节的：标题、起始时间戳、结束时间戳、一句话主旨。

【硬性要求】
1. 必须覆盖全文：第一节从文稿的第一个时间戳开始，最后一节的结束时间戳等于文稿的最后一个时间戳；
   节与节首尾相接，**不许有缝、不许重叠**。
2. 时间戳只能从文稿里真实出现过的 [HH:MM:SS] 里选，不要自己编，也不要用文稿里没有的时间。
3. 节的边界要落在**话题转换**的地方（讲话人换了在讲什么），不要按等长机械切分。
4. 标题 2~12 个字，用讲话人自己的说法命名这一节在讲什么，不要摘节内第一句话。
5. 主旨一句话，说清这一节在讲什么（不要复述细节、不要评价）。
6. 不要写任何别的内容：不要前言、不要结语、不要总结、不要代码块围栏。

【输出格式】
严格每行一节，用竖线分隔，行首编号从 S01 开始，不要有别的内容：

S01 | 00:00:00 | 00:03:12 | 标题 | 一句话主旨
S02 | 00:03:12 | 00:06:40 | 标题 | 一句话主旨

{_doc_block(body, seg_count, duration)}"""
    return system, user


def build_section_prompt(
    *,
    index: int,
    total: int,
    title: str,
    outline_md: str,
    section_body: str,
    full_body: str | None,
    with_timestamp: bool,
    lo: int,
    hi: int,
    cap: int,
) -> tuple[str, str]:
    """第二趟：带着完整骨架，只写第 index 节。

    `full_body` 不为 None 时，把**全文**也一并给出。这不是浪费：模型的上下文是百万级，
    8,700 字的正文只占 0.5%，而带上全文能让本节里"前面说过的那件事"有处可查，
    也避免逐节生成时各节互相不知道对方在讲什么。文稿特别长时（见 longdoc 的阈值）
    才退化成只给本节原文。
    """
    ctx = ""
    if full_body:
        ctx = f"""
【全文】（仅供你了解上下文，**不要写进这一节**）
{full_body}
"""
    user = f"""这是一份视频文字稿的**第 {index}/{total} 节**的写作任务：整篇已经被切好，
你只负责写这一节。

【整篇骨架】（每节的标题、位置和主旨）
{outline_md}

【本节】第 {index} 节 · 标题：{title}
{ctx}
【本节原文】
{section_body}

【任务】
只写第 {index} 节的正文，标题用上面给定的那一个（原样照抄）。
你的读者看过视频、想省掉再听一遍的时间，又不接受漏掉这一节的任何一处细节。
**但这是一篇总结，不是把原文重打一遍**：本节原文 {len(section_body)} 字，
你的正文要压到 {lo}~{hi} 字（硬上限 {cap} 字）。做不到这个字数就是不达标。
省字数的具体办法见下面硬性要求第 3 条。

【硬性要求】
{_requirements(with_timestamp, scope="section",
               length=(lo, hi, cap), source_chars=len(section_body),
               source_desc="本节原文")}

【输出格式】
Markdown，只输出这一节：

### {title}
[HH:MM:SS]
正文（连贯的自然段；引用的原话用「」标出）

不要写「讲话人说」「引用：」这类前缀，不要写导语或小结，不要提别的小节。"""
    return _SYSTEM, user


# ---------------------------------------------------------------- LLM 调用


def _endpoint(base_url: str, path: str = "/chat/completions") -> str:
    base = (base_url or "").strip().rstrip("/")
    if base.endswith(path):
        return base
    return base + path


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _timeout(read: float) -> httpx.Timeout:
    return httpx.Timeout(connect=15.0, read=read, write=120.0, pool=15.0)


def _friendly_http_error(status: int, text: str) -> str:
    detail = ""
    try:
        obj = json.loads(text)
        detail = (obj.get("error") or {}).get("message") or obj.get("message") or text
    except (ValueError, AttributeError):
        detail = text
    detail = (detail or "").strip()[:400]
    if status == 401:
        return f"API Key 无效或已过期（401）。请在「设置」里检查 Key。\n{detail}"
    if status == 402:
        return f"账户余额不足（402）。\n{detail}"
    if status == 404:
        return f"接口地址或模型名不对（404）。请在「设置」里点「测试连接」确认。\n{detail}"
    if status == 429:
        return f"触发限流（429），稍后重试。\n{detail}"
    return f"接口返回 {status}：{detail}"


async def stream_chat(
    cfg: dict,
    system: str,
    user: str,
    *,
    thinking: bool | None = None,
    max_tokens: int | None = None,
) -> AsyncIterator[dict]:
    """流式调用。产出三种事件：

    - ``{"type": "reasoning", "text": ...}`` 推理模型的思考过程（deepseek-flash 会先吐这个，
      可能长达几十秒；不转发的话用户只会看到一片空白）
    - ``{"type": "delta", "text": ...}``     正式回答的增量文本
    - ``{"type": "done", "finish_reason": ...}``

    `thinking` / `max_tokens` 是**单次调用**的覆盖值（None = 用配置里的）：
    长稿流程里"骨架"那趟值得开思考（全局结构判断），"逐节展开"那趟不值得——
    实测思考把耗时从 15.7s 拉到 75~95s（**5 倍**），而每一节本来就短，不需要想那么久。
    """
    llm = cfg.get("llm") or {}
    api_key = (llm.get("api_key") or "").strip()
    if not api_key:
        raise SummarizeError(
            "尚未配置 API Key。点页面右上角「设置」，填入 DeepSeek API Key 后再试。"
        )

    payload = {
        "model": llm.get("model") or "deepseek-chat",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": True,
        "max_tokens": int(max_tokens or llm.get("max_tokens") or 8000),
        "temperature": float(llm.get("temperature", 0.3)),
    }

    # 关掉推理模型的思考（实测有效：thinking.type=disabled；reasoning_effort=none 亦可。
    # 注意 `enable_thinking` / `chat_template_kwargs` 实测**无效**）
    want_thinking = llm.get("thinking_enabled", True) if thinking is None else thinking
    if not want_thinking:
        payload["thinking"] = {"type": "disabled"}

    url = _endpoint(llm.get("base_url") or "")
    timeout = _timeout(float(llm.get("timeout_read") or 300))

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload, headers=_headers(api_key)) as resp:
                if resp.status_code != 200:
                    raw = (await resp.aread()).decode("utf-8", "replace")
                    raise SummarizeError(_friendly_http_error(resp.status_code, raw))

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue  # 跳过空行与 ": keep-alive" 注释
                    data = line[5:].strip()
                    if not data:
                        continue
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    thought = delta.get("reasoning_content")
                    if thought:
                        yield {"type": "reasoning", "text": thought}
                    text = delta.get("content")
                    if text:
                        yield {"type": "delta", "text": text}
                    if choice.get("finish_reason"):
                        yield {"type": "done", "finish_reason": choice["finish_reason"]}
    except httpx.TimeoutException as e:
        raise SummarizeError(
            f"请求超时（{type(e).__name__}）。可能是网络问题，或模型输出太长；"
            "可在「设置」里调大读取超时后重试。"
        ) from e
    except httpx.HTTPError as e:
        raise SummarizeError(
            f"网络请求失败：{type(e).__name__}: {e}\n"
            "若走代理，注意 Python 不会自动使用 Windows 系统代理。"
        ) from e


async def fetch_models(cfg: dict) -> list[str]:
    """拉取可用模型列表（设置面板「测试连接」用）。"""
    llm = cfg.get("llm") or {}
    api_key = (llm.get("api_key") or "").strip()
    if not api_key:
        raise SummarizeError("尚未配置 API Key。")
    url = _endpoint(llm.get("base_url") or "", "/models")
    try:
        async with httpx.AsyncClient(timeout=_timeout(30)) as client:
            resp = await client.get(url, headers=_headers(api_key))
            if resp.status_code != 200:
                raise SummarizeError(_friendly_http_error(resp.status_code, resp.text))
            obj = resp.json()
    except httpx.HTTPError as e:
        raise SummarizeError(f"连接失败：{type(e).__name__}: {e}") from e
    items = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        return []
    return sorted(
        str(it.get("id")) for it in items if isinstance(it, dict) and it.get("id")
    )
