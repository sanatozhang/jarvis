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
    # 测试环境锚定 min_sessions=100；prod default 500（5/14 二次上调，与 core_metric 对齐）
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_MIN_SESSIONS", "100")
    # min_events_absolute=50 测试用值（prod 200）；dedup_hours=0 默认禁用，单测显式打开
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_MIN_EVENTS_ABSOLUTE", "50")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_DEDUP_HOURS", "0")

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
    # events=120 → sessions=120 ≥ default min_sessions=100，过阈值
    issues = [_fake_dd_issue("ddi_new_1", "NullPointerException", 120, "android")]

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
        # SHoW 基线：上周同时段 100 events / 1000 sessions → rate 10%
        session.add(CrashHourlySnapshot(
            datadog_issue_id="ddi_old",
            hour_utc=show_target,
            events_count=100,
            sessions_count=1000,
        ))
        await session.commit()

    # 本小时 120 events / 1000 sessions → events +20%，rate +20% → AND 双过
    issues = [_fake_dd_issue("ddi_old", "OldCrash", 120, "ios", sessions=1000)]

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
    issues = [_fake_dd_issue("ddi_dup", "DupCrash", 120, "android")]

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
        # 不写 SHoW snapshot；写过去 5 天的滚动数据，平均 100 events / 1000 sessions
        for i in range(1, 6):
            session.add(CrashHourlySnapshot(
                datadog_issue_id="ddi_rolling",
                hour_utc=window_start - timedelta(days=i),
                events_count=100,
                sessions_count=1000,
            ))
        await session.commit()

    # 当前 120 events / 1000 sessions → events +20% AND rate +20%
    issues = [_fake_dd_issue("ddi_rolling", "RollingCrash", 120, "ios", sessions=1000)]

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
        # 模拟 backfill 脚本（旧版）走 text() 落 19 字符；sessions_count=1000 让 rate-AND-check 过关
        await session.execute(text(
            "INSERT INTO crash_hourly_snapshots "
            "(datadog_issue_id, hour_utc, events_count, sessions_count, captured_at) "
            "VALUES (:iid, :hu, :ev, :ss, :now)"
        ), {"iid": "ddi_legacy_fmt", "hu": legacy_hu, "ev": 100, "ss": 1000,
            "now": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")})
        await session.commit()

    issues = [_fake_dd_issue("ddi_legacy_fmt", "LegacyFmt", 120, "ios", sessions=1000)]

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
async def test_rate_and_check_strict_blocks_when_no_sess_baseline(tmp_path, monkeypatch):
    """P0 严格化：SHoW 历史 snapshot 无 sessions_count + 14d 内也无 sessions 兜底
    → rate_base 算不出来 → 不告警（宁缺勿误报，而非旧版静默放行）。

    抓手：之前 rate_base=None 被 if 跳过，AND 退化为单 events 闸；现在强制 AND。
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
            datadog_issue_id="ddi_no_sess",
            platform="ios",
            title="NoSessHistory",
            first_seen_at=fake_now - timedelta(days=60),
        ))
        # SHoW 有 events 但 sessions_count=0（模拟老 snapshot 列刚加未回填）
        session.add(CrashHourlySnapshot(
            datadog_issue_id="ddi_no_sess",
            hour_utc=show_target,
            events_count=100,
            sessions_count=0,
        ))
        await session.commit()

    # 当前 200 events / 100 sessions：events +100% 远超 10%；但 rate_base 算不出来
    issues = [_fake_dd_issue("ddi_no_sess", "NoSessHistory", 200, "ios", sessions=100)]

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(now=fake_now)

    # P0：rate_base 算不出来 → 宁缺勿误报
    assert result.get("surge", 0) == 0
    assert result.get("alerted") is False


@pytest.mark.asyncio
async def test_sessions_baseline_fallback_kicks_in_when_show_lacks_sessions(tmp_path, monkeypatch):
    """P2 兜底：SHoW events 命中但 sessions_count=0 → 用 14 天 sessions>0 中位数兜底；
    兜底后 rate-AND-check 重新可用。
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
            datadog_issue_id="ddi_sess_fb",
            platform="ios",
            title="SessFallback",
            first_seen_at=fake_now - timedelta(days=60),
        ))
        # SHoW: events=100 但 sessions=0（无 sessions 历史）
        session.add(CrashHourlySnapshot(
            datadog_issue_id="ddi_sess_fb",
            hour_utc=show_target,
            events_count=100,
            sessions_count=0,
        ))
        # 14d 内有 5 个 snapshot 带 sessions_count → 中位数 = 1000
        for i in range(1, 6):
            session.add(CrashHourlySnapshot(
                datadog_issue_id="ddi_sess_fb",
                hour_utc=window_start - timedelta(days=i),
                events_count=80,
                sessions_count=1000,
            ))
        await session.commit()

    # 当前 200 events / 1000 sessions → rate 20%；
    # 兜底 sess_baseline=1000 → rate_base = 100/1000 = 10% → rate +100%
    issues = [_fake_dd_issue("ddi_sess_fb", "SessFallback", 200, "ios", sessions=1000)]

    sent_cards = []
    async def fake_send(chat_id="", card=None, email=""):
        sent_cards.append(card)
        return True

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.services.feishu_cli.send_interactive_card", side_effect=fake_send):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(now=fake_now)

    assert result.get("alerted") is True
    assert result.get("surge") == 1


