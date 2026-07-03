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
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("crashguard.pr_reviewer")

# 进程级 email → GH login 缓存。key=(email_lower, repo_slug_lower)，
# value=login str（命中）/ None（负缓存：找不到对应 GH 用户）。
# Plaud 员工映射稳定，缓存命中率高 → 大幅减少 GH API 调用。
_email_to_login_cache: Dict[Tuple[str, str], Optional[str]] = {}


# ============================================================
# 数据结构
# ============================================================
@dataclass
class ReviewerResolution:
    emails: List[str] = field(default_factory=list)
    line_counts: Dict[str, int] = field(default_factory=dict)
    # ok / pr_url_missing / diff_empty / blame_empty / repo_missing / bot_only
    reason: str = ""
    # GitHub add-reviewer 候选：blame 作者经 blocked + top_n 过滤但**不过**
    # @plaud.ai 域名白名单。域名白名单只约束「飞书按邮箱直发」（emails 字段），
    # 而 GitHub 指派走 commit email→GH login，与邮箱域名无关，所以单列一份。
    # 个人/构建机邮箱（492934747@qq.com / root@kaaaaai.cn）仍能进这里 → 经
    # commits search 反查到真人 login → add-reviewer；查不到 login 的自然落空。
    github_candidate_emails: List[str] = field(default_factory=list)


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
    allowed_domains: Optional[List[str]] = None,
) -> List[Tuple[str, int]]:
    """过滤 blocked author + 软占比阈值；按行数降序返回前 top_n。

    软门控：min_lines_pct 先做一轮"主推荐"筛选；若主推荐 < top_n，
    则从被门控掉的剩余 non-blocked author 里按行数降序补足 top_n。
    即"必须挑够 top_n 人"，min_lines_pct 只决定排序优先级而非硬上限。

    allowed_domains 非空时只保留域名匹配的 email（白名单先于黑名单）。
    用于剔除 @qq.com / 外部域名等无法对接飞书的历史 commit author。
    """
    if allowed_domains:
        domain_set = {d.lower().strip().lstrip("@") for d in allowed_domains if d}
        counter = Counter({
            e: n for e, n in counter.items()
            if "@" in e and e.rsplit("@", 1)[1].lower().strip() in domain_set
        })
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

    blocked = list(settings.pr_reviewer_blocked_authors or [])
    top_n = int(settings.pr_reviewer_top_n or 2)
    min_pct = float(settings.pr_reviewer_min_lines_pct or 0.20)

    # GitHub 候选：只过 blocked + top_n，不过域名白名单（add-reviewer 走 GH login）
    gh_filtered = _filter_authors(counter, blocked, top_n, min_pct, allowed_domains=None)
    github_candidate_emails = [e for e, _ in gh_filtered]

    # 飞书候选：额外叠加域名白名单（按邮箱直发，必须能对接飞书账号）
    filtered = _filter_authors(
        counter, blocked, top_n, min_pct,
        allowed_domains=list(getattr(settings, "pr_reviewer_allowed_email_domains", []) or []),
    )
    if not filtered:
        # 飞书无可发对象，但 GitHub 仍可凭真实作者 login 指派 → 候选单独带出
        return ReviewerResolution(
            reason="bot_only",
            github_candidate_emails=github_candidate_emails,
        )

    return ReviewerResolution(
        emails=[e for e, _ in filtered],
        line_counts={e: n for e, n in filtered},
        reason="ok",
        github_candidate_emails=github_candidate_emails,
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
    skip_fallback: bool = False,
) -> Tuple[List[str], str]:
    """
    依据 resolution 决定发给谁。返回 (sent_emails, fallback_reason_or_empty)。

    - resolution.reason == "ok" + 至少一个 email 发送成功 → 不 fallback
    - resolution.reason == "ok" + 全部失败 → fallback (reason="all_unresolved")
    - resolution.reason != "ok" → fallback (reason=原 reason)

    skip_fallback=True 时（daily sweep 模式）所有 fallback 路径都不发，
    返回 ([], reason)——只对明确 assignee 的 PR 才打扰人。

    飞书 send_interactive_card(email=...) 用 email 直发：飞书 API 会自动把
    email 解析为 open_id（前提：用户飞书绑定了该 email），无需我们维护映射。
    """
    from app.services import feishu_cli  # 隔离合约白名单

    pr_title = _pr_display_title(pr)
    crash_url = _build_crash_url(getattr(pr, "datadog_issue_id", "") or "")
    crash_title = f"issue {getattr(pr, 'datadog_issue_id', '') or 'unknown'}"
    fallback_email = (settings.pr_reviewer_fallback_email or "").strip()

    # skip_fallback 模式：无明确 assignee 直接返回，不打扰兜底人
    if skip_fallback and resolution.reason != "ok":
        return [], resolution.reason

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

        # skip_fallback 模式：全失败也不打扰兜底
        if skip_fallback:
            return [], "all_unresolved"

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
# GitHub 正式 reviewer 同步：email → GH login → gh pr edit --add-reviewer
# ============================================================
_PR_URL_RE = re.compile(r"https?://github\.com/([^/]+/[^/]+)/pull/(\d+)")


