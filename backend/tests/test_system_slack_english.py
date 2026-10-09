"""系统私聊（system_notify 调用点）的 Slack 文本必须**全英文**（2026-10-09）。

每个调用点都给 `system_notify.send_text` 传 `text_en`，Slack 发它、飞书仍发
中文 `text`。这里 patch `send_text` 抓 `text_en` 做 CJK 扫描，并顺带钉住飞书
`text` 没被改成英文。站点反馈的「内容」是用户原文，照发不翻译——扫描前剔除。
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app import config as app_config
from app.services import db_health_monitor as mon
from app.services import db_health_state as state

CJK = re.compile(r"[　-〿一-鿿＀-￯]")


class _Capture:
    def __init__(self):
        self.calls = []

    async def __call__(self, email, text, *, text_en=""):
        self.calls.append({"email": email, "text": text, "text_en": text_en})
        return True


@pytest.fixture(autouse=True)
def _reset_state():
    state._io_error_times.clear()
    state._last_alert_at.clear()
    yield
    state._io_error_times.clear()
    state._last_alert_at.clear()


def _assert_english(text_en: str, zh_text: str):
    assert text_en, "missing text_en"
    assert not CJK.search(text_en), CJK.findall(text_en)
    assert CJK.search(zh_text), "飞书 text 应保持中文"


# ---------------------------------------------------------------------------
# release._notify_branch_created
# ---------------------------------------------------------------------------
async def test_release_branch_created_text_en():
    from app.api import release

    cap = _Capture()
    with patch.object(release.get_settings().jenkins, "notify_emails", []), \
         patch("app.services.system_notify.send_text", cap):
        await release._notify_branch_created(
            branch="release/4.1.0", creator="a@plaud.ai",
            commits={"app": "abcdef123456", "native": "0123456789"},
            version_after="4.1.0+410", source_branch="main",
        )
    assert len(cap.calls) == 1
    c = cap.calls[0]
    _assert_english(c["text_en"], c["text"])
    assert "New branch created: release/4.1.0" in c["text_en"]
    assert "Version: 4.1.0+410" in c["text_en"] and "abcdef12" in c["text_en"]


# ---------------------------------------------------------------------------
# site_feedback
# ---------------------------------------------------------------------------
async def test_site_feedback_text_en(client):
    cap = _Capture()
    with patch("app.services.system_notify.send_text", cap):
        resp = await client.post("/api/site-feedback", json={
            "message": "按钮点不动",
            "page_url": "http://x/tracking?detail=abc",
            "user_email": "u@plaud.ai",
        })
    assert resp.status_code == 200
    c = cap.calls[0]
    assert "Message: 按钮点不动" in c["text_en"]            # 用户原文照发
    labels = c["text_en"].replace("按钮点不动", "")
    _assert_english(labels, c["text"])
    assert "Submitted by: u@plaud.ai" in labels and "Page: http://x/tracking?detail=abc" in labels


async def test_site_feedback_slack_sends_english_labels(client, monkeypatch):
    monkeypatch.setattr(app_config.get_settings(), "system_notify_provider", "slack")
    post = AsyncMock(return_value="1.0")
    with patch("app.services.slack_cli.uid_for_email", AsyncMock(return_value="U1")), \
         patch("app.services.slack_cli.post_message", post):
        resp = await client.post("/api/site-feedback", json={"message": "hello"})
    assert resp.status_code == 200
    assert not CJK.search(post.call_args.kwargs["text"])


# ---------------------------------------------------------------------------
# db_health_monitor：三条告警
# ---------------------------------------------------------------------------
@pytest.fixture
def alerts(monkeypatch):
    sent = []

    async def _fake(text, text_en=""):
        sent.append((text, text_en))

    monkeypatch.setattr(mon, "_send_alert", _fake)
    return sent


async def test_db_io_error_alert_en(alerts):
    for _ in range(mon._IO_ERROR_THRESHOLD):
        state.record_io_error()
    await mon._check_io_error_frequency()
    zh, en = alerts[0]
    _assert_english(en, zh)
    assert "disk I/O errors" in en


async def test_db_integrity_alert_en(alerts, tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    db.write_bytes(b"")
    monkeypatch.setattr("app.db.database.get_sqlite_file_path", lambda: str(db))
    monkeypatch.setattr(mon, "_health_snapshot_dir", lambda: tmp_path / "snap")
    monkeypatch.setattr(mon, "_last_integrity_check_at", 0.0)
    monkeypatch.setattr(mon, "_snapshot_and_check_integrity",
                        lambda p, d: (False, "integrity_check failed: bad page"))
    await mon._check_integrity_and_snapshot()
    zh, en = alerts[0]
    _assert_english(en, zh)
    assert "integrity check failed: integrity_check failed: bad page" in en


async def test_db_journal_mode_alert_en(alerts, tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    db.write_bytes(b"")
    monkeypatch.setattr("app.db.database.get_sqlite_file_path", lambda: str(db))
    monkeypatch.setattr(mon, "_check_journal_mode_sync",
                        lambda p: (False, "journal_mode drifted to 'wal' (expected 'delete')"))
    await mon._check_journal_mode()
    zh, en = alerts[0]
    _assert_english(en, zh)
    assert "journal_mode drifted" in en


async def test_db_alert_slack_end_to_end_is_english(monkeypatch):
    """不 patch `_send_alert`：走真实 system_notify → Slack，发出去的必须是英文。"""
    monkeypatch.setattr(app_config.get_settings(), "system_notify_provider", "slack")
    for _ in range(mon._IO_ERROR_THRESHOLD):
        state.record_io_error()
    post = AsyncMock(return_value="1.0")
    with patch("app.services.slack_cli.uid_for_email", AsyncMock(return_value="U1")), \
         patch("app.services.slack_cli.post_message", post):
        await mon._check_io_error_frequency()
    assert not CJK.search(post.call_args.kwargs["text"])


def test_modulehub_notifier_messages_are_english():
    """modulehub 的 notify 文案本来就是英文（只核实，不改）。"""
    src = (Path(__file__).resolve().parents[1] / "app" / "modulehub" / "service.py").read_text("utf-8")
    msgs = re.findall(r'notifier\.notify\((.+?)\)\n', src)
    assert msgs
    for m in msgs:
        assert not CJK.search(m), m
