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
    """过滤 blocked author + 占比阈值；按行数降序返回前 top_n。"""
    blocked_set = {b.lower().strip() for b in blocked}
    filtered = Counter({
        e: n for e, n in counter.items() if e.lower().strip() not in blocked_set
    })
    total = sum(filtered.values())
    if total == 0:
        return []
    sorted_authors = sorted(filtered.items(), key=lambda kv: (-kv[1], kv[0]))
    out: List[Tuple[str, int]] = []
    for email, n in sorted_authors:
        if n / total < min_lines_pct:
            continue
        out.append((email, n))
        if len(out) >= top_n:
            break
    return out


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