def _parse_repo_slug_and_pr_number(pr_url: str) -> Tuple[str, int]:
    """https://github.com/owner/repo/pull/123 → ('owner/repo', 123)。失败返回 ('', 0)。"""
    m = _PR_URL_RE.match(pr_url or "")
    if not m:
        return "", 0
    return m.group(1), int(m.group(2))


def _resolve_email_to_github_login(
    email: str, repo_slug: str, timeout: int = 15,
) -> Optional[str]:
    """通过 GH commits search API 反查 email 对应的 GitHub login。

    底层逻辑：crashguard 的 reviewer email 来自 git blame，必然在 repo
    commits 里出现过 → commits search 必然能找到匹配的 author.login。
    （实测命中率 4/4：aaron.luo/victor/chance/luffy 全部解析正确。）

    进程级 cache：员工 email→login 映射稳定，命中后直接返回；负缓存
    （找不到 GH user）也缓存，避免重复 API 调用。
    """
    if not email or not repo_slug or "/" not in repo_slug:
        return None
    key = (email.lower().strip(), repo_slug.lower())
    if key in _email_to_login_cache:
        return _email_to_login_cache[key]

    sub_env = dict(os.environ)
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        sub_env.pop(k, None)
    try:
        r = subprocess.run(
            ["gh", "api",
             f"search/commits?q=author-email:{email}+repo:{repo_slug}",
             "--jq", ".items[0].author.login // empty"],
            capture_output=True, text=True, timeout=timeout, env=sub_env,
        )
        if r.returncode != 0:
            logger.debug(
                "gh search/commits failed email=%s repo=%s: %s",
                email, repo_slug, (r.stderr or "")[:200],
            )
            _email_to_login_cache[key] = None
            return None
        login = (r.stdout or "").strip()
        if not login or login.lower() == "null":
            _email_to_login_cache[key] = None
            return None
        _email_to_login_cache[key] = login
        return login
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("email→login lookup exception email=%s: %s", email, e)
        return None


