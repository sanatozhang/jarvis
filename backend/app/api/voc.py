"""
VOC Portal taxonomy API — group→label→diagnosis tree, classification stats,
and a manual sync trigger.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query

from app.db import database as db
from app.services import voc_taxonomy
from app.services.voc_client import VocApiError, VocAuthError, VocCredentialsMissing

logger = logging.getLogger("jarvis.api.voc")
router = APIRouter()


def _build_tree(tags: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group active tags into a 3-level tree for frontend drill-down."""
    groups: Dict[str, Dict[str, Any]] = {}
    for tag in tags:
        g = tag.get("level_1_category") or "未分类"
        l = tag.get("level_2_label") or ""
        node = groups.setdefault(g, {"group": g, "labels": {}})
        label_node = node["labels"].setdefault(l, {"label": l, "diagnoses": []})
        label_node["diagnoses"].append({
            "tag_id": tag["id"],
            "diagnosis": tag.get("level_3_diagnosis", ""),
            "definition": tag.get("definition", ""),
        })

    return [
        {
            "group": g["group"],
            "labels": list(g["labels"].values()),
        }
        for g in groups.values()
    ]


@router.get("/taxonomy")
async def get_taxonomy():
    """Full active-tag tree (group → label → diagnosis) for the analytics
    drill-down UI. Reads the in-memory cache (see voc_taxonomy.reload_from_db,
    refreshed at startup and after every sync) — not a live DB hit."""
    tags = voc_taxonomy.active_tags()
    return {"total_active_tags": len(tags), "tree": _build_tree(tags)}


@router.post("/taxonomy/sync")
async def sync_taxonomy():
    """Manually pull the live taxonomy from VOC Portal and refresh the DB +
    cache. Requires VOC_CLIENT_ID/VOC_CLIENT_SECRET to be set — surfaces a
    clear 4xx rather than a generic 500 when they're missing, since that's
    the expected state until a service account is provisioned."""
    try:
        diff = await voc_taxonomy.sync_from_voc()
    except VocCredentialsMissing as e:
        raise HTTPException(status_code=412, detail=str(e))
    except (VocAuthError, VocApiError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"status": "ok", **diff}


@router.get("/classification-stats")
async def get_classification_stats(
    days: int = Query(30, ge=1, le=3650, description="Number of days to look back"),
    include_secondary: bool = Query(False, description="Fold secondary tags into the same tree"),
):
    """Three-level VOC classification stats for the analytics drill-down."""
    date_to = datetime.utcnow().strftime("%Y-%m-%d")
    date_from = (datetime.utcnow() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return await db.get_voc_classification_stats(date_from, date_to, include_secondary=include_secondary)
