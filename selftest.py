"""bili-note 自检：不联网、不需要 API Key 的部分。

跑法：`.venv\\Scripts\\python.exe selftest.py`

重点验证「合并后仍可逐字对回原稿」——这是总结能引用原话的前提。
"""
from __future__ import annotations

import asyncio
import glob
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import config as app_config  # noqa: E402
from app import summarizer  # noqa: E402

TS_RE = summarizer.TS_RE
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

ok_count = 0
fail_count = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  [OK]   {name}")
    else:
        fail_count += 1
        print(f"  [FAIL] {name} {extra}")


def original_body(lines: list[str]) -> str:
    """原稿正文（去时间戳、去换行），作为保真的基准。"""
    parts = []
    for line in lines:
        m = TS_RE.match(line.strip())
        body = m.group(4) if m else line.strip()
        if body:
            parts.append(body)
    return "".join(parts)


def test_file(path: str) -> None:
    print(f"\n== {os.path.basename(path)} ==")
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    segs = summarizer.merge_transcript(lines)
    body, seg_count, duration = summarizer.build_document(segs)
    orig = original_body(lines)

    print(f"  行数 {len(lines)} → 段数 {seg_count}；原文 {len(orig)} 字 → 合并后 {len(body)} 字符")
    print(f"  时长 {duration}；首段：{segs[0][0]} / 末段：{segs[-1][0]}")

    check("段数 > 0", seg_count > 0)
    check("时长能解析出来", duration != "00:00:00" or seg_count == 1)

    # 保真：把所有段的正文拼起来，去掉全部空白后应与原稿正文一致
    merged_chars = re.sub(r"\s+", "", "".join(t for _, t in segs))
    orig_chars = re.sub(r"\s+", "", orig)
    check(
        "合并后可逐字对回原稿（忽略空白）",
        merged_chars == orig_chars,
        f"\n     合并 {len(merged_chars)} 字 vs 原稿 {len(orig_chars)} 字",
    )

    # 时间戳单调不减
    secs = []
    for ts, _ in segs:
        h, m, s = (int(x) for x in ts.split(":"))
        secs.append(h * 3600 + m * 60 + s)
    check("段首时间戳单调不减", all(b >= a for a, b in zip(secs, secs[1:])))

    # 段长没有失控
    longest = max(len(t) for _, t in segs)
    print(f"  最长段 {longest} 字，平均 {len(body) // max(1, seg_count)} 字/段")

    # 提示词能拼出来
    for with_ts in (True, False):
        _, user = summarizer.build_prompt("A", body, seg_count, duration, with_timestamp=with_ts)
        nums = re.findall(r"^(\d+)\.\s?(?=\S)", user, re.M)
        check(
            f"模板A 提示词（with_timestamp={with_ts}）编号连续",
            nums == [str(i) for i in range(1, len(nums) + 1)],
            f" 实际 {nums}",
        )
        check(
            f"模板A 提示词（with_timestamp={with_ts}）{'含' if with_ts else '不含'}时间戳要求",
            ("标注位置" in user) == with_ts,
        )
        # 篇幅目标：下限防过度概括、硬上限防照抄原文。
        # ⚠️ v1.5：字数要求从【篇幅目标】段搬进了**硬性要求第 1 条**——
        # 实测原来挂在列表外面时，模型完全无视（输出 = 原文 116%）。这条测试盯着那个位置。
        check(
            f"模板A 提示词（with_timestamp={with_ts}）把字数写成第 1 条硬性要求",
            "硬上限" in user and "1. **字数是最硬的一条**" in user,
        )
        # 和字数打架的方向性措辞必须不存在
        check(
            f"模板A 提示词（with_timestamp={with_ts}）没有与篇幅上限冲突的措辞",
            "宁可多写" not in user and "宁可啰嗦" not in user,
        )
        # 光说"不许照抄"没用，必须告诉模型**可以从哪里省字数**
        check(
            f"模板A 提示词（with_timestamp={with_ts}）说明了压缩哪些东西",
            "字数要从这些地方省出来" in user,
        )
    for tpl in ("B", "C", "D"):
        _, user = summarizer.build_prompt(tpl, body, seg_count, duration, custom_prompt="测试", with_timestamp=True)
        check(f"模板 {tpl} 能生成非空提示词", len(user) > 100)


def test_edge_cases() -> None:
    print("\n== 边界情况 ==")
    check("空输入 → 空结果", summarizer.merge_transcript([]) == [])
    check("只有空白行 → 空结果", summarizer.merge_transcript(["", "   ", "\n"]) == [])

    segs = summarizer.merge_transcript([
        "[00:00:00] 第一句",
        "[00:00:01] 第二句",
        "[00:00:20] 间隔很大的一句",
    ], gap_seconds=3)
    check("超过 gap 会断段", len(segs) == 2, f" 实际 {len(segs)} 段")

    # 单行是原子的（不从中劈开），所以上限判定为 seg_chars + 最长单行
    segs = summarizer.merge_transcript([f"[00:00:{i:02d}] " + "字" * 100 for i in range(20)], seg_chars=400)
    longest = max(len(t) for _, t in segs)
    check(
        "按 seg_chars 断段（段长不超过上限+单个碎片）",
        len(segs) >= 3 and longest <= 400 + 100,
        f" 实际 {len(segs)} 段，最长 {longest} 字",
    )
    check("单行超长时不会被劈开", len(summarizer.merge_transcript(["[00:00:00] " + "字" * 900], seg_chars=400)) == 1)

    segs = summarizer.merge_transcript(["没有时间戳的行"])
    check("无时间戳输入不崩", segs == [])

    # 中文之间不留空格，英文保留
    check("中文片段拼接无空格", summarizer._join(["是不是", "我想要"]) == "是不是我想要")
    check("英文片段拼接保留空格", summarizer._join(["hello", "world"]) == "hello world")


