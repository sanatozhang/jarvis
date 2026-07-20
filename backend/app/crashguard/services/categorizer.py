"""
Crashguard issue 分类 — 把 Datadog issue 标记成 crash / anr / memory / web_warning / other。

只关注 crash + anr 两类（默认进 Top N），其他默认过滤掉。
"""
from __future__ import annotations

import re
from typing import Optional

# ---- 关键词规则（按优先级匹配，命中即返回） ----

_ANR_PATTERNS = [
    r"\bANRException\b",
    r"\bAppHang\b",
    r"\banr\b",
    r"application\s*not\s*responding",
    r"main\s*thread\s*hang",
]

_MEMORY_PATTERNS = [
    r"\bMemoryWarning\b",
    r"\bdid\s*receive\s*memory\s*warning\b",
    r"\bOutOfMemoryError\b",
    r"\bOOM\b",
]

# 浏览器 / 平台告警类，不是真崩溃
_WEB_WARNING_PATTERNS = [
    r"\bPreventDefaultPassive\b",
    r"\bResizeObserver\s+loop\b",
    r"\bScript\s+error\b",
]

# 卡顿(jank_watchdog_block)——jank_ingester.py 生成的标题固定前缀 "Jank @ "（2026-07-20）。
# 必须能识别，否则 migrations.py::_backfill_kind()（每次启动都跑一遍全表重分类）会把
# 识别不出来的标题一律归到默认的 "crash"，把卡顿悄悄冲回崩溃统计，每次重启都会发生。
_JANK_PATTERNS = [
    r"^Jank\s*@",
]

_ANR_RE = [re.compile(p, re.IGNORECASE) for p in _ANR_PATTERNS]
_MEM_RE = [re.compile(p, re.IGNORECASE) for p in _MEMORY_PATTERNS]
_WEB_RE = [re.compile(p, re.IGNORECASE) for p in _WEB_WARNING_PATTERNS]
_JANK_RE = [re.compile(p, re.IGNORECASE) for p in _JANK_PATTERNS]

# 只关注 app 端：Flutter / iOS / Android。BROWSER / NODE 等一律 'web_warning' 兜底。
_APP_PLATFORMS = {"flutter", "ios", "android"}


def is_app_platform(platform: Optional[str]) -> bool:
    return (platform or "").strip().lower() in _APP_PLATFORMS


def classify_kind(title: str, platform: Optional[str] = None, service: Optional[str] = None) -> str:
    """
    返回 issue 类别:
        - 'anr'           : ANR / 主线程卡顿
        - 'jank'          : 卡顿(jank_watchdog_block)，见 jank_ingester.py
        - 'memory'        : 内存告警（不是真崩溃）
        - 'web_warning'   : 浏览器告警
        - 'crash'         : 真崩溃（默认）
        - 'other'         : 兜底
    """
    t = (title or "").strip()
    if not t:
        return "other"

    # 非 app 平台直接归为 web_warning，不进 Top N
    if platform and not is_app_platform(platform):
        return "web_warning"

    for r in _JANK_RE:
        if r.search(t):
            return "jank"
    for r in _ANR_RE:
        if r.search(t):
            return "anr"
    for r in _MEM_RE:
        if r.search(t):
            return "memory"
    for r in _WEB_RE:
        if r.search(t):
            return "web_warning"

    return "crash"


# 默认参与 Top N 的 kinds（用户：只关注 crash + ANR）
DEFAULT_TRACKED_KINDS = ("crash", "anr")
