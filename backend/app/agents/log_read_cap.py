"""深度模式日志读取上限：判定一个工具调用是否在读 logs/，并跨调用累加计数。

hook 脚本（注入到 workspace/.claude/）import 本模块的 classify_and_count；单测也用它。
一切异常一律 fail-open（allow=True），绝不因计数逻辑卡死 agent。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

_LOGS_RE = re.compile(r"(^|[\s/'\"])logs/")


def _is_log_read(event: Dict[str, Any]) -> bool:
    tool = event.get("tool_name") or ""
    inp = event.get("tool_input") or {}
    if tool in ("Read", "Grep"):
        target = str(inp.get("file_path") or inp.get("path") or "")
        return "logs/" in target or target.strip() in ("logs", "./logs")
    if tool == "Bash":
        return bool(_LOGS_RE.search(str(inp.get("command") or "")))
    return False


def classify_and_count(event: Dict[str, Any], counter: Path, cap: int) -> Dict[str, Any]:
    """返回 {'allow': bool, 'reason': str}。只对"读 logs/"计数；超过 cap 则 deny。"""
    try:
        if not _is_log_read(event):
            return {"allow": True, "reason": ""}
        try:
            n = int(counter.read_text().strip()) if counter.exists() else 0
        except Exception:
            n = 0  # 计数文件损坏 → fail-open，从 0 重数
        n += 1
        counter.write_text(str(n))
        if n > cap:
            return {
                "allow": False,
                "reason": (f"已达日志读取上限（{cap} 次）。请立即基于已有证据写出 "
                           f"output/result.json，不要再 grep 日志。"),
            }
        return {"allow": True, "reason": ""}
    except Exception:
        return {"allow": True, "reason": ""}  # fail-open
