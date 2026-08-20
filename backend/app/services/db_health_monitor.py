"""SQLite 健康监控 + 早期预警（2026-08-20）。

背景：virtiofs（macOS Docker bind mount）与 SQLite WAL 模式的共享内存文件锁
支持不完整，102 服务器上从"偶发 disk I/O error"升级成真实的数据库损坏
（"database disk image is malformed"），发生过 4 次，每次都是等业务流程报错
才发现，没有任何提前信号。`database.py` 已经把 journal_mode 从 WAL 改成
DELETE 治本，这里补上"如果它又坏了 / 又开始抖动，能不能比业务报错更早知道"
这层监控。

三层信号：
1. I/O 错误频率异常——软提醒，可能只是 virtiofs 抖动，不代表已经坏了。
2. 定期 integrity_check（跑在 sqlite3.backup() 快照上，不锁活库）——硬告警，
   真的坏了。这个快照顺便当备份用，把"事故发生到最近一次可用备份"的窗口从
   之前的 24 小时（只有每日 02:00 一次）缩到 30 分钟。
3. journal_mode 漂移哨兵——配置被意外改回 WAL 的信号，冷却期内不重复告警。

故意不做的事：检测到损坏后自动切回最近快照——那是个写操作性质的决策，跟
`pr_conflict_resync` 默认关闭是同一个考量，留给人工决定，不在这里自动做。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.services import db_health_state as state

logger = logging.getLogger("jarvis.db_health_monitor")

_TICK_SEC = 300  # 5 分钟一个 tick
_IO_ERROR_WINDOW_SEC = 600  # 10 分钟窗口
_IO_ERROR_THRESHOLD = 5  # 窗口内 ≥5 次算异常
_IO_ALERT_COOLDOWN_SEC = 1800  # 同一种软提醒 30 分钟内不重复发
_INTEGRITY_CHECK_INTERVAL_SEC = 1800  # 30 分钟做一次快照+完整性检查
_INTEGRITY_ALERT_COOLDOWN_SEC = 900  # integrity_check 失败告警冷却（仍然频繁提醒，但不是每 5min 一次）
_JOURNAL_ALERT_COOLDOWN_SEC = 3600  # journal_mode 漂移 1 小时内不重复告警
_HEALTH_SNAPSHOT_KEEP = 24  # 30min * 24 = 最近 12 小时的高频快照
_HEALTH_SNAPSHOT_SUBDIR = "backups/health"

_last_integrity_check_at: float = 0.0


def _health_snapshot_dir() -> Optional[Path]:
    from app.db.database import get_sqlite_file_path
    db_path = get_sqlite_file_path()
    if not db_path:
        return None
    return Path(db_path).parent / _HEALTH_SNAPSHOT_SUBDIR


def _snapshot_and_check_integrity(db_path: str, snapshot_dir: Path) -> tuple[bool, str]:
    """同步阻塞函数——调用方必须用 asyncio.to_thread 跑，别卡事件循环。

    用 sqlite3.backup() 做在线快照（不像直接对活库跑 integrity_check 那样长时间
    占读锁——backup API 是设计给"边写边备份"用的，backup-db.sh 已经在用同一招）。
    返回 (ok, detail)。ok=True 时 detail 是快照文件名；False 时 detail 是错误信息。
    """
    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    snapshot_path = snapshot_dir / f"appllo_health_{ts}.db"
    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(str(snapshot_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    except Exception as e:
        return False, f"backup failed: {e}"

    try:
        check_conn = sqlite3.connect(str(snapshot_path))
        result = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
        check_conn.close()
    except Exception as e:
        return False, f"integrity_check errored: {e}"

    if result != "ok":
        # 坏快照留着供排查，不要静默删除——但也不当正常备份用。
        return False, f"integrity_check failed: {result}"

    _rotate_health_snapshots(snapshot_dir)
    return True, snapshot_path.name


def _rotate_health_snapshots(snapshot_dir: Path) -> None:
    """只保留最近 _HEALTH_SNAPSHOT_KEEP 份"好"快照；坏快照（上面提前 return 的那种）不受这里管，
    需要人工看。这里只清 appllo_health_*.db 这种正常产出的文件。
    """
    try:
        files = sorted(
            snapshot_dir.glob("appllo_health_*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in files[_HEALTH_SNAPSHOT_KEEP:]:
        try:
            stale.unlink()
        except OSError:
            pass


def _check_journal_mode_sync(db_path: str) -> tuple[bool, str]:
    try:
        conn = sqlite3.connect(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
    except Exception as e:
        return False, f"journal_mode query failed: {e}"
    if (mode or "").lower() != "delete":
        return False, f"journal_mode drifted to '{mode}' (expected 'delete')"
    return True, mode


async def _send_alert(text: str) -> None:
    settings = get_settings()
    target = getattr(settings, "db_health_alert_email", "") or "sanato.zhang@plaud.ai"
    try:
        from app.services.feishu_cli import send_message
        await send_message(email=target, text=text)
    except Exception:
        logger.exception("db_health_monitor: failed to send Feishu alert")


async def _check_io_error_frequency() -> None:
    count = state.count_recent_io_errors(_IO_ERROR_WINDOW_SEC)
    if count < _IO_ERROR_THRESHOLD:
        return
    if not state.should_alert("io_error_frequency", _IO_ALERT_COOLDOWN_SEC):
        return
    await _send_alert(
        f"⚠️ SQLite 过去 10 分钟内出现 {count} 次 disk I/O error（阈值 {_IO_ERROR_THRESHOLD}）。\n"
        "目前是软提醒——可能只是 virtiofs 抖动，业务连接会自动重试，但频率异常本身"
        "值得留意，之前的数据库损坏事故就是先有一段时间的密集报错。"
    )


async def _check_integrity_and_snapshot() -> None:
    global _last_integrity_check_at
    now = time.time()
    if now - _last_integrity_check_at < _INTEGRITY_CHECK_INTERVAL_SEC:
        return
    _last_integrity_check_at = now

    from app.db.database import get_sqlite_file_path
    db_path = get_sqlite_file_path()
    if not db_path or not os.path.exists(db_path):
        return  # 非 sqlite 后端，或还没初始化，静默跳过
    snapshot_dir = _health_snapshot_dir()
    if snapshot_dir is None:
        return

    ok, detail = await asyncio.to_thread(_snapshot_and_check_integrity, db_path, str(snapshot_dir))
    state.record_integrity_check(ok, detail, now=now)
    if not ok and state.should_alert("integrity_check_failed", _INTEGRITY_ALERT_COOLDOWN_SEC, now=now):
        await _send_alert(
            f"🔴 数据库完整性检查失败：{detail}\n"
            "这不是瞬时抖动，是真的结构性问题，需要人工介入（评估从最近快照恢复）。\n"
            f"快照目录：{snapshot_dir}"
        )


async def _check_journal_mode() -> None:
    from app.db.database import get_sqlite_file_path
    db_path = get_sqlite_file_path()
    if not db_path or not os.path.exists(db_path):
        return
    ok, detail = await asyncio.to_thread(_check_journal_mode_sync, db_path)
    state.record_journal_mode_check(ok, detail)
    if not ok and state.should_alert("journal_mode_drift", _JOURNAL_ALERT_COOLDOWN_SEC):
        await _send_alert(
            f"🟡 SQLite journal_mode 配置漂移：{detail}\n"
            "预期一直是 DELETE 模式（WAL 在 virtiofs 上会导致数据库损坏，8月20日事故的根因）。"
            "如果不是有意改回去的，需要马上查为什么。"
        )


async def run_once() -> None:
    """跑一次三层检查；单独拆出来方便测试直接调用，不用绕开 while+sleep。"""
    settings = get_settings()
    if not getattr(settings, "db_health_monitor_enabled", True):
        return
    await _check_io_error_frequency()
    await _check_integrity_and_snapshot()
    await _check_journal_mode()


async def db_health_monitor_loop() -> None:
    """启动即跑一轮，随后每 _TICK_SEC 跑一次。整个 loop 任何一步出错都不能带崩主服务。"""
    logger.info("db_health_monitor_loop started")
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("db_health_monitor tick failed (continuing)")
        try:
            await asyncio.sleep(_TICK_SEC)
        except asyncio.CancelledError:
            raise
