"""
Crashguard 轻量自动迁移 — 启动时补齐新增列。

只做安全的"加列"操作（无破坏性修改）。复杂迁移走 alembic。
"""
from __future__ import annotations

import logging
from typing import List, Tuple

from sqlalchemy import text

from app.db.database import get_session

logger = logging.getLogger("crashguard.migrations")

# (table, column, ddl_type, default_sql)
_REQUIRED_COLUMNS: List[Tuple[str, str, str, str]] = [
    ("crash_issues", "assignee", "VARCHAR(64)", "''"),
    ("crash_issues", "kind", "VARCHAR(16)", "'crash'"),
    ("crash_snapshots", "sessions_affected", "INTEGER", "0"),
    ("crash_analyses", "possible_causes", "TEXT", "'[]'"),
    ("crash_analyses", "complexity_kind", "VARCHAR(8)", "''"),
    ("crash_analyses", "solution", "TEXT", "''"),
    ("crash_analyses", "hint", "TEXT", "''"),
    ("crash_issues", "first_analyzed_at", "DATETIME", "NULL"),
    ("crash_issues", "last_analyzed_at", "DATETIME", "NULL"),
    ("crash_analyses", "followup_question", "TEXT", "''"),
    ("crash_analyses", "parent_run_id", "VARCHAR(64)", "''"),
    ("crash_analyses", "answer", "TEXT", "''"),
    ("crash_analyses", "agent_model", "VARCHAR(64)", "''"),
    ("crash_analyses", "fix_diff", "TEXT", "''"),
    # PR 状态同步（pr_sync 回填）
    ("crash_pull_requests", "merged_at", "DATETIME", "NULL"),
    ("crash_pull_requests", "closed_at", "DATETIME", "NULL"),
    ("crash_pull_requests", "last_synced_at", "DATETIME", "NULL"),
    ("crash_issues", "top_os", "VARCHAR(256)", "''"),
    ("crash_issues", "top_device", "VARCHAR(256)", "''"),
    ("crash_issues", "top_app_version", "VARCHAR(128)", "''"),
    # prewarm 重试计数 + 上次失败原因
    ("crash_issues", "prewarm_attempts", "INTEGER", "0"),
    ("crash_issues", "prewarm_last_error", "TEXT", "''"),
    ("crash_issues", "prewarm_last_at", "DATETIME", "NULL"),
]


async def ensure_columns() -> None:
    async with get_session() as session:
        for table, column, ddl_type, default in _REQUIRED_COLUMNS:
            existing = await _list_columns(session, table)
            if column in existing:
                continue
            ddl = f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type} DEFAULT {default}"
            await session.execute(text(ddl))
            logger.info("crashguard migration: %s.%s added", table, column)
        await session.commit()
    await _backfill_kind()
    await _backfill_agent_model()
    await rescue_orphan_analyses()


async def _backfill_agent_model() -> None:
    """历史行 agent_model 留空 → 用 jarvis config.yaml 中的 claude_code 默认 model 兜底。"""
    from sqlalchemy import select
    from app.crashguard.models import CrashAnalysis
    try:
        from app.config import get_settings
        s = get_settings()
        default_model = ""
        for p in (s.agent.providers or []):
            if (p.name or "").lower() == "claude_code":
                default_model = (p.model or "") or ""
                break
    except Exception:
        default_model = ""
    if not default_model:
        return
    async with get_session() as session:
        rows = (await session.execute(
            select(CrashAnalysis).where(
                (CrashAnalysis.agent_model == "") | (CrashAnalysis.agent_model.is_(None)),
                CrashAnalysis.agent_name == "claude_code",
            )
        )).scalars().all()
        n = 0
        for r in rows:
            r.agent_model = default_model
            n += 1
        if n:
            await session.commit()
            logger.info("crashguard migration: backfilled agent_model on %d rows → %s", n, default_model)


_ORPHAN_GRACE_SEC = 15 * 60  # 15 分钟内的 running 行视为仍在跑，不强行标 failed


