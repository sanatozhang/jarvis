# Crashguard PR 自动指派 Reviewer 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Crashguard PR 创建后用 git blame 自动定位 owner，飞书私聊通知；找不到 fallback 给 sanato；未 review 的 PR 每日 09:30 滚动提醒，review/merged/closed 即停。

**Architecture:** 单一新服务 `app/crashguard/services/pr_reviewer.py`，复用 `feishu_cli` 通信通路与现有 `pipeline_scheduler_loop` 周期心跳框架。无新表，仅在 `crash_pull_requests` 加 6 个 nullable 字段。隔离合约严格遵守，无新增对外耦合点。

**Tech Stack:** SQLAlchemy（既有 ORM）/ subprocess（git blame / gh pr diff / gh pr view）/ feishu_cli.send_card（interactive card）/ APScheduler-free cron tick（复用 warmup.py 的 pipeline_scheduler_loop 风格）

**Spec:** `docs/superpowers/specs/2026-05-21-crashguard-pr-reviewer-auto-assign-design.md`

---

## File Structure

**新增：**
- `backend/app/crashguard/services/pr_reviewer.py` — 核心服务（~400 行）
- `backend/tests/crashguard/test_pr_reviewer.py` — 单元测试（~300 行）

**修改：**
- `backend/app/crashguard/models.py` — 加 6 个字段到 `CrashPullRequest`
- `backend/app/crashguard/migrations.py` — 加 6 条 ADD COLUMN
- `backend/app/crashguard/config.py` — 加 5 个 setting
- `backend/app/crashguard/services/pr_drafter.py` — PR 创建成功后 fire-and-forget hook
- `backend/app/crashguard/workers/warmup.py` — `pipeline_scheduler_loop` 内挂 daily reminder tick

---

## Task 1: Schema + Migration + Config

**Files:**
- Modify: `backend/app/crashguard/models.py` (CrashPullRequest 类)
- Modify: `backend/app/crashguard/migrations.py` (_REQUIRED_COLUMNS 列表)
- Modify: `backend/app/crashguard/config.py` (Settings 类)

- [ ] **Step 1.1: 写失败测试 — 验证新字段存在**

`backend/tests/crashguard/test_pr_reviewer_schema.py`:
```python
from app.crashguard.models import CrashPullRequest


def test_crash_pr_has_reviewer_fields():
    cols = {c.name for c in CrashPullRequest.__table__.columns}
    assert "reviewer_emails" in cols
    assert "reviewer_open_ids" in cols
    assert "reviewer_assigned_at" in cols
    assert "last_reminder_at" in cols
    assert "reviewed_at" in cols
    assert "reviewer_fallback_reason" in cols
```

- [ ] **Step 1.2: 跑测试确认失败**

```bash
cd backend && pytest tests/crashguard/test_pr_reviewer_schema.py -v
```
Expected: FAIL — `reviewer_emails` not in cols

- [ ] **Step 1.3: 加字段到 `models.py::CrashPullRequest`**

在 `class CrashPullRequest` 内 `created_at` 字段前插入：
```python
    # === reviewer auto-assign (2026-05-21) ===
    reviewer_emails = Column(Text, default="[]")           # JSON: ["alice@plaud.ai", ...]
    reviewer_open_ids = Column(Text, default="[]")         # JSON: ["ou_xxx", ...]
    reviewer_assigned_at = Column(DateTime, nullable=True)
    last_reminder_at = Column(DateTime, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    reviewer_fallback_reason = Column(String(64), default="")  # blame_empty / all_unresolved / bot_only / ok
```

注意 SQLAlchemy `JSON` 列在 SQLite 上行为不稳，沿用项目惯例用 `Text` + 应用层 `json.dumps/loads`（见现有 `tags` / `external_refs` 字段）。

- [ ] **Step 1.4: 加 migration 列到 `migrations.py::_REQUIRED_COLUMNS`**

在 PR 状态同步段（`# PR 状态同步`）之后插入：
```python
    # PR reviewer auto-assign (2026-05-21)
    ("crash_pull_requests", "reviewer_emails", "TEXT", "'[]'"),
    ("crash_pull_requests", "reviewer_open_ids", "TEXT", "'[]'"),
    ("crash_pull_requests", "reviewer_assigned_at", "DATETIME", "NULL"),
    ("crash_pull_requests", "last_reminder_at", "DATETIME", "NULL"),
    ("crash_pull_requests", "reviewed_at", "DATETIME", "NULL"),
    ("crash_pull_requests", "reviewer_fallback_reason", "VARCHAR(64)", "''"),
```

- [ ] **Step 1.5: 加 config 到 `config.py::Settings`**

在合适位置（参考 `pr_dedup_days` 附近）：
```python
    # PR reviewer auto-assign (2026-05-21)
    pr_reviewer_enabled: bool = True
    pr_reviewer_top_n: int = 2
    pr_reviewer_min_lines_pct: float = 0.20
    pr_reviewer_blocked_authors: List[str] = Field(
        default_factory=lambda: [
            "jarvis-bot@plaud.ai",
            "noreply@github.com",
            "sanato.zhang@plaud.ai",
        ]
    )
    pr_reviewer_daily_cron: str = "30 9 * * *"  # 09:30 daily reminder
```

