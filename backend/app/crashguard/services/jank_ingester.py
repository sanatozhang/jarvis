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
    app_stack_module_offset: str = "",
    app_stack_frame: str = "",
    stack_top_module: str = "",
    stack_top_symbol: str = "",
) -> str:
    """算出同一处卡顿的聚合键（sha1 前16位）。

    - iOS 且 has_app_frame=True：platform + app_stack_module + app_stack_module_offset
      （module 内的相对偏移，不受 ASLR 影响——绝对地址 app_stack_pc 每次启动因
      module_base 随机化而变化，同一处代码会算出不同地址，不能参与聚合键计算）
    - Android 且 has_app_frame=True：platform + app_stack_frame（已是可读文本，
      如 "ai.plaud.android.payment.k.a"，天然稳定，不受符号化影响）
    - has_app_frame=False（任意平台，卡顿完全发生在系统框架内部）：
      platform + stack_top_module + stack_top_symbol（仅用于统计可见性分桶，
      这类卡顿不会进入符号化/AI分析，精度要求低）
    """
    plat = (platform or "").strip().lower()
    if has_app_frame and "ios" in plat and app_stack_module and app_stack_module_offset:
        raw = f"{plat}:{app_stack_module}:{app_stack_module_offset}"
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
    app_stack_module_offset = (attrs.get("app_stack_module_offset") or "").strip()
    app_stack_module_base = (attrs.get("app_stack_module_base") or "").strip()
    app_stack_frame = (attrs.get("app_stack_frame") or "").strip()
    stack_top_module = (attrs.get("stack_top_module") or "").strip()
    stack_top_symbol = (attrs.get("stack_top_symbol") or "").strip()
    stack_trace = attrs.get("stack_trace") or ""
    app_version = (attrs.get("version") or "").strip()
    # Datadog 卡顿看板按页面分组统计用的原生维度，生产环境实测 100% 有值。
    page = (attrs.get("page") or "").strip()

    # 完整多帧调用栈数组字段（pipe 分隔、等长，102 生产环境实测约 20 帧，系统框架
    # 帧也有非空 base）。原样字符串存起来，切分/校验交给 symbolication 层处理——
    # 这里只负责摄入，不做格式假设。
    stack_pcs = attrs.get("stack_pcs") or ""
    stack_modules = attrs.get("stack_modules") or ""
    stack_module_offsets = attrs.get("stack_module_offsets") or ""
    stack_module_bases = attrs.get("stack_module_bases") or ""

    agg_key = compute_jank_aggregation_key(
        platform=platform,
        has_app_frame=has_app_frame,
        app_stack_module=app_stack_module,
        app_stack_module_offset=app_stack_module_offset,
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
        "app_stack_module_offset": app_stack_module_offset,
        "app_stack_module_base": app_stack_module_base,
        "app_stack_frame": app_stack_frame,
        "stack_top_module": stack_top_module,
        "stack_top_symbol": stack_top_symbol,
        "stack_trace": stack_trace,
        "stack_pcs": stack_pcs,
        "stack_modules": stack_modules,
        "stack_module_offsets": stack_module_offsets,
        "stack_module_bases": stack_module_bases,
        "app_version": app_version,
        "frame_label": frame_label,
        "page": page,
    }


