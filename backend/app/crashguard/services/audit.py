"""
Crashguard audit log helper。
记录每次操作（早晚报 / PR / 预热 / 批量分析 / 追问）的成功失败 + 耗时。
"""
from __future__ import annotations

import json as _json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, Optional

from app.crashguard.models import CrashAuditLog
from app.db.database import get_session

logger = logging.getLogger("crashguard.audit")


async def write_audit(
    op: str,
    target_id: str = "",
    success: bool = True,
    detail: Optional[Dict[str, Any]] = None,
    error: str = "",
    duration_ms: int = 0,
) -> None:
    """单条 audit 写入。永不抛异常（避免影响主流程）。"""
    try:
        async with get_session() as session:
            row = CrashAuditLog(
                op=op[:32],
                target_id=(target_id or "")[:128],
                success=bool(success),
                detail=_json.dumps(detail or {}, ensure_ascii=False)[:5000],
                error=(error or "")[:1000],
                duration_ms=int(max(0, duration_ms)),
                created_at=datetime.utcnow(),
            )
            session.add(row)
            await session.commit()
    except Exception as e:
        # audit 失败不该传播
        logger.warning("audit write failed for op=%s: %s", op, e)


@asynccontextmanager
async def audit_op(op: str, target_id: str = "", detail: Optional[Dict[str, Any]] = None):
    """
    上下文管理器版：自动测耗时 + 捕获异常 + 写 audit。

    用法：
        async with audit_op("daily_report", target_id="morning") as ctx:
            ctx["detail"]["sent"] = True

    捕获到异常时仍写一条 success=False，并 re-raise。
    """
    start = time.time()
    payload: Dict[str, Any] = {"detail": dict(detail or {})}
    try:
        yield payload
        await write_audit(
            op=op,
            target_id=target_id,
            success=True,
            detail=payload.get("detail"),
            duration_ms=int((time.time() - start) * 1000),
        )
    except Exception as exc:
        await write_audit(
            op=op,
            target_id=target_id,
            success=False,
            detail=payload.get("detail"),
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.time() - start) * 1000),
        )
        raise
