"""验证 coreguard 飞书告警群配额 + overflow 路由。

底层逻辑：
- 群配额未满 → 走群（target_kind='group'）
- 群配额已满 → 走 overflow_email（target_kind='email', overflow_from_group=True）
- 都没配置 → skip 不发
"""
from __future__ import annotations

from datetime import date as _date_t
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.database import Base


def _fake_card(title: str = "[coreguard] ⚠️ 核心指标异常告警 (1/18)"):
    return {
        "header": {"title": {"tag": "plain_text", "content": title}},
        "elements": [],
    }


@pytest.fixture()
async def fresh_engine():
    """每个测试一个独立 in-memory DB，含 coreguard_* 表。"""
    # 触发 coreguard 模型注册（保证 Base.metadata.create_all 时建表）
    from app.coreguard import models as _m  # noqa: F401
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture()
async def patched_db(fresh_engine):
    """把模块级 _engine / _session_factory 指到测试 engine。"""
    import app.db.database as db_mod
    orig_e, orig_f = db_mod._engine, db_mod._session_factory
    db_mod._engine = fresh_engine
    db_mod._session_factory = async_sessionmaker(fresh_engine, expire_on_commit=False)
    yield
    db_mod._engine = orig_e
    db_mod._session_factory = orig_f


def _settings_with(**overrides):
    """构造一个最小 CoreguardSettings stub（绕过 lru_cache）。"""
    from app.coreguard.config import CoreguardSettings
    defaults = dict(
        feishu_enabled=True,
        feishu_target_chat_id="oc_TEST_GROUP",
        feishu_target_email="user@plaud.ai",
        feishu_overflow_email="user@plaud.ai",
        feishu_group_daily_quota=2,
    )
    defaults.update(overrides)
    return CoreguardSettings(**defaults)


async def _count_dispatches(target_kind: str | None = None):
    from app.coreguard.models import CoreguardAlertDispatch
    from app.db.database import get_session
    async with get_session() as session:
        q = select(func.count(CoreguardAlertDispatch.id))
        if target_kind:
            q = q.where(CoreguardAlertDispatch.target_kind == target_kind)
        return int((await session.execute(q)).scalar_one() or 0)


@pytest.mark.asyncio
async def test_group_under_quota_sends_to_group(patched_db):
    """配额未满 → 走群通道，dispatch 记 target_kind='group'。"""
    from app.coreguard.services import feishu_summary_card as fsc

    sent_args = {}

    async def _fake_send(*, chat_id=None, email=None, card=None):
        sent_args["chat_id"] = chat_id
        sent_args["email"] = email
        return True

    with patch("app.coreguard.config.get_coreguard_settings", return_value=_settings_with()), \
         patch("app.services.feishu_cli.send_interactive_card", new=AsyncMock(side_effect=_fake_send)):
        ok = await fsc.send(_fake_card(), breach_count=1)

    assert ok is True
    assert sent_args.get("chat_id") == "oc_TEST_GROUP"
    assert sent_args.get("email") is None
    assert await _count_dispatches("group") == 1
    assert await _count_dispatches("email") == 0