def _accumulate_page_count(row: Any, page: str) -> None:
    """把一条事件的 `page` 计入 `row.tags["page_counts"]`，重算 `row.top_page`。

    复用现有 `tags` JSON 列（不新建表）。空字符串 page 不计数——上游确认生产环境
    100% 有值，但摄入侧仍要容错（畸形/缺字段事件）。
    """
    from app.crashguard.services.analyzer import _format_top_dist

    try:
        tags = json.loads(row.tags) if row.tags else {}
        if not isinstance(tags, dict):
            tags = {}
    except (ValueError, TypeError, json.JSONDecodeError):
        tags = {}

    page_counts = tags.get("page_counts")
    if not isinstance(page_counts, dict):
        page_counts = {}

    if page:
        page_counts[page] = int(page_counts.get(page, 0) or 0) + 1

    tags["page_counts"] = page_counts
    row.tags = json.dumps(tags, ensure_ascii=False)

    total = sum(page_counts.values())
    if total > 0:
        ranked = sorted(page_counts.items(), key=lambda kv: kv[1], reverse=True)
        dist_items = [
            {"value": name, "count": count, "pct": round(count * 100.0 / total, 1)}
            for name, count in ranked
        ]
        row.top_page = _format_top_dist(dist_items)


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

        # 无论新建还是命中已有 issue，都要把这条事件的 page 计入分布——新建 issue 的
        # 第一条事件也该被计入 top_page，所以统一在 if/else 之后跑一遍，不拆两份。
        _accumulate_page_count(row, parsed.get("page") or "")

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


def _jank_frame_looks_symbolized(result: str, original_frame_text: str) -> bool:
    """判断 symbolicate_jank_frame() 的返回值是不是占位符（符号化失败/未命中）。

    占位符特征：等于原始输入文本，或形如 "{module} + {pc}"（含 " + 0x"）——
    两种失败态都在 symbolicate_jank_frame() 的落地路径里出现过。
    """
    if not result:
        return False
    if result == original_frame_text:
        return False
    if " + 0x" in result:
        return False
    return True


async def _symbolicate_new_jank_issue(issue_id: str, parsed: Dict[str, Any]) -> None:
    """新建的 fixable jank issue 立即符号化一次（单帧查询成本低，不走惰性 prewarmer）。

    两次调用：
      1. `symbolicate_jank_frame()`（已有）——单帧结果只用来判断标题是否可读，
         成功则回写 `row.title`，不再拿它覆盖 `representative_stack`。
      2. `symbolicate_jank_stack()`（新增，仅 iOS）——完整多帧堆栈符号化结果回写
         `row.representative_stack`，修复"详情页堆栈只显示一行"的 bug（之前用单帧
         结果整个覆盖掉摄入时存的完整堆栈）。
    两次调用命中同一份 (tag, asset) 磁盘缓存（`get_ios_dsyms_dir` 内部按 `.extracted`
    marker 判断，命中即返回），不会产生二次下载。Android 不受影响：
    `representative_stack` 保持摄入时存的原始完整 `stack_trace`。
    """
    from app.crashguard.models import CrashIssue
    from app.crashguard.services.symbolication import (
        symbolicate_jank_frame, symbolicate_jank_stack,
    )

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
        symbolized_frame = await symbolicate_jank_frame(
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

    full_stack: Optional[str] = None
    if "ios" in (parsed["platform"] or "").lower():
        try:
            full_stack = await symbolicate_jank_stack(
                platform=parsed["platform"],
                app_version=parsed["app_version"],
                stack_trace=parsed["stack_trace"],
                stack_modules=parsed.get("stack_modules", ""),
                stack_pcs=parsed.get("stack_pcs", ""),
                stack_module_bases=parsed.get("stack_module_bases", ""),
                symbol_profile=symbol_profile,
                github_repo=github_repo,
            )
        except Exception as exc:
            # 完整堆栈符号化失败不应该丢掉标题更新——原样保留摄入时存的完整
            # stack_trace，不覆盖 representative_stack。
            logger.warning("jank full-stack symbolication failed for %s: %s", issue_id, exc)
            full_stack = None

    title_updated = _jank_frame_looks_symbolized(symbolized_frame, parsed.get("app_stack_frame", ""))

    async with get_session() as session:
        row = (await session.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
        )).scalar_one_or_none()
        if row is None:
            return
        if full_stack:
            row.representative_stack = full_stack[:32000]
        if title_updated:
            row.title = f"Jank @ {symbolized_frame}"[:512]
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
