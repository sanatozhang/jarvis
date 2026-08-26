"""Tests for /api/eval endpoints."""
from unittest.mock import patch, AsyncMock


async def test_create_dataset(client):
    resp = await client.post("/api/eval/datasets", json={
        "name": "test-dataset", "description": "Test", "sample_ids": [],
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "test-dataset"
    assert "id" in resp.json()


async def test_list_datasets(client):
    await client.post("/api/eval/datasets", json={"name": "ds1"})
    resp = await client.get("/api/eval/datasets")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_get_dataset_not_found(client):
    resp = await client.get("/api/eval/datasets/999")
    assert resp.status_code == 404


async def test_start_run_no_dataset(client):
    resp = await client.post("/api/eval/run", json={"dataset_id": 999})
    assert resp.status_code == 404


async def test_start_run(client):
    ds_resp = await client.post("/api/eval/datasets", json={"name": "run-ds"})
    ds_id = ds_resp.json()["id"]
    with patch("app.services.eval_runner.run_eval", new_callable=AsyncMock):
        resp = await client.post("/api/eval/run", json={"dataset_id": ds_id})
    assert resp.status_code == 200
    assert resp.json()["status"] == "started"


async def test_list_runs(client):
    resp = await client.get("/api/eval/runs")
    assert resp.status_code == 200


async def test_get_run_not_found(client):
    resp = await client.get("/api/eval/runs/999")
    assert resp.status_code == 404


async def test_run_eval_passes_issue_platform_to_rule_matching(client):
    """eval_runner.run_eval() must forward the sample's issue.platform into
    RuleEngine.match_rules() — previously dropped, so platform-scoped rules
    (e.g. backend/rules/mcp.md) never matched during eval replay."""
    from app.db import database as db
    from app.services import eval_runner
    from app.services.agent_orchestrator import AgentOrchestrator

    issue = await db.upsert_issue({
        "record_id": "eval_platform_test_issue",
        "description": "mcp 客户端连不上",
        "platform": "MCP",
        "source": "api",
    })
    sample = await db.add_golden_sample({
        "issue_id": issue.id,
        "description": "mcp 客户端连不上",
        "problem_type": "connection",
        "root_cause": "timeout",
        "confidence": "high",
    })
    dataset = await db.create_eval_dataset({"name": "platform-test-ds", "sample_ids": [sample.id]})
    run = await db.create_eval_run({"dataset_id": dataset.id, "config": {"use_issue_logs": True}})

    captured_platform = {}
    orig_match_rules = eval_runner.RuleEngine.match_rules

    def _spy_match_rules(self, description, platform="", **kwargs):
        captured_platform["value"] = platform
        return orig_match_rules(self, description, platform=platform, **kwargs)

    with patch.object(eval_runner.RuleEngine, "match_rules", _spy_match_rules), \
         patch.object(AgentOrchestrator, "run_analysis", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = type("R", (), {
            "problem_type": "connection", "root_cause": "timeout",
            "confidence": type("C", (), {"value": "high"})(), "user_reply": "",
        })()
        await eval_runner.run_eval(run.id)

    assert captured_platform.get("value") == "mcp"
