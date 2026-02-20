"""
Analysis pipeline worker.

Orchestrates the full flow:
  Feishu fetch → Download → Decrypt → Rule match → Extract → Agent analyze → Result
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, List, Optional

from app.config import get_settings
from app.db import database as db
from app.models.schemas import AnalysisResult, Issue
from app.services.agent_orchestrator import AgentOrchestrator
from app.services.decrypt import process_log_file
from app.services.extractor import extract_for_rules
from app.services.feishu import FeishuClient
from app.services.rule_engine import RuleEngine

logger = logging.getLogger("jarvis.worker")

# Singletons
_rule_engine: Optional[RuleEngine] = None
_orchestrator: Optional[AgentOrchestrator] = None


def _get_rule_engine() -> RuleEngine:
    global _rule_engine
    if _rule_engine is None:
        _rule_engine = RuleEngine()
    return _rule_engine


def _get_orchestrator() -> AgentOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AgentOrchestrator()
    return _orchestrator


async def run_analysis_pipeline(
    issue_id: str,
    task_id: str,
    agent_override: Optional[str] = None,
    on_progress: Optional[Callable[[int, str], Any]] = None,
) -> AnalysisResult:
    """
    Run the complete analysis pipeline for a single issue.

    Steps:
    1. Fetch issue from Feishu
    2. Download log files
    3. Decrypt / process logs
    4. Match rules
    5. Pre-extract (grep)
    6. Prepare workspace
    7. Run agent
    8. Parse result
    """
    settings = get_settings()
    is_local = issue_id.startswith("fb_")    # locally submitted via feedback form
    is_linear = issue_id.startswith("lin_")  # Linear-sourced issue

    # --- Step 1: Fetch issue ---
    if on_progress:
        await on_progress(5, "获取工单信息...")

    if is_local or is_linear:
        # Local / Linear issue — read from DB (already saved by webhook handler)
        from app.db.database import get_session, IssueRecord
        import json as _json
        async with get_session() as session:
            rec = await session.get(IssueRecord, issue_id)
        if not rec:
            raise RuntimeError(f"Issue {issue_id} not found in local DB")
        log_files_raw = _json.loads(rec.log_files_json) if rec.log_files_json else []
        issue = Issue(
            record_id=rec.id,
            description=rec.description or "",
            device_sn=rec.device_sn or "",
            firmware=rec.firmware or "",
            app_version=rec.app_version or "",
            priority=rec.priority or "",
            zendesk=rec.zendesk or "",
            zendesk_id=rec.zendesk_id or "",
            source=rec.source or ("linear" if is_linear else "local"),
            feishu_link="",
            linear_issue_id=rec.linear_issue_id or "",
            linear_issue_url=rec.linear_issue_url or "",
            log_files=[],
        )
        logger.info("Processing %s issue %s: %s", issue.source, issue_id, issue.description[:80])
    else:
        # Feishu issue — fetch from API
        client = FeishuClient()
        issue = await client.get_issue(issue_id)
        log_files_raw = [lf.model_dump() for lf in issue.log_files]
        logger.info("Processing issue %s: %s", issue_id, issue.description[:80])
        await db.upsert_issue(issue.model_dump(), status="analyzing")

    # --- Step 2: Download / locate logs ---
    if on_progress:
        await on_progress(10, "准备日志文件...")

    workspace = Path(settings.storage.workspace_dir) / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    raw_dir = workspace / "raw"
    raw_dir.mkdir(exist_ok=True)

    downloaded_files: List[Path] = []

    if is_local:
        # Local files: already saved in workspaces/{record_id}/raw/
        local_raw = Path(settings.storage.workspace_dir) / issue_id / "raw"
        if local_raw.exists():
            for f in local_raw.iterdir():
                if f.is_file():
                    # Copy/link to task workspace
                    dest = raw_dir / f.name
                    if not dest.exists():
                        import shutil
                        shutil.copy2(f, dest)
                    downloaded_files.append(dest)
    else:
        # Feishu files: download via API
        client = FeishuClient()
        for lf_dict in log_files_raw:
            name = lf_dict.get("name", "")
            token = lf_dict.get("token", "")
            if not token:
                continue
            save_path = raw_dir / name
            if not save_path.exists():
                try:
                    await client.download_file(token, str(save_path))
                    downloaded_files.append(save_path)
                except Exception as e:
                    logger.error("Failed to download %s: %s", name, e)
            else:
                downloaded_files.append(save_path)

    if on_progress:
        await on_progress(25, f"已准备 {len(downloaded_files)} 个文件")

    # --- Step 3: Decrypt / process ---
    if on_progress:
        await on_progress(30, "解密日志...")

    log_paths: list[Path] = []
    log_parse_issues: list[str] = []

    for fp in downloaded_files:
        log_path, incorrect, reason = process_log_file(fp, workspace / "processed")
        if log_path:
            log_paths.append(log_path)
        if incorrect and reason:
            log_parse_issues.append(reason)

    has_logs = len(log_paths) > 0

    if has_logs:
        if on_progress:
            await on_progress(40, f"解密完成，{len(log_paths)} 个日志文件")
    else:
        if log_parse_issues:
            logger.warning("Log parse issues: %s", log_parse_issues)
        if downloaded_files:
            logger.warning("Had %d files but none produced usable logs", len(downloaded_files))
        if on_progress:
            await on_progress(40, "无日志文件，将基于描述和代码分析...")

    # --- Step 4: Match rules ---
    if on_progress:
        await on_progress(45, "匹配分析规则...")

    engine = _get_rule_engine()
    rules = engine.match_rules(issue.description)
    rule_type = engine.classify(issue.description)

    logger.info("Matched rules: %s (primary: %s), has_logs: %s", [r.meta.id for r in rules], rule_type, has_logs)

    # --- Step 5: Pre-extract ---
    extraction = {}
    if has_logs:
        if on_progress:
            await on_progress(50, "预提取关键日志...")
        problem_date = _guess_problem_date(issue.description)
        extraction = extract_for_rules(rules, log_paths, problem_date=problem_date)
    else:
        problem_date = _guess_problem_date(issue.description)

    if on_progress:
        await on_progress(55, "准备 Agent 工作空间..." if has_logs else "准备代码分析...")

    # --- Step 6: Prepare workspace ---
    code_repo = settings.code_repo_path if settings.code_repo_path else None
    engine.prepare_workspace(workspace, rules, log_paths, code_repo=code_repo)

    # --- Step 7: Run agent ---
    orchestrator = _get_orchestrator()
    result = await orchestrator.run_analysis(
        workspace=workspace,
        issue=issue,
        rules=rules,
        extraction=extraction,
        rule_type=rule_type,
        agent_override=agent_override,
        problem_date=problem_date,
        has_logs=has_logs,
        on_progress=on_progress,
    )

    result.task_id = task_id
    result.issue = issue

    if on_progress:
        await on_progress(100, "分析完成")

    return result


def _guess_problem_date(description: str) -> Optional[str]:
    """Try to extract a date from the problem description."""
    patterns = [
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{4}/\d{2}/\d{2})",
        r"(\d{1,2}/\d{1,2}/\d{4})",
    ]
    for pat in patterns:
        m = re.search(pat, description)
        if m:
            return m.group(1).replace("/", "-")
    return None
