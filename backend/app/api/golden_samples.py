"""
Golden Samples API — manage verified analysis samples.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.db import database as db
from app.services.golden_samples import promote_analysis_to_sample

logger = logging.getLogger("jarvis.api.golden_samples")
router = APIRouter()


class PromoteSampleRequest(BaseModel):
    analysis_id: int
    created_by: str = ""


@router.post("")
async def create_golden_sample(req: PromoteSampleRequest):
    """Promote an analysis to a golden sample."""
    try:
        sample = await promote_analysis_to_sample(req.analysis_id, req.created_by)
        return sample
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=str(e))


@router.get("")
async def list_samples(
    rule_type: str = Query("", description="Filter by rule type"),
    limit: int = Query(100, ge=1, le=500),
):
    """List golden samples, optionally filtered by rule_type."""
    return await db.list_golden_samples(rule_type=rule_type or None, limit=limit)


@router.get("/stats")
async def get_stats():
    """Get golden samples statistics."""
    return await db.get_golden_samples_stats()


@router.delete("/{sample_id}")
async def delete_sample(sample_id: int):
    """Delete a golden sample."""
    ok = await db.delete_golden_sample(sample_id)
    if not ok:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Sample not found")
    return {"status": "deleted"}
