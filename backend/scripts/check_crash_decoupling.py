"""
DB 隔离自检 — 表前缀参数化，检查两个独立解耦域：`crash_*`（crashguard）与
`pt_*`（platform_tickets）。

每张受保护前缀表的外键 target 必须指向**同一前缀**的表。这是两个各自独立的
解耦域——不要求 `crash_*` 和 `pt_*` 互相指向对方合法，只要求各自不外泄到
"不受保护的" jarvis 核心表（`issues`/`tasks`/...）或对方的前缀域。

启动时跑（main.py lifespan），违规则 raise，阻止启动。
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

_DEFAULT_PREFIXES: Tuple[str, ...] = ("crash_", "pt_")


def find_violating_foreign_keys(
    tables: List[Any],
    prefixes: Sequence[str] = _DEFAULT_PREFIXES,
) -> List[Dict[str, str]]:
    """
    返回违规外键列表。

    对每张表名以 `prefixes` 中任一前缀开头的表：检查其每个外键的 target 表名
    是否以**该表所属的同一个前缀**开头。若不是，则记为违规。

    例：`crash_pull_requests` 的外键必须指向 `crash_*` 表；`pt_tickets` 的外键
    必须指向 `pt_*` 表。`crash_*` 表的外键指向 `pt_*`（或反之）同样算违规——
    两个域互相独立，不允许跨域 FK。

    不匹配任何 `prefixes` 的表（如普通 jarvis 表 `issues`）不在检查范围内。
    """
    violations: List[Dict[str, str]] = []
    for table in tables:
        matched_prefix = next((p for p in prefixes if table.name.startswith(p)), None)
        if matched_prefix is None:
            continue
        for col in table.columns:
            for fk in col.foreign_keys:
                target = fk.target_fullname
                target_table = target.split(".", 1)[0]
                if not target_table.startswith(matched_prefix):
                    violations.append({
                        "table": table.name,
                        "target": target,
                    })
    return violations


def assert_crash_tables_decoupled(prefixes: Sequence[str] = _DEFAULT_PREFIXES) -> None:
    """启动时调用，违规则 raise RuntimeError。

    函数名保持 `assert_crash_tables_decoupled` 不变（向后兼容 `main.py` 现有
    调用），但内部现在同时检查 `("crash_", "pt_")` 两个独立前缀域。
    """
    from app.db.database import Base
    tables = list(Base.metadata.tables.values())
    violations = find_violating_foreign_keys(tables, prefixes)
    if violations:
        msg_lines = ["受保护前缀表存在违规外键，违反解耦约束（ADR-0001 / platform_tickets 同款约束）:"]
        for v in violations:
            msg_lines.append(f"  - {v['table']} -> {v['target']}")
        raise RuntimeError("\n".join(msg_lines))


if __name__ == "__main__":
    # 命令行调用方式: python -m scripts.check_crash_decoupling
    import sys
    sys.path.insert(0, ".")
    try:
        assert_crash_tables_decoupled()
        print("✅ crash_* / pt_* 表解耦检查通过")
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)
