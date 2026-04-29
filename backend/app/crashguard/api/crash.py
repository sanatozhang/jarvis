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


def _datadog_url_for(issue_id: str) -> str:
    """Datadog Error Tracking issue 跳转链接（RUM track 路径）。"""
    s = get_crashguard_settings()
    site = (s.datadog_site or "datadoghq.com").strip()
    if site == "datadoghq.com":
        host = "app.datadoghq.com"
    elif site.startswith("app."):
        host = site
    else:
        host = f"app.{site}"
    return f"https://{host}/rum/error-tracking/issue/{issue_id}"


@router.get("/top")
async def get_top(
    target_date: Optional[date] = None,
    limit: int = 40,
    kinds: str = "crash,anr",
) -> Dict[str, Any]:
    """读取指定日期的 Top N（不重新跑流水线）。

    kinds: 逗号分隔的类别白名单。默认 "crash,anr"——
    过滤掉 MemoryWarning / 浏览器告警等。传 "all" 不过滤。
    """
    from app.db.database import get_session
    from app.crashguard.services.ranker import pick_top_n

    if target_date is None:
        target_date = date.today()

    if kinds.strip().lower() == "all":
        kind_tuple: tuple = ()
    else:
        kind_tuple = tuple(k.strip().lower() for k in kinds.split(",") if k.strip())

    async with get_session() as session:
        top = await pick_top_n(
            session,
            today=target_date,
            n=limit,
            kinds=kind_tuple,
        )
    for item in top:
        item["datadog_url"] = _datadog_url_for(item["datadog_issue_id"])
    return {"date": target_date.isoformat(), "count": len(top), "issues": top}


@router.get("/issues/{issue_id}")
async def get_issue_detail(issue_id: str, target_date: Optional[date] = None) -> Dict[str, Any]:
    """
    单 issue 详情：基础属性 + 当日快照 + 代表性堆栈。
    AI 分析（root_cause / fix_suggestion）由 Plan 2 接入；当前返回空字段。
    """
    from sqlalchemy import select
    from app.db.database import get_session
    from app.crashguard.models import CrashIssue, CrashSnapshot, CrashAnalysis
    import json as _json

    if target_date is None:
        target_date = date.today()

    async with get_session() as session:
        issue = (await session.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
        )).scalar_one_or_none()
        if issue is None:
            raise HTTPException(status_code=404, detail=f"issue {issue_id} not found")

        snap = (await session.execute(
            select(CrashSnapshot).where(
                CrashSnapshot.datadog_issue_id == issue_id,
                CrashSnapshot.snapshot_date == target_date,
            )
        )).scalar_one_or_none()

        # 详情页展示策略：root_cause 分析（首轮）才进 analysis 区；followup 是另一个分区
        # 优先最新成功的 root；没成功就回落最新一条 root（含 pending/running 让前端 show 状态）
        analysis = (await session.execute(
            select(CrashAnalysis)
            .where(
                CrashAnalysis.datadog_issue_id == issue_id,
                CrashAnalysis.status == "success",
                CrashAnalysis.followup_question == "",
            )
            .order_by(CrashAnalysis.created_at.desc())
        )).scalars().first()
        if analysis is None:
            analysis = (await session.execute(
                select(CrashAnalysis)
                .where(
                    CrashAnalysis.datadog_issue_id == issue_id,
                    CrashAnalysis.followup_question == "",
                )
                .order_by(CrashAnalysis.created_at.desc())
            )).scalars().first()

    try:
        tags = _json.loads(issue.tags) if issue.tags else {}
    except (ValueError, TypeError):
        tags = {}

    snap_block: Dict[str, Any] = {}
    if snap is not None:
        snap_block = {
            "snapshot_date": snap.snapshot_date.isoformat() if snap.snapshot_date else None,
            "events_count": snap.events_count or 0,
            "users_affected": snap.users_affected or 0,
            "crash_free_impact_score": snap.crash_free_impact_score or 0.0,
            "is_new_in_version": bool(snap.is_new_in_version),
            "is_regression": bool(snap.is_regression),
            "is_surge": bool(snap.is_surge),
            "app_version": snap.app_version or "",
        }

    analysis_block: Dict[str, Any] = {}
    if analysis is not None:
        try:
            causes = _json.loads(analysis.possible_causes or "[]")
            if not isinstance(causes, list):
                causes = []
        except (ValueError, TypeError):
            causes = []
        analysis_block = {
            "scenario": analysis.scenario or "",
            "root_cause": analysis.root_cause or "",
            "fix_suggestion": analysis.fix_suggestion or "",
            "feasibility_score": float(analysis.feasibility_score or 0.0),
            "confidence": analysis.confidence or "",
            "reproducibility": analysis.reproducibility or "",
            "agent_name": analysis.agent_name or "",
            "agent_model": analysis.agent_model or "",
            "status": analysis.status or "",
            "possible_causes": causes,
            "complexity_kind": analysis.complexity_kind or "",
            "solution": analysis.solution or "",
            "hint": analysis.hint or "",
            "run_id": analysis.analysis_run_id,
            "created_at": analysis.created_at.isoformat() if analysis.created_at else None,
        }

    return {
        "datadog_issue_id": issue.datadog_issue_id,
        "datadog_url": _datadog_url_for(issue.datadog_issue_id),
        "stack_fingerprint": issue.stack_fingerprint,
        "title": issue.title or "",
        "platform": issue.platform or "",
        "service": issue.service or "",
        "top_os": getattr(issue, "top_os", "") or "",
        "top_device": getattr(issue, "top_device", "") or "",
        "top_app_version": getattr(issue, "top_app_version", "") or "",
        "first_seen_at": issue.first_seen_at.isoformat() if issue.first_seen_at else None,
        "last_seen_at": issue.last_seen_at.isoformat() if issue.last_seen_at else None,
        "first_seen_version": issue.first_seen_version or "",
        "last_seen_version": issue.last_seen_version or "",
        "total_events": issue.total_events or 0,
        "total_users_affected": issue.total_users_affected or 0,
        "representative_stack": issue.representative_stack or "",
        "tags": tags,
        "status": issue.status or "open",
        "assignee": getattr(issue, "assignee", "") or "",
        "snapshot": snap_block,
        "analysis": analysis_block,
    }


