"""
Crashguard PR Reviewer 自动指派

PR 创建后通过 git blame 定位"原作者"作为推荐 reviewer，飞书私聊（email 直发）。
找不到 owner 时 fallback 给 settings.pr_reviewer_fallback_email（默认 sanato）。
未 review 的 PR 每日 09:30 cron 滚动提醒，review/merged/closed 即停。

隔离合约：仅引用 app.services.feishu_cli / app.db.database / 模块内部符号。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("crashguard.pr_reviewer")


# ============================================================
# 数据结构
# ============================================================
@dataclass
class ReviewerResolution:
    emails: List[str] = field(default_factory=list)
    line_counts: Dict[str, int] = field(default_factory=dict)
    # ok / pr_url_missing / diff_empty / blame_empty / repo_missing / bot_only
    reason: str = ""


# ============================================================
# Pure helpers — diff & blame 解析
# ============================================================
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FILE_OLD_RE = re.compile(r"^--- a/(.+)$")


def parse_diff_target_lines(diff_text: str) -> Dict[str, List[int]]:
    """
    解析 unified diff，返回 {old_file_path: [old_line_numbers]}。

    我们 blame **被删除/修改前的行**（"- " 行），因为 blame 是基于 HEAD 上的
    某一行判断"这行原来是谁写的"。纯新增（只有 "+"）不前进 old_line，无法 blame。
    """
    result: Dict[str, List[int]] = {}
    current_file: Optional[str] = None
    old_line = 0
    for line in diff_text.splitlines():
        m_file = _FILE_OLD_RE.match(line)
        if m_file:
            current_file = m_file.group(1)
            result.setdefault(current_file, [])
            continue
        m_hunk = _HUNK_RE.match(line)
        if m_hunk:
            old_line = int(m_hunk.group(1))
            continue
        if current_file is None:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("-"):
            result[current_file].append(old_line)
            old_line += 1
        elif line.startswith("+"):
            # 纯新增，不前进 old_line
            continue
        elif line.startswith(" ") or line == "":
            old_line += 1
    return {f: lns for f, lns in result.items() if lns}


def parse_blame_author_email(porcelain: str) -> str:
    """从 git blame --porcelain 输出中解析 author-mail（去除 <>）。"""
    for line in porcelain.splitlines():
        if line.startswith("author-mail "):
            raw = line[len("author-mail "):].strip()
            return raw.strip("<>").strip()
    return ""


# ============================================================
# 主流程 — 远端拉 diff + blame 聚合
# ============================================================
def fetch_pr_diff_via_gh(pr_url: str, timeout: int = 30) -> str:
    """gh pr diff <url> 远端拉 unified diff，失败返回空串。"""
    if not pr_url:
        return ""
    try:
        r = subprocess.run(
            ["gh", "pr", "diff", pr_url],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("gh pr diff exception url=%s: %s", pr_url, e)
        return ""
    if r.returncode != 0:
        logger.warning("gh pr diff failed: rc=%d url=%s err=%s",
                       r.returncode, pr_url, (r.stderr or "")[:200])
        return ""
    return r.stdout or ""


def _filter_authors(
    counter: Counter,
    blocked: List[str],
    top_n: int,
    min_lines_pct: float,
) -> List[Tuple[str, int]]:
    """过滤 blocked author + 软占比阈值；按行数降序返回前 top_n。

    软门控：min_lines_pct 先做一轮"主推荐"筛选；若主推荐 < top_n，
    则从被门控掉的剩余 non-blocked author 里按行数降序补足 top_n。
    即"必须挑够 top_n 人"，min_lines_pct 只决定排序优先级而非硬上限。
    """
    blocked_set = {b.lower().strip() for b in blocked}
    filtered = Counter({
        e: n for e, n in counter.items() if e.lower().strip() not in blocked_set
    })
    total = sum(filtered.values())
    if total == 0:
        return []
    sorted_authors = sorted(filtered.items(), key=lambda kv: (-kv[1], kv[0]))
    primary: List[Tuple[str, int]] = [
        (e, n) for e, n in sorted_authors if n / total >= min_lines_pct
    ]
    if len(primary) < top_n:
        primary_set = {e for e, _ in primary}
        for email, n in sorted_authors:
            if email in primary_set:
                continue
            primary.append((email, n))
            if len(primary) >= top_n:
                break
    return primary[:top_n]


def resolve_reviewers_by_blame(
    pr_url: str,
    repo_path: str,
    settings,
) -> ReviewerResolution:
    """
    主入口：gh pr diff 拉远端 → 解析改动文件/行 → git blame → 过滤排序。

    repo_path: 本地 clone 的目标仓库路径（含 HEAD blame 所需 commit）
    settings:  crashguard Settings（含 pr_reviewer_* 字段）
    """
    if not pr_url:
        return ReviewerResolution(reason="pr_url_missing")

    diff_text = fetch_pr_diff_via_gh(pr_url)
    if not diff_text:
        return ReviewerResolution(reason="diff_empty")

    targets = parse_diff_target_lines(diff_text)
    if not targets:
        return ReviewerResolution(reason="blame_empty")

    if not repo_path or not Path(repo_path).exists():
        logger.warning("repo_path missing for blame: %s", repo_path)
        return ReviewerResolution(reason="repo_missing")

    counter: Counter = Counter()
    for fpath, lines in targets.items():
        for ln in lines:
            try:
                r = subprocess.run(
                    ["git", "blame", "-L", f"{ln},{ln}", "--porcelain", "HEAD", "--", fpath],
                    cwd=repo_path, capture_output=True, text=True, timeout=10,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
                logger.debug("blame timeout/err %s:%d: %s", fpath, ln, e)
                continue
            if r.returncode != 0:
                continue
            email = parse_blame_author_email(r.stdout)
            if email:
                counter[email] += 1

    filtered = _filter_authors(
        counter,
        list(settings.pr_reviewer_blocked_authors or []),
        int(settings.pr_reviewer_top_n or 2),
        float(settings.pr_reviewer_min_lines_pct or 0.20),
    )
    if not filtered:
        return ReviewerResolution(reason="bot_only")

    return ReviewerResolution(
        emails=[e for e, _ in filtered],
        line_counts={e: n for e, n in filtered},
        reason="ok",
    )


# ============================================================
# 飞书卡片 builder + 通知
# ============================================================
_FALLBACK_REASON_ZH = {
    "pr_url_missing": "PR URL 缺失",
    "diff_empty": "无法获取 diff",
    "blame_empty": "diff 解析后无可 blame 行",
    "repo_missing": "本地仓库路径缺失",
    "bot_only": "blame 结果全部为 bot author",
    "all_unresolved": "找到 author 但飞书账号无法解析",
}


def build_reviewer_card(
    pr_url: str,
    pr_title: str,
    crash_title: str,
    crash_url: str,
    line_count: int,
    total_lines: int,
) -> dict:
    """飞书 interactive card：请你 review crashguard 自动 PR。"""
    pct = int(line_count * 100 / max(total_lines, 1))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🔍 请你 review crashguard 自动 PR"},
            "template": "blue",
        },
        "elements": [
            {"tag": "div", "text": {
                "tag": "lark_md",
                "content": (
                    f"**PR**: {pr_title}\n"
                    f"**触发崩溃**: {crash_title}\n"
                    f"**你被选中的原因**: 你贡献了被修改代码的 {line_count} 行"
                    f"（占总改动 {pct}%）"
                ),
            }},
            {"tag": "action", "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "打开 PR"},
                    "url": pr_url,
                    "type": "primary",
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看崩溃详情"},
                    "url": crash_url,
                    "type": "default",
                },
            ]},
        ],
    }


def build_fallback_card(
    pr_url: str,
    pr_title: str,
    reason: str,
    unresolved_emails: Optional[List[str]] = None,
) -> dict:
    """兜底卡片：发给 sanato，告知需手动指派。"""
    reason_zh = _FALLBACK_REASON_ZH.get(reason, reason)
    extra = ""
    if unresolved_emails:
        extra = "\n**未解析 author**: " + ", ".join(unresolved_emails)
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": "⚠️ Crashguard PR 需手动指派 reviewer"},
            "template": "orange",
        },
        "elements": [
            {"tag": "div", "text": {
                "tag": "lark_md",
                "content": (
                    f"**PR**: {pr_title}\n"
                    f"**兜底原因**: {reason_zh}"
                    f"{extra}"
                ),
            }},
            {"tag": "action", "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打开 PR 手动指派"},
                "url": pr_url,
                "type": "primary",
            }]},
        ],
    }


def _build_crash_url(datadog_issue_id: str) -> str:
    if not datadog_issue_id:
        return ""
    return f"https://app.datadoghq.com/error-tracking/issue/{datadog_issue_id}"


def _pr_display_title(pr) -> str:
    return f"[crashguard][DRAFT] {pr.repo or 'unknown'} #{pr.pr_number or '?'}"


async def notify_reviewers(
    pr,  # CrashPullRequest ORM 实例 OR MagicMock（含 pr_url/pr_number/repo/datadog_issue_id）
    resolution: ReviewerResolution,
    settings,
) -> Tuple[List[str], str]:
    """
    依据 resolution 决定发给谁。返回 (sent_emails, fallback_reason_or_empty)。

    - resolution.reason == "ok" + 至少一个 email 发送成功 → 不 fallback
    - resolution.reason == "ok" + 全部失败 → fallback (reason="all_unresolved")
    - resolution.reason != "ok" → fallback (reason=原 reason)

    飞书 send_interactive_card(email=...) 用 email 直发：飞书 API 会自动把
    email 解析为 open_id（前提：用户飞书绑定了该 email），无需我们维护映射。
    """
    from app.services import feishu_cli  # 隔离合约白名单

    pr_title = _pr_display_title(pr)
    crash_url = _build_crash_url(getattr(pr, "datadog_issue_id", "") or "")
    crash_title = f"issue {getattr(pr, 'datadog_issue_id', '') or 'unknown'}"
    fallback_email = (settings.pr_reviewer_fallback_email or "").strip()

    if resolution.reason == "ok":
        total = sum(resolution.line_counts.values())
        sent: List[str] = []
        for email in resolution.emails:
            n = resolution.line_counts.get(email, 0)
            card = build_reviewer_card(
                pr_url=pr.pr_url,
                pr_title=pr_title,
                crash_title=crash_title,
                crash_url=crash_url,
                line_count=n,
                total_lines=total,
            )
            try:
                ok = await feishu_cli.send_interactive_card(email=email, card=card)
            except Exception as e:
                logger.warning("send_interactive_card raised pr=%s email=%s: %s",
                               pr.pr_url, email, e)
                ok = False
            if ok:
                sent.append(email)
                logger.info("reviewer notified pr=%s email=%s lines=%d",
                            pr.pr_url, email, n)
            else:
                logger.warning("reviewer notify failed pr=%s email=%s",
                               pr.pr_url, email)

        if sent:
            return sent, ""

        # 全部发送失败 → fallback
        await _send_fallback(
            pr_url=pr.pr_url, pr_title=pr_title,
            reason="all_unresolved",
            unresolved_emails=resolution.emails,
            fallback_email=fallback_email,
        )
        return [], "all_unresolved"

    # 非 ok reason → 直接 fallback
    await _send_fallback(
        pr_url=pr.pr_url, pr_title=pr_title,
        reason=resolution.reason,
        unresolved_emails=None,
        fallback_email=fallback_email,
    )
    return [], resolution.reason


async def _send_fallback(
    pr_url: str,
    pr_title: str,
    reason: str,
    unresolved_emails: Optional[List[str]],
    fallback_email: str,
) -> None:
    from app.services import feishu_cli
    if not fallback_email:
        logger.error("pr_reviewer_fallback_email empty — cannot send fallback (pr=%s)", pr_url)
        return
    card = build_fallback_card(pr_url, pr_title, reason, unresolved_emails)
    try:
        await feishu_cli.send_interactive_card(email=fallback_email, card=card)
        logger.info("fallback sent to %s for pr=%s reason=%s",
                    fallback_email, pr_url, reason)
    except Exception as e:
        logger.error("fallback send failed pr=%s: %s", pr_url, e)


# ============================================================
# GitHub review 状态检测
# ============================================================
def check_review_status_from_gh(pr_url: str, timeout: int = 20) -> bool:
    """True 表示该 PR 已 review / merged / closed，应停止提醒。"""
    if not pr_url:
        return False
    try:
        r = subprocess.run(
            ["gh", "pr", "view", pr_url,
             "--json", "state,mergedAt,closedAt,reviews"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("check_review_status exception url=%s: %s", pr_url, e)
        return False
    if r.returncode != 0:
        return False
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return False
    if data.get("state") in ("MERGED", "CLOSED"):
        return True
    if data.get("mergedAt") or data.get("closedAt"):
        return True
    reviews = data.get("reviews") or []
    return len(reviews) > 0


# ============================================================
# Orchestrator — 单次入口
# ============================================================
def _extract_flutter_sub_from_url(pr_url: str) -> str:
    """从 GitHub PR URL 解析 flutter 子仓 hint。

    历史包袱：CrashPullRequest.repo 字段对 flutter 三仓统一存 'flutter'，丢失
    common/global/cn 信息。但 pr_url 形如 'plaud-flutter-{sub}'，能恢复。
      plaud-flutter-common → ""（common 即默认主仓，sub_hint 为空即可）
      plaud-flutter-global → "global"
      plaud-flutter-cn     → "cn"
    """
    if not pr_url:
        return ""
    u = pr_url.lower()
    if "plaud-flutter-global" in u or "plaud_flutter_global" in u:
        return "global"
    if "plaud-flutter-cn" in u or "plaud_flutter_cn" in u:
        return "cn"
    return ""


def _resolve_repo_path_for_pr(pr, settings) -> str:
    """根据 pr.repo + pr.pr_url 映射本地仓库路径。

    flutter 子仓 sub_hint 优先从 pr_url 解析（pr.repo 只存 'flutter' 不带 sub）。
    """
    repo = (pr.repo or "").lower()
    if repo.startswith("plaud-flutter-") or repo == "flutter":
        # 优先用 pr_url 解析子仓（pr.repo='flutter' 时唯一可靠来源）
        sub_hint = _extract_flutter_sub_from_url(getattr(pr, "pr_url", "") or "")
        # pr.repo 自身带 sub 时也尊重（向后兼容未来若改字段）
        if not sub_hint and repo.startswith("plaud-flutter-"):
            tail = repo[len("plaud-flutter-"):]
            if tail in ("global", "cn"):
                sub_hint = tail
        try:
            from app.crashguard.services.pr_drafter import _platform_repo_path
            return _platform_repo_path("flutter", sub_hint)
        except Exception as e:
            logger.warning("_platform_repo_path failed: %s", e)
            return getattr(settings, "repo_path_flutter", "") or ""
    if repo.startswith("plaud-android") or repo in ("android", "plaud_android"):
        return getattr(settings, "repo_path_android", "") or ""
    if repo.startswith("plaud-ios") or repo in ("ios", "plaud_ios"):
        return getattr(settings, "repo_path_ios", "") or ""
    return ""


async def resolve_and_notify(pr_id: int) -> Dict:
    """
    单次入口：对一条 PR 做 blame → 通知 → 写回 DB。
    返回 {"sent_count": N, "fallback": bool, "reason": str}
    """
    from app.crashguard.config import get_crashguard_settings
    from app.db.database import get_session
    from app.crashguard.models import CrashPullRequest  # 延迟 import 避免循环

    s = get_crashguard_settings()
    if not s.pr_reviewer_enabled:
        return {"sent_count": 0, "fallback": False, "reason": "disabled"}

    async with get_session() as session:
        pr = await session.get(CrashPullRequest, pr_id)
        if pr is None:
            return {"sent_count": 0, "fallback": False, "reason": "pr_not_found"}

        if pr.reviewed_at is not None:
            return {"sent_count": 0, "fallback": False, "reason": "already_reviewed"}

        # 1. blame
        repo_path = _resolve_repo_path_for_pr(pr, s)
        resolution = resolve_reviewers_by_blame(pr.pr_url, repo_path, s)

        # 2. notify
        sent, fallback_reason = await notify_reviewers(pr, resolution, s)

        # 3. 写回 DB
        now = datetime.utcnow()
        pr.reviewer_emails = json.dumps(resolution.emails, ensure_ascii=False)
        pr.reviewer_open_ids = json.dumps(sent, ensure_ascii=False)
        pr.reviewer_fallback_reason = fallback_reason or resolution.reason or "ok"
        pr.last_reminder_at = now
        if pr.reviewer_assigned_at is None:
            pr.reviewer_assigned_at = now
        await session.commit()

    return {
        "sent_count": len(sent),
        "fallback": bool(fallback_reason),
        "reason": fallback_reason or resolution.reason,
    }


# ============================================================
# 每日提醒 cron 入口
# ============================================================
async def daily_reminder_sweep() -> Dict:
    """
    扫描所有未 reviewed 的 PR：
      - 已 reviewed/merged/closed → 写 reviewed_at，跳过
      - 当天已提醒过 → 跳过
      - 其余 → 重跑 resolve_and_notify
    """
    from app.crashguard.config import get_crashguard_settings
    from app.db.database import get_session
    from app.crashguard.models import CrashPullRequest
    from sqlalchemy import select

    s = get_crashguard_settings()
    if not s.pr_reviewer_enabled:
        return {
            "processed": 0, "skipped_same_day": 0,
            "newly_reviewed": 0, "notified": 0,
        }

    today = datetime.utcnow().date()
    processed = skipped_same_day = newly_reviewed = notified = 0
    pr_ids_to_notify: List[int] = []

    async with get_session() as session:
        stmt = select(CrashPullRequest).where(
            CrashPullRequest.reviewed_at.is_(None),
            CrashPullRequest.pr_status.in_(("draft", "open")),
        )
        rows = (await session.execute(stmt)).scalars().all()

        for pr in rows:
            processed += 1

            # 同日去重
            if pr.last_reminder_at and pr.last_reminder_at.date() == today:
                skipped_same_day += 1
                continue

            # 拉 GH 现态：已 review → 标记跳过
            if check_review_status_from_gh(pr.pr_url):
                pr.reviewed_at = datetime.utcnow()
                newly_reviewed += 1
                continue

            pr_ids_to_notify.append(pr.id)

        await session.commit()

    # session 外 await resolve_and_notify（其内部自己开 session，避免嵌套）
    for pid in pr_ids_to_notify:
        try:
            r = await resolve_and_notify(pid)
            if r.get("sent_count", 0) > 0 or r.get("fallback"):
                notified += 1
        except Exception as e:
            logger.exception("daily_sweep notify failed pr=%d: %s", pid, e)

    logger.info(
        "pr_reviewer daily_sweep: processed=%d skipped_same_day=%d "
        "newly_reviewed=%d notified=%d",
        processed, skipped_same_day, newly_reviewed, notified,
    )
    return {
        "processed": processed,
        "skipped_same_day": skipped_same_day,
        "newly_reviewed": newly_reviewed,
        "notified": notified,
    }
