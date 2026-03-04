"""Tests for /api/rules endpoints."""
from unittest.mock import patch, MagicMock
from app.models.schemas import Rule, RuleMeta, RuleTrigger


def _make_mock_engine(rules=None):
    engine = MagicMock()
    _rules = {r.meta.id: r for r in (rules or [])}
    engine.list_rules.return_value = list(_rules.values())
    engine.get_rule.side_effect = lambda rid: _rules.get(rid)

    async def save_rule(rule):
        _rules[rule.meta.id] = rule
        engine.list_rules.return_value = list(_rules.values())
        return rule
    engine.save_rule = save_rule

    async def delete_rule(rid):
        if rid in _rules:
            del _rules[rid]
            engine.list_rules.return_value = list(_rules.values())
            return True
        return False
    engine.delete_rule = delete_rule
    engine.reload.return_value = None

    async def sync():
        pass
    engine.sync_files_to_db = sync
    engine.match_rules.return_value = list(_rules.values())
    return engine


async def test_create_and_list_rules(client):
    mock_engine = _make_mock_engine()
    with patch("app.api.rules._get_engine", return_value=mock_engine):
        resp = await client.post("/api/rules", json={
            "id": "test-rule", "name": "Test Rule",
            "triggers": {"keywords": ["bluetooth"], "priority": 5},
            "content": "# Test Rule",
        })
        assert resp.status_code == 200
        assert resp.json()["meta"]["id"] == "test-rule"
        resp = await client.get("/api/rules")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


async def test_get_rule_not_found(client):
    mock_engine = _make_mock_engine()
    with patch("app.api.rules._get_engine", return_value=mock_engine):
        resp = await client.get("/api/rules/nonexistent")
        assert resp.status_code == 404


async def test_create_duplicate_rule(client):
    existing = Rule(meta=RuleMeta(id="dup", name="Dup", triggers=RuleTrigger()), content="c")
    mock_engine = _make_mock_engine([existing])
    with patch("app.api.rules._get_engine", return_value=mock_engine):
        resp = await client.post("/api/rules", json={
            "id": "dup", "name": "Dup2",
            "triggers": {"keywords": [], "priority": 5}, "content": "x",
        })
        assert resp.status_code == 409


async def test_delete_rule(client):
    existing = Rule(meta=RuleMeta(id="to-delete", name="Del", triggers=RuleTrigger()), content="c")
    mock_engine = _make_mock_engine([existing])
    with patch("app.api.rules._get_engine", return_value=mock_engine):
        resp = await client.delete("/api/rules/to-delete")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "to-delete"


async def test_delete_rule_not_found(client):
    mock_engine = _make_mock_engine()
    with patch("app.api.rules._get_engine", return_value=mock_engine):
        resp = await client.delete("/api/rules/nope")
        assert resp.status_code == 404


async def test_reload_rules(client):
    mock_engine = _make_mock_engine()
    with patch("app.api.rules._get_engine", return_value=mock_engine):
        resp = await client.post("/api/rules/reload")
        assert resp.status_code == 200
        assert "reloaded" in resp.json()


async def test_test_rule_match(client):
    existing = Rule(meta=RuleMeta(id="bt", name="BT", triggers=RuleTrigger(keywords=["bluetooth"])), content="c")
    mock_engine = _make_mock_engine([existing])
    with patch("app.api.rules._get_engine", return_value=mock_engine):
        resp = await client.post("/api/rules/bt/test?description=bluetooth+issue")
        assert resp.status_code == 200
        assert "matched_rules" in resp.json()
