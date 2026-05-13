"""Runtime 包装器：把 backend/scripts/ 下 CLI 脚本以函数形式暴露给 scheduler。

底层逻辑：scripts 目录下脚本既要支持运维 `python3 -m backend.scripts.xxx` 手动跑，
又要被 scheduler tick 周期调用。这层 wrapper 隔离了导入路径分歧。
"""
from __future__ import annotations

from typing import Any, Dict


async def run_backfill_all(days_hourly: int = 7, days_daily: int = 14) -> Dict[str, Any]:
    """跑 hourly + daily 两个 baseline 回填，返回汇总结果。

    用于 scheduler 周度 tick：days=3 即可补最近窗口空洞，保持基线持续可用。
    用于运维一次性大补：days_hourly=7 / days_daily=14（默认）。
    """
    out: Dict[str, Any] = {"ok": True}
    try:
        from scripts.backfill_hourly_snapshots import run_backfill as run_hourly
        out["hourly"] = await run_hourly(days=days_hourly, dry_run=False)
    except Exception as exc:
        out["ok"] = False
        out["hourly"] = {"ok": False, "error": f"hourly backfill failed: {exc}"}
    try:
        from scripts.backfill_daily_snapshots import run_backfill as run_daily
        out["daily"] = await run_daily(days=days_daily, dry_run=False)
    except Exception as exc:
        out["ok"] = False
        out["daily"] = {"ok": False, "error": f"daily backfill failed: {exc}"}
    return out
