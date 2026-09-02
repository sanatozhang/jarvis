"""graygate.workers.scheduler 单测。

覆盖 task-6-brief.md「验证要求」第 1 条列出的全部场景：
1. BJT 9 点整命中 → 触发一次 build_report_card + 发送（mock send_interactive_card）。
2. 同一天内多次 tick（9:00 和 9:01 都落在 hour==9）→ 只发一次（进程级幂等）。
3. 次日 9 点 → 再次触发（幂等状态按天重置）。
4. enabled=False / scheduler_enabled=False → 不调用 build_report_card。
5. feishu_enabled=False → 调用 build_report_card 但不调用 send_interactive_card。
6. GraygateReportCard(available=False) → 不调用 send_interactive_card，心跳仍然写
   （断言被调用且 summary 里记录了 available=false 的事实）。

全部 mock `get_graygate_settings` / `_now_bjt` / `build_report_card` / `send_interactive_card` /
`_write_heartbeat`，不碰真实 DB / Datadog / 飞书。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.graygate.workers import scheduler as sched

_BJT = ZoneInfo("Asia/Shanghai")


def _settings(
    enabled: bool = True,
    scheduler_enabled: bool = True,
    feishu_enabled: bool = True,
    report_hour_bjt: int = 9,
    feishu_chat_id: str = "oc_graygate",
    alert_email: str = "test-alert@plaud.ai",
) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=enabled,
        scheduler_enabled=scheduler_enabled,
        feishu_enabled=feishu_enabled,
        report_hour_bjt=report_hour_bjt,
        feishu_chat_id=feishu_chat_id,
        alert_email=alert_email,
    )


@pytest.fixture(autouse=True)
def _reset_idempotency_state():
    """`_last_fired_date` 是模块级全局，测试间必须互相隔离。"""
    sched._last_fired_date = None
    yield
    sched._last_fired_date = None


def _report(available: bool = True, card: Optional[dict] = None) -> object:
    from app.graygate.services.card_builder import GraygateReportCard
    return GraygateReportCard(available=available, card=card if card is not None else {"schema": "2.0"})


@pytest.mark.asyncio
async def test_fires_at_report_hour_and_sends():
    """BJT 9 点整命中 → build_report 被调用一次，且用昨天作为 target_date，
    send_message 被调用一次（feishu_enabled=True，available=True）。"""
    fake_now = datetime(2026, 8, 19, 9, 0, 0, tzinfo=_BJT)

    with patch.object(sched, "get_graygate_settings", return_value=_settings()), \
         patch.object(sched, "_now_bjt", return_value=fake_now), \
         patch.object(sched, "_already_handled_today", new=AsyncMock(return_value=False)), \
         patch.object(sched, "build_report_card", new=AsyncMock(return_value=_report())) as mock_build, \
         patch.object(sched, "send_interactive_card", new=AsyncMock(return_value=True)) as mock_send, \
         patch.object(sched, "_write_heartbeat", new=AsyncMock()) as mock_hb:
        await sched._tick_once()

    mock_build.assert_awaited_once_with(date(2026, 8, 18))  # BJT 昨天
    mock_send.assert_awaited_once_with(chat_id="oc_graygate", card={"schema": "2.0"})
    mock_hb.assert_awaited_once()
    status, duration_ms, summary, error = mock_hb.await_args.args
    assert status == "success"
    assert summary["sent"] is True


@pytest.mark.asyncio
async def test_same_day_multiple_ticks_send_only_once():
    """9:00 和 9:01 都落在 hour==9 → 只应该真正触发一次（进程级按天幂等）。"""
    with patch.object(sched, "get_graygate_settings", return_value=_settings()), \
         patch.object(sched, "_already_handled_today", new=AsyncMock(return_value=False)), \
         patch.object(sched, "build_report_card", new=AsyncMock(return_value=_report())) as mock_build, \
         patch.object(sched, "send_interactive_card", new=AsyncMock(return_value=True)), \
         patch.object(sched, "_write_heartbeat", new=AsyncMock()):

        with patch.object(sched, "_now_bjt", return_value=datetime(2026, 8, 19, 9, 0, 0, tzinfo=_BJT)):
            await sched._tick_once()
        with patch.object(sched, "_now_bjt", return_value=datetime(2026, 8, 19, 9, 1, 0, tzinfo=_BJT)):
            await sched._tick_once()

    mock_build.assert_awaited_once()  # 第二次 tick 被幂等挡住，没有第二次调用


@pytest.mark.asyncio
async def test_next_day_fires_again_after_idempotency_reset():
    """次日同一小时 → 再次触发，幂等状态按天重置，不是永久锁死。"""
    with patch.object(sched, "get_graygate_settings", return_value=_settings()), \
         patch.object(sched, "_already_handled_today", new=AsyncMock(return_value=False)), \
         patch.object(sched, "build_report_card", new=AsyncMock(return_value=_report())) as mock_build, \
         patch.object(sched, "send_interactive_card", new=AsyncMock(return_value=True)), \
         patch.object(sched, "_write_heartbeat", new=AsyncMock()):

        with patch.object(sched, "_now_bjt", return_value=datetime(2026, 8, 19, 9, 0, 0, tzinfo=_BJT)):
            await sched._tick_once()
        with patch.object(sched, "_now_bjt", return_value=datetime(2026, 8, 20, 9, 0, 0, tzinfo=_BJT)):
            await sched._tick_once()

    assert mock_build.await_count == 2
    mock_build.assert_any_await(date(2026, 8, 18))
    mock_build.assert_any_await(date(2026, 8, 19))


@pytest.mark.asyncio
async def test_enabled_false_skips_build_report():
    with patch.object(sched, "get_graygate_settings", return_value=_settings(enabled=False)), \
         patch.object(sched, "_now_bjt", return_value=datetime(2026, 8, 19, 9, 0, 0, tzinfo=_BJT)), \
         patch.object(sched, "build_report_card", new=AsyncMock()) as mock_build, \
         patch.object(sched, "send_interactive_card", new=AsyncMock()) as mock_send, \
         patch.object(sched, "_write_heartbeat", new=AsyncMock()) as mock_hb:
        await sched._tick_once()

    mock_build.assert_not_awaited()
    mock_send.assert_not_awaited()
    mock_hb.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_enabled_false_skips_build_report():
    with patch.object(sched, "get_graygate_settings", return_value=_settings(scheduler_enabled=False)), \
         patch.object(sched, "_now_bjt", return_value=datetime(2026, 8, 19, 9, 0, 0, tzinfo=_BJT)), \
         patch.object(sched, "build_report_card", new=AsyncMock()) as mock_build, \
         patch.object(sched, "send_interactive_card", new=AsyncMock()) as mock_send, \
         patch.object(sched, "_write_heartbeat", new=AsyncMock()) as mock_hb:
        await sched._tick_once()

    mock_build.assert_not_awaited()
    mock_send.assert_not_awaited()
    mock_hb.assert_not_awaited()


@pytest.mark.asyncio
async def test_feishu_enabled_false_builds_but_does_not_send():
    """feishu_enabled=False 是"算但不吵群"的总闸：build_report 照跑，
    send_message 不应被调用；status 仍是 success（这是预期的跳过，不是失败）。"""
    with patch.object(sched, "get_graygate_settings", return_value=_settings(feishu_enabled=False)), \
         patch.object(sched, "_now_bjt", return_value=datetime(2026, 8, 19, 9, 0, 0, tzinfo=_BJT)), \
         patch.object(sched, "_already_handled_today", new=AsyncMock(return_value=False)), \
         patch.object(sched, "build_report_card", new=AsyncMock(return_value=_report())) as mock_build, \
         patch.object(sched, "send_interactive_card", new=AsyncMock()) as mock_send, \
         patch.object(sched, "_write_heartbeat", new=AsyncMock()) as mock_hb:
        await sched._tick_once()

    mock_build.assert_awaited_once()
    mock_send.assert_not_awaited()
    status, duration_ms, summary, error = mock_hb.await_args.args
    assert status == "success"
    assert summary["sent"] is False
    assert summary["skip_reason"] == "feishu_enabled=False"


@pytest.mark.asyncio
async def test_report_unavailable_skips_send_but_still_writes_heartbeat():
    """GraygateReportCard(available=False) → 不调用 send_interactive_card，心跳仍然写，
    summary 里记录 available=false 这个事实（不是报错，status 仍是 success）。"""
    with patch.object(sched, "get_graygate_settings", return_value=_settings()), \
         patch.object(sched, "_now_bjt", return_value=datetime(2026, 8, 19, 9, 0, 0, tzinfo=_BJT)), \
         patch.object(sched, "_already_handled_today", new=AsyncMock(return_value=False)), \
         patch.object(sched, "build_report_card", new=AsyncMock(return_value=_report(available=False))), \
         patch.object(sched, "send_interactive_card", new=AsyncMock()) as mock_send, \
         patch.object(sched, "_write_heartbeat", new=AsyncMock()) as mock_hb:
        await sched._tick_once()

    mock_send.assert_not_awaited()
    mock_hb.assert_awaited_once()
    status, duration_ms, summary, error = mock_hb.await_args.args
    assert status == "success"
    assert summary["available"] is False
    assert summary["sent"] is False


@pytest.mark.asyncio
async def test_send_failure_marks_degraded():
    """取数成功但 send_message 返回 False → status=degraded（不是 failed），并私聊告警。"""
    with patch.object(sched, "get_graygate_settings", return_value=_settings()), \
         patch.object(sched, "_now_bjt", return_value=datetime(2026, 8, 19, 9, 0, 0, tzinfo=_BJT)), \
         patch.object(sched, "_already_handled_today", new=AsyncMock(return_value=False)), \
         patch.object(sched, "build_report_card", new=AsyncMock(return_value=_report())), \
         patch.object(sched, "send_interactive_card", new=AsyncMock(return_value=False)), \
         patch.object(sched, "_write_heartbeat", new=AsyncMock()) as mock_hb, \
         patch.object(sched, "send_message", new=AsyncMock(return_value=True)) as mock_alert:
        await sched._tick_once()

    status, duration_ms, summary, error = mock_hb.await_args.args
    assert status == "degraded"
    assert summary["sent"] is False
    mock_alert.assert_awaited_once()
    assert mock_alert.await_args.kwargs["email"] == "test-alert@plaud.ai"


@pytest.mark.asyncio
async def test_build_report_exception_marks_failed():
    """build_report 本身抛异常 → status=failed，error 字段记录异常，并私聊告警。"""
    with patch.object(sched, "get_graygate_settings", return_value=_settings()), \
         patch.object(sched, "_now_bjt", return_value=datetime(2026, 8, 19, 9, 0, 0, tzinfo=_BJT)), \
         patch.object(sched, "_already_handled_today", new=AsyncMock(return_value=False)), \
         patch.object(sched, "build_report_card", new=AsyncMock(side_effect=RuntimeError("datadog boom"))), \
         patch.object(sched, "send_interactive_card", new=AsyncMock()) as mock_send, \
         patch.object(sched, "_write_heartbeat", new=AsyncMock()) as mock_hb, \
         patch.object(sched, "send_message", new=AsyncMock(return_value=True)) as mock_alert:
        await sched._tick_once()

    mock_send.assert_not_awaited()
    status, duration_ms, summary, error = mock_hb.await_args.args
    assert status == "failed"
    assert "datadog boom" in error
    mock_alert.assert_awaited_once()
    assert "datadog boom" in mock_alert.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_success_does_not_trigger_failure_alert():
    """正常发送成功时不该多此一举发告警——只有 degraded/failed 才告警。"""
    with patch.object(sched, "get_graygate_settings", return_value=_settings()), \
         patch.object(sched, "_now_bjt", return_value=datetime(2026, 8, 19, 9, 0, 0, tzinfo=_BJT)), \
         patch.object(sched, "_already_handled_today", new=AsyncMock(return_value=False)), \
         patch.object(sched, "build_report_card", new=AsyncMock(return_value=_report())), \
         patch.object(sched, "send_interactive_card", new=AsyncMock(return_value=True)), \
         patch.object(sched, "_write_heartbeat", new=AsyncMock()), \
         patch.object(sched, "send_message", new=AsyncMock()) as mock_alert:
        await sched._tick_once()

    mock_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_alert_send_error_does_not_propagate():
    """告警本身发送失败（比如飞书 API 也在抽风）绝不能把这次 job 结果搞崩。"""
    with patch.object(sched, "get_graygate_settings", return_value=_settings()), \
         patch.object(sched, "_now_bjt", return_value=datetime(2026, 8, 19, 9, 0, 0, tzinfo=_BJT)), \
         patch.object(sched, "_already_handled_today", new=AsyncMock(return_value=False)), \
         patch.object(sched, "build_report_card", new=AsyncMock(side_effect=RuntimeError("boom"))), \
         patch.object(sched, "_write_heartbeat", new=AsyncMock()) as mock_hb, \
         patch.object(sched, "send_message", new=AsyncMock(side_effect=RuntimeError("feishu also down"))):
        await sched._tick_once()  # 不应该抛出任何异常

    status, duration_ms, summary, error = mock_hb.await_args.args
    assert status == "failed"


@pytest.mark.asyncio
async def test_before_hour_does_not_fire():
    """当前小时早于 report_hour_bjt → 还没到点，不触发。"""
    with patch.object(sched, "get_graygate_settings", return_value=_settings(report_hour_bjt=9)), \
         patch.object(sched, "_now_bjt", return_value=datetime(2026, 8, 19, 8, 0, 0, tzinfo=_BJT)), \
         patch.object(sched, "build_report_card", new=AsyncMock()) as mock_build:
        await sched._tick_once()

    mock_build.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_tick_after_missed_hour_still_fires():
    """2026-09-02 事故场景：进程在 report_hour_bjt 之后才重启起来（比如宿主机
    整机重启撞上 9 点窗口），当前小时已经过了 9 点、内存态 `_last_fired_date`
    是 None（刚重启）、心跳表里也没有今天 target_date 的记录（`_already_handled_today`
    返回 False）→ 仍然应该补跑一次，不能因为错过了精确的整点就永久跳过当天。"""
    with patch.object(sched, "get_graygate_settings", return_value=_settings(report_hour_bjt=9)), \
         patch.object(sched, "_now_bjt", return_value=datetime(2026, 8, 19, 10, 9, 0, tzinfo=_BJT)), \
         patch.object(sched, "_already_handled_today", new=AsyncMock(return_value=False)), \
         patch.object(sched, "build_report_card", new=AsyncMock(return_value=_report())) as mock_build, \
         patch.object(sched, "send_interactive_card", new=AsyncMock(return_value=True)), \
         patch.object(sched, "_write_heartbeat", new=AsyncMock()):
        await sched._tick_once()

    mock_build.assert_awaited_once_with(date(2026, 8, 18))  # 仍然是 BJT 昨天


@pytest.mark.asyncio
async def test_restart_after_already_sent_does_not_resend():
    """跟上一条相对：如果心跳表显示今天这个 target_date 已经跑过（比如今早 9
    点正常发过，下午 14 点做了一次日常 redeploy 导致内存态 `_last_fired_date`
    被清零）→ 不应该重新触发、重复往群里发一遍。"""
    with patch.object(sched, "get_graygate_settings", return_value=_settings(report_hour_bjt=9)), \
         patch.object(sched, "_now_bjt", return_value=datetime(2026, 8, 19, 14, 0, 0, tzinfo=_BJT)), \
         patch.object(sched, "_already_handled_today", new=AsyncMock(return_value=True)), \
         patch.object(sched, "build_report_card", new=AsyncMock()) as mock_build, \
         patch.object(sched, "send_interactive_card", new=AsyncMock()) as mock_send:
        await sched._tick_once()
        assert sched._last_fired_date == date(2026, 8, 19)  # 补记内存态，避免每个 tick 都重新查心跳表

    mock_build.assert_not_awaited()
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_handled_today_true_when_heartbeat_summary_matches(tmp_path, monkeypatch):
    await _setup_real_db(tmp_path, monkeypatch)
    await _seed_heartbeat_with_summary(json.dumps({"target_date": "2026-08-18"}))

    assert await sched._already_handled_today(date(2026, 8, 18)) is True


@pytest.mark.asyncio
async def test_already_handled_today_false_when_summary_is_different_date(tmp_path, monkeypatch):
    await _setup_real_db(tmp_path, monkeypatch)
    await _seed_heartbeat_with_summary(json.dumps({"target_date": "2026-08-17"}))

    assert await sched._already_handled_today(date(2026, 8, 18)) is False


@pytest.mark.asyncio
async def test_already_handled_today_false_when_no_heartbeat_rows(tmp_path, monkeypatch):
    await _setup_real_db(tmp_path, monkeypatch)

    assert await sched._already_handled_today(date(2026, 8, 18)) is False


# ---------------------------------------------------------------------------
# _check_staleness —— 跟 crashguard job_health_alerter 的 stale 判定对齐（简化版）
#
# 覆盖真实 DB（不是全 mock）：直接往 coreguard_job_heartbeats 表插行，验证
# "多久没成功过"的判定逻辑本身是对的，不是只测调用链。
# ---------------------------------------------------------------------------


async def _setup_real_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'graygate_staleness.db'}")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.db.database import init_db
    from app.coreguard import models  # noqa: F401
    await init_db()


async def _seed_heartbeat(fired_at, status="success"):
    from app.db.database import get_session
    from app.coreguard.models import CoreguardJobHeartbeat
    async with get_session() as session:
        session.add(CoreguardJobHeartbeat(
            job_name=sched._JOB_NAME, fired_at=fired_at, status=status,
            duration_ms=100, summary="{}", error="",
        ))
        await session.commit()


async def _seed_heartbeat_with_summary(summary_json: str, status="success"):
    """跟 `_seed_heartbeat` 一样，但允许指定 `summary`——`_already_handled_today`
    读的就是 summary 里的 `target_date` 字段，测它必须能控制这个值。"""
    from datetime import datetime as dt
    from app.db.database import get_session
    from app.coreguard.models import CoreguardJobHeartbeat
    async with get_session() as session:
        session.add(CoreguardJobHeartbeat(
            job_name=sched._JOB_NAME, fired_at=dt.utcnow(), status=status,
            duration_ms=100, summary=summary_json, error="",
        ))
        await session.commit()


@pytest.fixture(autouse=True)
def _reset_staleness_state():
    sched._last_staleness_alert_at = None
    yield
    sched._last_staleness_alert_at = None


@pytest.mark.asyncio
async def test_staleness_alert_fires_when_last_success_too_old(tmp_path, monkeypatch):
    await _setup_real_db(tmp_path, monkeypatch)
    from datetime import datetime as dt, timedelta as td
    await _seed_heartbeat(dt.utcnow() - td(hours=30))  # 超过 26h 阈值

    with patch.object(sched, "get_graygate_settings", return_value=_settings()), \
         patch.object(sched, "send_message", new=AsyncMock(return_value=True)) as mock_alert:
        await sched._check_staleness()

    mock_alert.assert_awaited_once()
    assert "小时没成功运行" in mock_alert.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_staleness_alert_does_not_fire_when_recent_success_exists(tmp_path, monkeypatch):
    await _setup_real_db(tmp_path, monkeypatch)
    from datetime import datetime as dt, timedelta as td
    await _seed_heartbeat(dt.utcnow() - td(hours=5))  # 5h 前刚成功过，远低于阈值

    with patch.object(sched, "get_graygate_settings", return_value=_settings()), \
         patch.object(sched, "send_message", new=AsyncMock()) as mock_alert:
        await sched._check_staleness()

    mock_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_staleness_alert_ignores_failed_rows_only_counts_success(tmp_path, monkeypatch):
    """只有 failed 记录、没有任何 success（比如刚上线还没跑成功过一次）——
    不该告警，"从来没成功过"跟"曾经成功、现在停摆"是两件不同的事。"""
    await _setup_real_db(tmp_path, monkeypatch)
    from datetime import datetime as dt, timedelta as td
    await _seed_heartbeat(dt.utcnow() - td(hours=40), status="failed")

    with patch.object(sched, "get_graygate_settings", return_value=_settings()), \
         patch.object(sched, "send_message", new=AsyncMock()) as mock_alert:
        await sched._check_staleness()

    mock_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_staleness_alert_respects_cooldown(tmp_path, monkeypatch):
    await _setup_real_db(tmp_path, monkeypatch)
    from datetime import datetime as dt, timedelta as td
    await _seed_heartbeat(dt.utcnow() - td(hours=30))

    with patch.object(sched, "get_graygate_settings", return_value=_settings()), \
         patch.object(sched, "send_message", new=AsyncMock(return_value=True)) as mock_alert:
        await sched._check_staleness()
        await sched._check_staleness()  # 冷却期内，不该再发一次

    assert mock_alert.await_count == 1
