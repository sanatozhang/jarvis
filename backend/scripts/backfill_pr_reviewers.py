"""存量回填：给当初没拿到 reviewer 的 crashguard PR 补指派 GitHub reviewer。

背景：48 条自动 PR 里 16 条 reason=bot_only —— 真实作者用个人/构建机 commit
邮箱（492934747@qq.com / root@kaaaaai.cn），被 @plaud.ai 域名白名单全削光，
旧逻辑下飞书没发、GitHub 也整批不指派。`github_candidate_emails` 解耦上线后
对**未来** PR 生效，但库里这批存量不会自动回填（daily_sweep 对 reviewer_emails
空的 PR 直接跳过），故有此一次性脚本。

安全约束：
- **默认 dry-run**，只读预览（gh pr diff / git blame / gh search-commits 均只读）；
  `--execute` 才真正 add-reviewer + 写 DB。
- 只挑「reviewer_emails 空（无飞书 assignee）且仍 open/draft 未 review」的 PR，
  绝不碰 reason=ok（已有 assignee）的 —— 否则重跑会重发飞书卡骚扰已通知的人。
- execute 走 resolve_and_notify(skip_fallback=True)：bot_only 时不发飞书兜底，
  只做 GitHub 指派 + 落 DB。

用法（容器内）：
    python -m scripts.backfill_pr_reviewers            # dry-run，打印计划
    python -m scripts.backfill_pr_reviewers --execute  # 真正回填
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger("crashguard.backfill_pr_reviewers")


def _emails_is_empty(raw: Any) -> bool:
    """reviewer_emails 视为「无 assignee」：None / 空串 / '[]' / 解析后空数组。"""
    s = (raw or "").strip()
    if not s or s == "[]":
        return True
    try:
        return not json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return True


async def _select_backfill_target_ids(session) -> List[int]:
    """选出需要回填的 PR id：无飞书 assignee + 仍 open/draft + 未 review。"""
    from app.crashguard.models import CrashPullRequest
    from sqlalchemy import select

    stmt = select(CrashPullRequest).where(
        CrashPullRequest.reviewed_at.is_(None),
        CrashPullRequest.pr_status.in_(("draft", "open")),
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [pr.id for pr in rows if _emails_is_empty(pr.reviewer_emails)]


def _preview_pr(pr, settings) -> Dict[str, Any]:
    """只读预览一条 PR：blame → github_candidate_emails → 反查 would-assign logins。"""
    from app.crashguard.services import pr_reviewer

    repo_path = pr_reviewer._resolve_repo_path_for_pr(pr, settings)
    resolution = pr_reviewer.resolve_reviewers_by_blame(pr.pr_url or "", repo_path, settings)
    candidates = list(resolution.github_candidate_emails or [])

    repo_slug, _ = pr_reviewer._parse_repo_slug_and_pr_number(pr.pr_url or "")
    logins: List[str] = []
    if repo_slug:
        for em in candidates:
            lg = pr_reviewer._resolve_email_to_github_login(em, repo_slug)
            if lg and lg not in logins:
                logins.append(lg)
    return {
        "pr_number": pr.pr_number,
        "repo": pr.repo,
        "pr_url": pr.pr_url,
        "reason": resolution.reason,
        "candidate_emails": candidates,
        "would_assign_logins": logins,
    }


async def run_backfill(execute: bool = False) -> Dict[str, Any]:
    """回填主入口。execute=False 只预览，True 才真正指派 + 写 DB。"""
    from app.crashguard.config import get_crashguard_settings
    from app.crashguard.services import pr_reviewer
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    settings = get_crashguard_settings()

    async with get_session() as session:
        target_ids = await _select_backfill_target_ids(session)

    preview: List[Dict[str, Any]] = []
    executed = 0
    assigned = 0

    if not execute:
        # dry-run：逐条只读预览，不写 DB / 不动 GitHub
        for pid in target_ids:
            async with get_session() as session:
                pr = await session.get(CrashPullRequest, pid)
            if pr is None:
                continue
            info = _preview_pr(pr, settings)
            preview.append(info)
            logger.info(
                "[dry-run] PR #%s %s reason=%s would_assign=%s (from %s)",
                info["pr_number"], info["repo"], info["reason"],
                info["would_assign_logins"], info["candidate_emails"],
            )
    else:
        for pid in target_ids:
            try:
                r = await pr_reviewer.resolve_and_notify(pid, skip_fallback=True)
                executed += 1
                if r.get("sent_count", 0) >= 0:  # resolve_and_notify 已落 DB
                    assigned += 1 if r.get("reason") in ("ok", "bot_only") else 0
                logger.info("[execute] PR id=%s -> %s", pid, r)
            except Exception as e:
                logger.exception("[execute] backfill failed pr id=%s: %s", pid, e)

    return {
        "targets": len(target_ids),
        "executed": executed,
        "preview": preview,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Backfill GitHub reviewers for legacy crashguard PRs")
    parser.add_argument("--execute", action="store_true",
                        help="真正指派 + 写 DB（默认只 dry-run 预览）")
    args = parser.parse_args()

    summary = asyncio.run(run_backfill(execute=args.execute))

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"\n=== backfill {mode}: {summary['targets']} target PR(s) ===")
    if not args.execute:
        for p in summary["preview"]:
            print(f"  PR #{p['pr_number']:<6} {p['repo']:<22} reason={p['reason']:<10} "
                  f"-> {p['would_assign_logins'] or '(none — 无可解析 GH 真人)'}  "
                  f"candidates={p['candidate_emails']}")
        print("\n(dry-run，未做任何写操作；确认无误后加 --execute 真正回填)")
    else:
        print(f"  executed={summary['executed']}（详见日志）")


if __name__ == "__main__":
    main()
