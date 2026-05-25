"""
Crashguard 启动预热 + 周期 pipeline 触发。

用途（闭环抓手）：
- 之前的设计把"自动 AI 分析"嵌套在 send_daily_report 里，必须等 07:00/17:00 cron 才会触发；
  服务重启后到下次 cron 之间是空跑窗口。
- 这里独立出一条"拉数 → 选 Top → 串行 auto-analyze"流水线，启动后异步跑一次（warmup），
  并按 pipeline_cron 周期复跑（默认每 4 小时），与早晚报解耦。

设计取舍：
- 启动延后 60s 再跑，避开应用刚起的初始化抖动；
- fire-and-forget，不阻塞 lifespan startup；
- _auto_analyze_attention 自带去重（success/running 跳过），与早晚报重复触发不会重跑。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

from sqlalchemy import select

logger = logging.getLogger("crashguard.warmup")

# 启动 5s 后跑 warmup（之前 60s 太长，重启 → 用户打开 → 空白窗口 1 分钟）
_WARMUP_DELAY_SEC = 5
_PIPELINE_TICK_SEC = 60
_pipeline_last_fired: str = ""
_pr_reviewer_last_fired: str = ""
_pr_pending_review_last_fired: str = ""


async def _resolve_latest_release(settings) -> tuple[str, List[str]]:
    """解析"线上最新版本" + 最近 N 个版本。

    口径：以 Flutter 平台为主（双端共享代码），其它平台由 API 单独按 platform 查。
    优先级：config override > 数据派生 > 字典序兜底。
    """
    from app.crashguard.models import CrashIssue
    from app.crashguard.services.version_util import (
        collect_recent_versions,
        resolve_effective_latest_release,
    )
    from app.db.database import get_session

    async with get_session() as session:
        latest = await resolve_effective_latest_release(
            session=session,
            platform="flutter",
            override=settings.current_release_flutter,
            min_events=300,
        )
        if not latest:
            return "", []
        all_vers = (await session.execute(
            select(CrashIssue.last_seen_version).where(CrashIssue.platform == "flutter")
        )).scalars().all()
        recent = collect_recent_versions(all_vers, latest=latest, n=3)
    return latest, recent


async def _collect_attention_ids(today: date) -> List[str]:
    """从今日 snapshot 选关注集——优先级合并并截断到 analyze_top_n（默认 20）。

    优先级（保留顺序，去重）：
      1. 新增崩溃 is_new_in_version（新版引入的，无论严重，强制 P0 入选）
      2. fatal + non_fatal 合并按 events DESC 全局排序（公平竞争名额）

    旧策略问题：fatal 串行优先填满 20 名额，导致 IOS AppHang 等低流量 fatal
    （ev=2~30）抢走所有名额，FLUTTER non_fatal 18k events 永远进不来。
    现策略：fatal 与 non_fatal 在 events 维度上公平竞争，真正的 Top events 进池。

    最终硬截断到 settings.analyze_top_n（默认 20），保证体验链路可控。
    """
    from app.crashguard.config import get_crashguard_settings
    from app.crashguard.models import CrashSnapshot
    from app.crashguard.services.ranker import pick_top_n
    from app.db.database import get_session

    s = get_crashguard_settings()
    cap = max(1, int(getattr(s, "analyze_top_n", 20) or 20))

    ordered: List[str] = []
    seen: set = set()

    def _add(iid: str) -> bool:
        if iid and iid not in seen:
            seen.add(iid)
            ordered.append(iid)
        return len(ordered) >= cap

    # 只分析能自动修复的平台（BROWSER/JS 错误无对应 mobile repo，分析后无法生成 PR，
    # 浪费 AI token 并挤占 Top N 名额）—— 由 pick_top_n 在源头按 include_platforms 过滤，
    # 避免"先拿 Top 20 再后置过滤"导致 BROWSER 挤占名额、FLUTTER 高流量 issue 进不了池。
    from app.crashguard.models import CrashIssue as _CrashIssue
    s_cfg = get_crashguard_settings()
    fixable_platforms = tuple(
        p.lower() for p in
        getattr(s_cfg, "auto_pr_fixable_platforms", ["android", "ios", "flutter"])
    )
    # 排序键：events DESC —— Plaud RUM users_affected 字段全 0（见 models.py:71），
    # 默认 score 排序会让所有 issue score=0，回退到不确定的 DB 顺序。改用 events 直接见效。
    _ORDER_BY = "events"

    async with get_session() as session:
        # ① P0 新增崩溃强制入选（业务对齐：新版引入的 crash 不能漏）
        new_rows = (await session.execute(
            select(CrashSnapshot.datadog_issue_id)
            .join(_CrashIssue, _CrashIssue.datadog_issue_id == CrashSnapshot.datadog_issue_id)
            .where(
                CrashSnapshot.snapshot_date == today,
                CrashSnapshot.is_new_in_version == True,  # noqa: E712
                _CrashIssue.platform.in_(list(fixable_platforms)
                                          + [p.upper() for p in fixable_platforms]),
            )
            .order_by(CrashSnapshot.events_count.desc())
        )).scalars().all()
        for iid in new_rows:
            if _add(iid or ""):
                return ordered

        # ② fatal + non_fatal 合并按 events DESC 全局排序 —— fatality="" 不过滤
        top_all = await pick_top_n(
            session, today=today, n=cap, kinds=(), fatality="", dedup_days=0,
            include_platforms=fixable_platforms, order_by=_ORDER_BY,
        )
        for item in top_all:
            iid = item.get("datadog_issue_id") or ""
            if _add(iid):
                return ordered

    return ordered


async def _backfill_attention_auto_pr(issue_ids: List[str]) -> Dict[str, Any]:
    """给已完成 root 分析但尚未建 PR 的关注 issue 补触发 auto PR。

    典型场景：某 issue 在自动 PR hook 上线前已经有 success 分析；后续 pipeline 拉数时
    _auto_analyze_attention 会跳过它，导致永远没有机会进 _maybe_auto_draft_pr。
    """
    if not issue_ids:
        return {"scanned": 0, "attempted": 0, "created": 0, "skipped": 0, "failed": []}

    from app.crashguard.config import get_crashguard_settings
    from app.crashguard.models import CrashAnalysis, CrashIssue, CrashPullRequest
    from app.crashguard.services.audit import write_audit
    from app.crashguard.services.pr_drafter import draft_prs_multi
    from app.db.database import get_session

    s = get_crashguard_settings()
    if not s.pr_enabled:
        return {"scanned": 0, "attempted": 0, "created": 0, "skipped": 0, "failed": []}

    threshold = float(getattr(s, "feasibility_pr_threshold", 0.7) or 0.7)
    since = datetime.utcnow() - timedelta(days=int(getattr(s, "pr_dedup_days", 30) or 30))
    valid_platforms = {"android", "ios", "flutter"}

    async with get_session() as session:
        rows = (await session.execute(
            select(CrashAnalysis)
            .where(
                CrashAnalysis.datadog_issue_id.in_(issue_ids),
                CrashAnalysis.status == "success",
                CrashAnalysis.followup_question == "",
                CrashAnalysis.feasibility_score >= threshold,
            )
            .order_by(CrashAnalysis.id.desc())
        )).scalars().all()

        latest: List[CrashAnalysis] = []
        seen_issue_ids: set[str] = set()
        for row in rows:
            if row.datadog_issue_id in seen_issue_ids:
                continue
            latest.append(row)
            seen_issue_ids.add(row.datadog_issue_id)

        issue_rows = []
        if seen_issue_ids:
            issue_rows = (await session.execute(
                select(CrashIssue).where(CrashIssue.datadog_issue_id.in_(seen_issue_ids))
            )).scalars().all()
        issue_map = {i.datadog_issue_id: i for i in issue_rows}

        existing_issue_ids = set()
        if seen_issue_ids:
            existing_issue_ids = set(r[0] for r in (await session.execute(
                select(CrashPullRequest.datadog_issue_id).where(
                    CrashPullRequest.datadog_issue_id.in_(seen_issue_ids),
                    CrashPullRequest.created_at >= since,
                )
            )).all())

    attempted = 0
    created = 0
    skipped = 0
    failed: List[Dict[str, str]] = []

    for ana in latest:
        issue = issue_map.get(ana.datadog_issue_id)
        platform = (getattr(issue, "platform", "") or "").lower() if issue else ""
        if platform not in valid_platforms:
            skipped += 1
            continue
        if ana.datadog_issue_id in existing_issue_ids:
            skipped += 1
            continue

        attempted += 1
        try:
            result = await draft_prs_multi(ana.id, approver="auto")
            ok = bool(result.get("ok"))
            if ok:
                created += int(result.get("succeeded", 0) or 0)
            else:
                first_err = next(
                    (p.get("error", "") for p in result.get("prs", []) if not p.get("ok")),
                    result.get("error", ""),
                )
                failed.append({"analysis_id": str(ana.id), "error": first_err or "unknown"})
            await write_audit(
                op="auto_draft_pr",
                target_id=str(ana.id),
                success=ok,
                detail=str({
                    "source": "attention_backfill",
                    "succeeded": result.get("succeeded", 0),
                    "failed": result.get("failed", 0),
                    "errors": [p.get("error") for p in result.get("prs", []) if not p.get("ok")][:3],
                })[:500],
                error=None if ok else (failed[-1]["error"] if failed else "unknown"),
            )
        except Exception as exc:
            err = str(exc)[:300]
            failed.append({"analysis_id": str(ana.id), "error": err})
            try:
                await write_audit(
                    op="auto_draft_pr",
                    target_id=str(ana.id),
                    success=False,
                    detail="attention_backfill exception",
                    error=err,
                )
            except Exception:
                pass

    return {
        "scanned": len(latest),
        "attempted": attempted,
        "created": created,
        "skipped": skipped,
        "failed": failed,
    }


async def run_data_only(reason: str = "warmup") -> dict:
    """只跑数据阶段（拉 Datadog + classify + top_n），不做 AI 分析。

    设计目的：让 API 同步调用（用户点"拉取并分析"）能在几秒内返回，
    AI 分析挪到 fire-and-forget 后台跑。

    返回 {"issues_processed": int, "top_n_count": int, "today": "YYYY-MM-DD"}
         若被跳过（disabled / no_key），返回 {"skipped": True, "reason": ...}
    """
    from app.crashguard.config import get_crashguard_settings
    from app.crashguard.workers.pipeline import run_data_phase

    s = get_crashguard_settings()
    if not s.enabled:
        logger.info("[%s] crashguard disabled, skip", reason)
        return {"skipped": True, "reason": "disabled",
                "issues_processed": 0, "top_n_count": 0, "today": ""}
    if not s.datadog_api_key:
        logger.info("[%s] datadog_api_key empty, skip", reason)
        return {"skipped": True, "reason": "no_datadog_key",
                "issues_processed": 0, "top_n_count": 0, "today": ""}

    today = date.today()
    logger.info("[%s] starting pipeline for %s", reason, today)

    latest_release, recent_versions = await _resolve_latest_release(s)
    logger.info(
        "[%s] latest_release=%r recent=%s (override=%r threshold=%d)",
        reason, latest_release, recent_versions,
        s.current_release_flutter, 300,
    )

    pipeline_result = await run_data_phase(
        today=today, latest_release=latest_release, recent_versions=recent_versions,
    )
    logger.info(
        "[%s] pipeline done: issues=%d top_n=%d",
        reason, pipeline_result.get("issues_processed", 0),
        pipeline_result.get("top_n_count", 0),
    )
    return {
        "issues_processed": pipeline_result.get("issues_processed", 0),
        "top_n_count": pipeline_result.get("top_n_count", 0),
        "today": today.isoformat(),
    }


async def run_ai_analysis_phase(today: date, reason: str = "warmup") -> dict:
    """对今日 attention 列表跑 auto-analyze + auto-PR。可能耗时数十分钟。"""
    from app.crashguard.services.daily_report import _auto_analyze_attention

    attention_ids = await _collect_attention_ids(today)
    logger.info("[%s] attention candidates: %d", reason, len(attention_ids))

    auto_pr = {"scanned": 0, "attempted": 0, "created": 0, "skipped": 0, "failed": []}
    analyzed = 0
    if attention_ids:
        auto_pr = await _backfill_attention_auto_pr(attention_ids)
        analyzed = await _auto_analyze_attention(attention_ids)

    logger.info(
        "[%s] ai phase done: attention=%d analyzed=%d auto_pr=%s",
        reason, len(attention_ids), analyzed, auto_pr,
    )
    return {
        "attention_count": len(attention_ids),
        "analyzed": analyzed,
        "auto_pr": auto_pr,
    }


async def run_pipeline_and_auto_analyze(reason: str = "warmup") -> dict:
    """跑一次完整闭环：拉数 → 选 Top → 串行 auto-analyze（含 auto-PR）。

    用于启动 warmup / 定时 cron 等"不等结果"的后台场景。
    API 同步调用应使用 run_data_only + create_task(run_ai_analysis_phase)。

    返回 {"issues_processed": int, "attention_count": int, "analyzed": int, "auto_pr": {...}}
    """
    data = await run_data_only(reason=reason)
    if data.get("skipped"):
        return {
            "issues_processed": 0, "attention_count": 0, "analyzed": 0,
            "auto_pr": {"scanned": 0, "attempted": 0, "created": 0, "skipped": 0, "failed": []},
        }
    ai = await run_ai_analysis_phase(today=date.fromisoformat(data["today"]), reason=reason)
    logger.info(
        "[%s] cycle complete: issues=%d attention=%d analyzed=%d auto_pr=%s",
        reason, data["issues_processed"], ai["attention_count"],
        ai["analyzed"], ai["auto_pr"],
    )
    return {
        "issues_processed": data["issues_processed"],
        "attention_count": ai["attention_count"],
        "analyzed": ai["analyzed"],
        "auto_pr": ai["auto_pr"],
    }


async def warmup_on_startup() -> None:
    """启动后延后 N 秒跑一次 pipeline + auto-analyze。fire-and-forget。

    底层逻辑：warmup 跑的 run_pipeline_and_auto_analyze 与 pipeline cron 完全等价，
    所以成功后**镜像写一条 pipeline 心跳**——避免重启正好错过整点 cron 时
    pipeline last_success 永远停在旧时间、触发 stale 告警的死结。
    """
    try:
        await asyncio.sleep(_WARMUP_DELAY_SEC)
    except asyncio.CancelledError:
        return
    from app.crashguard.services.job_heartbeat import record_heartbeat
    res = None
    try:
        async with record_heartbeat("warmup") as hb:
            res = await run_pipeline_and_auto_analyze(reason="warmup")
            hb.set_summary(res)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("warmup pipeline failed (non-fatal)")
        return

    # warmup 成功 → 镜像写 pipeline 心跳（语义：warmup 等价于一次 pipeline tick）
    try:
        async with record_heartbeat("pipeline") as hb2:
            hb2.set_summary({**(res or {}), "via": "warmup_startup"})
    except Exception:
        logger.exception("pipeline heartbeat mirror failed (non-fatal)")


async def run_deep_analysis_auto_tick() -> dict:
    """自动触发 Phase 1 深度诊断：扫 attention pool 中无 PR、分析质量不够的 issue。

    触发条件（全部满足才触发）：
    - 平台在可修复白名单（android/ios/flutter）
    - 没有 active PR（open/draft/merged）
    - 没有正在运行的 Phase 1（pending/running）
    - 没有高质量的 Phase 2 分析（confidence=high AND feasibility≥0.7），即：
        (a) 完全没有 success 分析，或
        (b) 有 success 分析但 confidence<high 或 feasibility<0.7
    - Phase 1 dedup 窗口内未跑过（由 start_deep_analysis 内部控制）

    每 tick 最多触发 deep_analysis_auto_max_per_tick 个（默认 1，因为 Phase 1 需 30min）。
    Phase 1 结束后：
    - auto_proceed=True（单假设 confidence≥0.9）→ 自动触发 Phase 2 → PR
    - 否则 → 结果可在前端看到，等人工确认假设
    """
    from datetime import date as _date
    from app.crashguard.config import get_crashguard_settings
    from app.crashguard.models import CrashAnalysis, CrashIssue, CrashPullRequest
    from app.crashguard.services.deep_analyzer import start_deep_analysis
    from app.db.database import get_session
    from sqlalchemy import select, desc, or_

    s = get_crashguard_settings()
    if not getattr(s, "deep_analysis_auto_enabled", False):
        return {"skipped_reason": "kill_switch_off", "triggered": 0}
    if not getattr(s, "deep_analysis_enabled", True):
        return {"skipped_reason": "deep_analysis_disabled", "triggered": 0}

    max_per_tick = int(getattr(s, "deep_analysis_auto_max_per_tick", 1) or 1)
    fixable = frozenset(
        p.lower() for p in
        getattr(s, "auto_pr_fixable_platforms", ["android", "ios", "flutter"])
    )

    attention_ids = await _collect_attention_ids(_date.today())
    if not attention_ids:
        return {"triggered": 0, "scanned": 0, "skipped_reason": "no_attention_issues"}

    triggered = 0
    skipped: list[str] = []

    async with get_session() as session:
        # 预拉活跃 PR 的 issue id 集合
        active_pr_ids: set[str] = set(r[0] for r in (await session.execute(
            select(CrashPullRequest.datadog_issue_id).where(
                CrashPullRequest.datadog_issue_id.in_(attention_ids),
                CrashPullRequest.pr_status.in_(["open", "draft", "merged"]),
            )
        )).all())

        # 预拉正在运行的 Phase 1 诊断的 issue id 集合
        running_diag_ids: set[str] = set(r[0] for r in (await session.execute(
            select(CrashAnalysis.datadog_issue_id).where(
                CrashAnalysis.datadog_issue_id.in_(attention_ids),
                CrashAnalysis.phase == "diagnosis",
                CrashAnalysis.status.in_(["pending", "running"]),
            )
        )).all())

        # 预拉高质量 fix 分析（confidence=high AND feasibility>=0.7）
        high_quality_ids: set[str] = set(r[0] for r in (await session.execute(
            select(CrashAnalysis.datadog_issue_id).where(
                CrashAnalysis.datadog_issue_id.in_(attention_ids),
                CrashAnalysis.status == "success",
                CrashAnalysis.confidence == "high",
                CrashAnalysis.feasibility_score >= 0.7,
                or_(CrashAnalysis.phase == "fix", CrashAnalysis.phase.is_(None)),
            )
        )).all())

        # 预拉 issue 的 platform
        issue_platforms: dict[str, str] = dict((await session.execute(
            select(CrashIssue.datadog_issue_id, CrashIssue.platform).where(
                CrashIssue.datadog_issue_id.in_(attention_ids)
            )
        )).all())

    for issue_id in attention_ids:
        if triggered >= max_per_tick:
            break

        platform = (issue_platforms.get(issue_id) or "").lower()
        if platform not in fixable:
            skipped.append(f"{issue_id}:non_fixable_platform={platform}")
            continue
        if issue_id in active_pr_ids:
            skipped.append(f"{issue_id}:has_active_pr")
            continue
        if issue_id in running_diag_ids:
            skipped.append(f"{issue_id}:phase1_running")
            continue
        if issue_id in high_quality_ids:
            skipped.append(f"{issue_id}:already_high_quality")
            continue

        # 触发 Phase 1（start_deep_analysis 内部处理 dedup 去重）
        try:
            run_id = await start_deep_analysis(
                issue_id=issue_id,
                triggered_by="auto_deep_analyze",
                force=False,
            )
            triggered += 1
            logger.info(
                "[deep_analyze_auto] triggered Phase 1: issue=%s run_id=%s",
                issue_id, run_id,
            )
        except Exception as exc:
            logger.warning(
                "[deep_analyze_auto] failed to start Phase 1: issue=%s error=%s",
                issue_id, exc,
            )
            skipped.append(f"{issue_id}:start_failed:{exc}")

    return {
        "triggered": triggered,
        "scanned": len(attention_ids),
        "skipped": len(skipped),
        "skipped_detail": skipped[:5],
    }


def _cron_matches(expr: str, now: datetime) -> bool:
    """复用 scheduler.py 的极简 cron 解析（M H * * * 或 */N 形式）。"""
    parts = (expr or "").split()
    if len(parts) != 5:
        return False
    minute_f, hour_f, dom_f, month_f, dow_f = parts
    if dom_f != "*" or month_f != "*" or dow_f != "*":
        return False

    def field_match(field: str, value: int) -> bool:
        if field == "*":
            return True
        if field.startswith("*/"):
            try:
                step = int(field[2:])
                return step > 0 and value % step == 0
            except ValueError:
                return False
        try:
            return int(field) == value
        except ValueError:
            return False

    return field_match(minute_f, now.minute) and field_match(hour_f, now.hour)


async def pipeline_scheduler_loop() -> None:
    """周期 pipeline 触发（独立于早晚报）。每 60 秒 tick。"""
    from app.crashguard.config import get_crashguard_settings

    logger.info("crashguard pipeline_scheduler_loop started")
    global _pipeline_last_fired, _pr_reviewer_last_fired, _pr_pending_review_last_fired
    while True:
        try:
            s = get_crashguard_settings()
            if s.enabled and getattr(s, "scheduler_enabled", True):
                cron = getattr(s, "pipeline_cron", "") or ""
                if cron:
                    now = datetime.now()
                    tag = now.strftime("%Y-%m-%d %H:%M")
                    if _pipeline_last_fired != tag and _cron_matches(cron, now):
                        _pipeline_last_fired = tag
                        try:
                            from app.crashguard.services.job_heartbeat import record_heartbeat
                            async with record_heartbeat("pipeline") as hb:
                                res = await run_pipeline_and_auto_analyze(reason="cron")
                                hb.set_summary(res)
                        except Exception:
                            logger.exception("pipeline cron tick failed")

                # PR reviewer daily reminder（默认 09:30）
                rev_cron = getattr(s, "pr_reviewer_daily_cron", "") or ""
                if getattr(s, "pr_reviewer_enabled", False) and rev_cron:
                    now2 = datetime.now()
                    rev_tag = now2.strftime("%Y-%m-%d %H:%M")
                    if _pr_reviewer_last_fired != rev_tag and _cron_matches(rev_cron, now2):
                        _pr_reviewer_last_fired = rev_tag
                        try:
                            from app.crashguard.services.pr_reviewer import daily_reminder_sweep
                            from app.crashguard.services.job_heartbeat import record_heartbeat
                            async with record_heartbeat("pr_reviewer_daily") as hb:
                                res = await daily_reminder_sweep()
                                hb.set_summary(res)
                        except Exception:
                            logger.exception("pr_reviewer daily sweep failed")

                # PR pending-review 工作日 10:00 积压提醒
                ppr_cron = getattr(s, "pr_pending_review_cron", "") or ""
                if getattr(s, "pr_pending_review_enabled", False) and ppr_cron:
                    now3 = datetime.now()
                    ppr_tag = now3.strftime("%Y-%m-%d %H:%M")
                    if (_pr_pending_review_last_fired != ppr_tag
                            and _cron_matches(ppr_cron, now3)):
                        _pr_pending_review_last_fired = ppr_tag
                        try:
                            from app.crashguard.services.pr_pending_review_alert import (
                                run_pending_review_alert,
                            )
                            from app.crashguard.services.job_heartbeat import record_heartbeat
                            async with record_heartbeat("pr_pending_review") as hb:
                                res = await run_pending_review_alert()
                                hb.set_summary(res)
                        except Exception:
                            logger.exception("pr_pending_review_alert failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("pipeline scheduler tick error (continuing)")
        try:
            await asyncio.sleep(_PIPELINE_TICK_SEC)
        except asyncio.CancelledError:
            raise
