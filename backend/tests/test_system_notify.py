"""系统私聊出口（services/system_notify）+ 四个调用点在 Slack 下的行为。

飞书路径的"逐字节不变"由 test_site_feedback.py / test_db_health_monitor.py
原有用例兜着（它们 patch 的是 feishu_cli，默认 provider=feishu 仍然命中）。
这里只钉 Slack 路径和渠道选择本身。
"""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock, patch

import pytest

from app import config as app_config


def get_settings():
    """**调用时现查**，不要改成 `from app.config import get_settings`：
    `client` fixture patch 的是 `app.config.get_settings` 这个属性，早绑定的
    名字拿到的是真实单例，于是这里切了 provider、被测代码却看不见。"""
    return app_config.get_settings()
from app.services import system_notify


@pytest.fixture
def slack_system(monkeypatch):
    monkeypatch.setattr(get_settings(), "system_notify_provider", "slack")


# ---------------------------------------------------------------------------
# 渠道选择
# ---------------------------------------------------------------------------
def test_default_provider_is_feishu():
    assert system_notify.provider() == "feishu"


def test_invalid_provider_falls_back_instead_of_raising(monkeypatch, caplog):
    """运行期不抛：DB 健康告警本身可能正在报线上故障，抛了就丢了。"""
    monkeypatch.setattr(get_settings(), "system_notify_provider", "slak")
    assert system_notify.provider() == "feishu"
    assert "slak" in caplog.text


# ---------------------------------------------------------------------------
# send_text
# ---------------------------------------------------------------------------
async def test_send_text_slack_resolves_email(slack_system):
    post = AsyncMock(return_value="1.0")
    with patch("app.services.slack_cli.uid_for_email", AsyncMock(return_value="U1")), \
         patch("app.services.slack_cli.post_message", post):
        assert await system_notify.send_text("a@plaud.ai", "hi") is True
    assert post.call_args.args[0] == "U1"
    assert post.call_args.kwargs["text"] == "hi"


async def test_send_text_feishu_uses_email(monkeypatch):
    send = AsyncMock(return_value=True)
    with patch("app.services.feishu_cli.send_message", send):
        assert await system_notify.send_text("a@plaud.ai", "hi") is True
    send.assert_awaited_once_with(email="a@plaud.ai", text="hi")


async def test_send_text_never_raises(slack_system):
    with patch("app.services.slack_cli.uid_for_email", AsyncMock(side_effect=RuntimeError("boom"))):
        assert await system_notify.send_text("a@plaud.ai", "hi") is False


async def test_send_text_empty_email_is_false():
    assert await system_notify.send_text("", "hi") is False


# ---------------------------------------------------------------------------
# send_image
# ---------------------------------------------------------------------------
async def test_send_image_slack_opens_dm_before_upload(slack_system):
    """completeUploadExternal 拒收 U...，必须先 conversations.open 拿 D...。"""
    upload = AsyncMock(return_value="F1")
    with patch("app.services.slack_cli.uid_for_email", AsyncMock(return_value="U1")), \
         patch("app.services.slack_cli.open_dm", AsyncMock(return_value="D1")) as open_dm, \
         patch("app.services.slack_cli.upload_file", upload):
        assert await system_notify.send_image("a@plaud.ai", b"png", "s.png") is True
    open_dm.assert_awaited_once_with("U1")
    assert upload.call_args.args[:3] == ("D1", b"png", "s.png")


async def test_send_image_slack_unknown_user_is_false(slack_system):
    with patch("app.services.slack_cli.uid_for_email", AsyncMock(return_value="")), \
         patch("app.services.slack_cli.upload_file", AsyncMock()) as upload:
        assert await system_notify.send_image("x@plaud.ai", b"png") is False
    upload.assert_not_awaited()


# ---------------------------------------------------------------------------
# slack_cli.upload_file：getUploadURLExternal 必须走 GET
# ---------------------------------------------------------------------------
def test_get_upload_url_is_a_get_method():
    """用 JSON body 调会得到误导性的 `invalid_arguments`（Apollo 实测）。"""
    from app.services import slack_cli

    assert "files.getUploadURLExternal" in slack_cli._GET_METHODS


