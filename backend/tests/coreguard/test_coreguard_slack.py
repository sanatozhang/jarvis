"""coreguard 的 Slack 渲染 + provider 路由。

配额路由本身（跟渠道无关的策略）由 `test_feishu_quota_routing.py` 覆盖，
这里只测两件事：
1. 同一批数据在两个渠道下的**严重度和排序**必须一致
2. 切到 slack 之后不能还在调飞书，且配额计数要跨渠道连续
"""
from __future__ import annotations

import json
from datetime import date as _date_t, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.coreguard.services import notify as cnotify
from app.coreguard.services.feishu_summary_card import build_summary_card
from app.coreguard.services.slack_summary import (
    _MAX_INLINE_BREACHES,
    build_summary_message,
)
from app.db.database import Base
from app.services.im.mrkdwn import MAX_BLOCKS

_CUR_START = datetime(2026, 9, 22, 6, 0, 0)
_CUR_END = datetime(2026, 9, 22, 7, 0, 0)
_BASE_START = datetime(2026, 9, 15, 6, 0, 0)
_BASE_END = datetime(2026, 9, 15, 7, 0, 0)


def _metric(title="Crash-free sessions", tier="P0", change=-0.8, **over):
    r = dict(
        title=title, tier=tier, value_type="percent",
        current_value=99.05, baseline_value=99.85, change=change,
        direction="down", threshold={"pp": 0.5}, error=None,
        datadog_widget_id=None, band_lower=None, band_upper=None, baseline_n=None,
    )
    r.update(over)
    return r


def _args(breached=(), errored=(), forced=False):
    return dict(
        cur_start=_CUR_START, cur_end=_CUR_END,
        base_start=_BASE_START, base_end=_BASE_END,
        breached=list(breached), healthy=[_metric("App start p95", "P1")],
        errored=list(errored), forced=forced,
        dashboard_id="4h8-qff-zra", datadog_site="datadoghq.com",
    )


# ---------------------------------------------------------------------------
# 两个渲染器的一致性
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("breached,forced,template,color", [
    ([_metric()], False, "red", "#E01E5A"),
    ([], True, "blue", "#1D9BD1"),
    ([], False, "green", "#2EB67D"),
])
def test_severity_matches_between_feishu_and_slack(breached, forced, template, color):
    """红/蓝/绿三态两边必须一致。

    各判各的后果很隐蔽：切渠道之后同一条告警在飞书是红的、Slack 是绿的，
    "看颜色决定要不要立刻看"这个习惯就废了。
    """
    a = _args(breached=breached, forced=forced)
    assert build_summary_card(**a)["header"]["template"] == template
    assert build_summary_message(**a).color == color


def test_title_is_identical_between_providers():
    a = _args(breached=[_metric()])
    feishu_title = build_summary_card(**a)["header"]["title"]["content"]
    assert build_summary_message(**a).text == feishu_title


def test_p0_sorts_before_p1_same_as_feishu():
    """排序要跟飞书一致——P0 在前，同 tier 按偏离幅度降序。"""
    a = _args(breached=[
        _metric("P1 小偏离", "P1", change=-0.1),
        _metric("P0 大偏离", "P0", change=-2.0),
        _metric("P1 大偏离", "P1", change=-1.5),
    ])
    body = json.dumps(build_summary_message(**a).payload, ensure_ascii=False)
    assert body.index("P0 大偏离") < body.index("P1 大偏离") < body.index("P1 小偏离")


def test_dashboard_button_is_a_url_button_not_a_callback():
    """按钮必须是纯 URL（`url` 字段）。

    带 `action_id` 的交互按钮需要 interactivity + HMAC 回调端点 + 新 scope，
    本次迁移刻意不做（jarvis 的飞书卡片按钮本来也全是 open_url）。
    """
    btn = build_summary_message(**_args(breached=[_metric()])).payload[-1]["elements"][0]
    assert btn["url"].startswith("https://app.datadoghq.com/dashboard/")
    assert "action_id" not in btn


