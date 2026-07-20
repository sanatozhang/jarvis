"""
卡顿(jank_watchdog_block) 摄入器（2026-07-20）。

背景：Datadog 卡顿看板上的 `jank_watchdog_block` 是纯 Logs 事件（App 主线程阻塞
>200ms 时客户端自己打的日志），完全不经过 Error Tracking，Datadog 不做任何 issue
分组/去重。本模块把它接成 crash_issues 里的新 kind='jank'，具备和崩溃一样的生命
周期（长期挂号、按天 CrashSnapshot 累加、first_seen_at/last_seen_at）。

聚合键设计：Datadog 自带的 stack_signature 字段太粗（只是顶层框架名，会把大量不
相关的卡顿点合并到同一个桶），改用符号化前的原始 app 帧地址/文本做聚合——同一个
地址/偏移/文本一定属于同一处卡顿，不用等符号化完成就能正确分桶。

入口：ingest_jank_logs() → {"scanned", "new_issues", "updated_issues"}
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select

from app.crashguard.config import get_crashguard_settings
from app.crashguard.services.datadog_client import DatadogClient
from app.db.database import get_session

logger = logging.getLogger("crashguard.jank_ingester")

_JANK_LOG_QUERY = "@category:performance jank_watchdog_block"
_DEFAULT_LOOKBACK_HOURS = 4  # 首次运行（无历史 cursor）时的回看窗口，与 pipeline 4h tick 对齐
_MAX_PAGES_PER_TICK = 20     # 分页安全上限（20*100=2000 条/tick，远高于实测 ~50-100/4h 量级）
_CURSOR_OP = "jank_ingest_cursor"


def compute_jank_aggregation_key(
    *,
    platform: str,
    has_app_frame: bool,
    app_stack_module: str = "",
    app_stack_pc: str = "",
    app_stack_frame: str = "",
    stack_top_module: str = "",
    stack_top_symbol: str = "",
) -> str:
    """算出同一处卡顿的聚合键（sha1 前16位）。

    - iOS 且 has_app_frame=True：platform + app_stack_module + app_stack_pc
      （符号化前的原始地址，同一地址必属于同一处卡顿，不用等符号化完成再分桶）
    - Android 且 has_app_frame=True：platform + app_stack_frame（已是可读文本，
      如 "ai.plaud.android.payment.k.a"，天然稳定，不受符号化影响）
    - has_app_frame=False（任意平台，卡顿完全发生在系统框架内部）：
      platform + stack_top_module + stack_top_symbol（仅用于统计可见性分桶，
      这类卡顿不会进入符号化/AI分析，精度要求低）
    """
    plat = (platform or "").strip().lower()
    if has_app_frame and "ios" in plat and app_stack_module and app_stack_pc:
        raw = f"{plat}:{app_stack_module}:{app_stack_pc}"
    elif has_app_frame and app_stack_frame:
        raw = f"{plat}:{app_stack_frame}"
    else:
        raw = f"{plat}:{stack_top_module}:{stack_top_symbol}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _parse_jank_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 Datadog Logs Search API 返回的一条原始 log event 解析成内部结构。

    字段缺失/畸形时返回 None（跳过该条，不中断整个摄入循环）。
    """
    attrs = ((event or {}).get("attributes") or {}).get("attributes") or {}
    os_info = attrs.get("os") or {}
    if not isinstance(os_info, dict):
        return None
    platform = (os_info.get("name") or "").strip().lower()
    if not platform:
        return None

    has_app_frame = bool(attrs.get("has_app_frame"))
    app_stack_module = (attrs.get("app_stack_module") or "").strip()
    app_stack_pc = (attrs.get("app_stack_pc") or "").strip()
    app_stack_module_base = (attrs.get("app_stack_module_base") or "").strip()
    app_stack_frame = (attrs.get("app_stack_frame") or "").strip()
    stack_top_module = (attrs.get("stack_top_module") or "").strip()
    stack_top_symbol = (attrs.get("stack_top_symbol") or "").strip()
    stack_trace = attrs.get("stack_trace") or ""
    app_version = (attrs.get("version") or "").strip()

    agg_key = compute_jank_aggregation_key(
        platform=platform,
        has_app_frame=has_app_frame,
        app_stack_module=app_stack_module,
        app_stack_pc=app_stack_pc,
        app_stack_frame=app_stack_frame,
        stack_top_module=stack_top_module,
        stack_top_symbol=stack_top_symbol,
    )

    if has_app_frame and "ios" in platform and app_stack_module:
        frame_label = app_stack_module
    elif has_app_frame and app_stack_frame:
        frame_label = app_stack_frame
    elif stack_top_module:
        frame_label = f"{stack_top_module}::{stack_top_symbol}" if stack_top_symbol else stack_top_module
    else:
        frame_label = "?"

    return {
        "issue_id": f"jank:{agg_key}",
        "platform": platform,
        "has_app_frame": has_app_frame,
        "app_stack_module": app_stack_module,
        "app_stack_pc": app_stack_pc,
        "app_stack_module_base": app_stack_module_base,
        "app_stack_frame": app_stack_frame,
        "stack_top_module": stack_top_module,
        "stack_top_symbol": stack_top_symbol,
        "stack_trace": stack_trace,
        "app_version": app_version,
        "frame_label": frame_label,
    }


