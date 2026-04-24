"""
Analysis pipeline worker.

Orchestrates the full flow:
  Feishu fetch → Download → Decrypt → Rule match → Extract
  → L1.5 context condense → Agent analyze → Result
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.config import get_settings, get_code_repo_for_platform
from app.db import database as db
from app.models.schemas import AnalysisResult, Issue
from app.services.agent_orchestrator import AgentOrchestrator
from app.services.decrypt import process_log_file_for_platform
from app.services.extractor import extract_for_rules, extract_log_metadata
from app.services.feishu import FeishuClient
from app.services.issue_text import guess_problem_date, normalize_description_for_matching
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
    followup_question: str = "",
) -> AnalysisResult:
    """
    Run the complete analysis pipeline for a single issue.

    Steps (full analysis):
    1. Fetch issue from Feishu
    2. Download log files
    3. Decrypt / process logs
    4. Match rules
    5. Pre-extract (grep)
    6. Prepare workspace
    7. Run agent
    8. Parse result

    For follow-up questions: reuse the previous task's workspace (logs, rules,
    code, extraction) and skip steps 2-6.  Falls back to full pipeline if the
    previous workspace is unavailable.
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
            platform=rec.platform or "",
            source=rec.source or ("linear" if is_linear else "local"),
            feishu_link="",
            linear_issue_id=rec.linear_issue_id or "",
            linear_issue_url=rec.linear_issue_url or "",
            occurred_at=rec.occurred_at,
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

    # ── Follow-up fast path: reuse previous workspace ──
    if followup_question:
        result = await _try_followup_fast_path(
            issue_id=issue_id,
            task_id=task_id,
            issue=issue,
            agent_override=agent_override,
            followup_question=followup_question,
            on_progress=on_progress,
        )
        if result is not None:
            result.task_id = task_id
            result.issue = issue
            result.followup_question = followup_question
            # Carry forward log_metadata from the previous analysis
            prev_analysis = await db.get_analysis_by_issue(issue_id)
            if prev_analysis and getattr(prev_analysis, "log_metadata_json", None):
                import json as _jm
                result.log_metadata = _jm.loads(prev_analysis.log_metadata_json)
            if on_progress:
                await on_progress(100, "分析完成")
            return result
        # Fast path unavailable — fall through to full pipeline
        logger.info("Follow-up fast path unavailable for %s, running full pipeline", issue_id)

    # --- Step 2: Download / locate logs ---
    if on_progress:
        await on_progress(10, "准备日志文件...")

    workspace = Path(settings.storage.workspace_dir) / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    raw_dir = workspace / "raw"
    raw_dir.mkdir(exist_ok=True)
    images_dir = workspace / "images"
    images_dir.mkdir(exist_ok=True)

    # Log cache: reuse previously downloaded logs for the same issue
    cache_dir = Path(settings.storage.workspace_dir) / "_cache" / issue_id / "raw"
    downloaded_files: List[Path] = []

    if is_local:
        local_raw = Path(settings.storage.workspace_dir) / issue_id / "raw"
        if local_raw.exists():
            for f in local_raw.iterdir():
                if f.is_file():
                    dest = raw_dir / f.name
                    if not dest.exists():
                        import shutil
                        shutil.copy2(f, dest)
                    downloaded_files.append(dest)

        local_images = Path(settings.storage.workspace_dir) / issue_id / "images"
        if local_images.exists():
            import shutil
            for img in local_images.iterdir():
                if img.is_file():
                    dest = images_dir / img.name
                    if not dest.exists():
                        shutil.copy2(img, dest)
    elif cache_dir.exists() and any(cache_dir.iterdir()):
        # Reuse cached logs
        import shutil
        for f in cache_dir.iterdir():
            if f.is_file():
                dest = raw_dir / f.name
                if not dest.exists():
                    shutil.copy2(f, dest)
                downloaded_files.append(dest)
        logger.info("Reusing cached logs for issue %s (%d files)", issue_id, len(downloaded_files))
    else:
        # Download from Feishu / Linear
        if not is_linear:
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

        # Save to cache for future re-analysis
        if downloaded_files:
            cache_dir.mkdir(parents=True, exist_ok=True)
            import shutil
            for f in downloaded_files:
                cache_dest = cache_dir / f.name
                if not cache_dest.exists():
                    shutil.copy2(f, cache_dest)
            logger.info("Cached %d log files for issue %s", len(downloaded_files), issue_id)

    # Cleanup: raw cache keeps many entries (files are small, compressed)
    _cleanup_log_cache(Path(settings.storage.workspace_dir) / "_cache", max_issues=500)
    # Cleanup: processed/logs dirs are large (decompressed); keep only recent 50
    _cleanup_workspace_processed(Path(settings.storage.workspace_dir), max_tasks=50)

    if on_progress:
        await on_progress(25, f"已准备 {len(downloaded_files)} 个文件")

    # --- Step 3: Decrypt / process ---
    if on_progress:
        await on_progress(30, "解密日志...")

    log_paths: list[Path] = []
    log_parse_issues: list[str] = []

    platform = (getattr(issue, "platform", "") or "").strip().lower()
    logger.info("Platform: %s (issue %s)", platform or "app (default)", issue_id)

    for fp in downloaded_files:
        log_path, incorrect, reason = process_log_file_for_platform(
            fp, workspace / "processed", platform=platform,
        )
        if log_path:
            log_paths.append(log_path)
        if incorrect and reason:
            log_parse_issues.append(reason)

    has_logs = len(log_paths) > 0

    # Extract log metadata (app version, OS, UID, device model, etc.)
    log_metadata: Dict[str, Any] = {}
    if has_logs:
        log_metadata = extract_log_metadata(log_paths)
        logger.info("Extracted log metadata: %s", {k: v for k, v in log_metadata.items() if k != "file_ids"})

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
    routing_text = normalize_description_for_matching(issue.description)
    rules = engine.match_rules(routing_text)
    rule_type = engine.classify(routing_text)

    logger.info("Matched rules: %s (primary: %s), has_logs: %s", [r.meta.id for r in rules], rule_type, has_logs)

    # --- Step 5: Pre-extract ---
    extraction = {}
    if has_logs:
        if on_progress:
            await on_progress(50, "预提取关键日志...")
        problem_date = guess_problem_date(routing_text, issue.occurred_at)
        extraction = extract_for_rules(rules, log_paths, problem_date=problem_date)
    else:
        problem_date = guess_problem_date(routing_text, issue.occurred_at)

    # --- Step 5.5: L1.5 Context Condensation ---
    condensation_result = None
    workspace_log_paths = log_paths  # default: use original logs

    if has_logs:
        condensation_result = await _run_context_condensation(
            log_paths=log_paths,
            workspace=workspace,
            issue=issue,
            extraction=extraction,
            rules=rules,
            problem_date=problem_date,
            on_progress=on_progress,
        )
        if condensation_result is not None:
            workspace_log_paths = condensation_result["log_paths"]

    if on_progress:
        await on_progress(60, "准备 Agent 工作空间..." if has_logs else "准备代码分析...")

    # --- Step 6: Prepare workspace ---
    code_repo = get_code_repo_for_platform(platform)
    engine.prepare_workspace(workspace, rules, workspace_log_paths, code_repo=code_repo)

    # --- Step 7: Run agent ---
    # For follow-ups that fell through from fast path, still load previous analysis
    previous_analysis = None
    if followup_question:
        prev = await db.get_analysis_by_issue(issue_id)
        if prev:
            import json as _json2
            previous_analysis = {
                "problem_type": prev.problem_type or "",
                "root_cause": prev.root_cause or "",
                "confidence": prev.confidence or "",
                "key_evidence": _json2.loads(prev.key_evidence_json) if prev.key_evidence_json else [],
                "user_reply": prev.user_reply or "",
                "fix_suggestion": prev.fix_suggestion or "",
            }

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
        previous_analysis=previous_analysis,
        followup_question=followup_question,
        condensation_context=condensation_result.get("structured_context") if condensation_result else None,
    )

    result.task_id = task_id
    result.issue = issue
    result.log_metadata = log_metadata
    if followup_question:
        result.followup_question = followup_question

    if on_progress:
        await on_progress(100, "分析完成")

    return result


