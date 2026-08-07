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
    resp = await client.get("/api/voc/taxonomy")
    assert resp.status_code == 200
    assert resp.json() == {"total_active_tags": 0, "tree": []}


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
