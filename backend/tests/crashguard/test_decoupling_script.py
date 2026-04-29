"""DB 解耦自检脚本测试"""
from __future__ import annotations


def test_check_no_foreign_keys_to_jarvis_tables():
    """crash_* 表不能有外键指向非 crash_* 表"""
    from scripts.check_crash_decoupling import find_violating_foreign_keys

    # 模拟 SQLAlchemy metadata
    class FakeFK:
        def __init__(self, target):
            self.target_fullname = target

    class FakeColumn:
        def __init__(self, fks):
            self.foreign_keys = fks

    class FakeTable:
        def __init__(self, name, columns):
            self.name = name
            self.columns = columns

    # crash_issues 有合法外键到 crash_snapshots（同前缀，OK）
    t1 = FakeTable("crash_issues", [
        FakeColumn([FakeFK("crash_snapshots.id")]),
    ])
    # crash_pull_requests 有非法外键到 issues（jarvis 主表，违规）
    t2 = FakeTable("crash_pull_requests", [
        FakeColumn([FakeFK("issues.id")]),
    ])
    # 普通 jarvis 表不在检查范围
    t3 = FakeTable("issues", [
        FakeColumn([FakeFK("users.id")]),
    ])

    violations = find_violating_foreign_keys([t1, t2, t3])
    assert len(violations) == 1
    assert violations[0]["table"] == "crash_pull_requests"
    assert "issues.id" in violations[0]["target"]


def test_no_violations_passes():
    from scripts.check_crash_decoupling import find_violating_foreign_keys

    class FakeFK:
        def __init__(self, target):
            self.target_fullname = target

    class FakeColumn:
        def __init__(self, fks):
            self.foreign_keys = fks

    class FakeTable:
        def __init__(self, name, columns):
            self.name = name
            self.columns = columns

    t1 = FakeTable("crash_issues", [FakeColumn([])])
    t2 = FakeTable("crash_snapshots", [FakeColumn([FakeFK("crash_issues.id")])])

    assert find_violating_foreign_keys([t1, t2]) == []
