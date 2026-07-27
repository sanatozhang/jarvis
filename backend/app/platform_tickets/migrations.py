"""
Platform Tickets 轻量自动迁移 — 启动时补齐新增列。

仿照 `app.crashguard.migrations` 的 `ensure_columns()` 模式（`_REQUIRED_COLUMNS`
列表 + `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`），但当前 `pt_tickets` 是
全新表、全新列——建表本身由 SQLAlchemy `Base.metadata.create_all`
（走 `app.db.database.init_db()`）自动完成，这里先留空骨架，未来给
`pt_tickets` 加列时往 `_REQUIRED_COLUMNS` 追加即可，无需改调用方。
"""
from __future__ import annotations

import logging
from typing import List, Tuple

from sqlalchemy import text

from app.db.database import get_session

logger = logging.getLogger("platform_tickets.migrations")

# (table, column, ddl_type, default_sql) — 目前为空，pt_tickets 全部列随建表一次到位。
_REQUIRED_COLUMNS: List[Tuple[str, str, str, str]] = []


async def ensure_columns() -> None:
    if not _REQUIRED_COLUMNS:
        return
    async with get_session() as session:
        for table, column, ddl_type, default in _REQUIRED_COLUMNS:
            existing = await _list_columns(session, table)
            if column in existing:
                continue
            ddl = f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type} DEFAULT {default}"
            await session.execute(text(ddl))
            logger.info("platform_tickets migration: %s.%s added", table, column)
        await session.commit()


async def _list_columns(session, table: str) -> List[str]:
    rows = (await session.execute(text(f"PRAGMA table_info({table})"))).all()
    return [r[1] for r in rows]
