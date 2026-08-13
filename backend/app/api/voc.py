"""
VOC Portal taxonomy API — group→label→diagnosis tree, classification stats,
and a manual sync trigger.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api._window import window_params
from app.db import database as db
from app.services import voc_digest, voc_taxonomy
from app.services.date_window import MAX_MOVERS_SPAN_DAYS, InvalidWindow, resolve_window
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
    drill-down UI, plus metadata about which checked-in seed snapshot is
    currently deployed (seed_fetched_at/seed_tag_count) — surfaced so a
    stale taxonomy (VOC changed something upstream, nobody re-pulled) is
    visible in the UI rather than a silent gap. Reads the in-memory active-
    tags cache (refreshed at startup and after every sync) for the tree, and
    the seed file directly for the metadata (the seed file, not the DB, is
    "what snapshot are we running" — see voc_taxonomy.sync_seed_to_db)."""
    tags = voc_taxonomy.active_tags()
    seed = voc_taxonomy.load_seed()
    return {
        "total_active_tags": len(tags),
        "tree": _build_tree(tags),
        "seed_fetched_at": seed.get("fetched_at", ""),
        "seed_tag_count": seed.get("tag_count", 0),
    }


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


@router.post("/taxonomy/reseed")
async def reseed_taxonomy():
    """Force re-upsert voc_tags from the checked-in seed file (backend/seeds/
    voc_taxonomy_seed.json), bypassing sync_seed_to_db()'s bootstrap-only
    skip. Use this after pulling a fresh MCP snapshot and redeploying — VOC
    has no service account provisioned yet so the daily sync_from_voc() loop
    (POST /taxonomy/sync) can't run; this is the manual substitute."""
    diff = await voc_taxonomy.sync_seed_to_db(force=True)
    return {"status": "ok", **diff}


@router.get("/classification-stats")
async def get_classification_stats(
    window: tuple = Depends(window_params(30)),
    include_secondary: bool = Query(False, description="Fold secondary tags into the same tree"),
):
    """Three-level VOC classification stats for the analytics drill-down."""
    date_from, date_to = window
    return await db.get_voc_classification_stats(date_from, date_to, include_secondary=include_secondary)


@router.get("/trend")
async def get_trend(
    window: tuple = Depends(window_params(30)),
    level: str = Query("group", pattern="^(group|label)$"),
):
    """Multi-line trend data for the VOC analytics tab — date -> {key: count}."""
    date_from, date_to = window
    rows = await db.get_voc_analysis_rows(date_from, date_to)
    trend = voc_digest.aggregate_trend(rows, level=level)
    return {"date_from": date_from, "date_to": date_to, "level": level, "trend": trend}


@router.get("/movers")
async def get_movers(
    days: Optional[int] = Query(None, ge=1, le=90, description="Lookback days (default 7); ignored when date_from/date_to given"),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    prev_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Explicit baseline start; defaults to the same-length period immediately before cur_from"),
    prev_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Explicit baseline end"),
    level: str = Query("label", pattern="^(group|label)$"),
    min_base: int = Query(3, ge=1, le=100),
):
    """Week-over-week (or `days`-over-`days`) movers for the diverging bar chart.

    The `days` path keeps its historical `le=90` cap (zero risk to existing
    callers). An explicit date_from/date_to range gets a wider cap
    (MAX_MOVERS_SPAN_DAYS) so a "last 1 year" selection doesn't 422 — it costs
    scanning roughly 2x the range (current + baseline period) so is capped
    well short of the 3650-day ceiling other analytics endpoints allow.
    """
    try:
        cur_from_s, cur_to_s = resolve_window(days, date_from, date_to, default_days=7, max_span_days=MAX_MOVERS_SPAN_DAYS)
    except InvalidWindow as e:
        raise HTTPException(status_code=422, detail=str(e))

    cur_from = date.fromisoformat(cur_from_s)
    cur_to = date.fromisoformat(cur_to_s)

    if (prev_from is None) != (prev_to is None):
        raise HTTPException(status_code=422, detail="prev_from and prev_to must be provided together")
    if prev_from is not None and prev_to is not None:
        prev_from_d, prev_to_d = date.fromisoformat(prev_from), date.fromisoformat(prev_to)
    else:
        span = (cur_to - cur_from).days + 1
        prev_to_d = cur_from - timedelta(days=1)
        prev_from_d = prev_to_d - timedelta(days=span - 1)

    cur_rows = await db.get_voc_analysis_rows(cur_from.isoformat(), cur_to.isoformat())
    prev_rows = await db.get_voc_analysis_rows(prev_from_d.isoformat(), prev_to_d.isoformat())
    movers = voc_digest.aggregate_movers(cur_rows, prev_rows, level=level, min_base=min_base)

    return {
        "cur_from": cur_from.isoformat(), "cur_to": cur_to.isoformat(),
        "prev_from": prev_from_d.isoformat(), "prev_to": prev_to_d.isoformat(),
        "level": level, "movers": movers,
    }


