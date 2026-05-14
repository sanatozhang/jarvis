"""Tests for ClaudeApiAgent agent loop with mocked HTTP client."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.agents.base import AgentConfig
from app.agents.claude_api import ClaudeApiAgent, _HttpError, _MessagesClient


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "main.log").write_text(
        "2026-05-13 10:00:05 BLE GATT connection timeout after 30s\n",
        encoding="utf-8",
    )
    (tmp_path / "output").mkdir()
    return tmp_path


@pytest.fixture()
def config() -> AgentConfig:
    return AgentConfig(
        agent_type="claude_api",
        model="claude-sonnet-4-6",
        fallback_model="claude-haiku-4-5",
        base_url="http://example.invalid/vertex",
        api_key="test-key",
        per_turn_timeout=10,
        max_tokens=4096,
        max_turns=10,
        enable_cache=True,
    )


class _FakeClient:
    """Replays a scripted sequence of API responses; records request bodies."""

    def __init__(self, responses: List[Any]):
        self._responses = list(responses)
        self.requests: List[Dict[str, Any]] = []

    async def create_message(self, body: Dict[str, Any]) -> Dict[str, Any]:
        # Deep copy: the agent mutates `messages` between turns and reuses
        # the same body dict — without a snapshot we'd see the final state
        # in every recorded request.
        self.requests.append(copy.deepcopy(body))
        if not self._responses:
            raise AssertionError("FakeClient ran out of scripted responses")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def close(self) -> None:
        pass


def _install_fake(monkeypatch, fake: _FakeClient):
    monkeypatch.setattr(
        "app.agents.claude_api._MessagesClient",
        lambda *a, **kw: fake,
    )


class TestAgentLoop:
    async def test_writes_result_and_ends(self, workspace, config, monkeypatch):
        """Happy path: model calls write_file then end_turn."""
        fake = _FakeClient([
            # Turn 0: write result.json
            {
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "write_file",
                        "input": {
                            "path": "output/result.json",
                            "content": json.dumps({
                                "problem_type": "BLE Timeout",
                                "root_cause": "GATT connection timed out after 30 seconds during pairing.",
                                "confidence": "high",
                                "user_reply": "Hi, your device's Bluetooth pairing failed because the connection timed out.",
                            }),
                        },
                    }
                ],
            },
            # Turn 1: end_turn
            {
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 50, "output_tokens": 10},
                "content": [{"type": "text", "text": "Done."}],
            },
        ])
        _install_fake(monkeypatch, fake)

        agent = ClaudeApiAgent(config)
        result = await agent.analyze(workspace=workspace, prompt="Analyze this issue")

        assert result.agent_type == "claude_api"
        assert result.problem_type == "BLE Timeout"
        assert "GATT connection" in result.root_cause
        # Trace should have start + 2 turns + end = 4 lines
        trace = (workspace / "output" / "agent_trace.jsonl").read_text().strip().splitlines()
        assert len(trace) == 4
        assert json.loads(trace[0])["event"] == "start"
        assert json.loads(trace[1])["stop_reason"] == "tool_use"
        assert json.loads(trace[2])["stop_reason"] == "end_turn"
        assert json.loads(trace[3])["event"] == "end"

    async def test_grep_then_write(self, workspace, config, monkeypatch):
        """Tool round-trip: grep returns matches, then model writes result."""
        fake = _FakeClient([
            {
                "stop_reason": "tool_use",
                "usage": {},
                "content": [{
                    "type": "tool_use", "id": "t1", "name": "grep",
                    "input": {"pattern": "BLE.*timeout", "path": "logs"},
                }],
            },
            {
                "stop_reason": "tool_use",
                "usage": {},
                "content": [{
                    "type": "tool_use", "id": "t2", "name": "write_file",
                    "input": {
                        "path": "output/result.json",
                        "content": '{"problem_type":"BLE","root_cause":"Connection timed out during pairing per logs.","confidence":"medium"}',
                    },
                }],
            },
            {
                "stop_reason": "end_turn", "usage": {},
                "content": [{"type": "text", "text": "Analysis complete."}],
            },
        ])
        _install_fake(monkeypatch, fake)

        agent = ClaudeApiAgent(config)
        result = await agent.analyze(workspace=workspace, prompt="prompt")

        # Verify the request sequence: 3 turns
        assert len(fake.requests) == 3
        # Second request must contain tool_result block from grep
        msgs = fake.requests[1]["messages"]
        last = msgs[-1]
        assert last["role"] == "user"
        assert any(b.get("type") == "tool_result" for b in last["content"])
        assert result.problem_type == "BLE"

    async def test_rate_limit_returns_quota_result(self, workspace, config, monkeypatch):
        fake = _FakeClient([_HttpError(429, '{"error":{"type":"rate_limit_error","message":"hit limit"}}')])
        _install_fake(monkeypatch, fake)

        agent = ClaudeApiAgent(config)
        result = await agent.analyze(workspace=workspace, prompt="x")
        assert result.problem_type == "Claude API Quota Exhausted"
        assert "rate_limit" in result.root_cause or "hit limit" in result.root_cause

    async def test_overloaded_swaps_to_fallback_model(self, workspace, config, monkeypatch):
        fake = _FakeClient([
            _HttpError(529, '{"error":{"type":"overloaded_error","message":"overloaded"}}'),
            {
                "stop_reason": "tool_use",
                "usage": {},
                "content": [{
                    "type": "tool_use", "id": "t1", "name": "write_file",
                    "input": {"path": "output/result.json",
                              "content": '{"problem_type":"Test","root_cause":"Recovered via fallback model after overload.","confidence":"low"}'},
                }],
            },
            {"stop_reason": "end_turn", "usage": {}, "content": [{"type": "text", "text": ""}]},
        ])
        _install_fake(monkeypatch, fake)

        agent = ClaudeApiAgent(config)
        result = await agent.analyze(workspace=workspace, prompt="x")
        # Second request should use fallback model
        assert fake.requests[1]["model"] == "claude-haiku-4-5"
        assert result.problem_type == "Test"

    async def test_cache_control_on_first_block(self, workspace, config, monkeypatch):
        fake = _FakeClient([{"stop_reason": "end_turn", "usage": {}, "content": []}])
        _install_fake(monkeypatch, fake)

        agent = ClaudeApiAgent(config)
        await agent.analyze(workspace=workspace, prompt="hello")
        first_msg = fake.requests[0]["messages"][0]
        assert first_msg["role"] == "user"
        assert first_msg["content"][0]["cache_control"] == {"type": "ephemeral"}

    async def test_tool_error_propagates_to_model(self, workspace, config, monkeypatch):
        """Tool errors should produce is_error=true tool_result, not crash the loop."""
        fake = _FakeClient([
            {
                "stop_reason": "tool_use",
                "usage": {},
                "content": [{
                    "type": "tool_use", "id": "t1", "name": "read_file",
                    "input": {"path": "does_not_exist.log"},
                }],
            },
            {"stop_reason": "end_turn", "usage": {}, "content": [{"type": "text", "text": "I cannot find the file."}]},
        ])
        _install_fake(monkeypatch, fake)

        agent = ClaudeApiAgent(config)
        result = await agent.analyze(workspace=workspace, prompt="x")

        # Second request's last user message should have a tool_result with is_error
        msgs = fake.requests[1]["messages"]
        last_user = msgs[-1]
        tr = next(b for b in last_user["content"] if b.get("type") == "tool_result")
        assert tr.get("is_error") is True
        assert "not found" in tr["content"]
        # Loop should still produce a result (salvage path turns text into root_cause)
        assert result.agent_type == "claude_api"


class TestMessagesClientInit:
    def test_requires_base_url(self):
        with pytest.raises(ValueError, match="base_url"):
            _MessagesClient(base_url="", api_key="k", timeout=10)

    def test_requires_api_key(self):
        with pytest.raises(ValueError, match="api_key"):
            _MessagesClient(base_url="http://x", api_key="", timeout=10)
