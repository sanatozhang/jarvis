"""
定时任务心跳记录器。

底层逻辑：每个 tick 调用 `record_heartbeat(job_name)` async context manager 包裹真实逻辑，
自动记录开始/结束时间、捕获异常、写入 `crash_job_heartbeats` 表。

用法：
    async with record_heartbeat("core_metric") as hb:
        res = await run_core_metric_tick()
        hb.set_summary(res)                # 可选：把任务自报结果存进 summary
        hb.set_status_from_result(res)     # 可选：根据 result dict 自动判定 status

异常：自动 status=failed + error=excerpt，不会吞掉异常（上层 logger 仍可记录）。
"""
from __future__ import annotations

import json as _json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from app.crashguard.models import CrashJobHeartbeat
from app.db.database import get_session

logger = logging.getLogger("crashguard.job_heartbeat")


# 已知 job 注册表 —— 前端 /api/crash/jobs/status 用此匹配 cron 配置 + 显示名
KNOWN_JOBS = (
    "core_metric",
    "hourly_alert",
    "analyze_tick",
    "pr_sync",
    "pipeline",
    "morning_daily",
    "evening_daily",
    "warmup",
    "top_crash_auto_pr",
)


class _HeartbeatCtx:
    """tick 内部使用的轻量收集器，避免每次都重新 query。"""

    def __init__(self, job_name: str):
        self.job_name = job_name
        self.summary: Dict[str, Any] = {}
        self.status: str = "success"
        self.error: str = ""

    def set_summary(self, payload: Any) -> None:
        try:
            if isinstance(payload, dict):
                # 仅保留可 JSON 化的简单字段，避免巨型 dict 撑爆 row
                self.summary = {
                    k: v for k, v in payload.items()
                    if isinstance(v, (str, int, float, bool, list, type(None)))
                }
            else:
                self.summary = {"raw": str(payload)[:500]}
        except Exception:
            self.summary = {"raw": "<unserializable>"}

    def set_status_from_result(self, res: Optional[Dict[str, Any]]) -> None:
        """根据 tick 返回的 dict 推断 status：
        - res.get("ok") is False → failed
        - res.get("error") 非空 → failed
        - res.get("skipped") 非空 → skipped
        - 其它 → success（默认）
        """
        if not isinstance(res, dict):
            return
        if res.get("error"):
            self.status = "failed"
            self.error = str(res["error"])[:500]
            return
        if res.get("ok") is False:
            self.status = "failed"
            self.error = str(res.get("reason") or "ok=false")[:500]
            return
        if res.get("skipped"):
            self.status = "skipped"

    def set_status_from_partial(
        self, success_count: int, total_count: int, error_hint: str = ""
    ) -> None:
        """批量任务的三态判定：
        - total==0 → success（空 tick，无可做之事，不视为异常）
        - success==total → success（全部成功）
        - 0<success<total → degraded（部分失败，可能 transient，不立刻告警）
        - success==0 且 total>0 → failed（全部失败，必然是 systemic 问题）

        抓手：避免「1/12 PR 偶发失败 → 全 job 标 failed → 告警」的误报噪音。
        degraded 进入 `job_health_alerter` 的弱信号通道，持续 N 次才升级。
        """
        if total_count <= 0:
            self.status = "success"
            return
        if success_count >= total_count:
            self.status = "success"
            return
        if success_count <= 0:
            self.status = "failed"
            self.error = (
                error_hint or f"all {total_count} items failed"
            )[:500]
            return
        # 部分失败 = degraded
        self.status = "degraded"
        self.error = (
            error_hint or f"{total_count - success_count}/{total_count} items failed"
        )[:500]


@asynccontextmanager
async def record_heartbeat(job_name: str):
    """async context manager：包裹 tick 自动记录心跳。

    yield 出 `_HeartbeatCtx`；ctx 可调 `set_summary` / `set_status_from_result`。
    异常自动捕获 → status=failed + error 摘要后**重新抛出**（上层日志不丢）。
    """
    ctx = _HeartbeatCtx(job_name)
    start = time.monotonic()
    exc: Optional[BaseException] = None
    try:
        yield ctx
    except BaseException as e:
        exc = e
        ctx.status = "failed"
        ctx.error = (f"{type(e).__name__}: {e}")[:500]
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        try:
            async with get_session() as session:
                session.add(CrashJobHeartbeat(
                    job_name=job_name,
                    status=ctx.status,
                    duration_ms=duration_ms,
                    summary=_json.dumps(ctx.summary, ensure_ascii=False, default=str),
                    error=ctx.error,
                ))
                await session.commit()
        except Exception:
            logger.exception("heartbeat write failed for job=%s (non-fatal)", job_name)
        if exc is not None:
            raise exc
