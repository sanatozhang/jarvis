"""
Eval Pipeline runner — batch evaluate analysis quality against golden samples.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import get_settings
from app.db import database as db
from app.services.golden_samples import _jaccard_similarity

logger = logging.getLogger("jarvis.eval_runner")


def _compare_result(golden: Dict[str, Any], actual: Dict[str, Any]) -> Dict[str, Any]:
    """Compare an actual analysis result against the golden sample ground truth."""
    # Problem type match: exact or containment
    gt_type = (golden.get("problem_type") or "").strip().lower()
    ac_type = (actual.get("problem_type") or "").strip().lower()
    type_match = 1.0 if (gt_type == ac_type or gt_type in ac_type or ac_type in gt_type) else 0.0

    # Root cause similarity via Jaccard
    gt_cause = golden.get("root_cause") or ""
    ac_cause = actual.get("root_cause") or ""
    cause_sim = _jaccard_similarity(gt_cause, ac_cause)

    # Confidence match
    gt_conf = golden.get("confidence") or "medium"
    ac_conf = actual.get("confidence") or "medium"
    conf_match = 1.0 if gt_conf == ac_conf else 0.0

    # Overall score: type 40% + cause 40% + confidence 20%
    overall = type_match * 0.4 + cause_sim * 0.4 + conf_match * 0.2

    return {
        "problem_type_match": type_match,
        "root_cause_similarity": round(cause_sim, 3),
        "confidence_match": conf_match,
        "overall_score": round(overall, 3),
    }


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

        # Load golden samples
        samples = []
        for sid in sample_ids:
            s = await db.get_golden_sample(sid)
            if s:
                samples.append(s)

        if not samples:
            raise ValueError("No valid samples found in dataset")

        config = run.get("config", {})
        results: List[Dict[str, Any]] = []

        from app.agents.base import BaseAgent
        from app.models.schemas import Issue, Rule
        from app.services.agent_orchestrator import AgentOrchestrator

        orchestrator = AgentOrchestrator()
        settings = get_settings()

        for i, sample in enumerate(samples):
            logger.info("Eval run %d: processing sample %d/%d (id=%s)", run_id, i + 1, len(samples), sample["id"])
            try:
                # Build a simplified issue from the golden sample
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

                # Create a temp workspace
                workspace = Path(settings.storage.workspace_dir) / f"eval_{run_id}_{sample['id']}"
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "output").mkdir(exist_ok=True)
                (workspace / "logs").mkdir(exist_ok=True)
                (workspace / "rules").mkdir(exist_ok=True)

                # Build prompt (no logs, no extraction — just description + rules)
                from app.services.rule_engine import RuleEngine
                engine = RuleEngine()
                rules = engine.match_rules(sample.get("description", ""))
                engine.prepare_workspace(workspace, rules, [], code_repo=None)

                prompt = BaseAgent.build_prompt(
                    issue=issue,
                    rules=rules,
                    extraction={},
                    has_logs=False,
                )

                agent = orchestrator.select_agent(sample.get("rule_type", ""), override=config.get("agent"))
                result = await agent.analyze(workspace=workspace, prompt=prompt)

                actual = {
                    "problem_type": result.problem_type,
                    "root_cause": result.root_cause,
                    "confidence": result.confidence.value if hasattr(result.confidence, 'value') else str(result.confidence),
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
                    "status": "ok",
                })

                # Cleanup eval workspace
                import shutil
                shutil.rmtree(workspace, ignore_errors=True)

            except Exception as e:
                logger.error("Eval sample %s failed: %s", sample["id"], e)
                results.append({
                    "sample_id": sample["id"],
                    "issue_id": sample.get("issue_id", ""),
                    "status": "error",
                    "error": str(e),
                    "scores": {"overall_score": 0},
                })

        # Compute summary
        ok_results = [r for r in results if r["status"] == "ok"]
        if ok_results:
            avg_score = round(sum(r["scores"]["overall_score"] for r in ok_results) / len(ok_results), 3)
            avg_type = round(sum(r["scores"]["problem_type_match"] for r in ok_results) / len(ok_results), 3)
            avg_cause = round(sum(r["scores"]["root_cause_similarity"] for r in ok_results) / len(ok_results), 3)
            avg_conf = round(sum(r["scores"]["confidence_match"] for r in ok_results) / len(ok_results), 3)
        else:
            avg_score = avg_type = avg_cause = avg_conf = 0

        summary = {
            "total_samples": len(samples),
            "completed": len(ok_results),
            "errors": len(results) - len(ok_results),
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

    except Exception as e:
        logger.error("Eval run %d failed: %s", run_id, e)
        await db.update_eval_run(
            run_id,
            status="failed",
            summary={"error": str(e)},
            finished_at=datetime.utcnow(),
        )