@pytest.mark.asyncio
async def test_group_at_quota_overflows_to_email(patched_db):
    """已发 2 条（=quota）后第 3 条走 overflow_email；dispatch 标 overflow_from_group=True。"""
    from app.coreguard.models import CoreguardAlertDispatch
    from app.coreguard.services import feishu_summary_card as fsc
    from app.db.database import get_session

    today = _date_t.today()
    async with get_session() as session:
        for _ in range(2):
            session.add(CoreguardAlertDispatch(
                sent_date=today, target_kind="group",
                target_value="oc_TEST_GROUP", sent_ok=True,
                alert_title="seed", breach_count=1, overflow_from_group=False,
            ))
        await session.commit()

    received = {}

    async def _fake_send(*, chat_id=None, email=None, card=None):
        received["chat_id"] = chat_id
        received["email"] = email
        return True

    with patch("app.coreguard.config.get_coreguard_settings", return_value=_settings_with()), \
         patch("app.services.feishu_cli.send_interactive_card", new=AsyncMock(side_effect=_fake_send)):
        ok = await fsc.send(_fake_card(), breach_count=3)

    assert ok is True
    assert received.get("email") == "user@plaud.ai"
    assert received.get("chat_id") is None
    assert await _count_dispatches("email") == 1
    # 群计数不变（仍是初始 seed 的 2）
    assert await _count_dispatches("group") == 2

    # 新增的一条 email dispatch 应标 overflow_from_group=True
    async with get_session() as session:
        last = (await session.execute(
            select(CoreguardAlertDispatch)
            .where(CoreguardAlertDispatch.target_kind == "email")
            .order_by(CoreguardAlertDispatch.id.desc())
        )).scalars().first()
    assert last.overflow_from_group is True


@pytest.mark.asyncio
async def test_failed_group_send_does_not_consume_quota(patched_db):
    """群发送失败（sent_ok=False）不计入配额，下次仍可发群。"""
    from app.coreguard.services import feishu_summary_card as fsc

    call_count = {"n": 0}

    async def _fake_send(*, chat_id=None, email=None, card=None):
        call_count["n"] += 1
        # 第 1 次群失败，第 2 次群成功
        if chat_id and call_count["n"] == 1:
            return False
        return True

    with patch("app.coreguard.config.get_coreguard_settings", return_value=_settings_with()), \
         patch("app.services.feishu_cli.send_interactive_card", new=AsyncMock(side_effect=_fake_send)):
        ok1 = await fsc.send(_fake_card(), breach_count=1)
        ok2 = await fsc.send(_fake_card(), breach_count=1)

    assert ok1 is False
    assert ok2 is True
    # 两次都尝试发群（第一次失败不计入配额）
    assert await _count_dispatches("group") == 2
    assert await _count_dispatches("email") == 0


@pytest.mark.asyncio
async def test_no_chat_id_falls_back_to_email_immediately(patched_db):
    """没配群 → 直接走 overflow_email，不算 overflow_from_group。"""
    from app.coreguard.services import feishu_summary_card as fsc
    from app.coreguard.models import CoreguardAlertDispatch
    from app.db.database import get_session

    s = _settings_with(feishu_target_chat_id="")  # 无群

    received = {}

    async def _fake_send(*, chat_id=None, email=None, card=None):
        received["email"] = email
        return True

    with patch("app.coreguard.config.get_coreguard_settings", return_value=s), \
         patch("app.services.feishu_cli.send_interactive_card", new=AsyncMock(side_effect=_fake_send)):
        ok = await fsc.send(_fake_card(), breach_count=1)

    assert ok is True
    assert received.get("email") == "user@plaud.ai"
    async with get_session() as session:
        last = (await session.execute(
            select(CoreguardAlertDispatch).order_by(CoreguardAlertDispatch.id.desc())
        )).scalars().first()
    assert last.target_kind == "email"
    # 无群配置 → 不算 overflow_from_group
    assert last.overflow_from_group is False


@pytest.mark.asyncio
async def test_no_targets_skips(patched_db):
    """chat_id / overflow_email / target_email 都没 → 不发，不写 dispatch。"""
    from app.coreguard.services import feishu_summary_card as fsc

    s = _settings_with(
        feishu_target_chat_id="", feishu_target_email="", feishu_overflow_email="",
    )
    sent_calls = AsyncMock(return_value=True)
    with patch("app.coreguard.config.get_coreguard_settings", return_value=s), \
         patch("app.services.feishu_cli.send_interactive_card", new=sent_calls):
        ok = await fsc.send(_fake_card(), breach_count=1)

    assert ok is False
    assert sent_calls.await_count == 0
    assert await _count_dispatches() == 0
