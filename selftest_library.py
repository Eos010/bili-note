"""bili-note 自检：`output/` 已有文稿的自动检索（`app/library.py` + `/api/library`）。

跑法：`.venv\\Scripts\\python.exe selftest_library.py`

这份自检盯的是**扫描规则的边界**和**只读接口的安全边界**，两件事都会安静地坏：
- 扫描放宽一点，`output/.tmp/` 里 AB 试跑的 `.stats.txt` 就会冒充文稿混进清单；
- 接口一旦改成"前端发路径"，路径穿越那一类问题立刻成立。
所以下面既有"该认出来的要认出来"，也有"不该认出来的一个都不许进来"。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import library  # noqa: E402
from app import tasks  # noqa: E402
from app import main as app_main  # noqa: E402

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


def write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def make_transcript(path: str, n: int = 4) -> str:
    lines = [f"[00:00:{i:02d}] 第{i}句正文" for i in range(n)]
    return write(path, "\n".join(lines))


def build_sandbox(root: str) -> dict:
    """造一个假 output/：三种"该认"和四种"不该认"各来一份。"""
    shutil.rmtree(root, ignore_errors=True)

    # ---- 该认出来的 ----
    good = make_transcript(os.path.join(root, "真文稿", "真文稿.txt"), 5)
    write(os.path.join(root, "真文稿", "真文稿.srt"), "1\n00:00:00,000 --> 00:00:01,000\n第0句正文\n")
    write(os.path.join(root, "真文稿", "真文稿.summary.md"), "---\nsource: x\n---\n\n总结正文")
    write(os.path.join(root, "真文稿", "真文稿.summary.outline.md"), "- [00:00:00] **标题** —— 主旨")
    write(os.path.join(root, "真文稿", "真文稿.mp4"), "\x00" * 64)

    # 只有文稿、没视频没总结（"仅转写"模式长这样）
    only_txt = make_transcript(os.path.join(root, "只有文稿", "只有文稿.txt"), 3)

    # ---- 不该认出来的 ----
    # ① 目录名带下划线（内部目录）
    make_transcript(os.path.join(root, "_内部", "带时间戳也白搭.txt"), 4)
    # ② 目录名带点（.tmp 试跑区）
    make_transcript(os.path.join(root, ".tmp", "带时间戳也白搭.txt"), 4)
    # ③ 文件名以 .stats.txt 结尾（AB 试跑产物，即使内容带时间戳也不是文稿）
    make_transcript(os.path.join(root, "试跑", "ab_安慰.stats.txt"), 4)
    # ④ 不是转写格式（说明文件）
    write(os.path.join(root, "杂项", "说明.txt"), "这是一份说明\n没有任何时间戳\n也没有正文\n")
    # ⑤ 行数不够
    make_transcript(os.path.join(root, "太短", "太短.txt"), 2)

    return {
        "good": good,
        "good_dir": os.path.join(root, "真文稿"),
        "only_txt": only_txt,
    }


def test_scan(sandbox_root: str, made: dict) -> dict:
    print("\n== 扫描规则 ==")
    items = library.scan(sandbox_root)
    titles = {i["title"] for i in items}

    check("扫到 2 份（该认的都认了）", len(items) == 2, f" 实际 {len(items)}：{sorted(titles)}")
    check("认出了有总结+字幕+视频的那份", "真文稿" in titles)
    check("认出了只有 .txt 的那份", "只有文稿" in titles)

    bad = ["带时间戳也白搭", "ab_安慰", "说明", "太短"]
    check("下划线目录被跳过", "带时间戳也白搭" not in titles or len([t for t in titles if t == "带时间戳也白搭"]) == 0)
    check("不认 .stats.txt / 非转写格式 / 行数不够", not (set(bad) & titles), f" 混进来：{sorted(set(bad) & titles)}")
    check("没有任何条目来自 .tmp", all(".tmp" not in i["dir_name"] for i in items))

    good = next(i for i in items if i["title"] == "真文稿")
    check("正文字数（去掉时间戳标记）", good["chars"] == sum(len(f"第{i}句正文") for i in range(5)),
          f" 实际 {good['chars']}")
    check("时长取最后一行时间戳", good["duration"] == "00:00:04", f" 实际 {good['duration']}")
    check("识别出有字幕", good["has_srt"] is True)
    check("识别出有总结", good["has_summary"] is True)
    check("识别出骨架速览", good["has_outline"] is True)
    check("识别出同目录的媒体", good["has_media"] is True and good["media_name"] == "真文稿.mp4",
          f" {good['media_name']}")
    check("内部字段（_txt_path/_media_path）存在但不外发",
          good["_txt_path"] == made["good"] and "_txt_path" not in library.public(good))

    only = next(i for i in items if i["title"] == "只有文稿")
    check("没有媒体时不虚报 has_media", only["has_media"] is False and only["media_name"] == "")
    check("没有总结时不虚报 has_summary", only["has_summary"] is False)
    check("id 是 12 位十六进制", len(good["id"]) == 12 and all(c in "0123456789abcdef" for c in good["id"]))

    again = library.scan(sandbox_root)
    check("两次扫描 id 稳定（前端靠它取回）", [i["id"] for i in again] == [i["id"] for i in items])
    check("两份文稿 id 不同", good["id"] != only["id"])
    check("按修改时间倒序（新的在前）",
          all(a["mtime"] >= b["mtime"] for a, b in zip(items, items[1:])))

    # resolve 是唯一入口：只认 id
    check("resolve 能用 id 取回", (library.resolve(good["id"], sandbox_root) or {}).get("title") == "真文稿")
    check("未知 id 取不回", library.resolve("000000000000", sandbox_root) is None)
    check("用路径当 id 取不回（穿越没有入口）",
          library.resolve("../../config.json", sandbox_root) is None
          and library.resolve(made["good"], sandbox_root) is None)
    return good


def test_http(sandbox_root: str, good: dict) -> None:
    """接口层：只读、字段不外泄、未知 id 一律 404（不是 500、也不是把别的文件吐出来）。"""
    print("\n== /api/library（只读接口）==")
    import httpx  # noqa: PLC0415

    real_output = tasks.OUTPUT_DIR
    tasks.OUTPUT_DIR = sandbox_root
    try:
        async def get(url: str):
            transport = httpx.ASGITransport(app=app_main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(url)

        r = asyncio.run(get("/api/library"))
        check("列表返回 200", r.status_code == 200, f" {r.status_code}")
        data = r.json()
        check("列表带 root 与 items", data.get("root") == sandbox_root and len(data.get("items", [])) == 2,
              f" {data.get('root')}")
        check("列表不外发内部字段",
              all(not k.startswith("_") for it in data["items"] for k in it))

        r = asyncio.run(get(f"/api/library/{good['id']}/text"))
        check("文稿返回 200", r.status_code == 200, f" {r.status_code}")
        check("文稿内容逐字一致", r.text.splitlines()[0] == "[00:00:00] 第0句正文", f" {r.text[:40]!r}")

        r = asyncio.run(get(f"/api/library/{good['id']}/summary"))
        check("总结返回 200 且 exists", r.status_code == 200 and r.json().get("exists") is True, f" {r.status_code}")
        check("总结正文带出来了", "总结正文" in r.json().get("content", ""))
        check("骨架速览也带出来了", r.json().get("outline", "").startswith("- [00:00:00]"))

        r = asyncio.run(get(f"/api/library/{good['id']}/media"))
        check("媒体返回 200", r.status_code == 200, f" {r.status_code} {r.text[:120]}")
        check("媒体类型是视频", r.headers.get("content-type", "").startswith("video/"),
              f" {r.headers.get('content-type')}")

        for tail in ("text", "summary", "media"):
            r = asyncio.run(get(f"/api/library/000000000000/{tail}"))
            check(f"未知 id 的 {tail} 返回 404", r.status_code == 404, f" {r.status_code}")

        # 穿越尝试：拿一个脏 id 去试探，必须 404，绝不能把 output/ 外面的文件读出来
        r = asyncio.run(get("/api/library/..%2F..%2Fconfig.json/text"))
        check("路径穿越尝试返回 404", r.status_code == 404, f" {r.status_code} {r.text[:120]}")

        # 目录不存在时必须是空清单 + 200，不是 500
        tasks.OUTPUT_DIR = os.path.join(sandbox_root, "不存在")
        r = asyncio.run(get("/api/library"))
        check("产物目录不存在时返回空清单而不是报错",
              r.status_code == 200 and r.json().get("items") == [], f" {r.status_code} {r.text[:120]}")
    finally:
        tasks.OUTPUT_DIR = real_output


def test_real_output() -> None:
    """对真实 `output/` 的回归：AB 试跑的产物一份都不许混进清单。"""
    print("\n== 真实 output/ ==")
    items = library.scan()
    print(f"  扫到 {len(items)} 份文稿：{[i['title'] for i in items]}")
    check("真实目录能扫到文稿", len(items) >= 1, f" {len(items)}")
    check("没有一份来自 .tmp / 下划线目录",
          all(not i["dir_name"].startswith((".", "_")) for i in items))
    check("没有一份是 .stats.txt", all(not i["name"].lower().endswith(".stats.txt") for i in items))
    check("每份都至少 3 行且有正文", all(i["lines"] >= 3 and i["chars"] > 0 for i in items))
    check("每份的 id 都能被 resolve 取回",
          all(library.resolve(i["id"]) is not None for i in items))


def main() -> int:
    print("bili-note 自检：已有文稿自动检索")
    # 沙盒放系统临时目录，**不放在 output/.tmp 下**（别的自检在那儿）：这份自检要造
    # `.tmp` / `_内部` 这些"应该被跳过"的假目录，放在真实产物目录里等于往他的 output/ 里
    # 塞垃圾；而且受限 shell 写不进既有子目录，会连测试都跑不起来。
    #
    # ⚠️ 这里**不能用 tempfile.mkdtemp()**：它按 0700 造目录（自带 DACL），
    # 在带沙箱 ACL 的环境里那个新目录会丢掉写权限，连创建者自己都写不进去
    # （实测 WinError 5）。用普通的 os.makedirs 继承父目录 ACL 才正常。
    sandbox_root = os.path.join(tempfile.gettempdir(), f"bili_note_library_selftest_{uuid.uuid4().hex[:8]}")
    os.makedirs(sandbox_root, exist_ok=False)
    try:
        made = build_sandbox(sandbox_root)
        good = test_scan(sandbox_root, made)
        test_http(sandbox_root, good)
    finally:
        shutil.rmtree(sandbox_root, ignore_errors=True)
    test_real_output()

    print(f"\n结果：{ok_count} 通过 / {fail_count} 失败")
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