@pytest.mark.asyncio
async def test_min_events_absolute_blocks_low_event_count(tmp_path, monkeypatch):
    """#2 events 绝对量底线：events_h < min_events_absolute 不告警，
    即便满足 sessions / 增长率（防小基数 issue 在 sessions 大涨时被反复挑出）。
    """
    await _setup_db_and_settings(tmp_path, monkeypatch)
    # 抬高 events 绝对底线到 100，让 events=80 被拦
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_MIN_EVENTS_ABSOLUTE", "100")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    # 新增 issue，sessions=200（过 min_sessions=100），但 events=80 < 100
    issues = [_fake_dd_issue("ddi_low_ev", "LowEvCrash", events=80, sessions=200, platform="ios")]

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(now=fake_now)

    assert result.get("alerted") is False
    assert result.get("reason") == "no_anomaly"


@pytest.mark.asyncio
async def test_dedup_skips_issue_alerted_recently(tmp_path, monkeypatch):
    """#1 跨告警 dedup：同 issue 在 12h 内已被 hourly 告警过 → 本 tick 跳过。

    抓手：早晚报 + hourly 8 tick/day 容易把同一 issue 反复点名；
    dedup 让用户每个 issue 一天最多见一次。
    """
    await _setup_db_and_settings(tmp_path, monkeypatch)
    # 打开 dedup
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_DEDUP_HOURS", "12")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    from app.db.database import get_session
    from app.crashguard.models import CrashHourlyAlert

    fake_now = datetime(2026, 5, 15, 10, 5, 0)

    # 模拟 3 小时前已存在一条告警，payload 含 ddi_repeat
    async with get_session() as session:
        session.add(CrashHourlyAlert(
            hour_utc=_floor_h(fake_now) - timedelta(hours=3),
            new_count=1, surge_count=0,
            feishu_message_id="",
            alert_payload='{"new":[{"issue_id":"ddi_repeat","title":"Repeat","events_h":300,"sessions_h":150,"first_seen":null}],"surge":[]}',
            created_at=fake_now - timedelta(hours=3),
        ))
        await session.commit()

    # 同 issue 在本 tick 又出现（新增条件成立，但 dedup 应拦截）
    issues = [_fake_dd_issue("ddi_repeat", "Repeat", events=300, sessions=150, platform="ios")]

    from app.crashguard.services.datadog_cache import DatadogCache
    DatadogCache.clear()

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.crashguard.services.hourly_alerter.DatadogCache.get_or_fetch",
               new=AsyncMock(return_value={})), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(now=fake_now)

    # 被 dedup 拦下 → no_anomaly
    assert result.get("alerted") is False


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


# ===== 通道 1：新版本桶（Task 4）=====

