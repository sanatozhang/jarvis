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

from app.crashguard.services.version_util import GEN_BADGE, classify_generation

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


async def _build_generation_lookup(session, issue_ids: List[str]) -> Dict[str, str]:
    """批量反查 CrashIssue.service/last_seen_version，分类每个 issue_id 的代际。

    CrashPullRequest 本身不存 service/version（只有 repo_router 的 logical_name），
    要判代际必须反查 CrashIssue。空 issue_ids 直接返回空 dict（避免空 IN() 查询）。
    """
    from app.crashguard.models import CrashIssue
    from sqlalchemy import select

    ids = [i for i in set(issue_ids) if i]
    if not ids:
        return {}
    stmt = select(
        CrashIssue.datadog_issue_id, CrashIssue.service, CrashIssue.last_seen_version,
    ).where(CrashIssue.datadog_issue_id.in_(ids))
    rows = (await session.execute(stmt)).all()
    return {
        iid: classify_generation(svc or "", ver or "")
        for iid, svc, ver in rows
    }


def build_pending_review_card(
    prs: List[Dict],
    stats: Dict[str, int] = None,
    frontend_base_url: str = "",
    approved_prs: List[Dict] = None,
    yesterday_merged_prs: List[Dict] = None,
    yesterday_closed_prs: List[Dict] = None,
    yesterday_created_prs: List[Dict] = None,
) -> Dict:
    """构造飞书 interactive card：昨日交付 stats + 4 个 PR 清单（merged/closed/新建/approved）+ 当前积压清单。

    prs: 当前 pending 清单（reviewed_at IS NULL）
    approved_prs: 已 approve 待 merge 清单（卡最后一公里）
    yesterday_merged_prs: 昨日已 merged 清单
    yesterday_closed_prs: 昨日已 closed (未合) 清单
    yesterday_created_prs: 昨日新建清单
    stats: 计数 dict，向后兼容
    frontend_base_url: "完整 PR 列表"链接，空字符串则不渲染
    """
    approved_prs = approved_prs or []
    yesterday_merged_prs = yesterday_merged_prs or []
    yesterday_closed_prs = yesterday_closed_prs or []
    yesterday_created_prs = yesterday_created_prs or []
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

    def _render_pr_section(title: str, prs_in: List[Dict], emoji: str, suffix_fn,
                            always_show: bool = True, empty_hint: str = "（昨日无）") -> None:
        """渲染一个 PR 清单小节：分隔线 + 标题 + 按 repo 分组 + 每个 PR 一行带链接。

        always_show=True：即便 prs_in 为空也渲染小节 + empty_hint 占位（保持结构对称）。
        always_show=False：空时不渲染该小节。
        """
        if not prs_in and not always_show:
            return
        blocks.append({"tag": "hr"})
        blocks.append({"tag": "div", "text": {
            "tag": "lark_md",
            "content": f"**{title}（{len(prs_in)} 条）**",
        }})
        if not prs_in:
            blocks.append({"tag": "div", "text": {
                "tag": "lark_md",
                "content": f"_{empty_hint}_",
            }})
            return
        by_repo_local: Dict[str, List[Dict]] = {}
        for p in prs_in:
            by_repo_local.setdefault(p.get("repo") or "unknown", []).append(p)
        for r in sorted(by_repo_local.keys()):
            repo_prs = sorted(
                by_repo_local[r],
                key=lambda x: (
                    0 if x.get("generation") == "native" else 1,
                    -x.get("age_days", 0),
                ),
            )
            lines = [f"\n**📦 {r} ({len(repo_prs)} 条)**"]
            for p in repo_prs:
                gb = GEN_BADGE.get(p.get("generation", ""), "")
                gb_str = f" {gb}" if gb else ""
                lines.append(
                    f"{emoji} [#{p.get('pr_number')}]({p.get('pr_url')}){gb_str} · {suffix_fn(p)}"
                )
            blocks.append({"tag": "div", "text": {
                "tag": "lark_md",
                "content": "\n".join(lines),
            }})

    # 「✅ 昨日 merged」清单 — 即便 0 也展示，对称用户期望
    _render_pr_section(
        title="✅ 昨日 merged",
        prs_in=yesterday_merged_prs,
        emoji="✅",
        suffix_fn=lambda p: f"{p.get('repo','')} merged",
        always_show=True,
    )

    # 「❌ 昨日 closed 未合」清单 — 即便 0 也展示
    _render_pr_section(
        title="❌ 昨日 closed（未合）",
        prs_in=yesterday_closed_prs,
        emoji="❌",
        suffix_fn=lambda p: f"{p.get('repo','')} closed",
        always_show=True,
    )

    # 「🆕 昨日新建」清单 — 即便 0 也展示
    _render_pr_section(
        title="🆕 昨日新建",
        prs_in=yesterday_created_prs,
        emoji="🆕",
        suffix_fn=lambda p: f"{p.get('repo','')} created",
        always_show=True,
    )

    # 「🟢 已 approve 待 merge」清单 — 即便 0 也展示
    _render_pr_section(
        title="🟢 已 approve 待 merge —— PR 作者请尽快合入",
        prs_in=approved_prs,
        emoji="🟢",
        suffix_fn=lambda p: (
            f"{(p.get('age_days') or 0)}天" if (p.get('age_days') or 0) > 0 else "今天"
        ) + " · approved",
        always_show=True,
        empty_hint="（暂无 approved 待合的 PR）",
    )

    blocks.append({"tag": "hr"})
    blocks.append({"tag": "div", "text": {
        "tag": "lark_md",
        "content": f"**📋 当前积压（{n} 条等 review）**——按仓库分组：",
    }})

    for repo in sorted(by_repo.keys()):
        repo_prs = sorted(
            by_repo[repo],
            key=lambda x: (
                0 if x.get("generation") == "native" else 1,
                -x.get("age_days", 0),
            ),
        )
        lines = [f"\n**📦 {repo} ({len(repo_prs)} 条)**"]
        for p in repo_prs:
            revs = p.get("reviewer_emails") or []
            rev_short = ", ".join(e.split("@")[0] for e in revs[:2]) if revs else "(未指派)"
            if len(revs) > 2:
                rev_short += f" +{len(revs)-2}"
            age = p.get("age_days", 0)
            age_str = f"{age}天" if age > 0 else "今天"
            status_emoji = "📝" if p.get("pr_status") == "draft" else "🔵"
            gb = GEN_BADGE.get(p.get("generation", ""), "")
            gb_str = f" {gb}" if gb else ""
            lines.append(
                f"{status_emoji} [#{p.get('pr_number')}]({p.get('pr_url')}){gb_str} "
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

    保持函数名 + 返回 dict 计数键，向后兼容单测。
    """
    breakdown = await _collect_yesterday_breakdown(session)
    return {
        "yesterday_merged": len(breakdown["merged"]),
        "yesterday_closed": len(breakdown["closed"]),
        "yesterday_created": len(breakdown["created"]),
    }


async def _collect_yesterday_breakdown(session) -> Dict[str, list]:
    """拉昨日（北京）完整 24h 的 merged / closed / created PR **实际行**。

    返回 {"merged": [CrashPullRequest, ...], "closed": [...], "created": [...]}
    closed 排除 merged 防重复。供日报渲染具体 PR 清单 + 链接。
    """
    from app.crashguard.models import CrashPullRequest
    from sqlalchemy import select, and_

    start_utc, end_utc = _yesterday_utc_window()

    merged_q = select(CrashPullRequest).where(
        and_(CrashPullRequest.merged_at >= start_utc,
             CrashPullRequest.merged_at < end_utc)
    )
    closed_q = select(CrashPullRequest).where(
        and_(CrashPullRequest.closed_at >= start_utc,
             CrashPullRequest.closed_at < end_utc,
             CrashPullRequest.merged_at.is_(None))
    )
    created_q = select(CrashPullRequest).where(
        and_(CrashPullRequest.created_at >= start_utc,
             CrashPullRequest.created_at < end_utc)
    )
    merged_rows = (await session.execute(merged_q)).scalars().all()
    closed_rows = (await session.execute(closed_q)).scalars().all()
    created_rows = (await session.execute(created_q)).scalars().all()
    return {
        "merged": list(merged_rows),
        "closed": list(closed_rows),
        "created": list(created_rows),
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
        # 昨日实际 merged / closed / created PR 行（供清单渲染）
        breakdown = await _collect_yesterday_breakdown(session)
        stats = {
            "yesterday_merged": len(breakdown["merged"]),
            "yesterday_closed": len(breakdown["closed"]),
            "yesterday_created": len(breakdown["created"]),
        }
        # 反查代际（4.0 native / 3.x flutter），供卡片角标 + 排序用
        all_issue_ids = (
            [r.datadog_issue_id for r in rows]
            + [r.datadog_issue_id for r in approved_rows]
            + [r.datadog_issue_id for r in breakdown["merged"]]
            + [r.datadog_issue_id for r in breakdown["closed"]]
            + [r.datadog_issue_id for r in breakdown["created"]]
        )
        gen_map = await _build_generation_lookup(session, all_issue_ids)

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

    def _row_to_dict(r, generation: str = "") -> Dict:
        # 优先用 GitHub 实际 reviewer（pr_sync 回写的 gh_reviewers），它覆盖手动/自动/
        # 兜底加的所有 reviewer；为空再退回 app blame 流程写的 reviewer_emails。
        revs = []
        try:
            revs = json.loads(getattr(r, "gh_reviewers", None) or "[]")
        except (json.JSONDecodeError, TypeError):
            revs = []
        if not revs:
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
            "generation": generation,
        }

    prs = [_row_to_dict(r, gen_map.get(r.datadog_issue_id, "")) for r in rows]
    approved_prs = [_row_to_dict(r, gen_map.get(r.datadog_issue_id, "")) for r in approved_rows]
    yesterday_merged_prs = [_row_to_dict(r, gen_map.get(r.datadog_issue_id, "")) for r in breakdown["merged"]]
    yesterday_closed_prs = [_row_to_dict(r, gen_map.get(r.datadog_issue_id, "")) for r in breakdown["closed"]]
    yesterday_created_prs = [_row_to_dict(r, gen_map.get(r.datadog_issue_id, "")) for r in breakdown["created"]]

    card = build_pending_review_card(
        prs,
        stats={
            **stats,
            "total_pending": total_pending,
            "total_approved": total_approved,
        },
        frontend_base_url=getattr(s, "frontend_base_url", "") or "",
        approved_prs=approved_prs,
        yesterday_merged_prs=yesterday_merged_prs,
        yesterday_closed_prs=yesterday_closed_prs,
        yesterday_created_prs=yesterday_created_prs,
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
