"""工作日 10:00 飞书提醒：列出所有等 review 的 crashguard 自动 PR。

抓手：crashguard 自动 PR 越攒越多没合入会污染指标 + 错过修复窗口；
每天上班时间给 reviewer 推一份积压清单逼着收尾。

口径："等 review" = merged_at IS NULL AND closed_at IS NULL AND reviewed_at IS NULL。
按 repo 分组、按 age 倒序展示；最老的放前面。

cron 由 settings.pr_pending_review_cron 控制（默认 "0 10 * * 1-5" 工作日 10:00）。
心跳记录 job_name="pr_pending_review"。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Dict, List

logger = logging.getLogger("crashguard.pr_pending_review_alert")


def _age_days(created_at: datetime) -> int:
    """从 created_at 到 now 的天数（向下取整）。"""
    delta = datetime.utcnow() - created_at
    return max(0, delta.days)


def build_pending_review_card(prs: List[Dict]) -> Dict:
    """构造飞书 interactive card：积压 PR 清单。

    prs: List[{pr_url, pr_number, repo, reviewer_emails(List[str]), age_days, pr_status}]
    """
    n = len(prs)
    template = "red" if n >= 10 else ("orange" if n >= 5 else "blue")

    # 按 repo 分组
    by_repo: Dict[str, List[Dict]] = {}
    for p in prs:
        by_repo.setdefault(p.get("repo") or "unknown", []).append(p)

    blocks: List[Dict] = []
    blocks.append({"tag": "div", "text": {
        "tag": "lark_md",
        "content": f"**积压 {n} 条 crashguard 自动 PR 等 review**——按仓库分组：",
    }})

    for repo in sorted(by_repo.keys()):
        repo_prs = sorted(by_repo[repo], key=lambda x: -x.get("age_days", 0))
        lines = [f"\n**📦 {repo} ({len(repo_prs)} 条)**"]
        for p in repo_prs:
            revs = p.get("reviewer_emails") or []
            rev_short = ", ".join(e.split("@")[0] for e in revs[:2]) if revs else "(未指派)"
            if len(revs) > 2:
                rev_short += f" +{len(revs)-2}"
            age = p.get("age_days", 0)
            age_str = f"{age}天" if age > 0 else "今天"
            status_emoji = "📝" if p.get("pr_status") == "draft" else "🔵"
            lines.append(
                f"{status_emoji} [#{p.get('pr_number')}]({p.get('pr_url')}) "
                f"· {age_str} · reviewer: {rev_short}"
            )
        blocks.append({"tag": "div", "text": {
            "tag": "lark_md",
            "content": "\n".join(lines),
        }})

    blocks.append({"tag": "hr"})
    blocks.append({"tag": "note", "elements": [{
        "tag": "plain_text",
        "content": "每个工作日 10:00 自动发送；merged / closed / 已 review 过的不再列出。",
    }]})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"⏰ crashguard 积压 PR 提醒 ({n} 条等 review)"},
            "template": template,
        },
        "elements": blocks,
    }


async def run_pending_review_alert() -> Dict:
    """主入口：拉等 review 的 PR → 构造卡片 → 发飞书。

    返回 {"pending_count": N, "sent": bool, "skip_reason": str}。
    无积压时不发，返回 sent=False, skip_reason="no_pending"。
    """
    from app.crashguard.config import get_crashguard_settings
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session
    from sqlalchemy import select

    s = get_crashguard_settings()
    if not getattr(s, "pr_pending_review_enabled", False):
        return {"pending_count": 0, "sent": False, "skip_reason": "disabled"}

    target_email = (
        (getattr(s, "feishu_alert_email", "") or "").strip()
        or (getattr(s, "pr_reviewer_fallback_email", "") or "").strip()
    )
    if not target_email:
        logger.warning("pr_pending_review_alert: no target_email configured")
        return {"pending_count": 0, "sent": False, "skip_reason": "no_target_email"}

    async with get_session() as session:
        stmt = select(CrashPullRequest).where(
            CrashPullRequest.merged_at.is_(None),
            CrashPullRequest.closed_at.is_(None),
            CrashPullRequest.reviewed_at.is_(None),
        )
        rows = (await session.execute(stmt)).scalars().all()

    if not rows:
        logger.info("pr_pending_review_alert: 0 pending PRs, skip")
        return {"pending_count": 0, "sent": False, "skip_reason": "no_pending"}

    prs: List[Dict] = []
    for r in rows:
        try:
            revs = json.loads(r.reviewer_emails or "[]")
        except (json.JSONDecodeError, TypeError):
            revs = []
        prs.append({
            "pr_url": r.pr_url or "",
            "pr_number": r.pr_number,
            "repo": r.repo or "unknown",
            "pr_status": r.pr_status or "",
            "reviewer_emails": revs,
            "age_days": _age_days(r.created_at) if r.created_at else 0,
        })

    card = build_pending_review_card(prs)

    from app.services import feishu_cli
    try:
        ok = await feishu_cli.send_interactive_card(email=target_email, card=card)
    except Exception as e:
        logger.exception("send_interactive_card failed: %s", e)
        return {"pending_count": len(prs), "sent": False, "skip_reason": f"send_error:{e}"}

    if not ok:
        return {"pending_count": len(prs), "sent": False, "skip_reason": "send_failed"}

    logger.info(
        "pr_pending_review_alert sent: count=%d target=%s",
        len(prs), target_email,
    )
    return {"pending_count": len(prs), "sent": True}