async def _upsert_jank_event(parsed: Dict[str, Any], today) -> bool:
    """upsert CrashIssue（kind='jank'）+ CrashSnapshot（按天累加）。

    返回 True 表示本次新建了一个 issue（触发同步符号化），False 表示命中已有 issue。

    注意：CrashIssue 上没有 events_count 字段（累计数是 total_events）；真正驱动
    daily_report.py attention pool 准入判断的是按天的 CrashSnapshot.events_count，
    所以这里两张表都要维护，否则卡顿 issue 摄入了也永远进不了自动分析池。
    """
    from app.crashguard.models import CrashIssue, CrashSnapshot

    issue_id = parsed["issue_id"]
    now = datetime.utcnow()

    async with get_session() as session:
        row = (await session.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
        )).scalar_one_or_none()

        is_new = row is None
        if is_new:
            row = CrashIssue(
                datadog_issue_id=issue_id,
                title=f"Jank @ {parsed['frame_label']}",
                platform=parsed["platform"],
                kind="jank",
                fatality="jank",
                fixable=parsed["has_app_frame"],
                first_seen_at=now,
                first_seen_version=parsed["app_version"],
                last_seen_at=now,
                last_seen_version=parsed["app_version"],
                total_events=1,
                representative_stack=(parsed["stack_trace"] or "")[:32000],
            )
            session.add(row)
        else:
            row.last_seen_at = now
            row.last_seen_version = parsed["app_version"] or row.last_seen_version
            row.total_events = int(row.total_events or 0) + 1

        snap = (await session.execute(
            select(CrashSnapshot).where(
                CrashSnapshot.datadog_issue_id == issue_id,
                CrashSnapshot.snapshot_date == today,
            )
        )).scalar_one_or_none()
        if snap is None:
            session.add(CrashSnapshot(
                datadog_issue_id=issue_id, snapshot_date=today,
                app_version=parsed["app_version"], events_count=1,
            ))
        else:
            snap.events_count = int(snap.events_count or 0) + 1

        await session.commit()

    if is_new and parsed["has_app_frame"]:
        await _symbolicate_new_jank_issue(issue_id, parsed)

    return is_new


async def _symbolicate_new_jank_issue(issue_id: str, parsed: Dict[str, Any]) -> None:
    """新建的 fixable jank issue 立即符号化一次（单帧查询成本低，不走惰性 prewarmer）。"""
    from app.crashguard.models import CrashIssue
    from app.crashguard.services.symbolication import symbolicate_jank_frame

    symbol_profile = ""
    github_repo = ""
    try:
        from app.config import get_repo_routing
        from app.services import repo_router
        res = repo_router.resolve(parsed["platform"], parsed["app_version"], get_repo_routing())
        if res:
            symbol_profile = res.symbol_profile or ""
            github_repo = res.github_repo or ""
    except Exception as exc:
        logger.debug("jank repo_router.resolve failed for %s: %s", issue_id, exc)

    try:
        symbolized = await symbolicate_jank_frame(
            platform=parsed["platform"],
            app_version=parsed["app_version"],
            module=parsed["app_stack_module"],
            frame_text=parsed["app_stack_frame"],
            pc=parsed["app_stack_pc"],
            module_base=parsed["app_stack_module_base"],
            symbol_profile=symbol_profile,
            github_repo=github_repo,
        )
    except Exception as exc:
        logger.warning("jank symbolication failed for %s: %s", issue_id, exc)
        await _record_jank_prewarm_result(issue_id, error=str(exc))
        return

    async with get_session() as session:
        row = (await session.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
        )).scalar_one_or_none()
        if row is None:
            return
        row.representative_stack = symbolized[:32000]
        row.prewarm_attempts = int(row.prewarm_attempts or 0) + 1
        row.prewarm_last_at = datetime.utcnow()
        row.prewarm_last_error = ""
        await session.commit()