async def _try_followup_fast_path(
    issue_id: str,
    task_id: str,
    issue: Issue,
    agent_override: Optional[str],
    followup_question: str,
    on_progress: Optional[Callable[[int, str], Any]],
) -> Optional[AnalysisResult]:
    """Attempt incremental follow-up: reuse previous workspace, skip heavy steps.

    Returns AnalysisResult on success, or None to fall back to full pipeline.
    """
    import json as _json
    import shutil

    settings = get_settings()

    # 1. Find the previous successful task for this issue
    prev_task = await db.get_latest_done_task_for_issue(issue_id)
    if not prev_task:
        logger.info("No previous done task for %s — cannot use fast path", issue_id)
        return None

    prev_workspace = Path(settings.storage.workspace_dir) / prev_task.id
    prev_logs_dir = prev_workspace / "logs"

    # Require that the previous workspace still has logs/ (not cleaned up)
    if not prev_logs_dir.exists() or not any(prev_logs_dir.iterdir()):
        logger.info("Previous workspace %s has no logs/ — cannot use fast path", prev_task.id)
        return None

    if on_progress:
        await on_progress(10, "追问模式：复用上次分析工作区...")

    # 2. Create new workspace and symlink/copy heavy dirs from previous workspace
    workspace = Path(settings.storage.workspace_dir) / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "output").mkdir(exist_ok=True)

    reused_dirs = []
    for dirname in ("logs", "rules", "code", "images", "raw"):
        src = prev_workspace / dirname
        dst = workspace / dirname
        if src.exists() and not dst.exists():
            try:
                # Use symlink for speed — these are read-only for the agent
                dst.symlink_to(src.resolve())
                reused_dirs.append(dirname)
            except OSError:
                # Fallback: copy if symlinks not supported (e.g. cross-device)
                shutil.copytree(src, dst)
                reused_dirs.append(dirname)

    logger.info(
        "Follow-up fast path: reusing %s from workspace %s",
        reused_dirs, prev_task.id,
    )

    if on_progress:
        await on_progress(30, f"已复用上次工作区（{', '.join(reused_dirs)}）")

    # 3. Load previous analysis result
    prev_analysis_rec = await db.get_analysis_by_issue(issue_id)
    previous_analysis = None
    if prev_analysis_rec:
        previous_analysis = {
            "problem_type": prev_analysis_rec.problem_type or "",
            "root_cause": prev_analysis_rec.root_cause or "",
            "confidence": prev_analysis_rec.confidence or "",
            "key_evidence": _json.loads(prev_analysis_rec.key_evidence_json) if prev_analysis_rec.key_evidence_json else [],
            "user_reply": prev_analysis_rec.user_reply or "",
            "fix_suggestion": prev_analysis_rec.fix_suggestion or "",
        }

    # 4. Load previous extraction from context (avoid re-running grep)
    extraction = {}
    prev_extraction_file = prev_workspace / "context" / "extraction_full.json"
    if prev_extraction_file.exists():
        try:
            extraction = _json.loads(prev_extraction_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to load previous extraction: %s", e)

    # 5. Load rules & metadata from previous workspace
    engine = _get_rule_engine()
    routing_text = normalize_description_for_matching(issue.description)
    rules = engine.match_rules(routing_text)
    rule_type = engine.classify(routing_text)
    problem_date = guess_problem_date(routing_text, issue.occurred_at)

    has_logs = any((workspace / "logs").iterdir()) if (workspace / "logs").exists() else False

    if on_progress:
        await on_progress(50, "追问模式：构建分析 prompt...")

    # 6. Run agent (the only expensive step)
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
        previous_analysis=previous_analysis,
        followup_question=followup_question,
    )

    return result