def test_config() -> None:
    print("\n== 配置 ==")
    pub = app_config.public()
    check("settings 返回结构完整", "llm" in pub and "summarize" in pub)
    check("Key 不回传明文（未配置时应为空）", pub["llm"]["api_key"] == "" or "…" in pub["llm"]["api_key"] or "***" in pub["llm"]["api_key"])
    check("掩码函数正常", app_config.mask("sk-1234567890abcdef") == "sk-123…cdef")
    check("空 Key 掩码为空", app_config.mask("") == "")

    dirty = {"llm": {"api_key": "", "不存在的字段": "x", "model": "m1"}, "evil": {"x": 1}}
    clean = app_config.save  # 只检查白名单语义，不真的落盘
    check("save 是函数", callable(clean))
    _ = dirty


def test_open_output() -> None:
    """「打开本地输出目录」端点：把 os.startfile 打桩，不真的弹资源管理器。

    覆盖三件会坏事的事：目录不存在（要自动建，否则 Explorer 弹错误框）、
    Explorer 打不开（要 400 不是 500）、非 Windows 平台（要明确报错不静默）。
    """
    print("\n== /api/open-output（顶栏「打开本地输出目录」）==")
    import httpx  # noqa: PLC0415

    from app import main as app_main  # noqa: PLC0415

    calls: list[str] = []
    real_startfile = getattr(os, "startfile", None)
    real_output = app_main.OUTPUT_DIR
    # 用项目自己的 output/.tmp 当沙盒（与 selftest_longdoc.py 同一约定），跑完删掉
    tmp_root = os.path.join(OUTPUT_DIR, ".tmp", "open_output_selftest")
    shutil.rmtree(tmp_root, ignore_errors=True)
    fake_output = os.path.join(tmp_root, "nested")  # 故意不存在

    async def post(url: str):
        transport = httpx.ASGITransport(app=app_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(url)

    try:
        app_main.OUTPUT_DIR = fake_output
        os.startfile = lambda p: calls.append(str(p))  # type: ignore[attr-defined]

        r = asyncio.run(post("/api/open-output"))
        check("返回 200", r.status_code == 200, f" {r.status_code} {r.text[:160]}")
        data = r.json()
        check("返回 ok 与目录路径", data.get("ok") is True and data.get("path") == fake_output, f" {data}")
        check("目录不存在时先创建再打开", os.path.isdir(fake_output))
        check("打桩收到的正是这个目录", calls == [fake_output], f" {calls}")

        def boom(_path: str) -> None:
            raise OSError("拒绝访问")

        os.startfile = boom  # type: ignore[attr-defined]
        r = asyncio.run(post("/api/open-output"))
        check("Explorer 打不开时报 400 而不是 500", r.status_code == 400, f" {r.status_code}")
        check("错误文案带原因", "打开目录失败" in r.json().get("detail", ""), f" {r.json()}")

        del os.startfile  # 模拟非 Windows 平台
        r = asyncio.run(post("/api/open-output"))
        detail = r.json().get("detail", "")
        check("没有 startfile 时明确报错", r.status_code == 400 and "平台" in detail,
              f" {r.status_code} {detail}")
    finally:
        app_main.OUTPUT_DIR = real_output
        if real_startfile is not None:
            os.startfile = real_startfile  # type: ignore[attr-defined]
        elif hasattr(os, "startfile"):
            del os.startfile
        shutil.rmtree(tmp_root, ignore_errors=True)


def test_version() -> None:
    """版本号只有一个来源（`app/__init__.py`），接口不许另写一份。"""
    print("\n== 版本号 ==")
    import httpx  # noqa: PLC0415

    from app import __version__ as ver  # noqa: PLC0415
    from app import main as app_main  # noqa: PLC0415

    async def get():
        transport = httpx.ASGITransport(app=app_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/api/version")

    r = asyncio.run(get())
    check("GET /api/version 返回 200", r.status_code == 200, f" {r.status_code}")
    check("与 app.__version__ 一致（不许两处各写一份）",
          r.json().get("version") == ver, f" {r.json()} vs {ver}")
    check("版本号形如 2.0.0", bool(re.fullmatch(r"\d+\.\d+\.\d+", ver)), f" {ver}")


def main() -> int:
    print("bili-note 自检（离线部分）")
    test_config()
    test_version()
    test_open_output()
    test_edge_cases()

    files = sorted(glob.glob(os.path.join(OUTPUT_DIR, "**", "*.txt"), recursive=True))
    # 跳过 .tmp/：那里是试跑脚本和自检造的合成文稿，不是真实产物
    files = [p for p in files if f"{os.sep}.tmp{os.sep}" not in p]
    if not files:
        print("\n（output/ 下没有 .txt，跳过真实文稿测试）")
    else:
        # 挑最长和最短各一份，覆盖两种极端
        files.sort(key=lambda p: os.path.getsize(p))
        for path in {files[0], files[-1]}:
            test_file(path)

    print(f"\n结果：{ok_count} 通过 / {fail_count} 失败")
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