async def test_upload_file_three_steps():
    from app.services import slack_cli

    calls = []

    async def _api(method, **kw):
        calls.append((method, kw))
        if method == "files.getUploadURLExternal":
            return {"ok": True, "upload_url": "https://files.slack.test/u", "file_id": "F1"}
        return {"ok": True}

    class _Resp:
        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, files=None):
            calls.append(("upload", url))
            return _Resp()

    with patch.object(slack_cli, "slack_api", _api), \
         patch.object(slack_cli.httpx, "AsyncClient", _Client):
        assert await slack_cli.upload_file("D1", b"abc", "s.png") == "F1"
    assert [c[0] for c in calls] == ["files.getUploadURLExternal", "upload",
                                     "files.completeUploadExternal"]
    assert calls[0][1] == {"filename": "s.png", "length": 3}
    assert calls[2][1]["channel_id"] == "D1"


# ---------------------------------------------------------------------------
# 调用点：切到 slack 之后**不再碰 feishu_cli**
# ---------------------------------------------------------------------------
async def test_site_feedback_goes_to_slack(client, monkeypatch):
    # client fixture 会换一个新的 Settings 单例，必须在它之后再切
    monkeypatch.setattr(get_settings(), "system_notify_provider", "slack")
    png_b64 = "data:image/png;base64," + base64.b64encode(b"\x89PNGfake").decode()
    post = AsyncMock(return_value="1.0")
    upload = AsyncMock(return_value="F1")
    with patch("app.services.slack_cli.uid_for_email", AsyncMock(return_value="U1")), \
         patch("app.services.slack_cli.open_dm", AsyncMock(return_value="D1")), \
         patch("app.services.slack_cli.post_message", post), \
         patch("app.services.slack_cli.upload_file", upload), \
         patch("app.services.feishu_cli.send_message", AsyncMock()) as feishu_send:
        resp = await client.post("/api/site-feedback", json={
            "message": "按钮点不动", "screenshot": png_b64,
        })
    assert resp.status_code == 200
    assert resp.json() == {"status": "sent", "image_sent": True}
    assert "按钮点不动" in post.call_args.kwargs["text"]
    upload.assert_awaited_once()
    feishu_send.assert_not_awaited()


async def test_site_feedback_502_names_the_provider(client, monkeypatch):
    # client fixture 会换一个新的 Settings 单例，必须在它之后再切
    monkeypatch.setattr(get_settings(), "system_notify_provider", "slack")
    with patch("app.services.slack_cli.uid_for_email", AsyncMock(return_value="")):
        resp = await client.post("/api/site-feedback", json={"message": "x"})
    assert resp.status_code == 502
    assert "slack" in resp.json()["detail"]


async def test_db_health_alert_goes_to_slack(slack_system):
    from app.services import db_health_monitor

    post = AsyncMock(return_value="1.0")
    with patch("app.services.slack_cli.uid_for_email", AsyncMock(return_value="U1")), \
         patch("app.services.slack_cli.post_message", post):
        await db_health_monitor._send_alert("db 坏了")
    assert post.call_args.kwargs["text"] == "db 坏了"


async def test_release_notify_goes_to_slack_per_recipient(slack_system, monkeypatch):
    from app.api import release

    # 改 release 模块**自己**看到的那个 settings：它若是在某个 client fixture
    # 的 patch 期间首次被 import，`release.get_settings` 会早绑定成那个 mock。
    monkeypatch.setattr(release.get_settings().jenkins, "notify_emails", ["b@plaud.ai"])
    sent = []

    async def _send(email, text):
        sent.append(email)
        return email != "a@plaud.ai"      # 第一个人失败不能影响第二个

    with patch("app.services.system_notify.send_text", _send):
        await release._notify_branch_created(
            branch="release/4.1.0", creator="a@plaud.ai", commits={"app": "abcdef123"})
    assert sent == ["a@plaud.ai", "b@plaud.ai"]