如果 `Settings` 顶层不接受 `List[str]`，参考 `feishu_admin_open_ids` 写法。

- [ ] **Step 1.6: 跑测试确认通过**

```bash
cd backend && pytest tests/crashguard/test_pr_reviewer_schema.py -v
```
Expected: PASS

- [ ] **Step 1.7: 跑全量 crashguard 测试 + lint-imports 保证未踩隔离合约**

```bash
cd backend && pytest tests/crashguard/ -v && lint-imports
```
Expected: 所有既有测试 PASS，lint KEPT

- [ ] **Step 1.8: Commit**

```bash
git add backend/app/crashguard/models.py backend/app/crashguard/migrations.py backend/app/crashguard/config.py backend/tests/crashguard/test_pr_reviewer_schema.py
git commit -m "feat(crashguard): CrashPullRequest 加 6 个 reviewer 字段 + config — PR 自动指派 Task 1/9"
```

---

## Task 2: Diff & Blame Parser（pure functions）

**Files:**
- Create: `backend/app/crashguard/services/pr_reviewer.py`
- Modify: `backend/tests/crashguard/test_pr_reviewer.py`（新建）

- [ ] **Step 2.1: 写失败测试 — parse_diff_target_lines**

`backend/tests/crashguard/test_pr_reviewer.py`:
```python
from app.crashguard.services.pr_reviewer import (
    parse_diff_target_lines,
    parse_blame_author_email,
)


def test_parse_diff_target_lines_basic():
    diff = """diff --git a/lib/foo.dart b/lib/foo.dart
index 1234567..89abcde 100644
--- a/lib/foo.dart
+++ b/lib/foo.dart
@@ -10,3 +10,4 @@ class Foo {
   void bar() {
-    print("old");
+    print("new");
+    log.info("added");
   }
"""
    result = parse_diff_target_lines(diff)
    # The 3 lines around hunk header @@ -10,3 +10,4 — modified/added lines
    # We blame OLD lines (the - lines and surrounding context for - hunks),
    # so target: lib/foo.dart line 11 (where 'print("old")' was)
    assert "lib/foo.dart" in result
    assert 11 in result["lib/foo.dart"]


def test_parse_diff_target_lines_multifile():
    diff = """diff --git a/a.dart b/a.dart
--- a/a.dart
+++ b/a.dart
@@ -5,1 +5,1 @@
-old line
+new line
diff --git a/b.dart b/b.dart
--- a/b.dart
+++ b/b.dart
@@ -100,2 +100,2 @@
 ctx
-old
+new
"""
    result = parse_diff_target_lines(diff)
    assert result == {"a.dart": [5], "b.dart": [101]}


def test_parse_blame_author_email_porcelain():
    porcelain = (
        "abc123def 1 1 1\n"
        "author Alice Wang\n"
        "author-mail <alice@plaud.ai>\n"
        "author-time 1700000000\n"
        "summary do something\n"
        "\tcode here\n"
    )
    assert parse_blame_author_email(porcelain) == "alice@plaud.ai"


def test_parse_blame_author_email_missing_returns_empty():
    assert parse_blame_author_email("") == ""
    assert parse_blame_author_email("no email here") == ""
```

- [ ] **Step 2.2: 跑测试确认失败**

```bash
cd backend && pytest tests/crashguard/test_pr_reviewer.py -v
```
Expected: FAIL — `parse_diff_target_lines` not importable

- [ ] **Step 2.3: 实现 pure functions**

新建 `backend/app/crashguard/services/pr_reviewer.py`:
```python
"""
PR Reviewer Auto-Assign — Crashguard

PR 创建后通过 git blame 定位"原作者"作为推荐 reviewer，飞书私聊通知；
找不到时 fallback 给 sanato（feishu_admin_open_ids[0]）。

隔离合约：仅引用 feishu_cli / db.database / 模块内符号。无跨表 join。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, date
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
    reason: str = ""  # ok / pr_url_missing / diff_empty / blame_empty / bot_only


# ============================================================
# Pure helpers — diff & blame 解析
# ============================================================
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FILE_OLD_RE = re.compile(r"^--- a/(.+)$")


def parse_diff_target_lines(diff_text: str) -> Dict[str, List[int]]:
    """
    解析 unified diff，返回 {old_file_path: [old_line_numbers]}。
    我们 blame **被删除/修改前**的行（- 行），因为 blame 是基于 base commit
    判断"这行原来是谁写的"。纯 + 行（新增）没有 base 可 blame，跳过。
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
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-") and not line.startswith("---"):
            result[current_file].append(old_line)
            old_line += 1
        elif line.startswith("+") and not line.startswith("+++"):
            # 新增行，不前进 old_line
            continue
        elif line.startswith(" "):
            old_line += 1
    # 清掉空文件 entry
    return {f: lns for f, lns in result.items() if lns}


def parse_blame_author_email(porcelain: str) -> str:
    """从 git blame --porcelain 输出中解析 author-mail（去除 <>）。"""
    for line in porcelain.splitlines():
        if line.startswith("author-mail "):
            raw = line[len("author-mail ") :].strip()
            return raw.strip("<>").strip()
    return ""
```

