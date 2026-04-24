"""
API routes for fetching issues from Feishu.

Feishu is the source of truth for pending issues.
On every fetch, we sync them to local DB (status='pending').
Issues already being analyzed or completed are excluded.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db import database as db
from app.models.schemas import Issue
from app.services.feishu import FeishuClient

logger = logging.getLogger("jarvis.api.issues")
router = APIRouter()


class ImportRequest(BaseModel):
    url: str  # record ID, record link, or keyword to search


class ImportByIdRequest(BaseModel):
    record_id: str


@router.get("")
async def list_pending_issues(
    assignee: Optional[str] = Query(None, description="Filter by assignee name"),
    include_in_progress: bool = Query(False, description="Also include recent in_progress issues"),
    in_progress_limit: int = Query(10, ge=1, le=50),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Fetch PENDING issues from Feishu, optionally including recent in_progress ones.
    """
    try:
        client = FeishuClient()
        all_pending = await client.list_pending_issues(assignee=assignee or "")

        # Sync to local DB (only if they don't already have a non-pending status)
        exclude_ids = await db.get_local_issue_ids()  # returns analyzing + done
        for issue in all_pending:
            if issue.record_id not in exclude_ids:
                await db.upsert_issue(issue.model_dump(), status="pending")

        # Filter out issues already being analyzed or completed
        filtered = [i for i in all_pending if i.record_id not in exclude_ids]

        # Optionally append recent in_progress issues
        in_progress_issues: list = []
        if include_in_progress:
            ip = await client.list_issues_by_status("in_progress", assignee=assignee or "", limit=in_progress_limit)
            in_progress_issues = [i for i in ip if i.record_id not in exclude_ids]
            # Deduplicate (shouldn't overlap but just in case)
            existing_ids = {i.record_id for i in filtered}
            in_progress_issues = [i for i in in_progress_issues if i.record_id not in existing_ids]
            filtered = filtered + in_progress_issues

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
            "in_progress_count": len(in_progress_issues),
        }
    except Exception as e:
        logger.error("Failed to list issues from Feishu: %s", e)
        # Degrade gracefully: return empty list instead of 500
        # The user can still access analyzed issues via tracking page
        return {
            "issues": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "total_pages": 1,
            "high_priority": 0,
            "in_progress_count": 0,
            "error": f"飞书同步失败: {str(e)[:200]}",
        }


@router.post("/refresh")
async def refresh_issues():
    """Force invalidate the Feishu records cache."""
    FeishuClient.invalidate_cache()
    return {"status": "cache_invalidated"}


@router.post("/import/search")
async def search_feishu_issues(body: ImportRequest):
    """
    Search Feishu issues by keyword (description, device SN, zendesk ID).
    Also accepts a record link or bare record ID for direct match.
    Returns a list of candidates for the user to pick from.
    """
    raw = body.url.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="请输入搜索关键词")

    try:
        client = FeishuClient()

        # Direct record ID
        if raw.startswith("rec") and "/" not in raw:
            issue = await client.get_issue(raw)
            return {"issues": [issue]}

        # URL with record param → direct match
        if raw.startswith("http"):
            parsed = urlparse(raw)
            qs = parse_qs(parsed.query)
            rec = (qs.get("record") or [None])[0]
            if rec:
                issue = await client.get_issue(rec)
                return {"issues": [issue]}

        # Keyword search across all cached records
        records = await client.list_records()
        keyword = raw.lower()
        matches = []
        for r in records:
            fields = r.get("fields", {})
            desc = client._get_text(fields.get("问题描述", "")).lower()
            sn = client._get_text(fields.get("设备 SN", "")).lower()
            zendesk = client._get_text(fields.get("Zendesk 工单链接", "")).lower()
            root_cause = client._get_text(fields.get("一句话归因", "")).lower()
            if keyword in desc or keyword in sn or keyword in zendesk or keyword in root_cause:
                matches.append(r)
        # Limit results and parse
        issues = [client.parse_record(r) for r in matches[:20]]
        return {"issues": issues}
    except Exception as e:
        logger.error("Failed to search issues: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/import")
async def import_issue(body: ImportByIdRequest):
    """Import a single issue by record ID into local DB as pending."""
    try:
        client = FeishuClient()
        issue = await client.get_issue(body.record_id)
        data = issue.model_dump()
        data["source"] = "feishu_import"
        await db.upsert_issue(data, status="pending")
        return {"status": "ok", "issue": issue}
    except Exception as e:
        logger.error("Failed to import issue %s: %s", body.record_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{record_id}", response_model=Issue)
async def get_issue(record_id: str):
    """Get a single issue by record ID."""
    try:
        client = FeishuClient()
        return await client.get_issue(record_id)
    except Exception as e:
        logger.error("Failed to get issue %s: %s", record_id, e)
        raise HTTPException(status_code=500, detail=str(e))
