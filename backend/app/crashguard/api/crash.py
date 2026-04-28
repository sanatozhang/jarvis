"""crashguard API — manual trigger / health"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.crashguard.config import get_crashguard_settings

logger = logging.getLogger("crashguard.api")

router = APIRouter(prefix="/api/crash", tags=["crashguard"])


class TriggerRequest(BaseModel):
    latest_release: str = Field(..., description="当前最新发布版本，如 '1.4.7'")
    recent_versions: List[str] = Field(default_factory=list, description="最近 N 个版本（用于回归判定）")
    target_date: Optional[date] = Field(None, description="指定快照日期，默认今日")


class TriggerResponse(BaseModel):
    issues_processed: int
    snapshots_written: int
    top_n_count: int


@router.post("/trigger", response_model=TriggerResponse)
async def trigger_pipeline(req: TriggerRequest) -> Any:
    """
    手动触发数据流水线 (Step 1-6)。

    AI 分析与日报推送在 Plan 2/3 实现。
    """
    s = get_crashguard_settings()
    if not s.enabled:
        raise HTTPException(status_code=503, detail="crashguard 已被 kill switch 关闭")

    from app.crashguard.workers.pipeline import run_data_phase

    target_date = req.target_date or date.today()
    try:
        result = await run_data_phase(
            today=target_date,
            latest_release=req.latest_release,
            recent_versions=req.recent_versions,
        )
    except Exception as e:
        logger.exception("pipeline failed")
        raise HTTPException(status_code=500, detail=f"pipeline failed: {e}")

    return TriggerResponse(
        issues_processed=result["issues_processed"],
        snapshots_written=result["snapshots_written"],
        top_n_count=result["top_n_count"],
    )


@router.get("/health")
async def health() -> Dict[str, Any]:
    """模块健康检查"""
    s = get_crashguard_settings()
    return {
        "module": "crashguard",
        "enabled": s.enabled,
        "datadog_configured": bool(s.datadog_api_key),
        "feishu_target_set": bool(s.feishu_target_chat_id),
    }


@router.get("/top")
async def get_top(target_date: Optional[date] = None, limit: int = 20) -> Dict[str, Any]:
    """读取指定日期的 Top N（不重新跑流水线）"""
    from app.db.database import get_session
    from app.crashguard.services.ranker import pick_top_n

    if target_date is None:
        target_date = date.today()

    async with get_session() as session:
        top = await pick_top_n(session, today=target_date, n=limit)
    return {"date": target_date.isoformat(), "count": len(top), "issues": top}
