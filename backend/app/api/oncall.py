"""
API routes for oncall schedule management.

- Admin: create/edit oncall groups
- All users: view current oncall, schedule
- Escalated tickets: view/resolve with Feishu group notification
- Stats: per-week oncall workload statistics
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta, datetime, time as dtime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db import database as db

logger = logging.getLogger("jarvis.api.oncall")
router = APIRouter()


async def resolve_duty_week(
    groups: List[Dict[str, Any]],
    start_date_str: str,
    email: str,
    today: date,
) -> Optional[Dict[str, Any]]:
    """Resolve the most recent duty week for `email`.

    Returns None when there is no schedule, the email is not in any group,
    or the person has not yet had a duty week (start in the future for them).

    2026-07-24：改成从"本周"往回逐周查排班快照表(`db.resolve_week_group`)，找
    第一个成员列表包含 email 的周次，替代原来"反向取模找 group_index"的公式——
    旧公式假设 group_index 与成员的对应关系从始至终不变，但组配置一旦被编辑
    (`save_oncall_groups` 全删全建)，同一个 group_index 可能对应不同的人，快照表
    按实际成员走则不受这个假设影响。
    """
    if not groups or not start_date_str:
        return None
    email_l = email.strip().lower()
    try:
        start = date.fromisoformat(start_date_str)
    except ValueError:
        return None

    current_week = max(0, (today - start).days // 7)
    for wn in range(current_week, -1, -1):
        info = await db.resolve_week_group(wn, groups, start)
        members_l = [m.strip().lower() for m in info["members"]]
        if email_l in members_l:
            partners = [m for m in info["members"] if m.strip().lower() != email_l]
            return {
                "group_index": info["group_index"],
                "week_num": wn,
                "week_start": info["week_start"],
                "week_end": info["week_end"],
                "is_current": wn == current_week,
                "partners": partners,
            }
    return None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class OncallGroupInput(BaseModel):
    members: List[str]  # feishu emails


class OncallScheduleInput(BaseModel):
    groups: List[OncallGroupInput]
    start_date: str  # ISO date: "2026-02-10"


# ---------------------------------------------------------------------------
# Read endpoints (all users)
# ---------------------------------------------------------------------------
@router.get("/current")
async def get_current_oncall():
    """Get this week's oncall members."""
    info = await db.get_current_oncall_info()
    return {"members": info["members"], "count": len(info["members"]), "group_index": info["group_index"]}


