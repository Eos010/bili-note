"""共享异常：取消必须是一个**唯一的**类型。

教训（2026-09-16，用户报「取消没反应」）：
`downloader` / `transcriber` / `tasks` 曾各自定义 `_CancelledError`。
三个类互不相同 → `tasks` 里的 `except _CancelledError` 接不住下载器抛的那个
→ 落到 `except Exception` → 任务被标成 error（红「失败」）而不是 cancelled，
且 `str(e)` 为空、界面上连错误原因都看不到。

**取消是唯一类型，不是三个各管各的。**
"""
from __future__ import annotations


class CancelledError(Exception):
    """用户主动取消。任务应标记为 cancelled，而不是 error。"""

    def __init__(self, message: str = "已取消") -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:  # noqa: D105
        return self.message


# ---------------------------------------------------------------- 报错翻译
#
# yt-dlp 的原始报错是给开发者看的（英文 + 内部堆栈），直接甩给用户等于没说。
# 这里把常见情形翻成「发生了什么 + 该怎么办」，**原始错误原文照旧附在后面**，
# 不丢排查线索。

_SIGNATURES: list[tuple[tuple[str, ...], str, str, str]] = [
    (
        ("大会员", "premium", "vip", "login required", "sign in", "需要登录", "not logged in"),
        "需要登录才能看",
        "这个视频要登录态（大会员专享 / 年龄限制 / 仅粉丝可见）。",
        "v1.5 才会支持 cookie 登录态，目前先换一个源。",
    ),
    (
        ("http error 412", "412"),
        "被 B 站风控拦了",
        "短时间请求太多，B 站触发了限流。",
        "等几分钟再重试；别一次提交太多链接。",
    ),
    (
        ("http error 403", "403 forbidden", "forbidden"),
        "服务器拒绝了这次请求",
        "可能是需要登录态，也可能是 B 站风控。",
        "先重试一次；仍失败就换个视频试试，确认是不是这条链接特有。",
    ),
    (
        ("http error 404", "video unavailable", "not found", "不存在", "已删除", "已下架"),
        "视频不存在或已下架",
        "链接指向的视频在 B 站上已经找不到了。",
        "核对一下链接，或者换个源。",
    ),
    (
        ("read operation timed out", "timed out", "timeout", "超时"),
        "网络超时",
        "拉取网页或媒体流时超时，通常是网络波动或 B 站响应慢——**不是你这条链接有问题**。",
        "点「重试」通常就能过。反复失败再检查代理 / VPN。",
    ),
    (
        ("connection reset", "connection aborted", "remote end closed",
         "incomplete read", "eof occurred", "connection broken"),
        "连接被中断",
        "传输中途连接断了，弱网下很常见。",
        "点「重试」；已下载的部分会续传，不用从头来。",
    ),
    (
        ("http error 5", "502", "503", "504", "bad gateway", "service unavailable"),
        "B 站服务端出错",
        "对方服务器 5xx，属于他们的临时故障。",
        "过几分钟再重试。",
    ),
    (
        ("ffmpeg",),
        "ffmpeg 出问题",
        "音视频合并需要 ffmpeg，可能没装或不在 PATH。",
        "确认 ffmpeg 可用（项目会自动在 WinGet 目录里找）。",
    ),
    (
        ("no space left", "磁盘空间", "disk full"),
        "磁盘空间不足",
        "目标盘写不下了。",
        "清点空间再重试。",
    ),
    (
        ("permission denied", "errno 13", "拒绝访问", "winerror 5"),
        "文件被占用或没有写权限",
        "目标文件正在被别的程序打开（比如播放器），或者目录没有写权限。",
        "关掉可能占用的程序再重试。",
    ),
    (
        ("unsupported url", "unable to extract", "no suitable extractor"),
        "链接无法识别",
        "yt-dlp 没有能处理这个链接的解析器。",
        "确认是 B 站视频页链接（不是直播、动态或短链失效页）。",
    ),
    (
        ("unable to download webpage",),
        "拉取网页失败",
        "没能取到 B 站的视频页面。",
        "点「重试」；持续失败检查网络或代理。",
    ),
]


def explain(error: object) -> str:
    """把异常翻成「发生了什么 + 怎么办」，原始报错附在最后。"""
    raw = str(error).strip() or type(error).__name__
    low = raw.lower()
    for keys, title, what, todo in _SIGNATURES:
        if any(k in low for k in keys):
            return f"{title} —— {what}\n→ {todo}\n\n原始报错：{raw}"
    return raw


def is_transient(error: object) -> bool:
    """这个错误值不值得重试（网络类才重试，链接错/视频没了重试没意义）。"""
    low = str(error).lower()
    keys = (
        "timed out", "timeout", "超时", "connection reset", "connection aborted",
        "remote end closed", "incomplete read", "eof occurred", "temporary failure",
        "http error 5", "502", "503", "504", "bad gateway", "service unavailable",
        "unable to download webpage",
    )
    return any(k in low for k in keys)
