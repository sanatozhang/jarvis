"""
Crashguard DB 隔离自检 — crash_* 表禁止外键指向非 crash_* 表。

启动时跑（main.py lifespan），违规则 raise，阻止启动。
"""
from __future__ import annotations

from typing import Any, Dict, List


def find_violating_foreign_keys(tables: List[Any]) -> List[Dict[str, str]]:
    """
    返回违规外键列表。

    违规定义: 表名以 'crash_' 开头，但其某个外键 target 不以 'crash_' 开头。
    """
    violations: List[Dict[str, str]] = []
    for table in tables:
        if not table.name.startswith("crash_"):
            continue
        for col in table.columns:
            for fk in col.foreign_keys:
                target = fk.target_fullname
                target_table = target.split(".", 1)[0]
                if not target_table.startswith("crash_"):
                    violations.append({
                        "table": table.name,
                        "target": target,
                    })
    return violations


def assert_crash_tables_decoupled() -> None:
    """启动时调用，违规则 raise RuntimeError"""
    from app.db.database import Base
    tables = list(Base.metadata.tables.values())
    violations = find_violating_foreign_keys(tables)
    if violations:
        msg_lines = ["crash_* 表存在违规外键，违反 ADR-0001 解耦约束:"]
        for v in violations:
            msg_lines.append(f"  - {v['table']} -> {v['target']}")
        raise RuntimeError("\n".join(msg_lines))


if __name__ == "__main__":
    # 命令行调用方式: python -m scripts.check_crash_decoupling
    import sys
    sys.path.insert(0, ".")
    try:
        assert_crash_tables_decoupled()
        print("✅ crash_* 表解耦检查通过")
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)
