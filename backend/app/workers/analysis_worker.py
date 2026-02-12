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
    issue_override: Optional[Issue] = None,
    local_files: Optional[List[Path]] = None,
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

    workspace = Path(settings.storage.workspace_dir) / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    raw_dir = workspace / "raw"
    raw_dir.mkdir(exist_ok=True)

    downloaded_files: List[Path] = []
    if issue_override is not None:
        issue = issue_override
        if on_progress:
            await on_progress(8, "读取用户上传数据...")
        for fp in local_files or []:
            p = Path(fp)
            if p.exists():
                downloaded_files.append(p)
        logger.info("Processing uploaded issue %s with %d files", issue_id, len(downloaded_files))
        await db.upsert_issue({**issue.model_dump(), "source": "user_upload"}, status="analyzing")
        if on_progress:
            await on_progress(25, f"已接收 {len(downloaded_files)} 个上传文件")
    else:
        # --- Step 1: Fetch issue ---
        if on_progress:
            await on_progress(5, "获取工单信息...")

        client = FeishuClient()
        issue = await client.get_issue(issue_id)
        logger.info("Processing issue %s: %s", issue_id, issue.description[:80])

        # Save issue to local DB and mark as "analyzing"
        await db.upsert_issue(issue.model_dump(), status="analyzing")

        # --- Step 2: Download logs ---
        if on_progress:
            await on_progress(10, "下载日志文件...")

        for lf in issue.log_files:
            save_path = raw_dir / lf.name
            if not save_path.exists():
                try:
                    await client.download_file(lf.token, str(save_path))
                    downloaded_files.append(save_path)
                except Exception as e:
                    logger.error("Failed to download %s: %s", lf.name, e)
            else:
                downloaded_files.append(save_path)

        if on_progress:
            await on_progress(25, f"已下载 {len(downloaded_files)} 个文件")

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

    if not log_paths:
        msg = "无可用日志文件"
        if log_parse_issues:
            msg += f": {'; '.join(log_parse_issues)}"
        await db.update_issue_status(issue_id, "failed")
        return AnalysisResult(
            task_id=task_id,
            issue_id=issue_id,
            problem_type="日志解析失败",
            root_cause=msg,
            confidence="low",
            needs_engineer=True,
            requires_more_info=True,
            more_info_guidance=(
                "请在 APP 内复现问题后，立即通过“反馈/导出日志”上传最新日志。"
                "同时补充问题发生时间、操作步骤和设备版本。"
            ),
            next_steps=["在 APP 内复现问题", "导出并上传最新日志", "补充问题发生时间与操作路径"],
            user_reply="您好，我们正在分析您的问题，由于日志文件格式异常，需要工程师进一步检查。我们会尽快回复您。",
            issue=issue,
        )

    if on_progress:
        await on_progress(40, f"解密完成，{len(log_paths)} 个日志文件")

    # --- Step 4: Match rules ---
    if on_progress:
        await on_progress(45, "匹配分析规则...")

    engine = _get_rule_engine()
    rules = engine.match_rules(issue.description)
    rule_type = engine.classify(issue.description)

    logger.info("Matched rules: %s (primary: %s)", [r.meta.id for r in rules], rule_type)

    # --- Step 5: Pre-extract ---
    if on_progress:
        await on_progress(50, "预提取关键日志...")

    problem_date = _guess_problem_date(issue.description)
    extraction = extract_for_rules(rules, log_paths, problem_date=problem_date)

    if on_progress:
        await on_progress(55, "预提取完成，准备 Agent 工作空间...")

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