- [ ] **Step 2.4: 跑测试确认通过**

```bash
cd backend && pytest tests/crashguard/test_pr_reviewer.py -v
```
Expected: PASS（4 tests）

- [ ] **Step 2.5: Commit**

```bash
git add backend/app/crashguard/services/pr_reviewer.py backend/tests/crashguard/test_pr_reviewer.py
git commit -m "feat(crashguard): pr_reviewer diff & blame 解析 pure 函数 — Task 2/9"
```

---

## Task 3: gh pr diff fetch + resolve_reviewers_by_blame

**Files:**
- Modify: `backend/app/crashguard/services/pr_reviewer.py`
- Modify: `backend/tests/crashguard/test_pr_reviewer.py`

- [ ] **Step 3.1: 写失败测试**

追加到 `test_pr_reviewer.py`:
```python
from unittest.mock import patch, MagicMock
import subprocess as sp


def _fake_run(stdout="", returncode=0):
    cp = MagicMock()
    cp.stdout = stdout
    cp.returncode = returncode
    return cp


def test_fetch_pr_diff_via_gh_success():
    from app.crashguard.services.pr_reviewer import fetch_pr_diff_via_gh
    diff = "diff --git a/a b/a\n--- a/a\n+++ b/a\n"
    with patch("subprocess.run", return_value=_fake_run(stdout=diff)):
        assert fetch_pr_diff_via_gh("https://github.com/x/y/pull/1") == diff


def test_fetch_pr_diff_via_gh_failure_returns_empty():
    from app.crashguard.services.pr_reviewer import fetch_pr_diff_via_gh
    with patch("subprocess.run", return_value=_fake_run(returncode=1)):
        assert fetch_pr_diff_via_gh("https://github.com/x/y/pull/1") == ""


def test_resolve_reviewers_filters_blocked():
    from app.crashguard.services.pr_reviewer import _filter_authors
    counter = Counter({
        "alice@plaud.ai": 5,
        "jarvis-bot@plaud.ai": 10,
        "bob@plaud.ai": 2,
        "sanato.zhang@plaud.ai": 3,
    })
    blocked = ["jarvis-bot@plaud.ai", "sanato.zhang@plaud.ai"]
    out = _filter_authors(counter, blocked, top_n=2, min_lines_pct=0.20)
    # total after filter = 5+2 = 7, alice=5/7=71%, bob=2/7=28%, both ≥ 20%
    assert out == [("alice@plaud.ai", 5), ("bob@plaud.ai", 2)]


def test_resolve_reviewers_min_pct_excludes_noise():
    from app.crashguard.services.pr_reviewer import _filter_authors
    counter = Counter({"alice@plaud.ai": 50, "bob@plaud.ai": 1})
    # total=51, bob=1/51=2% < 20%, excluded
    out = _filter_authors(counter, [], top_n=2, min_lines_pct=0.20)
    assert out == [("alice@plaud.ai", 50)]
```

注意 `from collections import Counter` 已在 pr_reviewer 内，测试也要 import。

- [ ] **Step 3.2: 跑测试确认失败**

```bash
cd backend && pytest tests/crashguard/test_pr_reviewer.py -v
```
Expected: FAIL（4 个新测试 import error）

- [ ] **Step 3.3: 实现 fetch_pr_diff_via_gh + _filter_authors + resolve_reviewers_by_blame**

追加到 `pr_reviewer.py`:
```python
def fetch_pr_diff_via_gh(pr_url: str, timeout: int = 30) -> str:
    """gh pr diff <url> 远端拉 unified diff，失败返回空串。"""
    if not pr_url:
        return ""
    try:
        r = subprocess.run(
            ["gh", "pr", "diff", pr_url],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            logger.warning("gh pr diff failed: rc=%d url=%s err=%s",
                           r.returncode, pr_url, r.stderr[:200])
            return ""
        return r.stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("gh pr diff exception url=%s: %s", pr_url, e)
        return ""


def _filter_authors(
    counter: Counter,
    blocked: List[str],
    top_n: int,
    min_lines_pct: float,
) -> List[Tuple[str, int]]:
    """过滤 bot author + 占比阈值；返回 [(email, lines), ...] 按行数降序前 top_n。"""
    blocked_set = {b.lower().strip() for b in blocked}
    filtered = Counter({e: n for e, n in counter.items() if e.lower() not in blocked_set})
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
    主入口：远端拉 PR diff → 解析改动文件/行 → git blame → 过滤 + 排序。

    settings: app.crashguard.config.Settings（包含 pr_reviewer_* 字段）
    repo_path: 本地 clone 的仓库路径（含目标 base commit）
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
        settings.pr_reviewer_blocked_authors,
        settings.pr_reviewer_top_n,
        settings.pr_reviewer_min_lines_pct,
    )
    if not filtered:
        return ReviewerResolution(reason="bot_only")

    return ReviewerResolution(
        emails=[e for e, _ in filtered],
        line_counts={e: n for e, n in filtered},
        reason="ok",
    )
```

- [ ] **Step 3.4: 跑测试确认通过**

