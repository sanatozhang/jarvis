"""def(dict) -> Datadog monitor API payload。纯函数，无网络，易单测。

query 字符串由调用方（skill 指导）写好；builder 负责组装 + 按检测类型补 options 默认值。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

# RUM 入仓 0-10min 才稳定，给 15min 缓冲（复用 coreguard hourly_watch 经验）
DEFAULT_EVALUATION_DELAY = 900


def _parse_critical_from_query(query: str) -> Optional[float]:
    """从 query 末尾的比较式解析 critical 阈值，如 '... > 1200000000' -> 1200000000.0。"""
    m = re.search(r"(?:>=|<=|>|<|==)\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*$", query.strip())
    return float(m.group(1)) if m else None


def build_monitor_payload(d: Dict[str, Any]) -> Dict[str, Any]:
    detection = d.get("detection", "threshold")
    query = d["query"]

    # message：正文 + notify 句柄
    message = d.get("message", "") or ""
    notify = d.get("notify", []) or []
    if notify:
        message = (message + "\n\n" + " ".join(notify)).strip()

    # options 默认值
    options: Dict[str, Any] = {
        "notify_no_data": False,
        "evaluation_delay": DEFAULT_EVALUATION_DELAY,
        "include_tags": True,
        "thresholds": {},
    }

    if detection == "anomaly":
        # anomaly：critical 固定为 1（异常点计数），需 threshold_windows
        options["thresholds"]["critical"] = 1.0
        options["threshold_windows"] = {"trigger_window": "last_30m", "recovery_window": "last_30m"}
    else:
        crit = _parse_critical_from_query(query)
        if crit is not None:
            options["thresholds"]["critical"] = crit

    # 调用方覆盖
    options.update(d.get("options", {}) or {})

    if d.get("muted_on_create"):
        options["silenced"] = {"*": None}

    return {
        "name": d["name"],
        "type": d["type"],
        "query": query,
        "message": message,
        "tags": list(d.get("tags", []) or []),
        "priority": d.get("priority"),
        "options": options,
    }
