"""
API routes for locally-tracked issues (analyzed by Jarvis).

- 进行中: issues.status = 'analyzing'
- 已完成: issues.status = 'done'
- 失败:   issues.status = 'failed'
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from app.db import database as db

logger = logging.getLogger("jarvis.api.local")
router = APIRouter()


@router.get("/in-progress")
async def list_in_progress(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Get issues being analyzed OR failed (from local DB). Failed shows retry button."""
    items, total = await db.get_local_issues_paginated("analyzing,failed", page, page_size)
    return {
        "issues": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/completed")
async def list_completed(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Get issues with completed AI analysis (from local DB)."""
    items, total = await db.get_local_issues_paginated("done", page, page_size)
    return {
        "issues": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


@router.get("/failed")
async def list_failed(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Get issues where analysis failed (from local DB)."""
    items, total = await db.get_local_issues_paginated("failed", page, page_size)
    return {
        "issues": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }
