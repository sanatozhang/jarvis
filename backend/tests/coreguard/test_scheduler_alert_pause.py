"""coreguard 报警暂停开关：alert_enabled=False → hourly_watch 以 dry_run 跑。

关键约束：早报（crashguard 早报里的 coreguard 板块）读的是 hourly_watch 写入的
CoreguardMetricSnapshot。所以「暂停报警」不能停掉 watch（会饿死快照 → 早报消失），
而是让 watch 继续评估 + 写快照，只把飞书告警分发关掉 —— 正是 run_all(dry_run=True)。
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.coreguard.workers import scheduler as sched


def _settings(alert_enabled):
    return SimpleNamespace(
        enabled=True, scheduler_enabled=True, alert_enabled=alert_enabled,
        hourly_watch_cron="15 * * * *",
    )


@pytest.mark.asyncio
async def test_hourly_watch_dry_run_when_alert_disabled():
    captured = {}

    async def fake_run_all(dry_run=False, force_alert=False):
        captured["dry_run"] = dry_run
        return {"evaluated": 1, "breached": 0, "healthy": 1, "errored": 0,
                "alert_sent": False}

    with patch("app.coreguard.services.metric_watcher.run_all",
               side_effect=fake_run_all), \
         patch.object(sched, "get_coreguard_settings",
                      return_value=_settings(alert_enabled=False)), \
         patch.object(sched, "_write_heartbeat", new=AsyncMock()):
        await sched._run_hourly_watch_once()

    # 暂停报警：快照照写，但 dry_run=True → 不发飞书
    assert captured["dry_run"] is True


@pytest.mark.asyncio
async def test_hourly_watch_sends_when_alert_enabled():
    captured = {}

    async def fake_run_all(dry_run=False, force_alert=False):
        captured["dry_run"] = dry_run
        return {"evaluated": 1, "breached": 0, "healthy": 1, "errored": 0,
                "alert_sent": False}

    with patch("app.coreguard.services.metric_watcher.run_all",
               side_effect=fake_run_all), \
         patch.object(sched, "get_coreguard_settings",
                      return_value=_settings(alert_enabled=True)), \
         patch.object(sched, "_write_heartbeat", new=AsyncMock()):
        await sched._run_hourly_watch_once()

    assert captured["dry_run"] is False


def test_alert_enabled_setting_defaults_true():
    """新开关默认 True（不改变现有行为），可经 COREGUARD_ALERT_ENABLED 覆盖。"""
    from app.coreguard.config import CoreguardSettings
    assert CoreguardSettings().alert_enabled is True
