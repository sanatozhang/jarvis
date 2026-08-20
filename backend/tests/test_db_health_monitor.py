"""db_health_monitor 单测。

覆盖三层：I/O 错误频率（mock 计数）、integrity_check（真的造一个坏文件，不是
纯 mock）、journal_mode 漂移（真的开一个 WAL 库去触发）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.services import db_health_monitor as mon
from app.services import db_health_state as state


@pytest.fixture(autouse=True)
def _reset_state():
    state._io_error_times.clear()
    state._last_alert_at.clear()
    yield
    state._io_error_times.clear()
    state._last_alert_at.clear()


@pytest.mark.asyncio
async def test_run_once_noop_when_disabled(monkeypatch):
    from app import config as config_mod
    settings = config_mod.get_settings()
    monkeypatch.setattr(settings, "db_health_monitor_enabled", False)

    called = {"io": False}

    async def _boom():
        called["io"] = True

    monkeypatch.setattr(mon, "_check_io_error_frequency", _boom)
    await mon.run_once()
    assert called["io"] is False


@pytest.mark.asyncio
async def test_io_error_frequency_alert_fires_above_threshold(monkeypatch):
    for _ in range(mon._IO_ERROR_THRESHOLD):
        state.record_io_error()

    sent = []

    async def _fake_alert(text):
        sent.append(text)

    monkeypatch.setattr(mon, "_send_alert", _fake_alert)
    await mon._check_io_error_frequency()
    assert len(sent) == 1
    assert "disk I/O error" in sent[0]


@pytest.mark.asyncio
async def test_io_error_frequency_respects_cooldown(monkeypatch):
    for _ in range(mon._IO_ERROR_THRESHOLD):
        state.record_io_error()

    sent = []

    async def _fake_alert(text):
        sent.append(text)

    monkeypatch.setattr(mon, "_send_alert", _fake_alert)
    await mon._check_io_error_frequency()
    await mon._check_io_error_frequency()
    assert len(sent) == 1  # 第二次在冷却期内，不该再发


@pytest.mark.asyncio
async def test_io_error_frequency_below_threshold_no_alert(monkeypatch):
    state.record_io_error()  # 只有 1 次，远低于阈值

    sent = []

    async def _fake_alert(text):
        sent.append(text)

    monkeypatch.setattr(mon, "_send_alert", _fake_alert)
    await mon._check_io_error_frequency()
    assert sent == []


def _make_valid_sqlite_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA page_size=4096")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    for i in range(2000):
        conn.execute("INSERT INTO t (val) VALUES (?)", (f"row-{i}-" + "x" * 40,))
    conn.commit()
    conn.close()


def _corrupt_page2_cell_count(path: Path) -> None:
    """把 page 2（第一个数据页，page_size=4096）b-tree 页头里的 cell-count 字段
    砸成垃圾值——这是可靠触发 "database disk image is malformed" 的手法。
    （随手改中间某段字节命中的往往是未用空间，integrity_check 测不出来。）
    """
    raw = bytearray(path.read_bytes())
    page2_start = 4096
    raw[page2_start + 3] = 0xFF
    raw[page2_start + 4] = 0xFF
    path.write_bytes(bytes(raw))


@pytest.mark.asyncio
async def test_integrity_check_snapshot_ok_on_healthy_db(tmp_path):
    db_path = tmp_path / "healthy.db"
    _make_valid_sqlite_db(db_path)
    snapshot_dir = tmp_path / "backups" / "health"

    ok, detail = await __import__("asyncio").to_thread(
        mon._snapshot_and_check_integrity, str(db_path), str(snapshot_dir)
    )
    assert ok is True
    assert (snapshot_dir / detail).exists()


@pytest.mark.asyncio
async def test_integrity_check_detects_real_corruption(tmp_path):
    """不是 mock——真的造一个物理损坏的 sqlite 文件，验证 integrity_check 真能测出来。"""
    db_path = tmp_path / "corrupt.db"
    _make_valid_sqlite_db(db_path)
    _corrupt_page2_cell_count(db_path)

    snapshot_dir = tmp_path / "backups" / "health"
    ok, detail = await __import__("asyncio").to_thread(
        mon._snapshot_and_check_integrity, str(db_path), str(snapshot_dir)
    )
    assert ok is False
    assert any(s in detail for s in ("integrity_check failed", "integrity_check errored", "backup failed"))


@pytest.mark.asyncio
async def test_check_integrity_and_snapshot_alerts_on_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "corrupt.db"
    _make_valid_sqlite_db(db_path)
    _corrupt_page2_cell_count(db_path)

    monkeypatch.setattr("app.db.database.get_sqlite_file_path", lambda: str(db_path))
    monkeypatch.setattr(mon, "_health_snapshot_dir", lambda: tmp_path / "backups" / "health")
    monkeypatch.setattr(mon, "_last_integrity_check_at", 0.0)

    sent = []

    async def _fake_alert(text):
        sent.append(text)

    monkeypatch.setattr(mon, "_send_alert", _fake_alert)

    await mon._check_integrity_and_snapshot()
    assert len(sent) == 1
    assert "完整性检查失败" in sent[0]
    snap = state.get_snapshot()
    assert snap.integrity.ok is False


def test_journal_mode_check_ok_for_delete_mode(tmp_path):
    db_path = tmp_path / "delete_mode.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    ok, detail = mon._check_journal_mode_sync(str(db_path))
    assert ok is True


def test_journal_mode_check_detects_wal_drift(tmp_path):
    db_path = tmp_path / "wal_mode.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    ok, detail = mon._check_journal_mode_sync(str(db_path))
    assert ok is False
    assert "wal" in detail.lower()


def test_rotate_health_snapshots_keeps_only_latest_n(tmp_path, monkeypatch):
    snapshot_dir = tmp_path / "snaps"
    snapshot_dir.mkdir()
    monkeypatch.setattr(mon, "_HEALTH_SNAPSHOT_KEEP", 3)

    import time
    for i in range(6):
        f = snapshot_dir / f"appllo_health_{i:02d}.db"
        f.write_text("x")
        # 保证 mtime 递增且互不相同
        mtime = time.time() + i
        __import__("os").utime(f, (mtime, mtime))

    mon._rotate_health_snapshots(snapshot_dir)
    remaining = sorted(snapshot_dir.glob("appllo_health_*.db"))
    assert len(remaining) == 3
    # 保留下来的应该是最新的三个（05, 04, 03）
    assert {p.name for p in remaining} == {
        "appllo_health_05.db", "appllo_health_04.db", "appllo_health_03.db",
    }
