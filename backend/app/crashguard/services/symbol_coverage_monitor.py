"""
符号表健康度监控（2026-09-14，符号断供 19 天事故后新增）。

底层逻辑：19 天断供之所以没人发现，根因是上传脚本自报成功——`curl` 不带 `-f`，
4xx/5xx 也 exit 0——没有任何一方去验证"符号到底在不在"。`job_health_alerter` 只能
证明"任务跑过"，证明不了"任务跑出了正确结果"。本模块直接查 crashguard 自己的符号表
存储，不依赖任何任务的自报状态，是"监控结果，不监控过程"原则的落地。

两类检查：
1. 符号覆盖率：今日 (platform, version) 里事件量最高的几个版本，符号包应该存在却
   查不到 → 告警。用与真实符号化路径完全相同的 `_uploaded_package_dir` 精确匹配
   （见 github_symbols.py::get_android_mapping / get_ios_dsyms_dir 内部调用），
   避免自己另写一套匹配逻辑，跟真实路径产生格式漂移。
   兜底：全平台超过 N 天无任何新符号包入库 → 独立告警，直接对应本次事故模式。
2. 符号化成功率：今日 fixable issue 按 (platform, version) 聚合，
   `representative_stack` 仍是 raw/未符号化占比超阈值 → 告警（"符号包在但版本对
   不上，或符号化环节本身坏了"这类，覆盖率检查抓不到）。
"""
from __future__ import annotations

import logging
from datetime import date as _date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select

from app.crashguard.config import get_crashguard_settings
from app.crashguard.models import CrashIssue, CrashSnapshot, CrashSymbolPackage
from app.db.database import get_session

logger = logging.getLogger("crashguard.symbol_coverage_monitor")

# platform -> 该平台"应用自身符号"对应的 symbol_type，与
# github_symbols.py::get_android_mapping/get_ios_dsyms_dir 内部检查的类型一致。
# 未在此表中的 platform（如未知值）不检查，避免误报。
_REQUIRED_SYMBOL_TYPE = {
    "android": "proguard_mapping",
    "ios": "dsym",
    "flutter": "dart_symbols",
}

# 判断"仍未真正符号化"的标签集合——与 datadog_client._stack_quality_label /
# distribution_prewarmer._RAW_STACK_QUALITY_LABELS 保持同一口径。
_RAW_STACK_QUALITY_LABELS = {"raw", "aot_pointers_unsymbolicated", "empty"}

# 进程级告警节流：(platform, version) → 上次告警的 UTC 时间
_coverage_last_alerted_at: Dict[Tuple[str, str], datetime] = {}
_quality_last_alerted_at: Dict[Tuple[str, str], datetime] = {}
# 全平台"无新符号入库"兜底告警节流（独立戳，跟上面两个按 key 节流的不同）
_stale_upload_last_alerted_at: Optional[datetime] = None


async def _today_top_versions(
    session, today: _date, top_n: int, min_events: int,
) -> Dict[str, List[Dict[str, Any]]]:
    """按 platform 分组，取今日 events 最多的 top_n 个 (version, events)，events>=min_events。

    version 口径与真实符号化路径一致：`CrashIssue.last_seen_version`——
    `pipeline.py::_try_symbolicate_issue` 和 `datadog_client.py::get_issue_detail`
    都用这个字段喂 `symbolicate_stack`，不是 `top_app_version` 那种带百分比的展示字符串。
    """
    rows = (await session.execute(
        select(CrashSnapshot.events_count, CrashIssue.platform, CrashIssue.last_seen_version)
        .join(CrashIssue, CrashIssue.datadog_issue_id == CrashSnapshot.datadog_issue_id)
        .where(CrashSnapshot.snapshot_date == today)
    )).all()

    agg: Dict[Tuple[str, str], int] = {}
    for events_count, platform, version in rows:
        plat = (platform or "").strip().lower()
        ver = (version or "").strip()
        if not plat or not ver:
            continue
        key = (plat, ver)
        agg[key] = agg.get(key, 0) + int(events_count or 0)

    by_platform: Dict[str, List[Dict[str, Any]]] = {}
    for (plat, ver), total in agg.items():
        if total < min_events:
            continue
        by_platform.setdefault(plat, []).append({"version": ver, "events": total})

    for plat, versions in by_platform.items():
        versions.sort(key=lambda x: x["events"], reverse=True)
        by_platform[plat] = versions[:top_n]

    return by_platform


