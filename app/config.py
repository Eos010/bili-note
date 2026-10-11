"""配置读写：`config.json`（含 API Key，已在 .gitignore）。

优先级：环境变量 > config.json > 内置默认值。
API Key 永不回传给前端（只回打码形式）。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"

ENV_API_KEY = "BILI_NOTE_API_KEY"
ENV_BASE_URL = "BILI_NOTE_BASE_URL"

DEFAULTS: dict[str, Any] = {
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "max_tokens": 8000,
        "temperature": 0.3,
        "timeout_read": 300,
        # 推理模型（如 deepseek-flash）默认会先"思考"再回答：首字可能要等 45 秒以上，
        # 且思考过程会挤占 max_tokens。关掉能秒回，但**质量差异未验证**——默认保持开启。
        "thinking_enabled": True,
    },
    "summarize": {
        "seg_chars": 400,       # 单段目标正文长度（合并用）
        "seg_seconds": 180,     # 单段最大时长（秒）
        "gap_seconds": 3,       # 超过此间隔强制断段
        "with_timestamp": True, # 是否要求总结标注时间戳
        "template": "A",
        # 同时跑几个总结（v2.1 后台作业）。总结现在是服务端作业，
        # 关掉页面也继续跑，所以这里要有个闸门，别让一批任务把并发和 token 速率打爆。
        "max_concurrent": 3,
    },
}

# 允许前端写入的字段白名单（防越权写任意键）
_WRITABLE = {
    "llm": {"base_url", "api_key", "model", "max_tokens", "temperature",
            "timeout_read", "thinking_enabled"},
    "summarize": {"seg_chars", "seg_seconds", "gap_seconds", "with_timestamp",
                  "template", "max_concurrent"},
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_raw() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load() -> dict:
    """完整配置（含明文 Key）。仅后端内部使用，不要直接回给前端。"""
    cfg = _deep_merge(DEFAULTS, _read_raw())
    if os.environ.get(ENV_API_KEY):
        cfg["llm"]["api_key"] = os.environ[ENV_API_KEY]
    if os.environ.get(ENV_BASE_URL):
        cfg["llm"]["base_url"] = os.environ[ENV_BASE_URL]
    return cfg


def save(patch: dict) -> dict:
    """按白名单合并写入 config.json。空字符串视为"不修改"（防误清 Key）。"""
    clean: dict[str, dict] = {}
    for section, fields in _WRITABLE.items():
        incoming = patch.get(section)
        if not isinstance(incoming, dict):
            continue
        kept = {
            k: v for k, v in incoming.items()
            if k in fields and v is not None and not (isinstance(v, str) and v.strip() == "")
        }
        if kept:
            clean[section] = kept
    if clean:
        merged = _deep_merge(_read_raw(), clean)
        CONFIG_PATH.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return load()


def public() -> dict:
    """回给前端的版本：Key 打码 + 状态标记。"""
    cfg = load()
    key = cfg["llm"].get("api_key") or ""
    cfg["llm"]["api_key"] = mask(key)
    cfg["llm"]["api_key_set"] = bool(key)
    cfg["llm"]["api_key_from_env"] = bool(os.environ.get(ENV_API_KEY))
    cfg["llm"].pop("api_key_raw", None)
    return cfg


def mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "***"
    return f"{key[:6]}…{key[-4:]}"