```bash
cd backend && pytest tests/crashguard/test_pr_reviewer.py -v
```
Expected: PASS（8 tests total）

- [ ] **Step 3.5: Commit**

```bash
git add backend/app/crashguard/services/pr_reviewer.py backend/tests/crashguard/test_pr_reviewer.py
git commit -m "feat(crashguard): pr_reviewer 远端拉 diff + blame 聚合 — Task 3/9"
```

---

## Task 4: 飞书卡片 + 通知（含 sanato fallback）

**Files:**
- Modify: `backend/app/crashguard/services/pr_reviewer.py`
- Modify: `backend/tests/crashguard/test_pr_reviewer.py`

- [ ] **Step 4.1: 写失败测试**

```python
import asyncio
from app.crashguard.services.pr_reviewer import (
    build_reviewer_card,
    build_fallback_card,
)


def test_build_reviewer_card_has_pr_link_and_lines():
    card = build_reviewer_card(
        pr_url="https://github.com/x/y/pull/42",
        pr_title="[crashguard][DRAFT] Fix LateInit",
        crash_title="LateInitializationError",
        crash_url="https://app.datadoghq.com/error-tracking/.../abc",
        line_count=15,
        total_lines=20,
    )
    assert "https://github.com/x/y/pull/42" in json.dumps(card, ensure_ascii=False)
    assert "15" in json.dumps(card, ensure_ascii=False)  # line count


def test_build_fallback_card_has_reason_and_pr():
    card = build_fallback_card(
        pr_url="https://github.com/x/y/pull/42",
        pr_title="[crashguard][DRAFT] Fix LateInit",
        reason="all_unresolved",
        unresolved_emails=["alice@plaud.ai", "bob@plaud.ai"],
    )
    payload = json.dumps(card, ensure_ascii=False)
    assert "all_unresolved" in payload
    assert "alice@plaud.ai" in payload
```

- [ ] **Step 4.2: 跑测试确认失败**

```bash
cd backend && pytest tests/crashguard/test_pr_reviewer.py::test_build_reviewer_card_has_pr_link_and_lines -v
```
Expected: FAIL — `build_reviewer_card` 未定义

- [ ] **Step 4.3: 实现 卡片 builder + notify**

追加到 `pr_reviewer.py`:
```python
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
                    f"**你被选中的原因**: 你贡献了被修改代码的 {line_count} 行（占总改动 {pct}%）"
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
    reason_zh = {
        "pr_url_missing": "PR URL 缺失",
        "diff_empty": "无法获取 diff",
        "blame_empty": "diff 解析后无可 blame 行",
        "repo_missing": "本地仓库路径缺失",
        "bot_only": "blame 结果全部为 bot author",
        "all_unresolved": "找到 author 但飞书账号无法解析",
    }.get(reason, reason)
    extra = ""
    if unresolved_emails:
        extra = "\n**未解析 author**: " + ", ".join(unresolved_emails)
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "⚠️ Crashguard PR 需手动指派 reviewer"},
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


async def notify_reviewers(
    pr,  # CrashPullRequest ORM instance
    resolution: ReviewerResolution,
    settings,
) -> Tuple[List[str], str]:
    """
    依据 resolution 决定发给谁。返回 (sent_open_ids, fallback_reason_or_empty)。

    - resolution.reason == "ok" + emails 全部解析失败 → fallback
    - resolution.reason != "ok" → fallback
    """
    from app.services import feishu_cli  # 隔离合约白名单

    pr_title = f"[crashguard][DRAFT] {pr.repo} #{pr.pr_number or '?'}"
    crash_title = f"issue {pr.datadog_issue_id}"
    crash_url = f"https://app.datadoghq.com/error-tracking/issue/{pr.datadog_issue_id}"

    if resolution.reason == "ok":
        mapping = await feishu_cli._emails_to_open_id_map(resolution.emails)
        resolved = [(e, mapping.get(e)) for e in resolution.emails if mapping.get(e)]
        unresolved = [e for e in resolution.emails if not mapping.get(e)]

        if resolved:
            sent: List[str] = []
            for email, open_id in resolved:
                line_count = resolution.line_counts.get(email, 0)
                total = sum(resolution.line_counts.values())
                card = build_reviewer_card(
                    pr_url=pr.pr_url,
                    pr_title=pr_title,
                    crash_title=crash_title,
                    crash_url=crash_url,
                    line_count=line_count,
                    total_lines=total,
                )
                ok = await feishu_cli.send_card(open_id=open_id, card=card)
                if ok:
                    sent.append(open_id)
                    logger.info("reviewer notified: pr=%s email=%s", pr.pr_url, email)
                else:
                    logger.warning("reviewer notify failed: pr=%s email=%s", pr.pr_url, email)
            # 有人发不出去但也有人发出去了 — 不算 fallback，避免噪音
            return sent, ""

        # all unresolved → fallback
        await _send_fallback(pr, "all_unresolved", unresolved, settings)
        return [], "all_unresolved"

    # 任何非 ok reason → fallback
    await _send_fallback(pr, resolution.reason, None, settings)
    return [], resolution.reason


async def _send_fallback(pr, reason: str, unresolved: Optional[List[str]], settings) -> None:
    from app.services import feishu_cli
    admin_ids = settings.feishu_admin_open_ids or []
    if not admin_ids:
        logger.error("no feishu_admin_open_ids configured — fallback cannot send (pr=%s)", pr.pr_url)
        return
    pr_title = f"[crashguard][DRAFT] {pr.repo} #{pr.pr_number or '?'}"
    card = build_fallback_card(pr.pr_url, pr_title, reason, unresolved)
    for open_id in admin_ids[:1]:  # 仅发 admin[0]（sanato）
        await feishu_cli.send_card(open_id=open_id, card=card)
        logger.info("fallback to sanato: pr=%s reason=%s", pr.pr_url, reason)
```

