"""Hourly alerter 单测：SHoW 基线、新增检测、阈值、回落、idempotency。"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest


def _floor_h(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _fake_dd_issue(
    issue_id: str, title: str, events: int, platform: str = "android",
    sessions: int | None = None,
) -> dict:
    """构造 datadog_client.list_issues_for_window 返回的 raw dict。

    sessions 默认 = events 等量（多数测试场景下 ≥ min_sessions=60 阈值）；
    显式传入可测低 sessions 过滤行为。
    """
    return {
        "id": issue_id,
        "type": "issue",
        "attributes": {
            "events_count": events,
            "sessions_affected": sessions if sessions is not None else events,
            "title": title,
            "platform": platform,
        },
    }


async def _setup_db_and_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'ha.db'}")
    monkeypatch.setenv("CRASHGUARD_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_FEISHU_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_FEISHU_TARGET_CHAT_ID", "test_chat")
    monkeypatch.setenv("CRASHGUARD_DATADOG_API_KEY", "x")
    monkeypatch.setenv("CRASHGUARD_DATADOG_APP_KEY", "y")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_GROWTH_THRESHOLD_PCT", "10")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_WINDOW_DAYS", "30")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_MIN_BASELINE_EVENTS", "20")

    from app.config import get_settings
    from app.crashguard.config import get_crashguard_settings
    get_settings.cache_clear()
    get_crashguard_settings.cache_clear()

    from app.db.database import init_db
    from app.crashguard import models  # noqa: F401
    await init_db()


@pytest.mark.asyncio
async def test_new_issue_triggers_alert(tmp_path, monkeypatch):
    """DB 不存在的 issue 应触发新增告警，且不依赖 SHoW 基线。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)

    fake_now = datetime(2026, 5, 15, 10, 5, 0)  # cron at :05
    # events=80 → sessions=80 ≥ default min_sessions=60，过阈值
    issues = [_fake_dd_issue("ddi_new_1", "NullPointerException", 80, "android")]

    sent_cards = []
    async def fake_send(chat_id="", card=None, email=""):
        sent_cards.append(card)
        return True

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.services.feishu_cli.send_interactive_card", side_effect=fake_send):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(now=fake_now)

    assert result["ok"] is True
    assert result["alerted"] is True
    assert result["new"] == 1
    assert result["surge"] == 0
    assert len(sent_cards) == 1
    # 卡片内含新增段
    elements_text = str(sent_cards[0])
    assert "新增崩溃" in elements_text
    assert "ddi_new_1" in elements_text or "NullPointerException" in elements_text


@pytest.mark.asyncio
async def test_show_baseline_growth_above_threshold_triggers_surge(tmp_path, monkeypatch):
    """已有 issue + 上周同时段基线 → 当前 +20% 应触发上涨告警。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)

    from app.db.database import get_session
    from app.crashguard.models import CrashHourlySnapshot, CrashIssue

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    now_hour = _floor_h(fake_now)
    window_start = now_hour - timedelta(hours=1)
    show_target = window_start - timedelta(days=7)

    async with get_session() as session:
        # 历史 issue：first_seen 在 60 天前，不算新增
        session.add(CrashIssue(
            datadog_issue_id="ddi_old",
            platform="ios",
            title="OldCrash",
            first_seen_at=fake_now - timedelta(days=60),
        ))
        # SHoW 基线：上周同时段 100 events
        session.add(CrashHourlySnapshot(
            datadog_issue_id="ddi_old",
            hour_utc=show_target,
            events_count=100,
        ))
        await session.commit()

    # 本小时 120 events → +20%（> 10% 阈值）
    issues = [_fake_dd_issue("ddi_old", "OldCrash", 120, "ios")]

    sent_cards = []
    async def fake_send(chat_id="", card=None, email=""):
        sent_cards.append(card)
        return True

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.services.feishu_cli.send_interactive_card", side_effect=fake_send):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(now=fake_now)

    assert result["ok"] is True
    assert result["alerted"] is True
    assert result["new"] == 0
    assert result["surge"] == 1
    assert "异常上涨" in str(sent_cards[0])


@pytest.mark.asyncio
async def test_show_growth_below_threshold_does_not_alert(tmp_path, monkeypatch):
    """+5% 增长不触发告警（阈值 10%）。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)

    from app.db.database import get_session
    from app.crashguard.models import CrashHourlySnapshot, CrashIssue

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    show_target = _floor_h(fake_now) - timedelta(hours=1) - timedelta(days=7)

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="ddi_stable",
            platform="android",
            title="StableCrash",
            first_seen_at=fake_now - timedelta(days=90),
        ))
        session.add(CrashHourlySnapshot(
            datadog_issue_id="ddi_stable",
            hour_utc=show_target,
            events_count=100,
        ))
        await session.commit()

    issues = [_fake_dd_issue("ddi_stable", "StableCrash", 105, "android")]  # +5%

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(now=fake_now)

    assert result["ok"] is True
    assert result["alerted"] is False
    assert result.get("reason") == "no_anomaly"