def test_missing_data_note_is_kept():
    msg = build_summary_message(**_args(breached=[_metric()],
                                        errored=[_metric("ANR rate", error="timeout")]))
    assert "缺数据" in json.dumps(msg.payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Block Kit 上限
# ---------------------------------------------------------------------------
def test_many_breaches_overflow_into_thread_instead_of_blowing_the_limit():
    """异常特别多时不能撑爆 50 blocks。

    撑爆的后果是 `invalid_blocks` —— **整条告警发不出去**，而这恰恰发生在
    "异常特别多"也就是最该收到告警的时候。
    """
    a = _args(breached=[_metric(f"指标 {i}", "P0", change=-float(i)) for i in range(45)])
    msg = build_summary_message(**a)
    assert len(msg.payload) <= MAX_BLOCKS
    assert len(msg.folds) == 1
    assert msg.folds[0].title == f"还有 {45 - _MAX_INLINE_BREACHES} 项异常"


def test_normal_breach_count_uses_no_thread():
    assert build_summary_message(**_args(breached=[_metric()])).folds == ()


# ---------------------------------------------------------------------------
# provider 路由
# ---------------------------------------------------------------------------
def _settings(provider="feishu"):
    from types import SimpleNamespace
    return SimpleNamespace(
        notify_provider=provider,
        feishu_enabled=True,
        feishu_target_chat_id="oc_core",
        slack_channel="C_core",
        feishu_target_email="ops@plaud.ai",
        feishu_overflow_email="ops@plaud.ai",
        feishu_group_daily_quota=2,
    )


@pytest.mark.parametrize("provider,channel", [("feishu", "oc_core"), ("slack", "C_core")])
def test_group_target_follows_provider(provider, channel):
    with patch.object(cnotify, "_settings", return_value=_settings(provider)):
        assert cnotify.group_target().channel == channel


def test_overflow_is_email_on_both_providers():
    """溢出目标是邮箱，两个渠道都能寻址 → 不需要并行的 slack 溢出字段。"""
    for provider in ("feishu", "slack"):
        with patch.object(cnotify, "_settings", return_value=_settings(provider)):
            t = cnotify.overflow_target()
        assert t.provider == provider and t.email == "ops@plaud.ai"


# ---------------------------------------------------------------------------
# 配额计数跨渠道连续
# ---------------------------------------------------------------------------
@pytest.fixture()
async def patched_db():
    from app.coreguard import models as _m  # noqa: F401
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    import app.db.database as db_mod
    orig_e, orig_f = db_mod._engine, db_mod._session_factory
    db_mod._engine = engine
    db_mod._session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield
    db_mod._engine, db_mod._session_factory = orig_e, orig_f
    await engine.dispose()


@pytest.mark.asyncio
async def test_switching_provider_does_not_reset_todays_quota(patched_db):
    """**切换渠道当天不能把群配额清零。**

    dispatch 行记的 `target_kind` 是 group/email 这个语义，不是 feishu/slack。
    如果配额按渠道分开计，切换那天会重新获得满额的"往群里发 N 条"，于是刚切
    过去的新频道当天被刷一遍——而切换恰恰是大家盯得最紧的时候。
    """
    from app.coreguard.models import CoreguardAlertDispatch
    from app.db.database import get_session

    # 先在"飞书"下用掉今天的 2 条群配额
    async with get_session() as session:
        for _ in range(2):
            session.add(CoreguardAlertDispatch(
                sent_date=_date_t.today(), target_kind="group",
                target_value="oc_core", sent_ok=True,
                alert_title="seed", breach_count=1, overflow_from_group=False,
            ))
        await session.commit()

    # 切到 slack 之后再发一条 —— 应该直接溢出到邮箱，而不是发频道
    post = AsyncMock(return_value="1790.1")
    with patch.object(cnotify, "_settings", return_value=_settings("slack")), \
         patch("app.services.slack_cli.uid_for_email", AsyncMock(return_value="U1")), \
         patch("app.services.slack_cli.post_message", post):
        from app.services.im import Rendered
        ok = await cnotify.send_alert(Rendered(payload=[], text="t"), breach_count=1)

    assert ok is True
    assert post.await_args.args[0] == "U1"        # DM，不是 C_core


@pytest.mark.asyncio
async def test_slack_provider_never_calls_feishu(patched_db):
    feishu = AsyncMock(return_value=True)
    post = AsyncMock(return_value="1790.2")
    with patch.object(cnotify, "_settings", return_value=_settings("slack")), \
         patch("app.services.feishu_cli.send_interactive_card", feishu), \
         patch("app.services.slack_cli.post_message", post):
        from app.services.im import Rendered
        ok = await cnotify.send_alert(Rendered(payload=[], text="t"), breach_count=1)

    assert ok is True
    feishu.assert_not_awaited()
    assert post.await_args.args[0] == "C_core"
