"""db_health_state 纯函数单测：计数窗口 + 告警冷却。"""
from __future__ import annotations

from app.services import db_health_state as state


def test_count_recent_io_errors_prunes_old_entries():
    state._io_error_times.clear()
    now = 1_000_000.0
    state.record_io_error(now - 700)  # 超过 600s 窗口，该被剔除
    state.record_io_error(now - 100)
    state.record_io_error(now - 50)
    count = state.count_recent_io_errors(600, now=now)
    assert count == 2
    # 剔除后队列里也不该再有那条老记录
    assert all(t >= now - 600 for t in state._io_error_times)


def test_should_alert_respects_cooldown():
    state._last_alert_at.clear()
    now = 2_000_000.0
    assert state.should_alert("kind_a", 1800, now=now) is True
    # 冷却期内第二次不该再触发
    assert state.should_alert("kind_a", 1800, now=now + 60) is False
    # 冷却期过了之后该恢复
    assert state.should_alert("kind_a", 1800, now=now + 1801) is True


def test_should_alert_kinds_are_independent():
    state._last_alert_at.clear()
    now = 3_000_000.0
    assert state.should_alert("kind_a", 1800, now=now) is True
    # 不同 kind 互不影响冷却
    assert state.should_alert("kind_b", 1800, now=now) is True


def test_get_snapshot_reflects_recorded_checks():
    state._io_error_times.clear()
    state.record_integrity_check(True, "ok")
    state.record_journal_mode_check(True, "delete")
    snap = state.get_snapshot()
    assert snap.integrity.ok is True
    assert snap.journal_mode.ok is True
    assert snap.recent_io_errors_10min == 0
