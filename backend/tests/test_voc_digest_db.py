"""Tests for the VOC digest DB layer: get_voc_analysis_rows (raw row reader
for app.services.voc_digest) and the voc_weekly_digests cache table CRUD."""
from __future__ import annotations

import json

import pytest


async def test_get_voc_analysis_rows_shape_and_date_filter(db_engine, db_session):
    import app.db.database as db_mod
    from app.db.database import AnalysisRecord
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        async with db_session() as s:
            s.add(AnalysisRecord(
                task_id="t1", issue_id="i1", created_at=__import__("datetime").datetime(2026, 8, 10),
                voc_tags_json=json.dumps([{"tag_id": "ai-01", "role": "primary"}]),
                root_cause="token mismatch", device_type="Note", platform="app", needs_engineer=True,
            ))
            s.add(AnalysisRecord(
                task_id="t2", issue_id="i2", created_at=__import__("datetime").datetime(2026, 7, 1),
                voc_tags_json="[]",
            ))
            await s.commit()

        rows = await db_mod.get_voc_analysis_rows("2026-08-01", "2026-08-31")
        assert len(rows) == 1  # the July row is outside the range
        row = rows[0]
        assert row["issue_id"] == "i1"
        assert row["needs_engineer"] is True
        assert row["device_type"] == "Note"
        assert json.loads(row["voc_tags_json"])[0]["tag_id"] == "ai-01"
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_get_voc_analysis_rows_empty_range_returns_empty_list(db_engine, db_session):
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        rows = await db_mod.get_voc_analysis_rows("2026-01-01", "2026-01-31")
        assert rows == []
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_upsert_voc_weekly_digest_creates_and_updates(db_engine, db_session):
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        stats = {"total_cur": 10}
        narrative = {"headline": "test week"}
        record = await db_mod.upsert_voc_weekly_digest(
            week_start="2026-08-03", stats=stats, narrative=narrative,
            markdown="# hi", model="claude-test", total_tokens=100, total_cost_usd=0.01,
        )
        assert record["week_start"] == "2026-08-03"
        assert record["stats"] == stats
        assert record["narrative"] == narrative
        assert record["markdown"] == "# hi"

        fetched = await db_mod.get_voc_weekly_digest("2026-08-03")
        assert fetched["stats"] == stats

        # Second call with the same week_start updates in place, not a duplicate row
        updated = await db_mod.upsert_voc_weekly_digest(
            week_start="2026-08-03", stats={"total_cur": 20}, narrative=None, markdown="# updated",
        )
        assert updated["stats"] == {"total_cur": 20}
        assert updated["narrative"] is None
        all_digests = await db_mod.list_voc_weekly_digests(limit=10)
        assert len(all_digests) == 1
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_get_voc_weekly_digest_missing_returns_none(db_engine, db_session):
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        assert await db_mod.get_voc_weekly_digest("2099-01-01") is None
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_list_voc_weekly_digests_orders_newest_first(db_engine, db_session):
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        await db_mod.upsert_voc_weekly_digest(week_start="2026-07-27", stats={}, narrative=None, markdown="")
        await db_mod.upsert_voc_weekly_digest(week_start="2026-08-10", stats={}, narrative=None, markdown="")
        await db_mod.upsert_voc_weekly_digest(week_start="2026-08-03", stats={}, narrative=None, markdown="")
        digests = await db_mod.list_voc_weekly_digests(limit=10)
        assert [d["week_start"] for d in digests] == ["2026-08-10", "2026-08-03", "2026-07-27"]
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory
