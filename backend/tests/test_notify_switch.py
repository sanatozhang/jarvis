"""通知渠道开关（DB override + 热生效 + 启动回灌）。

重点覆盖三件迁移期最容易出错的事：
1. 切了之后**运行中的进程立刻生效**（不用重启）
2. 重启后**不会弹回 yaml 的值**（启动回灌）
3. env 钉死的模块不被界面上的开关影响
"""
from __future__ import annotations

import json

import pytest

from app.db import database as db
from app.services import notify_switch as ns


@pytest.fixture(autouse=True)
def _restore_settings():
    """这些测试直接改运行中的 settings 单例，必须还原，否则会污染后面的测试。"""
    saved = {m: (getattr(ns._settings_for(m), ns._provider_attr(m)),
                 getattr(ns._settings_for(m), "slack_channel", None))
             for m in ns.modules()}
    yield
    for m, (prov, ch) in saved.items():
        s = ns._settings_for(m)
        setattr(s, ns._provider_attr(m), prov)
        if ch is not None:
            s.slack_channel = ch


async def test_default_is_feishu_for_every_module(client):
    st = ns.status()
    assert {m["module"] for m in st["modules"]} == {"crashguard", "coreguard", "graygate", "system"}
    assert all(m["provider"] == "feishu" for m in st["modules"])
    assert st["implemented"] == ["feishu", "slack"]


async def test_switch_takes_effect_immediately_without_restart(client):
    """切换必须对**运行中的进程**生效。

    只写 DB 不改内存的话，界面显示已切换、而下一条告警还是发去旧渠道，
    要等重启才对上——这种不一致查起来很痛苦。
    """
    await ns.set_provider("graygate", "slack", slack_channel="C_gray")

    from app.graygate.services import notify as gnotify

    assert gnotify.report_target().provider == "slack"
    assert gnotify.report_target().channel == "C_gray"


async def test_override_survives_restart(client):
    """启动回灌。漏掉这步的表现是"切了、重启后偷偷弹回去"。"""
    await ns.set_provider("coreguard", "slack", slack_channel="C_core")

    # 模拟重启：把内存值改回 yaml 默认，再跑一次回灌
    s = ns._settings_for("coreguard")
    s.notify_provider = "feishu"
    s.slack_channel = ""

    await ns.apply_notify_overrides_from_db()
    assert s.notify_provider == "slack"
    assert s.slack_channel == "C_core"


async def test_switch_is_per_module(client):
    """模块粒度——切一个不能带动另外两个。这是整个设计的前提
    （graygate 先烤，crashguard 最后动）。"""
    await ns.set_provider("graygate", "slack", slack_channel="C_gray")
    st = {m["module"]: m["provider"] for m in ns.status()["modules"]}
    assert st == {"graygate": "slack", "coreguard": "feishu", "crashguard": "feishu",
                  "system": "feishu"}


async def test_unknown_provider_is_rejected(client):
    with pytest.raises(ValueError) as e:
        await ns.set_provider("graygate", "wechat")
    assert "wechat" in str(e.value)
    # 被拒之后不能留下半个 override
    raw = await db.get_oncall_config(ns.NOTIFY_OVERRIDE_KEY, "")
    assert "wechat" not in (raw or "")


async def test_unknown_module_is_rejected(client):
    with pytest.raises(ValueError):
        await ns.set_provider("nosuchmodule", "slack")


async def test_env_pinned_module_ignores_the_toggle(client, monkeypatch):
    """env 优先：被 env 钉死的机器不该被界面上的开关影响。

    否则界面上"看起来生效了"，重启后 env 又把它顶回去——比开关直接禁用
    难查得多。所以 status 里要有 `env_pinned` 让前端能禁用并说明原因。
    """
    monkeypatch.setenv("GRAYGATE_NOTIFY_PROVIDER", "feishu")
    s = ns._settings_for("graygate")
    s.notify_provider = "feishu"

    await ns.set_provider("graygate", "slack", slack_channel="C_gray")

    assert s.notify_provider == "feishu"        # 内存没被改
    assert ns.status_for("graygate")["env_pinned"] is True