def _cleanup_log_cache(cache_root: Path, max_issues: int = 500):
    """Keep only the last N issues' cached raw logs, remove oldest.

    Raw files are small (compressed .plaud/.zip), so we keep many.
    """
    if not cache_root.exists():
        return
    try:
        issue_dirs = sorted(
            [d for d in cache_root.iterdir() if d.is_dir()],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        if len(issue_dirs) > max_issues:
            import shutil
            for old_dir in issue_dirs[max_issues:]:
                shutil.rmtree(old_dir, ignore_errors=True)
            logger.info("Cleaned up log cache: removed %d old entries", len(issue_dirs) - max_issues)
    except Exception as e:
        logger.warning("Log cache cleanup failed: %s", e)


def _cleanup_workspace_processed(workspace_root: Path, max_tasks: int = 50):
    """Delete processed/ and logs/ dirs from old task workspaces to save disk.

    These contain decompressed logs (tens of MB each) and are the main
    disk consumers. We keep raw/ (small, compressed originals) and
    output/ (analysis results) intact so re-analysis can rebuild from raw.
    """
    if not workspace_root.exists():
        return
    try:
        task_dirs = sorted(
            [d for d in workspace_root.iterdir()
             if d.is_dir() and d.name.startswith("task_")],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        if len(task_dirs) <= max_tasks:
            return

        import shutil
        cleaned = 0
        for old_dir in task_dirs[max_tasks:]:
            for subdir_name in ("processed", "logs"):
                subdir = old_dir / subdir_name
                if subdir.exists():
                    shutil.rmtree(subdir, ignore_errors=True)
                    cleaned += 1
        if cleaned:
            logger.info(
                "Cleaned up %d processed/logs dirs from %d old workspaces (kept raw/ and output/)",
                cleaned, len(task_dirs) - max_tasks,
            )
    except Exception as e:
        logger.warning("Workspace processed cleanup failed: %s", e)


async def _run_context_condensation(
    log_paths: List[Path],
    workspace: Path,
    issue: Issue,
    extraction: Dict[str, Any],
    rules: list,
    problem_date: Optional[str],
    on_progress: Optional[Callable[[int, str], Any]],
) -> Optional[Dict[str, Any]]:
    """Run L1.5 context condensation: time-window + optional LLM extraction.

    Returns a dict with:
        - "log_paths": list of (possibly windowed) log paths to use in workspace
        - "structured_context": dict from LLM extraction (or None)
        - "windowing_metadata": list of per-file windowing stats
    Or None if condensation is not applicable.
    """
    import json as _json

    settings = get_settings()
    cc = settings.context_condensation

    # Override with DB-persisted settings (user-configurable via Settings page)
    try:
        import json as _json_cc
        raw_cc = await db.get_oncall_config("condensation_config", "")
        if raw_cc:
            db_cc = _json_cc.loads(raw_cc)
            cc.enabled = db_cc.get("enabled", cc.enabled)
            cc.provider = db_cc.get("provider", cc.provider)
            cc.model = db_cc.get("model", cc.model)
            if db_cc.get("api_key"):
                cc.api_key = db_cc["api_key"]
            cc.log_size_threshold_mb = db_cc.get("log_size_threshold_mb", cc.log_size_threshold_mb)
            cc.time_window_hours_before = db_cc.get("time_window_hours_before", cc.time_window_hours_before)
            cc.time_window_hours_after = db_cc.get("time_window_hours_after", cc.time_window_hours_after)
            cc.timeout = db_cc.get("timeout", cc.timeout)
    except Exception as e:
        logger.warning("Failed to load condensation config from DB: %s", e)

    # Check if any log file exceeds the size threshold
    threshold_bytes = int(cc.log_size_threshold_mb * 1024 * 1024)
    large_logs = [lp for lp in log_paths if lp.exists() and lp.stat().st_size > threshold_bytes]
    if not large_logs:
        return None  # All logs are small, no condensation needed

    total_size = sum(lp.stat().st_size for lp in log_paths if lp.exists())
    logger.info(
        "L1.5: %d log files (%d large, total %.1fMB), threshold=%.1fMB",
        len(log_paths), len(large_logs), total_size / 1024 / 1024, cc.log_size_threshold_mb,
    )

    if on_progress:
        await on_progress(52, "L1.5: 日志时间窗口切割...")

    # --- Step A: Time-window extraction (always, free) ---
    from app.services.log_windower import (
        window_log_files,
        infer_center_time_from_extraction,
        find_error_dense_window,
    )

    center_time = None
    date_only = False

    # Priority 1: explicit problem_date from issue
    if problem_date:
        try:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    center_time = datetime.strptime(problem_date.strip()[:19], fmt)
                    if fmt == "%Y-%m-%d":
                        date_only = True
                        center_time = center_time.replace(hour=12)
                    break
                except ValueError:
                    continue
        except Exception:
            pass

    # Priority 2: infer from L1 extraction timestamps
    if center_time is None and extraction:
        center_time = infer_center_time_from_extraction(extraction)
        if center_time:
            logger.info("L1.5: center_time inferred from L1 extraction: %s", center_time)

    # Priority 3: find the most error-dense hour in the log
    if center_time is None and log_paths:
        for lp in log_paths:
            if lp.exists() and lp.stat().st_size > threshold_bytes:
                center_time = find_error_dense_window(lp)
                if center_time:
                    logger.info("L1.5: center_time from error-dense window: %s", center_time)
                    break

    # Priority 4: fallback to None → windower uses last N hours of log

    windowed_dir = workspace / "windowed"
    # If only a date was provided (no time), use wider window to cover the full day
    hours_before = cc.time_window_hours_before if not date_only else max(cc.time_window_hours_before, 14)
    hours_after = cc.time_window_hours_after if not date_only else max(cc.time_window_hours_after, 14)

    windowed_paths, windowing_meta = window_log_files(
        log_paths=log_paths,
        output_dir=windowed_dir,
        center_time=center_time,
        hours_before=hours_before,
        hours_after=hours_after,
        size_threshold=threshold_bytes,
    )

    # Log windowing results
    for meta in windowing_meta:
        if meta.get("windowed"):
            logger.info(
                "L1.5 windowed: %s → %d/%d lines (%.1f%% reduction)",
                meta.get("original_path", "?"),
                meta.get("kept_lines", 0),
                meta.get("total_lines", 0),
                meta.get("reduction_pct", 0),
            )

    # --- Step B: LLM context extraction (optional, costs money) ---
    structured_context = None
    if cc.enabled and cc.api_key:
        if on_progress:
            await on_progress(55, "L1.5: LLM 上下文提取...")

        from app.services.context_condenser import ContextCondenser, CondensationConfig

        condenser_config = CondensationConfig(
            enabled=cc.enabled,
            provider=cc.provider,
            model=cc.model,
            api_key=cc.api_key,
            api_base_url=cc.api_base_url,
            max_input_chars=cc.max_input_chars,
            timeout=cc.timeout,
            temperature=cc.temperature,
        )
        condenser = ContextCondenser(condenser_config)

        rules_summary = ", ".join(r.meta.name or r.meta.id for r in rules[:3])

        try:
            result = await condenser.condense(
                log_paths=windowed_paths,
                issue_description=issue.description,
                device_sn=issue.device_sn,
                problem_date=problem_date,
                l1_extraction=extraction,
                rules_summary=rules_summary,
            )

            if result.success:
                structured_context = result.structured_context
                # Save to workspace for reference
                context_dir = workspace / "context"
                context_dir.mkdir(parents=True, exist_ok=True)
                (context_dir / "llm_extraction.json").write_text(
                    _json.dumps(structured_context, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info(
                    "L1.5 LLM extraction success: provider=%s, duration=%dms, output=%d chars",
                    result.provider, result.duration_ms, result.output_chars,
                )
            else:
                logger.warning("L1.5 LLM extraction failed: %s", result.error)
                if result.raw_output:
                    # Save raw output even if JSON parsing failed
                    context_dir = workspace / "context"
                    context_dir.mkdir(parents=True, exist_ok=True)
                    (context_dir / "llm_extraction_raw.txt").write_text(
                        result.raw_output, encoding="utf-8",
                    )
        except Exception as e:
            logger.error("L1.5 LLM extraction error: %s", e, exc_info=True)

    # Save windowing metadata
    try:
        context_dir = workspace / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "windowing_meta.json").write_text(
            _json.dumps(windowing_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    return {
        "log_paths": windowed_paths,
        "structured_context": structured_context,
        "windowing_metadata": windowing_meta,
    }