def _require_monday(ws: str) -> None:
    """Raise a 422 if `ws` isn't a Monday. The Query `pattern` already
    rejects non-YYYY-MM-DD shapes, but date.fromisoformat() can still raise
    on e.g. "2026-02-30" — treat that as a 422 too rather than a 500."""
    try:
        is_monday = date.fromisoformat(ws).weekday() == 0
    except ValueError:
        raise HTTPException(status_code=422, detail="week_start must be a Monday (YYYY-MM-DD)")
    if not is_monday:
        raise HTTPException(status_code=422, detail="week_start must be a Monday (YYYY-MM-DD)")


def _require_month_start(ws: str) -> None:
    """Raise a 422 if `ws` isn't the 1st of a calendar month."""
    try:
        is_month_start = date.fromisoformat(ws).day == 1
    except ValueError:
        raise HTTPException(status_code=422, detail="week_start must be the 1st of a month (YYYY-MM-DD)")
    if not is_month_start:
        raise HTTPException(status_code=422, detail="week_start must be the 1st of a month (YYYY-MM-DD)")


def _resolve_and_validate_period(week_start: str, period_type: str) -> str:
    """Default (if omitted) and validate `week_start` against `period_type` —
    shared by all three digest endpoints below."""
    if period_type == "month":
        ws = week_start or voc_digest.default_month_start()
        _require_month_start(ws)
    else:
        ws = week_start or voc_digest.default_week_start()
        _require_monday(ws)
    return ws


@router.get("/weekly-digest")
async def get_weekly_digest(
    week_start: str = Query(
        "", pattern=r"^(\d{4}-\d{2}-\d{2})?$",
        description="YYYY-MM-DD period start (Monday for week, 1st for month); default = most recently completed period",
    ),
    period_type: str = Query("week", pattern="^(week|month)$"),
):
    ws = _resolve_and_validate_period(week_start, period_type)
    return await db.get_voc_weekly_digest(ws, period_type=period_type)


@router.post("/weekly-digest/generate")
async def generate_weekly_digest_endpoint(
    week_start: str = Query(
        "", pattern=r"^(\d{4}-\d{2}-\d{2})?$",
        description="YYYY-MM-DD period start (Monday for week, 1st for month); default = most recently completed period",
    ),
    period_type: str = Query("week", pattern="^(week|month)$"),
    force: bool = Query(False),
):
    ws = _resolve_and_validate_period(week_start, period_type)
    return await voc_digest.generate_weekly_digest(ws, force=force, period_type=period_type)


@router.get("/weekly-digests")
async def list_weekly_digests(
    limit: int = Query(12, ge=1, le=52),
    period_type: str = Query("week", pattern="^(week|month)$"),
):
    return {"digests": await db.list_voc_weekly_digests(limit=limit, period_type=period_type)}