# ---------------------------------------------------------------------------
# 体检表
# ---------------------------------------------------------------------------
async def test_slack_not_ready_without_token(client):
    """**只配频道不配 token 是最常见的半成品状态。**

    这种状态下切过去，发送会在 `_bot_token()` 抛 not_configured，被上游
    吞成"发送失败"——表现是告警静默消失。所以 ready.slack 必须同时看 token。
    """
    s = ns._settings_for("graygate")
    s.slack_channel = "C_gray"
    row = ns.status_for("graygate")
    # conftest 把 bot_token 桩成空串（安全边界，见那边注释）
    assert row["slack_token_configured"] is False
    assert row["ready"]["slack"] is False


async def test_feishu_ready_when_channel_configured(client):
    s = ns._settings_for("graygate")
    s.feishu_chat_id = "oc_gray"
    assert ns.status_for("graygate")["ready"]["feishu"] is True


async def test_status_exposes_both_channels_for_preflight(client):
    """体检表要同时给出两个渠道的地址，否则没法在切换前判断"切过去会不会空发"。"""
    row = ns.status_for("crashguard")
    assert set(row) >= {"provider", "feishu_channel", "slack_channel",
                        "alert_email", "ready", "env_pinned"}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
async def test_get_notify_endpoint(client):
    resp = await client.get("/api/settings/notify")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["modules"]) == 4
    assert body["implemented"] == ["feishu", "slack"]


async def test_put_notify_endpoint(client):
    resp = await client.put("/api/settings/notify", json={
        "module": "graygate", "provider": "slack", "slack_channel": "C_gray",
    })
    assert resp.status_code == 200
    assert resp.json()["provider"] == "slack"

    persisted = json.loads(await db.get_oncall_config(ns.NOTIFY_OVERRIDE_KEY, "{}"))
    assert persisted["graygate"]["provider"] == "slack"


async def test_put_notify_rejects_bad_provider_with_400(client):
    resp = await client.put("/api/settings/notify", json={
        "module": "graygate", "provider": "telegram",
    })
    assert resp.status_code == 400
    assert "telegram" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# system 行：发版通知 / modulehub / DB 健康 / 站内反馈这几处非模块私聊
# ---------------------------------------------------------------------------
async def test_system_switch_routes_system_notify(client):
    """切 system 行必须让 services/system_notify 立刻换渠道——这一行存在的
    全部意义就是让三个模块之外的私聊不再悄悄走飞书。"""
    from app.services import system_notify

    assert system_notify.provider() == "feishu"
    await ns.set_provider("system", "slack")
    assert system_notify.provider() == "slack"
    # 其他模块不受影响
    assert ns.status_for("crashguard")["provider"] == "feishu"


async def test_system_has_no_channel_and_ignores_slack_channel(client):
    """system 只有点对点，没有频道：传了 slack_channel 也不能落到顶层
    Settings 上（它没有这个字段，pydantic 会直接抛）。"""
    row = await ns.set_provider("system", "slack", slack_channel="C_should_be_ignored")
    assert row["has_channel"] is False
    assert row["slack_channel"] == ""
    raw = json.loads(await db.get_oncall_config(ns.NOTIFY_OVERRIDE_KEY, ""))
    assert "slack_channel" not in raw["system"]


async def test_system_override_survives_restart(client):
    await ns.set_provider("system", "slack")
    from app.config import get_settings

    get_settings().system_notify_provider = "feishu"     # 模拟重启后回到默认
    await ns.apply_notify_overrides_from_db()
    assert get_settings().system_notify_provider == "slack"


async def test_system_readiness_uses_feedback_recipient(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings().slack, "bot_token", "xoxb-test")
    row = ns.status_for("system")
    assert row["alert_email"] == get_settings().feedback_recipient
    assert row["ready"] == {"feishu": True, "slack": True}


async def test_system_env_pin(client, monkeypatch):
    monkeypatch.setenv("SYSTEM_NOTIFY_PROVIDER", "feishu")
    await ns.set_provider("system", "slack")
    from app.config import get_settings

    assert get_settings().system_notify_provider == "feishu"
    assert ns.status_for("system")["env_pinned"] is True
