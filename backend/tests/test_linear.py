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


async def test_webhook_bad_signature(client):
    body = json.dumps({"type": "Comment", "action": "create", "data": {}}).encode()
    resp = await client.post("/api/linear/webhook",
        content=body, headers={"Content-Type": "application/json", "Linear-Signature": "bad"},
    )
    assert resp.status_code == 401
