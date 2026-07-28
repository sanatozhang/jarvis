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

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.config import get_settings
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
