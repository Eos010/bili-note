"""总结质量量具：**不用 LLM 裁判**，纯本地、可复现、零成本。

## 为什么要有这个文件

v1.4 那轮校准只量了格式指标（字数、引用占比、小节数），结论是"改好了"；可是
在万字稿上实测，输出 9,765 字 = 原文 **112%**、引用只剩 4.4%、引号里一半不是原话——
**每一项格式指标都"正常"，实际是把稿子重写了一遍还把原味丢了**。
只量格式指标就会得出错误结论，所以这里量的是**内容**。

## 指标（都在同一份文稿上做版本间对比才有意义）

1. `anchor_coverage` 具体锚点覆盖率 —— 数字/年份/百分比/拉丁词/《》书名。
   **高精度、与压缩率无关**：一个数字要么活下来要么没有，不会因为"换个说法"误判。
   这是最硬的指标，也是主指标。
2. `segment_recall` 段落级召回 —— 原文每一段（合并后的段）里挑 8 个特征四字组，
   只要有一个出现在总结里，就算这一段"有着落"。
   **代理指标**：如果一段被完全跳过，这里必然掉；但同义改写也会让它偏低，所以只看相对值。
   它直接对应提示词里那句自检——"有没有哪一段话在总结里找不到对应"。
3. `quote_fidelity` 引号保真度 —— 总结里「」中的话是否逐字来自原文。
   这是**正确性**不是覆盖：提示词要求"引用必须逐字取自文字稿"，这条必须能被验证。
4. `section_balance` 分节字数分布（后半均值 / 前半均值）—— 检测"越写越薄"的退化。

外加 `timestamps`：总结里的时间戳是否真实存在、是否单调、是否重复。

## 两个坑（都踩过，别再踩）

- **必须先把 `[HH:MM:SS]` 从两边剥掉再比**。否则 `[00:00:32]` 里的 `32` 会被当成
  数字锚点，指标直接变成噪声（第一版就是这么错的：41 个"锚点"里一半是时间戳碎片）。
- **不要用"特征短语存活率"当指标**。输出越像照抄，这个值越高——它在奖励抄袭，
  方向是反的。改用段落级召回（只看"有没有着落"，不看"用了多少原词"）。
- **引用保真要按"字"比，不能按"标点"比**。whisper 原文**零标点**，而提示词又要求
  输出"补上必要标点让它可读"——两边同时满足是不可能的。所以比对前统一去掉标点与空白。
"""
from __future__ import annotations

import re
import sys
from typing import Iterable

# 中日韩统一表意文字（含扩展 A 与兼容区）
CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK_RE = re.compile(f"[{CJK}]")
_TS_RE = re.compile(r"\[(\d{1,2}):([0-5]\d):([0-5]\d)\]")
_TS_ANY_RE = re.compile(r"\[\d{1,2}:[0-5]\d:[0-5]\d\]\s?")

# 数字后面跟这些字才算"具体锚点"（否则 "1" "2" 这种孤独数字是噪声）
_UNITS = "年个月日天周时秒分次种个条点步层类位名人页本节章倍万亿千百十元块百分点套款项目阶段"

# 中文数词（口播稿里"12年"和"十二年"混着出现，两种都要算锚点）
_CN_DIGIT = {
    "一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5",
    "六": "6", "七": "7", "八": "8", "九": "9", "十": "10", "百": "100",
    "千": "1000", "万": "10000",
}
_CN_NUM_RE = re.compile(rf"([一二三四五六七八九十百千万两]{{1,3}})([{_UNITS}])")

# 四字组里出现任意一个就丢掉——这些是虚词/高频功能字，含它们的四字组不是"特征短语"
_FUNC_CHARS = set(
    "的了是我你他她它们在有和就都而及与这那不一也很到说要做为对会能可以什么怎么"
    "因为所以但是不过如果还是这样那样时候没知道觉得可能应该把被给让从向上下里外"
    "前后中大小多少好最更再又还只才已经并且或者以及然后于是其实真的确实"
)

# 比对引号保真时忽略的标点与空白（原文零标点，模型会补标点，不该算作弊）
_PUNCT_RE = re.compile(r"[\s，。、；：？！「」『』“”‘’（）()《》〈〉,.;:?!'\"\-—…·~]")

