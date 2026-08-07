"""Tests for scripts/backfill_voc_tags.py — dry-run must never write, and a
second run must be idempotent (only_empty=True by default)."""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from scripts.backfill_voc_tags import run_backfill
from tests.conftest import seed_analysis, seed_issue


@pytest.fixture()
async def wired_db(db_engine, db_session):
    """Point app.db.database at the test engine/session for the duration of
    the test, and stub out the two things run_backfill() calls that a plain
    unit test shouldn't hit for real: init_db() (tables already exist) and
    the taxonomy bootstrap (no seed file / VOC creds in a test environment)."""
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        with patch("app.db.database.init_db", new_callable=AsyncMock):
            with patch("app.services.voc_taxonomy.sync_seed_to_db", new_callable=AsyncMock, return_value=0):
                with patch("app.services.voc_taxonomy.reload_from_db", new_callable=AsyncMock):
                    with patch("app.services.voc_taxonomy.active_tags", return_value=[]):
                        yield db_session
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


def _fake_tags():
    return [{
        "tag_id": "ai-01", "level_1_category": "蓝牙连接", "level_2_label": "配对失败",
        "level_3_diagnosis": "Token不匹配", "role": "primary", "confidence": "high",
        "reason": "matches",
    }]


async def test_dry_run_writes_nothing(wired_db):
    await seed_issue(wired_db, issue_id="i1", category="hardware")
    await seed_analysis(wired_db, task_id="t1", issue_id="i1",
                         created_at=datetime.fromisoformat("2026-07-14T00:00:00"), voc_tags_json="[]")

    with patch("app.services.voc_classifier.classify_ticket", new_callable=AsyncMock) as mock_classify:
        summary = await run_backfill(
            since="2026-07-13", limit=100, concurrency=2, only_empty=True, execute=False,
        )
    mock_classify.assert_not_called()
    assert summary["mode"] == "dry-run"
    assert summary["scanned"] == 1

    from app.db.database import get_voc_tags, get_analyses_for_voc_backfill
    # No analysis row got touched.
    rows = await get_analyses_for_voc_backfill(since="2026-07-13", limit=100, only_empty=False)
    assert rows[0]["analysis_id"]  # sanity: row exists
    async with wired_db() as s:
        from app.db.database import AnalysisRecord
        rec = await s.get(AnalysisRecord, rows[0]["analysis_id"])
        assert rec.voc_tags_json == "[]"


async def test_execute_writes_tags_and_second_run_is_idempotent(wired_db):
    await seed_issue(wired_db, issue_id="i1", category="hardware")
    await seed_analysis(wired_db, task_id="t1", issue_id="i1",
                         created_at=datetime.fromisoformat("2026-07-14T00:00:00"), voc_tags_json="[]")

    with patch("app.services.voc_classifier.classify_ticket", new_callable=AsyncMock, return_value=_fake_tags()):
        summary1 = await run_backfill(
            since="2026-07-13", limit=100, concurrency=2, only_empty=True, execute=True,
        )
    assert summary1["mode"] == "execute"
    assert summary1["scanned"] == 1
    assert summary1["classified"] == 1

    from app.db.database import AnalysisRecord
    async with wired_db() as s:
        from sqlalchemy import select
        row = (await s.execute(select(AnalysisRecord))).scalars().first()
        stored = json.loads(row.voc_tags_json)
        assert stored[0]["tag_id"] == "ai-01"

    # Second run with only_empty=True (the default) must find nothing left to do —
    # this is the idempotency guarantee the docstring promises.
    with patch("app.services.voc_classifier.classify_ticket", new_callable=AsyncMock) as mock_classify2:
        summary2 = await run_backfill(
            since="2026-07-13", limit=100, concurrency=2, only_empty=True, execute=True,
        )
    mock_classify2.assert_not_called()
    assert summary2["scanned"] == 0


async def test_since_filter_excludes_older_rows(wired_db):
    await seed_issue(wired_db, issue_id="old1", category="hardware")
    await seed_analysis(wired_db, task_id="t-old", issue_id="old1",
                         created_at=datetime.fromisoformat("2026-06-01T00:00:00"), voc_tags_json="[]")

    with patch("app.services.voc_classifier.classify_ticket", new_callable=AsyncMock) as mock_classify:
        summary = await run_backfill(
            since="2026-07-13", limit=100, concurrency=2, only_empty=True, execute=False,
        )
    mock_classify.assert_not_called()
    assert summary["scanned"] == 0


async def test_include_tagged_reprocesses_already_tagged_rows(wired_db):
    await seed_issue(wired_db, issue_id="i1", category="hardware")
    await seed_analysis(wired_db, task_id="t1", issue_id="i1",
                         created_at=datetime.fromisoformat("2026-07-14T00:00:00"),
                         voc_tags_json=json.dumps(_fake_tags()))

    with patch("app.services.voc_classifier.classify_ticket", new_callable=AsyncMock, return_value=_fake_tags()) as mock_classify:
        summary = await run_backfill(
            since="2026-07-13", limit=100, concurrency=2, only_empty=False, execute=True,
        )
    mock_classify.assert_called_once()
    assert summary["scanned"] == 1
    assert summary["classified"] == 1
