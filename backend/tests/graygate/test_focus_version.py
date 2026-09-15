"""focus_version.py 单测。

纯 KV 读写用 mock（不碰真实 DB）；2026-09-15 新增的审计写入 + 通知用真实
in-memory DB（同 crashguard 测试的 patched_session 写法），因为要验证的正是
"落 DB、不随进程重启消失"这件事本身。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.graygate.services import focus_version as fv


@pytest.fixture
async def patched_session(db_engine):
    import app.db.database as db_mod
    import app.graygate.models  # noqa: F401 — 注册 graygate_* 表

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


def _mock_no_op_record_and_notify(monkeypatch):
    """屏蔽 _record_and_notify——纯 KV 读写测试不关心审计/通知这条副作用链路。"""
    mock = AsyncMock()
    monkeypatch.setattr(fv, "_record_and_notify", mock)
    return mock


@pytest.mark.asyncio
async def test_set_focus_version_writes_namespaced_key(monkeypatch):
    _mock_no_op_record_and_notify(monkeypatch)
    with patch.object(fv.db, "get_oncall_config", new=AsyncMock(return_value="")), \
         patch.object(fv.db, "set_oncall_config", new=AsyncMock()) as mock_set:
        await fv.set_focus_version("ios", "4.0.302-1050")
    mock_set.assert_awaited_once_with("graygate_focus_version_ios", "4.0.302-1050")


@pytest.mark.asyncio
async def test_get_focus_version_returns_none_when_unset():
    with patch.object(fv.db, "get_oncall_config", new=AsyncMock(return_value="")):
        result = await fv.get_focus_version("android")
    assert result is None


@pytest.mark.asyncio
async def test_get_focus_version_returns_stored_value():
    with patch.object(fv.db, "get_oncall_config", new=AsyncMock(return_value="4.0.302-2010")):
        result = await fv.get_focus_version("android")
    assert result == "4.0.302-2010"


@pytest.mark.asyncio
async def test_clear_focus_version_writes_empty_string(monkeypatch):
    _mock_no_op_record_and_notify(monkeypatch)
    with patch.object(fv.db, "get_oncall_config", new=AsyncMock(return_value="4.0.302-1050")), \
         patch.object(fv.db, "set_oncall_config", new=AsyncMock()) as mock_set:
        await fv.clear_focus_version("ios")
    mock_set.assert_awaited_once_with("graygate_focus_version_ios", "")


@pytest.mark.asyncio
async def test_get_all_focus_versions_returns_both_platforms():
    async def fake_get(key, default=""):
        return {"graygate_focus_version_ios": "4.0.302-1050"}.get(key, default)

    with patch.object(fv.db, "get_oncall_config", new=AsyncMock(side_effect=fake_get)):
        result = await fv.get_all_focus_versions()
    assert result == {"ios": "4.0.302-1050", "android": None}


@pytest.mark.asyncio
async def test_set_focus_version_records_audit_row_with_old_and_new_value(
    patched_session, monkeypatch,
):
    """变更真的落 DB——用户实测发现"查不到是谁改的"就是冲这个来的。"""
    from app.graygate.models import GraygateFocusVersionAudit
    from app.db.database import get_session

    monkeypatch.setattr(fv, "_notify_version_change", AsyncMock(return_value=True))
    with patch.object(fv.db, "get_oncall_config", new=AsyncMock(return_value="4.0.302-1100")), \
         patch.object(fv.db, "set_oncall_config", new=AsyncMock()):
        await fv.set_focus_version("ios", "4.0.302-1143", changed_by="sanato.zhang@plaud.ai")

    async with get_session() as session:
        rows = (await session.execute(
            select(GraygateFocusVersionAudit)
        )).scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.platform == "ios"
    assert row.old_value == "4.0.302-1100"
    assert row.new_value == "4.0.302-1143"
    assert row.changed_by == "sanato.zhang@plaud.ai"
    assert row.notify_sent is True


@pytest.mark.asyncio
async def test_set_focus_version_noop_when_value_unchanged_skips_audit_and_notify(
    patched_session, monkeypatch,
):
    """值没变化（同值重复设置）不记录也不通知，避免噪声。"""
    from app.graygate.models import GraygateFocusVersionAudit
    from app.db.database import get_session

    notify_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(fv, "_notify_version_change", notify_mock)
    with patch.object(fv.db, "get_oncall_config", new=AsyncMock(return_value="4.0.302-1143")), \
         patch.object(fv.db, "set_oncall_config", new=AsyncMock()):
        await fv.set_focus_version("ios", "4.0.302-1143", changed_by="sanato.zhang@plaud.ai")

    notify_mock.assert_not_called()
    async with get_session() as session:
        rows = (await session.execute(
            select(GraygateFocusVersionAudit)
        )).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_get_focus_version_audit_history_orders_recent_first(patched_session):
    from app.graygate.models import GraygateFocusVersionAudit
    from app.db.database import get_session

    async with get_session() as session:
        session.add(GraygateFocusVersionAudit(
            platform="ios", old_value="", new_value="4.0.302-1100", changed_by="a@plaud.ai",
        ))
        await session.commit()
        session.add(GraygateFocusVersionAudit(
            platform="ios", old_value="4.0.302-1100", new_value="4.0.302-1143", changed_by="b@plaud.ai",
        ))
        await session.commit()

    history = await fv.get_focus_version_audit_history("ios")
    assert len(history) == 2
    assert history[0]["new_value"] == "4.0.302-1143"  # 最近一条在前
    assert history[1]["new_value"] == "4.0.302-1100"


@pytest.mark.asyncio
async def test_notify_version_change_skips_when_chat_id_unset(monkeypatch):
    from unittest.mock import MagicMock

    s = MagicMock()
    s.feishu_enabled = True
    s.feishu_chat_id = ""
    monkeypatch.setattr(
        "app.graygate.config.get_graygate_settings", lambda: s,
    )
    sent = await fv._notify_version_change("ios", "4.0.302-1100", "4.0.302-1143", "sanato")
    assert sent is False


@pytest.mark.asyncio
async def test_notify_version_change_sends_via_im_bot(monkeypatch):
    """2026-09-15 实测：先前打算用的"主" app 不在目标群里（230002），改用
    jarvis 自己的 IM 专属 app——跟灰度日报本身走同一个已验证在群里的发送者身份。"""
    from unittest.mock import MagicMock

    s = MagicMock()
    s.feishu_enabled = True
    s.feishu_chat_id = "oc_test_grayscale_group"
    monkeypatch.setattr("app.graygate.config.get_graygate_settings", lambda: s)

    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.feishu_cli.send_interactive_card", send_mock,
    )

    sent = await fv._notify_version_change("ios", "4.0.302-1100", "4.0.302-1143", "sanato.zhang@plaud.ai")

    assert sent is True
    send_mock.assert_awaited_once()
    _, kwargs = send_mock.call_args
    assert kwargs["chat_id"] == "oc_test_grayscale_group"
