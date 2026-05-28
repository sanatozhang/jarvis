"""工作日 10:00 飞书 PR 日报：昨日 merged/closed/新建 stats + 当前积压清单。

抓手：crashguard 自动 PR 越攒越多没合入会污染指标 + 错过修复窗口；
每天上班时间给 reviewer 推**昨日完整 24h 的交付汇总** + **当前积压清单**，
闭环昨日、规划今日。早上 10:00 报"今日"无意义——才上班 1h，数据近乎零。

口径：
- 昨日 merged = merged_at 落在北京昨日 [00:00, 24:00)
- 昨日 closed = closed_at 落在北京昨日 [00:00, 24:00)（merged 也算 closed，分开统计）
- 昨日新建 = created_at 落在北京昨日
- pending（等 review）= merged_at IS NULL AND closed_at IS NULL AND reviewed_at IS NULL
  （当前快照，非昨日；积压是动态的）

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


def _yesterday_utc_window() -> tuple[datetime, datetime]:
    """北京"昨日" [00:00, 24:00) 对应的 UTC naive 范围。

    容器内 datetime.now() 已是北京时间（TZ=Asia/Shanghai）；
    db 字段（merged_at/closed_at/created_at）写入用 utcnow() → UTC naive。
    返回 (start_utc, end_utc) 用于 SQL >= / < 过滤。

    例：北京 2026-05-28 10:00 触发 → 窗口为北京 [2026-05-27 00:00, 2026-05-28 00:00)
    → UTC naive [2026-05-26 16:00, 2026-05-27 16:00)。
    """
    now_local = _now_local()
    today_local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_local_midnight = today_local_midnight - timedelta(days=1)
    # 北京 → UTC 减 8 小时
    start_utc = yesterday_local_midnight - timedelta(hours=8)
    end_utc = start_utc + timedelta(days=1)
    return start_utc, end_utc


def _yesterday_local_date_str() -> str:
    """北京"昨日"的 YYYY-MM-DD (Weekday) 字符串，用于卡片展示。"""
    yesterday = _now_local() - timedelta(days=1)
    return yesterday.strftime("%Y-%m-%d (%a)")


def _age_days(created_at: datetime) -> int:
    """从 created_at 到 now 的天数（向下取整）。"""
    delta = datetime.utcnow() - created_at
    return max(0, delta.days)


def build_pending_review_card(
    prs: List[Dict],
    stats: Dict[str, int] = None,
    frontend_base_url: str = "",
    approved_prs: List[Dict] = None,
) -> Dict:
    """构造飞书 interactive card：昨日交付 stats + approved 待 merge + 当前积压清单。

    prs: List[{pr_url, pr_number, repo, reviewer_emails(List[str]), age_days, pr_status}]
    approved_prs: 已 approve 但未 merge 的 PR 清单（卡最后一公里），同 prs 结构；
                  None 或空列表则不渲染该小节。
    stats: {"yesterday_merged": N, "yesterday_closed": M, "yesterday_created": K,
            "total_pending": T, "total_approved": A}
           若为 None，按 prs 长度回退（保持向后兼容）。
    frontend_base_url: 用于生成"查看完整 PR 列表"链接（按 status 筛选 merged/closed/...）；
                       空字符串则不渲染该链接（向后兼容）。
    """
    approved_prs = approved_prs or []
    n = len(prs)
    n_approved = len(approved_prs)
    s = stats or {}
    yesterday_merged = int(s.get("yesterday_merged", 0))
    yesterday_closed = int(s.get("yesterday_closed", 0))
    yesterday_created = int(s.get("yesterday_created", 0))
    total_pending = int(s.get("total_pending", n))
    total_approved = int(s.get("total_approved", n_approved))

    # 色阶：昨日 merged 多 = green（流速好），积压多 → orange/red
    if yesterday_merged >= 3 and total_pending < 10:
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
    # 顶部 stats 区块：报昨日（不是今日）
    today_str = _now_local().strftime("%Y-%m-%d (%a)")
    yesterday_str = _yesterday_local_date_str()
    blocks.append({"tag": "div", "text": {
        "tag": "lark_md",
        "content": (
            f"**📅 今日 {today_str} · 昨日收尾汇总**\n\n"
            f"📊 **昨日 PR 流速（{yesterday_str}）**:\n"
            f"  ✅ merged: **{yesterday_merged}**\n"
            f"  ❌ closed (未合): **{yesterday_closed}**\n"
            f"  🆕 新建: **{yesterday_created}**\n"
            f"  ⏳ 当前 pending (等 review): **{total_pending}**\n"
            f"  🟢 已 approve 待 merge: **{total_approved}**"
        ),
    }})
    # 加一行"完整 PR 列表"入口（按状态可筛 merged/closed/draft/open）
    if frontend_base_url:
        pr_list_url = f"{frontend_base_url.rstrip('/')}/crashguard/pull-requests"
        blocks.append({"tag": "div", "text": {
            "tag": "lark_md",
            "content": (
                f"🔗 **查看完整 PR 列表（merged / closed / draft / open 全状态）**："
                f"[{pr_list_url}]({pr_list_url})"
            ),
        }})

    # 「✅ 已 approve 待 merge」清单 — 卡最后一公里，PR 作者需推 merge
    if approved_prs:
        blocks.append({"tag": "hr"})
        blocks.append({"tag": "div", "text": {
            "tag": "lark_md",
            "content": f"**🟢 已 approve 待 merge（{n_approved} 条）**——PR 作者请尽快合入：",
        }})
        approved_by_repo: Dict[str, List[Dict]] = {}
        for p in approved_prs:
            approved_by_repo.setdefault(p.get("repo") or "unknown", []).append(p)
        for repo in sorted(approved_by_repo.keys()):
            repo_prs = sorted(approved_by_repo[repo], key=lambda x: -x.get("age_days", 0))
            lines = [f"\n**📦 {repo} ({len(repo_prs)} 条)**"]
            for p in repo_prs:
                age = p.get("age_days", 0)
                age_str = f"{age}天" if age > 0 else "今天"
                lines.append(
                    f"🟢 [#{p.get('pr_number')}]({p.get('pr_url')}) · {age_str} · approved"
                )
            blocks.append({"tag": "div", "text": {
                "tag": "lark_md",
                "content": "\n".join(lines),
            }})

    blocks.append({"tag": "hr"})
    blocks.append({"tag": "div", "text": {
        "tag": "lark_md",
        "content": f"**📋 当前积压（{n} 条等 review）**——按仓库分组：",
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
        "content": "每个工作日 10:00 自动发送；昨日完整 24h 交付 + 当前积压；merged / closed / 已 review 过的不再列出。",
    }]})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": (
                    f"📊 crashguard PR 日报 · 昨日 +{yesterday_merged} merged "
                    f"/ 待 merge {total_approved} / pending {total_pending}"
                ),
            },
            "template": template,
        },
        "elements": blocks,
    }


async def _collect_yesterday_stats(session) -> Dict[str, int]:
    """拉昨日（北京）完整 24h 的 merged / closed / created PR 计数。

    closed 计数排除 merged（避免重复 — merged PR 也会有 closed_at）。
    """
    from app.crashguard.models import CrashPullRequest
    from sqlalchemy import select, func, and_

    start_utc, end_utc = _yesterday_utc_window()

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
    yesterday_merged = (await session.execute(merged_q)).scalar_one() or 0
    yesterday_closed = (await session.execute(closed_q)).scalar_one() or 0
    yesterday_created = (await session.execute(created_q)).scalar_one() or 0
    return {
        "yesterday_merged": int(yesterday_merged),
        "yesterday_closed": int(yesterday_closed),
        "yesterday_created": int(yesterday_created),
    }


async def run_pending_review_alert() -> Dict:
    """主入口：拉昨日 stats + 当前等 review PR → 构造日报卡片 → 发飞书。

    返回 {"pending_count": N, "yesterday_merged": M, "yesterday_closed": K,
          "yesterday_created": L, "sent": bool, "skip_reason": str}。

    即便 pending=0，只要昨日有 merged / closed / created 任一不为 0 也发日报
    （管理者能看到昨日 PR 流速）；全 0 才跳过。
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
        # approved 待 merge：reviewDecision='APPROVED' 且仍未合入/未关闭
        approved_stmt = select(CrashPullRequest).where(
            CrashPullRequest.merged_at.is_(None),
            CrashPullRequest.closed_at.is_(None),
            CrashPullRequest.review_decision == "APPROVED",
        )
        approved_rows = (await session.execute(approved_stmt)).scalars().all()
        stats = await _collect_yesterday_stats(session)

    total_pending = len(rows)
    total_approved = len(approved_rows)
    activity_yesterday = (
        stats["yesterday_merged"] + stats["yesterday_closed"] + stats["yesterday_created"]
    )
    if total_pending == 0 and total_approved == 0 and activity_yesterday == 0:
        logger.info("pr_pending_review_alert: 0 pending + 0 approved + 0 yesterday activity, skip")
        return {
            "pending_count": 0, "approved_count": 0,
            **stats,
            "sent": False, "skip_reason": "no_pending_no_activity",
        }

    def _row_to_dict(r) -> Dict:
        try:
            revs = json.loads(r.reviewer_emails or "[]")
        except (json.JSONDecodeError, TypeError):
            revs = []
        return {
            "pr_url": r.pr_url or "",
            "pr_number": r.pr_number,
            "repo": r.repo or "unknown",
            "pr_status": r.pr_status or "",
            "reviewer_emails": revs,
            "age_days": _age_days(r.created_at) if r.created_at else 0,
        }

    prs = [_row_to_dict(r) for r in rows]
    approved_prs = [_row_to_dict(r) for r in approved_rows]

    card = build_pending_review_card(
        prs,
        stats={
            **stats,
            "total_pending": total_pending,
            "total_approved": total_approved,
        },
        frontend_base_url=getattr(s, "frontend_base_url", "") or "",
        approved_prs=approved_prs,
    )

    from app.services import feishu_cli
    try:
        ok = await feishu_cli.send_interactive_card(email=target_email, card=card)
    except Exception as e:
        logger.exception("send_interactive_card failed: %s", e)
        return {
            "pending_count": total_pending, "approved_count": total_approved, **stats,
            "sent": False, "skip_reason": f"send_error:{e}",
        }

    if not ok:
        return {
            "pending_count": total_pending, "approved_count": total_approved, **stats,
            "sent": False, "skip_reason": "send_failed",
        }

    logger.info(
        "pr_pending_review_alert sent: pending=%d approved=%d y_merged=%d y_closed=%d y_created=%d target=%s",
        total_pending, total_approved,
        stats["yesterday_merged"], stats["yesterday_closed"],
        stats["yesterday_created"], target_email,
    )
    return {
        "pending_count": total_pending, "approved_count": total_approved,
        **stats, "sent": True,
    }
