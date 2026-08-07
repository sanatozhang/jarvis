"""Tests for VOC taxonomy DB CRUD (app.db.database) and the seed/cache layer
(app.services.voc_taxonomy)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest


def _tag(id_="ai-01", **overrides):
    base = {
        "id": id_,
        "level_1_category": "蓝牙连接",
        "level_2_label": "配对失败",
        "level_3_diagnosis": "Token不匹配",
        "definition": "设备配对时本地 token 与云端不一致",
        "positive_examples": ["配对一直失败提示 token mismatch"],
        "mece_rules": [{"distinct_from": "ai-02", "reason": "ai-02 是连接后断开，不是配对阶段"}],
        "negative_examples": [{"example": "配对成功后录音丢失", "redirect_to": "ai-03"}],
        "updated_by": "voc-team",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# app.db.database.upsert_voc_tags / get_voc_tags
# ---------------------------------------------------------------------------

async def test_upsert_voc_tags_added(db_engine, db_session):
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        diff = await db_mod.upsert_voc_tags([_tag("ai-01"), _tag("ai-02", level_1_category="固件升级")])
        assert diff["added"] == ["ai-01", "ai-02"]
        assert diff["changed"] == []
        assert diff["retired"] == []

        tags = await db_mod.get_voc_tags()
        assert {t["id"] for t in tags} == {"ai-01", "ai-02"}
        one = next(t for t in tags if t["id"] == "ai-01")
        assert one["definition"] == "设备配对时本地 token 与云端不一致"
        assert one["mece_rules"] == [{"distinct_from": "ai-02", "reason": "ai-02 是连接后断开，不是配对阶段"}]
        assert one["retired"] is False
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_upsert_voc_tags_idempotent_second_run_reports_no_changes(db_engine, db_session):
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        snapshot = [_tag("ai-01"), _tag("ai-02")]
        await db_mod.upsert_voc_tags(snapshot)
        diff2 = await db_mod.upsert_voc_tags(snapshot)  # identical snapshot again
        assert diff2 == {"added": [], "changed": [], "retired": []}
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_upsert_voc_tags_detects_changed_field(db_engine, db_session):
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        await db_mod.upsert_voc_tags([_tag("ai-01", definition="旧定义")])
        diff = await db_mod.upsert_voc_tags([_tag("ai-01", definition="新定义")])
        assert diff["added"] == []
        assert diff["changed"] == ["ai-01"]
        assert diff["retired"] == []
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_upsert_voc_tags_retires_missing_ids(db_engine, db_session):
    """VOC's /api/taxonomy/tags only returns active tags — a tag id present in
    DB but absent from a new snapshot must be marked retired, not deleted."""
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        await db_mod.upsert_voc_tags([_tag("ai-01"), _tag("ai-02")])
        diff = await db_mod.upsert_voc_tags([_tag("ai-01")])  # ai-02 no longer in the snapshot
        assert diff["retired"] == ["ai-02"]

        active_only = await db_mod.get_voc_tags(include_retired=False)
        assert {t["id"] for t in active_only} == {"ai-01"}

        with_retired = await db_mod.get_voc_tags(include_retired=True)
        assert {t["id"] for t in with_retired} == {"ai-01", "ai-02"}
        retired_row = next(t for t in with_retired if t["id"] == "ai-02")
        assert retired_row["retired"] is True

        # Re-retiring the same tag on a subsequent run must not show up as a
        # fresh "retired" diff entry every time — idempotent from here on.
        diff2 = await db_mod.upsert_voc_tags([_tag("ai-01")])
        assert diff2["retired"] == []
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_upsert_voc_tags_reactivating_a_retired_tag_clears_retired(db_engine, db_session):
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        await db_mod.upsert_voc_tags([_tag("ai-01"), _tag("ai-02")])
        await db_mod.upsert_voc_tags([_tag("ai-01")])  # retires ai-02
        diff = await db_mod.upsert_voc_tags([_tag("ai-01"), _tag("ai-02")])  # ai-02 comes back
        assert diff["changed"] == ["ai-02"]  # retired flag flipping counts as a change
        active_only = await db_mod.get_voc_tags(include_retired=False)
        assert {t["id"] for t in active_only} == {"ai-01", "ai-02"}
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


# ---------------------------------------------------------------------------
# app.services.voc_taxonomy — seed loading, bootstrap, cache
# ---------------------------------------------------------------------------

async def test_sync_seed_to_db_skips_when_db_already_has_tags(db_engine, db_session):
    import app.db.database as db_mod
    from app.services import voc_taxonomy
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        await db_mod.upsert_voc_tags([_tag("ai-01")])
        with patch.object(voc_taxonomy, "load_seed", return_value={"tags": [_tag("ai-99")]}):
            seeded = await voc_taxonomy.sync_seed_to_db()
        assert seeded == 0
        tags = await db_mod.get_voc_tags()
        assert {t["id"] for t in tags} == {"ai-01"}  # seed's ai-99 was NOT inserted
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_sync_seed_to_db_bootstraps_empty_db(db_engine, db_session):
    import app.db.database as db_mod
    from app.services import voc_taxonomy
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        with patch.object(voc_taxonomy, "load_seed", return_value={"tags": [_tag("ai-01"), _tag("ai-02")]}):
            seeded = await voc_taxonomy.sync_seed_to_db()
        assert seeded == 2
        tags = await db_mod.get_voc_tags()
        assert {t["id"] for t in tags} == {"ai-01", "ai-02"}
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_sync_seed_to_db_no_seed_file_is_a_noop(db_engine, db_session):
    import app.db.database as db_mod
    from app.services import voc_taxonomy
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        with patch.object(voc_taxonomy, "load_seed", return_value={}):
            seeded = await voc_taxonomy.sync_seed_to_db()
        assert seeded == 0
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_reload_from_db_and_active_tags_cache(db_engine, db_session):
    import app.db.database as db_mod
    from app.services import voc_taxonomy
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        await db_mod.upsert_voc_tags([_tag("ai-01"), _tag("ai-02")])
        await db_mod.upsert_voc_tags([_tag("ai-01")])  # retires ai-02
        await voc_taxonomy.reload_from_db()
        active = voc_taxonomy.active_tags()
        assert {t["id"] for t in active} == {"ai-01"}  # retired tag excluded from the cache
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


def test_to_prompt_payload_shape():
    from app.services import voc_taxonomy
    tags = [_tag("ai-01")]
    payload = voc_taxonomy.to_prompt_payload(tags)
    assert payload["tags"][0]["id"] == "ai-01"
    assert payload["tags"][0]["path"] == ["蓝牙连接", "配对失败", "Token不匹配"]
    assert payload["tags"][0]["definition"] == _tag("ai-01")["definition"]
    assert "instructions" in payload


async def test_sync_from_voc_upserts_and_reloads(db_engine, db_session):
    import app.db.database as db_mod
    from app.services import voc_taxonomy
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        with patch("app.services.voc_client.fetch_taxonomy_tags", new_callable=AsyncMock,
                   return_value=[_tag("ai-01")]):
            diff = await voc_taxonomy.sync_from_voc()
        assert diff["added"] == ["ai-01"]
        assert {t["id"] for t in voc_taxonomy.active_tags()} == {"ai-01"}
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory
