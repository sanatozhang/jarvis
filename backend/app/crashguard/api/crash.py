"""crashguard API — manual trigger / health"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.crashguard.config import get_crashguard_settings

logger = logging.getLogger("crashguard.api")


def _require_enabled(request: Request) -> None:
    """Gate：crashguard 关闭时整个子模块返回 403。

    例外：/health 始终可访问，frontend 用它探测开关状态。
    """
    if request.url.path.endswith("/health"):
        return
    s = get_crashguard_settings()
    if not s.enabled:
        raise HTTPException(
            status_code=403,
            detail="crashguard is disabled (set CRASHGUARD_ENABLED=true to enable)",
        )


router = APIRouter(
    prefix="/api/crash",
    tags=["crashguard"],
    dependencies=[Depends(_require_enabled)],
)


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
    from sqlalchemy import select

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
        # 批量补 PR 状态：每个 issue 取最新一条 PR
        issue_ids = [item["datadog_issue_id"] for item in top]
        pr_map: Dict[str, Dict[str, Any]] = {}
        if issue_ids:
            from app.crashguard.models import CrashPullRequest
            pr_rows = (await session.execute(
                select(CrashPullRequest)
                .where(CrashPullRequest.datadog_issue_id.in_(issue_ids))
                .order_by(CrashPullRequest.created_at.desc())
            )).scalars().all()
            for pr in pr_rows:
                # 同一 issue 多条 PR 时，最新创建的覆盖（按 created_at desc 来的，第一次写入即最新）
                pr_map.setdefault(pr.datadog_issue_id, {
                    "pr_url": pr.pr_url or "",
                    "pr_number": pr.pr_number,
                    "pr_status": pr.pr_status or "draft",
                    "pr_repo": pr.repo or "",
                })

    for item in top:
        item["datadog_url"] = _datadog_url_for(item["datadog_issue_id"])
        pr = pr_map.get(item["datadog_issue_id"])
        item["has_pr"] = pr is not None
        item["pr_url"] = pr["pr_url"] if pr else ""
        item["pr_number"] = pr["pr_number"] if pr else None
        item["pr_status"] = pr["pr_status"] if pr else ""
        item["pr_repo"] = pr["pr_repo"] if pr else ""
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

    # 关联的 PR 列表（最多 5 条，最新在前）
    pull_requests: List[Dict[str, Any]] = []
    async with get_session() as session:
        from app.crashguard.models import CrashPullRequest
        pr_rows = (await session.execute(
            select(CrashPullRequest)
            .where(CrashPullRequest.datadog_issue_id == issue_id)
            .order_by(CrashPullRequest.created_at.desc())
            .limit(5)
        )).scalars().all()
        for pr in pr_rows:
            pull_requests.append({
                "id": pr.id,
                "pr_url": pr.pr_url,
                "pr_number": pr.pr_number,
                "pr_status": pr.pr_status or "draft",
                "repo": pr.repo or "",
                "branch_name": pr.branch_name or "",
                "created_at": pr.created_at.isoformat() if pr.created_at else None,
                "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
                "closed_at": pr.closed_at.isoformat() if pr.closed_at else None,
                "last_synced_at": pr.last_synced_at.isoformat() if pr.last_synced_at else None,
            })

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
        "pull_requests": pull_requests,
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


@router.get("/reports/history")
async def list_reports_history(
    days: int = Query(30, ge=1, le=180),
    report_type: Optional[str] = Query(None, regex="^(morning|evening)$"),
    limit: int = Query(60, ge=1, le=180),
) -> Dict[str, Any]:
    """列出最近 N 天的历史早晚报（含 attention 计数 + payload 摘要）。"""
    from datetime import datetime, timedelta, date as _date
    from sqlalchemy import select, desc
    from app.db.database import get_session
    from app.crashguard.models import CrashDailyReport
    import json as _json

    since = _date.today() - timedelta(days=days)
    async with get_session() as session:
        stmt = select(CrashDailyReport).where(CrashDailyReport.report_date >= since)
        if report_type:
            stmt = stmt.where(CrashDailyReport.report_type == report_type)
        stmt = stmt.order_by(
            desc(CrashDailyReport.report_date),
            desc(CrashDailyReport.created_at),
        ).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()

    items: List[Dict[str, Any]] = []
    for r in rows:
        try:
            payload = _json.loads(r.report_payload or "{}")
        except Exception:
            payload = {}
        items.append({
            "id": r.id,
            "report_date": r.report_date.isoformat() if r.report_date else None,
            "report_type": r.report_type,
            "top_n": r.top_n,
            "new_count": r.new_count,
            "regression_count": r.regression_count,
            "surge_count": r.surge_count,
            "feishu_message_id": r.feishu_message_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "summary": payload.get("summary") or "",
            "attention_total": (
                int(r.new_count or 0) + int(r.regression_count or 0) + int(r.surge_count or 0)
            ),
        })
    return {"items": items, "total": len(items), "days": days}


@router.get("/reports/{report_id}")
async def get_report_detail(report_id: int) -> Dict[str, Any]:
    """单份历史报告的完整 markdown + payload"""
    from sqlalchemy import select
    from app.db.database import get_session
    from app.crashguard.services.daily_report import compose_report
    from app.crashguard.models import CrashDailyReport
    import json as _json

    async with get_session() as session:
        row = (await session.execute(
            select(CrashDailyReport).where(CrashDailyReport.id == report_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="report not found")
        try:
            payload = _json.loads(row.report_payload or "{}")
        except Exception:
            payload = {}

    # 历史报告未存全量 markdown，重新基于落库时的当日数据 compose 一次
    try:
        text, _ = await compose_report(
            row.report_type, row.report_date, top_n=int(row.top_n or 5)
        )
    except Exception:
        text = "_报告内容已过期，无法重新生成（数据已轮转）_"

    return {
        "id": row.id,
        "report_date": row.report_date.isoformat() if row.report_date else None,
        "report_type": row.report_type,
        "markdown": text,
        "payload": payload,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/pull-requests")
async def list_pull_requests(
    days: int = Query(30, ge=1, le=180),
    status: Optional[str] = Query(None, regex="^(draft|open|merged|closed)$"),
    repo: Optional[str] = Query(None, regex="^(flutter|android|ios|app)$"),
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """自动 PR 列表（含 issue 标题 + 平台 + 状态）"""
    from datetime import datetime, timedelta
    from sqlalchemy import select, desc
    from app.db.database import get_session
    from app.crashguard.models import CrashPullRequest, CrashIssue, CrashAnalysis

    since = datetime.utcnow() - timedelta(days=days)
    async with get_session() as session:
        stmt = select(CrashPullRequest).where(CrashPullRequest.created_at >= since)
        if status:
            stmt = stmt.where(CrashPullRequest.pr_status == status)
        if repo:
            stmt = stmt.where(CrashPullRequest.repo == repo)
        stmt = stmt.order_by(desc(CrashPullRequest.created_at)).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()

        # 批量补 issue title + analysis feasibility
        issue_ids = [r.datadog_issue_id for r in rows]
        analysis_ids = [r.analysis_id for r in rows]
        title_map: Dict[str, str] = {}
        feas_map: Dict[int, float] = {}
        if issue_ids:
            issues = (await session.execute(
                select(CrashIssue).where(CrashIssue.datadog_issue_id.in_(issue_ids))
            )).scalars().all()
            title_map = {i.datadog_issue_id: i.title or "" for i in issues}
        if analysis_ids:
            analyses = (await session.execute(
                select(CrashAnalysis).where(CrashAnalysis.id.in_(analysis_ids))
            )).scalars().all()
            feas_map = {a.id: float(a.feasibility_score or 0.0) for a in analyses}

    items: List[Dict[str, Any]] = []
    for r in rows:
        items.append({
            "id": r.id,
            "datadog_issue_id": r.datadog_issue_id,
            "title": title_map.get(r.datadog_issue_id, ""),
            "repo": r.repo,
            "branch_name": r.branch_name,
            "pr_url": r.pr_url,
            "pr_number": r.pr_number,
            "pr_status": r.pr_status,
            "triggered_by": r.triggered_by,
            "approved_by": r.approved_by,
            "approved_at": r.approved_at.isoformat() if r.approved_at else None,
            "feasibility": feas_map.get(r.analysis_id, 0.0),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "merged_at": r.merged_at.isoformat() if r.merged_at else None,
            "closed_at": r.closed_at.isoformat() if r.closed_at else None,
            "last_synced_at": r.last_synced_at.isoformat() if r.last_synced_at else None,
        })
    return {"items": items, "total": len(items), "days": days}


@router.post("/pull-requests/{pr_id}/refresh")
async def refresh_pull_request(pr_id: int) -> Dict[str, Any]:
    """手动触发单条 PR 状态同步（前端按钮用）。"""
    from app.crashguard.services.pr_sync import sync_pr
    return await sync_pr(pr_id)


@router.post("/pull-requests/sync-all")
async def sync_all_pull_requests() -> Dict[str, Any]:
    """批量同步所有非终态 PR（cron 用，手动也可调）。"""
    from app.crashguard.services.pr_sync import sync_all_open_prs
    res = await sync_all_open_prs()
    # 不把每条 detail 全返回到前端（噪声），只给汇总
    return {
        "checked": res.get("checked", 0),
        "changed": res.get("changed", 0),
        "errors": res.get("errors", 0),
    }


class BackfillAutoPrRequest(BaseModel):
    days: int = Field(7, ge=1, le=90, description="回溯最近 N 天的 success 分析")
    dry_run: bool = Field(False, description="True=只列出候选，不实际建 PR")
    min_feasibility: Optional[float] = Field(None, description="覆盖 config 阈值")
    limit: int = Field(0, ge=0, le=100, description="最多创建 N 个 PR，0=不限")


@router.post("/backfill-auto-pr")
async def backfill_auto_pr(req: BackfillAutoPrRequest) -> Dict[str, Any]:
    """对历史 success 分析（feasibility ≥ threshold 且未建过 PR）批量补 draft PR。

    用途：在加自动 PR 勾子之前已经跑过的成功分析没机会触发 _maybe_auto_draft_pr，
    需要这个端点一次性补齐。
    """
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from app.db.database import get_session
    from app.crashguard.models import CrashAnalysis, CrashPullRequest
    from app.crashguard.services.pr_drafter import draft_pr_for_analysis
    from app.crashguard.services.audit import write_audit

    s = get_crashguard_settings()
    threshold = float(req.min_feasibility if req.min_feasibility is not None
                      else getattr(s, "feasibility_pr_threshold", 0.7) or 0.7)
    since = datetime.utcnow() - timedelta(days=req.days)

    candidates: List[Dict[str, Any]] = []
    async with get_session() as session:
        rows = (await session.execute(
            select(CrashAnalysis).where(
                CrashAnalysis.status == "success",
                CrashAnalysis.followup_question == "",
                CrashAnalysis.feasibility_score >= threshold,
                CrashAnalysis.created_at >= since,
            )
        )).scalars().all()
        # 过滤掉没对应 sub-repo 的 platform（browser/desktop/未知）
        from app.crashguard.models import CrashIssue
        issue_ids = list({a.datadog_issue_id for a in rows})
        plat_map: Dict[str, str] = {}
        if issue_ids:
            issues = (await session.execute(
                select(CrashIssue).where(CrashIssue.datadog_issue_id.in_(issue_ids))
            )).scalars().all()
            plat_map = {i.datadog_issue_id: (i.platform or "").lower() for i in issues}
        VALID_PLATFORMS = {"android", "ios", "flutter"}
        rows = [a for a in rows if plat_map.get(a.datadog_issue_id) in VALID_PLATFORMS]
        existing_pr_ana_ids = set(
            r[0] for r in (await session.execute(
                select(CrashPullRequest.analysis_id)
            )).all()
        )

    triggered = 0
    skipped_dup = 0
    failed: List[Dict[str, str]] = []
    candidates_out: List[Dict[str, Any]] = []
    limit = int(req.limit or 0)
    for ana in rows:
        # limit > 0 时，只创建 limit 个 PR；后面的标 skipped_limit
        if limit > 0 and triggered >= limit and not req.dry_run:
            candidates_out.append({
                "analysis_id": ana.id,
                "issue_id": ana.datadog_issue_id,
                "feasibility": float(ana.feasibility_score or 0.0),
                "status": "skipped_limit",
            })
            continue
        info = {
            "analysis_id": ana.id,
            "issue_id": ana.datadog_issue_id,
            "feasibility": float(ana.feasibility_score or 0.0),
        }
        if ana.id in existing_pr_ana_ids:
            info["status"] = "skipped_existing_pr"
            skipped_dup += 1
            candidates_out.append(info)
            continue
        if req.dry_run:
            info["status"] = "would_create"
            candidates_out.append(info)
            continue
        try:
            res = await draft_pr_for_analysis(ana.id, approver="backfill")
            if res.get("ok"):
                info["status"] = "created"
                info["pr_url"] = res.get("pr_url", "")
                triggered += 1
            else:
                info["status"] = "failed"
                info["error"] = res.get("error", "")
                failed.append({"analysis_id": str(ana.id), "error": res.get("error", "")})
        except Exception as exc:
            info["status"] = "exception"
            info["error"] = str(exc)[:300]
            failed.append({"analysis_id": str(ana.id), "error": str(exc)[:300]})
        candidates_out.append(info)
        try:
            await write_audit(
                op="backfill_auto_pr",
                target_id=str(ana.id),
                success=info["status"] == "created",
                detail=str(info)[:500],
                error=info.get("error", "") if info["status"] != "created" else None,
            )
        except Exception:
            pass

    return {
        "threshold": threshold,
        "days": req.days,
        "dry_run": req.dry_run,
        "total_candidates": len(rows),
        "triggered": triggered,
        "skipped_existing_pr": skipped_dup,
        "failed_count": len(failed),
        "candidates": candidates_out,
    }


class AuditCleanupRequest(BaseModel):
    keep_days: int = Field(30, ge=7, le=365, description="保留最近 N 天，超出删除")


@router.post("/audit-cleanup")
async def audit_cleanup(req: AuditCleanupRequest) -> Dict[str, Any]:
    """清理超过 N 天的审计日志（防止表无限增长）。"""
    from datetime import datetime, timedelta
    from sqlalchemy import delete
    from app.db.database import get_session
    from app.crashguard.models import CrashAuditLog
    from app.crashguard.services.audit import write_audit

    cutoff = datetime.utcnow() - timedelta(days=req.keep_days)
    async with get_session() as session:
        result = await session.execute(
            delete(CrashAuditLog).where(CrashAuditLog.created_at < cutoff)
        )
        deleted = int(getattr(result, "rowcount", 0) or 0)
        await session.commit()

    try:
        await write_audit(
            op="audit_cleanup",
            target_id=str(req.keep_days),
            success=True,
            detail=f"deleted {deleted} rows older than {req.keep_days}d",
        )
    except Exception:
        pass
    return {"deleted": deleted, "keep_days": req.keep_days, "cutoff": cutoff.isoformat()}
