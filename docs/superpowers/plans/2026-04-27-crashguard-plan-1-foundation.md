# Crashguard Plan 1 / Foundation + Data Layer 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 crashguard 子模块骨架、强解耦防腐机制、7 张数据库表、Datadog 数据接入、stack_fingerprint 跨版本去重、三维新增分类（全新/回归/飙升）、Top20 crash-free 影响排序。完成后，jarvis 可以从 Datadog 拉数据、入库、分类、排序，**无 AI 分析、无报告**——下一阶段（Plan 2）补上。

**Architecture:** 作为 jarvis 子模块 `backend/app/crashguard/` 集成，独立子包 + 独立配置段 + 独立 DB 表前缀 `crash_*`。通过 import-linter + DB 自检脚本 + ADR + 模块级 CLAUDE.md 四层防腐机制强制隔离。所有耦合点显式声明（仅复用 `db/database.get_session`），未来可一键拆分。

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy async / pydantic-settings / httpx / pytest / import-linter

**Spec 来源：** `docs/superpowers/specs/2026-04-27-crashguard-design.md`（commit `2ecd225`）

---

## File Structure

```
backend/
├── app/
│   └── crashguard/                      ← 新建子包
│       ├── __init__.py                  ← 仅暴露 public 接口
│       ├── README.md                    ← 模块文档
│       ├── CLAUDE.md                    ← 模块级 AI 隔离指引
│       ├── config.py                    ← CrashguardSettings (pydantic-settings)
│       ├── models.py                    ← 7 张 crash_* 表
│       └── services/
│           ├── __init__.py
│           ├── datadog_client.py        ← Datadog Error Tracking API
│           ├── dedup.py                 ← stack_fingerprint 算法
│           ├── classifier.py            ← 三维分类
│           └── ranker.py                ← Top20 排序
├── tests/
│   └── crashguard/
│       ├── __init__.py
│       ├── conftest.py                  ← crashguard 专用 fixtures
│       ├── test_config.py
│       ├── test_models.py
│       ├── test_datadog_client.py
│       ├── test_dedup.py
│       ├── test_classifier.py
│       ├── test_ranker.py
│       └── fixtures/
│           ├── datadog_issue_response.json
│           └── stack_traces.json
├── scripts/
│   └── check_crash_decoupling.py        ← DB 隔离自检
└── .importlinter.cfg                    ← lint 合约
docs/
└── adr/
    └── 0001-crashguard-isolation.md     ← 架构决策记录
```

**修改：**
- `backend/requirements.txt` — 加 `import-linter>=2.0` 到 dev 依赖
- `backend/app/main.py` — lifespan 加 crashguard DB 自检 + import models
- `config.yaml` — 顶层加 `crashguard:` 段
- `.env.example` — 加 `CRASHGUARD_DATADOG_API_KEY` 等

---

## Phase A / 模块骨架 + 解耦防腐机制（Tasks 1-7）

### Task 1: 创建 crashguard 子包目录与 `__init__.py`

**Files:**
- Create: `backend/app/crashguard/__init__.py`
- Create: `backend/app/crashguard/services/__init__.py`
- Create: `backend/tests/crashguard/__init__.py`
- Create: `backend/tests/crashguard/fixtures/.gitkeep`

- [ ] **Step 1: 创建目录与空 `__init__.py`**

```bash
mkdir -p backend/app/crashguard/services
mkdir -p backend/tests/crashguard/fixtures
touch backend/tests/crashguard/fixtures/.gitkeep
```

写入 `backend/app/crashguard/__init__.py`：

```python
"""
Crashguard — 崩溃自动化分析与 PR 子模块

⚠️  这是独立模块，未来可能拆分为独立服务。
    模块隔离约束见 backend/app/crashguard/CLAUDE.md
"""

from app.crashguard.config import get_crashguard_settings  # noqa: F401

__all__ = ["get_crashguard_settings"]
```

写入 `backend/app/crashguard/services/__init__.py`：

```python
"""crashguard services 子模块"""
```

写入 `backend/tests/crashguard/__init__.py`：

```python
```

- [ ] **Step 2: 提交**

```bash
git add backend/app/crashguard backend/tests/crashguard
git commit -m "feat(crashguard): 初始化模块骨架"
```

---

### Task 2: 写 `crashguard/config.py` — 模块配置加载

**Files:**
- Create: `backend/app/crashguard/config.py`
- Create: `backend/tests/crashguard/test_config.py`

- [ ] **Step 1: 写失败的测试 `test_config.py`**

写入 `backend/tests/crashguard/test_config.py`：

```python
"""crashguard 配置加载测试"""
from __future__ import annotations

import os

import pytest


def test_settings_loads_defaults(monkeypatch):
    """无 env 时使用 yaml 默认值"""
    monkeypatch.delenv("CRASHGUARD_DATADOG_API_KEY", raising=False)
    monkeypatch.delenv("CRASHGUARD_ENABLED", raising=False)

    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    s = get_crashguard_settings()
    assert s.enabled is True
    assert s.pr_enabled is True
    assert s.feishu_enabled is True
    assert s.max_top_n == 20
    assert s.surge_multiplier == 1.5
    assert s.surge_min_events == 10
    assert s.regression_silent_versions == 3
    assert s.feasibility_pr_threshold == 0.7


def test_env_overrides_yaml(monkeypatch):
    """env 变量覆盖 yaml"""
    monkeypatch.setenv("CRASHGUARD_DATADOG_API_KEY", "test-key")
    monkeypatch.setenv("CRASHGUARD_ENABLED", "false")

    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    s = get_crashguard_settings()
    assert s.datadog_api_key == "test-key"
    assert s.enabled is False
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_config.py -v
```

Expected: `ImportError: No module named 'app.crashguard.config'` — fail.

- [ ] **Step 3: 写最小实现 `config.py`**

写入 `backend/app/crashguard/config.py`：

```python
"""
Crashguard 模块配置 — 独立配置段，与 jarvis 全局配置解耦。

加载顺序: env (CRASHGUARD_*) > config.yaml crashguard 段 > 默认值
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, List

from pydantic import Field
from pydantic_settings import BaseSettings

from app.config import PROJECT_ROOT, _load_yaml


class CrashguardSettings(BaseSettings):
    # Kill switches
    enabled: bool = True
    pr_enabled: bool = True
    feishu_enabled: bool = True

    # Datadog
    datadog_api_key: str = ""
    datadog_app_key: str = ""
    datadog_site: str = "datadoghq.com"
    datadog_window_hours: int = 24

    # Schedule
    morning_cron: str = "0 7 * * *"
    evening_cron: str = "0 17 * * *"

    # Top N + thresholds
    max_top_n: int = 20
    surge_multiplier: float = 1.5
    surge_min_events: int = 10
    regression_silent_versions: int = 3
    feasibility_pr_threshold: float = 0.7

    # Feishu
    feishu_target_chat_id: str = ""
    feishu_admin_open_ids: List[str] = Field(default_factory=list)

    model_config = {
        "env_prefix": "CRASHGUARD_",
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


def _yaml_overrides() -> Dict[str, Any]:
    """从 config.yaml crashguard 段读取覆盖项"""
    cfg = _load_yaml().get("crashguard") or {}
    flat: Dict[str, Any] = {}
    for k in (
        "enabled", "pr_enabled", "feishu_enabled",
        "max_top_n",
    ):
        if k in cfg:
            flat[k] = cfg[k]
    if "thresholds" in cfg:
        t = cfg["thresholds"] or {}
        for k_yaml, k_py in [
            ("surge_multiplier", "surge_multiplier"),
            ("surge_min_events", "surge_min_events"),
            ("regression_silent_versions", "regression_silent_versions"),
            ("feasibility_pr_threshold", "feasibility_pr_threshold"),
        ]:
            if k_yaml in t:
                flat[k_py] = t[k_yaml]
    if "datadog" in cfg:
        d = cfg["datadog"] or {}
        if "site" in d:
            flat["datadog_site"] = d["site"]
    if "feishu" in cfg:
        f = cfg["feishu"] or {}
        if "target_chat_id" in f:
            flat["feishu_target_chat_id"] = f["target_chat_id"]
        if "admin_open_ids" in f:
            flat["feishu_admin_open_ids"] = f["admin_open_ids"]
        if "morning_cron" in f:
            flat["morning_cron"] = f["morning_cron"]
        if "evening_cron" in f:
            flat["evening_cron"] = f["evening_cron"]
    return flat


@lru_cache
def get_crashguard_settings() -> CrashguardSettings:
    """获取 crashguard 配置（cached singleton）"""
    overrides = _yaml_overrides()
    return CrashguardSettings(**overrides)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_config.py -v
```

Expected: 2 passed.

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/config.py backend/tests/crashguard/test_config.py
git commit -m "feat(crashguard): 模块配置加载，env 优先于 yaml"
```

---

### Task 3: 写 `crashguard/CLAUDE.md` — 模块级 AI 指引

**Files:**
- Create: `backend/app/crashguard/CLAUDE.md`

- [ ] **Step 1: 写入 CLAUDE.md**

写入 `backend/app/crashguard/CLAUDE.md`：

```markdown
# Crashguard 模块隔离约束

⚠️ 这是独立模块，未来可能拆分为独立服务。修改前必读：

## 禁止项

1. ❌ 禁止 `from app.models import ...`（除 `app.db.database.get_session`）
2. ❌ 禁止 `from app.workers.analysis_worker import ...`
3. ❌ 禁止 `from app.services.rule_engine import ...`
4. ❌ 禁止 `from app.api.issues|tasks|feedback import ...`
5. ❌ 禁止 SQL join 到非 `crash_*` 表（如 `issues`、`tasks`、`feedbacks`）
6. ❌ 禁止把 crashguard 字段塞进 jarvis 全局配置（用顶层 `crashguard:` 段）

## 允许的耦合点（仅这 4 个）

1. ✅ `app.services.feishu_cli.send_message` — 群消息推送
2. ✅ `app.services.repo_updater.create_branch_pr` — git PR 能力（仅 draft）
3. ✅ `app.services.agent_orchestrator.run_agent` — agent 调度
4. ✅ `app.db.database.get_session` — 共用 connection pool

## 新增耦合点的流程

1. 先更新 `docs/adr/0001-crashguard-isolation.md`
2. 修改 `backend/.importlinter.cfg` 的 forbidden_modules 白名单
3. 在 PR 描述里说明引入的耦合点 + 必要性
4. 通过 lint：`cd backend && lint-imports`

## 关于 PR 创建（重要）

crashguard 创建 PR **必须**：
- 始终 `--draft`，永不取消 draft 状态
- 严禁调用 `gh pr merge`、`git merge`、`gh pr ready` 任何合入操作
- 所有 PR 由人工 review + approve + merge

如有任何疑问，参考 `docs/superpowers/specs/2026-04-27-crashguard-design.md`。
```

- [ ] **Step 2: 提交**

```bash
git add backend/app/crashguard/CLAUDE.md
git commit -m "docs(crashguard): 模块级 AI 隔离约束指引"
```

---

### Task 4: 写 ADR-0001 架构决策记录

**Files:**
- Create: `docs/adr/0001-crashguard-isolation.md`

- [ ] **Step 1: 写入 ADR**

```bash
mkdir -p docs/adr
```

写入 `docs/adr/0001-crashguard-isolation.md`：

```markdown
# ADR-0001 / Crashguard 模块隔离

**状态:** Accepted
**日期:** 2026-04-27
**决策者:** sanato

## 背景

Crashguard 是 jarvis 的子模块，用于自动化崩溃分析与 PR 提交。
未来可能拆分为独立服务，因此当前必须维持强解耦边界。

## 决策

1. 所有 crashguard 代码限制在 `backend/app/crashguard/` 子包内
2. 数据库表前缀 `crash_*`，无外键指向 jarvis 既有表
3. 仅允许 4 个对外耦合点（见模块 CLAUDE.md）
4. 通过 import-linter + DB 自检脚本强制约束
5. PR 必须 draft 创建，禁止任何合入操作

## 后果

**正面：**
- 未来可独立拆分为微服务，迁移成本可控
- 模块边界清晰，jarvis 主线 refactor 不影响 crashguard
- AI agent 修改时有明确指引（CLAUDE.md）

**负面：**
- 短期开发成本 +10%（无法直接复用 jarvis 业务代码）
- 跨模块查询需要应用层 lookup（如 crash → 工单关联）

## 实施要点

- `backend/.importlinter.cfg`：forbidden 合约
- `backend/scripts/check_crash_decoupling.py`：启动时跑外键自检
- `backend/app/crashguard/CLAUDE.md`：AI 修改指引
- PR 模板加 checkbox：确认未引入新耦合点

## 修订历史

- 2026-04-27 创建
```

- [ ] **Step 2: 提交**

```bash
git add docs/adr/0001-crashguard-isolation.md
git commit -m "docs(adr): 0001 crashguard 模块隔离决策"
```

---

### Task 5: 配置 import-linter

**Files:**
- Create: `backend/.importlinter.cfg`
- Modify: `backend/requirements.txt`

- [ ] **Step 1: 加 import-linter 到 dev 依赖**

修改 `backend/requirements.txt`，在 Testing 段（`pytest>=8.0.0` 附近）追加：

```
# Architecture lint
import-linter>=2.0
```

- [ ] **Step 2: 写 import-linter 配置**

写入 `backend/.importlinter.cfg`：

```ini
[importlinter]
root_packages =
    app

