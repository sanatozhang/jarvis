"""Graygate 日报落库 + 网页端完整版 markdown 缓存。

写入点只有一个：`notify.send_daily_report` 取完数、发送之前（调度和手动触发都走它）。
读取点是 `api/graygate.py` 的报告列表 / 详情；详情缓存缺失时现算一次并回填
（`ensure_report_markdown`）。

所有写操作失败都**不影响发送**——缓存是锦上添花，日报本身照发。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from app.graygate.services.card_builder import GraygateReportData, collect_report_data
from app.graygate.services.report_markdown import assemble_report_markdown

logger = logging.getLogger("jarvis.graygate.report_store")

# 缓存只留这么多天，更早的清空 markdown（行保留，列表要用）。和 Datadog RUM 30 天
# 保留期对齐：再往前现算也算不出来了。
REPORT_MARKDOWN_RETENTION_DAYS = 30

_NO_DATA_MD = "_当天两平台都没有灰度版本数据，未生成日报。_\n"
_EXPIRED_MD = "_报告内容已过期，无法重新生成（超出 Datadog 数据保留期）。_\n"


def _row_dict(row) -> Dict[str, Any]:
    return {
        "date": row.report_date.isoformat(),
        "title": row.title or "",
        "is_red": bool(row.is_red),
        "worsen_count": int(row.worsen_count or 0),
        "new_crash_count": int(row.new_crash_count or 0),
        "has_markdown": bool(row.report_markdown),
        "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else None,
    }


async def save_report(data: GraygateReportData, markdown: Optional[str] = None) -> None:
    """按 report_date upsert，并顺手清掉保留期外的 markdown。失败只记日志。"""
    from sqlalchemy import select, update

    from app.db.database import get_session
    from app.graygate.models import GraygateDailyReport

    md = markdown if markdown is not None else assemble_report_markdown(data)
    values = dict(
        title=data.title,
        is_red=data.is_red,
        worsen_count=len(data.worsen_lines),
        new_crash_count=data.new_crash_count,
        report_markdown=md,
        updated_at=datetime.utcnow(),
    )
    try:
        async with get_session() as session:
            row = (await session.execute(
                select(GraygateDailyReport).where(GraygateDailyReport.report_date == data.target_date)
            )).scalar_one_or_none()
            if row is None:
                session.add(GraygateDailyReport(report_date=data.target_date, **values))
            else:
                for k, v in values.items():
                    setattr(row, k, v)
            cutoff = date.today() - timedelta(days=REPORT_MARKDOWN_RETENTION_DAYS)
            await session.execute(
                update(GraygateDailyReport)
                .where(GraygateDailyReport.report_date < cutoff, GraygateDailyReport.report_markdown != "")
                .values(report_markdown="")
            )
            await session.commit()
    except Exception:
        logger.exception("graygate save_report failed (non-fatal) date=%s", data.target_date)


async def list_reports(limit: int = 60) -> List[Dict[str, Any]]:
    from sqlalchemy import select

    from app.db.database import get_session
    from app.graygate.models import GraygateDailyReport

    async with get_session() as session:
        rows = (await session.execute(
            select(GraygateDailyReport).order_by(GraygateDailyReport.report_date.desc()).limit(limit)
        )).scalars().all()
    return [_row_dict(r) for r in rows]


async def get_report(target_date: date) -> Optional[Dict[str, Any]]:
    from sqlalchemy import select

    from app.db.database import get_session
    from app.graygate.models import GraygateDailyReport

    async with get_session() as session:
        row = (await session.execute(
            select(GraygateDailyReport).where(GraygateDailyReport.report_date == target_date)
        )).scalar_one_or_none()
    if row is None:
        return None
    return {**_row_dict(row), "markdown": row.report_markdown or ""}


# 缓存缺失时的后台生成任务。现算要把 Datadog 全查一遍，102 上实测好几分钟，
# 同步放在请求里前端必超时（2026-10-09 首发就是这样，页面一直空白）——改成后台跑、
# 前端轮询。进程内状态就够：生成完会落库，进程重启后再点一次重新生成即可。
_jobs: Dict[date, "asyncio.Task"] = {}
_failures: Dict[date, str] = {}


async def _generate(target_date: date) -> None:
    try:
        data = await collect_report_data(target_date)
        if data is None:
            _failures[target_date] = _NO_DATA_MD
            return
        await save_report(data)
        if not (await get_report(target_date) or {}).get("markdown"):
            # 落库失败（比如 database is locked）：至少让这次能看到，下次点再重试落库
            _failures[target_date] = assemble_report_markdown(data)
    except Exception as e:
        logger.exception("graygate report generation failed date=%s", target_date)
        _failures[target_date] = f"_生成失败：{type(e).__name__}，稍后刷新重试。_\n"
    finally:
        _jobs.pop(target_date, None)


def _empty(target_date: date) -> Dict[str, Any]:
    return {"date": target_date.isoformat(), "title": "", "is_red": False,
            "worsen_count": 0, "new_crash_count": 0}


async def ensure_report_markdown(target_date: date) -> Dict[str, Any]:
    """详情页入口。`status`：ready = markdown 可用；generating = 后台生成中，前端轮询。"""
    import asyncio

    cached = await get_report(target_date)
    if cached and cached["markdown"]:
        return {**cached, "cached": True, "status": "ready"}

    base = cached or _empty(target_date)
    if (date.today() - target_date).days >= REPORT_MARKDOWN_RETENTION_DAYS:
        return {**base, "markdown": _EXPIRED_MD, "cached": False, "status": "ready"}

    if target_date in _jobs:
        return {**base, "markdown": "", "cached": False, "status": "generating"}
    if target_date in _failures:
        # 一次性：展示完就清掉，下次再点重新生成
        return {**base, "markdown": _failures.pop(target_date), "cached": False, "status": "ready"}

    _jobs[target_date] = asyncio.create_task(_generate(target_date))
    return {**base, "markdown": "", "cached": False, "status": "generating"}
