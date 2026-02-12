"""
API routes for fetching PENDING issues from Feishu.

Feishu is the source of truth for pending issues.
On every fetch, we sync them to local DB (status='pending').
Issues already being analyzed or completed are excluded.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.db import database as db
from app.models.schemas import Issue
from app.services.feishu import FeishuClient

logger = logging.getLogger("jarvis.api.issues")
router = APIRouter()


@router.get("")
async def list_pending_issues(
    assignee: Optional[str] = Query(None, description="Filter by assignee name"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Fetch PENDING issues from Feishu, sync to local DB, then return paginated.
    """
    try:
        client = FeishuClient()
        all_pending = await client.list_pending_issues(assignee=assignee or "")

        # Sync ALL pending issues to local DB (only if they don't already have a non-pending status)
        exclude_ids = await db.get_local_issue_ids()  # returns analyzing + done
        for issue in all_pending:
            if issue.record_id not in exclude_ids:
                await db.upsert_issue(issue.model_dump(), status="pending")

        # Filter out issues already being analyzed or completed
        filtered = [i for i in all_pending if i.record_id not in exclude_ids]

        # Pagination
        total = len(filtered)
        start = (page - 1) * page_size
        page_items = filtered[start : start + page_size]

        return {
            "issues": page_items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size),
            "high_priority": sum(1 for i in filtered if i.priority == "H"),
        }
    except Exception as e:
        logger.error("Failed to list issues: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/refresh")
async def refresh_issues():
    """Force invalidate the Feishu records cache."""
    FeishuClient.invalidate_cache()
    return {"status": "cache_invalidated"}


@router.get("/{record_id}", response_model=Issue)
async def get_issue(record_id: str):
    """Get a single issue by record ID."""
    try:
        client = FeishuClient()
        return await client.get_issue(record_id)
    except Exception as e:
        logger.error("Failed to get issue %s: %s", record_id, e)
        raise HTTPException(status_code=500, detail=str(e))
