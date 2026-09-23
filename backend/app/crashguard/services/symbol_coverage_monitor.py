"""
符号表健康度监控（2026-09-14 新增；2026-09-15 改为主动拉取，不再只提醒）。

底层逻辑：19 天断供之所以没人发现，根因是上传脚本自报成功——`curl` 不带 `-f`，
4xx/5xx 也 exit 0——没有任何一方去验证"符号到底在不在"。`job_health_alerter` 只能
证明"任务跑过"，证明不了"任务跑出了正确结果"。本模块直接查 crashguard 自己的符号表
存储，不依赖任何任务的自报状态，是"监控结果，不监控过程"原则的落地。

2026-09-15 用户反馈：发现覆盖率缺口后不该只是提醒人去手动补——应该跟当初手工
补 1143 符号表时做的事一样，自动去"查已上传 → 查不到再从 GitHub release 下载"，
只有主动拉取也失败（GitHub 上也没有，说明真的需要人上传）才告警。做法：
`check_symbol_coverage` 直接调用真实符号化路径同一套 `get_android_mapping` /
`get_ios_dsyms_dir` / `get_dart_symbols_dir`（Plan B 已上传优先，Plan C GitHub
下载兜底，下载后本地落盘缓存），而不是只查"是否已上传"这一半。拉取成功的版本
立即触发一次 `pipeline._try_symbolicate_issue` 重跑，今天该版本已经产生的
raw/未符号化 issue 立刻回填，不必等下一次自然分析。

两类检查：
1. 符号覆盖率：今日 (platform, version) 里事件量最高的几个版本，主动拉取符号包
   （已上传 → GitHub release 兜底），拉取后仍确实找不到才告警——这是"人真的需要
   手动上传"的信号，不是"程序偷懒没查"。
   兜底：全平台超过 N 天无任何新符号包入库 → 独立告警，直接对应本次事故模式
   （这条本身无法通过"拉取"解决——问题出在上传通道，不是某个具体版本缺包）。
2. 符号化成功率：今日 fixable issue 按 (platform, version) 聚合，
   `representative_stack` 仍是 raw/未符号化占比超阈值 → 告警（"符号包在但版本对
   不上，或符号化环节本身坏了"这类，不是简单缺包，主动拉取解决不了，仍保持
   纯观测告警）。在 check_symbol_coverage 的拉取 + 重符号化跑完之后才计算，
   避免把"马上就会被自动修复"的旧数据也报出来。
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


async def _try_fetch_symbol(platform: str, version: str) -> bool:
    """主动拉取该 (platform, version) 的符号包：先查已上传，查不到再从 GitHub release
    下载（成功后落盘缓存）。直接复用真实符号化路径同一套函数——保证"监控认为可用"
    和"符号化时实际能用到"永远是同一个结果，不会出现两套口径漂移。

    返回 True 表示符号包现在确实可用（已上传的，或刚从 GitHub 下载缓存的）。
    网络/下载失败按 False 处理，不抛异常——单个版本拉取失败不该打断整个 tick。
    """
    from app.config import get_repo_routing
    from app.services import repo_router
    from app.crashguard.services.github_symbols import (
        _ASSET_IOS_DSYM, _ASSET_IOS_DSYM_NATIVE,
        get_android_mapping, get_dart_symbols_dir, get_ios_dsyms_dir,
    )
    from app.crashguard.services.symbolication import _profile_strategy

    try:
        res = repo_router.resolve(platform, version, get_repo_routing())
        symbol_profile = res.symbol_profile if res else ""
        github_repo = res.github_repo if res else ""
        kwargs = {"repo": github_repo} if github_repo else {}

        if platform == "android":
            path = await get_android_mapping(version, **kwargs)
            return path is not None
        if platform == "ios":
            strategy = _profile_strategy(symbol_profile)
            asset = _ASSET_IOS_DSYM_NATIVE if strategy.get("use_app_dsym") else _ASSET_IOS_DSYM
            path = await get_ios_dsyms_dir(version, asset_name=asset, **kwargs)
            return path is not None
        if platform == "flutter":
            path = await get_dart_symbols_dir(version, **kwargs)
            return path is not None
    except Exception:
        logger.warning(
            "symbol_health: active fetch failed for platform=%s version=%s",
            platform, version, exc_info=True,
        )
        return False
    return True  # 未知 platform 不检查，避免误报（同旧版本行为）


async def _resymbolicate_bucket(session, today: _date, platform: str, version: str) -> int:
    """符号刚确认可用后，把今日该 (platform, version) 桶里仍是 raw 的 issue 立即重跑
    一次符号化——让修复马上生效，不必等下一次自然分析/手动重新分析触发。

    复用 `pipeline.py::_try_symbolicate_issue`（内部自行 resolve repo_router +
    回写 representative_stack），本函数只负责挑出"今天这个桶里还没修好"的 issue。
    """
    from app.crashguard.services.datadog_client import DatadogClient
    from app.crashguard.workers.pipeline import _try_symbolicate_issue

    rows = (await session.execute(
        select(CrashIssue.datadog_issue_id, CrashIssue.representative_stack)
        .join(CrashSnapshot, CrashSnapshot.datadog_issue_id == CrashIssue.datadog_issue_id)
        .where(
            CrashSnapshot.snapshot_date == today,
            CrashIssue.platform == platform,
            CrashIssue.last_seen_version == version,
        )
    )).all()

    resymbolized = 0
    for issue_id, stack in rows:
        if DatadogClient._stack_quality_label(stack or "") in _RAW_STACK_QUALITY_LABELS:
            await _try_symbolicate_issue(issue_id, platform)
            resymbolized += 1
    return resymbolized


async def check_symbol_coverage(
    session, today: _date, s: Any,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """今日高流量版本逐个主动拉取符号包。

    返回 (missing, resolved)：
    - missing：拉取（含 GitHub 兜底）后确实找不到，需要人工上传——告警用。
    - resolved：拉取后确认可用（本来就有 / 刚从 GitHub 下载），调用方应据此触发
      `_resymbolicate_bucket` 让今日已产生的 issue 立刻受益，不进告警。
    """
    top_n = int(getattr(s, "symbol_coverage_top_n_versions", 3) or 3)
    min_events = int(getattr(s, "symbol_coverage_min_events", 100) or 100)
    by_platform = await _today_top_versions(session, today, top_n, min_events)

    missing: List[Dict[str, Any]] = []
    resolved: List[Dict[str, Any]] = []
    for plat, versions in by_platform.items():
        for v in versions:
            item = {"platform": plat, "version": v["version"], "events": v["events"]}
            if await _try_fetch_symbol(plat, v["version"]):
                resolved.append(item)
            else:
                missing.append(item)
    return missing, resolved


async def check_stale_symbol_upload(session, s: Any) -> Optional[Dict[str, Any]]:
    """全平台超过 N 天无任何新符号包入库 → 返回告警信息 dict；否则 None。

    直接对应本次事故模式：上传方一直"看起来在跑"，但实际从未成功写入过一条新记录。
    这条本身无法靠"拉取"解决——问题在上传通道本身，不是某个具体版本缺包，仍保持
    纯告警，交给人去排查上传链路。
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
    这类不是简单缺包，主动拉取解决不了，仍保持纯观测告警。调用方应在
    `check_symbol_coverage` 的拉取 + `_resymbolicate_bucket` 跑完之后再调用本函数，
    避免把"马上就会被自动修复"的旧数据也报出来。
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
    """单次扫描入口，由 scheduler 每日调用一次（入队后台执行，见 scheduler.py——
    主动拉取 GitHub release 可能耗时数十秒到数分钟，不能内联占用 60s tick loop）。
    返回 dict 供 heartbeat summary 使用。
    """
    s = get_crashguard_settings()
    if not s.enabled or not s.feishu_enabled:
        return {"skipped": "kill_switch"}
    if not getattr(s, "symbol_health_enabled", True):
        return {"skipped": "symbol_health_disabled"}

    today = _date.today()

    async with get_session() as session:
        missing_coverage, resolved = await check_symbol_coverage(session, today, s)

    # 拉取成功的版本立即重符号化今日已产生的 issue，让修复马上生效
    resymbolized_total = 0
    async with get_session() as session:
        for it in resolved:
            resymbolized_total += await _resymbolicate_bucket(
                session, today, it["platform"], it["version"],
            )

    # 重符号化之后再算成功率，避免把"马上就会被自动修复"的旧数据也报出来
    async with get_session() as session:
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
            "checked_coverage": len(missing_coverage) + len(resolved),
            "auto_resolved": resolved,
            "resymbolized": resymbolized_total,
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
    # 路由收口到 notify.alert_target()（alert_email > 群 > target_email）
    from app.crashguard.services import notify
    from app.services.im.feishu_to_slack import compile_card

    sent_ok = await notify.send_alert(card, lambda: compile_card(card), s=s, what="symbol_health_alert")

    for it in fresh_missing:
        _coverage_last_alerted_at[(it["platform"], it["version"])] = now
    for it in fresh_quality:
        _quality_last_alerted_at[(it["platform"], it["version"])] = now
    if fresh_stale is not None:
        _stale_upload_last_alerted_at = now

    logger.info(
        "symbol_health_check fired: missing=%d resolved=%d resymbolized=%d quality=%d stale=%s sent=%s",
        len(fresh_missing), len(resolved), resymbolized_total, len(fresh_quality), bool(fresh_stale), sent_ok,
    )
    return {
        "ok": True, "alerted": True, "sent": sent_ok,
        "missing_coverage": fresh_missing,
        "auto_resolved": resolved,
        "resymbolized": resymbolized_total,
        "bad_quality": fresh_quality,
        "stale_upload": fresh_stale,
    }