# 〔疑为「XX」〕：提示词要求模型标注的"疑似识别错误"。里面的引号是**正确写法**，
# 不是"冒充原话"，算保真时要整段排除。
_ANNOT_RE = re.compile(r"〔[^〕]*〕")

_TOP_PHRASES = 8

# 主指标只认这三类锚点——它们**换了说法也还是那个东西**，不会误判（见 measure() 里的说明）
_HARD_KINDS = {"数字", "拉丁词", "书名"}


def _strip_ts(text: str) -> str:
    """剥掉 `[HH:MM:SS]` 标记——两边都要剥，否则时间戳里的数字会污染锚点指标。"""
    return _TS_ANY_RE.sub("", text)


def _norm(s: str) -> str:
    """去掉标点与空白，只留字。"""
    return _PUNCT_RE.sub("", s)


class _Anchor:
    """一个待检查的锚点。`pat` 是匹配总结用的正则。"""

    __slots__ = ("text", "kind", "pat")

    def __init__(self, text: str, kind: str, pat: re.Pattern) -> None:
        self.text = text
        self.kind = kind
        self.pat = pat


def _num_pattern(num: str) -> re.Pattern:
    """数字锚点：前后不能再有数字，否则 "12" 会被 "2012" 命中。"""
    return re.compile(rf"(?<!\d){re.escape(num)}(?!\d)")


