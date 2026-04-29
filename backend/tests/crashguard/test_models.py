"""crashguard 模型测试"""
from __future__ import annotations

import pytest


def test_all_seven_tables_present():
    """7 张 crash_* 表必须全部定义"""
    from app.crashguard import models  # noqa: F401
    from app.db.database import Base

    expected = {
        "crash_issues",
        "crash_snapshots",
        "crash_fingerprints",
        "crash_analyses",
        "crash_pull_requests",
        "crash_daily_reports",
        "crash_versions",
    }
    actual = {t for t in Base.metadata.tables if t.startswith("crash_")}
    assert expected.issubset(actual), f"缺失表: {expected - actual}"


def test_no_foreign_keys_to_jarvis_tables():
    """crash_* 表不能有外键指向非 crash_* 表"""
    from app.crashguard import models  # noqa: F401
    from app.db.database import Base
    from scripts.check_crash_decoupling import find_violating_foreign_keys

    tables = list(Base.metadata.tables.values())
    violations = find_violating_foreign_keys(tables)
    assert violations == [], f"发现违规外键: {violations}"


def test_unique_constraint_snapshots():
    """crash_snapshots 必须有 (datadog_issue_id, snapshot_date) 唯一约束"""
    from app.crashguard.models import CrashSnapshot

    constraints = CrashSnapshot.__table__.constraints
    has_unique = any(
        len(c.columns) == 2
        and {col.name for col in c.columns} == {"datadog_issue_id", "snapshot_date"}
        for c in constraints
        if c.__class__.__name__ == "UniqueConstraint"
    )
    assert has_unique


def test_crash_versions_composite_pk():
    """crash_versions 必须以 (version, platform) 为复合主键"""
    from app.crashguard.models import CrashVersion

    pk_cols = {c.name for c in CrashVersion.__table__.primary_key.columns}
    assert pk_cols == {"version", "platform"}
