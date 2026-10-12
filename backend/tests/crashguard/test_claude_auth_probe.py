"""容器内 claude 登录态探测 + 告警（2026-10-12）。

背景：登录失效时 AI 分析静默全挂、任务心跳仍正常，102 上曾静默 20 天。
"""
from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta

import pytest

from app.crashguard.services import claude_auth_probe as cap
# 模块导入发生在 autouse 桩之前，这里拿到的是真实现
from app.crashguard.services.claude_auth_probe import probe_claude_auth as _real_probe


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_send_alert(card, slack=None, *, s=None, what="alert"):
        calls.append({"card": card, "what": what})
        return True

    monkeypatch.setattr("app.crashguard.services.notify.send_alert", fake_send_alert)
    return calls


def _stub_probe(monkeypatch, *results):
    seq = list(results)
    calls = []

    async def fake(timeout: float = 0):
        calls.append(1)
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(cap, "probe_claude_auth", fake)
    return calls


def _expire_interval():
    cap._last_probe_at = datetime.utcnow() - timedelta(hours=1)


@pytest.mark.asyncio
async def test_auth_failure_alerts_immediately_with_recovery_command(monkeypatch, sent):
    _stub_probe(monkeypatch, (False, "auth", "Failed to authenticate: OAuth session expired"))
    res = await cap.check_claude_auth_and_alert()
    assert res["alerted"] is True and res["kind"] == "auth"
    assert len(sent) == 1 and sent[0]["what"] == "claude_auth_alert"
    card_text = str(sent[0]["card"])
    assert cap.RECOVERY_COMMAND in card_text
    assert "OAuth session expired" in card_text
    assert sent[0]["card"]["header"]["template"] == "red"


@pytest.mark.asyncio
async def test_non_auth_failure_needs_two_in_a_row(monkeypatch, sent):
    _stub_probe(monkeypatch, (False, "other", "timed out"))
    first = await cap.check_claude_auth_and_alert()
    assert first["alerted"] is False and sent == []
    _expire_interval()
    second = await cap.check_claude_auth_and_alert()
    assert second["alerted"] is True and len(sent) == 1
    assert sent[0]["card"]["header"]["template"] == "orange"


@pytest.mark.asyncio
async def test_probe_throttled_by_interval(monkeypatch, sent):
    calls = _stub_probe(monkeypatch, (True, "", "OK"))
    await cap.check_claude_auth_and_alert()
    res = await cap.check_claude_auth_and_alert()
    assert res == {"skipped": "interval"}
    assert len(calls) == 1  # 间隔内不重复真调 CLI


@pytest.mark.asyncio
async def test_repeat_alert_throttled_by_cooldown(monkeypatch, sent):
    _stub_probe(monkeypatch, (False, "auth", "401"))
    await cap.check_claude_auth_and_alert()
    _expire_interval()  # 探测间隔已过，但告警冷却（默认 120min）未过
    res = await cap.check_claude_auth_and_alert()
    assert res.get("throttled") is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_recovery_resets_so_next_failure_alerts_again(monkeypatch, sent):
    _stub_probe(
        monkeypatch,
        (False, "auth", "401"), (True, "", "OK"), (False, "auth", "401"),
    )
    await cap.check_claude_auth_and_alert()
    _expire_interval()
    ok = await cap.check_claude_auth_and_alert()
    assert ok == {"ok": True} and len(sent) == 1  # 恢复不发卡
    _expire_interval()
    await cap.check_claude_auth_and_alert()
    assert len(sent) == 2  # 恢复后再挂，不受上一轮冷却影响


@pytest.mark.asyncio
async def test_disabled_does_nothing(monkeypatch, sent):
    calls = _stub_probe(monkeypatch, (False, "auth", "401"))
    from app.crashguard import config as cfg
    real = cfg.get_crashguard_settings()
    monkeypatch.setattr(
        cap, "get_crashguard_settings",
        lambda: real.model_copy(update={"claude_auth_probe_enabled": False}),
    )
    assert await cap.check_claude_auth_and_alert() == {"skipped": "disabled"}
    assert calls == [] and sent == []


# ── 真实子进程路径：用假 claude 脚本放进 PATH ──────────────────────────

def _fake_claude(tmp_path, monkeypatch, body: str):
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")


@pytest.mark.asyncio
async def test_real_probe_detects_oauth_expiry(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch,
                 'echo "Failed to authenticate: OAuth session expired and could not be refreshed"; exit 1')
    ok, kind, detail = await _real_probe(timeout=10)
    assert ok is False and kind == "auth"
    assert "OAuth session expired" in detail


@pytest.mark.asyncio
async def test_real_probe_success(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, 'echo OK')
    ok, kind, _ = await _real_probe(timeout=10)
    assert ok is True and kind == ""


@pytest.mark.asyncio
async def test_real_probe_nonzero_exit_without_output_is_other(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, 'exit 3')
    ok, kind, detail = await _real_probe(timeout=10)
    assert ok is False and kind == "other"
    assert "exit code 3" in detail


@pytest.mark.asyncio
async def test_real_probe_timeout(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, 'sleep 5')
    ok, kind, detail = await _real_probe(timeout=0.5)
    assert ok is False and kind == "other" and "timed out" in detail