def _symbol_package_exists(platform: str, version: str) -> bool:
    from app.crashguard.services.github_symbols import _uploaded_package_dir

    symbol_type = _REQUIRED_SYMBOL_TYPE.get(platform)
    if not symbol_type:
        return True
    return _uploaded_package_dir(platform, symbol_type, version) is not None


async def check_symbol_coverage(session, today: _date, s: Any) -> List[Dict[str, Any]]:
    """返回今日高流量版本里符号包确实缺失的列表：[{platform, version, events}]。"""
    top_n = int(getattr(s, "symbol_coverage_top_n_versions", 3) or 3)
    min_events = int(getattr(s, "symbol_coverage_min_events", 100) or 100)
    by_platform = await _today_top_versions(session, today, top_n, min_events)

    missing: List[Dict[str, Any]] = []
    for plat, versions in by_platform.items():
        for v in versions:
            if not _symbol_package_exists(plat, v["version"]):
                missing.append({"platform": plat, "version": v["version"], "events": v["events"]})
    return missing


async def check_stale_symbol_upload(session, s: Any) -> Optional[Dict[str, Any]]:
    """全平台超过 N 天无任何新符号包入库 → 返回告警信息 dict；否则 None。

    直接对应本次事故模式：上传方一直"看起来在跑"，但实际从未成功写入过一条新记录。
    """
    stale_days = int(getattr(s, "symbol_coverage_stale_upload_days", 5) or 5)
    latest = (await session.execute(select(func.max(CrashSymbolPackage.created_at)))).scalar()
    if latest is None:
        return {"last_upload_at": None, "age_days": None, "stale_days": stale_days}
    age_days = (datetime.utcnow() - latest).total_seconds() / 86400.0
    if age_days <= stale_days:
        return None
    return {
        "last_upload_at": latest.isoformat(),
        "age_days": round(age_days, 1),
        "stale_days": stale_days,
    }


async def check_symbolication_quality(session, today: _date, s: Any) -> List[Dict[str, Any]]:
    """今日 fixable issue 按 (platform, version) 聚合，raw/未符号化占比超阈值 → 告警。

    抓手："符号包在，但版本对不上"或符号化环节本身坏了（如 GH_TOKEN 权限问题）——
    这类不会被 check_symbol_coverage 抓到，因为符号包确实存在，只是没生效。
    """
    from app.crashguard.services.datadog_client import DatadogClient

    min_issues = int(getattr(s, "symbolication_quality_min_issues", 5) or 5)
    raw_threshold = float(getattr(s, "symbolication_quality_raw_rate_threshold", 0.5) or 0.5)

    rows = (await session.execute(
        select(CrashIssue.platform, CrashIssue.last_seen_version, CrashIssue.representative_stack)
        .join(CrashSnapshot, CrashSnapshot.datadog_issue_id == CrashIssue.datadog_issue_id)
        .where(
            CrashSnapshot.snapshot_date == today,
            CrashIssue.fixable == True,  # noqa: E712
        )
    )).all()

    buckets: Dict[Tuple[str, str], Dict[str, int]] = {}
    for platform, version, stack in rows:
        plat = (platform or "").strip().lower()
        ver = (version or "").strip()
        if not plat or not ver:
            continue
        b = buckets.setdefault((plat, ver), {"total": 0, "raw": 0})
        b["total"] += 1
        if DatadogClient._stack_quality_label(stack or "") in _RAW_STACK_QUALITY_LABELS:
            b["raw"] += 1

    result: List[Dict[str, Any]] = []
    for (plat, ver), b in buckets.items():
        if b["total"] < min_issues:
            continue
        rate = b["raw"] / b["total"]
        if rate > raw_threshold:
            result.append({
                "platform": plat, "version": ver,
                "raw_count": b["raw"], "total": b["total"],
                "raw_rate": round(rate, 2),
            })
    return result