@pytest.mark.asyncio
async def test_min_baseline_skips_small_baselines(tmp_path, monkeypatch):
    """SHoW 基线 < min_baseline（20）→ 跳过百分比判定，不告警。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)

    from app.db.database import get_session
    from app.crashguard.models import CrashHourlySnapshot, CrashIssue

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    show_target = _floor_h(fake_now) - timedelta(hours=1) - timedelta(days=7)

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="ddi_tiny",
            platform="flutter",
            title="TinyCrash",
            first_seen_at=fake_now - timedelta(days=90),
        ))
        # 基线只有 5 events（< min_baseline=20）
        session.add(CrashHourlySnapshot(
            datadog_issue_id="ddi_tiny",
            hour_utc=show_target,
            events_count=5,
        ))
        await session.commit()

    # 当前 50 events → 看似 +900%，但小基数应跳过
    issues = [_fake_dd_issue("ddi_tiny", "TinyCrash", 50, "flutter")]

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(now=fake_now)

    # 旧 issue（first_seen 90d 前）+ 小基线 → 既不算新增也不算上涨
    assert result["alerted"] is False


@pytest.mark.asyncio
async def test_idempotency_same_hour_skips(tmp_path, monkeypatch):
    """同 hour_utc 已发过 → 第二次 tick 跳过。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    issues = [_fake_dd_issue("ddi_dup", "DupCrash", 80, "android")]

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        r1 = await run_hourly_alert_tick(now=fake_now)
        r2 = await run_hourly_alert_tick(now=fake_now)

    assert r1["alerted"] is True
    assert r2.get("skipped") == "already_alerted"


@pytest.mark.asyncio
async def test_fallback_to_rolling_avg_when_show_missing(tmp_path, monkeypatch):
    """SHoW 数据缺失 → 回落 7d 滚动均值；满足阈值仍告警。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)

    from app.db.database import get_session
    from app.crashguard.models import CrashHourlySnapshot, CrashIssue

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    now_hour = _floor_h(fake_now)
    window_start = now_hour - timedelta(hours=1)

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="ddi_rolling",
            platform="ios",
            title="RollingCrash",
            first_seen_at=fake_now - timedelta(days=60),
        ))
        # 不写 SHoW snapshot；写过去 5 天的滚动数据，平均 100
        for i in range(1, 6):
            session.add(CrashHourlySnapshot(
                datadog_issue_id="ddi_rolling",
                hour_utc=window_start - timedelta(days=i),
                events_count=100,
            ))
        await session.commit()

    # 当前 120 → 100 均值 → +20%
    issues = [_fake_dd_issue("ddi_rolling", "RollingCrash", 120, "ios")]

    sent_cards = []
    async def fake_send(chat_id="", card=None, email=""):
        sent_cards.append(card)
        return True

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.services.feishu_cli.send_interactive_card", side_effect=fake_send):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(now=fake_now)

    assert result["alerted"] is True
    assert result["surge"] == 1
    # 卡片中标注 fallback 源
    assert "7d 均值" in str(sent_cards[0])


@pytest.mark.asyncio
async def test_rate_and_check_blocks_traffic_growth_false_positive(tmp_path, monkeypatch):
    """rate-AND-check：events 涨 ≥ 10% 但 rate=events/sessions 持平 / 跌 → 不告警。

    场景：用户量自然增长（如新版本发布、海外开闸）导致 events 等比例上涨，
    但 crash rate 没变，本质用户体验没劣化，不应该 surge。
    """
    await _setup_db_and_settings(tmp_path, monkeypatch)

    from app.db.database import get_session
    from app.crashguard.models import CrashHourlySnapshot, CrashIssue

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    now_hour = _floor_h(fake_now)
    window_start = now_hour - timedelta(hours=1)
    show_target = window_start - timedelta(days=7)

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="ddi_traffic",
            platform="ios",
            title="TrafficGrowth",
            first_seen_at=fake_now - timedelta(days=60),
        ))
        # SHoW 基线：100 events / 1000 sessions → rate 10%
        session.add(CrashHourlySnapshot(
            datadog_issue_id="ddi_traffic",
            hour_utc=show_target,
            events_count=100,
            sessions_count=1000,
        ))
        await session.commit()

    # 当前：150 events / 1500 sessions → rate 10%（持平）
    # events 涨 +50%（>>10% 阈值）但 rate 没动 → 应被 rate-AND 拦住
    issues = [_fake_dd_issue("ddi_traffic", "TrafficGrowth", 150, "ios", sessions=1500)]

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(now=fake_now)

    assert result["ok"] is True
    # events 维度涨了 +50%，但 rate 持平 → surge=0 才符合预期
    assert result.get("surge", 0) == 0


@pytest.mark.asyncio
async def test_rate_and_check_allows_real_severity_growth(tmp_path, monkeypatch):
    """rate-AND-check 不能误伤真劣化：events 涨且 rate 也涨 → 必须告警。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)

    from app.db.database import get_session
    from app.crashguard.models import CrashHourlySnapshot, CrashIssue

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    now_hour = _floor_h(fake_now)
    window_start = now_hour - timedelta(hours=1)
    show_target = window_start - timedelta(days=7)

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="ddi_real",
            platform="ios",
            title="RealRegression",
            first_seen_at=fake_now - timedelta(days=60),
        ))
        # SHoW: 100 events / 1000 sessions → rate 10%
        session.add(CrashHourlySnapshot(
            datadog_issue_id="ddi_real",
            hour_utc=show_target,
            events_count=100,
            sessions_count=1000,
        ))
        await session.commit()

    # 当前：200 events / 1000 sessions → rate 20%（×2）— 真劣化
    issues = [_fake_dd_issue("ddi_real", "RealRegression", 200, "ios", sessions=1000)]

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(now=fake_now)

    assert result.get("surge", 0) == 1