@pytest.mark.asyncio
async def test_channel_1_new_version_triggers_when_user_rate_meets(tmp_path, monkeypatch):
    """通道 1：新版本桶，events ≥ min_events AND user_rate ≥ threshold → 告警。

    50 events / 10000 users = 0.5%，恰好等于 threshold=0.005 → 应触发（>= 语义）。
    注意：issue version 3.20.0-700 > top_version 3.19.0-600 → bucket=new
    """
    await _setup_db_and_settings(tmp_path, monkeypatch)
    # 打开通道 1，关闭影子模式
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_SHADOW_MODE", "false")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_MIN_EVENTS", "30")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_USER_RATE_PCT", "0.005")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    # 新版本 issue：version > top_version
    issues = [{
        "id": "test_new_ver_1",
        "type": "issue",
        "attributes": {
            "events_count": 50,
            "sessions_affected": 800,
            "title": "NewVersionCrash",
            "platform": "android",
            "version": "3.20.0-700",
        },
    }]
    # top_version = 3.19.0-600，用户量 10000
    top_version_data = {"android": {"version": "3.19.0-600", "users": 10000}}

    from app.crashguard.services.datadog_cache import DatadogCache
    DatadogCache.clear()

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.crashguard.services.hourly_alerter.DatadogCache.get_or_fetch",
               new=AsyncMock(return_value=top_version_data)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(force=True, now=fake_now)

    assert result["alerted"] is True
    assert result["new_version"] == 1


@pytest.mark.asyncio
async def test_channel_1_bypasses_min_events_absolute(tmp_path, monkeypatch):
    """[回归测试] 通道 1 必须绕过 min_events_absolute（200，生产值）。

    底层逻辑：通道 1 是"灰度新版本"专属抓手，events 通常 30~150 区间。
    若 min_events_abs（生产 200）卡在分桶之前，通道 1 永远触发不了。
    本测试模拟生产配置——MIN_EVENTS_ABSOLUTE=200, events=50, user_rate=0.5%——
    通道 1 必须仍然触发。
    """
    await _setup_db_and_settings(tmp_path, monkeypatch)
    # 生产值：MIN_EVENTS_ABSOLUTE=200（远高于本测试的 events=50）
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_MIN_EVENTS_ABSOLUTE", "200")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_SHADOW_MODE", "false")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_MIN_EVENTS", "30")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_USER_RATE_PCT", "0.005")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    issues = [{
        "id": "test_new_ver_bypass_abs", "type": "issue",
        "attributes": {
            "events_count": 50,                 # < min_events_abs=200，但应被通道 1 接住
            "sessions_affected": 800,
            "title": "NewVersionCrash",
            "platform": "android",
            "version": "3.20.0-700",
        },
    }]
    top_version_data = {"android": {"version": "3.19.0-600", "users": 10000}}

    from app.crashguard.services.datadog_cache import DatadogCache
    DatadogCache.clear()

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.crashguard.services.hourly_alerter.DatadogCache.get_or_fetch",
               new=AsyncMock(return_value=top_version_data)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(force=True, now=fake_now)

    # 关键断言：events=50 < min_events_abs=200，但通道 1 仍触发
    assert result.get("new_version") == 1, (
        f"通道 1 被 min_events_abs 拦截了！alerted={result.get('alerted')} "
        f"new_version={result.get('new_version')}"
    )


@pytest.mark.asyncio
async def test_channel_1_blocked_by_user_rate(tmp_path, monkeypatch):
    """通道 1：user_rate = 50/1_000_000 = 0.000005 << threshold=0.005 → 不告警。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_SHADOW_MODE", "false")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_MIN_EVENTS", "30")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_USER_RATE_PCT", "0.005")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    issues = [{
        "id": "test_new_ver_2",
        "type": "issue",
        "attributes": {
            "events_count": 50,
            "sessions_affected": 800,
            "title": "NewVersionCrash",
            "platform": "android",
            "version": "3.20.0-700",
        },
    }]
    # 1M 用户 → user_rate = 50/1_000_000 远小于 0.5%
    top_version_data = {"android": {"version": "3.19.0-600", "users": 1_000_000}}

    from app.crashguard.services.datadog_cache import DatadogCache
    DatadogCache.clear()

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.crashguard.services.hourly_alerter.DatadogCache.get_or_fetch",
               new=AsyncMock(return_value=top_version_data)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(force=True, now=fake_now)

    assert result.get("new_version", 0) == 0


@pytest.mark.asyncio
async def test_channel_1_blocked_by_min_events(tmp_path, monkeypatch):
    """通道 1：events_count=20 < min_events=30 → 不触发。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_SHADOW_MODE", "false")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_MIN_EVENTS", "30")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_USER_RATE_PCT", "0.005")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    issues = [{
        "id": "test_new_ver_3",
        "type": "issue",
        "attributes": {
            "events_count": 20,  # < min_events=30
            "sessions_affected": 800,
            "title": "NewVersionCrash",
            "platform": "android",
            "version": "3.20.0-700",
        },
    }]
    top_version_data = {"android": {"version": "3.19.0-600", "users": 10000}}

    from app.crashguard.services.datadog_cache import DatadogCache
    DatadogCache.clear()

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.crashguard.services.hourly_alerter.DatadogCache.get_or_fetch",
               new=AsyncMock(return_value=top_version_data)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(force=True, now=fake_now)

    assert result.get("new_version", 0) == 0