def _add_github_reviewers(
    pr_url: str, github_logins: List[str], timeout: int = 30,
) -> Tuple[List[str], List[str]]:
    """gh api POST .../requested_reviewers 加 GH 正式 reviewer。

    返回 (added, failed)。failed 含 GH 返回 422/403 的 login（非 collaborator
    / 已经是 reviewer / author 自己等情况都进 failed）。

    一次性 batch 加（一次 API call），失败时 fall back 单个加（隔离单条错误）。
    """
    if not pr_url or not github_logins:
        return [], []
    repo_slug, pr_number = _parse_repo_slug_and_pr_number(pr_url)
    if not repo_slug or pr_number <= 0:
        return [], list(github_logins)

    sub_env = dict(os.environ)
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        sub_env.pop(k, None)

    # 一次性 batch：用 -f 'reviewers[]=login' 多次表达数组
    args = ["gh", "api", "-X", "POST",
            f"repos/{repo_slug}/pulls/{pr_number}/requested_reviewers"]
    for lg in github_logins:
        args.extend(["-f", f"reviewers[]={lg}"])
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, env=sub_env,
        )
        if r.returncode == 0:
            logger.info(
                "gh add-reviewer ok pr=%s logins=%s",
                pr_url, github_logins,
            )
            return list(github_logins), []
        err = (r.stderr or "").strip()[:200]
        logger.warning(
            "gh batch add-reviewer failed pr=%s logins=%s err=%s; trying one-by-one",
            pr_url, github_logins, err,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("gh add-reviewer exception: %s", e)
        return [], list(github_logins)

    # Fallback：逐个加，隔离失败 login
    added: List[str] = []
    failed: List[str] = []
    for lg in github_logins:
        try:
            r1 = subprocess.run(
                ["gh", "api", "-X", "POST",
                 f"repos/{repo_slug}/pulls/{pr_number}/requested_reviewers",
                 "-f", f"reviewers[]={lg}"],
                capture_output=True, text=True, timeout=timeout, env=sub_env,
            )
            if r1.returncode == 0:
                added.append(lg)
            else:
                logger.info(
                    "gh add-reviewer single fail pr=%s login=%s: %s",
                    pr_url, lg, (r1.stderr or "").strip()[:150],
                )
                failed.append(lg)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning("gh add-reviewer single exception login=%s: %s", lg, e)
            failed.append(lg)
    return added, failed


def sync_github_reviewers_for_emails(
    pr_url: str, emails: List[str],
) -> Tuple[List[str], List[str], List[str]]:
    """高级入口：emails → resolve login → batch add-reviewer。

    返回 (resolved_logins, added, failed)：
      - resolved_logins：成功反查到 GH login 的子集
      - added：成功 add-reviewer 的 login
      - failed：解析失败的 email + add 失败的 login（合并）
    """
    repo_slug, _ = _parse_repo_slug_and_pr_number(pr_url)
    if not repo_slug:
        return [], [], list(emails)

    resolved: List[str] = []
    unresolved_emails: List[str] = []
    for em in emails:
        lg = _resolve_email_to_github_login(em, repo_slug)
        if lg:
            resolved.append(lg)
        else:
            unresolved_emails.append(em)

    if not resolved:
        return [], [], unresolved_emails
    added, add_failed = _add_github_reviewers(pr_url, resolved)
    return resolved, added, unresolved_emails + add_failed


# ============================================================
# GitHub review 状态检测
# ============================================================
# 已知 review-bot login（这些 author 的 review record 不算"真人 review"）
_REVIEW_BOT_LOGINS = {
    "claude",                          # claude.ai code review bot
    "copilot-pull-request-reviewer",   # GitHub Copilot
    "copilot",
    "dependabot",
    "dependabot[bot]",
    "renovate",
    "renovate[bot]",
    "github-actions",
    "github-actions[bot]",
    "coderabbitai",
    "coderabbitai[bot]",
}

# authorAssociation == "NONE" 通常是 bot 或外部贡献者；review 不算被 review
_VALID_REVIEW_ASSOCIATIONS = {"MEMBER", "OWNER", "COLLABORATOR", "CONTRIBUTOR"}


def _is_human_reviewer(review_obj: dict, pr_author_login: str) -> bool:
    """判定一条 reviews[i] 是否为"有效真人 review"。

    排除：
      - bot login（claude / copilot-pull-request-reviewer / dependabot / ...）
      - PR 作者自己（自己 comment 自己的 PR 不算被 review）
      - authorAssociation == NONE（外部 / 未关联用户，多为 bot）
    """
    author = (review_obj.get("author") or {}).get("login") or ""
    assoc = (review_obj.get("authorAssociation") or "").upper()
    if not author:
        return False
    if author.lower() in _REVIEW_BOT_LOGINS:
        return False
    if pr_author_login and author.lower() == pr_author_login.lower():
        return False
    if assoc not in _VALID_REVIEW_ASSOCIATIONS:
        return False
    return True


def check_review_status_from_gh(pr_url: str, timeout: int = 20) -> bool:
    """True 表示该 PR 已 review / merged / closed，应停止提醒。

    "已 review" 判定：至少 1 条 reviews record 满足 _is_human_reviewer。
    bot review、PR 作者自我 comment、authorAssociation=NONE 均不算。
    """
    if not pr_url:
        return False
    try:
        r = subprocess.run(
            ["gh", "pr", "view", pr_url,
             "--json", "state,mergedAt,closedAt,reviews,author"],
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
    pr_author_login = (data.get("author") or {}).get("login") or ""
    for rv in (data.get("reviews") or []):
        if _is_human_reviewer(rv, pr_author_login):
            return True
    return False


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
    # Native / desktop (and any future repo): resolve via repo_routing by logical sub-repo name.
    try:
        from app.config import get_repo_routing
        import os as _os
        for _plat, _cfg in (get_repo_routing() or {}).items():
            for _band in _cfg.get("bands", []):
                _sub = (_band.get("sub") or "").strip()
                _wrap = _os.path.expanduser(_band.get("wrapper", "") or "")
                _logical = _sub or _os.path.basename(_wrap.rstrip("/"))
                if _logical.lower() == repo and _wrap:
                    _path = _os.path.join(_wrap, _sub) if _sub else _wrap
                    if _os.path.exists(_path):
                        return _path
    except Exception as e:
        logger.warning("repo_routing lookup failed for pr.repo=%s: %s", repo, e)
    return ""


async def resolve_and_notify(pr_id: int, skip_fallback: bool = False) -> Dict:
    """
    单次入口：对一条 PR 做 blame → 通知 → 写回 DB。
    返回 {"sent_count": N, "fallback": bool, "reason": str}

    skip_fallback=True 时（daily sweep 模式）跳过所有 fallback 路径，
    不打扰兜底人；只对明确 assignee 才发卡。
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
        sent, fallback_reason = await notify_reviewers(
            pr, resolution, s, skip_fallback=skip_fallback,
        )

        # 2.5 GH 正式 reviewer 同步（fire-and-forget；不阻塞主流程）
        # 抓手：飞书私聊只是建议，reviewer 在 GH UI 上无 review-requested 标记，
        # 也看不到"我负责 review 的 PR 中心"。同步 add-reviewer 后：
        # - reviewer 在 GH 主页 /pulls 看到该 PR
        # - 在 PR 上有"Awaiting requested review"提示
        # 失败（非 collaborator / author 自己 / 已是 reviewer）graceful 静默。
        #
        # 用 github_candidate_emails（未过域名白名单）而非飞书 `sent`：很多真实
        # 作者用个人/构建机 commit 邮箱（@qq.com / kaaaaai.cn），被 @plaud.ai
        # 白名单削光后 sent 为空，过去导致 GitHub 也整批不指派（48 条 PR 里 16 条
        # bot_only 的根因）。GitHub 指派靠 commit email→GH login，与域名无关，
        # 故与飞书路径解耦：候选非空就同步，查不到 GH login 的邮箱自然落空。
        gh_synced_logins: List[str] = []
        gh_candidate_emails = list(resolution.github_candidate_emails or [])
        if getattr(s, "pr_reviewer_github_sync_enabled", True) and gh_candidate_emails:
            try:
                resolved, added, gh_failed = sync_github_reviewers_for_emails(
                    pr.pr_url or "", gh_candidate_emails,
                )
                gh_synced_logins = added
                if gh_failed:
                    logger.info(
                        "gh reviewer sync partial pr=%s added=%s failed=%s",
                        pr.pr_url, added, gh_failed,
                    )
            except Exception as e:
                logger.warning(
                    "gh reviewer sync exception pr=%s: %s", pr.pr_url, e,
                )

        # 2.6 兜底 GitHub reviewer：上面一个都没加上（blame 空 / 反查不到 login /
        # 作者非 collaborator）→ 把配置的兜底人加为 reviewer，避免 PR 无人 review 悬空。
        if getattr(s, "pr_reviewer_github_sync_enabled", True) and not gh_synced_logins:
            fb_emails = list(getattr(s, "pr_reviewer_fallback_github_emails", []) or [])
            if fb_emails:
                try:
                    _, fb_added, fb_failed = sync_github_reviewers_for_emails(
                        pr.pr_url or "", fb_emails,
                    )
                    if fb_added:
                        gh_synced_logins = fb_added
                        logger.info(
                            "gh reviewer fallback pr=%s added=%s (no blame owner)",
                            pr.pr_url, fb_added,
                        )
                    elif fb_failed:
                        logger.info(
                            "gh reviewer fallback all-failed pr=%s failed=%s",
                            pr.pr_url, fb_failed,
                        )
                except Exception as e:
                    logger.warning(
                        "gh reviewer fallback exception pr=%s: %s", pr.pr_url, e,
                    )

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

    skipped_no_assignee = 0
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

            # 必须有明确 assignee（首次 blame 命中 plaud.ai 邮箱并通知成功过）
            # reviewer_emails 字段为 "[]" / None / 空数组 都跳过，不打扰兜底人
            emails_raw = (pr.reviewer_emails or "").strip()
            if not emails_raw or emails_raw == "[]":
                skipped_no_assignee += 1
                continue
            try:
                emails_parsed = json.loads(emails_raw)
            except (json.JSONDecodeError, TypeError):
                emails_parsed = []
            if not emails_parsed:
                skipped_no_assignee += 1
                continue

            # 拉 GH 现态：已 review → 标记跳过
            if check_review_status_from_gh(pr.pr_url):
                pr.reviewed_at = datetime.utcnow()
                newly_reviewed += 1
                continue

            pr_ids_to_notify.append(pr.id)

        await session.commit()

    # session 外 await resolve_and_notify（其内部自己开 session，避免嵌套）
    # daily sweep 必须 skip_fallback：只对明确 assignee 的 PR 才发，不打扰 sanato
    for pid in pr_ids_to_notify:
        try:
            r = await resolve_and_notify(pid, skip_fallback=True)
            if r.get("sent_count", 0) > 0 or r.get("fallback"):
                notified += 1
        except Exception as e:
            logger.exception("daily_sweep notify failed pr=%d: %s", pid, e)

    logger.info(
        "pr_reviewer daily_sweep: processed=%d skipped_same_day=%d "
        "skipped_no_assignee=%d newly_reviewed=%d notified=%d",
        processed, skipped_same_day, skipped_no_assignee, newly_reviewed, notified,
    )
    return {
        "processed": processed,
        "skipped_same_day": skipped_same_day,
        "skipped_no_assignee": skipped_no_assignee,
        "newly_reviewed": newly_reviewed,
        "notified": notified,
    }
