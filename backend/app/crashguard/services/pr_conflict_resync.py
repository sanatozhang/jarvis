"""自动 PR 落后 base / 冲突自愈（2026-08-20）。

不做本地 git checkout/rebase/force-push——`pr_drafter._run_git` 的安全护栏
明确禁止 `rebase`/`merge` 子命令（防自动化误合并），这里也不去绕开它。
改用 GitHub 官方 "Update pull request branch" API（服务端把 base 合并进
PR 分支，`gh api -X PUT .../pulls/{n}/update-branch`）：干净能合就直接
成功，真冲突就报错——把这个报错当成"需要人工解决"的信号去通知，机器人
绝不猜着改代码解冲突。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from app.crashguard.config import get_crashguard_settings
from app.crashguard.models import CrashPullRequest
from app.db.database import get_session
from app.services.feishu_cli import send_message

logger = logging.getLogger("crashguard.pr_conflict_resync")

_PR_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)")


def _parse_pr_url(pr_url: str) -> Tuple[Optional[str], Optional[int]]:
    m = _PR_URL_RE.search(pr_url or "")
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


def _gh(cmd: list[str], timeout: int = 30) -> Tuple[int, str, str]:
    # 同 pr_drafter._run_git 的剥 token 处理：GH_TOKEN/GITHUB_TOKEN 个人 PAT
    # 没有 org SSO 权限，剥掉让 gh CLI 走 hosts.yml 的 OAuth token。
    sub_env = dict(os.environ)
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        sub_env.pop(k, None)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=sub_env)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:
        return 1, "", str(e)


def _check_merge_state(slug: str, pr_number: int) -> Dict[str, Any]:
    rc, out, err = _gh([
        "gh", "pr", "view", str(pr_number), "--repo", slug,
        "--json", "mergeStateStatus,state",
    ])
    if rc != 0:
        return {"ok": False, "error": err[:200] or "gh pr view failed"}
    try:
        data = json.loads(out)
    except Exception as e:
        return {"ok": False, "error": f"parse failed: {e}"}
    return {"ok": True, **data}


def _update_branch(slug: str, pr_number: int) -> Dict[str, Any]:
    """PUT /repos/{slug}/pulls/{pr_number}/update-branch —— 服务端把 base 合并进这个分支。"""
    rc, out, err = _gh([
        "gh", "api", "-X", "PUT", f"repos/{slug}/pulls/{pr_number}/update-branch",
    ])
    if rc == 0:
        return {"ok": True}
    return {"ok": False, "error": err[:300] or "update-branch failed"}


async def run_conflict_resync_sweep() -> Dict[str, Any]:
    """主入口：扫所有非终态 PR，落后 base 就服务端更新，真冲突就收集起来通知。"""
    s = get_crashguard_settings()
    if not getattr(s, "conflict_resync_enabled", False):
        return {"ok": True, "skipped": "disabled"}

    async with get_session() as session:
        rows = (await session.execute(
            select(CrashPullRequest).where(CrashPullRequest.pr_status.in_(("draft", "open")))
        )).scalars().all()

    updated: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for row in rows:
        slug, pr_number = _parse_pr_url(row.pr_url)
        if not slug or not pr_number:
            skipped.append({"id": row.id, "reason": "unparseable_pr_url"})
            continue

        state = _check_merge_state(slug, pr_number)
        if not state.get("ok"):
            errors.append({"id": row.id, "pr_url": row.pr_url, "error": state.get("error")})
            continue
        if state.get("state") != "OPEN":
            skipped.append({"id": row.id, "reason": f"state={state.get('state')}"})
            continue

        merge_status = state.get("mergeStateStatus")
        if merge_status == "BEHIND":
            res = _update_branch(slug, pr_number)
            if res.get("ok"):
                updated.append({"id": row.id, "pr_url": row.pr_url})
            else:
                # update-branch 服务端报错通常就是真冲突（GitHub 自己算出来合不了）。
                conflicts.append({
                    "id": row.id, "pr_url": row.pr_url,
                    "reason": res.get("error"), "reviewer_emails": row.reviewer_emails,
                })
        elif merge_status == "DIRTY":
            conflicts.append({
                "id": row.id, "pr_url": row.pr_url,
                "reason": "conflicting with base branch",
                "reviewer_emails": row.reviewer_emails,
            })
        # CLEAN / UNKNOWN / BLOCKED / UNSTABLE / DRAFT / HAS_HOOKS：没有可动的事，跳过。

    if conflicts:
        await _notify_conflicts(conflicts)

    return {
        "ok": True,
        "updated": len(updated),
        "conflicts": len(conflicts),
        "skipped": len(skipped),
        "errors": len(errors),
        "detail": {
            "updated": updated, "conflicts": conflicts,
            "skipped": skipped, "errors": errors,
        },
    }


async def _notify_conflicts(conflicts: List[Dict[str, Any]]) -> None:
    s = get_crashguard_settings()
    target = (
        getattr(s, "conflict_resync_fallback_email", "")
        or getattr(s, "pr_reviewer_fallback_email", "")
    )
    if not target:
        logger.warning("conflict_resync: no fallback email configured, skip notify")
        return

    lines = [
        "🔴 以下 crashguard PR 与 base 分支有冲突，需要人工解决（机器人不会自动改代码解冲突）：",
        "",
    ]
    for c in conflicts:
        try:
            emails = json.loads(c.get("reviewer_emails") or "[]")
        except Exception:
            emails = []
        who = "、".join(emails) if emails else "未指派"
        lines.append(f"- {c['pr_url']}（负责人：{who}）")

    try:
        await send_message(email=target, text="\n".join(lines))
    except Exception:
        logger.exception("conflict_resync: failed to send Feishu notification")
