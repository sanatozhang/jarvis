"""
Rule accuracy statistics service.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

from app.db import database as db

logger = logging.getLogger("jarvis.rule_accuracy")

_CONFIDENCE_SCORE = {"high": 3, "medium": 2, "low": 1}


async def get_rule_accuracy_stats(days: int = 30) -> List[Dict[str, Any]]:
    """
    Compute per-rule accuracy stats by joining analyses and issues.

    Returns list of dicts with:
    - rule_type, total, done, inaccurate, accuracy_rate, avg_confidence_score
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    async with db.get_session() as session:
        from sqlalchemy import select, and_
        stmt = select(
            db.AnalysisRecord.rule_type,
            db.AnalysisRecord.confidence,
            db.IssueRecord.status,
        ).join(
            db.IssueRecord,
            db.AnalysisRecord.issue_id == db.IssueRecord.id,
        ).where(
            db.AnalysisRecord.created_at >= cutoff,
        )
        result = await session.execute(stmt)
        rows = result.fetchall()

    # Aggregate by rule_type
    stats: Dict[str, Dict[str, Any]] = {}
    for rule_type, confidence, issue_status in rows:
        rt = rule_type or "general"
        if rt not in stats:
            stats[rt] = {"rule_type": rt, "total": 0, "done": 0, "inaccurate": 0, "confidence_scores": []}
        stats[rt]["total"] += 1
        if issue_status == "done":
            stats[rt]["done"] += 1
        elif issue_status == "inaccurate":
            stats[rt]["inaccurate"] += 1
        score = _CONFIDENCE_SCORE.get(confidence, 1)
        stats[rt]["confidence_scores"].append(score)

    result_list = []
    for rt, s in stats.items():
        denominator = s["done"] + s["inaccurate"]
        accuracy_rate = round(s["done"] / denominator * 100, 1) if denominator > 0 else 0
        scores = s["confidence_scores"]
        avg_confidence = round(sum(scores) / len(scores), 2) if scores else 0
        result_list.append({
            "rule_type": s["rule_type"],
            "total": s["total"],
            "done": s["done"],
            "inaccurate": s["inaccurate"],
            "accuracy_rate": accuracy_rate,
            "avg_confidence_score": avg_confidence,
        })

    result_list.sort(key=lambda x: x["total"], reverse=True)
    return result_list
