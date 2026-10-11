"""长稿流程实跑脚本（改提示词/流程时用）：跑一份文稿、落盘、打印质量报告。

用法：
  .venv\\Scripts\\python.exe _ab_longdoc.py <txt路径> <输出前缀> [--threshold N]

和 `_ab_test.py` 的区别：`_ab_test.py` 跑的是**单趟**模板 A，这个是**完整流程**
（长度评估 → 骨架 → 逐节 → 装配），并且用 `app/quality.py` 出质量报告。

对比两个版本时看这几项，别的都是噪声：
  - 长度占比：目标区间内？（基线是 116%，等于没总结）
  - 锚点覆盖率：**压缩之后不能塌**——这才是"细节有没有丢"的硬指标
  - 引号保真：<100% 说明有编造或被"顺手改对"的原话
  - 耗费时间：长稿会明显更慢，这是分节换质量的代价
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import config as app_config  # noqa: E402
from app import longdoc, quality  # noqa: E402
from app.summarizer import read_and_merge  # noqa: E402


async def main() -> None:
    txt, prefix = sys.argv[1], sys.argv[2]
    cfg = app_config.load()
    opts: dict = {}
    if "--threshold" in sys.argv:
        opts["long_doc_threshold"] = int(sys.argv[sys.argv.index("--threshold") + 1])

    segs, body, duration = read_and_merge(txt)
    job = longdoc.Job(
        cfg=cfg, body=body, segs=segs, duration=duration,
        summary_path=prefix + ".summary.md", template="A",
        with_timestamp=True, opts=opts,
    )

    t0 = time.time()
    stage_t0 = t0
    final: dict = {}
    outline = ""
    sections = 0
    live = []

    print(f"文稿 {len(body)} 字 / {len(segs)} 段 / 时长 {duration}")
    print(f"模型 {cfg['llm'].get('model')}，max_tokens {cfg['llm'].get('max_tokens')}")
    print("-" * 72)

    async for event, data in longdoc.run(job):
        if event == "plan":
            print(f"[评估] {data['note']}")
            print(f"       策略={data['strategy']} 档位={data['tier']} "
                  f"目标 {data['target_lo']}~{data['target_hi']} 字（硬上限 {data['target_cap']}）")
        elif event == "stage":
            now = time.time()
            print(f"  [{now - t0:6.1f}s] {data.get('title') or data.get('stage')}"
                  f"（上一段 {now - stage_t0:.1f}s）")
            stage_t0 = now
        elif event == "warn":
            print(f"  ⚠ {data['message']}")
        elif event == "outline":
            outline = data.get("markdown") or ""
            sections = len(data.get("sections") or [])
            print(f"  → 骨架 {sections} 节（{'复用上次' if data.get('reused') else '新生成'}）")
        elif event == "done":
            final = data
        elif event == "delta":
            live.append(data.get("text", ""))

    seconds = time.time() - t0
    text = final.get("text") or "".join(live)
    outline = final.get("outline") or outline

    out_md = prefix + ".md"
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(text)
    if outline:
        with open(prefix + ".outline.md", "w", encoding="utf-8") as f:
            f.write(outline + "\n")

    plan = final.get("plan") or {}
    r = final.get("quality") or quality.measure(
        body, text, [t for _ts, t in segs], [ts for ts, _t in segs]
    )
    report = "\n".join([
        "-" * 72,
        f"耗时 {seconds:.1f}s（{sections} 节 + 1 趟骨架）",
        f"目标 {plan.get('target_lo')}~{plan.get('target_hi')} 字",
        quality.format_report(r, label=os.path.basename(out_md)),
        f"→ {out_md}",
    ])
    print(report)
    with open(prefix + ".stats.txt", "w", encoding="utf-8") as f:
        f.write(report + "\n")


if __name__ == "__main__":
    asyncio.run(main())
