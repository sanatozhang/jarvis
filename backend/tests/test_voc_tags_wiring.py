"""Tests for the voc_tags wiring added to app.agents.base (_safe_voc_tags,
consumed by parse_result) and app.db.database.save_analysis (writes
voc_tags_json only when the AI provided it — no LLM fallback in the hot path)."""
from __future__ import annotations

import json

from app.services import voc_taxonomy

ACTIVE_TAGS = [
    {
        "id": "ai-01", "level_1_category": "蓝牙连接", "level_2_label": "配对失败",
        "level_3_diagnosis": "Token不匹配", "definition": "本地 token 与云端不一致",
        "positive_examples": [], "mece_rules": [], "negative_examples": [],
        "updated_by": "", "retired": False,
    },
]


def test_safe_voc_tags_valid(monkeypatch):
    from app.agents.base import _safe_voc_tags
    monkeypatch.setattr(voc_taxonomy, "active_tags", lambda: ACTIVE_TAGS)
    result = _safe_voc_tags([{"tag_id": "ai-01", "role": "primary", "confidence": "high", "reason": "matches"}])
    assert len(result) == 1
    assert result[0].tag_id == "ai-01"
    assert result[0].role == "primary"


def test_safe_voc_tags_unknown_tag_dropped(monkeypatch):
    from app.agents.base import _safe_voc_tags
    monkeypatch.setattr(voc_taxonomy, "active_tags", lambda: ACTIVE_TAGS)
    result = _safe_voc_tags([{"tag_id": "hallucinated", "role": "primary"}])
    assert result == []


def test_safe_voc_tags_not_a_list_returns_empty():
    from app.agents.base import _safe_voc_tags
    assert _safe_voc_tags("not a list") == []
    assert _safe_voc_tags(None) == []


async def test_save_analysis_persists_ai_provided_voc_tags(db_engine, db_session):
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        record = await db_mod.save_analysis({
            "task_id": "t1", "issue_id": "i1",
            "problem_type": "蓝牙连接失败", "root_cause": "token mismatch",
            "voc_tags": [{"tag_id": "ai-01", "level_1_category": "蓝牙连接",
                          "role": "primary", "confidence": "high", "reason": "x"}],
        })
        assert json.loads(record.voc_tags_json)[0]["tag_id"] == "ai-01"
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_save_analysis_leaves_voc_tags_empty_when_ai_omits_it(db_engine, db_session):
    """No backend LLM fallback in the hot save_analysis path — an empty/missing
    voc_tags from the AI must stay empty, picked up later by the backfill script."""
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        record = await db_mod.save_analysis({
            "task_id": "t2", "issue_id": "i2",
            "problem_type": "录音丢失", "root_cause": "unknown",
        })
        assert json.loads(record.voc_tags_json) == []
        # And the OLD classification path still auto-classifies as before —
        # this change must not have touched that behavior.
        assert record.problem_categories_json != "[]"
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory
