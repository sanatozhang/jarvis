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


def test_pt_prefix_violation_detected():
    """pt_* 表（platform_tickets 模块）外键指向非 pt_* 表也应被判定违规。"""
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

    # pt_tickets 有非法外键指向 jarvis 主表 issues —— 违规
    t1 = FakeTable("pt_tickets", [FakeColumn([FakeFK("issues.id")])])
    # 假想的 pt_comments 合法外键指向 pt_tickets —— OK（同前缀域）
    t2 = FakeTable("pt_comments", [FakeColumn([FakeFK("pt_tickets.id")])])
    # crash_* 和 pt_* 互相指向对方同样违规：两个域各自独立，不允许跨域 FK
    t3 = FakeTable("crash_issues", [FakeColumn([FakeFK("pt_tickets.id")])])
    t4 = FakeTable("pt_tickets", [FakeColumn([FakeFK("crash_issues.id")])])

    violations = find_violating_foreign_keys([t1, t2, t3, t4], prefixes=("crash_", "pt_"))
    violation_tables = {v["table"] for v in violations}
    assert "pt_tickets" in violation_tables
    assert "pt_comments" not in violation_tables
    # both t3 (crash_issues -> pt_tickets) and t4 (pt_tickets -> crash_issues) violate
    assert len(violations) == 3


def test_default_prefixes_include_pt():
    """默认前缀集合应同时覆盖 crash_ 和 pt_，无需每次调用手动传参。"""
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

    t1 = FakeTable("pt_tickets", [FakeColumn([FakeFK("issues.id")])])

    # 不传 prefixes，走默认值，pt_* 违规依然能被发现
    violations = find_violating_foreign_keys([t1])
    assert len(violations) == 1
    assert violations[0]["table"] == "pt_tickets"