async def _record_jank_prewarm_result(issue_id: str, error: str) -> None:
    from app.crashguard.models import CrashIssue

    async with get_session() as session:
        row = (await session.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
        )).scalar_one_or_none()
        if row is None:
            return
        row.prewarm_attempts = int(row.prewarm_attempts or 0) + 1
        row.prewarm_last_error = (error or "")[:500]
        row.prewarm_last_at = datetime.utcnow()
        await session.commit()


async def _load_cursor_ms() -> Optional[int]:
    """读取上次成功摄入的 to_ms（持久化在 CrashAuditLog，避免依赖固定时间窗口漏抓/重抓）。"""
    from app.crashguard.models import CrashAuditLog

    async with get_session() as session:
        row = (await session.execute(
            select(CrashAuditLog)
            .where(CrashAuditLog.op == _CURSOR_OP, CrashAuditLog.success == True)  # noqa: E712
            .order_by(CrashAuditLog.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()
    if row is None:
        return None
    try:
        detail = json.loads(row.detail or "{}")
        return int(detail.get("to_ms"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


async def _save_cursor_ms(to_ms: int) -> None:
    from app.crashguard.models import CrashAuditLog

    async with get_session() as session:
        session.add(CrashAuditLog(
            op=_CURSOR_OP, target_id="", success=True,
            detail=json.dumps({"to_ms": to_ms}),
        ))
        await session.commit()


async def ingest_jank_logs(now: Optional[datetime] = None) -> Dict[str, Any]:
    """拉取 jank_watchdog_block 日志，按聚合键 upsert crash_issues/crash_snapshots。

    Args:
        now: 便于测试注入固定时间；生产调用不传，默认 datetime.utcnow()

    Returns:
        {"scanned": int, "new_issues": int, "updated_issues": int}
    """
    s = get_crashguard_settings()
    if not s.datadog_api_key:
        logger.info("jank ingester skipped: datadog_api_key 未配置")
        return {"scanned": 0, "new_issues": 0, "updated_issues": 0}

    now = now or datetime.utcnow()
    to_ms = int(now.timestamp() * 1000)
    cursor_ms = await _load_cursor_ms()
    from_ms = cursor_ms if cursor_ms is not None else (to_ms - _DEFAULT_LOOKBACK_HOURS * 3600 * 1000)

    client = DatadogClient(
        api_key=s.datadog_api_key, app_key=s.datadog_app_key,
        site=s.datadog_site, service_filter=s.datadog_service_filter,
    )

    today = now.date()
    scanned = 0
    new_issues = 0
    updated_issues = 0
    cursor: Optional[str] = None

    for _ in range(_MAX_PAGES_PER_TICK):
        page = await client.search_logs_page(
            query=_JANK_LOG_QUERY, from_ms=from_ms, to_ms=to_ms, cursor=cursor, limit=100,
        )
        events = page.get("data") or []
        for event in events:
            scanned += 1
            parsed = _parse_jank_event(event)
            if parsed is None:
                continue
            created = await _upsert_jank_event(parsed, today)
            if created:
                new_issues += 1
            else:
                updated_issues += 1

        cursor = page.get("next_cursor")
        if not cursor or not events:
            break

    await _save_cursor_ms(to_ms)
    logger.info(
        "jank ingester done: scanned=%d new_issues=%d updated_issues=%d",
        scanned, new_issues, updated_issues,
    )
    return {"scanned": scanned, "new_issues": new_issues, "updated_issues": updated_issues}
