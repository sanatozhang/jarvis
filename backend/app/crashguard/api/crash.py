"""crashguard API — manual trigger / health"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.crashguard.config import get_crashguard_settings

logger = logging.getLogger("crashguard.api")

# fire-and-forget 后台任务强引用——asyncio.create_task() 返回值若不保留，
# Python GC 可能在任务运行中将其回收 → 任务静默消失。用 set 保留 + done callback 释放。
_BG_TASKS: set = set()


def _spawn_bg(coro, name: str = "crashguard-bg"):
    import asyncio
    task = asyncio.create_task(coro, name=name)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


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


@router.post("/warmup")
async def trigger_warmup() -> Dict[str, Any]:
    """立即拉数（同步，几秒返回），AI 分析 fire-and-forget 后台跑。

    返回的 analyzed / auto_pr 字段保留兼容前端（值为 0/queued，后台真分析完成后入库）。
    """
    import asyncio
    from datetime import date as _date

    s = get_crashguard_settings()
    if not s.enabled:
        raise HTTPException(status_code=503, detail="crashguard 已被 kill switch 关闭")
    from app.crashguard.workers.warmup import (
        run_ai_analysis_phase,
        run_data_only,
    )
    try:
        data = await run_data_only(reason="manual")
    except Exception as e:
        logger.exception("manual warmup data phase failed")
        raise HTTPException(status_code=500, detail=f"warmup failed: {e}")

    # 数据阶段已完成（前端能看到新 issue），AI 分析后台跑——不阻塞响应
    # ⚠️ 用 _spawn_bg 保留强引用，避免 asyncio.create_task 返回值丢失被 GC 回收
    if not data.get("skipped"):
        try:
            today = _date.fromisoformat(data["today"])
            _spawn_bg(
                run_ai_analysis_phase(today=today, reason="manual-bg"),
                name=f"ai-analysis-{today.isoformat()}",
            )
            logger.info("manual warmup: AI analysis dispatched in background for %s", today)
        except Exception:
            logger.exception("failed to schedule background AI analysis")

    return {
        "issues_processed": data.get("issues_processed", 0),
        "top_n_count": data.get("top_n_count", 0),
        "attention_count": 0,   # 真值在后台计算，前端通过 /auto-pr-queue 轮询
        "analyzed": 0,
        "auto_pr": {"scanned": 0, "attempted": 0, "created": 0, "skipped": 0, "failed": [], "queued": True},
        "ai_background": not data.get("skipped"),
    }


@router.get("/auto-pr-queue")
async def auto_pr_queue() -> Dict[str, Any]:
    """自动 PR 队列状态总览。

    返回 4 个分桶（每桶最多 30 条），让前端渲染进度面板：
    - pending: success 且 feasibility≥阈值，但还没建过 PR、也没失败审计
    - running: CrashAnalysis.status == "running"（AI 在分析）
    - recent_prs: 最近 30 天的真实 PR（已建出来）
    - recent_failures: 最近 7 天的 auto_draft_pr 失败审计（含原因）
    """
    from datetime import datetime, timedelta
    from sqlalchemy import select, desc
    from app.db.database import get_session
    from app.crashguard.models import (
        CrashAnalysis, CrashPullRequest, CrashAuditLog, CrashIssue,
    )

    s = get_crashguard_settings()
    threshold = float(getattr(s, "feasibility_pr_threshold", 0.7) or 0.7)
    now = datetime.utcnow()
    pr_since = now - timedelta(days=30)
    audit_since = now - timedelta(days=7)
    pending_since = now - timedelta(days=14)
    VALID_PLATFORMS = {"android", "ios", "flutter"}

    async with get_session() as session:
        # 1. running: AI 正在分析
        running_rows = (await session.execute(
            select(CrashAnalysis).where(
                CrashAnalysis.status == "running",
                CrashAnalysis.followup_question == "",
            ).order_by(desc(CrashAnalysis.id)).limit(30)
        )).scalars().all()

        # 2. pending: success + feasibility≥阈值 + 无 PR + 无失败审计
        success_rows = (await session.execute(
            select(CrashAnalysis).where(
                CrashAnalysis.status == "success",
                CrashAnalysis.followup_question == "",
                CrashAnalysis.feasibility_score >= threshold,
                CrashAnalysis.created_at >= pending_since,
            ).order_by(desc(CrashAnalysis.id))
        )).scalars().all()
        pr_ana_ids = set(
            r[0] for r in (await session.execute(
                select(CrashPullRequest.analysis_id)
            )).all() if r[0] is not None
        )
        failed_ana_ids = set(
            (a.target_id for a in (await session.execute(
                select(CrashAuditLog).where(
                    CrashAuditLog.op == "auto_draft_pr",
                    CrashAuditLog.success == False,  # noqa: E712
                    CrashAuditLog.created_at >= audit_since,
                )
            )).scalars().all())
        )
        # 解析 issue 拿 title/platform
        all_issue_ids = list({a.datadog_issue_id for a in success_rows} |
                             {a.datadog_issue_id for a in running_rows})
        plat_map: Dict[str, Dict[str, str]] = {}
        if all_issue_ids:
            issues = (await session.execute(
                select(CrashIssue).where(CrashIssue.datadog_issue_id.in_(all_issue_ids))
            )).scalars().all()
            for i in issues:
                plat_map[i.datadog_issue_id] = {
                    "title": (i.title or "")[:120],
                    "platform": (i.platform or "").lower(),
                }

        pending = []
        for a in success_rows:
            if a.id in pr_ana_ids:
                continue
            if str(a.id) in failed_ana_ids:
                continue
            meta = plat_map.get(a.datadog_issue_id, {})
            if meta.get("platform") not in VALID_PLATFORMS:
                continue
            pending.append({
                "analysis_id": a.id,
                "datadog_issue_id": a.datadog_issue_id,
                "title": meta.get("title", ""),
                "platform": meta.get("platform", ""),
                "feasibility_score": a.feasibility_score,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            })
            if len(pending) >= 30:
                break

        running = [{
            "analysis_id": a.id,
            "datadog_issue_id": a.datadog_issue_id,
            "title": plat_map.get(a.datadog_issue_id, {}).get("title", ""),
            "platform": plat_map.get(a.datadog_issue_id, {}).get("platform", ""),
            "started_at": a.created_at.isoformat() if a.created_at else None,
        } for a in running_rows]

        # 3. recent PRs
        prs = (await session.execute(
            select(CrashPullRequest).where(
                CrashPullRequest.created_at >= pr_since
            ).order_by(desc(CrashPullRequest.id)).limit(30)
        )).scalars().all()
        recent_prs = [{
            "id": p.id,
            "datadog_issue_id": p.datadog_issue_id,
            "repo": p.repo,
            "pr_number": p.pr_number,
            "pr_url": p.pr_url,
            "pr_status": p.pr_status,
            "branch_name": p.branch_name,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        } for p in prs]

        # 4. recent failures
        fails = (await session.execute(
            select(CrashAuditLog).where(
                CrashAuditLog.op == "auto_draft_pr",
                CrashAuditLog.success == False,  # noqa: E712
                CrashAuditLog.created_at >= audit_since,
            ).order_by(desc(CrashAuditLog.id)).limit(30)
        )).scalars().all()
        recent_failures = [{
            "analysis_id": int(f.target_id) if (f.target_id or "").isdigit() else f.target_id,
            "error": (f.error or "")[:200],
            "created_at": f.created_at.isoformat() if f.created_at else None,
        } for f in fails]

    return {
        "threshold": threshold,
        "summary": {
            "pending": len(pending),
            "running": len(running),
            "recent_prs": len(recent_prs),
            "recent_failures": len(recent_failures),
        },
        "pending": pending,
        "running": running,
        "recent_prs": recent_prs,
        "recent_failures": recent_failures,
    }


@router.get("/latest-release")
async def get_latest_release() -> Dict[str, Any]:
    """获取各平台「线上最新版本」+「用户量最大版本」。

    最新版本口径：
      - 配置 `current_release.{flutter,android,ios}` 优先（手动覆盖）
      - 否则按崩溃数据派生：版本累计 events ≥ `latest_version_min_events`（默认 300）后取最大 semver
      - 都不满足返回 ""

    用户量最大版本口径（仅 android / ios，flutter 也跑在这俩上）：
      - Datadog RUM @type:session 24h 窗口，cardinality(@usr.id) group by (@os.name, @application.version)
      - 失败/无数据时回落 crash_issues.top_app_version 加权聚合
    """
    from app.crashguard.services.datadog_client import DatadogClient
    from app.crashguard.services.version_util import (
        derive_top_user_version_from_crashes,
        resolve_effective_latest_release,
    )
    from app.db.database import get_session

    s = get_crashguard_settings()
    overrides = {
        "flutter": s.current_release_flutter,
        "android": s.current_release_android,
        "ios": s.current_release_ios,
    }
    result: Dict[str, str] = {}
    async with get_session() as session:
        for platform, override in overrides.items():
            result[platform] = await resolve_effective_latest_release(
                session=session,
                platform=platform,
                override=override,
                min_events=s.latest_version_min_events,
            )

    # 用户量最大版本：优先 Datadog RUM，失败回落 crash_issues 聚合
    top_user: Dict[str, Dict[str, Any]] = {}
    top_user_source: Dict[str, str] = {}
    if s.datadog_api_key:
        try:
            client = DatadogClient(
                api_key=s.datadog_api_key,
                app_key=s.datadog_app_key,
                site=s.datadog_site,
            )
            top_user = await client.top_user_version_by_platform(window_hours=24)
            for p in ("android", "ios"):
                if top_user.get(p):
                    top_user_source[p] = "datadog_rum"
        except Exception as exc:
            logger.warning("top_user_version_by_platform fetch failed: %s", exc)

    # Fallback：crash_issues.top_app_version 加权聚合
    async with get_session() as session:
        for p in ("android", "ios"):
            if top_user.get(p):
                continue
            fallback = await derive_top_user_version_from_crashes(session, platform=p)
            if fallback:
                top_user[p] = fallback
                top_user_source[p] = "crash_issues_fallback"
            else:
                top_user_source[p] = "unknown"

    return {
        "versions": result,
        "min_events_threshold": s.latest_version_min_events,
        "source": {
            p: ("config_override" if overrides[p].strip() else
                ("derived" if result[p] else "unknown"))
            for p in overrides
        },
        "top_user_versions": top_user,           # {"android": {"version":"...","users":N}, "ios": {...}}
        "top_user_versions_source": top_user_source,
    }


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


def _datadog_url_for(issue_id: str, window_hours: int = 24) -> str:
    """Datadog Error Tracking issue 跳转链接（RUM track 路径）。

    window_hours: 显式传入时附 `from_ts/to_ts`（毫秒），让 Datadog UI 默认窗口对齐我们的口径；
    不传则 Datadog 自动 fallback "Past 14 Days"（UI 默认）—— 这就是你看到 14 天数据的原因。
    """
    import time as _time
    s = get_crashguard_settings()
    site = (s.datadog_site or "datadoghq.com").strip()
    if site == "datadoghq.com":
        host = "app.datadoghq.com"
    elif site.startswith("app."):
        host = site
    else:
        host = f"app.{site}"
    base = f"https://{host}/rum/error-tracking/issue/{issue_id}"
    if window_hours and window_hours > 0:
        to_ms = int(_time.time() * 1000)
        from_ms = to_ms - int(window_hours) * 3600 * 1000
        return f"{base}?from_ts={from_ms}&to_ts={to_ms}&live=true"
    return base


# 支持的时间窗口档位（小时）。1d / 7d / 14d / 30d
_ALLOWED_WINDOW_HOURS = {24, 168, 336, 720}


async def _aggregate_snapshots_window(
    session, issue_ids: List[str], target_date: date, window_hours: int,
) -> Dict[str, Dict[str, int]]:
    """跨 N 天 CrashSnapshot 聚合 events / users / sessions。

    window_hours=24 → 只取 target_date 当天（与默认 pick_top_n 行为一致，由 caller 跳过此路径）。
    window_hours=N*24 → 取 [target_date - (N-1) days, target_date] 闭区间 sum。

    返回 {issue_id: {events_count, users_affected, sessions_affected}}。缺失的 issue 不返回。
    """
    from sqlalchemy import select, func
    from app.crashguard.models import CrashSnapshot

    if not issue_ids or window_hours <= 24:
        return {}
    days = max(1, window_hours // 24)
    start_date = target_date - timedelta(days=days - 1)
    rows = (await session.execute(
        select(
            CrashSnapshot.datadog_issue_id,
            func.sum(CrashSnapshot.events_count).label("events"),
            func.sum(CrashSnapshot.users_affected).label("users"),
            func.sum(CrashSnapshot.sessions_affected).label("sessions"),
        )
        .where(
            CrashSnapshot.datadog_issue_id.in_(issue_ids),
            CrashSnapshot.snapshot_date >= start_date,
            CrashSnapshot.snapshot_date <= target_date,
        )
        .group_by(CrashSnapshot.datadog_issue_id)
    )).all()
    return {
        r[0]: {
            "events_count": int(r[1] or 0),
            "users_affected": int(r[2] or 0),
            "sessions_affected": int(r[3] or 0),
        }
        for r in rows
    }


@router.get("/top")
async def get_top(
    target_date: Optional[date] = None,
    # 旧字段（兼容）：未传 page 时按 limit 截断
    limit: int = 40,
    kinds: str = "all",
    # 新分页字段
    page: Optional[int] = Query(None, ge=1),
    page_size: int = Query(40, ge=1, le=100),
    # 全后端过滤（首页分页化后客户端不再 useMemo 过滤）
    fatality: str = "",
    platform: str = "",
    status: str = "",
    search: str = "",
    sort_by: str = "events",  # events / impact / users / new_first
    window_hours: int = Query(24, description="时间窗口（小时）：24 / 168 / 336 / 720 = 1d/7d/14d/30d"),
) -> Dict[str, Any]:
    """读取指定日期的 issue 列表（首页用，**跳过早晚报 dedup**，列今日全集）。

    - kinds: 逗号分隔类别白名单；默认 "all"
    - fatality: "fatal" / "non_fatal" / "" (=全部)
    - platform: "android" / "ios" / "flutter" / "" (=全部)
    - status: "open" / "investigating" / "resolved_by_pr" / "ignored" / "wontfix" / "" (=全部)
    - search: title 子串匹配（大小写不敏感）
    - sort_by: events(默认) / impact / users / new_first
    - window_hours: 时间窗口。**24 直接读今日 snapshot；>24 跨多天 sum CrashSnapshot**。
      三维标签（is_new_in_version / is_regression / is_surge）始终按今日 snapshot 固定，
      不随窗口漂移——窗口拉长只影响 events/users/sessions 数值。
    - 分页：传 `page` 启用；未传则旧行为（按 limit 截取头 N 条，page_size 忽略）

    返回:
      issues[]（仅当前页）, total, page, page_size, total_pages,
      aggregates: {p0_count, surge_count, new_count, fatal_count, non_fatal_count,
                   total_events, total_users}
      date, window_hours
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

    paginated = page is not None
    fatality_norm = (fatality or "").strip().lower()
    platform_norm = (platform or "").strip().lower()
    status_norm = (status or "").strip().lower()
    search_norm = (search or "").strip().lower()
    sort_norm = (sort_by or "events").strip().lower()
    # 时间窗口归一化：未匹配档位 → 退回 24h（颗粒度对齐 Datadog 首页默认）
    if window_hours not in _ALLOWED_WINDOW_HOURS:
        window_hours = 24

    async with get_session() as session:
        # 分页模式 → 拉全集 + skip_dedup；非分页（旧调用方）→ 沿用截断 + dedup
        items = await pick_top_n(
            session,
            today=target_date,
            n=0 if paginated else limit,
            kinds=kind_tuple,
            fatality=fatality_norm if not paginated else "",  # 分页路径下统一在外层过滤
            skip_dedup=paginated,
        )

        # window_hours > 24 → 用跨天 sum 覆盖当日 snapshot 的 events/users/sessions（标签不动）
        if window_hours > 24 and items:
            agg_map = await _aggregate_snapshots_window(
                session,
                issue_ids=[it["datadog_issue_id"] for it in items],
                target_date=target_date,
                window_hours=window_hours,
            )
            for it in items:
                agg = agg_map.get(it["datadog_issue_id"])
                if agg:
                    it["events_count"] = agg["events_count"]
                    it["users_affected"] = agg["users_affected"]
                    it["sessions_affected"] = agg["sessions_affected"]

        # 后端过滤（仅分页路径用；非分页路径已由 pick_top_n 处理 fatality）
        if paginated:
            if fatality_norm in ("fatal", "non_fatal"):
                items = [x for x in items if (x.get("fatality") or "fatal") == fatality_norm]
            if platform_norm:
                items = [x for x in items if (x.get("platform") or "").lower() == platform_norm]
            if status_norm:
                items = [x for x in items if (x.get("status") or "open").lower() == status_norm]
            if search_norm:
                items = [
                    x for x in items
                    if search_norm in (x.get("title") or "").lower()
                    or search_norm in (x.get("datadog_issue_id") or "").lower()
                ]
            # 排序
            if sort_norm == "events":
                items.sort(key=lambda x: x.get("events_count") or 0, reverse=True)
            elif sort_norm == "users":
                items.sort(key=lambda x: x.get("users_affected") or 0, reverse=True)
            elif sort_norm == "new_first":
                items.sort(key=lambda x: (
                    1 if x.get("is_new_in_version") else 0,
                    x.get("events_count") or 0,
                ), reverse=True)
            else:  # impact（默认或显式）
                items.sort(key=lambda x: x.get("crash_free_impact_score") or 0.0, reverse=True)

        # 聚合（基于过滤后但未分页的全集，便于头部展示真实统计）
        aggregates = {
            "p0_count": sum(1 for x in items if x.get("tier") == "P0"),
            "surge_count": sum(1 for x in items if x.get("is_surge")),
            "new_count": sum(1 for x in items if x.get("is_new_in_version")),
            "fatal_count": sum(1 for x in items if (x.get("fatality") or "fatal") == "fatal"),
            "non_fatal_count": sum(1 for x in items if x.get("fatality") == "non_fatal"),
            "fatal_events": sum(
                int(x.get("events_count") or 0)
                for x in items if (x.get("fatality") or "fatal") == "fatal"
            ),
            "non_fatal_events": sum(
                int(x.get("events_count") or 0)
                for x in items if x.get("fatality") == "non_fatal"
            ),
            "total_events": sum(int(x.get("events_count") or 0) for x in items),
            "total_users": sum(int(x.get("users_affected") or 0) for x in items),
            "total_sessions": sum(int(x.get("sessions_affected") or 0) for x in items),
        }

        total = len(items)
        if paginated:
            start = (page - 1) * page_size
            end = start + page_size
            page_items = items[start:end]
            total_pages = max(1, (total + page_size - 1) // page_size)
        else:
            page_items = items
            total_pages = 1

        # 仅给当前页 issue 补 PR/分析（控成本）
        issue_ids = [item["datadog_issue_id"] for item in page_items]
        pr_map: Dict[str, Dict[str, Any]] = {}
        ana_map: Dict[str, Dict[str, Any]] = {}
        if issue_ids:
            from app.crashguard.models import CrashAnalysis, CrashPullRequest
            pr_rows = (await session.execute(
                select(CrashPullRequest)
                .where(CrashPullRequest.datadog_issue_id.in_(issue_ids))
                .order_by(CrashPullRequest.created_at.desc())
            )).scalars().all()
            for pr in pr_rows:
                pr_map.setdefault(pr.datadog_issue_id, {
                    "pr_url": pr.pr_url or "",
                    "pr_number": pr.pr_number,
                    "pr_status": pr.pr_status or "draft",
                    "pr_repo": pr.repo or "",
                })
            ana_rows = (await session.execute(
                select(CrashAnalysis)
                .where(
                    CrashAnalysis.datadog_issue_id.in_(issue_ids),
                    CrashAnalysis.followup_question == "",
                    CrashAnalysis.status == "success",
                )
                .order_by(CrashAnalysis.id.desc())
            )).scalars().all()
            for ana in ana_rows:
                ana_map.setdefault(ana.datadog_issue_id, {
                    "analysis_id": ana.id,
                    "analysis_feasibility_score": float(ana.feasibility_score or 0.0),
                    "analysis_confidence": ana.confidence or "",
                })

    for item in page_items:
        item["datadog_url"] = _datadog_url_for(item["datadog_issue_id"], window_hours=window_hours)
        pr = pr_map.get(item["datadog_issue_id"])
        item["has_pr"] = pr is not None
        item["pr_url"] = pr["pr_url"] if pr else ""
        item["pr_number"] = pr["pr_number"] if pr else None
        item["pr_status"] = pr["pr_status"] if pr else ""
        item["pr_repo"] = pr["pr_repo"] if pr else ""
        ana = ana_map.get(item["datadog_issue_id"])
        item["analysis_id"] = ana["analysis_id"] if ana else None
        item["analysis_feasibility_score"] = ana["analysis_feasibility_score"] if ana else None
        item["analysis_confidence"] = ana["analysis_confidence"] if ana else ""

    out: Dict[str, Any] = {
        "date": target_date.isoformat(),
        "window_hours": window_hours,
        "count": len(page_items),
        "issues": page_items,
        "total": total,
        "aggregates": aggregates,
    }
    if paginated:
        out["page"] = page
        out["page_size"] = page_size
        out["total_pages"] = total_pages
    return out


@router.get("/issues/{issue_id}")
async def get_issue_detail(
    issue_id: str,
    target_date: Optional[date] = None,
    window_hours: int = Query(24, description="时间窗口：24/168/336/720 = 1d/7d/14d/30d"),
) -> Dict[str, Any]:
    """
    单 issue 详情：基础属性 + 当日快照 + 代表性堆栈。

    window_hours：影响 `total_events` / `total_users_affected` 的口径（CrashSnapshot 跨 N 天 sum）
    和 datadog_url 的 from_ts/to_ts。24h 默认对齐 Datadog 首页 "Past 1 Day"。
    """
    from sqlalchemy import select
    from app.db.database import get_session
    from app.crashguard.models import CrashIssue, CrashSnapshot, CrashAnalysis
    import json as _json

    if target_date is None:
        target_date = date.today()
    if window_hours not in _ALLOWED_WINDOW_HOURS:
        window_hours = 24

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
            "id": analysis.id,
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

    # 窗口聚合：window_hours > 24 时跨多天 sum；否则用 issue 上原始字段（max-ever）
    window_total_events = int(issue.total_events or 0)
    window_total_users = int(issue.total_users_affected or 0)
    window_total_sessions = 0
    if window_hours > 24:
        async with get_session() as session:
            agg = await _aggregate_snapshots_window(
                session, [issue_id], target_date, window_hours,
            )
            if issue_id in agg:
                window_total_events = agg[issue_id]["events_count"]
                window_total_users = agg[issue_id]["users_affected"]
                window_total_sessions = agg[issue_id]["sessions_affected"]
    else:
        # 24h：直接读今日 snapshot（如有），与 Datadog "Past 1 Day" 对齐
        if snap is not None:
            window_total_events = int(snap.events_count or 0)
            window_total_users = int(snap.users_affected or 0)
            window_total_sessions = int(getattr(snap, "sessions_affected", 0) or 0)

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
        "datadog_url": _datadog_url_for(issue.datadog_issue_id, window_hours=window_hours),
        "window_hours": window_hours,
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
        "total_events": window_total_events,
        "total_users_affected": window_total_users,
        "total_sessions_affected": window_total_sessions,
        # legacy 历史最大值口径（保留以便回看，前端不再展示）
        "lifetime_max_events": int(issue.total_events or 0),
        "lifetime_max_users": int(issue.total_users_affected or 0),
        "representative_stack": issue.representative_stack or "",
        "tags": tags,
        "status": issue.status or "open",
        "assignee": getattr(issue, "assignee", "") or "",
        "snapshot": snap_block,
        "analysis": analysis_block,
        "pull_requests": pull_requests,
    }


