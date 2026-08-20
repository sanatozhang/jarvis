"""SQLite journal_mode 回归测试（2026-08-20）。

背景：virtiofs（macOS Docker bind mount）上 WAL 模式的共享内存文件锁支持不完整，
102 服务器上从"偶发 disk I/O error"升级成真实的 "database disk image is
malformed"。治本方案：连接建立时改用 DELETE（传统 rollback journal）模式，
不再依赖 -wal/-shm 共享内存文件。这里验证改动真的生效，而不是留在注释里。
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_new_sqlite_connections_use_delete_journal_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'jmode.db'}")
    from app.config import get_settings
    get_settings.cache_clear()

    from app.db import database as db_mod
    db_mod._engine = None
    db_mod._session_factory = None
    await db_mod.init_db()

    async with db_mod.get_session() as session:
        from sqlalchemy import text
        result = await session.execute(text("PRAGMA journal_mode"))
        mode = result.scalar()
        assert mode.lower() == "delete"

        sync_result = await session.execute(text("PRAGMA synchronous"))
        # SQLite returns the numeric level; FULL == 2
        assert sync_result.scalar() == 2

    # No -wal/-shm sidecar files should exist under DELETE mode.
    db_path = tmp_path / "jmode.db"
    assert not (tmp_path / "jmode.db-wal").exists()
    assert not (tmp_path / "jmode.db-shm").exists()
    assert db_path.exists()
