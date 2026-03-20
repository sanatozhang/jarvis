"""
Eval Pipeline runner — batch evaluate analysis quality against golden samples.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import get_code_repo_for_platform, get_settings
from app.db import database as db
from app.models.schemas import Issue
from app.services.decrypt import process_log_file_for_platform
from app.services.extractor import extract_for_rules
from app.services.golden_samples import _jaccard_similarity
from app.services.issue_text import guess_problem_date, normalize_description_for_matching
from app.services.rule_engine import RuleEngine

logger = logging.getLogger("jarvis.eval_runner")


def _compare_result(golden: Dict[str, Any], actual: Dict[str, Any]) -> Dict[str, Any]:
    """Compare an actual analysis result against the golden sample ground truth."""
    gt_type = (golden.get("problem_type") or "").strip().lower()
    ac_type = (actual.get("problem_type") or "").strip().lower()
    type_match = 1.0 if (gt_type == ac_type or gt_type in ac_type or ac_type in gt_type) else 0.0

    gt_cause = golden.get("root_cause") or ""
    ac_cause = actual.get("root_cause") or ""
    cause_sim = _jaccard_similarity(gt_cause, ac_cause)

    gt_conf = golden.get("confidence") or "medium"
    ac_conf = actual.get("confidence") or "medium"
    conf_match = 1.0 if gt_conf == ac_conf else 0.0

    overall = type_match * 0.4 + cause_sim * 0.4 + conf_match * 0.2
    return {
        "problem_type_match": type_match,
        "root_cause_similarity": round(cause_sim, 3),
        "confidence_match": conf_match,
        "overall_score": round(overall, 3),
    }


async def _load_issue_from_db(issue_id: str, override_description: str = "") -> Optional[Issue]:
    async with db.get_session() as session:
        record = await session.get(db.IssueRecord, issue_id)

    if not record:
        return None

    return Issue(
        record_id=record.id,
        description=override_description or (record.description or ""),
        device_sn=record.device_sn or "",
        firmware=record.firmware or "",
        app_version=record.app_version or "",
        priority=record.priority or "",
        zendesk=record.zendesk or "",
        zendesk_id=record.zendesk_id or "",
        platform=record.platform or "",
        source=record.source or "eval",
        feishu_link=record.feishu_link or "",
        linear_issue_id=record.linear_issue_id or "",
        linear_issue_url=record.linear_issue_url or "",
        created_at_ms=record.created_at_ms or 0,
        occurred_at=record.occurred_at,
        log_files=[],
    )


async def _find_issue_asset_paths(issue_id: str, include_images: bool = False) -> List[Path]:
    settings = get_settings()
    subdir = "images" if include_images else "raw"
    candidates: List[Path] = []
    search_dirs = [
        Path(settings.storage.workspace_dir) / issue_id / subdir,
        Path(settings.storage.workspace_dir) / "_cache" / issue_id / subdir,
    ]

    async with db.get_session() as session:
        from sqlalchemy import select

        stmt = (
            select(db.TaskRecord)
            .where(db.TaskRecord.issue_id == issue_id)
            .order_by(db.TaskRecord.created_at.desc())
            .limit(3)
        )
        tasks = list((await session.execute(stmt)).scalars().all())

    for task in tasks:
        search_dirs.append(Path(settings.storage.workspace_dir) / task.id / subdir)

    seen: set[Path] = set()
    for directory in search_dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(path)
    return candidates


async def _prepare_issue_workspace_assets(workspace: Path, issue: Issue) -> List[Path]:
    raw_dir = workspace / "raw"
    processed_dir = workspace / "processed"
    images_dir = workspace / "images"
    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(exist_ok=True)
    images_dir.mkdir(exist_ok=True)

    raw_candidates = await _find_issue_asset_paths(issue.record_id, include_images=False)
    image_candidates = await _find_issue_asset_paths(issue.record_id, include_images=True)

    copied_raw: List[Path] = []
    for src in raw_candidates:
        dest = raw_dir / src.name
        if not dest.exists():
            shutil.copy2(src, dest)
        copied_raw.append(dest)

    for src in image_candidates:
        dest = images_dir / src.name
        if not dest.exists():
            shutil.copy2(src, dest)

    platform = (issue.platform or "").strip().lower()
    processed_logs: List[Path] = []
    for raw_path in copied_raw:
        log_path, incorrect, reason = process_log_file_for_platform(raw_path, processed_dir, platform=platform)
        if log_path:
            processed_logs.append(log_path)
        elif incorrect and reason:
            logger.info("Eval replay skipped unusable log %s: %s", raw_path.name, reason)

    return processed_logs


async def run_eval(run_id: int):
    """Execute an evaluation run in background."""
    run = await db.get_eval_run(run_id)
    if not run:
        logger.error("Eval run %d not found", run_id)
        return

    await db.update_eval_run(run_id, status="running", started_at=datetime.utcnow())

    try:
        dataset = await db.get_eval_dataset(run["dataset_id"])
        if not dataset:
            raise ValueError(f"Dataset {run['dataset_id']} not found")

        sample_ids = dataset.get("sample_ids", [])
        if not sample_ids:
            raise ValueError("Dataset has no samples")

        samples = []
        for sid in sample_ids:
            sample = await db.get_golden_sample(sid)
            if sample:
                samples.append(sample)

        if not samples:
            raise ValueError("No valid samples found in dataset")

        config = run.get("config", {})
        use_issue_logs = config.get("use_issue_logs", True)
        results: List[Dict[str, Any]] = []

        from app.services.agent_orchestrator import AgentOrchestrator

        orchestrator = AgentOrchestrator()
        settings = get_settings()

        for i, sample in enumerate(samples):
            logger.info("Eval run %d: processing sample %d/%d (id=%s)", run_id, i + 1, len(samples), sample["id"])
            workspace = Path(settings.storage.workspace_dir) / f"eval_{run_id}_{sample['id']}"
            try:
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "output").mkdir(exist_ok=True)
                (workspace / "logs").mkdir(exist_ok=True)
                (workspace / "rules").mkdir(exist_ok=True)

                issue = None
                log_paths: List[Path] = []
                if use_issue_logs and sample.get("issue_id"):
                    issue = await _load_issue_from_db(
                        sample["issue_id"],
                        override_description=sample.get("description", ""),
                    )
                    if issue:
                        log_paths = await _prepare_issue_workspace_assets(workspace, issue)

                if issue is None:
                    issue = Issue(
                        record_id=f"eval_{run_id}_{sample['id']}",
                        description=sample.get("description", ""),
                        device_sn="",
                        firmware="",
                        app_version="",
                        priority="",
                        zendesk="",
                        zendesk_id="",
                        source="eval",
                        feishu_link="",
                        linear_issue_id="",
                        linear_issue_url="",
                        log_files=[],
                    )

                has_logs = len(log_paths) > 0
                routing_text = normalize_description_for_matching(issue.description)
                problem_date = guess_problem_date(routing_text, issue.occurred_at)

                engine = RuleEngine()
                rules = engine.match_rules(routing_text)
                extraction = extract_for_rules(rules, log_paths, problem_date=problem_date) if has_logs else {}
                code_repo = get_code_repo_for_platform((issue.platform or "").strip().lower())
                engine.prepare_workspace(workspace, rules, log_paths, code_repo=code_repo)

                result = await orchestrator.run_analysis(
                    workspace=workspace,
                    issue=issue,
                    rules=rules,
                    extraction=extraction,
                    rule_type=sample.get("rule_type", ""),
                    agent_override=config.get("agent"),
                    problem_date=problem_date,
                    has_logs=has_logs,
                )

                actual = {
                    "problem_type": result.problem_type,
                    "root_cause": result.root_cause,
                    "confidence": result.confidence.value if hasattr(result.confidence, "value") else str(result.confidence),
                    "user_reply": result.user_reply,
                }
                comparison = _compare_result(sample, actual)

                results.append({
                    "sample_id": sample["id"],
                    "issue_id": sample.get("issue_id", ""),
                    "golden": {
                        "problem_type": sample.get("problem_type", ""),
                        "root_cause": sample.get("root_cause", ""),
                        "confidence": sample.get("confidence", ""),
                    },
                    "actual": actual,
                    "scores": comparison,
                    "used_logs": has_logs,
                    "status": "ok",
                })
            except Exception as exc:
                logger.error("Eval sample %s failed: %s", sample["id"], exc)
                results.append({
                    "sample_id": sample["id"],
                    "issue_id": sample.get("issue_id", ""),
                    "used_logs": False,
                    "status": "error",
                    "error": str(exc),
                    "scores": {"overall_score": 0},
                })
            finally:
                shutil.rmtree(workspace, ignore_errors=True)

        ok_results = [result for result in results if result["status"] == "ok"]
        if ok_results:
            avg_score = round(sum(result["scores"]["overall_score"] for result in ok_results) / len(ok_results), 3)
            avg_type = round(sum(result["scores"]["problem_type_match"] for result in ok_results) / len(ok_results), 3)
            avg_cause = round(sum(result["scores"]["root_cause_similarity"] for result in ok_results) / len(ok_results), 3)
            avg_conf = round(sum(result["scores"]["confidence_match"] for result in ok_results) / len(ok_results), 3)
            with_logs = sum(1 for result in ok_results if result.get("used_logs"))
        else:
            avg_score = avg_type = avg_cause = avg_conf = 0
            with_logs = 0

        summary = {
            "total_samples": len(samples),
            "completed": len(ok_results),
            "errors": len(results) - len(ok_results),
            "used_logs": with_logs,
            "without_logs": len(ok_results) - with_logs,
            "avg_overall_score": avg_score,
            "avg_problem_type_match": avg_type,
            "avg_root_cause_similarity": avg_cause,
            "avg_confidence_match": avg_conf,
        }

        await db.update_eval_run(
            run_id,
            status="done",
            results=results,
            summary=summary,
            finished_at=datetime.utcnow(),
        )
        logger.info("Eval run %d completed: avg_score=%.3f", run_id, avg_score)

    except Exception as exc:
        logger.error("Eval run %d failed: %s", run_id, exc)
        await db.update_eval_run(
            run_id,
            status="failed",
            summary={"error": str(exc)},
            finished_at=datetime.utcnow(),
        )
