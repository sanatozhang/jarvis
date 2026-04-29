"""
GitHub PR 状态同步服务。

闭环：定时器 → 拉 gh pr view --json → 写回 DB（pr_status / merged_at / closed_at / last_synced_at）。
终态（merged / closed）后停止轮询，节省 API 配额。

🚫 严禁触发任何写操作（gh pr merge / close / ready）—— 同步仅 read。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from app.crashguard.models import CrashPullRequest
from app.db.database import get_session

logger = logging.getLogger("crashguard.pr_sync")


# 终态：不再轮询的状态
_TERMINAL_STATUSES = {"merged", "closed"}
# 同步时只查这些 GitHub state 字段
_GH_FIELDS = "state,isDraft,mergedAt,closedAt"


def _parse_repo_slug(pr_url: str) -> Optional[str]:
    """从 PR URL 抽取 owner/repo。

    https://github.com/Plaud-AI/plaud-flutter-common/pull/887 → "Plaud-AI/plaud-flutter-common"
    """
    if not pr_url:
        return None
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/pull/\d+", pr_url.strip())
    return m.group(1) if m else None


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    """gh 返回的 ISO 时间字符串（带 Z）→ naive UTC datetime。失败返回 None。"""
    if not value or not isinstance(value, str):
        return None
    try:
        # 2026-04-29T08:38:18Z → 2026-04-29T08:38:18+00:00
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        # 转为 naive UTC（与 DB 其他列一致）
        if dt.tzinfo is not None:
            dt = dt.astimezone(tz=None).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def _gh_view(repo_slug: str, pr_number: int, timeout: int = 30) -> Tuple[bool, Dict[str, Any], str]:
    """调 `gh pr view <num> --repo <slug> --json <fields>`。

    返回 (ok, parsed_json_dict, error_str)。
    抽出来便于单测 mock。
    """
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", repo_slug, "--json", _GH_FIELDS],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if r.returncode != 0:
            return False, {}, (r.stderr or "").strip()[:300]
        try:
            return True, json.loads(r.stdout or "{}"), ""
        except json.JSONDecodeError as e:
            return False, {}, f"json decode failed: {e}"
    except subprocess.TimeoutExpired:
        return False, {}, f"gh pr view timeout after {timeout}s"
    except FileNotFoundError:
        return False, {}, "gh CLI not installed"
    except Exception as e:
        return False, {}, f"gh pr view error: {e}"


def _derive_status(gh_payload: Dict[str, Any]) -> Optional[str]:
    """从 gh 输出推导本地 pr_status。

    GitHub state: OPEN / MERGED / CLOSED；isDraft: bool。
    映射：
      MERGED → merged
      CLOSED（且非 merged）→ closed
      OPEN + isDraft=True → draft
      OPEN + isDraft=False → open
    无法识别 → None（不更新）
    """
    state = (gh_payload.get("state") or "").upper()
    if state == "MERGED":
        return "merged"
    if state == "CLOSED":
        return "closed"
    if state == "OPEN":
        return "draft" if gh_payload.get("isDraft") else "open"
    return None


async def sync_pr(pr_id: int) -> Dict[str, Any]:
    """同步单条 PR。返回 {ok, pr_id, old_status?, new_status?, error?, changed?}。"""
    async with get_session() as session:
        row = (await session.execute(
            select(CrashPullRequest).where(CrashPullRequest.id == pr_id)
        )).scalar_one_or_none()
        if row is None:
            return {"ok": False, "pr_id": pr_id, "error": "pr not found"}

        old_status = row.pr_status or ""
        if old_status in _TERMINAL_STATUSES:
            # 终态不再查（safety net；批量入口已过滤）
            return {"ok": True, "pr_id": pr_id, "old_status": old_status, "skipped": "terminal"}

        repo_slug = _parse_repo_slug(row.pr_url or "")
        pr_number = row.pr_number
        if not repo_slug or not pr_number:
            return {"ok": False, "pr_id": pr_id, "error": "missing repo_slug or pr_number"}

        ok, payload, err = _gh_view(repo_slug, pr_number)
        if not ok:
            row.last_synced_at = datetime.utcnow()  # 记录尝试过，避免空转
            await session.commit()
            return {"ok": False, "pr_id": pr_id, "error": err}

        new_status = _derive_status(payload)
        if new_status is None:
            row.last_synced_at = datetime.utcnow()
            await session.commit()
            return {
                "ok": False, "pr_id": pr_id,
                "error": f"unknown gh state: {payload.get('state')}",
            }

        row.pr_status = new_status
        merged_at = _parse_iso_dt(payload.get("mergedAt"))
        if merged_at:
            row.merged_at = merged_at
        closed_at = _parse_iso_dt(payload.get("closedAt"))
        if closed_at:
            row.closed_at = closed_at
        row.last_synced_at = datetime.utcnow()
        await session.commit()

    changed = old_status != new_status
    if changed:
        logger.info(
            "crashguard pr_sync: pr=%d %s → %s (repo=%s #%d)",
            pr_id, old_status, new_status, repo_slug, pr_number,
        )
    return {
        "ok": True,
        "pr_id": pr_id,
        "old_status": old_status,
        "new_status": new_status,
        "changed": changed,
    }


async def sync_all_open_prs(limit: int = 200) -> Dict[str, Any]:
    """批量同步：所有非终态 PR。

    增量友好：terminal 状态（merged / closed）跳过，每次只查活跃 PR。
    """
    async with get_session() as session:
        rows: List[CrashPullRequest] = (await session.execute(
            select(CrashPullRequest)
            .where(CrashPullRequest.pr_status.notin_(list(_TERMINAL_STATUSES)))
            .order_by(CrashPullRequest.id.asc())
            .limit(limit)
        )).scalars().all()

    pr_ids = [r.id for r in rows]
    summary = {"checked": 0, "changed": 0, "errors": 0, "details": []}
    for pid in pr_ids:
        res = await sync_pr(pid)
        summary["checked"] += 1
        if res.get("ok") and res.get("changed"):
            summary["changed"] += 1
        elif not res.get("ok"):
            summary["errors"] += 1
        summary["details"].append(res)
    logger.info(
        "crashguard pr_sync batch done: checked=%d changed=%d errors=%d",
        summary["checked"], summary["changed"], summary["errors"],
    )
    return summary
