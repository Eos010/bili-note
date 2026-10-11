"""长稿流程自检：**不联网、不花钱**（本地起一个假 LLM）。

跑法：`.venv\\Scripts\\python.exe selftest_longdoc.py`

覆盖：
  1. 长度评估（策略分档、压缩曲线、节数区间）
  2. 骨架解析（容错）与骨架自检
  3. 分节归属的**保真性**（拼接回来必须等于原文，一段都不能丢）
  4. 机械兜底（骨架没用时不至于整个任务失败）
  5. 质量量具（锚点/引号/段落召回能不能真的抓到问题）
  6. 端到端 SSE（走 HTTP，含断点续传：中途失败 → 第二次只补跑缺的节）

为什么断点续传要专门测：长稿会跑十几趟调用、几分钟，
跑到一半断网就全丢的话，用户要重新花一遍钱和时间——这是这套流程存在的理由之一。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
TMP_DIR = os.path.join(BASE, "output", ".tmp", "longdoc_selftest")
MOCK_PORT = 8098
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}/v1"

ok_count = 0
fail_count = 0

# 假 LLM 的可控状态
mock_state: dict = {
    "fail_at_section": 0,   # 第 N 次"写某一节"的调用返回 500（0 = 不失败）
    "section_calls": 0,
    "outline_calls": 0,
    "seen_section_targets": [],
}


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  [OK]   {name}")
    else:
        fail_count += 1
        print(f"  [FAIL] {name} {extra}")


# ---------------------------------------------------------------- 假 LLM

mock = FastAPI()

_OUTLINE_SPLIT = 6


def _timestamps(text: str) -> list[str]:
    return re.findall(r"\[(\d{2}:\d{2}:\d{2})\]", text)


@mock.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    msgs = body.get("messages") or []
    user = msgs[-1]["content"] if msgs else ""

    # ---- 骨架请求 ----
    if "严格每行一节" in user:
        mock_state["outline_calls"] += 1
        src = user.split("【文稿正文】")[-1]
        ts = _timestamps(src)
        if not ts:
            return JSONResponse({"error": {"message": "假 LLM 没找到时间戳"}}, status_code=400)
        n = min(_OUTLINE_SPLIT, max(1, len(ts)))
        idx = [round(i * (len(ts) - 1) / n) for i in range(n)] + [len(ts) - 1]
        lines = []
        for k in range(n):
            a, b = ts[idx[k]], ts[idx[k + 1]]
            lines.append(f"S{k + 1:02d} | {a} | {b} | 小节{k + 1} | 这是第{k + 1}节的主旨")
        text = "\n".join(lines) + "\n"
        return await _stream(text, reasoning="先看看文稿怎么分段……")

    # ---- 某节请求 ----
    if "这是一份视频文字稿的**第" in user:
        mock_state["section_calls"] += 1
        if mock_state["fail_at_section"] and mock_state["section_calls"] == mock_state["fail_at_section"]:
            return JSONResponse({"error": {"message": "假 LLM 故意失败"}}, status_code=500)
        m = re.search(r"### (.+?)\n", user)
        title = m.group(1).strip() if m else "小节"
        mm = re.search(r"写到 (\d+)~(\d+) 字", user)
        lo = int(mm.group(1)) if mm else 120
        mock_state["seen_section_targets"].append(lo)
        src = user.split("【本节原文】")[-1].split("【任务】")[0]
        ts = _timestamps(src)
        start = ts[0] if ts else "00:00:00"
        filler = re.sub(r"\[\d{2}:\d{2}:\d{2}\]\s*", "", src).strip()
        body_text = filler[:max(60, lo)]
        text = f"### {title}\n[{start}]\n{body_text}\n"
        return await _stream(text)

    # ---- 单趟请求（模板 A / B / C / D）----
    return await _stream("### 单趟\n[00:00:00]\n单趟总结正文。\n")


async def _stream(text: str, reasoning: str = "") -> StreamingResponse:
    async def gen():
        for p in ([reasoning] if reasoning else []):
            yield "data: " + json.dumps(
                {"choices": [{"delta": {"reasoning_content": p}, "finish_reason": None}]},
                ensure_ascii=False,
            ) + "\n\n"
        for i in range(0, len(text), 60):
            yield "data: " + json.dumps(
                {"choices": [{"delta": {"content": text[i:i + 60]}, "finish_reason": None}]},
                ensure_ascii=False,
            ) + "\n\n"
            await asyncio.sleep(0)
        yield "data: " + json.dumps(
            {"choices": [{"delta": {}, "finish_reason": "stop"}]}, ensure_ascii=False
        ) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@mock.get("/v1/models")
async def models():
    return {"data": [{"id": "mock-chat"}]}


def start_mock() -> uvicorn.Server:
    cfg = uvicorn.Config(mock, host="127.0.0.1", port=MOCK_PORT, log_level="error")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{MOCK_PORT}/v1/models", timeout=1)
            return server
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError("假 LLM 没起来")


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        event, data = None, None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if event and data:
            try:
                events.append((event, json.loads(data)))
            except json.JSONDecodeError:
                pass
    return events


# ---------------------------------------------------------------- 造文稿


def make_transcript(path: str, n_lines: int = 420) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = []
    for i in range(n_lines):
        sec = i * 3
        ts = f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
        lines.append(f"[{ts}] 第{i:03d}段测试文本甲" + "内容乙丙丁" * 3)
    body = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


# ---------------------------------------------------------------- 1. 长度评估


def test_assess() -> None:
    print("\n== 1. 长度评估 ==")
    from app import longdoc, summarizer

    short = longdoc.assess("字" * 2000, 5, "00:05:00")
    check("短稿走单趟", short.strategy == "single", f" {short.strategy}")
    check("短稿有篇幅目标", short.target_lo > 0 and short.target_cap > short.target_hi)

    long_ = longdoc.assess("字" * 8704, 25, "00:23:08")
    check("万字稿走骨架流程", long_.strategy == "outline", f" {long_.strategy}")
    check("万字稿节数区间合理", 5 <= long_.sections_lo <= long_.sections_hi <= 28,
          f" {long_.sections_lo}~{long_.sections_hi}")
    check("万字稿篇幅目标进入 30~50% 档",
          0.30 <= long_.target_lo / 8704 <= 0.50, f" {long_.target_lo / 8704:.0%}")

    huge = longdoc.assess("字" * 200_000, 500, "08:00:00")
    check("超长稿有绝对上限（不超过 2 万字）", huge.target_cap <= 20_000, f" {huge.target_cap}")
    check("超长稿压缩率明显更狠", huge.target_lo / 200_000 < 0.20,
          f" {huge.target_lo / 200_000:.0%}")

    # 曲线必须单调不增（越长的稿子压缩越狠）
    ratios = [summarizer.length_target(n)[0] / n for n in (2000, 6000, 15000, 40000, 100000)]
    check("压缩曲线随长度单调下弯", all(b <= a for a, b in zip(ratios, ratios[1:])),
          f" {[round(r, 2) for r in ratios]}")

    check("estimate_tokens 中文按 0.6/字", 590 <= longdoc.quality.estimate_tokens("字" * 1000) <= 610,
          f" {longdoc.quality.estimate_tokens('字' * 1000)}")


# ---------------------------------------------------------------- 2. 骨架解析


def test_outline_parse() -> None:
    print("\n== 2. 骨架解析（容错） ==")
    from app import longdoc

    ok = longdoc.parse_outline(
        "S01 | 00:00:00 | 00:03:12 | 开场 | 交代背景\n"
        "S02 | 00:03:12 | 00:06:40 | 方法 | 讲了三个方法\n"
    )
    check("标准格式解析出 2 节", len(ok) == 2, f" {len(ok)}")
    check("标题与主旨都取到了", ok[1].title == "方法" and ok[1].gist == "讲了三个方法")
    check("起始秒数算对了", ok[1].start_sec == 192, f" {ok[1].start_sec}")

    messy = longdoc.parse_outline(
        "```\n"
        "S01｜[00:00:00]｜[00:01:00]｜全角竖线｜也能认\n"
        "这里是一句废话，应该被跳过\n"
        "S02 | 00:01:00 | 00:02:00 | 第二节 | 主旨里 | 带竖线\n"
        "```\n"
    )
    check("容忍代码块围栏与全角竖线", len(messy) == 2, f" {len(messy)}")
    check("主旨里的竖线不被截断", "带竖线" in messy[1].gist, f" {messy[1].gist}")
    check("认不出的行被跳过而不报错", messy[0].title == "全角竖线")

    unsorted = longdoc.parse_outline(
        "S01 | 00:05:00 | 00:06:00 | 后面 | x\n"
        "S02 | 00:01:00 | 00:02:00 | 前面 | y\n"
    )
    check("骨架按时间戳重排", unsorted[0].title == "前面", f" {unsorted[0].title}")
    check("重排后编号重排", [s.index for s in unsorted] == [1, 2])

    check("空输入不崩", longdoc.parse_outline("") == [])
    check("时间戳非法的行被丢", longdoc.parse_outline("S01 | xx | yy | a | b\n") == [])


# ---------------------------------------------------------------- 3. 骨架自检


def test_outline_problems() -> None:
    print("\n== 3. 骨架自检（hard 才降级，soft 只提醒） ==")
    from app import longdoc

    segs = [("00:00:00", "a"), ("00:03:00", "b"), ("00:06:00", "c"), ("00:09:00", "d")]
    plan = longdoc.assess("字" * 5000, 4, "00:09:00")

    def split(text):
        return longdoc.outline_problems(longdoc.parse_outline(text), segs, plan)

    good = ("S01 | 00:00:00 | 00:03:00 | 一 | x\n"
            "S02 | 00:03:00 | 00:06:00 | 二 | y\n"
            "S03 | 00:06:00 | 00:09:00 | 三 | z\n")
    hard, soft = split(good)
    check("合格骨架 hard 为空", hard == [], f" {hard}")
    check("合格骨架 soft 也为空", soft == [], f" {soft}")

    # 缝/倒序/首节晚 —— 都只是 soft，绝不因此丢掉模型的语义切分
    hard, soft = split("S01 | 00:00:00 | 00:03:00 | 一 | x\nS02 | 00:04:00 | 00:09:00 | 二 | y\n")
    check("节与节有缝只是 soft（不降级）", hard == [] and any("缝" in p for p in soft),
          f" hard={hard} soft={soft}")

    hard, soft = split("S01 | 00:01:00 | 00:04:00 | 一 | x\nS02 | 00:04:00 | 00:09:00 | 二 | y\n")
    check("首节晚于开头只是 soft", hard == [] and any("晚于文稿开头" in p for p in soft),
          f" hard={hard} soft={soft}")

    # 真的会坏事的才算 hard
    hard, _ = split("S01 | 00:00:00 | 00:09:00 | 只有一节 | x\n")
    check("只切出一节是 hard", any("只切出 1 节" in p for p in hard), f" {hard}")

    hard, _ = split("S01 | 00:00:00 | 00:03:00 | 一 | x\nS02 | 00:00:00 | 00:09:00 | 二 | y\n")
    check("两节起点相同是 hard（会切出空节）", any("同一个起始时间戳" in p for p in hard), f" {hard}")

    hard, _ = longdoc.outline_problems([], segs, plan)
    check("空骨架是 hard", hard != [])

    # 单节过大 → 又变回超长生成
    huge_plan = longdoc.assess("字" * 40000, 100, "02:00:00")
    big_segs = [("00:00:00", "字" * 20000), ("01:00:00", "字" * 20000)]
    sec = longdoc.Section(index=1, title="大节", start="00:00:00", end="02:00:00",
                          gist="", start_sec=0, segs=list(big_segs))
    hard, _ = longdoc.outline_problems([sec], big_segs, huge_plan)
    check("单节超过 15000 字是 hard", any("15,000" in p for p in hard), f" {hard}")


# ---------------------------------------------------------------- 4. 分节保真


def test_assign_fidelity() -> None:
    print("\n== 4. 分节归属保真（关键） ==")
    from app import longdoc

    segs = [(f"00:0{i}:00", f"第{i}段") for i in range(10)]
    sections = longdoc.parse_outline(
        "S01 | 00:00:00 | 00:03:00 | 一 | x\n"
        "S02 | 00:03:00 | 00:06:00 | 二 | y\n"
        "S03 | 00:06:00 | 00:09:00 | 三 | z\n"
    )
    longdoc.assign_segments(segs, sections)

    total = sum(len(s.segs) for s in sections)
    check("每一段都分到了某一节（一段不丢）", total == len(segs), f" {total}/{len(segs)}")

    joined = "".join(t for s in sections for _ts, t in s.segs)
    check("拼接回来等于原文", joined == "".join(t for _ts, t in segs))

    # 骨架故意漏掉尾部：最后一段仍必须落到最后一节
    partial = longdoc.parse_outline("S01 | 00:00:00 | 00:03:00 | 一 | x\n")
    longdoc.assign_segments(segs, partial)
    check("骨架只切了一节时，所有段仍归到这一节", len(partial[0].segs) == 10,
          f" {len(partial[0].segs)}")

    # 骨架边界略偏（不在段边界上）也不能丢段
    off = longdoc.parse_outline(
        "S01 | 00:00:00 | 00:03:30 | 一 | x\n"
        "S02 | 00:03:30 | 00:09:00 | 二 | y\n"
    )
    longdoc.assign_segments(segs, off)
    check("骨架边界不落在段上也不丢段",
          sum(len(s.segs) for s in off) == 10, f" {sum(len(s.segs) for s in off)}")


def test_mechanical() -> None:
    print("\n== 5. 机械兜底 ==")
    from app import longdoc

    segs = [(f"00:0{i}:00", f"第{i}段") for i in range(12)]
    secs = longdoc.mechanical_outline(segs, "00:11:00", 4)
    check("兜底切出请求的节数", len(secs) == 4, f" {len(secs)}")
    check("兜底也不丢段", sum(len(s.segs) for s in secs) == 12)
    check("兜底后拼接仍等于原文",
          "".join(t for s in secs for _ts, t in s.segs) == "".join(t for _ts, t in segs))
    check("兜底末节延伸到文稿结束", secs[-1].end == "00:11:00", f" {secs[-1].end}")
    check("空输入不崩", longdoc.mechanical_outline([], "00:00:00", 3) == [])


# ---------------------------------------------------------------- 6. 质量量具


def test_quality_metrics() -> None:
    print("\n== 6. 质量量具 ==")
    from app import quality

    src = (
        "[00:00:00] 他提到 2026 年夏天,用了 12 年才想明白,读了《逃避自由》。\n"
        "[00:00:10] 心理学家埃里克埃里克·埃里克把人的一生分成八个阶段。\n"
        "[00:00:20] 前央叶皮层要到 25 岁左右才发育成熟。\n"
    )
    segs = [("00:00:00", "他提到 2026 年夏天,用了 12 年才想明白,读了《逃避自由》。"),
            ("00:00:10", "心理学家埃里克埃里克·埃里克把人的一生分成八个阶段。"),
            ("00:00:20", "前央叶皮层要到 25 岁左右才发育成熟。")]

    good = ("### 一\n[00:00:00]\n他等了 12 年才想明白，2026 年夏天读了《逃避自由》。\n"
            "### 二\n[00:00:10]\n八个阶段。\n### 三\n[00:00:20]\n25 岁成熟。\n")
    r = quality.measure(src, good, [t for _ts, t in segs], [ts for ts, _t in segs])
    check("时间戳不会被当成数字锚点", "00" not in r["anchors_missed"] and r["anchors_total"] >= 4,
          f" {r['anchors_total']} 个锚点 {r['anchors_missed']}")
    check("该有的锚点都在（覆盖率 100%）", r["anchor_coverage"] == 100.0,
          f" {r['anchors_missed']}")

    bad = "### 一\n[00:00:00]\n他读了一本书。\n"
    r2 = quality.measure(src, bad, [t for _ts, t in segs], [ts for ts, _t in segs])
    check("漏掉数字 → 覆盖率下降", r2["anchor_coverage"] < 70, f" {r2['anchor_coverage']}")
    check("段落召回能反映整段缺失", r2["segment_recall"] <= 40, f" {r2['segment_recall']}")

    # 引号保真：编造的 / 用背景知识"改对"的都要抓到
    fabricated = "### 一\n[00:00:00]\n他说「弥散性」和「埃里克·埃里克森」。\n"
    r3 = quality.measure(src, fabricated, [t for _ts, t in segs], [ts for ts, _t in segs])
    check("凭空编的引号被抓到", r3["quote_fidelity"] < 100, f" {r3['quote_fidelity']}")
    check("按字比对：标点差异不算作弊",
          quality.measure(src, "### 一\n[00:00:00]\n他说「埃里克埃里克·埃里克」。\n",
                          [t for _ts, t in segs], [ts for ts, _t in segs])["quote_fidelity"] == 100.0)

    # 越写越薄
    thin = "### 一\n[00:00:00]\n" + "前" * 400 + "\n### 二\n[00:00:10]\n" + "后" * 50 + "\n"
    r4 = quality.measure(src, thin, [t for _ts, t in segs], [ts for ts, _t in segs])
    check("后半变薄会被 tail_ratio 抓到", r4["tail_ratio"] < 0.8, f" {r4['tail_ratio']}")

    ts_bad = quality.measure(src, "### 一\n[00:09:99]\n不算\n", None, ["00:00:00"])
    check("非法时间戳不会被计入", ts_bad["timestamps"]["count"] == 0,
          f" {ts_bad['timestamps']['count']}")


# ---------------------------------------------------------------- 7. 端到端


def outline2_secs(events: list[tuple[str, dict]]) -> list:
    return next((d.get("sections") or [] for e, d in events if e == "outline"), [])


async def run_e2e() -> None:
    from app.main import app, manager
    from app.tasks import Task

    await manager.start()
    txt = make_transcript(os.path.join(TMP_DIR, "合成文稿.txt"))
    raw = open(txt, encoding="utf-8").read()
    print(f"\n合成文稿：{len(raw)} 字符 / {raw.count(chr(10))} 行")

    tid = "ldtest000001"
    t = Task(id=tid, url=txt, download=False, transcribe=True, kind="file")
    t.status = "done"
    t.title = "长稿流程测试"
    t.txt_path = txt
    t.output_dir = os.path.dirname(txt)
    manager.tasks[tid] = t

    summary_path = os.path.splitext(txt)[0] + ".summary.md"
    parts_dir = summary_path + ".parts"
    outline_path = os.path.splitext(txt)[0] + ".summary.outline.md"
    for p in (summary_path, outline_path):
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(parts_dir, ignore_errors=True)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=120) as client:
        # ---- 7.1 正常流 ----
        print("\n== 7.1 端到端（骨架 → 逐节） ==")
        mock_state.update({"fail_at_section": 0, "section_calls": 0, "outline_calls": 0,
                           "seen_section_targets": []})
        async with client.stream("POST", f"/api/tasks/{tid}/summarize",
                                 json={"template": "A", "with_timestamp": True}) as r:
            check("HTTP 200", r.status_code == 200)
            raw_sse = "".join([chunk async for chunk in r.aiter_text()])

        events = parse_sse(raw_sse)
        kinds = [e for e, _ in events]
        check("有 plan 事件（长度评估）", "plan" in kinds, f" {sorted(set(kinds))}")
        check("有 outline 事件（骨架）", "outline" in kinds)
        check("有 stage 事件（分节进度）", kinds.count("stage") >= 6, f" {kinds.count('stage')}")
        check("有 done 事件", "done" in kinds)
        check("没有 error 事件", "error" not in kinds,
              f" {[d for e, d in events if e == 'error']}")

        plan = next((d for e, d in events if e == "plan"), {})
        check("plan 判定为长稿走骨架", plan.get("strategy") == "outline", f" {plan.get('strategy')}")

        outline_ev = next((d for e, d in events if e == "outline"), {})
        check("骨架解析出多节", len(outline_ev.get("sections") or []) >= 3,
              f" {len(outline_ev.get('sections') or [])}")

        body = "".join(d.get("text", "") for e, d in events if e == "delta")
        check("正文非空", len(body) > 100, f" {len(body)}")
        check("正文只由各节拼成（每节都有标题）",
              body.count("### ") == len(outline_ev.get("sections") or []),
              f" {body.count('### ')} 个标题 vs {len(outline_ev.get('sections') or [])} 节")
        check("思考过程没混进正文", "先看看文稿怎么分段" not in body)

        done = next((d for e, d in events if e == "done"), {})
        check("done 带质量报告", bool(done.get("quality")), f" {done.get('quality')}")
        check("done 带骨架速览", bool(done.get("outline")))
        check("落盘路径存在", os.path.isfile(summary_path), f" {summary_path}")
        check("骨架速览单独落盘", os.path.isfile(outline_path))
        check("跑完清掉分节缓存", not os.path.isdir(parts_dir),
              f" 残留 {sorted(os.listdir(parts_dir)) if os.path.isdir(parts_dir) else ''}")

        q = done.get("quality") or {}
        check("质量报告带锚点与段落召回",
              "anchor_coverage" in q and "segment_recall" in q, f" {sorted(q)[:6]}")

        # 落盘文件里应当有流程元信息
        head = open(summary_path, encoding="utf-8").read(400)
        check("front matter 记录了策略", "strategy: outline" in head, f" {head[:120]!r}")
        check("front matter 指向骨架文件", "outline:" in head)

        # ---- 7.2 断点续传 ----
        print("\n== 7.2 断点续传（中途失败 → 只补跑缺的节） ==")
        for p in (summary_path, outline_path):
            if os.path.exists(p):
                os.remove(p)
        mock_state.update({"fail_at_section": 3, "section_calls": 0, "outline_calls": 0})
        async with client.stream("POST", f"/api/tasks/{tid}/summarize",
                                 json={"template": "A", "with_timestamp": True}) as r:
            raw_sse = "".join([chunk async for chunk in r.aiter_text()])
        ev1 = parse_sse(raw_sse)
        check("第一次跑到第 3 节失败并报错", any(e == "error" for e, _ in ev1),
              f" {[d for e, d in ev1 if e == 'error']}")
        check("失败后分节缓存被保留（能续跑）", os.path.isdir(parts_dir))
        saved = sorted(os.listdir(parts_dir)) if os.path.isdir(parts_dir) else []
        check("缓存里有已完成的节", any(x.startswith("sec") for x in saved), f" {saved}")
        check("缓存里有 manifest", "manifest.json" in saved, f" {saved}")

        first_section_calls = mock_state["section_calls"]
        first_outline_calls = mock_state["outline_calls"]

        mock_state.update({"fail_at_section": 0, "section_calls": 0, "outline_calls": 0})
        async with client.stream("POST", f"/api/tasks/{tid}/summarize",
                                 json={"template": "A", "with_timestamp": True}) as r:
            raw_sse = "".join([chunk async for chunk in r.aiter_text()])
        ev2 = parse_sse(raw_sse)
        check("第二次跑成功", any(e == "done" for e, _ in ev2) and not any(e == "error" for e, _ in ev2),
              f" {[d for e, d in ev2 if e == 'error']}")
        check("第二次不再重跑骨架（复用上次的）", mock_state["outline_calls"] == 0,
              f" 重新调用了 {mock_state['outline_calls']} 次")
        check("第二次只补跑缺的节（复用的不再调用）",
              mock_state["section_calls"] < len(ev2 and outline2_secs(ev2)),
              f" 第一次调了 {first_section_calls} 次，第二次 {mock_state['section_calls']} 次；"
              f"共 {len(outline2_secs(ev2))} 节")

        body2 = "".join(d.get("text", "") for e, d in ev2 if e == "delta")
        outline2 = next((d for e, d in ev2 if e == "outline"), {})
        check("续跑后正文仍然完整（含复用的节）",
              len(outline2.get("sections") or []) > 0
              and body2.count("### ") == len(outline2.get("sections") or []),
              f" {body2.count('### ')} 个标题 vs {len(outline2.get('sections') or [])} 节")
        check("续跑完成后缓存被清掉", not os.path.isdir(parts_dir),
              f" 残留 {sorted(os.listdir(parts_dir)) if os.path.isdir(parts_dir) else ''}")
        _ = first_outline_calls

        # ---- 7.3 短稿仍走单趟 ----
        print("\n== 7.3 短稿仍走单趟 ==")
        short_txt = make_transcript(os.path.join(TMP_DIR, "短文稿.txt"), n_lines=20)
        tid2 = "ldtest000002"
        t2 = Task(id=tid2, url=short_txt, download=False, transcribe=True, kind="file")
        t2.status = "done"
        t2.title = "短稿"
        t2.txt_path = short_txt
        t2.output_dir = os.path.dirname(short_txt)
        manager.tasks[tid2] = t2
        mock_state["section_calls"] = 0
        async with client.stream("POST", f"/api/tasks/{tid2}/summarize",
                                 json={"template": "A", "with_timestamp": True}) as r:
            raw_sse = "".join([chunk async for chunk in r.aiter_text()])
        ev3 = parse_sse(raw_sse)
        plan3 = next((d for e, d in ev3 if e == "plan"), {})
        check("短稿判定为单趟", plan3.get("strategy") == "single", f" {plan3.get('strategy')}")
        check("短稿没有发起分节调用", mock_state["section_calls"] == 0,
              f" {mock_state['section_calls']}")
        check("短稿也能出结果", any(e == "done" for e, _ in ev3))

        # ---- 7.4 骨架失败 → 机械兜底 ----
        print("\n== 7.4 骨架不可用时的兜底 ==")
        from app import longdoc
        secs = longdoc.mechanical_outline(
            [("00:00:00", "a"), ("00:01:00", "b")], "00:01:00", 4
        )
        check("骨架失败也有兜底结构", len(secs) >= 1 and sum(len(s.segs) for s in secs) == 2)

    for p in (summary_path, outline_path):
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(parts_dir, ignore_errors=True)
    manager.tasks.pop(tid, None)
    manager.tasks.pop("ldtest000002", None)
    # 合成文稿也一起清掉：`output/` 是产物目录，别让自检的东西混在里面
    # （`selftest.py` 会扫 output/ 下所有 .txt 挑最长最短，留着会污染它）
    shutil.rmtree(TMP_DIR, ignore_errors=True)


def main() -> int:
    print("bili-note 长稿流程自检（本地假 LLM，不联网不花钱）")

    test_assess()
    test_outline_parse()
    test_outline_problems()
    test_assign_fidelity()
    test_mechanical()
    test_quality_metrics()

    backup = CONFIG_PATH + ".selftest-backup"
    had_config = os.path.exists(CONFIG_PATH)
    if had_config:
        shutil.move(CONFIG_PATH, backup)
        print(f"\n（已把你的 config.json 暂存为 {os.path.basename(backup)}，跑完自动放回）")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "llm": {"base_url": MOCK_BASE, "api_key": "sk-selftest-fake",
                    "model": "mock-chat", "max_tokens": 4000},
            "summarize": {"long_doc_threshold": 4000},
        }, f, ensure_ascii=False, indent=2)

    server = start_mock()
    try:
        asyncio.run(run_e2e())
    finally:
        server.should_exit = True
        time.sleep(0.3)
        if had_config:
            shutil.move(backup, CONFIG_PATH)
        elif os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)

    print(f"\n结果：{ok_count} 通过 / {fail_count} 失败")
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
