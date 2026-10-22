"""2026-10-19：灰度报告发布频率开关（daily / 30m / 2h / 6h / 12h）。

- 槽位：对齐整点（30m → :00/:30，2h → 偶数整点…），00:00 槽位窗口为空跳过
- 「今日至今」窗口：当天 00:00 → 槽位钟点；基线工作日截到同一钟点
- 调度：每个槽位只发一次；daily 不发高频；错过的槽位只发最新的
- 渲染：标题 / banner 标「今日至今」，Slack 仍无中文
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.graygate.services import frequency as freq
from app.graygate.services.card_builder import GraygateReportData, _window_ms
from app.graygate.services.slack_report import assemble_slack_message
from app.graygate.workers import scheduler as sched

_BJT = ZoneInfo("Asia/Shanghai")
_CJK = re.compile(r"[㐀-鿿]")


def _bjt(h, m=0, d=19):
    return datetime(2026, 10, d, h, m, tzinfo=_BJT)


@pytest.mark.parametrize("frequency,now,expected", [
    ("daily", _bjt(14, 40), None),
    ("30m", _bjt(14, 40), _bjt(14, 30)),
    ("30m", _bjt(14, 29), _bjt(14, 0)),
    ("30m", _bjt(0, 10), None),            # 00:00 槽位窗口为空
    ("30m", _bjt(0, 30), _bjt(0, 30)),
    ("2h", _bjt(15, 59), _bjt(14, 0)),
    ("6h", _bjt(5, 59), None),
    ("6h", _bjt(17, 0), _bjt(12, 0)),
    ("12h", _bjt(23, 59), _bjt(12, 0)),
])
def test_current_slot(frequency, now, expected):
    assert freq.current_slot(now, frequency) == expected


def test_intraday_window_cuts_today_and_baseline_at_same_clock():
    start, end = _window_ms(date(2026, 10, 19), time(14, 30))
    assert end - start == (14 * 60 + 30) * 60 * 1000
    full = _window_ms(date(2026, 10, 19))
    assert full[0] == start and full[1] - full[0] == 24 * 3600 * 1000


def _settings(frequency="30m", **kw):
    base = dict(enabled=True, scheduler_enabled=True, feishu_enabled=True, send_enabled=True,
                notify_provider="slack", report_frequency=frequency)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _reset():
    sched._last_intraday_slot = None
    sched._last_intraday_alert_at = None
    yield
    sched._last_intraday_slot = None


@pytest.mark.asyncio
async def test_intraday_fires_once_per_slot(monkeypatch):
    s = _settings("30m")
    send = AsyncMock(return_value=True)
    hb = AsyncMock()
    monkeypatch.setattr(sched, "get_graygate_settings", lambda: s)
    monkeypatch.setattr(freq, "get_graygate_settings", lambda: s)
    monkeypatch.setattr(sched, "_intraday_slot_handled", AsyncMock(return_value=False))
    monkeypatch.setattr(sched, "_write_heartbeat", hb)
    with patch.object(sched.notify, "send_daily_report", send):
        for now in (_bjt(14, 30), _bjt(14, 31), _bjt(14, 59)):
            monkeypatch.setattr(sched, "_now_bjt", lambda now=now: now)
            await sched._intraday_tick_once()
        assert send.await_count == 1
        send.assert_awaited_with(date(2026, 10, 19), until=time(14, 30))

        monkeypatch.setattr(sched, "_now_bjt", lambda: _bjt(15, 0))
        await sched._intraday_tick_once()
        assert send.await_count == 2
        send.assert_awaited_with(date(2026, 10, 19), until=time(15, 0))
    summary = hb.await_args.args[2]
    assert summary["slot"] == _bjt(15, 0).isoformat()
    assert hb.await_args.kwargs["job_name"] == "graygate_intraday_report"


@pytest.mark.asyncio
async def test_daily_frequency_never_sends_intraday(monkeypatch):
    s = _settings("daily")
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(sched, "get_graygate_settings", lambda: s)
    monkeypatch.setattr(freq, "get_graygate_settings", lambda: s)
    monkeypatch.setattr(sched, "_now_bjt", lambda: _bjt(14, 30))
    with patch.object(sched.notify, "send_daily_report", send):
        await sched._intraday_tick_once()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_slot_already_in_heartbeat_is_not_resent(monkeypatch):
    """进程重启后内存态清零：心跳表显示这个槽位跑过 → 不重发。"""
    s = _settings("2h")
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(sched, "get_graygate_settings", lambda: s)
    monkeypatch.setattr(freq, "get_graygate_settings", lambda: s)
    monkeypatch.setattr(sched, "_now_bjt", lambda: _bjt(14, 5))
    monkeypatch.setattr(sched, "_intraday_slot_handled", AsyncMock(return_value=True))
    with patch.object(sched.notify, "send_daily_report", send):
        await sched._intraday_tick_once()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_disabled_skips_fetch(monkeypatch):
    s = _settings("30m", feishu_enabled=False, send_enabled=False)
    send = AsyncMock(return_value=True)
    hb = AsyncMock()
    monkeypatch.setattr(sched, "get_graygate_settings", lambda: s)
    monkeypatch.setattr(freq, "get_graygate_settings", lambda: s)
    monkeypatch.setattr(sched, "_now_bjt", lambda: _bjt(14, 30))
    monkeypatch.setattr(sched, "_intraday_slot_handled", AsyncMock(return_value=False))
    monkeypatch.setattr(sched, "_write_heartbeat", hb)
    with patch.object(sched.notify, "send_daily_report", send):
        await sched._intraday_tick_once()
    send.assert_not_awaited()
    assert hb.await_args.args[2]["skip_reason"] == "send_enabled=False"


@pytest.mark.asyncio
async def test_set_frequency_validates_and_applies(monkeypatch):
    s = _settings("daily")
    monkeypatch.setattr(freq, "get_graygate_settings", lambda: s)
    store = {}

    async def fake_set(k, v):
        store[k] = v

    monkeypatch.setattr(freq.db, "set_oncall_config", fake_set)
    assert await freq.set_frequency("2H") == "2h"
    assert s.report_frequency == "2h" and store[freq.FREQUENCY_KEY] == "2h"
    with pytest.raises(ValueError):
        await freq.set_frequency("1h")


def test_intraday_title_and_banner():
    data = GraygateReportData(
        target_date=date(2026, 10, 19), d1_day=date(2026, 10, 17), version_pattern="4.*",
        worsen_lines=[], columns_md=["", ""], new_crash_md=None, top_crash_md=None, top_jank_md=None,
        until=time(14, 30),
    )
    assert "今日至今" in data.title and "00:00–14:30" in data.title
    assert "00:00~14:30" in data.banner_md and "同时段" in data.banner_md
    r = assemble_slack_message(data, "https://j/graygate/reports?date=2026-10-19")
    text = json.dumps([r.payload, r.text], ensure_ascii=False)
    assert "[4.* Gray] Today so far · 2026-10-19 00:00–14:30 BJT" in text
    assert "previous workday, same hours" in text
    assert not _CJK.search(text)

    daily = GraygateReportData(
        target_date=date(2026, 10, 18), d1_day=date(2026, 10, 17), version_pattern="4.*",
        worsen_lines=[], columns_md=["", ""], new_crash_md=None, top_crash_md=None, top_jank_md=None,
    )
    assert daily.title == "🆕 [4.* 灰度] 每日指标 · 2026-10-18"
    assert "00:00~24:00" in daily.banner_md