def extract_anchors(text: str) -> list[_Anchor]:
    """从原文抽"丢了就是真丢了"的具体锚点。

    三类：数字（含单位/百分比）、拉丁词、`《》` 里的书名。
    中文专有名词不做——没有分词器时误判率太高，宁缺毋滥。
    """
    out: list[_Anchor] = []
    seen: set[tuple[str, str]] = set()

    # 1) 数字：两位以上，或任意位数但紧跟量词（"4种" 也算）
    for m in re.finditer(rf"(\d+(?:\.\d+)?)(%|[{_UNITS}])?", text):
        num, unit = m.group(1), m.group(2) or ""
        digits = num.replace(".", "")
        if len(digits) < 2 and not unit:
            continue
        key = ("num", num)
        if key in seen:
            continue
        seen.add(key)
        out.append(_Anchor(f"{num}{unit}", "数字", _num_pattern(num)))

    # 1b) 中文数词 + 量词（"四年" "两种" "二十年"）。
    #     光看阿拉伯数字会漏掉一大半——口播稿里"四年"这种写法很常见，而它正是视频标题里的关键词。
    #     但要过滤掉"一个/一种/一件"这类高频虚指：单独一个"一"开头的全部丢掉。
    #     ⚠️ 先折叠重复字：ASR 偶尔吐"一一个"这种，“折叠后是"一个"→ 应当被过滤掉，
    #     否则它会变成一条永远命不中的垃圾锚点（第一版就漏了这个，白白拉低覆盖率）。
    for m in _CN_NUM_RE.finditer(text):
        cn, unit = m.group(1), m.group(2)
        cn = re.sub(r"(.)\1+", r"\1", cn)
        if cn == "一":
            continue
        key = ("cnnum", cn + unit)
        if key in seen:
            continue
        seen.add(key)
        alts = [_CN_DIGIT[cn] + unit] if len(cn) == 1 else []
        pat = "|".join(re.escape(x) for x in [cn + unit, *alts])
        out.append(_Anchor(cn + unit, "中文数词", re.compile(pat)))

    # 2) 拉丁词（AI、Kaggle、PPT…）：匹配时忽略大小写
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9\-_]{1,}", text):
        word = m.group(0)
        key = ("latin", word.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(_Anchor(word, "拉丁词", re.compile(re.escape(word), re.I)))

    # 3) 《书名/作品名》
    for m in re.finditer(r"《([^》]{1,30})》", text):
        inner = m.group(1)
        key = ("title", inner)
        if key in seen:
            continue
        seen.add(key)
        out.append(_Anchor(f"《{inner}》", "书名", re.compile(re.escape(inner))))

    return out


def _grams(text: str) -> list[str]:
    """文本里所有不含虚词的 CJK 四字组（按出现顺序）。"""
    out: list[str] = []
    for run in re.findall(f"[{CJK}]+", text):
        for i in range(len(run) - 3):
            g = run[i:i + 4]
            if not (_FUNC_CHARS & set(g)):
                out.append(g)
    return out


def segment_recall(source_segments: Iterable[str], summary: str) -> dict:
    """段落级召回：原文每一段是否有"着落"。

    做法：整篇统计四字组频次；每一段取**最稀有**的 8 个（稀有的更具体、更能代表这段），
    只要其中一个出现在总结里，这段就算被覆盖。
    """
    segs = [s for s in source_segments if s and s.strip()]
    if not segs:
        return {"segments": 0, "covered": 0, "recall": 0.0, "orphans": []}

    df: dict[str, int] = {}
    per_seg: list[list[str]] = []
    for s in segs:
        gs = _grams(s)
        per_seg.append(gs)
        for g in set(gs):
            df[g] = df.get(g, 0) + 1

    covered = 0
    orphans: list[str] = []
    for s, gs in zip(segs, per_seg):
        uniq = sorted(set(gs), key=lambda g: (df.get(g, 0), g))
        picks = uniq[: _TOP_PHRASES] or gs[: _TOP_PHRASES]
        if any((p in summary) or (p in _norm(summary)) for p in picks):
            covered += 1
        else:
            orphans.append(s[:24])

    return {
        "segments": len(segs),
        "covered": covered,
        "recall": round(100.0 * covered / len(segs), 1),
        "orphans": orphans[:10],
    }


def _section_chars(summary: str) -> list[int]:
    """按 `###` 小节切，返回每节正文字数（不含标题行与时间戳行）。"""
    sizes: list[int] = []
    cur: list[str] = []
    for line in summary.splitlines():
        if line.startswith("#"):
            if cur:
                sizes.append(sum(len(x) for x in cur))
                cur = []
            continue
        s = line.strip()
        if not s or _TS_RE.fullmatch(s):
            continue
        cur.append(s)
    if cur:
        sizes.append(sum(len(x) for x in cur))
    return sizes


def check_timestamps(summary: str, valid: Iterable[str] | None = None) -> dict:
    """总结里的时间戳：是否真实存在、是否单调、是否重复。"""
    found = _TS_RE.findall(summary)
    secs = [int(h) * 3600 + int(m) * 60 + int(s) for h, m, s in found]
    texts = [f"{h}:{m}:{s}" for h, m, s in found]

    unknown: list[str] = []
    if valid:
        allowed = {v.strip("[]") for v in valid}
        unknown = [t for t in texts if t not in allowed]

    backwards = sum(1 for a, b in zip(secs, secs[1:]) if b < a)
    return {
        "count": len(texts),
        "first": texts[0] if texts else "",
        "last": texts[-1] if texts else "",
        "not_in_source": unknown,
        "backwards": backwards,
        "duplicates": len(texts) - len(set(texts)),
    }


def measure(
    source_body: str,
    summary: str,
    segments: Iterable[str] | None = None,
    valid_ts: Iterable[str] | None = None,
) -> dict:
    """核心入口。`segments` 是合并后的各段正文（用于段落级召回）。"""
    src = _strip_ts(source_body)
    sm = _strip_ts(summary)

    anchors = extract_anchors(src)
    # **主指标只算"改写也不会误判"的锚点**：阿拉伯数字 / 拉丁词 / 《》书名。
    # 中文数词（"第二种""三项"）单独算：它们在**枚举**语境里会被合理地改写成别的说法
    # （2026-09-20 实测：新流程把四种身份状态一个不落地写全了，只是没写"第二种"这三个字，
    # 主指标却判它"漏了"——那是量具的假阳性，不是内容丢了）。把它混进主指标会误导判断。
    hard = [a for a in anchors if a.kind in _HARD_KINDS]
    soft = [a for a in anchors if a.kind not in _HARD_KINDS]
    hit = [a for a in hard if a.pat.search(sm)]
    miss = [a for a in hard if not a.pat.search(sm)]
    soft_miss = [a.text for a in soft if not a.pat.search(sm)]

    # 引号保真：按"字"比，忽略标点（原文零标点，模型补标点不算错）
    # ⚠️ 先把〔疑为「XX」〕整段排除掉：那是提示词**要求**模型标注的疑似识别错误
    # （比如原文是 ASR 错的「埃里克埃里克·埃里克」，正确写法本来就该在引号里），
    # 它不是"冒充原话"，不该算保真失败。（2026-09-20 实测踩过这个假阳性。）
    quotes = [
        q for q in re.findall(r"「([^」]*)」", _ANNOT_RE.sub("", summary)) if q.strip()
    ]
    src_norm = _norm(src)
    bad_quotes = [q for q in quotes if _norm(q) not in src_norm]

    seg = segment_recall(segments or [], sm)

    sizes = _section_chars(summary)
    mid = len(sizes) // 2
    front = (sum(sizes[:mid]) / mid) if mid else 0.0
    back = (sum(sizes[mid:]) / max(1, len(sizes) - mid)) if sizes else 0.0

    quote_chars = sum(len(q) for q in quotes)
    return {
        "summary_chars": len(summary),
        "source_chars": len(src),
        "pct_of_source": round(100.0 * len(summary) / max(1, len(src)), 1),
        "anchors_total": len(hard),
        "anchors_hit": len(hit),
        "anchor_coverage": round(100.0 * len(hit) / max(1, len(hard)), 1),
        "anchors_missed": [a.text for a in miss][:40],
        "numerals_total": len(soft),
        "numerals_hit": len(soft) - len(soft_miss),
        "numerals_missed": soft_miss[:12],
        "segment_recall": seg["recall"],
        "segments": seg["segments"],
        "segments_covered": seg["covered"],
        "orphan_segments": seg["orphans"],
        "quotes": len(quotes),
        "quote_pct": round(100.0 * quote_chars / max(1, len(summary)), 1),
        "quote_fidelity": (
            round(100.0 * (len(quotes) - len(bad_quotes)) / len(quotes), 1)
            if quotes else 100.0
        ),
        "bad_quotes": bad_quotes[:5],
        "sections": len(sizes),
        "section_sizes": sizes,
        "tail_ratio": round(back / front, 2) if front else 0.0,
        "timestamps": check_timestamps(summary, valid_ts),
    }


def format_report(r: dict, label: str = "") -> str:
    """把人看的报告排成几行。"""
    ts = r["timestamps"]
    lines = []
    if label:
        lines.append(f"[{label}]")
    lines += [
        f"  长度    {r['summary_chars']} 字 / 原文 {r['source_chars']} 字 = {r['pct_of_source']}%",
        f"  锚点    {r['anchors_hit']}/{r['anchors_total']} = {r['anchor_coverage']}%"
        f"   ← 主指标（数字/年份/拉丁词/书名，改写也不会误判）",
        f"  数词    {r['numerals_hit']}/{r['numerals_total']}"
        f" 参考值（中文数词，枚举改写会误判，别拿它下结论）",
        f"  段落    {r['segments_covered']}/{r['segments']} = {r['segment_recall']}%"
        f"   ← 代理指标（有哪几段完全没着落）",
        f"  引用    {r['quotes']} 处、占正文 {r['quote_pct']}%、逐字保真 {r['quote_fidelity']}%",
        f"  结构    {r['sections']} 节 | 后半/前半 字数比 {r['tail_ratio']}"
        f"（<0.8 说明越写越薄）",
        f"  时间戳  {ts['count']} 个 | 不在原文 {len(ts['not_in_source'])}"
        f" | 倒序 {ts['backwards']} | 重复 {ts['duplicates']}",
    ]
    if r["anchors_missed"]:
        lines.append("  漏掉的锚点：" + "、".join(r["anchors_missed"][:20]))
    if r["orphan_segments"]:
        lines.append("  没着落的段：" + " | ".join(r["orphan_segments"][:5]))
    if r["bad_quotes"]:
        lines.append("  ⚠ 引号里不是原文：" + " | ".join(r["bad_quotes"][:3]))
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """粗估 token：DeepSeek 口径——中文 1 字 ≈ 0.6 token，英文 1 字符 ≈ 0.3 token。

    只用来决定"要不要走多趟流程"和"给用户看预计规模"，不追求精确。
    """
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return int(cjk * 0.6 + other * 0.3)


def _main(argv: list[str]) -> int:
    """命令行：python -m app.quality <文稿.txt> <总结.md>"""
    import os

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.summarizer import read_and_merge

    txt, md = argv[1], argv[2]
    segs, body, _dur = read_and_merge(txt)
    with open(md, encoding="utf-8") as f:
        summary = f.read()
    r = measure(body, summary, [t for _ts, t in segs], [ts for ts, _t in segs])
    print(format_report(r, label=os.path.basename(md)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
