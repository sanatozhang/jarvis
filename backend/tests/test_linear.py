"""Tests for /api/linear webhook endpoints."""
import hashlib
import hmac
import json
from unittest.mock import patch, AsyncMock


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_webhook_no_trigger(client):
    payload = {
        "type": "Comment", "action": "create",
        "data": {"id": "c1", "issueId": "i1", "body": "Just a regular comment", "user": {"name": "Alice"}},
    }
    body = json.dumps(payload).encode()
    sig = _sign(body, "test-secret")
    resp = await client.post("/api/linear/webhook",
        content=body, headers={"Content-Type": "application/json", "Linear-Signature": sig},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_webhook_with_trigger(client):
    payload = {
        "type": "Comment", "action": "create",
        "data": {"id": "c2", "issueId": "i2", "body": "@ai-agent please analyze this", "user": {"name": "Bob"}},
    }
    body = json.dumps(payload).encode()
    sig = _sign(body, "test-secret")
    with patch("app.api.linear_webhook._run_linear_analysis", new_callable=AsyncMock):
        resp = await client.post("/api/linear/webhook",
            content=body, headers={"Content-Type": "application/json", "Linear-Signature": sig},
        )
    assert resp.status_code == 200


async def test_webhook_followup_trigger(client):
    """@ai-agent-followup routes to followup analysis, not initial analysis."""
    payload = {
        "type": "Comment", "action": "create",
        "data": {
            "id": "c3", "issueId": "i3",
            "body": "@ai-agent-followup why does the battery drain so fast?",
            "user": {"name": "Carol"},
        },
    }
    body = json.dumps(payload).encode()
    sig = _sign(body, "test-secret")
    with patch("app.api.linear_webhook._run_linear_analysis", new_callable=AsyncMock) as mock_run:
        resp = await client.post("/api/linear/webhook",
            content=body, headers={"Content-Type": "application/json", "Linear-Signature": sig},
        )
    assert resp.status_code == 200
    # Should be called with followup_question extracted from the comment
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args.kwargs
    assert "why does the battery drain so fast?" in call_kwargs["followup_question"]
    assert call_kwargs["trigger_user"] == "Carol"


async def test_webhook_followup_not_treated_as_initial(client):
    """@ai-agent-followup should NOT trigger as a regular @ai-agent analysis (no double dispatch)."""
    payload = {
        "type": "Comment", "action": "create",
        "data": {
            "id": "c4", "issueId": "i4",
            "body": "@ai-agent-followup what else can you tell me?",
            "user": {"name": "Dave"},
        },
    }
    body = json.dumps(payload).encode()
    sig = _sign(body, "test-secret")
    with patch("app.api.linear_webhook._run_linear_analysis", new_callable=AsyncMock) as mock_run:
        resp = await client.post("/api/linear/webhook",
            content=body, headers={"Content-Type": "application/json", "Linear-Signature": sig},
        )
    assert resp.status_code == 200
    # Must be called exactly once (not twice — not once for followup + once for initial)
    assert mock_run.call_count == 1


async def test_webhook_bad_signature(client):
    body = json.dumps({"type": "Comment", "action": "create", "data": {}}).encode()
    resp = await client.post("/api/linear/webhook",
        content=body, headers={"Content-Type": "application/json", "Linear-Signature": "bad"},
    )
    assert resp.status_code == 401