def _claude_subprocess_alive() -> bool:
    """检测系统中是否仍有 claude 子进程在跑。pgrep 不存在时返回 True（保守）。"""
    import shutil
    import subprocess
    if not shutil.which("pgrep"):
        return True
    try:
        rc = subprocess.run(
            ["pgrep", "-f", "claude"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).returncode
        return rc == 0
    except Exception:
        return True


async def rescue_orphan_analyses() -> None:
    """启动时清理：上次进程被杀但 agent 已写完 result.json 的 running/pending 行。

    硬化逻辑（防误杀）：
    - 有 result.json → 解析回填，置 success
    - 无 result.json + 任务年龄 < 15min → 跳过（可能仍在跑，留给下一轮）
    - 无 result.json + 仍有 claude 子进程在跑 → 跳过
    - 无 result.json + 年龄 ≥ 15min + 无 claude 进程 → 才标 failed
    """
    import json as _json
    from datetime import datetime
    from pathlib import Path
    from sqlalchemy import select
    from app.crashguard.models import CrashAnalysis, CrashIssue

    workspaces = Path(_workspace_root())
    rescued = aborted = skipped = 0
    now = datetime.utcnow()
    claude_alive_cached: bool | None = None

    async with get_session() as session:
        rows = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.status.in_(["running", "pending"]))
        )).scalars().all()
        for r in rows:
            target = workspaces / r.datadog_issue_id.replace("/", "_") / "output" / "result.json"
            if target.exists():
                try:
                    d = _json.loads(target.read_text(encoding="utf-8").lstrip("\ufeff"))
                except Exception:
                    r.status = "failed"
                    r.error = "orphan: result.json parse failed"
                    aborted += 1
                    continue
                r.scenario = d.get("scenario", "") or ""
                r.root_cause = d.get("root_cause", "") or ""
                r.fix_suggestion = d.get("fix_suggestion", "") or ""
                r.feasibility_score = float(d.get("feasibility_score") or 0.0)
                r.confidence = str(d.get("confidence", "") or "low").lower()
                r.reproducibility = str(d.get("reproducibility", "") or "unknown").lower()
                causes = d.get("possible_causes") or []
                if isinstance(causes, list):
                    r.possible_causes = _json.dumps(causes[:5], ensure_ascii=False)
                r.complexity_kind = str(d.get("complexity", "") or "").lower()
                r.solution = d.get("solution", "") or ""
                r.hint = d.get("hint", "") or ""
                r.status = "success" if r.root_cause else "empty"
                r.agent_name = r.agent_name or "claude_code"

                issue = (await session.execute(
                    select(CrashIssue).where(CrashIssue.datadog_issue_id == r.datadog_issue_id)
                )).scalar_one_or_none()
                if issue and issue.first_analyzed_at is None:
                    issue.first_analyzed_at = now
                    issue.last_analyzed_at = now
                rescued += 1
            else:
                age = (now - r.created_at).total_seconds() if r.created_at else float("inf")
                if age < _ORPHAN_GRACE_SEC:
                    skipped += 1
                    continue
                if claude_alive_cached is None:
                    claude_alive_cached = _claude_subprocess_alive()
                if claude_alive_cached:
                    skipped += 1
                    continue
                r.status = "failed"
                r.error = f"orphan: no result.json after {int(age)}s, no claude process alive"
                aborted += 1
        await session.commit()
    if rescued or aborted or skipped:
        logger.info(
            "crashguard rescue_orphan_analyses: rescued=%d aborted=%d skipped=%d",
            rescued, aborted, skipped,
        )


def _workspace_root() -> str:
    import os
    return os.path.join(os.environ.get("WORKSPACE_DIR", "workspaces"), "_crashguard")


async def _backfill_kind() -> None:
    """根据 title + platform 给 crash_issues.kind 回填正确分类（幂等）。"""
    from app.crashguard.models import CrashIssue
    from app.crashguard.services.categorizer import classify_kind
    from sqlalchemy import select

    async with get_session() as session:
        rows = (await session.execute(select(CrashIssue))).scalars().all()
        updated = 0
        for r in rows:
            new_kind = classify_kind(r.title or "", r.platform or "", r.service or "")
            if (r.kind or "") != new_kind:
                r.kind = new_kind
                updated += 1
        if updated:
            await session.commit()
            logger.info("crashguard migration: backfilled kind on %d issues", updated)


async def _list_columns(session, table: str) -> List[str]:
    rows = (await session.execute(text(f"PRAGMA table_info({table})"))).all()
    return [r[1] for r in rows]