class AnalyzeRequest(BaseModel):
    user_prompt: str = Field(
        default="",
        max_length=4000,
        description="可选——用户引导 prompt，会作为 followup_question 注入 AI；空串则跑默认分析",
    )


@router.post("/analyze/{issue_id}")
async def analyze_issue(
    issue_id: str,
    req: Optional[AnalyzeRequest] = None,
) -> Dict[str, Any]:
    """异步触发分析。立即返回 run_id；前端轮询 GET /analyses/{run_id} 查结果。

    可选 body `{"user_prompt": "..."}` 引导 AI 分析方向（复用 followup_question 机制）。
    """
    from app.crashguard.services.analyzer import start_analysis

    user_prompt = (req.user_prompt or "").strip() if req else ""
    try:
        # UI「重新分析」按钮 = 用户主动按下 = 强制重跑（不命中去重窗口）
        # 带 user_prompt 时函数内部已自动当 followup（绕过去重）；这里 force 是兜底
        run_id = await start_analysis(
            issue_id,
            triggered_by="manual",
            followup_question=user_prompt,
            force=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("start_analysis failed for %s", issue_id)
        raise HTTPException(status_code=500, detail=f"start_analysis failed: {e}")
    return {"run_id": run_id, "status": "pending", "user_prompt": user_prompt}


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
    from app.crashguard.services.pr_drafter import draft_prs_multi

    try:
        result = await draft_prs_multi(
            analysis_id=analysis_id,
            approver=req.approver or "human",
            dry_run=req.dry_run,
        )
    except Exception as e:
        logger.exception("approve-pr failed for analysis_id=%d", analysis_id)
        raise HTTPException(status_code=500, detail=f"approve-pr failed: {e}")
    if not result.get("ok") and not req.dry_run:
        raise HTTPException(status_code=400, detail=result)
    return result


class RetryFailedPrsRequest(BaseModel):
    limit: int = Field(50, ge=1, le=200, description="本次最多重试多少个 analysis")
    dry_run: bool = Field(False)
    lookback_days: int = Field(
        30, ge=1, le=180,
        description="audit_log 回溯窗口（天）",
    )
    issue_ids: Optional[List[str]] = Field(
        None,
        description="可选——只重试这些 datadog_issue_id；空 = 全表扫描",
    )
    diagnose_only: bool = Field(
        False,
        description="True 时仅返回候选清单 + audit_log 调试信息，不实际触发 PR",
    )


@router.post("/retry-failed-prs")
async def retry_failed_prs(req: RetryFailedPrsRequest) -> Dict[str, Any]:
    """
    重试"PR 创建失败"的 analysis。

    底层逻辑：PR 创建是 analysis success 后的独立后置步骤，PR 失败不会回退
    `CrashAnalysis.status`。所以 batch-analyze 的去重凭证（status=success）
    会把这些 issue 当"已分析"跳过——它们需要单独入口重新触发 PR。

    **候选源（audit_log 反查，比拍脑袋枚举 CrashAnalysis 准）**:
        crash_audit_logs WHERE op IN ('pr_draft', 'auto_draft_pr')
            AND success=false
            AND error != 'below_threshold'   -- 排除 feasibility 跳过的
            AND created_at >= now() - lookback_days
        DISTINCT target_id (= analysis_id)

    再二次确认：对应 analysis_id 没有非空 `CrashPullRequest.pr_url`（不重复开 PR）。
    feasibility 阈值由 `draft_prs_multi` 内部自行处理；显式重试场景下不再前置过滤。
    """
    from datetime import datetime, timedelta
    from sqlalchemy import select, desc
    from app.crashguard.models import CrashAnalysis, CrashAuditLog, CrashPullRequest
    from app.crashguard.services.pr_drafter import draft_prs_multi
    from app.db.database import get_session

    s = get_crashguard_settings()
    if not s.pr_enabled and not req.diagnose_only:
        raise HTTPException(
            status_code=400,
            detail="pr_enabled is false; 先在 config 打开 PR 总开关",
        )

    since = datetime.utcnow() - timedelta(days=req.lookback_days)

    async with get_session() as session:
        # 1) 从 audit_log 反查失败 PR 尝试
        audit_rows = (await session.execute(
            select(CrashAuditLog).where(
                CrashAuditLog.op.in_(["pr_draft", "auto_draft_pr"]),
                CrashAuditLog.success == False,  # noqa: E712 — SQLA 必须 ==
                CrashAuditLog.created_at >= since,
            ).order_by(desc(CrashAuditLog.created_at))
        )).scalars().all()

        # 去重：同一 analysis_id 取最近一条；过滤 below_threshold
        failed_ana_ids: List[int] = []
        seen_ana: set = set()
        audit_samples: Dict[int, Dict[str, Any]] = {}
        for a in audit_rows:
            err = (a.error or "").strip()
            if err == "below_threshold":
                continue
            try:
                ana_id = int(a.target_id)
            except (ValueError, TypeError):
                continue
            if ana_id in seen_ana:
                continue
            seen_ana.add(ana_id)
            failed_ana_ids.append(ana_id)
            audit_samples[ana_id] = {
                "op": a.op,
                "error_excerpt": err[:200],
                "audit_at": a.created_at.isoformat() if a.created_at else None,
            }

        # 2) 已有非空 pr_url 的 analysis_id 集合（视作 PR 已创建过，跳过）
        with_pr_rows = (await session.execute(
            select(CrashPullRequest.analysis_id).where(CrashPullRequest.pr_url != "")
        )).all()
        analyses_with_pr = {row[0] for row in with_pr_rows}

        # 3) 拉对应 analysis 行做最终过滤
        if not failed_ana_ids:
            return {
                "scanned": 0,
                "candidates": [],
                "summary": {"total": 0, "succeeded": 0, "failed": 0},
                "lookback_days": req.lookback_days,
                "diagnose": {
                    "audit_rows_in_window": len(audit_rows),
                    "reason": "no failed pr_draft audit rows; gh auth fix may not be the issue, OR audit_log empty",
                },
            }

        ana_rows = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.id.in_(failed_ana_ids))
        )).scalars().all()
        ana_by_id = {r.id: r for r in ana_rows}

        candidates: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        for ana_id in failed_ana_ids:
            ana = ana_by_id.get(ana_id)
            if ana is None:
                skipped.append({"analysis_id": ana_id, "reason": "analysis_not_found"})
                continue
            if req.issue_ids and ana.datadog_issue_id not in set(req.issue_ids):
                continue
            if ana_id in analyses_with_pr:
                skipped.append({
                    "analysis_id": ana_id,
                    "datadog_issue_id": ana.datadog_issue_id,
                    "reason": "pr_already_created",
                })
                continue
            if (ana.status or "") != "success":
                skipped.append({
                    "analysis_id": ana_id,
                    "datadog_issue_id": ana.datadog_issue_id,
                    "reason": f"analysis_status={ana.status}",
                })
                continue
            entry = {
                "analysis_id": ana_id,
                "datadog_issue_id": ana.datadog_issue_id,
                "feasibility": float(ana.feasibility_score or 0.0),
                "last_audit": audit_samples.get(ana_id, {}),
            }
            candidates.append(entry)
            if len(candidates) >= req.limit:
                break

    if req.diagnose_only:
        return {
            "scanned": len(candidates),
            "candidates": candidates,
            "skipped": skipped,
            "lookback_days": req.lookback_days,
            "summary": {"total": len(candidates), "succeeded": 0, "failed": 0},
            "diagnose": {
                "audit_rows_in_window": len(audit_rows),
                "distinct_failed_analyses": len(failed_ana_ids),
                "already_has_pr": len([s for s in skipped if s.get("reason") == "pr_already_created"]),
                "analysis_missing": len([s for s in skipped if s.get("reason") == "analysis_not_found"]),
            },
        }

    succeeded: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for cand in candidates:
        try:
            result = await draft_prs_multi(
                analysis_id=cand["analysis_id"], approver="retry", dry_run=req.dry_run,
            )
            prs = result.get("prs", [])
            out = {
                **cand,
                "ok": bool(result.get("ok")),
                "total": result.get("total", 0),
                "succeeded": result.get("succeeded", 0),
                "failed_count": result.get("failed", 0),
                "pr_urls": [p.get("pr_url") for p in prs if p.get("pr_url")],
                "errors": [p.get("error") for p in prs if not p.get("ok") and p.get("error")][:3],
            }
            (succeeded if out["ok"] else failed).append(out)
        except Exception as exc:
            logger.exception("retry-failed-prs failed for analysis_id=%d", cand["analysis_id"])
            failed.append({**cand, "ok": False, "error": str(exc)[:300]})

    return {
        "scanned": len(candidates),
        "lookback_days": req.lookback_days,
        "dry_run": req.dry_run,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "summary": {
            "total": len(candidates),
            "succeeded": len(succeeded),
            "failed": len(failed),
            "skipped": len(skipped),
        },
    }


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
    report_type: Optional[str] = Query(None, regex="^(morning|evening|hourly_alert|core_metric_alert)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    # 兼容旧前端：保留 limit 但忽略（用 page_size 代替）
    limit: Optional[int] = Query(None, ge=1, le=200),
) -> Dict[str, Any]:
    """列出最近 N 天的历史早晚报 + 实时告警（混合列表，按时间 desc）。

    返回 item 含 `kind: "daily" | "hourly_alert"`；前端按 kind 分发详情请求。
    分页：1-based page，page_size 默认 20。
    """
    from datetime import datetime, timedelta, date as _date
    from sqlalchemy import select, desc
    from app.db.database import get_session
    from app.crashguard.models import CrashDailyReport, CrashHourlyAlert, CrashMetricAlert
    import json as _json

    today = _date.today()
    since_date = today - timedelta(days=days)
    since_dt = datetime.utcnow() - timedelta(days=days)

    daily_rows: List[Any] = []
    hourly_rows: List[Any] = []
    metric_rows: List[Any] = []

    async with get_session() as session:
        # daily reports
        if report_type in (None, "morning", "evening"):
            stmt = select(CrashDailyReport).where(CrashDailyReport.report_date >= since_date)
            if report_type in ("morning", "evening"):
                stmt = stmt.where(CrashDailyReport.report_type == report_type)
            stmt = stmt.order_by(
                desc(CrashDailyReport.report_date),
                desc(CrashDailyReport.created_at),
            )
            daily_rows = (await session.execute(stmt)).scalars().all()
        # hourly alerts
        if report_type in (None, "hourly_alert"):
            stmt2 = (
                select(CrashHourlyAlert)
                .where(CrashHourlyAlert.hour_utc >= since_dt)
                .order_by(desc(CrashHourlyAlert.hour_utc))
            )
            hourly_rows = (await session.execute(stmt2)).scalars().all()
        # core metric alerts
        if report_type in (None, "core_metric_alert"):
            stmt3 = (
                select(CrashMetricAlert)
                .where(CrashMetricAlert.window_start >= since_dt)
                .order_by(desc(CrashMetricAlert.window_start))
            )
            metric_rows = (await session.execute(stmt3)).scalars().all()

    items: List[Dict[str, Any]] = []
    for r in daily_rows:
        try:
            payload = _json.loads(r.report_payload or "{}")
        except Exception:
            payload = {}
        # sort_key：用 created_at（每日早晚报）作为统一时间轴
        sort_key = r.created_at or datetime.combine(r.report_date or today, datetime.min.time())
        items.append({
            "kind": "daily",
            "id": r.id,
            "sort_key": sort_key.isoformat() if sort_key else None,
            "report_date": r.report_date.isoformat() if r.report_date else None,
            "report_type": r.report_type,  # "morning" | "evening"
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
    for a in hourly_rows:
        items.append({
            "kind": "hourly_alert",
            "id": a.id,
            "sort_key": a.hour_utc.isoformat() if a.hour_utc else None,
            "report_date": a.hour_utc.date().isoformat() if a.hour_utc else None,
            "report_type": "hourly_alert",
            "hour_utc": a.hour_utc.isoformat() if a.hour_utc else None,
            "top_n": 0,
            "new_count": int(a.new_count or 0),
            "regression_count": 0,
            "surge_count": int(a.surge_count or 0),
            "feishu_message_id": a.feishu_message_id or "",
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "summary": "",
            "attention_total": int(a.new_count or 0) + int(a.surge_count or 0),
        })
    for m in metric_rows:
        items.append({
            "kind": "core_metric_alert",
            "id": m.id,
            "sort_key": m.window_start.isoformat() if m.window_start else None,
            "report_date": m.window_start.date().isoformat() if m.window_start else None,
            "report_type": "core_metric_alert",
            "window_start": m.window_start.isoformat() if m.window_start else None,
            "platforms_alerted": m.platforms_alerted or "",
            "direction": m.direction or "",
            "top_n": 0, "new_count": 0, "regression_count": 0, "surge_count": 0,
            "feishu_message_id": m.feishu_message_id or "",
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "summary": "",
            "attention_total": 0,
        })

    # 按时间 desc 排序后分页
    items.sort(key=lambda x: x.get("sort_key") or "", reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    paged = items[start:end]

    return {
        "items": paged,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "days": days,
    }


@router.get("/alerts/hourly/{alert_id}")
async def get_hourly_alert_detail(alert_id: int) -> Dict[str, Any]:
    """单次 hourly 告警详情：渲染 markdown 视图 + 完整 payload。"""
    from datetime import timedelta
    from sqlalchemy import select
    from app.db.database import get_session
    from app.crashguard.models import CrashHourlyAlert
    from app.crashguard.config import get_crashguard_settings
    import json as _json

    async with get_session() as session:
        row = (await session.execute(
            select(CrashHourlyAlert).where(CrashHourlyAlert.id == alert_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="hourly alert not found")
        try:
            payload = _json.loads(row.alert_payload or "{}")
        except Exception:
            payload = {}

    s = get_crashguard_settings()
    base = s.frontend_base_url.rstrip("/")
    # 显示用新加坡时区（UTC+8）
    sg_label = "—"
    if row.hour_utc:
        sg_dt = row.hour_utc + timedelta(hours=8)
        sg_label = sg_dt.strftime("%Y-%m-%d %H:%M SGT")
    lines: List[str] = [
        f"# 🚨 实时告警 · {sg_label}",
        "",
        (f"> Σ 过去 3 小时 · 新增 **{row.new_count}** · 上涨 **{row.surge_count}**  ·  "
         f"阈值 +{payload.get('threshold_pct', 10):.0f}%（SHoW-3h 同周同 3h 块对比）"),
        "",
    ]
    new_items = payload.get("new") or []
    surge_items = payload.get("surge") or []
    if new_items:
        lines.append("## 🆕 新增崩溃（近 30 天首现）")
        lines.append("")
        for it in new_items:
            url = f"{base}/crashguard?issue={it.get('issue_id', '')}"
            title = it.get("title") or it.get("issue_id", "")
            platform = it.get("platform", "")
            events = it.get("events_h", 0)
            lines.append(f"- [{title}]({url}) · {platform} · **{events}** events/h")
        lines.append("")
    if surge_items:
        lines.append("## 📈 异常上涨")
        lines.append("")
        for it in surge_items:
            url = f"{base}/crashguard?issue={it.get('issue_id', '')}"
            title = it.get("title") or it.get("issue_id", "")
            platform = it.get("platform", "")
            events = it.get("events_h", 0)
            baseline = it.get("baseline", 0)
            growth = it.get("growth_pct", 0)
            src = "SHoW" if it.get("baseline_source") == "show" else "7d 均值"
            lines.append(
                f"- [{title}]({url}) · {platform} · **{events}** vs {baseline:.0f} ({src}) · **+{growth:.1f}%** ⬆️"
            )
        lines.append("")
    if not new_items and not surge_items:
        lines.append("_本小时无异常_")

    return {
        "id": row.id,
        "kind": "hourly_alert",
        "hour_utc": row.hour_utc.isoformat() if row.hour_utc else None,
        "new_count": int(row.new_count or 0),
        "surge_count": int(row.surge_count or 0),
        "feishu_message_id": row.feishu_message_id or "",
        "markdown": "\n".join(lines),
        "payload": payload,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/alerts/core-metric/{alert_id}")
async def get_core_metric_alert_detail(alert_id: int) -> Dict[str, Any]:
    """核心指标告警详情：渲染 markdown + 完整 payload。"""
    from datetime import timedelta
    from sqlalchemy import select
    from app.db.database import get_session
    from app.crashguard.models import CrashMetricAlert
    import json as _json

    async with get_session() as session:
        row = (await session.execute(
            select(CrashMetricAlert).where(CrashMetricAlert.id == alert_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="core metric alert not found")
        try:
            payload = _json.loads(row.alert_payload or "{}")
        except Exception:
            payload = {}

    sg_label = "—"
    if row.window_start:
        sg_dt = row.window_start + timedelta(hours=8)
        sg_label = sg_dt.strftime("%Y-%m-%d %H:%M SGT")
    threshold_pp = payload.get("threshold_pp", 0.3)
    items = payload.get("items") or []

    lines: List[str] = [
        f"# 📉 核心指标告警 · {sg_label}",
        "",
        (f"> 10 分钟窗口 · 触发 **{len(items)}** 平台 · "
         f"阈值 ±{threshold_pp:.2f} pp（vs 前 1h 加权均值）"),
        "",
        "## 平台明细",
        "",
    ]
    for it in items:
        arrow = "🔻" if it.get("direction") == "down" else "🔺"
        delta = it.get("delta_pp", 0.0)
        sign = "+" if delta >= 0 else ""
        lines.append(
            f"- **{(it.get('platform') or '').upper()}** · "
            f"crash-free **{it.get('crash_free_pct', 0):.2f}%** "
            f"(基线 {it.get('baseline_pct', 0):.2f}%) · "
            f"{arrow} **{sign}{delta:.2f} pp** · "
            f"会话 {it.get('total_sessions', 0)} / 崩溃 {it.get('crashed_sessions', 0)}"
        )
    if not items:
        lines.append("_无平台触发_")

    return {
        "id": row.id,
        "kind": "core_metric_alert",
        "window_start": row.window_start.isoformat() if row.window_start else None,
        "direction": row.direction or "",
        "platforms_alerted": row.platforms_alerted or "",
        "feishu_message_id": row.feishu_message_id or "",
        "markdown": "\n".join(lines),
        "payload": payload,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/reports/{report_id}")
async def get_report_detail(
    report_id: int,
    window_hours: int = Query(24, description="渲染时展示窗口：24/168/336/720 = 1d/7d/14d/30d"),
) -> Dict[str, Any]:
    """单份历史报告的完整 markdown + payload

    window_hours: 仅影响每 issue 的 events 数字（跨 N 天 CrashSnapshot sum）；
    SHoW-24h 基线对比逻辑不受影响（基线本身就是 24h 维度）。
    """
    from sqlalchemy import select
    from app.db.database import get_session
    from app.crashguard.services.daily_report import compose_report
    from app.crashguard.models import CrashDailyReport
    import json as _json

    if window_hours not in _ALLOWED_WINDOW_HOURS:
        window_hours = 24

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
            row.report_type, row.report_date, top_n=int(row.top_n or 5),
            view_window_hours=window_hours,
        )
    except Exception:
        text = "_报告内容已过期，无法重新生成（数据已轮转）_"

    return {
        "id": row.id,
        "report_date": row.report_date.isoformat() if row.report_date else None,
        "report_type": row.report_type,
        "window_hours": window_hours,
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
    from app.crashguard.services.pr_drafter import draft_prs_multi
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
            res = await draft_prs_multi(ana.id, approver="backfill")
            if res.get("ok"):
                info["status"] = "created"
                # multi 可能产 N 条 PR；这里把所有 PR url 拼起来
                pr_urls = [p.get("pr_url") for p in res.get("prs", []) if p.get("pr_url")]
                info["pr_url"] = " ; ".join(pr_urls)
                info["pr_count"] = res.get("succeeded", 0)
                triggered += 1
            else:
                info["status"] = "failed"
                first_err = next(
                    (p.get("error", "") for p in res.get("prs", []) if not p.get("ok")),
                    res.get("error", ""),
                )
                info["error"] = first_err
                failed.append({"analysis_id": str(ana.id), "error": first_err})
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


# === Job Heartbeats / Cron 任务观测 ===

# job_name → (cron 配置字段名, 显示名, 任务说明)
_JOB_META: List[Dict[str, str]] = [
    {"name": "core_metric", "cron_field": "core_metric_cron",
     "label": "核心指标告警",
     "desc": "10min crash-free sessions % vs 前 1h 加权均值",
     "enabled_field": "core_metric_enabled"},
    {"name": "hourly_alert", "cron_field": "hourly_alert_cron",
     "label": "小时级告警 (SHoW-3h)",
     "desc": "单 issue 突增/新增告警，每 3h 第 5min 触发",
     "enabled_field": "hourly_alert_enabled"},
    {"name": "analyze_tick", "cron_field": "analyze_cron",
     "label": "AI 分析 tick",
     "desc": "今日 attention 池小步分批跑 AI 分析", "enabled_field": ""},
    {"name": "pr_sync", "cron_field": "pr_sync_cron",
     "label": "PR 状态同步",
     "desc": "GitHub 现态回填到 crash_pull_requests", "enabled_field": ""},
    {"name": "pipeline", "cron_field": "pipeline_cron",
     "label": "数据 pipeline",
     "desc": "Datadog 全量拉取 + snapshot/issue upsert", "enabled_field": ""},
    {"name": "morning_daily", "cron_field": "morning_cron",
     "label": "日报 (07:00)",
     "desc": "昨日 24h 总览，SHoW-24h 基线",
     "enabled_field": "feishu_enabled"},
    {"name": "evening_daily", "cron_field": "evening_cron",
     "label": "速报 (17:00)",
     "desc": "日内 N 小时增量，SHoW-Nh 基线",
     "enabled_field": "feishu_enabled"},
    {"name": "warmup", "cron_field": "",
     "label": "启动 warmup",
     "desc": "后端重启后一次性补 pipeline + auto-analyze", "enabled_field": ""},
    {"name": "job_health_alert", "cron_field": "job_health_alert_cron",
     "label": "任务健康度兜底告警",
     "desc": "每 5min 扫心跳表，任一任务连续失败/超期 → 飞书告警",
     "enabled_field": "job_health_alert_enabled"},
    {"name": "top_crash_auto_pr", "cron_field": "top_crash_auto_pr_cron",
     "label": "Top crash 自动 PR",
     "desc": "Top N 专属低门槛 (0.5) + 节流，兜底覆盖 feasibility 0.5~0.7 区间的崩溃",
     "enabled_field": "top_crash_auto_pr_enabled"},
]


def _next_fire_time(cron_expr: str, now_dt) -> Optional[str]:
    """对极简 cron 算下一个触发时刻。仅支持 M H * * * 或 */N。"""
    from datetime import datetime, timedelta
    parts = (cron_expr or "").split()
    if len(parts) != 5:
        return None
    minute_f, hour_f, dom_f, month_f, dow_f = parts
    if dom_f != "*" or month_f != "*" or dow_f != "*":
        return None

    def _step(f: str) -> Optional[int]:
        if f.startswith("*/"):
            try:
                return int(f[2:])
            except ValueError:
                return None
        return None

    def _fixed(f: str) -> Optional[int]:
        if f == "*":
            return None
        try:
            return int(f)
        except ValueError:
            return None

    cur = (now_dt or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    # 暴力扫描 24h + 60min = 1440 步上限
    for _ in range(60 * 24 + 60):
        m_ok = (minute_f == "*"
                or (_step(minute_f) is not None and cur.minute % _step(minute_f) == 0)
                or (_fixed(minute_f) is not None and cur.minute == _fixed(minute_f)))
        h_ok = (hour_f == "*"
                or (_step(hour_f) is not None and cur.hour % _step(hour_f) == 0)
                or (_fixed(hour_f) is not None and cur.hour == _fixed(hour_f)))
        if m_ok and h_ok:
            return cur.isoformat()
        cur += timedelta(minutes=1)
    return None


@router.get("/jobs/status")
async def jobs_status() -> Dict[str, Any]:
    """所有定时任务的 cron + 上次心跳 + 下次预计时间 + 健康度判定。"""
    from datetime import datetime, timedelta
    from sqlalchemy import select, desc, func
    from app.db.database import get_session
    from app.crashguard.models import CrashJobHeartbeat
    import json as _json

    s = get_crashguard_settings()
    now = datetime.now()
    now_utc = datetime.utcnow()

    items: List[Dict[str, Any]] = []
    async with get_session() as session:
        for meta in _JOB_META:
            jn = meta["name"]
            cron_expr = getattr(s, meta["cron_field"], "") if meta["cron_field"] else ""
            enabled_flag = (
                bool(getattr(s, meta["enabled_field"], True))
                if meta["enabled_field"] else True
            )

            # 上次心跳（含 failed/skipped）
            last_row = (await session.execute(
                select(CrashJobHeartbeat)
                .where(CrashJobHeartbeat.job_name == jn)
                .order_by(desc(CrashJobHeartbeat.fired_at))
                .limit(1)
            )).scalars().first()
            # 上次成功心跳
            last_success_row = (await session.execute(
                select(CrashJobHeartbeat)
                .where(
                    CrashJobHeartbeat.job_name == jn,
                    CrashJobHeartbeat.status == "success",
                )
                .order_by(desc(CrashJobHeartbeat.fired_at))
                .limit(1)
            )).scalars().first()
            # 最近 50 次中失败数（健康度判定）
            recent = (await session.execute(
                select(CrashJobHeartbeat)
                .where(CrashJobHeartbeat.job_name == jn)
                .order_by(desc(CrashJobHeartbeat.fired_at))
                .limit(50)
            )).scalars().all()
            fail_count_50 = sum(1 for r in recent if r.status == "failed")
            degraded_count_50 = sum(1 for r in recent if r.status == "degraded")
            consecutive_failures = 0
            for r in recent:
                if r.status == "failed":
                    consecutive_failures += 1
                else:
                    break
            # 连续非 success（含 degraded）—— degraded 弱信号通道
            consecutive_unhealthy = 0
            for r in recent:
                if r.status in ("degraded", "failed"):
                    consecutive_unhealthy += 1
                else:
                    break

            # 是否超期（last_success_at 超过 2× 预期间隔）
            interval_minutes = _interval_minutes_from_cron(cron_expr)
            stale = False
            if interval_minutes and last_success_row is not None and last_success_row.fired_at:
                age_minutes = (now_utc - last_success_row.fired_at).total_seconds() / 60.0
                if age_minutes > 2 * interval_minutes:
                    stale = True
            elif interval_minutes and last_success_row is None and last_row is not None:
                stale = True  # 从来没成功过

            last_summary: Dict[str, Any] = {}
            if last_row and last_row.summary:
                try:
                    last_summary = _json.loads(last_row.summary or "{}")
                except Exception:
                    last_summary = {}

            items.append({
                "name": jn,
                "label": meta["label"],
                "desc": meta["desc"],
                "cron": cron_expr,
                "enabled": enabled_flag,
                "interval_minutes": interval_minutes,
                "next_fire_at": _next_fire_time(cron_expr, now) if cron_expr else None,
                "last_fired_at": last_row.fired_at.isoformat() if last_row and last_row.fired_at else None,
                "last_status": last_row.status if last_row else None,
                "last_duration_ms": int(last_row.duration_ms or 0) if last_row else 0,
                "last_error": (last_row.error or "")[:300] if last_row else "",
                "last_summary": last_summary,
                "last_success_at": (
                    last_success_row.fired_at.isoformat()
                    if last_success_row and last_success_row.fired_at else None
                ),
                "fail_count_in_recent_50": fail_count_50,
                "degraded_count_in_recent_50": degraded_count_50,
                "consecutive_failures": consecutive_failures,
                "consecutive_unhealthy": consecutive_unhealthy,
                "stale": stale,
                # 健康度综合判定（前端用色块）
                # 三态 heartbeat 升级后：consecutive_unhealthy ≥ 6（pr_sync 30min × 6 = 3h
                # 持续非 success）也升级为 failing；普通 degraded（不持续）仅黄色
                "health": (
                    "stale" if stale
                    else "failing" if consecutive_failures >= 3
                    else "failing" if consecutive_unhealthy >= 6
                    else "degraded" if (fail_count_50 + degraded_count_50) >= 10
                    else "ok"
                ),
            })

    return {
        "items": items,
        "server_time_local": now.isoformat(),
        "server_time_utc": now_utc.isoformat(),
    }


def _interval_minutes_from_cron(cron_expr: str) -> Optional[int]:
    """从极简 cron 推算"两次触发预期间隔分钟数"，用于 stale 判定。"""
    parts = (cron_expr or "").split()
    if len(parts) != 5:
        return None
    minute_f, hour_f, *_ = parts
    if minute_f.startswith("*/"):
        try:
            return max(1, int(minute_f[2:]))
        except ValueError:
            return None
    if hour_f.startswith("*/"):
        try:
            return max(1, int(hour_f[2:])) * 60
        except ValueError:
            return None
    if minute_f != "*" and hour_f != "*":
        # 固定时刻 (e.g. "0 7 * * *") → 一天一次
        return 24 * 60
    return None


@router.get("/jobs/{job_name}/heartbeats")
async def list_job_heartbeats(
    job_name: str,
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    """单任务最近 N 条心跳历史。"""
    from sqlalchemy import select, desc
    from app.db.database import get_session
    from app.crashguard.models import CrashJobHeartbeat
    import json as _json

    async with get_session() as session:
        rows = (await session.execute(
            select(CrashJobHeartbeat)
            .where(CrashJobHeartbeat.job_name == job_name)
            .order_by(desc(CrashJobHeartbeat.fired_at))
            .limit(limit)
        )).scalars().all()

    items: List[Dict[str, Any]] = []
    for r in rows:
        try:
            summary = _json.loads(r.summary or "{}")
        except Exception:
            summary = {}
        items.append({
            "id": r.id,
            "fired_at": r.fired_at.isoformat() if r.fired_at else None,
            "status": r.status,
            "duration_ms": int(r.duration_ms or 0),
            "error": (r.error or "")[:500],
            "summary": summary,
        })
    return {"job_name": job_name, "items": items, "total": len(items)}


@router.post("/jobs/{job_name}/run-now")
async def trigger_job_now(job_name: str) -> Dict[str, Any]:
    """手动触发指定 job 立即跑一次。

    - 复用 cron tick 同一份核心函数 → 行为与定时调度完全一致
    - 同样 wrap heartbeat（job_name 不变），summary 里加 triggered_by="manual"
    - 部分任务支持 force=True 跳过节流（hourly_alert / core_metric）
    - warmup / pipeline 共享 run_pipeline_and_auto_analyze
    """
    from app.crashguard.services.job_heartbeat import record_heartbeat

    try:
        async with record_heartbeat(job_name) as hb:
            res: Dict[str, Any]
            if job_name == "core_metric":
                from app.crashguard.services.core_metric_alerter import run_core_metric_tick
                res = await run_core_metric_tick(force=True)
            elif job_name == "hourly_alert":
                from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
                res = await run_hourly_alert_tick(force=True)
            elif job_name == "analyze_tick":
                from app.crashguard.workers.scheduler import _run_analyze_tick
                s = get_crashguard_settings()
                res = await _run_analyze_tick(
                    max_per_tick=int(getattr(s, "analyze_max_per_tick", 1) or 1)
                )
            elif job_name == "pr_sync":
                from app.crashguard.services.pr_sync import sync_all_open_prs
                res = await sync_all_open_prs()
            elif job_name in ("pipeline", "warmup"):
                from app.crashguard.workers.warmup import run_pipeline_and_auto_analyze
                res = await run_pipeline_and_auto_analyze(reason="manual")
            elif job_name == "morning_daily":
                from app.crashguard.services.daily_report import send_daily_report
                res = await send_daily_report("morning")
            elif job_name == "evening_daily":
                from app.crashguard.services.daily_report import send_daily_report
                res = await send_daily_report("evening")
            elif job_name == "job_health_alert":
                from app.crashguard.services.job_health_alerter import run_job_health_check
                res = await run_job_health_check()
            else:
                raise HTTPException(status_code=400, detail=f"unknown job: {job_name}")

            res = res if isinstance(res, dict) else {"raw": str(res)}
            res["triggered_by"] = "manual"
            hb.set_summary(res)
            hb.set_status_from_result(res)
            return {"ok": True, "job_name": job_name, "result": res}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("manual run-now failed for job=%s", job_name)
        raise HTTPException(status_code=500, detail=f"run-now failed: {exc}")


@router.get("/pr-diagnose")
async def pr_diagnose() -> Dict[str, Any]:
    """一站式 PR 创建失败排查接口（102 部署后 curl 这个就知道哪里卡住）。

    检查 5 大盲点：
      1. kill switch 状态（enabled / pr_enabled / feishu_enabled）
      2. 仓库路径配置 + 路径在容器内是否存在
      3. gh CLI 是否安装且认证
      4. 最近 20 条 pr_draft / auto_draft_pr audit log（成败 + 错误摘要）
      5. 最近 success 分析中"feasibility ≥ 阈值但没建 PR"的 issue 数量
    """
    import os
    import shutil
    import subprocess
    from datetime import datetime, timedelta
    from sqlalchemy import select, desc, func
    from app.db.database import get_session
    from app.crashguard.models import CrashAnalysis, CrashAuditLog, CrashPullRequest

    s = get_crashguard_settings()
    out: Dict[str, Any] = {}

    # 1. kill switches
    out["kill_switches"] = {
        "enabled": s.enabled,
        "pr_enabled": s.pr_enabled,
        "feishu_enabled": s.feishu_enabled,
        "scheduler_enabled": getattr(s, "scheduler_enabled", True),
    }
    out["feasibility_pr_threshold"] = float(getattr(s, "feasibility_pr_threshold", 0.7) or 0.7)

    # 2. 仓库路径
    repo_paths = {
        "flutter": getattr(s, "repo_path_flutter", "") or os.environ.get("CRASHGUARD_REPO_PATH_FLUTTER", ""),
        "android": getattr(s, "repo_path_android", "") or os.environ.get("CRASHGUARD_REPO_PATH_ANDROID", ""),
        "ios": getattr(s, "repo_path_ios", "") or os.environ.get("CRASHGUARD_REPO_PATH_IOS", ""),
    }
    out["repo_paths"] = {}
    for plat, p in repo_paths.items():
        info: Dict[str, Any] = {"configured": bool(p), "path": p}
        if p:
            info["exists"] = os.path.isdir(p)
            info["is_git_repo"] = os.path.isdir(os.path.join(p, ".git")) if info["exists"] else False
        out["repo_paths"][plat] = info

    # 3. gh CLI
    gh_path = shutil.which("gh")
    gh_info: Dict[str, Any] = {"available": gh_path is not None, "path": gh_path or ""}
    if gh_path:
        try:
            r = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True, text=True, timeout=10,
            )
            gh_info["auth_returncode"] = r.returncode
            # gh auth status 的输出在 stderr
            gh_info["auth_output"] = (r.stderr or r.stdout)[-500:]
        except Exception as exc:
            gh_info["auth_error"] = str(exc)[:200]
    out["gh_cli"] = gh_info

    # 4. 最近 audit logs
    since_24h = datetime.utcnow() - timedelta(days=7)
    async with get_session() as session:
        audit_rows = (await session.execute(
            select(CrashAuditLog).where(
                CrashAuditLog.op.in_(["pr_draft", "auto_draft_pr"]),
                CrashAuditLog.created_at >= since_24h,
            ).order_by(desc(CrashAuditLog.created_at)).limit(20)
        )).scalars().all()
        audit_list: List[Dict[str, Any]] = []
        # 错误聚合
        error_buckets: Dict[str, int] = {}
        success_count = fail_count = 0
        for a in audit_rows:
            err = (a.error or "").strip()
            if a.success:
                success_count += 1
            else:
                fail_count += 1
                bucket = err[:50] or "(empty)"
                error_buckets[bucket] = error_buckets.get(bucket, 0) + 1
            audit_list.append({
                "op": a.op,
                "target_id": a.target_id,
                "success": bool(a.success),
                "error": err[:200],
                "created_at": a.created_at.isoformat() if a.created_at else None,
            })

        # 5. success 分析 vs PR 行 gap
        threshold = float(getattr(s, "feasibility_pr_threshold", 0.7) or 0.7)
        eligible = (await session.execute(
            select(func.count(CrashAnalysis.id)).where(
                CrashAnalysis.status == "success",
                CrashAnalysis.followup_question == "",
                CrashAnalysis.feasibility_score >= threshold,
            )
        )).scalar() or 0
        with_pr = (await session.execute(
            select(func.count(func.distinct(CrashPullRequest.analysis_id)))
            .where(CrashPullRequest.pr_url != "")
        )).scalar() or 0

    out["audit_logs_last_7d"] = {
        "total": len(audit_rows),
        "success": success_count,
        "failed": fail_count,
        "error_buckets": error_buckets,
        "recent_sample": audit_list[:10],
    }
    out["analysis_vs_pr_gap"] = {
        "eligible_success_analyses": int(eligible),
        "analyses_with_pr_created": int(with_pr),
        "gap": max(0, int(eligible) - int(with_pr)),
        "hint": "gap > 0 即未建 PR 的合格分析；用 /api/crash/retry-failed-prs 重试",
    }

    # 综合判定
    blockers: List[str] = []
    if not s.enabled:
        blockers.append("crashguard kill switch (enabled=false)")
    if not s.pr_enabled:
        blockers.append("PR kill switch (pr_enabled=false)")
    if not any(v.get("exists") for v in out["repo_paths"].values()):
        blockers.append("no repo path configured / exists in container")
    if not gh_info["available"]:
        blockers.append("gh CLI not installed in container")
    elif gh_info.get("auth_returncode", 1) != 0:
        blockers.append("gh CLI not authenticated (gh auth login)")
    out["blockers"] = blockers
    out["next_steps"] = (
        "若 blockers 非空 → 先解决 blockers；"
        "blockers 为空但 audit_logs 仍有 fail → 看 error_buckets，对症"
    )
    return out