注意：依赖 `feishu_cli.send_card(open_id=..., card=...)`，若该方法不存在需要先确认 feishu_cli 实际 API（任务执行时先查）。

- [ ] **Step 4.4: 跑测试确认通过**

```bash
cd backend && pytest tests/crashguard/test_pr_reviewer.py -v -k "card"
```
Expected: PASS（2 个 card 测试）

- [ ] **Step 4.5: 整体测试 + lint**

```bash
cd backend && pytest tests/crashguard/ -v && lint-imports
```
Expected: 所有 PASS + lint KEPT

- [ ] **Step 4.6: Commit**

```bash
git add backend/app/crashguard/services/pr_reviewer.py backend/tests/crashguard/test_pr_reviewer.py
git commit -m "feat(crashguard): pr_reviewer 飞书卡片 + 通知 + sanato fallback — Task 4/9"
```

---

## Task 5: check_review_status_from_gh

**Files:**
- Modify: `backend/app/crashguard/services/pr_reviewer.py`
- Modify: `backend/tests/crashguard/test_pr_reviewer.py`

- [ ] **Step 5.1: 写失败测试**

```python
def test_check_review_status_merged_returns_true():
    from app.crashguard.services.pr_reviewer import check_review_status_from_gh
    out = '{"state":"MERGED","mergedAt":"2026-05-21T10:00:00Z","closedAt":null,"reviews":[]}'
    with patch("subprocess.run", return_value=_fake_run(stdout=out)):
        assert check_review_status_from_gh("https://github.com/x/y/pull/1") is True


def test_check_review_status_has_review_returns_true():
    from app.crashguard.services.pr_reviewer import check_review_status_from_gh
    out = '{"state":"OPEN","mergedAt":null,"closedAt":null,"reviews":[{"state":"COMMENTED"}]}'
    with patch("subprocess.run", return_value=_fake_run(stdout=out)):
        assert check_review_status_from_gh("https://github.com/x/y/pull/1") is True


def test_check_review_status_no_review_returns_false():
    from app.crashguard.services.pr_reviewer import check_review_status_from_gh
    out = '{"state":"OPEN","mergedAt":null,"closedAt":null,"reviews":[]}'
    with patch("subprocess.run", return_value=_fake_run(stdout=out)):
        assert check_review_status_from_gh("https://github.com/x/y/pull/1") is False


def test_check_review_status_gh_failure_returns_false():
    from app.crashguard.services.pr_reviewer import check_review_status_from_gh
    with patch("subprocess.run", return_value=_fake_run(returncode=1, stdout="")):
        assert check_review_status_from_gh("https://github.com/x/y/pull/1") is False
```

- [ ] **Step 5.2: 跑测试确认失败**

```bash
cd backend && pytest tests/crashguard/test_pr_reviewer.py -v -k "review_status"
```
Expected: FAIL — function not defined

- [ ] **Step 5.3: 实现**

追加到 `pr_reviewer.py`:
```python
def check_review_status_from_gh(pr_url: str, timeout: int = 20) -> bool:
    """True 表示该 PR 已 review / merged / closed，应停止提醒。"""
    if not pr_url:
        return False
    try:
        r = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "state,mergedAt,closedAt,reviews"],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return False
        data = json.loads(r.stdout or "{}")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, json.JSONDecodeError) as e:
        logger.warning("check_review_status exception url=%s: %s", pr_url, e)
        return False
    if data.get("state") in ("MERGED", "CLOSED"):
        return True
    if data.get("mergedAt") or data.get("closedAt"):
        return True
    reviews = data.get("reviews") or []
    return len(reviews) > 0
```

- [ ] **Step 5.4: 跑测试确认通过**

```bash
cd backend && pytest tests/crashguard/test_pr_reviewer.py -v -k "review_status"
```
Expected: PASS（4 个）

- [ ] **Step 5.5: Commit**

```bash
git add backend/app/crashguard/services/pr_reviewer.py backend/tests/crashguard/test_pr_reviewer.py
git commit -m "feat(crashguard): pr_reviewer check_review_status_from_gh — Task 5/9"
```

---

## Task 6: resolve_and_notify orchestrator

**Files:**
- Modify: `backend/app/crashguard/services/pr_reviewer.py`
- Modify: `backend/tests/crashguard/test_pr_reviewer.py`

- [ ] **Step 6.1: 写失败测试（用 in-memory SQLite）**

