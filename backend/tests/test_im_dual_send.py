"""provider=both 的双发：im.dual_send / effective_provider / 各模块发送入口 / 切换校验。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import im
from app.services.im import NotifyTarget, Rendered


def _getter(value):
    box = {"v": value}
    return box, (lambda: box["v"])


@pytest.mark.asyncio
async def test_dual_send_runs_both_legs_in_order_and_resolves_provider():
    seen = []

    @im.dual_send(lambda: "both")
    async def send():
        seen.append(im.effective_provider("both"))
        return True

    assert await send() is True
    assert seen == ["feishu", "slack"]


@pytest.mark.asyncio
async def test_dual_send_passthrough_when_not_both():
    calls = []

    @im.dual_send(lambda: "slack")
    async def send():
        calls.append(1)
        return True

    assert await send() is True
    assert calls == [1]


@pytest.mark.asyncio
async def test_dual_send_one_leg_failure_does_not_block_the_other():
    seen = []

    @im.dual_send(lambda: "both")
    async def send():
        leg = im.effective_provider("both")
        seen.append(leg)
        if leg == "feishu":
            raise RuntimeError("feishu down")
        return True

    assert await send() is True       # slack 成功 → 整体成功
    assert seen == ["feishu", "slack"]


@pytest.mark.asyncio
async def test_dual_send_all_fail_is_false_and_all_none_is_none():
    @im.dual_send(lambda: "both")
    async def fail():
        return False

    @im.dual_send(lambda: "both")
    async def nodata():
        return None

    assert await fail() is False
    assert await nodata() is None


def test_effective_provider_defaults_to_feishu_outside_dual():
    assert im.effective_provider("both") == "feishu"
    assert im.effective_provider("slack") == "slack"
    assert im.effective_provider(" Feishu ") == "feishu"
    sentinel = object()
    assert im.effective_provider(sentinel) is sentinel   # Mock settings 不被吃掉


def test_validate_provider_accepts_both_unless_disallowed():
    assert im.validate_provider("graygate", "both") == "both"
    with pytest.raises(ValueError):
        im.validate_provider("system", "both", allow_both=False)
    with pytest.raises(ValueError):
        im.validate_provider("graygate", "slak")


@pytest.mark.asyncio
async def test_graygate_send_focus_change_sends_to_both_providers():
    from app.graygate.services import notify as gn

    settings = SimpleNamespace(
        notify_provider="both", feishu_chat_id="oc_x", slack_channel="C123", alert_email="",
    )
    sent = []

    class _T:
        def __init__(self, name):
            self.name = name

        async def send(self, target, msg):
            sent.append((self.name, target.provider, target.channel))
            return True

    with patch.object(gn, "_settings", return_value=settings), \
         patch.object(gn, "resolve_transport", side_effect=lambda p: _T(p)):
        assert await gn.send_focus_change("ios", "设为 x", "原值 y", "u") is True

    assert sent == [("feishu", "feishu", "oc_x"), ("slack", "slack", "C123")]


@pytest.mark.asyncio
async def test_coreguard_secondary_leg_does_not_record_dispatch_or_use_quota():
    from app.coreguard.services import notify as cn

    s = SimpleNamespace(
        notify_provider="both", feishu_enabled=True, feishu_target_chat_id="oc_g",
        slack_channel="C1", feishu_overflow_email="", feishu_target_email="",
        feishu_group_daily_quota=10,
    )
    sent, recorded = [], []

    class _T:
        async def send(self, target, msg):
            sent.append(target.provider)
            return True

    async def _rec(**kw):
        recorded.append(kw["target_kind"])

    with patch.object(cn, "_settings", return_value=s), \
         patch.object(cn, "resolve_transport", return_value=_T()), \
         patch.object(cn, "_count_group_sent_today", new=AsyncMock(return_value=0)), \
         patch.object(cn, "_record_dispatch", new=_rec), \
         patch.object(cn, "render_summary", return_value=Rendered(payload={})):
        assert await cn.send_summary(breach_count=1) is True

    assert sent == ["feishu", "slack"]
    assert recorded == ["group"]          # 只有第一条腿记审计 / 占配额


@pytest.mark.asyncio
async def test_notify_switch_rejects_both_for_system_and_accepts_for_groups():
    from app.services import notify_switch

    with pytest.raises(ValueError):
        await notify_switch.set_provider("system", "both")

    with patch.object(notify_switch.db, "get_oncall_config", new=AsyncMock(return_value="")), \
         patch.object(notify_switch.db, "set_oncall_config", new=AsyncMock()), \
         patch.object(notify_switch, "_apply_one"), \
         patch.object(notify_switch, "status_for", return_value={"provider": "both"}):
        assert (await notify_switch.set_provider("graygate", "both"))["provider"] == "both"
