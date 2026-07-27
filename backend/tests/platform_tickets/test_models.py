"""PlatformTicket (pt_tickets) 模型测试 — 建表 / 插入 / 查询。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker


def test_pt_tickets_table_registered():
    """pt_tickets 必须注册进 SQLAlchemy Base.metadata（main.py lifespan 靠这个建表）。"""
    from app.platform_tickets import models  # noqa: F401
    from app.db.database import Base

    assert "pt_tickets" in Base.metadata.tables


def test_pt_tickets_no_foreign_keys_to_other_domains():
    """pt_* 表不能有外键指向非 pt_* 表（复用参数化后的解耦自检）。"""
    from app.platform_tickets import models  # noqa: F401
    from app.db.database import Base
    from scripts.check_crash_decoupling import find_violating_foreign_keys

    tables = list(Base.metadata.tables.values())
    violations = find_violating_foreign_keys(tables, prefixes=("crash_", "pt_"))
    assert violations == [], f"发现违规外键: {violations}"


async def test_insert_and_query_platform_ticket(db_engine):
    """起临时 SQLite（复用 tests/conftest.py 的 db_engine fixture），
    验证 PlatformTicket 能建表、插入、查询一条记录成功。
    """
    from app.platform_tickets.models import PlatformTicket

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        ticket = PlatformTicket(
            id="pt_test0001",
            platform="web",
            description="登录后白屏",
            priority="P1",
            source="web_widget",
            status="pending",
            rule_type="",
            category="登录",
            created_by="tester",
            payload_json='{"url": "https://app.plaud.ai/dashboard", "browser": "Chrome 126"}',
        )
        session.add(ticket)
        await session.commit()

    async with factory() as session:
        row = (
            await session.execute(
                select(PlatformTicket).where(PlatformTicket.id == "pt_test0001")
            )
        ).scalar_one_or_none()
        assert row is not None
        assert row.platform == "web"
        assert row.description == "登录后白屏"
        assert row.priority == "P1"
        assert row.status == "pending"
        assert row.deleted in (False, 0, None)  # SQLite Boolean 兼容
        assert "Chrome" in row.payload_json