```python
import asyncio
import pytest
from sqlalchemy import select
from app.crashguard.models import CrashPullRequest


@pytest.mark.asyncio
async def test_resolve_and_notify_writes_assigned_at(db_session_factory):
    """db_session_factory 是 conftest 提供的 fixture（沿用既有 crashguard 测试模式）"""
    # 准备一条 PR
    async with db_session_factory() as s:
        pr = CrashPullRequest(
            analysis_id=1,
            datadog_issue_id="abc",
            repo="plaud-flutter-global",
            branch_name="crashguard/auto-fix/abc-202605211000",
            pr_url="https://github.com/Plaud-AI/plaud-flutter-global/pull/999",
            pr_number=999,
            pr_status="draft",
        )
        s.add(pr)
        await s.commit()
        pr_id = pr.id

    # mock blame + feishu
    from app.crashguard.services import pr_reviewer
    from app.crashguard.services.pr_reviewer import ReviewerResolution

    async def fake_emails_to_open_id_map(emails):
        return {"alice@plaud.ai": "ou_alice"}

    async def fake_send_card(open_id, card):
        return True

    with patch.object(pr_reviewer, "resolve_reviewers_by_blame",
                      return_value=ReviewerResolution(
                          emails=["alice@plaud.ai"],
                          line_counts={"alice@plaud.ai": 5},
                          reason="ok",
                      )), \
         patch("app.services.feishu_cli._emails_to_open_id_map",
               side_effect=fake_emails_to_open_id_map), \
         patch("app.services.feishu_cli.send_card",
               side_effect=fake_send_card):
        result = await pr_reviewer.resolve_and_notify(pr_id)

    assert result["sent_count"] == 1
    assert result["fallback"] is False

    # 校验 DB 字段
    async with db_session_factory() as s:
        pr2 = await s.get(CrashPullRequest, pr_id)
        assert pr2.reviewer_assigned_at is not None
        assert pr2.last_reminder_at is not None
        assert "alice@plaud.ai" in pr2.reviewer_emails
        assert pr2.reviewer_fallback_reason == "ok"
```

> 备注：如 crashguard 已有 `conftest.py::db_session_factory` 沿用之；否则用现有测试里的 session 创建模式。Implementer 执行时先看 `backend/tests/crashguard/conftest.py`。

- [ ] **Step 6.2: 跑测试确认失败**

```bash
cd backend && pytest tests/crashguard/test_pr_reviewer.py::test_resolve_and_notify_writes_assigned_at -v
```
Expected: FAIL

- [ ] **Step 6.3: 实现 resolve_and_notify**

追加到 `pr_reviewer.py`:
```python
async def resolve_and_notify(pr_id: int) -> Dict:
    """
    单次入口：对一条 PR 做 blame → 通知 → 写回 DB。
    返回 {"sent_count": N, "fallback": bool, "reason": str}
    """
    from app.crashguard.config import get_settings as get_cg_settings
    from app.db.database import get_session
    s = get_cg_settings()
    if not s.pr_reviewer_enabled:
        return {"sent_count": 0, "fallback": False, "reason": "disabled"}

    async with get_session() as session:
        pr = await session.get(CrashPullRequest, pr_id)
        if pr is None:
            return {"sent_count": 0, "fallback": False, "reason": "pr_not_found"}

        # 已 review 的就不再通知
        if pr.reviewed_at is not None:
            return {"sent_count": 0, "fallback": False, "reason": "already_reviewed"}

        # 1. blame
        repo_path = _resolve_repo_path_for_pr(pr, s)
        resolution = resolve_reviewers_by_blame(pr.pr_url, repo_path, s)

        # 2. notify
        sent, fallback_reason = await notify_reviewers(pr, resolution, s)

        # 3. 写回 DB
        now = datetime.utcnow()
        pr.reviewer_emails = json.dumps(resolution.emails)
        pr.reviewer_open_ids = json.dumps(sent)
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


def _resolve_repo_path_for_pr(pr, settings) -> str:
    """根据 pr.repo 映射本地仓库路径。复用现有 pr_drafter 的 _platform_repo_path 风格。"""
    # 简化：根据 repo 字段 startswith 推断 platform + sub
    repo = (pr.repo or "").lower()
    if repo.startswith("plaud-flutter-"):
        sub = repo.replace("plaud-flutter-", "")  # common / global / cn
        from app.crashguard.services.pr_drafter import _platform_repo_path
        return _platform_repo_path("flutter", sub if sub != "common" else "")
    if repo.startswith("plaud-android") or repo == "plaud_android":
        return settings.repo_path_android or ""
    if repo.startswith("plaud-ios") or repo == "plaud_ios":
        return settings.repo_path_ios or ""
    return ""
```

注意 `_platform_repo_path` 是 pr_drafter 内私有函数，跨模块引用同一子模块内部符号是允许的（隔离合约约束的是跨 crashguard 模块）。

- [ ] **Step 6.4: 跑测试确认通过**

```bash
cd backend && pytest tests/crashguard/test_pr_reviewer.py -v
```
Expected: 全部 PASS

- [ ] **Step 6.5: Commit**

