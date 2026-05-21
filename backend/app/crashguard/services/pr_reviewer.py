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
