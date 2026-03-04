"""
Golden Samples service — manage verified analysis samples and similarity search.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from app.db import database as db

logger = logging.getLogger("jarvis.golden_samples")


async def promote_analysis_to_sample(analysis_id: int, created_by: str = "") -> Dict[str, Any]:
    """Promote a completed analysis to a golden sample."""
    async with db.get_session() as session:
        analysis = await session.get(db.AnalysisRecord, analysis_id)
        if not analysis:
            raise ValueError(f"Analysis {analysis_id} not found")

        issue = await session.get(db.IssueRecord, analysis.issue_id)
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


def _bigrams(text: str) -> set:
    """Generate bigram tokens from text. Chinese chars individually, English by word."""
    tokens = []
    # Split into Chinese chars and English words
    for part in re.findall(r'[\u4e00-\u9fff]|[a-zA-Z0-9]+', text.lower()):
        if len(part) == 1 and '\u4e00' <= part <= '\u9fff':
            tokens.append(part)
        else:
            tokens.append(part)

    # Generate bigrams
    bigrams = set()
    for i in range(len(tokens)):
        bigrams.add(tokens[i])
        if i + 1 < len(tokens):
            bigrams.add(tokens[i] + tokens[i + 1])
    return bigrams


def _jaccard_similarity(text_a: str, text_b: str) -> float:
    """Compute Jaccard similarity between two texts using bigram tokens."""
    a = _bigrams(text_a)
    b = _bigrams(text_b)
    if not a or not b:
        return 0.0
    intersection = a & b
    union = a | b
    return len(intersection) / len(union) if union else 0.0


async def find_similar_samples(
    description: str,
    rule_type: Optional[str] = None,
    top_k: int = 3,
    threshold: float = 0.15,
) -> List[Dict[str, Any]]:
    """Find golden samples most similar to the given description."""
    samples = await db.list_golden_samples(rule_type=rule_type, limit=200)

    scored = []
    for sample in samples:
        sim = _jaccard_similarity(description, sample.get("description", ""))
        if sim >= threshold:
            scored.append((sim, sample))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:top_k]]
