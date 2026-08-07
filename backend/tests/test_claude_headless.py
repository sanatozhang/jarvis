"""Tests for app.services.claude_headless.run_json — the shared subprocess
caller extracted from voc_classifier.classify_ticket(). Mocks
asyncio.create_subprocess_exec directly (no real CLI binary needed)."""
from __future__ import annotations

import asyncio
import json as _json

import pytest

from app.services import claude_headless


class _FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, delay=0.0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._delay = delay
        self.killed = False

    async def communicate(self, input=None):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


_next_process: "_FakeProcess | None" = None
_raise_not_found = False


async def _fake_create_subprocess_exec(*args, **kwargs):
    if _raise_not_found:
        raise FileNotFoundError("claude: command not found")
    return _next_process


def _set_fake_process(proc, raise_not_found: bool = False):
    global _next_process, _raise_not_found
    _next_process = proc
    _raise_not_found = raise_not_found


def _envelope(result_obj) -> bytes:
    return _json.dumps({
        "type": "result", "is_error": False, "stop_reason": "tool_use",
        "result": _json.dumps(result_obj, ensure_ascii=False),
    }).encode("utf-8")


@pytest.fixture(autouse=True)
def patched_subprocess(monkeypatch):
    monkeypatch.setattr(claude_headless, "_make_cli_env", lambda: {})
    monkeypatch.setattr(claude_headless.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    _set_fake_process(_FakeProcess())
    yield
    _set_fake_process(_FakeProcess())


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


async def test_run_json_success():
    _set_fake_process(_FakeProcess(stdout=_envelope({"ok": True})))
    result = await claude_headless.run_json(
        system_prompt="sys", user_input="hi", schema=SCHEMA, model="claude-test", timeout=5,
    )
    assert result == {"ok": True}


async def test_run_json_non_json_output_returns_none():
    _set_fake_process(_FakeProcess(stdout=b"not json"))
    result = await claude_headless.run_json(
        system_prompt="sys", user_input="hi", schema=SCHEMA, model="claude-test", timeout=5,
    )
    assert result is None


async def test_run_json_non_dict_json_returns_none():
    _set_fake_process(_FakeProcess(stdout=_envelope([1, 2, 3])))
    result = await claude_headless.run_json(
        system_prompt="sys", user_input="hi", schema=SCHEMA, model="claude-test", timeout=5,
    )
    assert result is None


async def test_run_json_nonzero_exit_returns_none():
    _set_fake_process(_FakeProcess(stdout=b"", stderr=b"boom", returncode=1))
    result = await claude_headless.run_json(
        system_prompt="sys", user_input="hi", schema=SCHEMA, model="claude-test", timeout=5,
    )
    assert result is None


async def test_run_json_timeout_kills_process_and_returns_none():
    _set_fake_process(_FakeProcess(delay=999))
    result = await claude_headless.run_json(
        system_prompt="sys", user_input="hi", schema=SCHEMA, model="claude-test", timeout=0.05,
    )
    assert result is None
    assert _next_process.killed is True


async def test_run_json_cli_not_found_returns_none():
    _set_fake_process(None, raise_not_found=True)
    result = await claude_headless.run_json(
        system_prompt="sys", user_input="hi", schema=SCHEMA, model="claude-test", timeout=5,
    )
    assert result is None