async def run_symbol_health_check() -> Dict[str, Any]:
    """单次扫描入口，由 scheduler 每日调用一次。返回 dict 供 heartbeat summary 使用。"""
    s = get_crashguard_settings()
    if not s.enabled or not s.feishu_enabled:
        return {"skipped": "kill_switch"}
    if not getattr(s, "symbol_health_enabled", True):
        return {"skipped": "symbol_health_disabled"}

    today = _date.today()
    async with get_session() as session:
        missing_coverage = await check_symbol_coverage(session, today, s)
        stale_upload = await check_stale_symbol_upload(session, s)
        bad_quality = await check_symbolication_quality(session, today, s)

    now = datetime.utcnow()
    cooldown = timedelta(hours=int(getattr(s, "symbol_coverage_alert_cooldown_hours", 24) or 24))

    fresh_missing: List[Dict[str, Any]] = []
    for it in missing_coverage:
        key = (it["platform"], it["version"])
        last = _coverage_last_alerted_at.get(key)
        if last is not None and (now - last) < cooldown:
            continue
        fresh_missing.append(it)

    fresh_quality: List[Dict[str, Any]] = []
    for it in bad_quality:
        key = (it["platform"], it["version"])
        last = _quality_last_alerted_at.get(key)
        if last is not None and (now - last) < cooldown:
            continue
        fresh_quality.append(it)

    global _stale_upload_last_alerted_at
    fresh_stale: Optional[Dict[str, Any]] = None
    if stale_upload is not None:
        if _stale_upload_last_alerted_at is None or (now - _stale_upload_last_alerted_at) >= cooldown:
            fresh_stale = stale_upload

    if not fresh_missing and not fresh_quality and not fresh_stale:
        return {
            "ok": True, "alerted": False,
            "checked_coverage": len(missing_coverage),
            "checked_quality_buckets": len(bad_quality),
            "stale_upload": stale_upload,
        }

    from app.crashguard.services.feishu_card import build_symbol_health_alert_card
    card = build_symbol_health_alert_card(
        missing_coverage=fresh_missing,
        bad_quality=fresh_quality,
        stale_upload=fresh_stale,
        frontend_base_url=s.frontend_base_url,
    )
    sent_ok = False
    try:
        from app.services.feishu_cli import send_interactive_card
        # 路由：alert_email > chat_id > target_email，跟 job_health_alerter 现有逻辑一致
        if s.feishu_alert_email:
            sent_ok = await send_interactive_card(email=s.feishu_alert_email, card=card)
        elif s.feishu_target_chat_id:
            sent_ok = await send_interactive_card(chat_id=s.feishu_target_chat_id, card=card)
        elif s.feishu_target_email:
            sent_ok = await send_interactive_card(email=s.feishu_target_email, card=card)
    except Exception:
        logger.exception("symbol_health_check: feishu send error")

    for it in fresh_missing:
        _coverage_last_alerted_at[(it["platform"], it["version"])] = now
    for it in fresh_quality:
        _quality_last_alerted_at[(it["platform"], it["version"])] = now
    if fresh_stale is not None:
        _stale_upload_last_alerted_at = now

    logger.info(
        "symbol_health_check fired: missing=%d quality=%d stale=%s sent=%s",
        len(fresh_missing), len(fresh_quality), bool(fresh_stale), sent_ok,
    )
    return {
        "ok": True, "alerted": True, "sent": sent_ok,
        "missing_coverage": fresh_missing,
        "bad_quality": fresh_quality,
        "stale_upload": fresh_stale,
    }
