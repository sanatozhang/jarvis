"""Crash-free 色点阈值（早晚报 markdown + 飞书卡片共用）。

2026-10-21 按团队 APM 指标体系改成分平台、分口径三档：🟩 Healthy / 🟨 Warning / 🟥 Critical。
原来不分平台、不分 user/session 一律 ≥99.9 绿 / ≥99.5 黄。

graygate 的同名指标阈值在 `graygate/metrics.yaml`（两个模块互不 import，数字需手动保持一致）。
"""
from __future__ import annotations

from typing import Optional

# (platform, 口径) → (green 下限, yellow 下限)
_CF_THRESHOLDS = {
    ("ios", "users"): (99.90, 99.50),
    ("ios", "sessions"): (99.95, 99.90),
    ("android", "users"): (99.80, 99.50),
    ("android", "sessions"): (99.90, 99.80),
}


def cf_emoji(pct: float, platform: Optional[str], dim: str) -> str:
    """platform: "ios" / "android"；None（iOS+Android 汇总）按较宽松的 Android 档。
    dim: "users" / "sessions"。"""
    green, yellow = _CF_THRESHOLDS[((platform or "android").lower(), dim)]
    if pct >= green:
        return "🟩"
    if pct >= yellow:
        return "🟨"
    return "🟥"
