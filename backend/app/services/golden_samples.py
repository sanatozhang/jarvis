"""
Golden Samples service — manage verified analysis samples and similarity search.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.db import database as db
from app.services.issue_text import normalize_description_for_matching
from app.services.text_similarity import bigrams as _bigrams, jaccard_similarity as _jaccard_similarity

logger = logging.getLogger("jarvis.golden_samples")


async def promote_analysis_to_sample(analysis_id: int, created_by: str = "") -> Dict[str, Any]:
    """Promote a completed analysis to a golden sample."""
    async with db.get_session() as session:
        analysis = await session.get(db.AnalysisRecord, analysis_id)
        if not analysis:
            raise ValueError(f"Analysis {analysis_id} not found")

        issue = await db.get_ticket_record(session, analysis.issue_id)
        description = issue.description if issue else ""

    sample = await db.add_golden_sample({
        "issue_id": analysis.issue_id,
        "analysis_id": analysis_id,
        "problem_type": analysis.problem_type or "",
        "description": description,
        "root_cause": analysis.root_cause or "",
        "user_reply": analysis.user_reply or "",
        "confidence": analysis.confidence or "high",
        "rule_type": analysis.rule_type or "",
        "tags": [],
        "quality": "verified",
        "created_by": created_by,
    })
    logger.info("Promoted analysis %d to golden sample %d", analysis_id, sample.id)
    return db._golden_sample_to_dict(sample)


async def find_similar_samples(
    description: str,
    rule_type: Optional[str] = None,
    top_k: int = 3,
    threshold: float = 0.3,
) -> List[Dict[str, Any]]:
    """Find golden samples most similar to the given description."""
    samples = await db.list_golden_samples(rule_type=rule_type, limit=200)
    query = normalize_description_for_matching(description)

    scored = []
    for sample in samples:
        sample_desc = normalize_description_for_matching(sample.get("description", ""))
        sim = _jaccard_similarity(query, sample_desc)
        if sim >= threshold:
            scored.append((sim, sample))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:top_k]]