[importlinter:contract:crashguard-isolation]
name = crashguard 模块隔离合约
type = forbidden
source_modules =
    app.crashguard
forbidden_modules =
    app.models
    app.workers.analysis_worker
    app.services.rule_engine
    app.api.issues
    app.api.tasks
    app.api.feedback
ignore_imports =
    # crashguard.config 允许 import app.config 的 PROJECT_ROOT 与 _load_yaml
    app.crashguard.config -> app.config
```

- [ ] **Step 3: 安装并跑 lint**

```bash
cd backend
pip install import-linter
lint-imports
```

Expected: `Contracts: 1 kept, 0 broken.`

- [ ] **Step 4: 验证违反时会报错（手动反向测试）**

临时在 `backend/app/crashguard/__init__.py` 顶部加一行：

```python
from app.models.schemas import IssueRecord  # 故意违规
```

跑：
```bash
cd backend && lint-imports
```

Expected: `1 broken contract` — 包含 `app.crashguard -> app.models.schemas`。

撤销违规：
```bash
git checkout backend/app/crashguard/__init__.py
```

- [ ] **Step 5: 提交**

```bash
git add backend/.importlinter.cfg backend/requirements.txt
git commit -m "feat(crashguard): import-linter 模块隔离合约"
```

---

### Task 6: 写 DB 隔离自检脚本

**Files:**
- Create: `backend/scripts/check_crash_decoupling.py`
- Create: `backend/tests/crashguard/test_decoupling_script.py`

- [ ] **Step 1: 写测试**

写入 `backend/tests/crashguard/test_decoupling_script.py`：

```python
"""DB 解耦自检脚本测试"""
from __future__ import annotations


def test_check_no_foreign_keys_to_jarvis_tables():
    """crash_* 表不能有外键指向非 crash_* 表"""
    from backend.scripts.check_crash_decoupling import find_violating_foreign_keys

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
    from backend.scripts.check_crash_decoupling import find_violating_foreign_keys

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
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_decoupling_script.py -v
```

Expected: ImportError — fail.

- [ ] **Step 3: 写脚本实现**

```bash
mkdir -p backend/scripts
touch backend/scripts/__init__.py
```

写入 `backend/scripts/__init__.py`：

```python
```

写入 `backend/scripts/check_crash_decoupling.py`：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_decoupling_script.py -v
```

Expected: 2 passed.

- [ ] **Step 5: 提交**

```bash
git add backend/scripts/__init__.py backend/scripts/check_crash_decoupling.py \
        backend/tests/crashguard/test_decoupling_script.py
git commit -m "feat(crashguard): DB 解耦自检脚本"
```

---

### Task 7: 写 README + PR 模板更新

**Files:**
- Create: `backend/app/crashguard/README.md`
- Modify: `.github/PULL_REQUEST_TEMPLATE.md`（如不存在则创建）

- [ ] **Step 1: 写模块 README**

写入 `backend/app/crashguard/README.md`：