@pytest.mark.asyncio
async def test_show_baseline_matches_legacy_19char_format(tmp_path, monkeypatch):
    """回归：回填脚本在 microsecond=0 时落 19 字符 'YYYY-MM-DD HH:MM:SS'，
    而 ORM 用 26 字符 '...ffffff'。`_resolve_baseline` 必须用区间匹配，否则 SHoW miss。
    """
    await _setup_db_and_settings(tmp_path, monkeypatch)

    from sqlalchemy import text
    from app.db.database import get_session
    from app.crashguard.models import CrashIssue

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    now_hour = _floor_h(fake_now)
    window_start = now_hour - timedelta(hours=1)
    show_target = window_start - timedelta(days=7)
    legacy_hu = show_target.strftime("%Y-%m-%d %H:%M:%S")  # 19 字符，无 microseconds

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="ddi_legacy_fmt",
            platform="ios",
            title="LegacyFmt",
            first_seen_at=fake_now - timedelta(days=60),
        ))
        await session.commit()
        # 模拟 backfill 脚本（旧版）走 text() 落 19 字符
        await session.execute(text(
            "INSERT INTO crash_hourly_snapshots "
            "(datadog_issue_id, hour_utc, events_count, captured_at) "
            "VALUES (:iid, :hu, :ev, :now)"
        ), {"iid": "ddi_legacy_fmt", "hu": legacy_hu, "ev": 100,
            "now": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")})
        await session.commit()

    issues = [_fake_dd_issue("ddi_legacy_fmt", "LegacyFmt", 120, "ios")]

    sent_cards = []
    async def fake_send(chat_id="", card=None, email=""):
        sent_cards.append(card)
        return True

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.services.feishu_cli.send_interactive_card", side_effect=fake_send):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(now=fake_now)

    assert result["alerted"] is True
    assert result["surge"] == 1
    # 必须走 SHoW（不是 rolling_7d fallback）
    assert "SHoW" in str(sent_cards[0])


@pytest.mark.asyncio
async def test_min_sessions_filter_blocks_low_volume(tmp_path, monkeypatch):
    """绝对量级阈值：sessions_affected < 60 的 issue 不告警（脏数据/低频噪声过滤）。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    # 即便是新增 issue（高敏感），sessions=10 < 60 也应被过滤
    issues = [
        _fake_dd_issue("ddi_low_vol", "RareEdgeCase", events=200, sessions=10, platform="ios"),
    ]

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(now=fake_now)

    assert result["alerted"] is False
    assert result.get("reason") == "no_anomaly"


@pytest.mark.asyncio
async def test_min_sessions_allows_high_volume(tmp_path, monkeypatch):
    """sessions=100 ≥ 阈值 60 → 即便其它条件普通也能告警（验证非阻塞）。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    issues = [
        _fake_dd_issue("ddi_high_vol", "HighVolNewCrash", events=200, sessions=100, platform="android"),
    ]

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(now=fake_now)

    assert result["alerted"] is True
    assert result["new"] == 1


@pytest.mark.asyncio
async def test_kill_switch_skips_when_disabled(tmp_path, monkeypatch):
    """hourly_alert_enabled=false 时直接跳过。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_ENABLED", "false")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
    result = await run_hourly_alert_tick()
    assert result.get("skipped") == "hourly_alert_disabled"
