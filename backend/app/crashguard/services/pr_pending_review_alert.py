"""工作日 10:00 飞书 PR 日报：今日 merged/closed/新建 stats + 积压清单。

抓手：crashguard 自动 PR 越攒越多没合入会污染指标 + 错过修复窗口；
每天上班时间给 reviewer 推一份积压清单逼着收尾。同时附今日 merged/closed/新建
统计，让管理者一眼看到 PR 流速。

口径：
- 今日 merged = merged_at 落在北京当日 [00:00, 24:00)
- 今日 closed = closed_at 落在北京当日 [00:00, 24:00)（merged 也算 closed，分开统计）
- 今日新建 = created_at 落在北京当日
- pending（等 review）= merged_at IS NULL AND closed_at IS NULL AND reviewed_at IS NULL

cron 由 settings.pr_pending_review_cron 控制（默认 "0 10 * * 1-5" 工作日 10:00）。
心跳记录 job_name="pr_pending_review"。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List

logger = logging.getLogger("crashguard.pr_pending_review_alert")


def _now_local() -> datetime:
    """容器内 datetime.now() 已是北京时间（TZ=Asia/Shanghai）。

    抽出成 helper 方便测试 monkeypatch（datetime.datetime.now 不可直接 setattr）。
    """
    return datetime.now()


def _today_utc_window() -> tuple[datetime, datetime]:
    """北京"今日" [00:00, 24:00) 对应的 UTC naive 范围。

    容器内 datetime.now() 已是北京时间（TZ=Asia/Shanghai）；
    db 字段（merged_at/closed_at/created_at）写入用 utcnow() → UTC naive。
    返回 (start_utc, end_utc) 用于 SQL >= / < 过滤。
    """
    now_local = _now_local()
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    # 北京 → UTC 减 8 小时（容器内 .now() 是 +08，所以本地 0:00 = UTC 16:00 前一天）
    start_utc = start_local - timedelta(hours=8)
    end_utc = start_utc + timedelta(days=1)
    return start_utc, end_utc


def _age_days(created_at: datetime) -> int:
    """从 created_at 到 now 的天数（向下取整）。"""
    delta = datetime.utcnow() - created_at
    return max(0, delta.days)


def build_pending_review_card(prs: List[Dict], stats: Dict[str, int] = None) -> Dict:
    """构造飞书 interactive card：日报 stats + 积压 PR 清单。

    prs: List[{pr_url, pr_number, repo, reviewer_emails(List[str]), age_days, pr_status}]
    stats: {"today_merged": N, "today_closed": M, "today_created": K, "total_pending": T}
           若为 None，按 prs 长度回退（保持向后兼容）。
    """
    n = len(prs)
    s = stats or {}
    today_merged = int(s.get("today_merged", 0))
    today_closed = int(s.get("today_closed", 0))
    today_created = int(s.get("today_created", 0))
    total_pending = int(s.get("total_pending", n))

    # 色阶：今日 merged 多 = green, 单看 pending → orange/red
    if today_merged >= 3 and total_pending < 10:
        template = "green"
    elif total_pending >= 10:
        template = "red"
    elif total_pending >= 5:
        template = "orange"
    else:
        template = "blue"

    # 按 repo 分组
    by_repo: Dict[str, List[Dict]] = {}
    for p in prs:
        by_repo.setdefault(p.get("repo") or "unknown", []).append(p)

    blocks: List[Dict] = []
    # 顶部 stats 区块
    today_str = datetime.now().strftime("%Y-%m-%d (%a)")
    blocks.append({"tag": "div", "text": {
        "tag": "lark_md",
        "content": (
            f"**📅 {today_str}**\n\n"
            f"📊 **今日 PR 流速**:\n"
            f"  ✅ merged: **{today_merged}**\n"
            f"  ❌ closed (未合): **{today_closed}**\n"
            f"  🆕 新建: **{today_created}**\n"
            f"  ⏳ 当前 pending (等 review): **{total_pending}**"
        ),
    }})
    blocks.append({"tag": "hr"})
    blocks.append({"tag": "div", "text": {
        "tag": "lark_md",
        "content": f"**📋 积压清单（{n} 条等 review）**——按仓库分组：",
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
            "title": {
                "tag": "plain_text",
                "content": f"📊 crashguard PR 日报 · 今日 +{today_merged} merged / {total_pending} pending",
            },
            "template": template,
        },
        "elements": blocks,
    }


async def _collect_today_stats(session) -> Dict[str, int]:
    """拉今日（北京）的 merged / closed / created PR 计数。

    closed 计数排除 merged（避免重复 — merged PR 也会有 closed_at）。
    """
    from app.crashguard.models import CrashPullRequest
    from sqlalchemy import select, func, and_

    start_utc, end_utc = _today_utc_window()

    merged_q = select(func.count()).select_from(CrashPullRequest).where(
        and_(CrashPullRequest.merged_at >= start_utc,
             CrashPullRequest.merged_at < end_utc)
    )
    closed_q = select(func.count()).select_from(CrashPullRequest).where(
        and_(CrashPullRequest.closed_at >= start_utc,
             CrashPullRequest.closed_at < end_utc,
             CrashPullRequest.merged_at.is_(None))  # 排除 merged 双计
    )
    created_q = select(func.count()).select_from(CrashPullRequest).where(
        and_(CrashPullRequest.created_at >= start_utc,
             CrashPullRequest.created_at < end_utc)
    )
    today_merged = (await session.execute(merged_q)).scalar_one() or 0
    today_closed = (await session.execute(closed_q)).scalar_one() or 0
    today_created = (await session.execute(created_q)).scalar_one() or 0
    return {
        "today_merged": int(today_merged),
        "today_closed": int(today_closed),
        "today_created": int(today_created),
    }


async def run_pending_review_alert() -> Dict:
    """主入口：拉今日 stats + 等 review PR → 构造日报卡片 → 发飞书。

    返回 {"pending_count": N, "today_merged": M, "today_closed": K,
          "today_created": L, "sent": bool, "skip_reason": str}。

    即便 pending=0，只要今日有 merged / closed / created 任一不为 0 也发日报
    （管理者能看到 PR 流速）；全 0 才跳过。
    """
    from app.crashguard.config import get_crashguard_settings
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session
    from sqlalchemy import select

    s = get_crashguard_settings()
    if not getattr(s, "pr_pending_review_enabled", False):
        return {"pending_count": 0, "sent": False, "skip_reason": "disabled"}

    # 双保险：cron 已经限制工作日，service 层再过滤一次（即使配置被误改也不会周末发）
    if _now_local().weekday() >= 5:
        return {"pending_count": 0, "sent": False, "skip_reason": "weekend"}

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
        stats = await _collect_today_stats(session)

    total_pending = len(rows)
    activity_today = (
        stats["today_merged"] + stats["today_closed"] + stats["today_created"]
    )
    if total_pending == 0 and activity_today == 0:
        logger.info("pr_pending_review_alert: 0 pending + 0 today activity, skip")
        return {
            "pending_count": 0,
            **stats,
            "sent": False, "skip_reason": "no_pending_no_activity",
        }

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

    card = build_pending_review_card(prs, stats={
        **stats, "total_pending": total_pending,
    })

    from app.services import feishu_cli
    try:
        ok = await feishu_cli.send_interactive_card(email=target_email, card=card)
    except Exception as e:
        logger.exception("send_interactive_card failed: %s", e)
        return {
            "pending_count": total_pending, **stats,
            "sent": False, "skip_reason": f"send_error:{e}",
        }

    if not ok:
        return {
            "pending_count": total_pending, **stats,
            "sent": False, "skip_reason": "send_failed",
        }

    logger.info(
        "pr_pending_review_alert sent: pending=%d merged=%d closed=%d created=%d target=%s",
        total_pending, stats["today_merged"], stats["today_closed"],
        stats["today_created"], target_email,
    )
    return {"pending_count": total_pending, **stats, "sent": True}
