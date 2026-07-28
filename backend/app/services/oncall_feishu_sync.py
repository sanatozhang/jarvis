"""
Weekly oncall schedule sync FROM the team's Feishu Base ("本周值班（feature 同事兼顾发版）")
INTO Jarvis's internal oncall_week_assignments snapshot table.

飞书表是值班排班的权威来源（团队手工维护）；Jarvis 内部的 oncall_groups/
oncall_week_assignments 用于工单升级拉群、统计等场景。本模块每周一 08:00
(Asia/Shanghai) 把 Feishu 表里当周及未来周次的"值班人员（Feature）"+
"值班人员（Fundamentals）"合并写入 Jarvis 的排班快照，出现差异直接覆盖
(不经人工确认 — 2026-07-28 与用户确认过)。

字段结构(person 类型，取自实测 API 响应)：
  值班人员（Feature）: [{"email": "leon@plaud.ai", "name": "...", ...}, ...]
  值班人员（Fundamentals）: [{"email": "...", ...}, ...]
  发版责任人（Release Manager): 同上结构，代表"谁负责这周发版"，不算 oncall
    值班的一部分，本模块不读取这个字段。
  日期: 该值班周的周一 0 点时间戳(ms, Asia/Shanghai)。

历史脏数据：2026-01-19 之前的行是表格草创期的测试/候选池数据（同一字段可能
塞进 3-4 个人、Fundamentals 全部为空），本模块只处理 >= 本周一的行，天然
排除这段历史，不需要专门过滤。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db import database as db
from app.services.feishu_cli import _run_cli

logger = logging.getLogger("jarvis.oncall_feishu_sync")

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

_FIELD_FEATURE = "值班人员（Feature）"
_FIELD_FUNDAMENTALS = "值班人员（Fundamentals）"
_FIELD_DATE = "日期"


def _emails_from_person_field(value: Any) -> List[str]:
    """把 Feishu person 类型字段解析成邮箱列表(小写、去重、丢弃没有 email 的脏数据)。"""
    if not value:
        return []
    emails: List[str] = []
    for person in value:
        email = (person.get("email") or "").strip().lower()
        if email and email not in emails:
            emails.append(email)
    return emails


async def fetch_feishu_oncall_weeks(min_week_start: date) -> List[Dict[str, Any]]:
    """拉取 Feishu"本周值班"表,解析成 [{week_start, members}], 只保留 >= min_week_start 的行。

    `members` 是 Feature + Fundamentals 两个角色去重合并后的邮箱列表；两个角色都
    没人的行会被跳过——避免拿一行空数据把 Jarvis 已有排班冲掉。
    """
    settings = get_settings().feishu
    url = (
        f"/open-apis/bitable/v1/apps/{settings.app_token}"
        f"/tables/{settings.oncall_table_id}/records"
        f"?view_id={settings.oncall_view_id}&page_size=100"
    )
    result = await _run_cli("api", "GET", url, "--page-all", timeout=300)

    weeks: List[Dict[str, Any]] = []
    for item in result.get("data", {}).get("items", []):
        fields = item.get("fields", {})
        ms = fields.get(_FIELD_DATE)
        if ms is None:
            continue
        week_start = datetime.fromtimestamp(ms / 1000, tz=SHANGHAI_TZ).date()
        if week_start < min_week_start:
            continue
        members = sorted(set(
            _emails_from_person_field(fields.get(_FIELD_FEATURE))
            + _emails_from_person_field(fields.get(_FIELD_FUNDAMENTALS))
        ))
        if not members:
            continue
        weeks.append({"week_start": week_start, "members": members})

    weeks.sort(key=lambda w: w["week_start"])
    return weeks


async def diff_and_sync_oncall(feishu_weeks: List[Dict[str, Any]], dry_run: bool = False) -> Dict[str, Any]:
    """把 Feishu 的值班数据对齐进 Jarvis 排班网格,有差异的周直接覆盖写入快照表。

    没有配置 `start_date`(oncall 从未初始化过)时跳过整个同步并记录 warning——
    没有网格基准,写入的 key 没有意义。

    `dry_run=True` 时仍会算出完整的 before/after 差异并分类进 updated/unchanged
    （所以调用方能预览"如果跑了会改什么"），但跳过实际的 `upsert_week_assignment`
    写入——不落一个字节。
    """
    start_date_str = await db.get_oncall_config("start_date", "")
    if not start_date_str:
        logger.warning("Oncall start_date not configured — skip Feishu sync entirely")
        return {"skipped": True, "reason": "no_start_date", "updated": [], "unchanged": []}

    start = date.fromisoformat(start_date_str)
    groups = await db.get_oncall_groups()

    updated: List[Dict[str, Any]] = []
    unchanged: List[Dict[str, Any]] = []

    for week in feishu_weeks:
        feishu_week_start = week["week_start"]
        feishu_members = week["members"]

        week_num = (feishu_week_start - start).days // 7
        if week_num < 0:
            logger.warning(
                "Feishu week %s predates oncall start_date %s — skip", feishu_week_start, start,
            )
            continue

        jarvis_info = await db.resolve_week_group(week_num, groups, start)
        jarvis_members = sorted(m.strip().lower() for m in jarvis_info["members"])

        entry = {
            "week_start": jarvis_info["week_start"].isoformat(),
            "feishu_week_start": feishu_week_start.isoformat(),
            "before": jarvis_members,
            "after": feishu_members,
        }

        if feishu_members == jarvis_members:
            unchanged.append(entry)
            continue

        if not dry_run:
            await db.upsert_week_assignment(
                jarvis_info["week_start"], jarvis_info["week_end"],
                # 复用 resolve_week_group 覆盖前算出的 group_index（而非 -1 哨兵值）：
                # group_index 仅供展示参考，members_json 才是权威来源；这样只有本周的
                # members 被 Feishu 数据覆盖，不会往 rotation-continuation 锚点链
                # (_find_latest_week_anchor) 里塞进一个外来的、破坏后续周续轮计算的哨兵值。
                group_index=jarvis_info["group_index"],
                members=feishu_members,
                only_if_missing=False,
            )
        updated.append(entry)

    return {"skipped": False, "updated": updated, "unchanged": unchanged}


SYNC_HOUR_LOCAL = 8


async def sync_oncall_from_feishu(today: Optional[date] = None, dry_run: bool = False) -> Dict[str, Any]:
    """入口:拉取 Feishu + 对比 + 覆盖写入,并记录审计事件。供 loop 和手动触发端点复用。

    `today` 可注入(测试用),缺省取 Asia/Shanghai 的真实当前日期。
    `dry_run=True` 时只预览差异,不写库(见 `diff_and_sync_oncall`)。
    """
    resolved_today = today or datetime.now(SHANGHAI_TZ).date()
    this_monday = resolved_today - timedelta(days=resolved_today.weekday())

    try:
        feishu_weeks = await fetch_feishu_oncall_weeks(this_monday)
        result = await diff_and_sync_oncall(feishu_weeks, dry_run=dry_run)
    except Exception as e:
        logger.error("Oncall Feishu sync failed: %s", e, exc_info=True)
        await db.log_event("oncall_feishu_sync", detail={"error": str(e), "dry_run": dry_run})
        raise

    await db.log_event(
        "oncall_feishu_sync",
        detail={
            "updated_weeks": [u["week_start"] for u in result["updated"]],
            "unchanged_weeks": len(result["unchanged"]),
            "skipped": result["skipped"],
            "dry_run": dry_run,
        },
    )
    if result["updated"]:
        logger.info(
            "Oncall Feishu sync: updated %d week(s): %s",
            len(result["updated"]), [u["week_start"] for u in result["updated"]],
        )
    else:
        logger.info("Oncall Feishu sync: no changes (%d week(s) checked)", len(result["unchanged"]))
    return result


def _seconds_until_next_monday_8am(now_local: datetime) -> float:
    target = now_local.replace(hour=SYNC_HOUR_LOCAL, minute=0, second=0, microsecond=0)
    days_ahead = (0 - now_local.weekday()) % 7  # Monday == 0
    if days_ahead == 0 and now_local >= target:
        days_ahead = 7
    target = target + timedelta(days=days_ahead)
    return (target - now_local).total_seconds()


async def oncall_feishu_sync_loop():
    """Background loop: fire every Monday at 08:00 Asia/Shanghai."""
    while True:
        try:
            now_local = datetime.now(SHANGHAI_TZ)
            wait_s = _seconds_until_next_monday_8am(now_local)
            logger.info("Next oncall Feishu sync in %.1f hours (Monday 08:00 SH)", wait_s / 3600)
            await asyncio.sleep(wait_s)
            await sync_oncall_from_feishu()
            await asyncio.sleep(60)  # 避免本次跑得飞快导致同一分钟内重复触发
        except asyncio.CancelledError:
            logger.info("Oncall Feishu sync loop cancelled")
            return
        except Exception as e:
            logger.error("Oncall Feishu sync loop error (retry in 1h): %s", e, exc_info=True)
            await asyncio.sleep(3600)