```bash
git add backend/app/crashguard/services/pr_reviewer.py backend/tests/crashguard/test_pr_reviewer.py
git commit -m "feat(crashguard): pr_reviewer resolve_and_notify orchestrator — Task 6/9"
```

---

## Task 7: daily_reminder_sweep（cron 入口）

**Files:**
- Modify: `backend/app/crashguard/services/pr_reviewer.py`
- Modify: `backend/tests/crashguard/test_pr_reviewer.py`

- [ ] **Step 7.1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_daily_sweep_skips_already_reminded_today(db_session_factory):
    from app.crashguard.services import pr_reviewer
    today = datetime.utcnow()
    async with db_session_factory() as s:
        pr = CrashPullRequest(
            analysis_id=2,
            datadog_issue_id="def",
            repo="plaud-flutter-global",
            pr_url="https://github.com/Plaud-AI/plaud-flutter-global/pull/888",
            pr_number=888,
            pr_status="draft",
            last_reminder_at=today,  # 今天已提醒过
        )
        s.add(pr)
        await s.commit()

    with patch.object(pr_reviewer, "resolve_and_notify") as m:
        result = await pr_reviewer.daily_reminder_sweep()
    m.assert_not_called()
    assert result["skipped_same_day"] >= 1


@pytest.mark.asyncio
async def test_daily_sweep_marks_reviewed_and_skips(db_session_factory):
    from app.crashguard.services import pr_reviewer
    async with db_session_factory() as s:
        pr = CrashPullRequest(
            analysis_id=3,
            datadog_issue_id="ghi",
            repo="plaud-flutter-global",
            pr_url="https://github.com/Plaud-AI/plaud-flutter-global/pull/777",
            pr_number=777,
            pr_status="open",
            last_reminder_at=datetime.utcnow() - timedelta(days=2),
        )
        s.add(pr)
        await s.commit()
        pr_id = pr.id

    with patch.object(pr_reviewer, "check_review_status_from_gh", return_value=True), \
         patch.object(pr_reviewer, "resolve_and_notify") as m_notify:
        result = await pr_reviewer.daily_reminder_sweep()

    m_notify.assert_not_called()
    async with db_session_factory() as s:
        pr2 = await s.get(CrashPullRequest, pr_id)
        assert pr2.reviewed_at is not None
```

记得 `from datetime import timedelta` import。

- [ ] **Step 7.2: 跑测试确认失败**

```bash
cd backend && pytest tests/crashguard/test_pr_reviewer.py -v -k "sweep"
```
Expected: FAIL

- [ ] **Step 7.3: 实现 daily_reminder_sweep**

追加到 `pr_reviewer.py`:
```python
async def daily_reminder_sweep() -> Dict:
    """
    扫描所有未 reviewed 的 PR：
      - 检查 GH 现态，已 reviewed/merged/closed → 写 reviewed_at，跳过
      - 当天已提醒过 → 跳过
      - 其余 → 重跑 resolve_and_notify
    """
    from app.crashguard.config import get_settings as get_cg_settings
    from app.db.database import get_session
    s = get_cg_settings()
    if not s.pr_reviewer_enabled:
        return {"processed": 0, "skipped_same_day": 0, "newly_reviewed": 0, "notified": 0}

    today = datetime.utcnow().date()
    processed = 0
    skipped_same_day = 0
    newly_reviewed = 0
    notified = 0

    async with get_session() as session:
        stmt = select(CrashPullRequest).where(
            CrashPullRequest.reviewed_at.is_(None),
            CrashPullRequest.pr_status.in_(("draft", "open")),
        )
        result = await session.execute(stmt)
        prs = result.scalars().all()

        for pr in prs:
            processed += 1

            # 同日去重
            if pr.last_reminder_at and pr.last_reminder_at.date() == today:
                skipped_same_day += 1
                continue

            # GH 现态检查
            if check_review_status_from_gh(pr.pr_url):
                pr.reviewed_at = datetime.utcnow()
                newly_reviewed += 1
                await session.commit()
                continue

            # 否则重发提醒（resolve_and_notify 自己会写 last_reminder_at）

        await session.commit()
        pr_ids_to_notify = [
            pr.id for pr in prs
            if pr.reviewed_at is None and (
                not pr.last_reminder_at or pr.last_reminder_at.date() != today
            )
        ]

    # session 外重新调（resolve_and_notify 自己开 session 避免嵌套）
    for pid in pr_ids_to_notify:
        try:
            r = await resolve_and_notify(pid)
            if r.get("sent_count", 0) > 0 or r.get("fallback"):
                notified += 1
        except Exception as e:
            logger.exception("daily_sweep notify failed pr=%d: %s", pid, e)

    return {
        "processed": processed,
        "skipped_same_day": skipped_same_day,
        "newly_reviewed": newly_reviewed,
        "notified": notified,
    }