```markdown
# Crashguard 模块

崩溃自动化分析与 PR 提交（jarvis 子模块）。

## 概览

每天 07:00 + 17:00 自动从 Datadog 拉崩溃 → 三维分类（全新/回归/飙升）→ Top20 排序 → AI agent 分析 → Flutter 自动 draft PR / Android·iOS 半自动 → Feishu 群消息日报。

## 入口

- API: `/api/crash/*`（详见 `api/`）
- 调度: APScheduler in `workers/scheduler.py`
- 手动触发: `POST /api/crash/trigger`

## 隔离约束

⚠️ **必读** `CLAUDE.md` — 修改本模块前的隔离规则与允许的对外耦合点。

ADR: `docs/adr/0001-crashguard-isolation.md`

## 配置

- env: `CRASHGUARD_*`（如 `CRASHGUARD_DATADOG_API_KEY`）
- yaml: `config.yaml` 顶层 `crashguard:` 段

## 开发

```bash
# 单元测试
cd backend
pytest tests/crashguard/ -v

# 解耦 lint
lint-imports

# 启动时 DB 自检
python -m scripts.check_crash_decoupling
```

## 未来拆分预案

如未来要拆出独立服务，按以下顺序：

1. `backend/app/crashguard/` 整体迁移到独立 repo
2. 替换 4 个 jarvis 函数调用 → HTTP 调用对应 jarvis API
3. `crash_*` 表迁移到独立 SQLite
4. 部署: 独立 docker-compose service

详见 ADR-0001。
```

- [ ] **Step 2: 写/更新 PR 模板**

如果 `.github/PULL_REQUEST_TEMPLATE.md` 不存在则创建；如果存在则在末尾追加 Crashguard 段。

写入或追加（在末尾）`.github/PULL_REQUEST_TEMPLATE.md`：

```markdown
## Crashguard 隔离检查（仅当本 PR 改动 `backend/app/crashguard/`）

- [ ] 已确认未引入新的 jarvis 耦合点（参见 ADR-0001）
- [ ] `lint-imports` 通过
- [ ] crash_* 表无新增外键指向非 crash_* 表
```

- [ ] **Step 3: 提交**

```bash
git add backend/app/crashguard/README.md .github/PULL_REQUEST_TEMPLATE.md
git commit -m "docs(crashguard): 模块 README + PR 模板隔离 checklist"
```

---

## Phase B / DB Models（Tasks 8-10）

### Task 8: 写 7 张 crash_* 表的 SQLAlchemy 模型

**Files:**
- Create: `backend/app/crashguard/models.py`
- Create: `backend/tests/crashguard/test_models.py`

- [ ] **Step 1: 写测试**

写入 `backend/tests/crashguard/test_models.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_models.py -v
```

Expected: ImportError — fail.

- [ ] **Step 3: 写 models.py**

写入 `backend/app/crashguard/models.py`：

```python
"""
Crashguard SQLAlchemy 模型 — 7 张 crash_* 表。

⚠️ 严禁外键指向非 crash_* 表，违反 ADR-0001。
"""
from __future__ import annotations

from datetime import datetime, date

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)

from app.db.database import Base


class CrashIssue(Base):
    __tablename__ = "crash_issues"

    id = Column(Integer, primary_key=True, autoincrement=True)
    datadog_issue_id = Column(String(128), unique=True, nullable=False, index=True)
    stack_fingerprint = Column(String(64), index=True, default="")
    title = Column(String(512), default="")
    platform = Column(String(16), default="")  # flutter / ios / android
    service = Column(String(128), default="")
    first_seen_at = Column(DateTime, nullable=True)
    first_seen_version = Column(String(32), default="")
    last_seen_at = Column(DateTime, nullable=True)
    last_seen_version = Column(String(32), default="")
    status = Column(String(32), default="open")  # open / resolved_by_pr / ignored / wontfix
    total_events = Column(Integer, default=0)
    total_users_affected = Column(Integer, default=0)
    representative_stack = Column(Text, default="")
    tags = Column(Text, default="{}")           # JSON
    external_refs = Column(Text, default="[]")  # JSON
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CrashSnapshot(Base):
    __tablename__ = "crash_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    datadog_issue_id = Column(String(128), nullable=False, index=True)
    snapshot_date = Column(Date, nullable=False)
    app_version = Column(String(32), default="")
    events_count = Column(Integer, default=0)
    users_affected = Column(Integer, default=0)
    crash_free_rate = Column(Float, default=1.0)
    crash_free_impact_score = Column(Float, default=0.0)
    is_new_in_version = Column(Boolean, default=False)
    is_regression = Column(Boolean, default=False)
    is_surge = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "datadog_issue_id", "snapshot_date",
            name="uq_crash_snapshots_issue_date",
        ),
        Index(
            "ix_crash_snapshots_date_score",
            "snapshot_date", "crash_free_impact_score",
        ),
    )


class CrashFingerprint(Base):
    __tablename__ = "crash_fingerprints"

    fingerprint = Column(String(64), primary_key=True)
    datadog_issue_ids = Column(Text, default="[]")  # JSON 数组
    first_seen_version = Column(String(32), default="")
    total_events_across_versions = Column(Integer, default=0)
    normalized_top_frames = Column(Text, default="[]")  # JSON
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CrashAnalysis(Base):
    __tablename__ = "crash_analyses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    datadog_issue_id = Column(String(128), index=True, nullable=False)
    analysis_run_id = Column(String(64), unique=True, nullable=False)
    agent_name = Column(String(32), default="")
    triggered_by = Column(String(32), default="scheduled")
    problem_type = Column(String(64), default="")
    root_cause = Column(Text, default="")
    scenario = Column(Text, default="")
    key_evidence = Column(Text, default="[]")  # JSON
    reproducibility = Column(String(32), default="unreproducible")
    verification_method = Column(String(16), default="static")  # static / unit_test
    verification_result = Column(String(32), default="")
    feasibility_score = Column(Float, default=0.0)
    feasibility_reasoning = Column(Text, default="")
    fix_suggestion = Column(Text, default="")
    fix_diff = Column(Text, nullable=True)
    reproduction_test_path = Column(String(256), nullable=True)
    reproduction_test_code = Column(Text, nullable=True)
    verification_log = Column(Text, default="")
    complexity_level = Column(String(8), default="high")  # low / high
    confidence = Column(String(8), default="low")
    agent_raw_output = Column(Text, default="")
    status = Column(String(16), default="success")  # success / failed
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CrashPullRequest(Base):
    __tablename__ = "crash_pull_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    analysis_id = Column(Integer, index=True, nullable=False)  # → crash_analyses.id (应用层 lookup)
    datadog_issue_id = Column(String(128), index=True, nullable=False)
    repo = Column(String(64), default="")  # plaud_ai / plaud_ios / plaud_android
    branch_name = Column(String(256), default="")
    pr_url = Column(String(512), default="")
    pr_number = Column(Integer, nullable=True)
    pr_status = Column(String(16), default="draft")  # draft / open / merged / closed
    triggered_by = Column(String(16), default="auto_verified")  # auto_verified / human_approved
    approved_by = Column(String(64), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    verification_status = Column(String(32), default="pending")
    verified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CrashDailyReport(Base):
    __tablename__ = "crash_daily_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_date = Column(Date, nullable=False)
    report_type = Column(String(16), nullable=False)  # morning / evening
    top_n = Column(Integer, default=0)
    new_count = Column(Integer, default=0)
    regression_count = Column(Integer, default=0)
    surge_count = Column(Integer, default=0)
    feishu_message_id = Column(String(128), default="")
    report_payload = Column(Text, default="{}")  # JSON
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "report_date", "report_type",
            name="uq_crash_daily_reports_date_type",
        ),
    )


class CrashVersion(Base):
    __tablename__ = "crash_versions"

    version = Column(String(32), nullable=False)
    platform = Column(String(16), nullable=False)
    released_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=False)
    notes = Column(Text, default="")

    __table_args__ = (
        PrimaryKeyConstraint("version", "platform", name="pk_crash_versions"),
    )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_models.py -v
```

Expected: 4 passed.

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/models.py backend/tests/crashguard/test_models.py
git commit -m "feat(crashguard): 7 张 crash_* 表 SQLAlchemy 模型"
```

---

### Task 9: 集成 models 到 jarvis 启动流程

**Files:**
- Modify: `backend/app/main.py`

- [ ] **Step 1: 阅读 main.py 了解 lifespan 结构**

```bash
cd backend
grep -n "lifespan\|init_db\|sync_files_to_db" app/main.py | head -20
```

定位到 lifespan 函数。

- [ ] **Step 2: 修改 main.py — import crashguard models 并加 DB 自检**

在 `backend/app/main.py` 中：

a) 在 `await init_db()` 调用**之前**，加 import 让 SQLAlchemy 注册表：

找到这一行：
```python
    await init_db()
    logger.info("Database initialized.")
```

改为：
```python
    # Import crashguard models to register with SQLAlchemy Base
    from app.crashguard import models as _crashguard_models  # noqa: F401

    await init_db()
    logger.info("Database initialized.")

    # Crashguard DB 解耦自检 — 违规则阻止启动
    try:
        from scripts.check_crash_decoupling import assert_crash_tables_decoupled
        assert_crash_tables_decoupled()
        logger.info("Crashguard decoupling check passed.")
    except RuntimeError as e:
        logger.error("Crashguard decoupling check FAILED: %s", e)
        raise
```

- [ ] **Step 3: 启动 jarvis 验证表创建成功**

```bash
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!
sleep 5
curl -s http://localhost:8000/api/health
kill $SERVER_PID 2>/dev/null
```

Expected: 启动日志包含 `Crashguard decoupling check passed.`，无 RuntimeError。

- [ ] **Step 4: 验证 SQLite 中 crash_* 表已创建**

```bash
sqlite3 data/appllo.db ".tables crash%"
```

Expected: 输出 7 张表 — `crash_analyses crash_daily_reports crash_fingerprints crash_issues crash_pull_requests crash_snapshots crash_versions`

- [ ] **Step 5: 提交**

```bash
git add backend/app/main.py
git commit -m "feat(crashguard): main.py 集成 — 注册 models + DB 自检"
```

---

### Task 10: config.yaml 加 crashguard 段 + .env.example

**Files:**
- Modify: `config.yaml`
- Modify: `.env.example`（如不存在则创建）

- [ ] **Step 1: 在 config.yaml 末尾追加 crashguard 段**

```yaml

# ===========================
# Crashguard 模块配置
# ===========================
crashguard:
  enabled: true                # kill switch
  pr_enabled: true             # PR 创建总开关
  feishu_enabled: true         # 群消息开关
  max_top_n: 20

  datadog:
    site: "datadoghq.com"      # 或 "datadoghq.eu"
    # api_key / app_key 请通过 .env 配置:
    #   CRASHGUARD_DATADOG_API_KEY=xxx
    #   CRASHGUARD_DATADOG_APP_KEY=xxx

  feishu:
    target_chat_id: ""         # 群聊 chat_id（必填）
    admin_open_ids: []         # 一键 PR approve 白名单
    morning_cron: "0 7 * * *"
    evening_cron: "0 17 * * *"

  thresholds:
    surge_multiplier: 1.5              # 飙升判定阈值（倍数）
    surge_min_events: 10               # 飙升最小事件数
    regression_silent_versions: 3      # 回归判定的静默版本数
    feasibility_pr_threshold: 0.7      # 自动创建 PR 的 feasibility 触发线
```

- [ ] **Step 2: 更新 .env.example**

如 `.env.example` 不存在，先创建。在末尾加：

```
# Crashguard
CRASHGUARD_DATADOG_API_KEY=
CRASHGUARD_DATADOG_APP_KEY=

# 三平台仓库路径（已存在的复用，新增的填上）
# CODE_REPO_APP=/path/to/plaud_ai
# CODE_REPO_IOS=/path/to/plaud_ios
# CODE_REPO_ANDROID=/path/to/plaud_android
```

- [ ] **Step 3: 验证 yaml 加载正常**

```bash
cd backend
python -c "from app.crashguard.config import get_crashguard_settings; s = get_crashguard_settings(); print(s.max_top_n, s.surge_multiplier, s.feasibility_pr_threshold)"
```

Expected: `20 1.5 0.7`

- [ ] **Step 4: 提交**

```bash
git add config.yaml .env.example
git commit -m "feat(crashguard): config.yaml crashguard 段 + .env.example"
```

---

## Phase C / Datadog Client（Tasks 11-15）

### Task 11: TDD — DatadogClient.list_issues 基本调用

**Files:**
- Create: `backend/app/crashguard/services/datadog_client.py`
- Create: `backend/tests/crashguard/test_datadog_client.py`
- Create: `backend/tests/crashguard/fixtures/datadog_issues_page1.json`
- Create: `backend/tests/crashguard/fixtures/datadog_issues_page2.json`

- [ ] **Step 1: 准备 fixture**

写入 `backend/tests/crashguard/fixtures/datadog_issues_page1.json`：

```json
{
  "data": [
    {
      "id": "abc123",
      "type": "error_tracking_issue",
      "attributes": {
        "title": "NullPointerException @ AudioPlayer.play",
        "service": "plaud_ai",
        "platform": "flutter",
        "first_seen_timestamp": 1714003200000,
        "last_seen_timestamp": 1714176000000,
        "first_seen_version": "1.4.7",
        "last_seen_version": "1.4.7",
        "events_count": 145,
        "users_affected": 23,
        "stack_trace": "NullPointerException\n  at AudioPlayer.play (lib/audio/player.dart:42)\n  at PlaybackController.start (lib/audio/playback.dart:18)",
        "tags": {"env": "prod", "os.version": "14"}
      }
    }
  ],
  "meta": {
    "page": {"after": "next-cursor"}
  }
}
```

写入 `backend/tests/crashguard/fixtures/datadog_issues_page2.json`：

```json
{
  "data": [
    {
      "id": "def456",
      "type": "error_tracking_issue",
      "attributes": {
        "title": "EXC_BAD_ACCESS @ AudioEngine.swift:78",
        "service": "plaud_ios",
        "platform": "ios",
        "first_seen_timestamp": 1714003200000,
        "last_seen_timestamp": 1714176000000,
        "first_seen_version": "1.4.7",
        "last_seen_version": "1.4.7",
        "events_count": 67,
        "users_affected": 12,
        "stack_trace": "EXC_BAD_ACCESS\n  at AudioEngine.start (AudioEngine.swift:78)",
        "tags": {"env": "prod"}
      }
    }
  ],
  "meta": {
    "page": {}
  }
}
```

- [ ] **Step 2: 写测试**

写入 `backend/tests/crashguard/test_datadog_client.py`：

```python
"""DatadogClient 测试"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest


FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.asyncio
async def test_list_issues_single_page(monkeypatch):
    """单页响应：返回所有 issue"""
    from app.crashguard.services.datadog_client import DatadogClient

    page = _load_fixture("datadog_issues_page2.json")  # meta.page 为空 = 末页

    async def fake_get(self, url, **kw):
        return httpx.Response(200, json=page)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    issues = await client.list_issues(window_hours=24)
    assert len(issues) == 1
    assert issues[0]["id"] == "def456"
    assert issues[0]["attributes"]["platform"] == "ios"


@pytest.mark.asyncio
async def test_list_issues_paginates(monkeypatch):
    """多页响应：跨页拼接"""
    from app.crashguard.services.datadog_client import DatadogClient

    pages = [
        _load_fixture("datadog_issues_page1.json"),
        _load_fixture("datadog_issues_page2.json"),
    ]
    call_count = {"n": 0}

    async def fake_get(self, url, **kw):
        idx = call_count["n"]
        call_count["n"] += 1
        return httpx.Response(200, json=pages[idx])

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    issues = await client.list_issues(window_hours=24)
    assert len(issues) == 2
    assert issues[0]["id"] == "abc123"
    assert issues[1]["id"] == "def456"


@pytest.mark.asyncio
async def test_list_issues_retries_on_5xx(monkeypatch):
    """5xx 错误重试 3 次后成功"""
    from app.crashguard.services.datadog_client import DatadogClient

    page = _load_fixture("datadog_issues_page2.json")
    call_count = {"n": 0}

    async def fake_get(self, url, **kw):
        call_count["n"] += 1
        if call_count["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=page)

    async def fake_sleep(s):
        return  # 跳过真实等待

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    import asyncio
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    issues = await client.list_issues(window_hours=24)
    assert len(issues) == 1
    assert call_count["n"] == 3
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_datadog_client.py -v
```

Expected: ImportError — fail.

- [ ] **Step 4: 写实现**

写入 `backend/app/crashguard/services/datadog_client.py`：

```python
"""
Datadog Error Tracking API client.

API 文档: https://docs.datadoghq.com/api/latest/error-tracking/
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("crashguard.datadog")

_RETRY_STATUS = {500, 502, 503, 504}
_RATE_LIMIT_STATUS = {429}


class DatadogRateLimitError(Exception):
    """Datadog 触发限流"""


class DatadogClient:
    """异步 Datadog Error Tracking client"""

    def __init__(
        self,
        api_key: str,
        app_key: str,
        site: str = "datadoghq.com",
        timeout: float = 30.0,
    ):
        self.api_key = api_key
        self.app_key = app_key
        self.site = site
        self.timeout = timeout
        self.base_url = f"https://api.{site}/api/v2/error-tracking"

    def _headers(self) -> Dict[str, str]:
        return {
            "DD-API-KEY": self.api_key,
            "DD-APPLICATION-KEY": self.app_key,
            "Content-Type": "application/json",
        }

    async def list_issues(
        self,
        window_hours: int = 24,
        page_size: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        分页拉取所有 error tracking issue。

        失败重试 3 次指数退避（1s/2s/4s），429 抛 DatadogRateLimitError。
        """
        params: Dict[str, Any] = {
            "filter[from]": f"now-{window_hours}h",
            "filter[to]": "now",
            "page[size]": page_size,
        }
        all_issues: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            while True:
                if cursor:
                    params["page[after]"] = cursor

                payload = await self._get_with_retry(
                    client,
                    f"{self.base_url}/issues",
                    params=params,
                )
                all_issues.extend(payload.get("data", []))

                meta = payload.get("meta", {}).get("page", {})
                cursor = meta.get("after")
                if not cursor:
                    break

        return all_issues

    async def _get_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: Dict[str, Any],
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """指数退避重试，429 不重试直接抛"""
        last_error: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                resp = await client.get(url, headers=self._headers(), params=params)
                if resp.status_code in _RATE_LIMIT_STATUS:
                    raise DatadogRateLimitError(
                        f"Datadog 限流 (429), retry-after={resp.headers.get('retry-after')}"
                    )
                if resp.status_code in _RETRY_STATUS:
                    last_error = httpx.HTTPStatusError(
                        f"{resp.status_code}", request=resp.request, response=resp,
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue
                resp.raise_for_status()
                return resp.json()
            except DatadogRateLimitError:
                raise
            except httpx.HTTPError as e:
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise

        raise last_error if last_error else RuntimeError("未知错误")
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_datadog_client.py -v
```

Expected: 3 passed.

- [ ] **Step 6: 提交**

```bash
git add backend/app/crashguard/services/datadog_client.py \
        backend/tests/crashguard/test_datadog_client.py \
        backend/tests/crashguard/fixtures/datadog_issues_page1.json \
        backend/tests/crashguard/fixtures/datadog_issues_page2.json
git commit -m "feat(crashguard): Datadog Error Tracking client + 重试 + 分页"
```

---

### Task 12: 429 限流熔断（10 分钟内 5 次 → 暂停 30 分钟）

**Files:**
- Modify: `backend/app/crashguard/services/datadog_client.py`
- Modify: `backend/tests/crashguard/test_datadog_client.py`

- [ ] **Step 1: 加测试**

在 `test_datadog_client.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_rate_limit_circuit_breaker(monkeypatch):
    """10 分钟内 5 次 429 → 熔断 30 分钟"""
    from app.crashguard.services.datadog_client import (
        DatadogClient,
        DatadogRateLimitError,
        CircuitBreakerOpen,
    )

    async def fake_get(self, url, **kw):
        return httpx.Response(429, headers={"retry-after": "5"})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")

    # 前 5 次都应抛 DatadogRateLimitError
    for _ in range(5):
        with pytest.raises(DatadogRateLimitError):
            await client.list_issues(window_hours=24)

    # 第 6 次应抛熔断
    with pytest.raises(CircuitBreakerOpen):
        await client.list_issues(window_hours=24)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_datadog_client.py::test_rate_limit_circuit_breaker -v
```

Expected: ImportError on CircuitBreakerOpen — fail.

- [ ] **Step 3: 实现熔断器**

修改 `backend/app/crashguard/services/datadog_client.py`，在文件顶部追加：

```python
import time
from collections import deque


class CircuitBreakerOpen(Exception):
    """限流熔断器开启中"""
```

在 `DatadogClient.__init__` 内追加成员：

```python
        self._rate_limit_events: deque = deque(maxlen=10)
        self._circuit_open_until: float = 0.0
        self._circuit_threshold: int = 5
        self._circuit_window_sec: int = 600     # 10 分钟
        self._circuit_open_sec: int = 1800      # 30 分钟
```

在 `list_issues` 方法**最开始**加熔断检查：

```python
        now = time.time()
        if now < self._circuit_open_until:
            raise CircuitBreakerOpen(
                f"Datadog 熔断中，将于 {int(self._circuit_open_until - now)}s 后恢复"
            )
```

在 `_get_with_retry` 的 `DatadogRateLimitError` raise **之前**加事件计数：

```python
                if resp.status_code in _RATE_LIMIT_STATUS:
                    self._record_rate_limit_event()
                    raise DatadogRateLimitError(
                        f"Datadog 限流 (429), retry-after={resp.headers.get('retry-after')}"
                    )
```

在 `DatadogClient` 类内追加：

```python
    def _record_rate_limit_event(self) -> None:
        now = time.time()
        # 清理窗口外事件
        while self._rate_limit_events and self._rate_limit_events[0] < now - self._circuit_window_sec:
            self._rate_limit_events.popleft()
        self._rate_limit_events.append(now)
        # 触发熔断
        if len(self._rate_limit_events) >= self._circuit_threshold:
            self._circuit_open_until = now + self._circuit_open_sec
            logger.error(
                "Datadog 熔断开启 — %d 次 429 in %ds，暂停 %ds",
                self._circuit_threshold, self._circuit_window_sec, self._circuit_open_sec,
            )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_datadog_client.py -v
```

Expected: 4 passed.

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/services/datadog_client.py backend/tests/crashguard/test_datadog_client.py
git commit -m "feat(crashguard): Datadog 429 限流熔断器（10 分钟 5 次→暂停 30 分钟）"
```

---

### Task 13: 加 issue payload normalization helper

**Files:**
- Modify: `backend/app/crashguard/services/datadog_client.py`
- Modify: `backend/tests/crashguard/test_datadog_client.py`

- [ ] **Step 1: 加测试**

在 `test_datadog_client.py` 末尾追加：

```python
def test_normalize_issue_payload():
    """Datadog 原始响应 → 内部统一结构"""
    from app.crashguard.services.datadog_client import normalize_issue
    raw = {
        "id": "abc123",
        "type": "error_tracking_issue",
        "attributes": {
            "title": "NullPointerException @ play",
            "service": "plaud_ai",
            "platform": "flutter",
            "first_seen_timestamp": 1714003200000,
            "last_seen_timestamp": 1714176000000,
            "first_seen_version": "1.4.7",
            "last_seen_version": "1.4.7",
            "events_count": 145,
            "users_affected": 23,
            "stack_trace": "NullPointerException\n  at A.x\n  at B.y",
            "tags": {"env": "prod"},
        },
    }
    norm = normalize_issue(raw)
    assert norm["datadog_issue_id"] == "abc123"
    assert norm["title"] == "NullPointerException @ play"
    assert norm["platform"] == "flutter"
    assert norm["service"] == "plaud_ai"
    assert norm["events_count"] == 145
    assert norm["users_affected"] == 23
    assert norm["first_seen_version"] == "1.4.7"
    assert norm["stack_trace"].startswith("NullPointerException")
    assert norm["tags"] == {"env": "prod"}
    assert norm["first_seen_at"].year == 2024  # 2024-04-25 unix ms


def test_normalize_handles_missing_fields():
    """缺失字段不报错，给默认值"""
    from app.crashguard.services.datadog_client import normalize_issue
    raw = {"id": "xxx", "attributes": {}}
    norm = normalize_issue(raw)
    assert norm["datadog_issue_id"] == "xxx"
    assert norm["title"] == ""
    assert norm["platform"] == ""
    assert norm["events_count"] == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_datadog_client.py::test_normalize_issue_payload -v
```

Expected: ImportError — fail.

- [ ] **Step 3: 实现 normalize_issue**

在 `backend/app/crashguard/services/datadog_client.py` 末尾追加：

```python
from datetime import datetime, timezone


def normalize_issue(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Datadog raw issue → 统一字段名结构（喂给上游 dedup/classifier 等）。

    所有字段缺失时给安全默认值，避免 KeyError 中断流水线。
    """
    attrs = raw.get("attributes") or {}

    def _ts_to_dt(ms: Any) -> datetime:
        if not ms:
            return None
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)

    return {
        "datadog_issue_id": raw.get("id", ""),
        "title": attrs.get("title", ""),
        "service": attrs.get("service", ""),
        "platform": attrs.get("platform", ""),
        "first_seen_at": _ts_to_dt(attrs.get("first_seen_timestamp")),
        "last_seen_at": _ts_to_dt(attrs.get("last_seen_timestamp")),
        "first_seen_version": attrs.get("first_seen_version", ""),
        "last_seen_version": attrs.get("last_seen_version", ""),
        "events_count": int(attrs.get("events_count") or 0),
        "users_affected": int(attrs.get("users_affected") or 0),
        "stack_trace": attrs.get("stack_trace", ""),
        "tags": attrs.get("tags") or {},
    }
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_datadog_client.py -v
```

Expected: 6 passed.

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/services/datadog_client.py backend/tests/crashguard/test_datadog_client.py
git commit -m "feat(crashguard): Datadog issue payload 归一化"
```

---

## Phase D / Stack Fingerprint 去重（Tasks 14-16）

### Task 14: TDD — 堆栈帧归一化

**Files:**
- Create: `backend/app/crashguard/services/dedup.py`
- Create: `backend/tests/crashguard/test_dedup.py`
- Create: `backend/tests/crashguard/fixtures/stack_traces.json`

- [ ] **Step 1: 准备 fixture**

写入 `backend/tests/crashguard/fixtures/stack_traces.json`：

```json
{
  "flutter_v1": "NullPointerException: buffer is null\n  at AudioPlayer.play (lib/audio/player.dart:42)\n  at PlaybackController._start (lib/audio/playback.dart:18)\n  at <anonymous> (package:flutter/src/widgets/framework.dart:4567)\n  at _$xxxxx_closure (lib/main.dart)\n  at dart:async/zone.dart:1234\n",
  "flutter_v2_same_bug": "NullPointerException: buffer is null\n  at AudioPlayer.play (lib/audio/player.dart:55)\n  at PlaybackController._start (lib/audio/playback.dart:23)\n  at <anonymous> (package:flutter/src/widgets/framework.dart:4789)\n  at _$yyyyy_closure (lib/main.dart)\n  at dart:async/zone.dart:1234\n",
  "ios_native": "EXC_BAD_ACCESS\n  at AudioEngine.start (AudioEngine.swift:78)\n  at PlaybackVC.viewDidLoad (PlaybackVC.swift:34)\n  at libsystem_pthread.dylib`__pthread_start\n",
  "different_bug": "OutOfMemoryError\n  at ImageDecoder.decode (lib/image/decoder.dart:99)\n  at GalleryView.build (lib/ui/gallery.dart:45)\n"
}
```

- [ ] **Step 2: 写测试**

写入 `backend/tests/crashguard/test_dedup.py`：

```python
"""stack_fingerprint 算法测试"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def stacks() -> dict:
    return json.loads((FIXTURES / "stack_traces.json").read_text())


def test_normalize_strips_line_numbers(stacks):
    """归一化剥离行号"""
    from app.crashguard.services.dedup import normalize_stack_frames

    frames = normalize_stack_frames(stacks["flutter_v1"], top_n=5)
    # 不应包含 :42, :18 这类行号
    for f in frames:
        assert ":" not in f or f.endswith(".dart")  # 行号被剥离
        assert not any(c.isdigit() and i > 0 and f[i - 1] == ":" for i, c in enumerate(f))


def test_normalize_strips_anonymous_closures(stacks):
    """归一化剥离 <anonymous> / _$xxxxx_closure"""
    from app.crashguard.services.dedup import normalize_stack_frames

    frames = normalize_stack_frames(stacks["flutter_v1"], top_n=5)
    for f in frames:
        assert "_$xxxxx" not in f
        assert "<anonymous>" not in f
        assert "closure" not in f.lower() or "_$" not in f


def test_same_bug_same_fingerprint_across_versions(stacks):
    """同一 bug 不同版本（行号变了）→ 同一 fingerprint"""
    from app.crashguard.services.dedup import compute_fingerprint

    fp1 = compute_fingerprint(stacks["flutter_v1"])
    fp2 = compute_fingerprint(stacks["flutter_v2_same_bug"])
    assert fp1 == fp2


def test_different_bugs_different_fingerprint(stacks):
    """不同 bug → 不同 fingerprint"""
    from app.crashguard.services.dedup import compute_fingerprint

    fp1 = compute_fingerprint(stacks["flutter_v1"])
    fp_other = compute_fingerprint(stacks["different_bug"])
    assert fp1 != fp_other


def test_empty_stack_returns_stable_fingerprint():
    """空字符串/异常输入不应崩溃"""
    from app.crashguard.services.dedup import compute_fingerprint

    fp = compute_fingerprint("")
    assert isinstance(fp, str)
    assert len(fp) == 40  # SHA1


def test_ios_stack_strips_libsystem(stacks):
    """iOS 栈归一化剥离 libsystem 噪音"""
    from app.crashguard.services.dedup import normalize_stack_frames

    frames = normalize_stack_frames(stacks["ios_native"], top_n=5)
    assert all("libsystem" not in f.lower() for f in frames)
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_dedup.py -v
```

Expected: ImportError — fail.

- [ ] **Step 4: 实现 dedup.py**

写入 `backend/app/crashguard/services/dedup.py`：

```python
"""
Stack fingerprint 算法 — 跨版本同 bug 去重抓手。

归一化规则:
1. 取 stack trace 前 5 帧
2. 剥离行号: foo.dart:123 → foo.dart
3. 剥离匿名闭包/生成代码: <anonymous>, _$xxxx, closure_at_
4. 剥离版本号路径: pub-cache/.../package-1.2.3/ → package-*
5. 剥离 SDK/framework 噪音帧 (dart:async, Flutter framework, libsystem)
6. 剩余规范化文本拼接 → SHA1
"""
from __future__ import annotations

import hashlib
import re
from typing import List

# 噪音帧黑名单（substring 匹配，case-insensitive）
_NOISE_PATTERNS = [
    "dart:async",
    "dart:core",
    "dart:io",
    "package:flutter/src/",
    "libsystem",
    "libdyld",
    "libobjc",
    "java.lang.Thread",
    "java.util.concurrent",
    "kotlin.coroutines",
    "<anonymous>",
]

_LINE_NUM_RE = re.compile(r":\d+(?=[\s\)]|$)")
_CLOSURE_RE = re.compile(r"_\$[a-zA-Z0-9]+(_closure)?")
_VERSIONED_PATH_RE = re.compile(r"(pub-cache|node_modules|\.gradle/caches)/[^/]+-\d+\.\d+\.\d+", re.IGNORECASE)


def normalize_stack_frames(stack_trace: str, top_n: int = 5) -> List[str]:
    """
    把堆栈拆成帧列表，归一化噪音，返回前 top_n 个有效帧。
    """
    if not stack_trace:
        return []

    # 1. 拆行
    lines = [ln.strip() for ln in stack_trace.splitlines() if ln.strip()]

    # 2. 跳过非帧行（如错误标题）— 启发式: 包含 "at " 或 "  at "
    frames = [ln for ln in lines if ln.startswith("at ") or " at " in ln or ln.startswith("- ")]
    if not frames:
        # 兜底: 取所有非空行（异常情况）
        frames = lines[1:] if len(lines) > 1 else lines

    # 3. 归一化每帧
    normalized: List[str] = []
    for frame in frames:
        # 跳过噪音帧
        if any(p.lower() in frame.lower() for p in _NOISE_PATTERNS):
            continue

        # 剥离行号
        f = _LINE_NUM_RE.sub("", frame)
        # 剥离匿名闭包
        f = _CLOSURE_RE.sub("", f)
        # 版本号路径替换
        f = _VERSIONED_PATH_RE.sub(r"\1/*", f)
        # 折叠多余空白
        f = " ".join(f.split())

        normalized.append(f)

        if len(normalized) >= top_n:
            break

    return normalized


def compute_fingerprint(stack_trace: str, top_n: int = 5) -> str:
    """
    计算 stack_fingerprint (SHA1)。

    空栈/异常输入仍返回稳定哈希（避免上游中断）。
    """
    frames = normalize_stack_frames(stack_trace or "", top_n=top_n)
    payload = "\n".join(frames) if frames else "empty"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_dedup.py -v
```

Expected: 6 passed.

- [ ] **Step 6: 提交**

```bash
git add backend/app/crashguard/services/dedup.py \
        backend/tests/crashguard/test_dedup.py \
        backend/tests/crashguard/fixtures/stack_traces.json
git commit -m "feat(crashguard): stack_fingerprint 跨版本去重算法"
```

---

### Task 15: 跨版本 issue 关联（fingerprint → multiple datadog_issue_ids）

**Files:**
- Modify: `backend/app/crashguard/services/dedup.py`
- Modify: `backend/tests/crashguard/test_dedup.py`

- [ ] **Step 1: 加测试**

在 `test_dedup.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_link_issue_to_fingerprint_creates_new_record(tmp_path, monkeypatch):
    """fingerprint 不存在 → 新建 record"""
    from app.crashguard.services.dedup import upsert_fingerprint_link
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa
    import os

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    # 重新初始化 settings 缓存
    from app.config import get_settings
    get_settings.cache_clear()

    await init_db()
    async with get_session() as s:
        await upsert_fingerprint_link(
            s, fingerprint="abc", datadog_issue_id="issue1",
            first_seen_version="1.4.7", events_count=100,
            normalized_top_frames=["frame1", "frame2"],
        )
        await s.commit()

    async with get_session() as s:
        from sqlalchemy import select
        from app.crashguard.models import CrashFingerprint
        row = (await s.execute(
            select(CrashFingerprint).where(CrashFingerprint.fingerprint == "abc")
        )).scalar_one()
        import json as _json
        assert _json.loads(row.datadog_issue_ids) == ["issue1"]
        assert row.total_events_across_versions == 100


@pytest.mark.asyncio
async def test_link_issue_appends_existing_fingerprint(tmp_path, monkeypatch):
    """同 fingerprint 第二个 issue → 数组追加，count 累加"""
    from app.crashguard.services.dedup import upsert_fingerprint_link
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test2.db'}")
    from app.config import get_settings
    get_settings.cache_clear()

    await init_db()

    async with get_session() as s:
        await upsert_fingerprint_link(
            s, fingerprint="xyz", datadog_issue_id="issue_a",
            first_seen_version="1.4.6", events_count=50,
            normalized_top_frames=["fA"],
        )
        await s.commit()
    async with get_session() as s:
        await upsert_fingerprint_link(
            s, fingerprint="xyz", datadog_issue_id="issue_b",
            first_seen_version="1.4.7", events_count=80,
            normalized_top_frames=["fA"],
        )
        await s.commit()

    async with get_session() as s:
        from sqlalchemy import select
        from app.crashguard.models import CrashFingerprint
        row = (await s.execute(
            select(CrashFingerprint).where(CrashFingerprint.fingerprint == "xyz")
        )).scalar_one()
        import json as _json
        ids = _json.loads(row.datadog_issue_ids)
        assert set(ids) == {"issue_a", "issue_b"}
        assert row.total_events_across_versions == 130
        assert row.first_seen_version == "1.4.6"  # 早的版本
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_dedup.py::test_link_issue_to_fingerprint_creates_new_record -v
```

Expected: ImportError on upsert_fingerprint_link — fail.

- [ ] **Step 3: 实现 upsert_fingerprint_link**

在 `backend/app/crashguard/services/dedup.py` 末尾追加：

```python
import json
from typing import List as _List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_fingerprint_link(
    session: AsyncSession,
    fingerprint: str,
    datadog_issue_id: str,
    first_seen_version: str,
    events_count: int,
    normalized_top_frames: _List[str],
) -> None:
    """
    把 (fingerprint, datadog_issue_id) 关系写入 crash_fingerprints 表。

    - 不存在 → 新建
    - 已存在 → 把 datadog_issue_id 追加到 list；累加 events count；
              first_seen_version 取早版本（字符串字典序兜底）
    """
    from app.crashguard.models import CrashFingerprint

    row = (await session.execute(
        select(CrashFingerprint).where(CrashFingerprint.fingerprint == fingerprint)
    )).scalar_one_or_none()

    if row is None:
        row = CrashFingerprint(
            fingerprint=fingerprint,
            datadog_issue_ids=json.dumps([datadog_issue_id]),
            first_seen_version=first_seen_version,
            total_events_across_versions=events_count,
            normalized_top_frames=json.dumps(normalized_top_frames),
        )
        session.add(row)
        return

    ids = json.loads(row.datadog_issue_ids or "[]")
    if datadog_issue_id not in ids:
        ids.append(datadog_issue_id)
        row.datadog_issue_ids = json.dumps(ids)

    row.total_events_across_versions = (row.total_events_across_versions or 0) + events_count

    # 取更早的版本（简单字典序，足够大多数 semver 场景）
    if first_seen_version and (
        not row.first_seen_version or first_seen_version < row.first_seen_version
    ):
        row.first_seen_version = first_seen_version
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_dedup.py -v
```

Expected: 8 passed.

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/services/dedup.py backend/tests/crashguard/test_dedup.py
git commit -m "feat(crashguard): 跨版本 issue 关联（fingerprint upsert）"
```

---

## Phase E / Classifier 三维分类（Tasks 16-19）

### Task 16: TDD — is_new_in_version

**Files:**
- Create: `backend/app/crashguard/services/classifier.py`
- Create: `backend/tests/crashguard/test_classifier.py`

- [ ] **Step 1: 写测试**

写入 `backend/tests/crashguard/test_classifier.py`：

```python
"""三维分类器测试"""
from __future__ import annotations

import pytest


def test_is_new_in_version_true_when_first_seen_matches_latest():
    """issue 的 first_seen_version 等于当前最新发布版 → is_new_in_version=True"""
    from app.crashguard.services.classifier import is_new_in_version

    assert is_new_in_version(
        first_seen_version="1.4.7",
        latest_release="1.4.7",
    ) is True


def test_is_new_in_version_false_for_old_issue():
    """老 issue（first_seen_version 早于最新版）→ False"""
    from app.crashguard.services.classifier import is_new_in_version

    assert is_new_in_version(
        first_seen_version="1.4.5",
        latest_release="1.4.7",
    ) is False


def test_is_new_in_version_handles_missing():
    """缺数据时返回 False（保守）"""
    from app.crashguard.services.classifier import is_new_in_version

    assert is_new_in_version(first_seen_version="", latest_release="1.4.7") is False
    assert is_new_in_version(first_seen_version="1.4.7", latest_release="") is False
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_classifier.py -v
```

Expected: ImportError — fail.

- [ ] **Step 3: 实现 is_new_in_version**

写入 `backend/app/crashguard/services/classifier.py`：

```python
"""
三维新增分类器：
- is_new_in_version  : 该 issue 的首发版本就是当前最新发布版（"全新"）
- is_regression      : fingerprint 在最近 N 个版本静默后又出现（"回归"）
- is_surge           : 当日事件数环比飙升（"飙升"）
"""
from __future__ import annotations

from typing import List


def is_new_in_version(first_seen_version: str, latest_release: str) -> bool:
    """全新崩溃: 首次出现的版本 == 当前线上最新版"""
    if not first_seen_version or not latest_release:
        return False
    return first_seen_version.strip() == latest_release.strip()
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_classifier.py -v
```

Expected: 3 passed.

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/services/classifier.py backend/tests/crashguard/test_classifier.py
git commit -m "feat(crashguard): is_new_in_version 分类（全新崩溃）"
```

---

### Task 17: TDD — is_regression（fingerprint 静默 N 版本后再现）

**Files:**
- Modify: `backend/app/crashguard/services/classifier.py`
- Modify: `backend/tests/crashguard/test_classifier.py`

- [ ] **Step 1: 加测试**

在 `test_classifier.py` 末尾追加：

```python
def test_is_regression_when_silent_then_returns():
    """fingerprint 在 v1.4.4 出现，1.4.5/1.4.6/1.4.7 都静默，今日 v1.4.8 又出现 → True"""
    from app.crashguard.services.classifier import is_regression

    assert is_regression(
        fingerprint_seen_versions=["1.4.4"],
        recent_versions=["1.4.5", "1.4.6", "1.4.7"],
        current_version="1.4.8",
        silent_threshold=3,
    ) is True


def test_is_regression_false_when_continuously_present():
    """连续出现，从未静默 → False"""
    from app.crashguard.services.classifier import is_regression

    assert is_regression(
        fingerprint_seen_versions=["1.4.4", "1.4.5", "1.4.6", "1.4.7"],
        recent_versions=["1.4.5", "1.4.6", "1.4.7"],
        current_version="1.4.8",
        silent_threshold=3,
    ) is False


def test_is_regression_false_for_brand_new_fingerprint():
    """全新 fingerprint（之前从未出现）→ 不算 regression（应归为 is_new_in_version）"""
    from app.crashguard.services.classifier import is_regression

    assert is_regression(
        fingerprint_seen_versions=[],
        recent_versions=["1.4.5", "1.4.6", "1.4.7"],
        current_version="1.4.8",
        silent_threshold=3,
    ) is False


def test_is_regression_false_when_silence_too_short():
    """只静默 1 个版本（少于 threshold=3）→ False"""
    from app.crashguard.services.classifier import is_regression

    assert is_regression(
        fingerprint_seen_versions=["1.4.6"],
        recent_versions=["1.4.7"],
        current_version="1.4.8",
        silent_threshold=3,
    ) is False
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_classifier.py -v
```

Expected: 4 fail (ImportError on is_regression).

- [ ] **Step 3: 实现 is_regression**

在 `classifier.py` 末尾追加：

```python
from typing import List


def is_regression(
    fingerprint_seen_versions: List[str],
    recent_versions: List[str],
    current_version: str,
    silent_threshold: int = 3,
) -> bool:
    """
    回归崩溃判定:
    - fingerprint 历史上出现过（fingerprint_seen_versions 非空）
    - 但在最近 silent_threshold 个版本里**完全静默**（recent_versions 与 seen 不相交）
    - 当前版本（current_version）又出现了（这里调用方保证 current_version 命中）
    """
    if not fingerprint_seen_versions:
        return False  # 全新 fingerprint，不算 regression

    if len(recent_versions) < silent_threshold:
        return False  # 历史窗口不足，无法判定

    seen_set = set(fingerprint_seen_versions)
    recent_set = set(recent_versions)

    # 历史出现过 + 最近窗口完全静默 = regression
    if seen_set & recent_set:
        return False  # 最近还出现过，不算静默
    return True
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_classifier.py -v
```

Expected: 7 passed.

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/services/classifier.py backend/tests/crashguard/test_classifier.py
git commit -m "feat(crashguard): is_regression 分类（回归崩溃）"
```

---

### Task 18: TDD — is_surge（环比飙升）

**Files:**
- Modify: `backend/app/crashguard/services/classifier.py`
- Modify: `backend/tests/crashguard/test_classifier.py`

- [ ] **Step 1: 加测试**

在 `test_classifier.py` 末尾追加：

```python
def test_is_surge_true_when_more_than_multiplier_and_min_events():
    """today=20, prev_avg=10, multiplier=1.5, min_events=10 → 20 > 15 AND 20 >= 10 → True"""
    from app.crashguard.services.classifier import is_surge

    assert is_surge(
        today_events=20, prev_avg_events=10,
        multiplier=1.5, min_events=10,
    ) is True


def test_is_surge_false_when_below_multiplier():
    """today=14, prev_avg=10, multiplier=1.5 → 14 < 15 → False"""
    from app.crashguard.services.classifier import is_surge

    assert is_surge(
        today_events=14, prev_avg_events=10,
        multiplier=1.5, min_events=10,
    ) is False


def test_is_surge_false_when_below_min_events():
    """today=8, prev_avg=2, ratio=4 但 8 < min_events=10 → False（防小数刷量）"""
    from app.crashguard.services.classifier import is_surge

    assert is_surge(
        today_events=8, prev_avg_events=2,
        multiplier=1.5, min_events=10,
    ) is False


def test_is_surge_handles_zero_baseline():
    """prev_avg=0 时，只要超 min_events 就算 surge（无前值，新爆发）"""
    from app.crashguard.services.classifier import is_surge

    assert is_surge(
        today_events=15, prev_avg_events=0,
        multiplier=1.5, min_events=10,
    ) is True

    assert is_surge(
        today_events=5, prev_avg_events=0,
        multiplier=1.5, min_events=10,
    ) is False  # 仍未到 min_events
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_classifier.py -v
```

Expected: 4 fail (ImportError on is_surge).

- [ ] **Step 3: 实现 is_surge**

在 `classifier.py` 末尾追加：

```python
def is_surge(
    today_events: int,
    prev_avg_events: float,
    multiplier: float = 1.5,
    min_events: int = 10,
) -> bool:
    """
    飙升判定:
    - today_events > prev_avg_events * multiplier
    - 且 today_events >= min_events（防小基数刷量）
    - prev_avg_events == 0 时，只要 today_events >= min_events 就算
    """
    if today_events < min_events:
        return False
    if prev_avg_events <= 0:
        return True
    return today_events > prev_avg_events * multiplier
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_classifier.py -v
```

Expected: 11 passed.

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/services/classifier.py backend/tests/crashguard/test_classifier.py
git commit -m "feat(crashguard): is_surge 分类（飙升崩溃）"
```

---

### Task 19: classify_today 一站式分类（DB 集成）

**Files:**
- Modify: `backend/app/crashguard/services/classifier.py`
- Modify: `backend/tests/crashguard/test_classifier.py`

- [ ] **Step 1: 加测试**

在 `test_classifier.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_classify_today_writes_three_flags(tmp_path, monkeypatch):
    """classify_today 跑完，crash_snapshots 当天三个 flag 字段都填上"""
    from datetime import date, datetime, timedelta
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa
    from app.crashguard.models import CrashSnapshot, CrashIssue, CrashFingerprint
    from app.crashguard.services.classifier import classify_today

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cls.db'}")
    from app.config import get_settings
    get_settings.cache_clear()
    await init_db()

    today = date.today()
    yesterday = today - timedelta(days=1)

    # Issue 1: 全新（first_seen_version == 1.4.7）
    # Issue 2: 飙升（昨日 5 事件，今日 30）— 但需 min_events=10 满足
    # Issue 3: 回归（fingerprint 之前在 1.4.3 出现，最近 1.4.4/5/6 静默，今日 1.4.7 出现）
    async with get_session() as s:
        s.add(CrashIssue(
            datadog_issue_id="i1", stack_fingerprint="fp1", platform="flutter",
            first_seen_version="1.4.7", last_seen_version="1.4.7",
        ))
        s.add(CrashIssue(
            datadog_issue_id="i2", stack_fingerprint="fp2", platform="flutter",
            first_seen_version="1.4.3", last_seen_version="1.4.7",
        ))
        s.add(CrashIssue(
            datadog_issue_id="i3", stack_fingerprint="fp3", platform="flutter",
            first_seen_version="1.4.3", last_seen_version="1.4.7",
        ))
        # 今日 snapshot
        s.add(CrashSnapshot(datadog_issue_id="i1", snapshot_date=today, app_version="1.4.7", events_count=10))
        s.add(CrashSnapshot(datadog_issue_id="i2", snapshot_date=today, app_version="1.4.7", events_count=30))
        s.add(CrashSnapshot(datadog_issue_id="i3", snapshot_date=today, app_version="1.4.7", events_count=15))
        # 昨日 snapshot（用于 surge 计算）
        s.add(CrashSnapshot(datadog_issue_id="i2", snapshot_date=yesterday, app_version="1.4.6", events_count=5))
        # fingerprint 历史
        import json as _json
        s.add(CrashFingerprint(
            fingerprint="fp3",
            datadog_issue_ids=_json.dumps(["i3"]),
            first_seen_version="1.4.3",
        ))
        await s.commit()

    async with get_session() as s:
        await classify_today(
            session=s,
            today=today,
            latest_release="1.4.7",
            recent_versions=["1.4.4", "1.4.5", "1.4.6"],
            surge_multiplier=1.5,
            surge_min_events=10,
            regression_silent_threshold=3,
        )
        await s.commit()

    async with get_session() as s:
        from sqlalchemy import select
        rows = (await s.execute(
            select(CrashSnapshot).where(CrashSnapshot.snapshot_date == today)
        )).scalars().all()
        by_id = {r.datadog_issue_id: r for r in rows}

        assert by_id["i1"].is_new_in_version is True
        assert by_id["i2"].is_surge is True
        assert by_id["i3"].is_regression is True
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_classifier.py::test_classify_today_writes_three_flags -v
```

Expected: ImportError on classify_today — fail.

- [ ] **Step 3: 实现 classify_today**

在 `classifier.py` 末尾追加：

```python
import json
from datetime import date, timedelta
from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def classify_today(
    session: AsyncSession,
    today: date,
    latest_release: str,
    recent_versions: List[str],
    surge_multiplier: float = 1.5,
    surge_min_events: int = 10,
    regression_silent_threshold: int = 3,
    surge_baseline_days: int = 7,
) -> None:
    """
    跑完后，crash_snapshots 当天每行的 is_new_in_version / is_regression /
    is_surge 三个 flag 都被填上。
    """
    from app.crashguard.models import CrashSnapshot, CrashIssue, CrashFingerprint

    # 1. 拉今日所有 snapshot + 关联 issue + fingerprint
    today_rows = (await session.execute(
        select(CrashSnapshot).where(CrashSnapshot.snapshot_date == today)
    )).scalars().all()

    if not today_rows:
        return

    issue_ids = [r.datadog_issue_id for r in today_rows]
    issues = (await session.execute(
        select(CrashIssue).where(CrashIssue.datadog_issue_id.in_(issue_ids))
    )).scalars().all()
    issue_by_id = {i.datadog_issue_id: i for i in issues}

    fingerprints = {i.stack_fingerprint for i in issues if i.stack_fingerprint}
    fp_rows = (await session.execute(
        select(CrashFingerprint).where(CrashFingerprint.fingerprint.in_(fingerprints))
    )).scalars().all()
    fp_by_key = {f.fingerprint: f for f in fp_rows}

    # 2. surge 基线: 过去 surge_baseline_days 的 events 平均
    baseline_start = today - timedelta(days=surge_baseline_days)
    baseline_rows = (await session.execute(
        select(CrashSnapshot).where(
            CrashSnapshot.snapshot_date >= baseline_start,
            CrashSnapshot.snapshot_date < today,
        )
    )).scalars().all()
    baseline_by_id: dict = {}
    for b in baseline_rows:
        baseline_by_id.setdefault(b.datadog_issue_id, []).append(b.events_count or 0)

    # 3. 逐条更新 flag
    for snap in today_rows:
        issue = issue_by_id.get(snap.datadog_issue_id)
        if not issue:
            continue

        # is_new_in_version
        snap.is_new_in_version = is_new_in_version(
            first_seen_version=issue.first_seen_version or "",
            latest_release=latest_release,
        )

        # is_regression
        fp_seen_versions: List[str] = []
        if issue.stack_fingerprint and issue.stack_fingerprint in fp_by_key:
            # crash_fingerprints.datadog_issue_ids 是 JSON list, 但版本在 issues 表
            # 简化：取该 fingerprint 关联所有 issue 的 last_seen_version 集合作为 seen
            ids_for_fp = json.loads(fp_by_key[issue.stack_fingerprint].datadog_issue_ids or "[]")
            for related in issues:
                if related.datadog_issue_id in ids_for_fp and related.last_seen_version:
                    fp_seen_versions.append(related.last_seen_version)

        snap.is_regression = is_regression(
            fingerprint_seen_versions=fp_seen_versions,
            recent_versions=recent_versions,
            current_version=latest_release,
            silent_threshold=regression_silent_threshold,
        )

        # is_surge
        history = baseline_by_id.get(snap.datadog_issue_id, [])
        prev_avg = sum(history) / len(history) if history else 0.0
        snap.is_surge = is_surge(
            today_events=snap.events_count or 0,
            prev_avg_events=prev_avg,
            multiplier=surge_multiplier,
            min_events=surge_min_events,
        )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_classifier.py -v
```

Expected: 12 passed.

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/services/classifier.py backend/tests/crashguard/test_classifier.py
git commit -m "feat(crashguard): classify_today 一站式三维分类（DB 集成）"
```

---

## Phase F / Ranker Top20（Tasks 20-22）

### Task 20: TDD — crash_free_impact_score 计算

**Files:**
- Create: `backend/app/crashguard/services/ranker.py`
- Create: `backend/tests/crashguard/test_ranker.py`

- [ ] **Step 1: 写测试**

写入 `backend/tests/crashguard/test_ranker.py`：

```python
"""Top20 排序器测试"""
from __future__ import annotations

import pytest


def test_compute_impact_score_basic():
    """impact_score = users_affected × log10(events_count + 1) — 简单分布"""
    from app.crashguard.services.ranker import compute_impact_score

    # 基线: 高用户数 × 中等事件数 → 高分
    score_high = compute_impact_score(users_affected=100, events_count=1000)
    score_low = compute_impact_score(users_affected=5, events_count=10)
    assert score_high > score_low


def test_compute_impact_score_returns_zero_for_empty():
    """无数据时为 0"""
    from app.crashguard.services.ranker import compute_impact_score
    assert compute_impact_score(users_affected=0, events_count=0) == 0.0


def test_compute_impact_score_user_dominated():
    """1 用户崩 1000 次 < 1000 用户各崩 1 次（用户多样性优先）"""
    from app.crashguard.services.ranker import compute_impact_score
    s_one_user = compute_impact_score(users_affected=1, events_count=1000)
    s_many = compute_impact_score(users_affected=1000, events_count=1)
    assert s_many > s_one_user
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_ranker.py -v
```

Expected: ImportError — fail.

- [ ] **Step 3: 实现 compute_impact_score**

写入 `backend/app/crashguard/services/ranker.py`：

```python
"""
Top20 排序器：
- compute_impact_score: crash-free 影响分（用户优先 + 事件加权）
- pick_top_n           : 取 Top N，P0（new/regression）强制入选
"""
from __future__ import annotations

import math
from typing import List


def compute_impact_score(users_affected: int, events_count: int) -> float:
    """
    Crash-free 影响分:
        score = users_affected * log10(events_count + 1)

    底层逻辑: 受影响用户数为主权重，事件次数对数加权（避免单用户死循环刷榜）。
    """
    users = max(0, int(users_affected or 0))
    events = max(0, int(events_count or 0))
    if users == 0 and events == 0:
        return 0.0
    return users * math.log10(events + 1)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_ranker.py -v
```

Expected: 3 passed.

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/services/ranker.py backend/tests/crashguard/test_ranker.py
git commit -m "feat(crashguard): compute_impact_score 影响分公式"
```

---

### Task 21: TDD — pick_top_n（P0 强制入选 + P1 排序）

**Files:**
- Modify: `backend/app/crashguard/services/ranker.py`
- Modify: `backend/tests/crashguard/test_ranker.py`

- [ ] **Step 1: 加测试**

在 `test_ranker.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_pick_top_n_p0_priority(tmp_path, monkeypatch):
    """P0 (is_new OR is_regression) 强制入选；剩余按 impact_score 排序"""
    from datetime import date
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa
    from app.crashguard.models import CrashSnapshot, CrashIssue
    from app.crashguard.services.ranker import pick_top_n

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'rank.db'}")
    from app.config import get_settings
    get_settings.cache_clear()
    await init_db()

    today = date.today()

    async with get_session() as s:
        # i_p0_new: P0 全新（影响分较低，但必须入选）
        s.add(CrashIssue(datadog_issue_id="i_p0_new", platform="flutter", title="P0 new"))
        s.add(CrashSnapshot(
            datadog_issue_id="i_p0_new", snapshot_date=today,
            events_count=5, users_affected=2, crash_free_impact_score=0.6,
            is_new_in_version=True,
        ))
        # i_p0_reg: P0 回归
        s.add(CrashIssue(datadog_issue_id="i_p0_reg", platform="flutter", title="P0 reg"))
        s.add(CrashSnapshot(
            datadog_issue_id="i_p0_reg", snapshot_date=today,
            events_count=10, users_affected=3, crash_free_impact_score=3.0,
            is_regression=True,
        ))
        # i_p1_high: P1 高影响
        s.add(CrashIssue(datadog_issue_id="i_p1_high", platform="flutter", title="P1 high"))
        s.add(CrashSnapshot(
            datadog_issue_id="i_p1_high", snapshot_date=today,
            events_count=500, users_affected=80, crash_free_impact_score=216.4,
        ))
        # i_p1_low: P1 低影响
        s.add(CrashIssue(datadog_issue_id="i_p1_low", platform="flutter", title="P1 low"))
        s.add(CrashSnapshot(
            datadog_issue_id="i_p1_low", snapshot_date=today,
            events_count=10, users_affected=5, crash_free_impact_score=5.2,
        ))
        await s.commit()

    async with get_session() as s:
        top = await pick_top_n(s, today=today, n=3)
        ids = [t["datadog_issue_id"] for t in top]
        # P0 必须入选（位置不限，但前 2 个肯定有 P0）
        assert "i_p0_new" in ids
        assert "i_p0_reg" in ids
        # 还应有一个 P1 high（影响分最大的 P1）
        assert "i_p1_high" in ids
        # i_p1_low 影响分最低，3 个名额应被前 3 个挤掉
        assert "i_p1_low" not in ids


@pytest.mark.asyncio
async def test_pick_top_n_returns_sorted_by_score_within_tier(tmp_path, monkeypatch):
    """同 tier 内按 impact_score DESC 排"""
    from datetime import date
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa
    from app.crashguard.models import CrashSnapshot, CrashIssue
    from app.crashguard.services.ranker import pick_top_n

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'rank2.db'}")
    from app.config import get_settings
    get_settings.cache_clear()
    await init_db()

    today = date.today()

    async with get_session() as s:
        for i, score in enumerate([1.0, 100.0, 50.0]):
            s.add(CrashIssue(datadog_issue_id=f"x{i}", platform="flutter", title=f"x{i}"))
            s.add(CrashSnapshot(
                datadog_issue_id=f"x{i}", snapshot_date=today,
                crash_free_impact_score=score,
            ))
        await s.commit()

    async with get_session() as s:
        top = await pick_top_n(s, today=today, n=10)
        scores = [t["crash_free_impact_score"] for t in top]
        assert scores == sorted(scores, reverse=True)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_ranker.py -v
```

Expected: 2 fail (ImportError on pick_top_n).

- [ ] **Step 3: 实现 pick_top_n**

在 `ranker.py` 末尾追加：

```python
from datetime import date
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def pick_top_n(
    session: AsyncSession,
    today: date,
    n: int = 20,
) -> List[Dict[str, Any]]:
    """
    返回 Top N issue（dict 形式）。

    优先级:
    - P0: is_new_in_version OR is_regression → 强制入选
    - P1: 剩余席位按 crash_free_impact_score DESC 填满

    返回字段: datadog_issue_id, title, platform, events_count, users_affected,
             crash_free_impact_score, is_new_in_version, is_regression, is_surge,
             tier ('P0' / 'P1')
    """
    from app.crashguard.models import CrashSnapshot, CrashIssue

    rows = (await session.execute(
        select(CrashSnapshot, CrashIssue)
        .join(
            CrashIssue,
            CrashIssue.datadog_issue_id == CrashSnapshot.datadog_issue_id,
        )
        .where(CrashSnapshot.snapshot_date == today)
    )).all()

    enriched: List[Dict[str, Any]] = []
    for snap, issue in rows:
        enriched.append({
            "datadog_issue_id": snap.datadog_issue_id,
            "title": issue.title or "",
            "platform": issue.platform or "",
            "events_count": snap.events_count or 0,
            "users_affected": snap.users_affected or 0,
            "crash_free_impact_score": snap.crash_free_impact_score or 0.0,
            "is_new_in_version": bool(snap.is_new_in_version),
            "is_regression": bool(snap.is_regression),
            "is_surge": bool(snap.is_surge),
        })

    p0 = [e for e in enriched if e["is_new_in_version"] or e["is_regression"]]
    p1 = [e for e in enriched if not (e["is_new_in_version"] or e["is_regression"])]

    p0.sort(key=lambda e: e["crash_free_impact_score"], reverse=True)
    p1.sort(key=lambda e: e["crash_free_impact_score"], reverse=True)

    selected: List[Dict[str, Any]] = []
    for e in p0[:n]:
        selected.append({**e, "tier": "P0"})
    remaining = n - len(selected)
    if remaining > 0:
        for e in p1[:remaining]:
            selected.append({**e, "tier": "P1"})
    return selected
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_ranker.py -v
```

Expected: 5 passed.

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/services/ranker.py backend/tests/crashguard/test_ranker.py
git commit -m "feat(crashguard): pick_top_n（P0 强制入选 + P1 影响分排序）"
```

---

### Task 22: 同周防重复推送（surge 例外）

**Files:**
- Modify: `backend/app/crashguard/services/ranker.py`
- Modify: `backend/tests/crashguard/test_ranker.py`

- [ ] **Step 1: 加测试**

在 `test_ranker.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_pick_top_n_skips_recently_reported(tmp_path, monkeypatch):
    """同 issue 7 天内已在某日报里推送过 → 跳过（除非 is_surge）"""
    from datetime import date, timedelta
    import json
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa
    from app.crashguard.models import CrashSnapshot, CrashIssue, CrashDailyReport
    from app.crashguard.services.ranker import pick_top_n

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'rank3.db'}")
    from app.config import get_settings
    get_settings.cache_clear()
    await init_db()

    today = date.today()
    five_days_ago = today - timedelta(days=5)

    async with get_session() as s:
        # i_recently_reported: 5 天前已推送，今日普通 P1（应被跳过）
        s.add(CrashIssue(datadog_issue_id="i_dup", platform="flutter", title="dup"))
        s.add(CrashSnapshot(
            datadog_issue_id="i_dup", snapshot_date=today,
            crash_free_impact_score=100.0,
        ))
        # i_dup_surge: 5 天前推过，今日是 surge（应保留）
        s.add(CrashIssue(datadog_issue_id="i_dup_surge", platform="flutter", title="dup surge"))
        s.add(CrashSnapshot(
            datadog_issue_id="i_dup_surge", snapshot_date=today,
            crash_free_impact_score=80.0, is_surge=True,
        ))
        # i_fresh: 全新，未推过
        s.add(CrashIssue(datadog_issue_id="i_fresh", platform="flutter", title="fresh"))
        s.add(CrashSnapshot(
            datadog_issue_id="i_fresh", snapshot_date=today,
            crash_free_impact_score=50.0,
        ))
        # 历史报告记录
        s.add(CrashDailyReport(
            report_date=five_days_ago,
            report_type="morning",
            top_n=2,
            report_payload=json.dumps({
                "issues": [
                    {"datadog_issue_id": "i_dup"},
                    {"datadog_issue_id": "i_dup_surge"},
                ],
            }),
        ))
        await s.commit()

    async with get_session() as s:
        top = await pick_top_n(s, today=today, n=10, dedup_days=7)
        ids = [t["datadog_issue_id"] for t in top]

    assert "i_dup" not in ids          # 7 天内重复 → 跳过
    assert "i_dup_surge" in ids         # surge 例外
    assert "i_fresh" in ids
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_ranker.py::test_pick_top_n_skips_recently_reported -v
```

Expected: TypeError on `dedup_days` 参数 — fail.

- [ ] **Step 3: 修改 pick_top_n 加 dedup_days 逻辑**

修改 `pick_top_n` 签名与实现：

```python
from datetime import date, timedelta
import json as _json


async def pick_top_n(
    session: AsyncSession,
    today: date,
    n: int = 20,
    dedup_days: int = 7,
) -> List[Dict[str, Any]]:
    """
    返回 Top N issue。

    优先级:
    - P0: is_new_in_version OR is_regression → 强制入选
    - P1: 剩余席位按 crash_free_impact_score DESC 填满
    - 同 issue 在 dedup_days 内已推送过 → 跳过（is_surge 例外）
    """
    from app.crashguard.models import CrashSnapshot, CrashIssue, CrashDailyReport

    # 1. 取最近 dedup_days 内已推过的 issue ids
    recently_reported: set = set()
    if dedup_days > 0:
        since = today - timedelta(days=dedup_days)
        report_rows = (await session.execute(
            select(CrashDailyReport).where(CrashDailyReport.report_date >= since)
        )).scalars().all()
        for r in report_rows:
            try:
                payload = _json.loads(r.report_payload or "{}")
                for issue in payload.get("issues", []):
                    iid = issue.get("datadog_issue_id")
                    if iid:
                        recently_reported.add(iid)
            except (ValueError, TypeError):
                continue

    rows = (await session.execute(
        select(CrashSnapshot, CrashIssue)
        .join(
            CrashIssue,
            CrashIssue.datadog_issue_id == CrashSnapshot.datadog_issue_id,
        )
        .where(CrashSnapshot.snapshot_date == today)
    )).all()

    enriched: List[Dict[str, Any]] = []
    for snap, issue in rows:
        # 2. 7 天内已推 + 非 surge → 跳过
        if snap.datadog_issue_id in recently_reported and not snap.is_surge:
            continue
        enriched.append({
            "datadog_issue_id": snap.datadog_issue_id,
            "title": issue.title or "",
            "platform": issue.platform or "",
            "events_count": snap.events_count or 0,
            "users_affected": snap.users_affected or 0,
            "crash_free_impact_score": snap.crash_free_impact_score or 0.0,
            "is_new_in_version": bool(snap.is_new_in_version),
            "is_regression": bool(snap.is_regression),
            "is_surge": bool(snap.is_surge),
        })

    p0 = [e for e in enriched if e["is_new_in_version"] or e["is_regression"]]
    p1 = [e for e in enriched if not (e["is_new_in_version"] or e["is_regression"])]

    p0.sort(key=lambda e: e["crash_free_impact_score"], reverse=True)
    p1.sort(key=lambda e: e["crash_free_impact_score"], reverse=True)

    selected: List[Dict[str, Any]] = []
    for e in p0[:n]:
        selected.append({**e, "tier": "P0"})
    remaining = n - len(selected)
    if remaining > 0:
        for e in p1[:remaining]:
            selected.append({**e, "tier": "P1"})
    return selected
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_ranker.py -v
```

Expected: 6 passed.

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/services/ranker.py backend/tests/crashguard/test_ranker.py
git commit -m "feat(crashguard): Top20 同周防重复（surge 例外）"
```

---

## Phase G / 数据流水线集成（Tasks 23-25）

### Task 23: 写 pipeline.run_data_phase 整合 Step 1-6

**Files:**
- Create: `backend/app/crashguard/workers/__init__.py`
- Create: `backend/app/crashguard/workers/pipeline.py`
- Create: `backend/tests/crashguard/test_pipeline_data.py`

- [ ] **Step 1: 准备 workers 目录**

```bash
mkdir -p backend/app/crashguard/workers
```

写入 `backend/app/crashguard/workers/__init__.py`：

```python
```

- [ ] **Step 2: 写测试（端到端 mock 数据流）**

写入 `backend/tests/crashguard/test_pipeline_data.py`：

```python
"""端到端数据流水线测试（不含 AI）"""
from __future__ import annotations

from datetime import date

import pytest


@pytest.mark.asyncio
async def test_run_data_phase_end_to_end(tmp_path, monkeypatch):
    """
    Mock Datadog → 跑完 pipeline.run_data_phase 后:
    - crash_issues 表有 N 条
    - crash_snapshots 表有 N 条且 is_new_in_version 等 flag 已填
    - crash_fingerprints 表关联正确
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'pipe.db'}")
    monkeypatch.setenv("CRASHGUARD_DATADOG_API_KEY", "test-key")
    monkeypatch.setenv("CRASHGUARD_DATADOG_APP_KEY", "test-app")
    from app.config import get_settings
    from app.crashguard.config import get_crashguard_settings
    get_settings.cache_clear()
    get_crashguard_settings.cache_clear()

    from app.db.database import init_db, get_session
    from app.crashguard import models  # noqa
    from app.crashguard.models import CrashIssue, CrashSnapshot, CrashFingerprint
    await init_db()

    # Mock DatadogClient.list_issues 返回 2 条
    mock_issues = [
        {
            "id": "ddi_1",
            "attributes": {
                "title": "NullPointerException @ play",
                "service": "plaud_ai",
                "platform": "flutter",
                "first_seen_timestamp": 1714003200000,
                "last_seen_timestamp": 1714176000000,
                "first_seen_version": "1.4.7",
                "last_seen_version": "1.4.7",
                "events_count": 145,
                "users_affected": 23,
                "stack_trace": "NPE\n  at AudioPlayer.play (lib/audio/player.dart:42)\n  at PB._start (lib/audio/playback.dart:18)",
                "tags": {"env": "prod"},
            },
        },
        {
            "id": "ddi_2",
            "attributes": {
                "title": "OOM",
                "service": "plaud_ai",
                "platform": "flutter",
                "first_seen_timestamp": 1714003200000,
                "last_seen_timestamp": 1714176000000,
                "first_seen_version": "1.4.5",
                "last_seen_version": "1.4.7",
                "events_count": 30,
                "users_affected": 8,
                "stack_trace": "OOM\n  at ImgDecoder.decode (lib/image/decoder.dart:99)",
                "tags": {},
            },
        },
    ]

    async def fake_list_issues(self, window_hours=24, page_size=100):
        return mock_issues

    from app.crashguard.services.datadog_client import DatadogClient
    monkeypatch.setattr(DatadogClient, "list_issues", fake_list_issues)

    from app.crashguard.workers.pipeline import run_data_phase
    today = date.today()
    result = await run_data_phase(
        today=today,
        latest_release="1.4.7",
        recent_versions=["1.4.4", "1.4.5", "1.4.6"],
    )
    assert result["issues_processed"] == 2
    assert result["snapshots_written"] == 2
    assert result["top_n_count"] >= 1

    async with get_session() as s:
        from sqlalchemy import select, func
        n_issues = (await s.execute(select(func.count()).select_from(CrashIssue))).scalar()
        n_snaps = (await s.execute(
            select(func.count()).select_from(CrashSnapshot).where(CrashSnapshot.snapshot_date == today)
        )).scalar()
        assert n_issues == 2
        assert n_snaps == 2

        ddi1 = (await s.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == "ddi_1")
        )).scalar_one()
        assert ddi1.platform == "flutter"
        assert ddi1.first_seen_version == "1.4.7"
        assert ddi1.stack_fingerprint  # 已计算

        snap1 = (await s.execute(
            select(CrashSnapshot)
            .where(CrashSnapshot.datadog_issue_id == "ddi_1", CrashSnapshot.snapshot_date == today)
        )).scalar_one()
        assert snap1.is_new_in_version is True   # 1.4.7 == latest_release
        assert snap1.crash_free_impact_score > 0
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd backend
pytest tests/crashguard/test_pipeline_data.py -v
```

Expected: ImportError — fail.

- [ ] **Step 4: 实现 pipeline.run_data_phase**

写入 `backend/app/crashguard/workers/pipeline.py`：

```python
"""
Crashguard 端到端流水线 — 数据阶段（Step 1-6）。

不含 AI 分析（Step 7+）—— 由 Plan 2 实现。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crashguard.config import get_crashguard_settings
from app.crashguard.services.classifier import classify_today
from app.crashguard.services.datadog_client import DatadogClient, normalize_issue
from app.crashguard.services.dedup import compute_fingerprint, normalize_stack_frames, upsert_fingerprint_link
from app.crashguard.services.ranker import compute_impact_score, pick_top_n
from app.db.database import get_session

logger = logging.getLogger("crashguard.pipeline")


async def run_data_phase(
    today: date,
    latest_release: str,
    recent_versions: List[str],
) -> Dict[str, Any]:
    """
    Step 1-6 数据阶段：
    1. 拉 Datadog issue（24h 窗口）
    2. 计算 stack_fingerprint
    3. Upsert crash_issues 主表
    4. Upsert crash_snapshots 当日快照（含 impact_score）
    5. 跑 classify_today 三维分类
    6. pick_top_n 选 Top20

    返回:
    {
        "issues_processed": int,
        "snapshots_written": int,
        "top_n_count": int,
        "top_n": [...],
    }
    """
    s = get_crashguard_settings()
    if not s.datadog_api_key:
        logger.warning("CRASHGUARD_DATADOG_API_KEY 未配置，pipeline 跳过")
        return {"issues_processed": 0, "snapshots_written": 0, "top_n_count": 0, "top_n": []}

    client = DatadogClient(
        api_key=s.datadog_api_key,
        app_key=s.datadog_app_key,
        site=s.datadog_site,
    )
    raw_issues = await client.list_issues(window_hours=s.datadog_window_hours)
    logger.info("Datadog 拉取 %d 条 issue", len(raw_issues))

    issues_processed = 0
    snapshots_written = 0

    async with get_session() as session:
        for raw in raw_issues:
            norm = normalize_issue(raw)
            if not norm["datadog_issue_id"]:
                continue

            # Step 2: fingerprint
            fp = compute_fingerprint(norm["stack_trace"])
            top_frames = normalize_stack_frames(norm["stack_trace"])

            # Step 3: upsert issues
            await _upsert_issue(session, norm, fp)

            # 关联 fingerprint 表
            await upsert_fingerprint_link(
                session=session,
                fingerprint=fp,
                datadog_issue_id=norm["datadog_issue_id"],
                first_seen_version=norm["first_seen_version"],
                events_count=norm["events_count"],
                normalized_top_frames=top_frames,
            )

            # Step 4: upsert snapshot
            await _upsert_snapshot(session, today, norm)

            issues_processed += 1
            snapshots_written += 1

        await session.commit()

        # Step 5: 三维分类
        await classify_today(
            session=session,
            today=today,
            latest_release=latest_release,
            recent_versions=recent_versions,
            surge_multiplier=s.surge_multiplier,
            surge_min_events=s.surge_min_events,
            regression_silent_threshold=s.regression_silent_versions,
        )
        await session.commit()

        # Step 6: 选 Top N
        top = await pick_top_n(session, today=today, n=s.max_top_n)

    logger.info(
        "pipeline data phase done: issues=%d snapshots=%d top_n=%d",
        issues_processed, snapshots_written, len(top),
    )
    return {
        "issues_processed": issues_processed,
        "snapshots_written": snapshots_written,
        "top_n_count": len(top),
        "top_n": top,
    }


async def _upsert_issue(
    session: AsyncSession,
    norm: Dict[str, Any],
    stack_fingerprint: str,
) -> None:
    """upsert crash_issues 主表"""
    from app.crashguard.models import CrashIssue

    row = (await session.execute(
        select(CrashIssue).where(CrashIssue.datadog_issue_id == norm["datadog_issue_id"])
    )).scalar_one_or_none()

    if row is None:
        row = CrashIssue(
            datadog_issue_id=norm["datadog_issue_id"],
            stack_fingerprint=stack_fingerprint,
            title=norm["title"],
            platform=norm["platform"],
            service=norm["service"],
            first_seen_at=norm["first_seen_at"],
            first_seen_version=norm["first_seen_version"],
            last_seen_at=norm["last_seen_at"],
            last_seen_version=norm["last_seen_version"],
            total_events=norm["events_count"],
            total_users_affected=norm["users_affected"],
            representative_stack=norm["stack_trace"][:8000],  # 限长
            tags=json.dumps(norm["tags"]),
        )
        session.add(row)
    else:
        row.stack_fingerprint = stack_fingerprint
        row.title = norm["title"] or row.title
        row.last_seen_at = norm["last_seen_at"] or row.last_seen_at
        row.last_seen_version = norm["last_seen_version"] or row.last_seen_version
        row.total_events = max(row.total_events or 0, norm["events_count"])
        row.total_users_affected = max(row.total_users_affected or 0, norm["users_affected"])
        if not row.representative_stack:
            row.representative_stack = norm["stack_trace"][:8000]


async def _upsert_snapshot(
    session: AsyncSession,
    snapshot_date: date,
    norm: Dict[str, Any],
) -> None:
    """upsert crash_snapshots 当日行（impact_score 一并算）"""
    from app.crashguard.models import CrashSnapshot

    row = (await session.execute(
        select(CrashSnapshot).where(
            CrashSnapshot.datadog_issue_id == norm["datadog_issue_id"],
            CrashSnapshot.snapshot_date == snapshot_date,
        )
    )).scalar_one_or_none()

    score = compute_impact_score(
        users_affected=norm["users_affected"],
        events_count=norm["events_count"],
    )

    if row is None:
        row = CrashSnapshot(
            datadog_issue_id=norm["datadog_issue_id"],
            snapshot_date=snapshot_date,
            app_version=norm["last_seen_version"],
            events_count=norm["events_count"],
            users_affected=norm["users_affected"],
            crash_free_impact_score=score,
        )
        session.add(row)
    else:
        row.events_count = norm["events_count"]
        row.users_affected = norm["users_affected"]
        row.crash_free_impact_score = score
        row.app_version = norm["last_seen_version"] or row.app_version
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd backend
pytest tests/crashguard/test_pipeline_data.py -v
```

Expected: 1 passed.

- [ ] **Step 6: 跑全部 crashguard 测试确认无回归**

```bash
cd backend
pytest tests/crashguard/ -v
```

Expected: 全绿，36+ passed.

- [ ] **Step 7: 提交**

```bash
git add backend/app/crashguard/workers \
        backend/tests/crashguard/test_pipeline_data.py
git commit -m "feat(crashguard): pipeline 数据阶段（Step 1-6 集成）"
```

---

### Task 24: 加 manual trigger API + smoke test

**Files:**
- Create: `backend/app/crashguard/api/__init__.py`
- Create: `backend/app/crashguard/api/crash.py`
- Modify: `backend/app/main.py` — 注册 router

- [ ] **Step 1: 创建 api 目录**

```bash
mkdir -p backend/app/crashguard/api
```

写入 `backend/app/crashguard/api/__init__.py`：

```python
```

- [ ] **Step 2: 写 manual trigger 端点**

写入 `backend/app/crashguard/api/crash.py`：

```python
"""crashguard API — manual trigger / health"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.crashguard.config import get_crashguard_settings

logger = logging.getLogger("crashguard.api")

router = APIRouter(prefix="/api/crash", tags=["crashguard"])


class TriggerRequest(BaseModel):
    latest_release: str = Field(..., description="当前最新发布版本，如 '1.4.7'")
    recent_versions: List[str] = Field(default_factory=list, description="最近 N 个版本（用于回归判定）")
    target_date: Optional[date] = Field(None, description="指定快照日期，默认今日")


class TriggerResponse(BaseModel):
    issues_processed: int
    snapshots_written: int
    top_n_count: int


@router.post("/trigger", response_model=TriggerResponse)
async def trigger_pipeline(req: TriggerRequest) -> Any:
    """
    手动触发数据流水线 (Step 1-6)。

    AI 分析与日报推送在 Plan 2/3 实现。
    """
    s = get_crashguard_settings()
    if not s.enabled:
        raise HTTPException(status_code=503, detail="crashguard 已被 kill switch 关闭")

    from app.crashguard.workers.pipeline import run_data_phase

    target_date = req.target_date or date.today()
    try:
        result = await run_data_phase(
            today=target_date,
            latest_release=req.latest_release,
            recent_versions=req.recent_versions,
        )
    except Exception as e:
        logger.exception("pipeline failed")
        raise HTTPException(status_code=500, detail=f"pipeline failed: {e}")

    return TriggerResponse(
        issues_processed=result["issues_processed"],
        snapshots_written=result["snapshots_written"],
        top_n_count=result["top_n_count"],
    )


@router.get("/health")
async def health() -> Dict[str, Any]:
    """模块健康检查"""
    s = get_crashguard_settings()
    return {
        "module": "crashguard",
        "enabled": s.enabled,
        "datadog_configured": bool(s.datadog_api_key),
        "feishu_target_set": bool(s.feishu_target_chat_id),
    }


@router.get("/top")
async def get_top(target_date: Optional[date] = None, limit: int = 20) -> Dict[str, Any]:
    """读取指定日期的 Top N（不重新跑流水线）"""
    from app.db.database import get_session
    from app.crashguard.services.ranker import pick_top_n

    if target_date is None:
        target_date = date.today()

    async with get_session() as session:
        top = await pick_top_n(session, today=target_date, n=limit)
    return {"date": target_date.isoformat(), "count": len(top), "issues": top}
```

- [ ] **Step 3: 在 main.py 注册 router**

修改 `backend/app/main.py`，找到现有 router 注册段（如 `app.include_router(...)` 列表），追加：

```python
    from app.crashguard.api import crash as _crash_api
    app.include_router(_crash_api.router)
```

（具体位置：在其他 `include_router` 调用旁）

- [ ] **Step 4: 启动并 smoke test**

```bash
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!
sleep 5

# health
curl -s http://localhost:8000/api/crash/health | python -m json.tool

# top（应为空）
curl -s "http://localhost:8000/api/crash/top?target_date=$(date +%Y-%m-%d)" | python -m json.tool

kill $SERVER_PID 2>/dev/null
```

Expected health 输出：
```json
{
  "module": "crashguard",
  "enabled": true,
  "datadog_configured": false,
  "feishu_target_set": false
}
```

Expected top 输出：
```json
{
  "date": "2026-04-27",
  "count": 0,
  "issues": []
}
```

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/api backend/app/main.py
git commit -m "feat(crashguard): manual trigger / health / top API 端点"
```

---

### Task 25: 跑 import-linter + 最终回归 + 推分支

**Files:** 无

- [ ] **Step 1: 跑 import-linter 确保零违规**

```bash
cd backend
lint-imports
```

Expected: `Contracts: 1 kept, 0 broken.`

- [ ] **Step 2: 跑全部测试**

```bash
cd backend
pytest tests/crashguard/ -v --tb=short
```

Expected: 全绿。统计 passed 数 ≥ 36。

- [ ] **Step 3: 跑 jarvis 既有测试确认无回归**

```bash
cd backend
pytest tests/ -v --tb=short -x
```

Expected: 既有测试全绿（不应该有任何 jarvis 既有功能因为 crashguard 接入而 break）。

- [ ] **Step 4: 跑 DB 自检脚本一次（命令行）**

```bash
cd backend
python -m scripts.check_crash_decoupling
```

Expected: `✅ crash_* 表解耦检查通过`

- [ ] **Step 5: 推分支**

```bash
git push -u origin zhangmeng/feature/crash_guard
```

确认远程接收成功。

- [ ] **Step 6: 写 PR 描述（手动）**

```bash
gh pr create --draft --title "[crashguard] Plan 1 / Foundation + Data Layer" --body "$(cat <<'EOF'
## Summary

实现 crashguard 子模块基础层（Plan 1/3）：模块骨架、强解耦防腐、7 张表、Datadog 数据接入、stack_fingerprint 跨版本去重、三维分类、Top20 排序。

完成后 jarvis 可以从 Datadog 拉数据 → 入库 → 分类 → 排序，**未集成 AI 分析与日报推送**（Plan 2/3 实现）。

## What's New

### Module Skeleton & Decoupling
- `backend/app/crashguard/` 子包 + CLAUDE.md / README.md
- `docs/adr/0001-crashguard-isolation.md` 架构决策
- `backend/.importlinter.cfg` lint 合约（CI 强制）
- `backend/scripts/check_crash_decoupling.py` 启动时 DB 自检

### Database
- 7 张 `crash_*` 表（issues / snapshots / fingerprints / analyses / pull_requests / daily_reports / versions）
- 集成进 main.py lifespan，启动时自动注册 + 自检

### Data Pipeline
- `services/datadog_client.py` — Datadog Error Tracking API（重试 + 限流熔断 + 分页）
- `services/dedup.py` — stack_fingerprint 算法（行号/闭包/版本路径剥离）
- `services/classifier.py` — is_new_in_version / is_regression / is_surge 三维分类
- `services/ranker.py` — Top20 排序（P0 强制入选 + 同周防重 + impact_score 加权）
- `workers/pipeline.py` — 端到端 Step 1-6 编排

### API
- `POST /api/crash/trigger` 手动触发数据流水线
- `GET /api/crash/health` 模块健康
- `GET /api/crash/top` 读取指定日期 Top N

## Test plan

- [ ] `cd backend && pytest tests/crashguard/ -v` 全绿
- [ ] `cd backend && lint-imports` 零违规
- [ ] `cd backend && pytest tests/ -v` 既有测试无回归
- [ ] 启动后 `curl /api/crash/health` 200
- [ ] 配置 Datadog key 后 `curl /api/crash/trigger` 实跑数据流水线（生产前在 staging 验证）

## Spec

`docs/superpowers/specs/2026-04-27-crashguard-design.md` (commit 2ecd225)

## Next

- Plan 2: AI Analysis Engine (3 平台 analyzer + Flutter Verifier + reproducer)
- Plan 3: Output + Operations (Feishu reporter + 半自动 PR API + scheduler + 灰度)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 7: 标记 Plan 1 完成**

将本计划文件中所有未勾选 `- [ ]` 检查为已完成（手动或脚本）。

```bash
echo "✅ Plan 1 / Foundation + Data Layer 完成"
```

---

## Self-Review

**Spec coverage check:**

| Spec 章节 | 是否覆盖 | 实现 Task |
|-----------|---------|----------|
| §1 顶层架构 + 模块边界 | ✅ | Task 1, 7 |
| §1.4 解耦约束防腐 | ✅ | Task 4 (ADR), 5 (lint), 6 (DB 自检), 3 (CLAUDE.md) |
| §2 DB Schema 7 张表 | ✅ | Task 8, 9 |
| §3.2 Step 1-6 流水线 | ✅ | Task 23 (整合) |
| §3.3 ReproducerContext | ⏳ | Plan 2（AI 分析阶段） |
| §3.4 Agent 输出契约 | ⏳ | Plan 2 |
| §3.5 Quality Gate | ⏳ | Plan 2 |
| §3.6 平台覆盖矩阵 | ⏳ | Plan 2 |
| §3.7 Agent 工具白名单 | ⏳ | Plan 2 |
| §3.8 PR 安全栏 | ⏳ | Plan 3 |
| §3.9 Stack fingerprint | ✅ | Task 14, 15 |
| §3.10 超时预算 | ⏳ | Plan 3（含 scheduler 时） |
| §3.11 半自动 PR | ⏳ | Plan 3 |
| §4 Feishu 日报 | ⏳ | Plan 3 |
| §5.1 错误分类 | 部分 ✅ | Task 11/12（Datadog 错误处理）；Plan 3 完整覆盖 |
| §5.2 重试与幂等 | 部分 ✅ | Task 11/12, 23（upsert 幂等） |
| §5.3 监控埋点 | ⏳ | Plan 3 |
| §5.4 测试策略 | ✅ | 各 Task TDD |
| §5.5 灰度上线 | ⏳ | Plan 3 |
| §5.6 紧急回滚 | ✅ | Task 10（kill switch 配置） |

**Plan 1 范围：spec 中数据层 + 解耦防腐部分完整覆盖。AI 分析层 / 输出层 / 灰度上线分别由 Plan 2 / Plan 3 实现。**

**Placeholder scan:** 通读全计划无 TODO/TBD/"implement later"。所有代码块完整可运行。

**Type consistency:**
- `compute_fingerprint(stack_trace)` 在 Task 14 定义、Task 23 调用 — 签名一致 ✅
- `normalize_stack_frames(stack_trace, top_n=5)` 同上 ✅
- `is_new_in_version(first_seen_version, latest_release)` 在 Task 16 定义、Task 19 调用 ✅
- `pick_top_n(session, today, n=20, dedup_days=7)` 在 Task 22 改签名后所有调用同步 ✅
- `classify_today(session, today, latest_release, recent_versions, ...)` 在 Task 19 定义、Task 23 调用 ✅
- 所有 model 字段（`stack_fingerprint`, `is_new_in_version`, `is_regression`, `is_surge`, `crash_free_impact_score`）在 Task 8 模型与各调用点拼写一致 ✅

**Plan 1 自审通过。**
