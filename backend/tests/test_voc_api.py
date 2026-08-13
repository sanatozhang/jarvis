"""Tests for /api/voc endpoints (app.api.voc)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.services import voc_taxonomy
from app.services.voc_client import VocCredentialsMissing

ACTIVE_TAGS = [
    {
        "id": "ai-01", "level_1_category": "蓝牙连接", "level_2_label": "配对失败",
        "level_3_diagnosis": "Token不匹配", "definition": "本地 token 与云端不一致",
        "positive_examples": [], "mece_rules": [], "negative_examples": [],
        "updated_by": "", "retired": False,
    },
    {
        "id": "ai-02", "level_1_category": "蓝牙连接", "level_2_label": "配对失败",
        "level_3_diagnosis": "配对超时", "definition": "配对流程超过 30s 无响应",
        "positive_examples": [], "mece_rules": [], "negative_examples": [],
        "updated_by": "", "retired": False,
    },
]


async def test_get_taxonomy_builds_tree(client, monkeypatch):
    monkeypatch.setattr(voc_taxonomy, "active_tags", lambda: ACTIVE_TAGS)
    resp = await client.get("/api/voc/taxonomy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_active_tags"] == 2
    assert len(body["tree"]) == 1  # both tags share level_1_category "蓝牙连接"
    group = body["tree"][0]
    assert group["group"] == "蓝牙连接"
    assert len(group["labels"]) == 1  # both share level_2_label "配对失败"
    diagnoses = group["labels"][0]["diagnoses"]
    assert {d["tag_id"] for d in diagnoses} == {"ai-01", "ai-02"}


async def test_get_taxonomy_empty(client, monkeypatch):
    monkeypatch.setattr(voc_taxonomy, "active_tags", lambda: [])
    monkeypatch.setattr(voc_taxonomy, "load_seed", lambda: {})
    resp = await client.get("/api/voc/taxonomy")
    assert resp.status_code == 200
    assert resp.json() == {
        "total_active_tags": 0, "tree": [], "seed_fetched_at": "", "seed_tag_count": 0,
    }


async def test_sync_taxonomy_missing_credentials_returns_412(client):
    with patch("app.services.voc_taxonomy.sync_from_voc", new_callable=AsyncMock,
               side_effect=VocCredentialsMissing("no creds")):
        resp = await client.post("/api/voc/taxonomy/sync")
    assert resp.status_code == 412


async def test_sync_taxonomy_success(client):
    with patch("app.services.voc_taxonomy.sync_from_voc", new_callable=AsyncMock,
               return_value={"added": ["ai-01"], "changed": [], "retired": []}):
        resp = await client.post("/api/voc/taxonomy/sync")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["added"] == ["ai-01"]


async def test_classification_stats(client, db_session):
    from app.db.database import upsert_voc_tags, IssueRecord, AnalysisRecord
    import app.db.database as db_mod
    import json

    async with db_session() as s:
        s.add(IssueRecord(id="i1", description="蓝牙配对失败", category="hardware"))
        s.add(AnalysisRecord(
            task_id="t1", issue_id="i1",
            voc_tags_json=json.dumps([{
                "tag_id": "ai-01", "level_1_category": "蓝牙连接", "level_2_label": "配对失败",
                "level_3_diagnosis": "Token不匹配", "role": "primary", "confidence": "high", "reason": "x",
            }]),
        ))
        await s.commit()

    resp = await client.get("/api/voc/classification-stats", params={"days": 30})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_tagged"] == 1
    assert body["groups"][0]["group"] == "蓝牙连接"
    assert body["groups"][0]["count"] == 1


async def _seed_voc_row(db_session, issue_id, created_at, group="蓝牙连接", label="配对失败"):
    from app.db.database import IssueRecord, AnalysisRecord
    import json as _json
    async with db_session() as s:
        s.add(IssueRecord(id=issue_id, description="desc", category="hardware"))
        s.add(AnalysisRecord(
            task_id=f"t-{issue_id}", issue_id=issue_id, created_at=created_at,
            voc_tags_json=_json.dumps([{
                "tag_id": "ai-01", "level_1_category": group, "level_2_label": label,
                "level_3_diagnosis": "", "role": "primary", "confidence": "high", "reason": "x",
            }]),
        ))
        await s.commit()


async def test_get_trend_groups_by_date_and_level(client, db_session):
    from datetime import datetime
    await _seed_voc_row(db_session, "i1", datetime.utcnow())
    resp = await client.get("/api/voc/trend", params={"days": 7, "level": "group"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["level"] == "group"
    assert any("蓝牙连接" in day for day in body["trend"].values())


async def test_get_trend_rejects_invalid_level(client):
    resp = await client.get("/api/voc/trend", params={"level": "diagnosis"})
    assert resp.status_code == 422


async def test_get_movers_min_base_filters_noise(client, db_session):
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    for i in range(3):
        await _seed_voc_row(db_session, f"cur{i}", now, group="A")
    await _seed_voc_row(db_session, "prev0", now - timedelta(days=8), group="A")

    resp = await client.get("/api/voc/movers", params={"days": 7, "level": "group", "min_base": 5})
    assert resp.status_code == 200
    assert resp.json()["movers"] == []  # 1 -> 3 filtered out by min_base=5

    resp2 = await client.get("/api/voc/movers", params={"days": 7, "level": "group", "min_base": 1})
    movers = resp2.json()["movers"]
    assert movers[0]["key"] == "A"
    assert movers[0]["cur"] == 3
    assert movers[0]["prev"] == 1


async def test_get_taxonomy_includes_seed_metadata(client, monkeypatch):
    monkeypatch.setattr(voc_taxonomy, "active_tags", lambda: ACTIVE_TAGS)
    monkeypatch.setattr(voc_taxonomy, "load_seed", lambda: {"fetched_at": "2026-08-07", "tag_count": 158})
    resp = await client.get("/api/voc/taxonomy")
    body = resp.json()
    assert body["seed_fetched_at"] == "2026-08-07"
    assert body["seed_tag_count"] == 158


async def test_get_taxonomy_seed_metadata_missing_seed_file_is_empty(client, monkeypatch):
    monkeypatch.setattr(voc_taxonomy, "active_tags", lambda: [])
    monkeypatch.setattr(voc_taxonomy, "load_seed", lambda: {})
    resp = await client.get("/api/voc/taxonomy")
    body = resp.json()
    assert body["seed_fetched_at"] == ""
    assert body["seed_tag_count"] == 0


async def test_reseed_taxonomy_forces_upsert(client):
    from unittest.mock import AsyncMock, patch as _patch
    with _patch("app.services.voc_taxonomy.sync_seed_to_db", new_callable=AsyncMock,
                return_value={"added": ["ai-01"], "changed": [], "retired": [], "skipped": False}) as mock_sync:
        resp = await client.post("/api/voc/taxonomy/reseed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["added"] == ["ai-01"]
    mock_sync.assert_called_once_with(force=True)


async def test_get_weekly_digest_defaults_to_most_recent_complete_week(client):
    from unittest.mock import AsyncMock, patch as _patch
    with _patch("app.db.database.get_voc_weekly_digest", new_callable=AsyncMock, return_value=None) as mock_get:
        resp = await client.get("/api/voc/weekly-digest")
    assert resp.status_code == 200
    assert resp.json() is None
    assert mock_get.call_count == 1


async def test_generate_weekly_digest_endpoint(client):
    from unittest.mock import AsyncMock, patch as _patch
    fake_record = {"week_start": "2026-08-03", "stats": {}, "narrative": None, "markdown": "x"}
    with _patch("app.services.voc_digest.generate_weekly_digest", new_callable=AsyncMock,
                return_value=fake_record) as mock_gen:
        resp = await client.post("/api/voc/weekly-digest/generate", params={"week_start": "2026-08-03", "force": True})
    assert resp.status_code == 200
    assert resp.json()["week_start"] == "2026-08-03"
    mock_gen.assert_called_once_with("2026-08-03", force=True)


async def test_list_weekly_digests_endpoint(client):
    from unittest.mock import AsyncMock, patch as _patch
    with _patch("app.db.database.list_voc_weekly_digests", new_callable=AsyncMock,
                return_value=[{"week_start": "2026-08-03"}]):
        resp = await client.get("/api/voc/weekly-digests", params={"limit": 5})
    assert resp.status_code == 200
    assert resp.json()["digests"] == [{"week_start": "2026-08-03"}]


# ---------------------------------------------------------------------------
# week_start validation (malformed / non-Monday) — final-review fix
# ---------------------------------------------------------------------------

async def test_get_weekly_digest_malformed_week_start_returns_422(client):
    resp = await client.get("/api/voc/weekly-digest", params={"week_start": "not-a-date"})
    assert resp.status_code == 422


async def test_generate_weekly_digest_malformed_week_start_returns_422(client):
    resp = await client.post("/api/voc/weekly-digest/generate", params={"week_start": "not-a-date"})
    assert resp.status_code == 422


async def test_get_weekly_digest_non_monday_returns_422(client):
    # 2026-08-04 is a Tuesday
    resp = await client.get("/api/voc/weekly-digest", params={"week_start": "2026-08-04"})
    assert resp.status_code == 422


async def test_generate_weekly_digest_non_monday_returns_422(client):
    resp = await client.post("/api/voc/weekly-digest/generate", params={"week_start": "2026-08-04"})
    assert resp.status_code == 422


async def test_get_weekly_digest_invalid_calendar_date_returns_422(client):
    # Regex-shaped but not a real date — date.fromisoformat() would raise;
    # must be caught and turned into a 422, not surfaced as a 500.
    resp = await client.get("/api/voc/weekly-digest", params={"week_start": "2026-02-30"})
    assert resp.status_code == 422


async def test_get_weekly_digest_empty_week_start_uses_default(client):
    from unittest.mock import AsyncMock, patch as _patch
    with _patch("app.db.database.get_voc_weekly_digest", new_callable=AsyncMock, return_value=None) as mock_get:
        resp = await client.get("/api/voc/weekly-digest")
    assert resp.status_code == 200
    assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# date_from/date_to window support (analytics natural-week refactor)
# ---------------------------------------------------------------------------

async def test_trend_accepts_explicit_date_range(client, db_session):
    from datetime import datetime
    await _seed_voc_row(db_session, "i1", datetime.utcnow())
    resp = await client.get("/api/voc/trend", params={"date_from": "2020-01-01", "date_to": "2026-08-12", "level": "group"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["date_from"] == "2020-01-01"
    assert body["date_to"] == "2026-08-12"


async def test_classification_stats_only_one_of_date_from_to_is_422(client):
    resp = await client.get("/api/voc/classification-stats", params={"date_from": "2026-08-01"})
    assert resp.status_code == 422


async def test_movers_days_cap_unchanged_at_90(client):
    resp = await client.get("/api/voc/movers", params={"days": 91})
    assert resp.status_code == 422


async def test_movers_explicit_range_allows_365_days(client):
    resp = await client.get("/api/voc/movers", params={"date_from": "2025-08-13", "date_to": "2026-08-12"})
    assert resp.status_code == 200


async def test_movers_default_baseline_is_immediately_prior_same_length(client, db_session):
    resp = await client.get("/api/voc/movers", params={"date_from": "2026-08-06", "date_to": "2026-08-12"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["cur_from"] == "2026-08-06"
    assert body["prev_to"] == "2026-08-05"
    assert body["prev_from"] == "2026-07-30"  # 7-day span immediately before cur_from


async def test_movers_explicit_baseline_is_used_verbatim(client, db_session):
    resp = await client.get("/api/voc/movers", params={
        "date_from": "2026-08-10", "date_to": "2026-08-12",
        "prev_from": "2026-08-03", "prev_to": "2026-08-05",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["prev_from"] == "2026-08-03"
    assert body["prev_to"] == "2026-08-05"


async def test_movers_only_one_of_prev_from_to_is_422(client):
    resp = await client.get("/api/voc/movers", params={"prev_from": "2026-08-01"})
    assert resp.status_code == 422


async def test_generate_weekly_digest_omitted_week_start_uses_default(client):
    from unittest.mock import AsyncMock, patch as _patch
    fake_record = {"week_start": "2026-08-03", "stats": {}, "narrative": None, "markdown": "x"}
    with _patch("app.services.voc_digest.generate_weekly_digest", new_callable=AsyncMock,
                return_value=fake_record) as mock_gen:
        resp = await client.post("/api/voc/weekly-digest/generate")
    assert resp.status_code == 200
    assert mock_gen.call_count == 1
    # called with the resolved default (a Monday), not the empty string
    called_ws = mock_gen.call_args.args[0]
    from datetime import date as _date
    assert _date.fromisoformat(called_ws).weekday() == 0
