"""A/B 试跑脚本（自包含，改提示词时用）：跑一份文稿、落盘、打印质量指标。

用法：
  .venv\\Scripts\\python.exe _ab_test.py <txt路径> <输出前缀> [--nothink] [--maxtok N]

会写出 <前缀>.md 和 <前缀>.stats.txt，并打印：
  长度占比 / 锚点覆盖率 / 段落召回 / 引用占比与保真 / 分节分布 / 时间戳
改归档的 `app/summarizer.py` 模板 A 时，用它对比"改之前 vs 改之后"，别凭感觉。

⚠️ 2026-09-20：这里原来只量格式指标（字数/引用占比/小节数），于是 v1.4 得出了
**错误结论**——格式全对，内容却在照抄（输出 = 原文 116%，还有 3 处引号是编的）。
指标已换成 `app/quality.py` 的内容指标。**主指标是锚点覆盖率**：它与压缩率无关，
压到 40% 还能保住 95%，才叫没丢细节。

跑**长稿完整流程**（长度评估 → 骨架 → 逐节）请用 `_ab_longdoc.py`。
"""
import asyncio
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import config as app_config
from app import quality
from app.summarizer import build_prompt, read_and_merge, stream_chat


def fmt_metrics(text: str) -> dict:
    """格式层面的补充指标（内容指标在 app/quality.py）。"""
    paras = [
        len(x) for x in text.split("\n")
        if x and not x.startswith("#") and not x.startswith("[")
    ]
    return {
        "paragraphs": len(paras),
        "longest_para": max(paras) if paras else 0,
        "curly_quotes": len(re.findall(r"[“”]", text)),
    }


async def main() -> None:
    txt, prefix = sys.argv[1], sys.argv[2]

    cfg = app_config.load()
    if "--nothink" in sys.argv:
        cfg["llm"]["thinking_enabled"] = False
    if "--maxtok" in sys.argv:
        cfg["llm"]["max_tokens"] = int(sys.argv[sys.argv.index("--maxtok") + 1])

    segs, body, duration = read_and_merge(txt)
    system, user = build_prompt("A", body, len(segs), duration)

    t0 = time.time()
    chunks, thoughts, finish = [], [], ""
    async for ev in stream_chat(cfg, system, user):
        if ev["type"] == "reasoning":
            thoughts.append(ev["text"])
        elif ev["type"] == "delta":
            chunks.append(ev["text"])
        elif ev["type"] == "done":
            finish = ev.get("finish_reason") or ""
    text = "".join(chunks)
    seconds = time.time() - t0

    out_md = f"{prefix}.md"
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(text)

    m = fmt_metrics(text)
    q = quality.measure(body, text, [t for _ts, t in segs], [ts for ts, _t in segs])
    report = (
        f"文稿 {len(body)} 字 / {len(segs)} 段，覆盖 {duration}\n"
        f"模型 {cfg['llm']['model']}，思考 "
        f"{'开' if cfg['llm'].get('thinking_enabled', True) else '关'}，"
        f"max_tokens {cfg['llm']['max_tokens']}\n"
        f"耗时 {seconds:.1f}s，思考 {sum(len(x) for x in thoughts)} 字，"
        f"finish_reason={finish}{'  ⚠️ 被 max_tokens 截断！' if finish == 'length' else ''}\n"
        f"段落 {m['paragraphs']} | 最长段 {m['longest_para']} | 弯引号 {m['curly_quotes']} 处\n"
        + quality.format_report(q)
        + f"\n→ {out_md}"
    )
    print(report)
    with open(f"{prefix}.stats.txt", "w", encoding="utf-8") as f:
        f.write(report + "\n")


if __name__ == "__main__":
    asyncio.run(main())
