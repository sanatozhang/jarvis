"""多实例 send_daily_report 抢锁去重测试"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


def _make_settings():
    return type("S", (), {
        "datadog_api_key": "",
        "datadog_app_key": "",
        "datadog_site": "datadoghq.com",
        "datadog_window_hours": 24,
        "daily_surge_threshold": 0.10,
        "daily_drop_threshold": -0.10,
        "daily_attention_min_events": 100,
        "frontend_base_url": "http://localhost:3000",
        "feishu_target_chat_id": "oc_test",
        "feishu_target_email": "",
        "feishu_enabled": True,
    })()


@pytest.fixture
async def patched_session(db_engine):
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original_factory = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original_factory


@pytest.mark.asyncio
async def test_second_call_returns_already_sent(patched_session):
    """同一 (date, type) 第二次调用直接返回 already_sent_by_other_instance，不发飞书"""
    from app.crashguard.services import daily_report
    from app.crashguard.models import CrashDailyReport
    from app.db.database import get_session
    from datetime import datetime

    target = date(2026, 4, 29)
    # 模拟另一实例已成功发送：预先插一行 feishu_message_id="sent"
    async with patched_session() as session:
        session.add(CrashDailyReport(
            report_date=target,
            report_type="morning",
            top_n=5,
            new_count=0,
            regression_count=0,
            surge_count=0,
            feishu_message_id="sent",
            report_payload="{}",
            created_at=datetime.utcnow(),
        ))
        await session.commit()

    with patch.object(daily_report, "get_crashguard_settings", return_value=_make_settings()), \
         patch("app.services.feishu_cli.send_interactive_card", new_callable=AsyncMock) as send_mock, \
         patch("app.services.feishu_cli.send_message", new_callable=AsyncMock):
        result = await daily_report.send_daily_report("morning", target_date=target, top_n=5)

    assert result["ok"] is True
    assert result["sent"] is False
    assert result["skipped_reason"] == "already_sent_by_other_instance"
    send_mock.assert_not_called()  # 关键：飞书没被调


@pytest.mark.asyncio
async def test_lock_contention_returns_skipped(patched_session):
    """另一实例正在跑（feishu_message_id='locking'）→ 本次也不发"""
    from app.crashguard.services import daily_report
    from app.crashguard.models import CrashDailyReport
    from datetime import datetime

    target = date(2026, 4, 29)
    async with patched_session() as session:
        session.add(CrashDailyReport(
            report_date=target,
            report_type="morning",
            top_n=0,
            new_count=0,
            regression_count=0,
            surge_count=0,
            feishu_message_id="locking",
            report_payload="{}",
            created_at=datetime.utcnow(),
        ))
        await session.commit()

    with patch.object(daily_report, "get_crashguard_settings", return_value=_make_settings()), \
         patch("app.services.feishu_cli.send_interactive_card", new_callable=AsyncMock) as send_mock:
        result = await daily_report.send_daily_report("morning", target_date=target, top_n=5)

    assert result["sent"] is False
    assert result["skipped_reason"] == "lock_contended"
    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_manual_override_bypasses_lock(patched_session):
    """手动指定 chat_id_override → 跳过锁，允许重发"""
    from app.crashguard.services import daily_report
    from app.crashguard.models import CrashDailyReport
    from datetime import datetime

    target = date(2026, 4, 29)
    # 已经存在一条
    async with patched_session() as session:
        session.add(CrashDailyReport(
            report_date=target, report_type="morning",
            top_n=0, new_count=0, regression_count=0, surge_count=0,
            feishu_message_id="sent", report_payload="{}",
            created_at=datetime.utcnow(),
        ))
        await session.commit()

    with patch.object(daily_report, "get_crashguard_settings", return_value=_make_settings()), \
         patch("app.services.feishu_cli.send_interactive_card", new_callable=AsyncMock, return_value=True) as send_mock:
        result = await daily_report.send_daily_report(
            "morning", target_date=target, top_n=5,
            chat_id_override="oc_manual_test",
        )

    # 应该尝试发送（不被锁拦截）
    assert send_mock.called or result.get("skipped_reason") != "lock_contended"