@router.get("/week-groups")
async def get_week_groups():
    """周 → 值班组的完整映射(从 week 0 到当前周)，优先查排班快照表，查不到才现算。

    供前端替换本地"重算取模"逻辑用——按历史日期给工单归组/高亮当前组时，不再
    自己用 JS 重新实现一遍取模公式，直接查这个权威列表。
    """
    groups = await db.get_oncall_groups()
    start_date_str = await db.get_oncall_config("start_date", "")
    if not start_date_str or not groups:
        return {"weeks": [], "current_week_num": 0}

    start = date.fromisoformat(start_date_str)
    today = date.today()
    current_week_num = max(0, (today - start).days // 7)

    weeks = []
    for wn in range(0, current_week_num + 1):
        info = await db.resolve_week_group(wn, groups, start)
        weeks.append({
            "week_num": wn,
            "group_index": info["group_index"],
            "members": info["members"],
            "week_start": info["week_start"].isoformat(),
            "week_end": info["week_end"].isoformat(),
        })
    return {"weeks": weeks, "current_week_num": current_week_num}


@router.get("/schedule")
async def get_schedule():
    """Get full oncall schedule (all groups + config)."""
    groups = await db.get_oncall_groups()
    start_date = await db.get_oncall_config("start_date", "")
    return {
        "groups": groups,
        "start_date": start_date,
        "total_groups": len(groups),
    }


# ---------------------------------------------------------------------------
# Write endpoints (admin only — enforced by frontend, checked by username)
# ---------------------------------------------------------------------------
_ASSIGNMENT_HORIZON_WEEKS = 52  # 每次编辑往未来预生成多少周的快照(经验值，写入量小)


async def _regenerate_week_assignments(
    old_groups: List[Dict[str, Any]],
    old_start_date_str: str,
    new_groups: List[List[str]],
    new_start_date_str: str,
) -> None:
    """组配置变化时重算排班快照(2026-07-24)。

    1) 冻结"本周"：本周从没被冻结过时才写入一次——若有旧配置，按旧配置算出本周
       该是谁（保证这次编辑不会把正在进行中的本周值班顶替掉）；若是首次配置
       （没有旧配置），按新配置算（没有"旧值"可保护）。`only_if_missing=True`，
       已经冻结过的本周不会因为同一周内再编辑而被重新计算。
    2) 未来 `_ASSIGNMENT_HORIZON_WEEKS` 周：一律按新配置覆盖生成——未来周次在
       轮到之前都可以被后续编辑改变，这是设计上允许的。
    """
    today = date.today()

    if old_groups and old_start_date_str:
        try:
            old_start = date.fromisoformat(old_start_date_str)
            old_week_num = max(0, (today - old_start).days // 7)
            old_info = await db.resolve_week_group(old_week_num, old_groups, old_start)
            await db.upsert_week_assignment(
                old_info["week_start"], old_info["week_end"], old_info["group_index"],
                old_info["members"], only_if_missing=True,
            )
        except ValueError:
            pass

    if not new_groups or not new_start_date_str:
        return
    new_groups_dicts = [{"group_index": i, "members": m} for i, m in enumerate(new_groups)]
    try:
        new_start = date.fromisoformat(new_start_date_str)
    except ValueError:
        return
    new_week_num_today = max(0, (today - new_start).days // 7)

    if not (old_groups and old_start_date_str):
        # 首次配置：本周也按新配置生成(insert-only-if-missing，避免覆盖上面刚写的)
        info = await db.resolve_week_group(new_week_num_today, new_groups_dicts, new_start)
        await db.upsert_week_assignment(
            info["week_start"], info["week_end"], info["group_index"], info["members"],
            only_if_missing=True,
        )

    for offset in range(1, _ASSIGNMENT_HORIZON_WEEKS + 1):
        wn = new_week_num_today + offset
        idx = wn % len(new_groups)
        week_start = new_start + timedelta(weeks=wn)
        week_end = week_start + timedelta(days=6)
        await db.upsert_week_assignment(
            week_start, week_end, idx, new_groups[idx], only_if_missing=False,
        )


@router.put("/schedule")
async def update_schedule(
    req: OncallScheduleInput,
    username: str = Query(..., description="Admin username"),
):
    """Update oncall schedule (admin only)."""
    user = await db.get_user(username)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can edit oncall schedule")

    # 2026-07-24：改动前先读旧配置，用于"冻结本周"——必须在两个写操作之前读，
    # 否则读到的就已经是这次编辑后的新配置，没法保护正在进行中的本周值班。
    old_groups = await db.get_oncall_groups()
    old_start_date_str = await db.get_oncall_config("start_date", "")

    groups = [g.members for g in req.groups]
    await db.save_oncall_groups(groups, created_by=username)
    await db.set_oncall_config("start_date", req.start_date)

    await _regenerate_week_assignments(old_groups, old_start_date_str, groups, req.start_date)

    logger.info("Oncall schedule updated by %s: %d groups, start=%s", username, len(groups), req.start_date)
    return {"status": "ok", "groups": len(groups), "start_date": req.start_date}


# ---------------------------------------------------------------------------
# Escalated tickets (oncall workload view)
# ---------------------------------------------------------------------------
@router.get("/tickets")
async def get_escalated_tickets(
    status: Optional[str] = Query(None, description="Filter: in_progress / resolved"),
    weeks: int = Query(0, description="0 = all history, N = last N weeks"),
):
    """Get escalated tickets. weeks=0 returns all."""

    since_date = None
    if weeks > 0:
        start_date_str = await db.get_oncall_config("start_date", "")
        oncall_start = None
        if start_date_str:
            try:
                oncall_start = date.fromisoformat(start_date_str)
            except ValueError:
                pass

        today = date.today()
        if oncall_start:
            days_since_start = (today - oncall_start).days
            current_week_num = days_since_start // 7
            cutoff_week_num = max(0, current_week_num - weeks + 1)
            since_date = oncall_start + timedelta(weeks=cutoff_week_num)
        else:
            since_date = today - timedelta(days=weeks * 7)

    items = await db.get_escalated_issues(status=status, since_date=since_date)

    return {
        "tickets": items,
        "count": len(items),
        "since_date": since_date.isoformat() if since_date else "",
        "weeks": weeks,
    }


@router.get("/feishu-tickets")
async def get_feishu_tickets(
    status: str = Query("open", description="open = pending+in_progress / done / all"),
    limit: int = Query(200, ge=1, le=500),
    oncall_only: bool = Query(True, description="Only tickets assigned to the current oncall members"),
):
    """List tickets being handled directly in Feishu (read-only, no DB write).

    `open` (default) returns pending + in_progress. When `oncall_only` is true
    (default), only tickets whose 问题指派人 list CONTAINS a current oncall member
    (matched by email) are returned — the assignee list usually has 2 people, so
    matching is membership, not equality.
    """
    from app.services.feishu import FeishuClient

    emails = await db.get_current_oncall() if oncall_only else []

    client = FeishuClient()
    if status == "open":
        pending = await client.list_issues_by_status("pending", limit=limit, assignee_emails=emails)
        in_progress = await client.list_issues_by_status("in_progress", limit=limit, assignee_emails=emails)
        issues = in_progress + pending
    else:
        issues = await client.list_issues_by_status(status, limit=limit, assignee_emails=emails)

    # Many tickets → default sort by creation time, newest first.
    issues.sort(key=lambda i: i.created_at_ms, reverse=True)

    tickets = [i.model_dump(mode="json") for i in issues]
    return {
        "tickets": tickets,
        "count": len(tickets),
        "status": status,
        "oncall_only": oncall_only,
        "oncall_members": emails,
    }


@router.put("/feishu-tickets/{record_id}/resolve")
async def resolve_feishu_ticket(record_id: str):
    """Mark a Feishu ticket as done (sets 确认提交=true on the bitable)."""
    from app.services.feishu import FeishuClient

    ok = await FeishuClient().mark_completed(record_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to mark Feishu ticket complete")
    return {"status": "resolved", "record_id": record_id}


@router.get("/stats")
async def get_oncall_stats():
    """Per-week oncall workload statistics."""
    groups = await db.get_oncall_groups()
    start_date_str = await db.get_oncall_config("start_date", "")
    if not start_date_str or not groups:
        return {"weeks": [], "groups": [g["members"] for g in groups]}

    oncall_start = date.fromisoformat(start_date_str)
    today = date.today()
    current_week_num = max(0, (today - oncall_start).days // 7)

    # Fetch ALL escalated tickets
    all_tickets = await db.get_escalated_issues()

    # Build a lookup: week_number -> list of tickets
    week_tickets: Dict[int, List[Dict[str, Any]]] = {}
    for tk in all_tickets:
        esc_at = tk.get("escalated_at", "")
        if not esc_at:
            continue
        esc_date = date.fromisoformat(esc_at[:10])
        wn = (esc_date - oncall_start).days // 7
        week_tickets.setdefault(wn, []).append(tk)

    # Build week stats (most recent first, up to 12 weeks)
    # 2026-07-24：每周的 group_index/members 改为优先查排班快照表(历史/当前周
    # 固定不受后续组数变化影响)，查不到才现算 wn % total_groups 兜底。
    week_stats = []
    start_week = max(0, current_week_num - 11)
    for wn in range(current_week_num, start_week - 1, -1):
        info = await db.resolve_week_group(wn, groups, oncall_start)
        gi = info["group_index"]
        w_start = info["week_start"]
        w_end = info["week_end"]
        tks = week_tickets.get(wn, [])
        in_progress = sum(1 for t in tks if t.get("escalation_status") != "resolved")
        resolved = sum(1 for t in tks if t.get("escalation_status") == "resolved")
        week_stats.append({
            "week_num": wn,
            "group_index": gi,
            "members": info["members"],
            "week_start": w_start.isoformat(),
            "week_end": w_end.isoformat(),
            "is_current": wn == current_week_num,
            "total": len(tks),
            "in_progress": in_progress,
            "resolved": resolved,
        })

    return {
        "weeks": week_stats,
        "groups": [g["members"] for g in groups],
        "start_date": start_date_str,
        "current_week_num": current_week_num,
    }


@router.get("/my-workload")
async def get_my_workload(email: str = Query(..., description="Oncall member email")):
    """Aggregate the tickets an oncall member must handle in their most recent
    duty week: apollo escalated tickets + Feishu tickets, with links + attachments.
    Read-only.
    """
    from app.config import get_settings
    from app.services.feishu import FeishuClient

    groups = await db.get_oncall_groups()
    start_date_str = await db.get_oncall_config("start_date", "")
    info = await resolve_duty_week(groups, start_date_str, email, date.today())
    if info is None:
        raise HTTPException(status_code=404, detail=f"{email} is not an oncall member or no schedule configured")

    week_start = info["week_start"]
    week_end = info["week_end"]
    frontend_base = (get_settings().frontend_base_url or "").rstrip("/")
    email_l = email.strip().lower()

    # --- apollo escalated tickets within window, still open ---
    apollo_tickets = []
    for it in await db.get_escalated_issues(status=None):
        if it.get("escalation_status") == "resolved":
            continue
        esc = it.get("escalated_at") or ""
        if not esc:
            continue
        try:
            esc_d = date.fromisoformat(esc[:10])
        except ValueError:
            continue
        if not (week_start <= esc_d <= week_end):
            continue
        zid = it.get("zendesk_id", "")
        rid = it["record_id"]
        apollo_tickets.append({
            "record_id": rid,
            "description": it.get("description", ""),
            "problem_type": it.get("problem_type", ""),
            "root_cause": it.get("root_cause", ""),
            "confidence": it.get("confidence", ""),
            "zendesk_id": zid,
            "zendesk_url": FeishuClient._normalize_zendesk_url(zid) if zid else "",
            "escalated_at": esc,
            "escalated_by": it.get("escalated_by", ""),
            "escalation_status": it.get("escalation_status", ""),
            "escalation_share_link": it.get("escalation_share_link", ""),
            "apollo_url": f"{frontend_base}/tracking?detail={rid}" if frontend_base else "",
            "logs_download_url": f"/api/local/{rid}/download-logs",
        })

    # --- Feishu tickets assigned to email, created within window, open ---
    start_ms = int(datetime.combine(week_start, dtime.min).timestamp() * 1000)
    end_ms = int(datetime.combine(week_end, dtime.max).timestamp() * 1000)
    client = FeishuClient()
    pending = await client.list_issues_by_status("pending", limit=200, assignee_emails=[email_l])
    in_progress = await client.list_issues_by_status("in_progress", limit=200, assignee_emails=[email_l])

    feishu_tickets = []
    for iss in in_progress + pending:
        if not (start_ms <= iss.created_at_ms <= end_ms):
            continue
        attachments = [
            {"name": f.name, "size": f.size, "download_path": f"/api/local/{iss.record_id}/files/{f.name}"}
            for f in iss.log_files
        ]
        feishu_tickets.append({
            "record_id": iss.record_id,
            "description": iss.description,
            "priority": iss.priority,
            "device_sn": iss.device_sn,
            "firmware": iss.firmware,
            "app_version": iss.app_version,
            "assignee": iss.assignee,
            "assignee_emails": iss.assignee_emails,
            "feishu_link": iss.feishu_link,
            "zendesk": iss.zendesk,
            "zendesk_id": iss.zendesk_id,
            "feishu_status": iss.feishu_status.value if hasattr(iss.feishu_status, "value") else iss.feishu_status,
            "created_at_ms": iss.created_at_ms,
            "attachments": attachments,
        })

    with_attachments = sum(1 for t in feishu_tickets if t["attachments"]) + len(apollo_tickets)
    return {
        "email": email_l,
        "duty_week": {
            "week_num": info["week_num"],
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "is_current": info["is_current"],
        },
        "oncall_partners": info["partners"],
        "apollo_tickets": apollo_tickets,
        "feishu_tickets": feishu_tickets,
        "summary": {
            "apollo_count": len(apollo_tickets),
            "feishu_count": len(feishu_tickets),
            "total": len(apollo_tickets) + len(feishu_tickets),
            "with_attachments": with_attachments,
        },
    }


@router.put("/tickets/{issue_id}/resolve")
async def resolve_ticket(issue_id: str):
    """Mark an escalated ticket as resolved + notify Feishu group."""
    async with db.get_session() as session:
        issue = await session.get(db.IssueRecord, issue_id)
    if not issue or not issue.escalated_at:
        raise HTTPException(status_code=404, detail="Escalated issue not found")

    # resolve + 群通知统一走 feishu_cli 的共享逻辑（详情页 mark_complete 也调它）
    from app.services.feishu_cli import resolve_escalation_and_notify
    esc = await resolve_escalation_and_notify(issue_id)
    if not esc["resolved"]:
        raise HTTPException(status_code=404, detail="Failed to resolve")

    return {"status": "resolved", "issue_id": issue_id, "feishu_notified": esc["feishu_notified"]}