# ===== 通道 3：全局新 crash 兜底（Task 5）=====

@pytest.mark.asyncio
async def test_channel_3_triggers_when_thresholds_met(tmp_path, monkeypatch):
    """通道 3：first_seen 在 10 天内 + events=200 ≥ 150 + sessions=400 ≥ 300 → 告警。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_SHADOW_MODE", "false")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_MIN_EVENTS", "150")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_MIN_SESSIONS", "300")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    fake_now = datetime(2026, 5, 15, 10, 5, 0)

    from app.db.database import get_session
    from app.crashguard.models import CrashIssue

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="newcrash1",
            platform="ios",
            title="NewBug",
            first_seen_at=fake_now - timedelta(days=10),
        ))
        await session.commit()

    raw_24h = [{"id": "newcrash1", "attributes": {
        "events_count": 200, "sessions_affected": 400, "title": "NewBug", "platform": "ios",
    }}]

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=[])), \
         patch("app.crashguard.services.hourly_alerter._fetch_24h_events",
               new=AsyncMock(return_value=raw_24h)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(force=True, now=fake_now)

    assert result["alerted"] is True
    assert result.get("new_crash", 0) == 1


@pytest.mark.asyncio
async def test_channel_3_blocked_when_first_seen_too_old(tmp_path, monkeypatch):
    """通道 3：first_seen 在 60 天前（> new_window_days=30）→ 不触发。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_SHADOW_MODE", "false")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_MIN_EVENTS", "150")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_MIN_SESSIONS", "300")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    fake_now = datetime(2026, 5, 15, 10, 5, 0)

    from app.db.database import get_session
    from app.crashguard.models import CrashIssue

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="oldcrash1",
            platform="ios",
            title="OldBug",
            first_seen_at=fake_now - timedelta(days=60),
        ))
        await session.commit()

    raw_24h = [{"id": "oldcrash1", "attributes": {
        "events_count": 200, "sessions_affected": 400, "title": "OldBug", "platform": "ios",
    }}]

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=[])), \
         patch("app.crashguard.services.hourly_alerter._fetch_24h_events",
               new=AsyncMock(return_value=raw_24h)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(force=True, now=fake_now)

    assert result.get("new_crash", 0) == 0


@pytest.mark.asyncio
async def test_channel_3_blocked_when_events_below_threshold(tmp_path, monkeypatch):
    """通道 3：events_count=100 < min_events=150 → 不触发。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_SHADOW_MODE", "false")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_MIN_EVENTS", "150")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_MIN_SESSIONS", "300")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    fake_now = datetime(2026, 5, 15, 10, 5, 0)

    from app.db.database import get_session
    from app.crashguard.models import CrashIssue

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="lowevents1",
            platform="android",
            title="LowEventsBug",
            first_seen_at=fake_now - timedelta(days=10),
        ))
        await session.commit()

    raw_24h = [{"id": "lowevents1", "attributes": {
        "events_count": 100, "sessions_affected": 400, "title": "LowEventsBug", "platform": "android",
    }}]

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=[])), \
         patch("app.crashguard.services.hourly_alerter._fetch_24h_events",
               new=AsyncMock(return_value=raw_24h)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(force=True, now=fake_now)

    assert result.get("new_crash", 0) == 0


# ===== Task 6: 三通道合卡 dedup + shadow_mode =====

@pytest.mark.asyncio
async def test_multi_channel_merge_keeps_new_version_priority(tmp_path, monkeypatch):
    """三通道合卡 dedup：同一 issue_id 在通道 1（new_version）和通道 3（new_crash）均命中时，
    new_version 优先级更高，通道 3 中该 issue 应被去重移除。
    """
    await _setup_db_and_settings(tmp_path, monkeypatch)
    # 打开通道 1（new_version）和通道 3（new_crash），关闭 shadow mode
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_SHADOW_MODE", "false")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_MIN_EVENTS", "30")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_USER_RATE_PCT", "0.005")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_SHADOW_MODE", "false")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_MIN_EVENTS", "50")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_MIN_SESSIONS", "100")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    fake_now = datetime(2026, 5, 15, 10, 5, 0)

    from app.db.database import get_session
    from app.crashguard.models import CrashIssue
    from app.crashguard.services.datadog_cache import DatadogCache
    DatadogCache.clear()

    # Pre-seed issue with first_seen_at within 30 days → channel 3 would also match
    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="dup1",
            platform="android",
            title="DupCrash",
            first_seen_at=fake_now - timedelta(days=5),
        ))
        await session.commit()

    # Channel 1: hourly events, version 3.20.0 > top_version 3.19.0 → bucket=new
    # events=50 >= min_events=30, user_rate = 50/10000 = 0.5% >= threshold=0.005
    hourly_issues = [{
        "id": "dup1",
        "type": "issue",
        "attributes": {
            "events_count": 50,
            "sessions_affected": 800,
            "title": "DupCrash",
            "platform": "android",
            "version": "3.20.0",
        },
    }]
    top_version_data = {"android": {"version": "3.19.0", "users": 10000}}

    # Channel 3: same issue_id "dup1" in 24h data, meets channel 3 thresholds
    raw_24h = [{"id": "dup1", "attributes": {
        "events_count": 200, "sessions_affected": 400, "title": "DupCrash", "platform": "android",
    }}]

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=hourly_issues)), \
         patch("app.crashguard.services.hourly_alerter.DatadogCache.get_or_fetch",
               new=AsyncMock(return_value=top_version_data)), \
         patch("app.crashguard.services.hourly_alerter._fetch_24h_events",
               new=AsyncMock(return_value=raw_24h)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(force=True, now=fake_now)

    # Channel 1 (new_version) wins; channel 3 (new_crash) dedup'd
    assert result["new_version"] == 1, f"expected new_version=1, got {result}"
    assert result["new_crash"] == 0, f"expected new_crash=0 (dedup'd), got {result}"


@pytest.mark.asyncio
async def test_shadow_mode_skips_feishu_when_only_new_version_hits(tmp_path, monkeypatch):
    """Shadow mode：仅通道 1 有命中且处于 shadow_mode=true，不发飞书但保留 audit log。"""
    await _setup_db_and_settings(tmp_path, monkeypatch)
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_SHADOW_MODE", "true")  # shadow on
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_MIN_EVENTS", "30")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_USER_RATE_PCT", "0.005")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_ENABLED", "false")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    fake_now = datetime(2026, 5, 15, 11, 5, 0)  # different hour to avoid idempotency conflict

    from app.crashguard.services.datadog_cache import DatadogCache
    DatadogCache.clear()

    # Channel 1 has 1 hit (shadow); channels 2/3 have 0 hits
    hourly_issues = [{
        "id": "shadow_ver_1",
        "type": "issue",
        "attributes": {
            "events_count": 50,
            "sessions_affected": 800,
            "title": "ShadowVersionCrash",
            "platform": "android",
            "version": "3.20.0",
        },
    }]
    top_version_data = {"android": {"version": "3.19.0", "users": 10000}}

    mock_send = AsyncMock(return_value=True)

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=hourly_issues)), \
         patch("app.crashguard.services.hourly_alerter.DatadogCache.get_or_fetch",
               new=AsyncMock(return_value=top_version_data)), \
         patch("app.crashguard.services.hourly_alerter._fetch_24h_events",
               new=AsyncMock(return_value=[])), \
         patch("app.services.feishu_cli.send_interactive_card", mock_send):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(force=True, now=fake_now)

    # Shadow mode: audit log written but feishu NOT sent
    assert result.get("shadow") is True, f"expected shadow=True, got {result}"
    assert result.get("alerted") is False, f"expected alerted=False, got {result}"
    assert mock_send.call_count == 0, \
        f"send_interactive_card should NOT be called in shadow mode, got {mock_send.call_count} calls"


@pytest.mark.asyncio
async def test_real_send_when_mixed_shadow_and_real_hit(tmp_path, monkeypatch):
    """混合场景：通道 1 shadow_mode=true 有命中，但通道 2（new_items）也有命中 →
    通道 2 强制真发，shadow_mode 不生效。
    """
    await _setup_db_and_settings(tmp_path, monkeypatch)
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_SHADOW_MODE", "true")  # shadow on
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_MIN_EVENTS", "30")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_USER_RATE_PCT", "0.005")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_ENABLED", "false")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    fake_now = datetime(2026, 5, 15, 12, 5, 0)  # different hour

    from app.crashguard.services.datadog_cache import DatadogCache
    DatadogCache.clear()

    # Two issues:
    # 1. "shadow_ver_2": version > top_version → channel 1 hit (shadow)
    # 2. "real_new_1": no DB record → channel 2 hit (new_items, always real)
    hourly_issues = [
        {
            "id": "shadow_ver_2",
            "type": "issue",
            "attributes": {
                "events_count": 50,
                "sessions_affected": 800,
                "title": "ShadowVersionCrash2",
                "platform": "android",
                "version": "3.20.0",
            },
        },
        {
            "id": "real_new_1",
            "type": "issue",
            "attributes": {
                "events_count": 200,
                "sessions_affected": 300,
                "title": "RealNewCrash",
                "platform": "android",
                "version": "3.19.0",  # same as top_version → not "new" bucket → channel 2
            },
        },
    ]
    top_version_data = {"android": {"version": "3.19.0", "users": 10000}}

    mock_send = AsyncMock(return_value=True)

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=hourly_issues)), \
         patch("app.crashguard.services.hourly_alerter.DatadogCache.get_or_fetch",
               new=AsyncMock(return_value=top_version_data)), \
         patch("app.crashguard.services.hourly_alerter._fetch_24h_events",
               new=AsyncMock(return_value=[])), \
         patch("app.services.feishu_cli.send_interactive_card", mock_send):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(force=True, now=fake_now)

    # Channel 2 (new_items) forces real send despite channel 1 being in shadow
    assert result.get("shadow") is not True, f"shadow should not be active, got {result}"
    assert result.get("alerted") is True, f"expected alerted=True (real send), got {result}"
    assert mock_send.call_count == 1, \
        f"send_interactive_card should be called once, got {mock_send.call_count}"


# ===== 通道 3：API first_seen 优先（Sprint A）=====

@pytest.mark.asyncio
async def test_channel_3_uses_api_first_seen_when_db_missing(tmp_path, monkeypatch):
    """通道 3：DB 无记录时用 API first_seen_timestamp（5 天内）触发告警，first_seen_source='api'。"""
    import json
    await _setup_db_and_settings(tmp_path, monkeypatch)
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_SHADOW_MODE", "false")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_MIN_EVENTS", "150")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_MIN_SESSIONS", "300")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    fake_now = datetime(2026, 5, 14, 10, 5, 0)
    # API 返回 first_seen_timestamp：5 天内（new_window_days=30 内）
    api_first_seen = (fake_now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 不在 DB 预置 CrashIssue，让 DB 查询返回 None
    raw_24h = [{"id": "apinew1", "attributes": {
        "events_count": 200,
        "sessions_affected": 400,
        "title": "APIFirstSeenCrash",
        "platform": "ios",
        "first_seen_timestamp": api_first_seen,
    }}]

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=[])), \
         patch("app.crashguard.services.hourly_alerter._fetch_24h_events",
               new=AsyncMock(return_value=raw_24h)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(force=True, now=fake_now)

    assert result.get("new_crash", 0) == 1, f"expected new_crash=1, got {result}"
    assert result.get("alerted") is True

    # 从 DB alert_payload 验证 first_seen_source == "api"
    from app.db.database import get_session
    from app.crashguard.models import CrashHourlyAlert
    async with get_session() as session:
        from sqlalchemy import select
        row = (await session.execute(select(CrashHourlyAlert))).scalars().first()
    assert row is not None
    payload = json.loads(row.alert_payload)
    new_crash_list = payload.get("new_crash") or []
    assert len(new_crash_list) == 1
    assert new_crash_list[0]["first_seen_source"] == "api", (
        f"expected first_seen_source='api', got {new_crash_list[0]}"
    )


@pytest.mark.asyncio
async def test_channel_3_prefers_api_over_db_for_freshness(tmp_path, monkeypatch):
    """通道 3 API 优先语义：API first_seen=60 天前 > DB first_seen=5 天前，应不告警。

    DB 滞后记录（"近期才写入"）不代表 crash 真实很新；API 反映真实历史首次出现时间。
    API 值 60 天前超过 new_window_days=30，故不触发。
    """
    await _setup_db_and_settings(tmp_path, monkeypatch)
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_SHADOW_MODE", "false")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_MIN_EVENTS", "150")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_CRASH_MIN_SESSIONS", "300")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    fake_now = datetime(2026, 5, 14, 10, 5, 0)

    from app.db.database import get_session
    from app.crashguard.models import CrashIssue

    # DB 预置 first_seen_at=5 天前（本身属于"新"，如果纯 DB 逻辑会触发告警）
    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="stale_api_1",
            platform="android",
            title="StaleAPIBug",
            first_seen_at=fake_now - timedelta(days=5),
        ))
        await session.commit()

    # API 返回 first_seen_timestamp=60 天前（真实历史时间，早于 new_window_days=30）
    api_first_seen_old = (fake_now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw_24h = [{"id": "stale_api_1", "attributes": {
        "events_count": 200,
        "sessions_affected": 400,
        "title": "StaleAPIBug",
        "platform": "android",
        "first_seen_timestamp": api_first_seen_old,
    }}]

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=[])), \
         patch("app.crashguard.services.hourly_alerter._fetch_24h_events",
               new=AsyncMock(return_value=raw_24h)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(force=True, now=fake_now)

    # API 优先：60 天前超出 new_window_days=30，不应触发
    assert result.get("new_crash", 0) == 0, (
        f"expected new_crash=0 (API overrides stale DB), got {result}"
    )


# ===== Sprint B: 通道 1 user_rate 分母校准（3h 窗口）=====

@pytest.mark.asyncio
async def test_channel_1_uses_3h_denom_when_available(tmp_path, monkeypatch):
    """Sprint B：3h 分母可用时，user_rate 用 3h 窗口计算（夜间低流量场景召回）。

    场景：凌晨 3-6 点 UTC（日本/欧洲深夜）
    - 24h 累计用户 10000，但过去 3h 实际活跃用户只有 800
    - events_h=50 → 24h 分母 rate=50/10000=0.5%（仅刚踩阈值）
    - 3h 分母 rate=50/800=6.25%（严重告警级，远超 0.5% 阈值）
    期望：用 3h 分母，rate=6.25% 触发告警；denom_source="3h"
    """
    import json
    await _setup_db_and_settings(tmp_path, monkeypatch)
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_SHADOW_MODE", "false")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_MIN_EVENTS", "30")
    # 阈值设为 1%（介于 24h rate=0.5% 和 3h rate=6.25% 之间）
    # 若用 24h 分母：0.5% < 1% → 不触发；若用 3h 分母：6.25% > 1% → 触发
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_USER_RATE_PCT", "0.01")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    fake_now = datetime(2026, 5, 15, 10, 5, 0)
    issues = [{
        "id": "test_3h_denom_1",
        "type": "issue",
        "attributes": {
            "events_count": 50,
            "sessions_affected": 800,
            "title": "NightlyCrash",
            "platform": "android",
            "version": "3.20.0-700",
        },
    }]

    from app.crashguard.services.datadog_cache import DatadogCache
    DatadogCache.clear()

    # mock 按 key 分流：24h 返回 10000 用户，3h 返回 800 用户（夜间低流量）
    async def _mock_cache(key, ttl_seconds, fetch_fn):
        if key == "top_user_version:24":
            return {"android": {"version": "3.19.0-600", "users": 10000}}
        elif key == "top_user_version:3":
            return {"android": {"version": "3.19.0-600", "users": 800}}
        return {}

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.crashguard.services.hourly_alerter.DatadogCache.get_or_fetch",
               new=AsyncMock(side_effect=_mock_cache)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(force=True, now=fake_now)

    # 3h 分母（800）：50/800=6.25% > 1% 阈值 → 应触发
    assert result["alerted"] is True, (
        f"expected alerted=True with 3h denom (6.25% > 1%), got {result}"
    )
    assert result["new_version"] == 1, (
        f"expected new_version=1, got {result}"
    )

    # 验证 audit payload 中 denom_source="3h"
    from app.db.database import get_session
    from app.crashguard.models import CrashHourlyAlert
    from sqlalchemy import select
    async with get_session() as session:
        row = (await session.execute(select(CrashHourlyAlert))).scalars().first()
    assert row is not None
    payload = json.loads(row.alert_payload)
    nv_list = payload.get("new_version") or []
    assert len(nv_list) == 1, f"expected 1 new_version item, got {nv_list}"
    assert nv_list[0]["denom_source"] == "3h", (
        f"expected denom_source='3h', got {nv_list[0]}"
    )
    assert nv_list[0]["denom_users"] == 800, (
        f"expected denom_users=800 (3h window), got {nv_list[0]}"
    )


@pytest.mark.asyncio
async def test_channel_1_falls_back_to_24h_denom(tmp_path, monkeypatch):
    """Sprint B：3h 分母缺失时自动降级到 24h 兜底（如新版本 day-1 上线 <3h 数据不足）。

    场景：3h 窗口数据为空（{}），24h 窗口有数据（8000 用户）
    - events=50, denom=8000 → rate=50/8000=0.625% > 0.5% 阈值 → 触发
    期望：denom_source="24h_fallback"，告警正常发出
    """
    import json
    await _setup_db_and_settings(tmp_path, monkeypatch)
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_SHADOW_MODE", "false")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_MIN_EVENTS", "30")
    monkeypatch.setenv("CRASHGUARD_HOURLY_ALERT_NEW_VERSION_USER_RATE_PCT", "0.005")
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    fake_now = datetime(2026, 5, 15, 13, 5, 0)  # different hour to avoid idempotency conflict
    issues = [{
        "id": "test_24h_fallback_1",
        "type": "issue",
        "attributes": {
            "events_count": 50,
            "sessions_affected": 800,
            "title": "NewVersionFallback",
            "platform": "android",
            "version": "3.20.0-700",
        },
    }]

    from app.crashguard.services.datadog_cache import DatadogCache
    DatadogCache.clear()

    # mock：3h 数据缺失（{}），24h 有数据
    async def _mock_cache(key, ttl_seconds, fetch_fn):
        if key == "top_user_version:24":
            return {"android": {"version": "3.19.0-600", "users": 8000}}
        elif key == "top_user_version:3":
            return {}  # 3h 数据缺失
        return {}

    with patch("app.crashguard.services.hourly_alerter._fetch_hourly_events",
               new=AsyncMock(return_value=issues)), \
         patch("app.crashguard.services.hourly_alerter.DatadogCache.get_or_fetch",
               new=AsyncMock(side_effect=_mock_cache)), \
         patch("app.services.feishu_cli.send_interactive_card",
               new=AsyncMock(return_value=True)):
        from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
        result = await run_hourly_alert_tick(force=True, now=fake_now)

    # 24h 兜底（8000）：50/8000=0.625% > 0.5% 阈值 → 应触发
    assert result["alerted"] is True, (
        f"expected alerted=True with 24h fallback denom (0.625% > 0.5%), got {result}"
    )
    assert result["new_version"] == 1, (
        f"expected new_version=1, got {result}"
    )

    # 验证 audit payload 中 denom_source="24h_fallback"
    from app.db.database import get_session
    from app.crashguard.models import CrashHourlyAlert
    from sqlalchemy import select
    async with get_session() as session:
        row = (await session.execute(select(CrashHourlyAlert))).scalars().first()
    assert row is not None
    payload = json.loads(row.alert_payload)
    nv_list = payload.get("new_version") or []
    assert len(nv_list) == 1, f"expected 1 new_version item, got {nv_list}"
    assert nv_list[0]["denom_source"] == "24h_fallback", (
        f"expected denom_source='24h_fallback', got {nv_list[0]}"
    )
    assert nv_list[0]["denom_users"] == 8000, (
        f"expected denom_users=8000 (24h fallback), got {nv_list[0]}"
    )