```

注意 `select` 要 `from sqlalchemy import select`。

- [ ] **Step 7.4: 跑测试确认通过**

```bash
cd backend && pytest tests/crashguard/test_pr_reviewer.py -v
```
Expected: 全部 PASS

- [ ] **Step 7.5: lint + 整体**

```bash
cd backend && lint-imports && pytest tests/crashguard/ -v
```
Expected: KEPT + 全 PASS

- [ ] **Step 7.6: Commit**

```bash
git add backend/app/crashguard/services/pr_reviewer.py backend/tests/crashguard/test_pr_reviewer.py
git commit -m "feat(crashguard): pr_reviewer daily_reminder_sweep cron 入口 — Task 7/9"
```

---

## Task 8: pr_drafter.py 集成（PR 创建后 fire-and-forget）

**Files:**
- Modify: `backend/app/crashguard/services/pr_drafter.py`

- [ ] **Step 8.1: 找到 PR 创建成功后的写入点**

```bash
cd backend && grep -n "session.add(pr_record)\|pr_record = CrashPullRequest\|s.add(pr_row)\|pr_url=.*pr_url" app/crashguard/services/pr_drafter.py | head -20
```
找到所有 `CrashPullRequest` 入库后的位置。

- [ ] **Step 8.2: 加 fire-and-forget 调用**

在每处 `await session.commit()` 紧接 `CrashPullRequest` insert 之后，加：
```python
            # PR Reviewer auto-assign (fire-and-forget, 不阻塞 pr_drafter 主流程)
            try:
                import asyncio
                from app.crashguard.services.pr_reviewer import resolve_and_notify
                asyncio.create_task(resolve_and_notify(pr_row.id))
            except Exception as e:
                logger.warning("pr_reviewer dispatch failed pr=%d: %s", pr_row.id, e)
```

变量名以实际代码为准（`pr_row` / `pr_record` / `pr` 等）。

- [ ] **Step 8.3: 跑既有 pr_drafter 测试确保未破坏**

```bash
cd backend && pytest tests/crashguard/test_pr_drafter.py -v
```
Expected: 既有全部 PASS

- [ ] **Step 8.4: Commit**

```bash
git add backend/app/crashguard/services/pr_drafter.py
git commit -m "feat(crashguard): pr_drafter 集成 reviewer 自动指派 fire-and-forget — Task 8/9"
```

---

## Task 9: warmup.py 加 daily reminder cron tick

**Files:**
- Modify: `backend/app/crashguard/workers/warmup.py`

- [ ] **Step 9.1: 找到 pipeline_scheduler_loop 的 cron 解析点**

```bash
cd backend && grep -n "pipeline_cron\|pipeline_scheduler_loop\|_should_fire\|croniter" app/crashguard/workers/warmup.py
```

- [ ] **Step 9.2: 在 loop 内加 reminder tick**

参考 `pipeline_cron` 的判定逻辑，在 `pipeline_scheduler_loop` 内追加同样模式的 reminder 触发块。例如（伪代码，按实际 loop 结构调整）：

```python
            # PR reviewer daily reminder（默认 09:30）
            cron_rev = getattr(s, "pr_reviewer_daily_cron", "") or ""
            if s.pr_reviewer_enabled and cron_rev and _should_fire(cron_rev, now, last_fired_reviewer):
                try:
                    from app.crashguard.services.pr_reviewer import daily_reminder_sweep
                    result = await daily_reminder_sweep()
                    logger.info("pr_reviewer daily_sweep: %s", result)
                except Exception as e:
                    logger.exception("pr_reviewer daily_sweep failed: %s", e)
                last_fired_reviewer = now
```

注意 `last_fired_reviewer` 这种 in-loop 局部变量在 loop 顶端 declare。

- [ ] **Step 9.3: 跑既有 warmup 相关测试 + crashguard 全量**

```bash
cd backend && pytest tests/crashguard/ -v && lint-imports
```
Expected: 全部 PASS + lint KEPT

- [ ] **Step 9.4: Commit**

```bash
git add backend/app/crashguard/workers/warmup.py
git commit -m "feat(crashguard): pipeline scheduler 挂 pr_reviewer daily 09:30 sweep — Task 9/9"
```

---

## 集成验证（部署前）

不部署，仅本地干跑：

- [ ] **本地拉取 102 数据 dry-run**：用 sshpass 把 102 上的 `crash_pull_requests` 表里一条 PR 数据导出到本地 SQLite，然后 `python -c` 调 `resolve_reviewers_by_blame(pr_url, repo_path, settings)`，验证 blame 出真实 email
- [ ] **不开飞书发卡测试**：把 `feishu_cli.send_card` mock 掉，确认逻辑路径覆盖到 fallback / 正常两种
- [ ] **`lint-imports` 最终保证未踩隔离合约**：crashguard 内只引用了 `feishu_cli` + `db.database` + 模块内部符号

## 部署节奏

1. 全部 9 个 task commit 完成，本地 dry-run 通过
2. 用户授权部署（铁律：不擅自部署） → `./deploy-all.sh`
3. 部署后先设 `pr_reviewer_enabled=False`（hot reload config），手动 trigger 一个 PR 的 `resolve_and_notify` 验证 → 确认无误后改 `enabled=True`
4. 观察 24h，看 audit log 没有滥发

## Rollback 预案

万一上线后飞书轰炸：
1. `pr_reviewer_enabled=False`（config 热加载，无需重启）
2. 6 个 DB 字段是 nullable + default，不影响其他流程
3. cron tick 在 `enabled=False` 时直接 return