@router.post("/analyze/{issue_id}")
async def analyze_issue(issue_id: str) -> Dict[str, Any]:
    """异步触发分析。立即返回 run_id；前端轮询 GET /analyses/{run_id} 查结果。"""
    from app.crashguard.services.analyzer import start_analysis

    try:
        run_id = await start_analysis(issue_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("start_analysis failed for %s", issue_id)
        raise HTTPException(status_code=500, detail=f"start_analysis failed: {e}")
    return {"run_id": run_id, "status": "pending"}


@router.get("/analyses/{run_id}")
async def get_analysis_run(run_id: str) -> Dict[str, Any]:
    """轮询单次分析的最新状态。status: pending / running / success / empty / failed"""
    from app.crashguard.services.analyzer import get_analysis_status

    st = await get_analysis_status(run_id)
    if st is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return st


@router.get("/issues/{issue_id}/analyses")
async def list_issue_analyses(issue_id: str) -> Dict[str, Any]:
    """获取该 issue 全部分析（含追问）按时间正序。前端用于会话化渲染。"""
    from sqlalchemy import select
    from app.db.database import get_session
    from app.crashguard.models import CrashAnalysis
    import json as _json

    async with get_session() as session:
        rows = (await session.execute(
            select(CrashAnalysis)
            .where(CrashAnalysis.datadog_issue_id == issue_id)
            .order_by(CrashAnalysis.created_at)
        )).scalars().all()

    items = []
    for r in rows:
        try:
            causes = _json.loads(r.possible_causes or "[]")
            if not isinstance(causes, list):
                causes = []
        except (ValueError, TypeError):
            causes = []
        items.append({
            "run_id": r.analysis_run_id,
            "status": r.status or "",
            "is_followup": bool((r.followup_question or "").strip()),
            "followup_question": r.followup_question or "",
            "answer": r.answer or "",
            "scenario": r.scenario or "",
            "root_cause": r.root_cause or "",
            "fix_suggestion": r.fix_suggestion or "",
            "possible_causes": causes,
            "complexity_kind": r.complexity_kind or "",
            "solution": r.solution or "",
            "hint": r.hint or "",
            "feasibility_score": float(r.feasibility_score or 0.0),
            "confidence": r.confidence or "",
            "reproducibility": r.reproducibility or "",
            "agent_name": r.agent_name or "",
            "agent_model": r.agent_model or "",
            "parent_run_id": r.parent_run_id or "",
            "error": r.error or "",
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"datadog_issue_id": issue_id, "count": len(items), "analyses": items}


class FollowupRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    parent_run_id: Optional[str] = None


@router.post("/issues/{issue_id}/followup")
async def followup_issue(issue_id: str, req: FollowupRequest) -> Dict[str, Any]:
    """对已分析过的 issue 发起追问。立即返回 run_id，前端轮询 /analyses/{run_id}。"""
    from app.crashguard.services.analyzer import start_analysis

    try:
        run_id = await start_analysis(
            issue_id,
            triggered_by="followup",
            followup_question=req.question.strip(),
            parent_run_id=req.parent_run_id or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("followup failed for %s", issue_id)
        raise HTTPException(status_code=500, detail=f"followup failed: {e}")
    return {"run_id": run_id, "status": "pending"}


class BatchAnalyzeRequest(BaseModel):
    top_n: Optional[int] = Field(None, ge=1, le=100, description="本次批量分析的 Top N，默认走 config.analyze_top_n")
    force: bool = Field(False, description="True 时即使已分析过也重跑")
    target_date: Optional[date] = Field(None, description="指定日期，默认今日")


@router.post("/batch-analyze")
async def batch_analyze(req: BatchAnalyzeRequest) -> Dict[str, Any]:
    """对今日 Top N 批量启动 AI 分析（去重）。立即返回 run_id 列表，前端按 run_id 各自轮询。"""
    from app.crashguard.services.batch_analyzer import batch_analyze_top

    s = get_crashguard_settings()
    top_n = req.top_n or s.analyze_top_n
    try:
        result = await batch_analyze_top(
            top_n=top_n,
            target_date=req.target_date,
            force=req.force,
        )
    except Exception as e:
        logger.exception("batch-analyze failed")
        raise HTTPException(status_code=500, detail=f"batch-analyze failed: {e}")
    return result


class DailyReportRunRequest(BaseModel):
    report_type: str = Field("morning", description="morning / evening")
    target_date: Optional[date] = Field(None, description="默认今日")
    top_n: int = Field(10, ge=1, le=50)
    chat_id: Optional[str] = Field(None, description="覆盖 config 的 target_chat_id（测试用）")
    dry_run: bool = Field(False, description="True 时只生成 markdown 不发飞书")


@router.post("/reports/run-now")
async def run_daily_report_now(req: DailyReportRunRequest) -> Dict[str, Any]:
    """手动触发一次早/晚报。dry_run=True 仅返回 markdown 预览不写库不发飞书。"""
    from app.crashguard.services.daily_report import compose_report, send_daily_report

    if req.report_type not in ("morning", "evening"):
        raise HTTPException(status_code=400, detail="report_type must be morning or evening")

    if req.dry_run:
        try:
            text, payload = await compose_report(
                req.report_type, req.target_date, top_n=req.top_n,
            )
        except Exception as e:
            logger.exception("compose_report failed")
            raise HTTPException(status_code=500, detail=f"compose failed: {e}")
        return {"ok": True, "dry_run": True, "preview": text, "payload": payload}

    try:
        result = await send_daily_report(
            req.report_type,
            target_date=req.target_date,
            top_n=req.top_n,
            chat_id_override=req.chat_id or "",
        )
    except Exception as e:
        logger.exception("send_daily_report failed")
        raise HTTPException(status_code=500, detail=f"send failed: {e}")
    return result


@router.get("/audit-summary")
async def audit_summary(hours: int = 48) -> Dict[str, Any]:
    """系统健康卡片：最近 N 小时各类操作的成功/失败统计 + 最近一条错误。"""
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from app.db.database import get_session
    from app.crashguard.models import CrashAuditLog

    since = datetime.utcnow() - timedelta(hours=max(1, min(int(hours), 168)))
    async with get_session() as session:
        rows = (await session.execute(
            select(CrashAuditLog).where(CrashAuditLog.created_at >= since)
            .order_by(CrashAuditLog.created_at.desc())
        )).scalars().all()

    by_op: Dict[str, Dict[str, Any]] = {}
    recent_errors: List[Dict[str, Any]] = []
    for r in rows:
        op = r.op or "unknown"
        bucket = by_op.setdefault(op, {"success": 0, "failed": 0, "last_at": None})
        if r.success:
            bucket["success"] += 1
        else:
            bucket["failed"] += 1
            if len(recent_errors) < 10:
                recent_errors.append({
                    "op": op,
                    "target_id": r.target_id,
                    "error": r.error,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                })
        if not bucket["last_at"]:
            bucket["last_at"] = r.created_at.isoformat() if r.created_at else None
    return {
        "window_hours": hours,
        "total": len(rows),
        "by_op": by_op,
        "recent_errors": recent_errors,
    }


class PrewarmRequest(BaseModel):
    target_date: Optional[date] = Field(None, description="默认今日")
    max_issues: int = Field(30, ge=1, le=100)
    only_missing: bool = Field(True, description="True=仅补 top_os 为空的；False=全量刷新")


@router.post("/prewarm-distributions")
async def prewarm_distributions(req: PrewarmRequest) -> Dict[str, Any]:
    """
    手动给今日 snapshot 的 issue 拉 RUM 分布，写回 crash_issues.top_os/top_app_version/top_device。
    早晚报里"❓ 未确定"桶清空靠它。
    """
    from app.crashguard.services.distribution_prewarmer import prewarm_today_distributions
    from datetime import date as _date

    target = req.target_date or _date.today()
    try:
        result = await prewarm_today_distributions(
            today=target,
            max_issues=req.max_issues,
            only_missing=req.only_missing,
        )
    except Exception as e:
        logger.exception("prewarm-distributions failed")
        raise HTTPException(status_code=500, detail=f"prewarm failed: {e}")
    return {"target_date": target.isoformat(), **result}


class ApprovePrRequest(BaseModel):
    approver: str = Field("human", description="approver 标识，可填飞书 open_id 或邮箱")
    dry_run: bool = Field(False, description="True 时只返回 branch / pr_body 不真推 git")


@router.post("/approve-pr/{analysis_id}")
async def approve_pr(analysis_id: int, req: ApprovePrRequest) -> Dict[str, Any]:
    """
    人工 ✋ approve 后创建 draft PR。
    强制 --draft，永远不合入。同 issue+platform 30 天内只允许一次。
    """
    from app.crashguard.services.pr_drafter import draft_pr_for_analysis

    try:
        result = await draft_pr_for_analysis(
            analysis_id=analysis_id,
            approver=req.approver or "human",
            dry_run=req.dry_run,
        )
    except Exception as e:
        logger.exception("approve-pr failed for analysis_id=%d", analysis_id)
        raise HTTPException(status_code=500, detail=f"approve-pr failed: {e}")
    if not result.get("ok") and not req.dry_run:
        # 业务校验失败用 4xx 而非 5xx
        raise HTTPException(status_code=400, detail=result)
    return result


_ALLOWED_STATUS = {"open", "investigating", "resolved_by_pr", "ignored", "wontfix"}


class IssuePatch(BaseModel):
    status: Optional[str] = Field(None, description="open / investigating / resolved_by_pr / ignored / wontfix")
    assignee: Optional[str] = Field(None, description="指派人 username（空字符串=取消指派）")


@router.patch("/issues/{issue_id}")
async def patch_issue(issue_id: str, patch: IssuePatch) -> Dict[str, Any]:
    """更新 issue 的指派人 / 状态。"""
    from sqlalchemy import select
    from app.db.database import get_session
    from app.crashguard.models import CrashIssue

    if patch.status is not None and patch.status not in _ALLOWED_STATUS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid status; must be one of {sorted(_ALLOWED_STATUS)}",
        )

    async with get_session() as session:
        row = (await session.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=f"issue {issue_id} not found")

        if patch.status is not None:
            row.status = patch.status
        if patch.assignee is not None:
            row.assignee = patch.assignee.strip()
        await session.commit()
        return {
            "datadog_issue_id": row.datadog_issue_id,
            "status": row.status or "open",
            "assignee": getattr(row, "assignee", "") or "",
        }
