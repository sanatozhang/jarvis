# 源码仓库路由（按工单类型 + 版本）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 引入单一真相源 `repo_router`，按 `(platform, version)` 解析出源码路径 / 子仓 / GitHub 仓 / 符号化 profile / family，统一驱动「工单分析源码、crashguard PR 目标仓、崩溃符号化来源、Datadog service 过滤」四个出口，支撑 Flutter→native 按版本切换 + web/desktop 工单分析。

**Architecture:** 新建纯函数模块 `app/services/repo_router.py`（零副作用、先 TDD 立地基），配置走「每平台版本带 bands」（`min_version` 降序匹配，4.0.0 为 Flutter/native 切换线）。四个出口的现有调用点改为消费 `repo_router.resolve(...)`。配置走 env > DB(UI 设置页) > yaml > defaults，复用现有 agent_overrides 持久化模式。crashguard 调 repo_router 是新增隔离耦合点，走 ADR + importlinter 白名单。

**Tech Stack:** Python 3 / FastAPI / Pydantic Settings / SQLAlchemy(SQLite) / pytest；前端 Next.js 15 + React 19 + Tailwind 4。

## Global Constraints

- **切换线 verbatim**：`3.x.x` 及以下 = Flutter family，`4.0.0` 起 = native family；android / ios 同线 `4.0.0`。
- **版本比较**：先 strip build 后缀（`3.16.0-634` → `3.16.0`）再 semver 比较；band 按 `min_version` 降序取第一个 `version >= min_version`。
- **crash issue 路由用版本**：用 `representative_stack` JSON 的 `sample_app_version`（单值），**禁止**用 `top_app_version`（分布串）。
- **降级铁律**：`resolve` 返回 `None` 时 → 工单分析走 logs-only、crashguard 不建 PR，**绝不抛异常崩流程**。
- **crashguard 隔离合约**：crashguard 仅允许通过白名单耦合点调 jarvis；新增 `app.services.repo_router` 必须同步改 `backend/.importlinter` + ADR-0001，否则 `lint-imports` / 启动自检会红。
- **不主动 commit/push/deploy**：每个 Task 末尾的 `git commit` 是本地分支提交（subagent-driven 流程内）；**严禁 push、严禁 deploy、严禁改服务器**。
- **服务器路径 verbatim**：旧 Flutter 壳 `/Users/mac/Downloads/plaud_ai`、native `/Users/mac/Downloads/plaud-native-app`、web `/Users/mac/Downloads/plaud-web`、desktop `/Users/mac/Downloads/fe-nexus`。本地 Flutter 壳 `/Users/sanato/Desktop/code/newplaud/Plaud2`。
- **仓结构**：`plaud-native-app` / `fe-nexus` = git submodule 壳（有顶层 `.git` + `.gitmodules`）；`plaud-web` = 普通单仓；旧 `plaud_ai` = mt 工作区（无顶层 `.git`）。

---

## 文件结构

| 文件 | 责任 | 动作 |
|---|---|---|
| `backend/app/services/repo_router.py` | 核心解析：bands → RepoResolution（纯函数）| Create |
| `backend/tests/test_repo_router.py` | repo_router 单测 | Create |
| `backend/app/config.py` | `repo_routing` 配置加载 + backfill；`get_code_repo_for_platform` 降级 thin wrapper | Modify |
| `config.yaml` | `repo_routing` 段 + `crashguard.datadog.service_filter` | Modify |
| `backend/app/workers/analysis_worker.py` | 源码出口：用 router + version | Modify `:581` |
| `backend/app/services/eval_runner.py` | 源码出口：用 router + version | Modify `:227` |
| `backend/app/services/repo_updater.py` | submodule 壳更新分支 + 每仓 lock + 从 routing 收集 wrappers | Modify |
| `backend/app/crashguard/services/pr_drafter.py` | PR 出口：用 router 选仓 + family 门控 + github_repo 目标 | Modify |
| `backend/app/crashguard/services/github_symbols.py` | 符号来源仓参数化（`_REPO` → 入参）| Modify |
| `backend/app/crashguard/services/symbolication.py` | 按 symbol_profile + github_repo 分流 | Modify |
| `backend/app/crashguard/services/analyzer.py` | 符号化调用点传 github_repo + symbol_profile | Modify |
| `backend/app/crashguard/config.py` | `datadog_service_filter` 默认覆盖两代 | Modify `:140` |
| `backend/.importlinter` + `docs/adr/0001-crashguard-isolation.md` | 隔离合约新增耦合点 | Modify |
| `backend/app/api/settings.py` | `repo_routing` GET/PUT + DB override + 启动 apply + 解析预览/校验 | Modify |
| `frontend/src/app/settings/...` | 「源码仓库路由」卡片 | Modify/Create |

---

## Task 1: `repo_router` 核心模块（纯函数 + TDD）

**Files:**
- Create: `backend/app/services/repo_router.py`
- Test: `backend/tests/test_repo_router.py`

**Interfaces:**
- Consumes: 配置 dict（本任务用注入参数，下一任务接 `config.get_repo_routing()`）
- Produces:
  - `@dataclass RepoResolution(family, platform, wrapper_path, sub_repo_path, logical_name, github_repo, symbol_profile, confidence)`
  - `parse_version(v: str | None) -> tuple[int,int,int] | None`
  - `select_band(bands: list[dict], version: str | None) -> tuple[dict, str]`  返回 `(band, confidence)`
  - `resolve(platform, version, routing, *, sub_hint="", stack_text="", path_exists=os.path.exists) -> RepoResolution | None`
  - `normalize_platform(raw: str, os_name: str = "") -> str | None`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_repo_router.py
import pytest
from app.services import repo_router as rr

ROUTING = {
    "android": {"bands": [
        {"min_version": "0", "family": "flutter", "wrapper": "/repos/plaud_ai",
         "sub": "plaud-android", "github_repo": "Plaud-AI/Plaud-App", "symbol_profile": "flutter_android"},
        {"min_version": "4.0.0", "family": "native", "wrapper": "/repos/plaud-native-app",
         "sub": "plaud-native-android", "github_repo": "Plaud-AI/plaud-native-android", "symbol_profile": "native_android"},
    ]},
    "ios": {"bands": [
        {"min_version": "0", "family": "flutter", "wrapper": "/repos/plaud_ai",
         "sub": "plaud-ios", "github_repo": "Plaud-AI/Plaud-App", "symbol_profile": "flutter_ios"},
        {"min_version": "4.0.0", "family": "native", "wrapper": "/repos/plaud-native-app",
         "sub": "plaud-native-ios", "github_repo": "Plaud-AI/plaud-native-ios", "symbol_profile": "native_ios"},
    ]},
    "web": {"bands": [
        {"min_version": "0", "family": "web", "wrapper": "/repos/plaud-web",
         "sub": "", "github_repo": "Plaud-AI/plaud-web", "symbol_profile": "none"},
    ]},
}

# 测试里所有路径都"存在"
ALWAYS = lambda p: True


def test_parse_version_strips_build_suffix():
    assert rr.parse_version("3.16.0-634") == (3, 16, 0)
    assert rr.parse_version("4.0.0") == (4, 0, 0)
    assert rr.parse_version("4.2") == (4, 2, 0)
    assert rr.parse_version("") is None
    assert rr.parse_version(None) is None
    assert rr.parse_version("garbage") is None


def test_cutover_boundary_4_0_0():
    # 3.99.0 → flutter；4.0.0 → native（边界归 native）
    r3 = rr.resolve("android", "3.99.0", ROUTING, path_exists=ALWAYS)
    assert r3.family == "flutter" and r3.logical_name == "plaud-android"
    r4 = rr.resolve("android", "4.0.0", ROUTING, path_exists=ALWAYS)
    assert r4.family == "native" and r4.logical_name == "plaud-native-android"
    assert r4.github_repo == "Plaud-AI/plaud-native-android"
    assert r4.symbol_profile == "native_android"
    assert r4.sub_repo_path == "/repos/plaud-native-app/plaud-native-android"
    assert r4.confidence == "high"


def test_version_missing_falls_back_to_newest_band_low_confidence():
    r = rr.resolve("ios", None, ROUTING, path_exists=ALWAYS)
    assert r.family == "native"          # 最新 band
    assert r.confidence == "low"


def test_web_single_band_no_subrepo():
    r = rr.resolve("web", "1.2.3", ROUTING, path_exists=ALWAYS)
    assert r.family == "web"
    assert r.sub_repo_path == "/repos/plaud-web"   # sub 为空 → wrapper 即代码根
    assert r.symbol_profile == "none"


def test_unconfigured_platform_returns_none():
    assert rr.resolve("desktop", "1.0.0", ROUTING, path_exists=ALWAYS) is None


def test_missing_path_returns_none():
    # 路径不存在 → None（降级）
    assert rr.resolve("android", "4.0.0", ROUTING, path_exists=lambda p: False) is None


def test_normalize_platform():
    assert rr.normalize_platform("app", os_name="Android") == "android"
    assert rr.normalize_platform("flutter", os_name="iOS") == "ios"
    assert rr.normalize_platform("ANDROID") == "android"
    assert rr.normalize_platform("web") == "web"
    assert rr.normalize_platform("") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_repo_router.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.services.repo_router'`）

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/repo_router.py
"""源码仓库路由 —— 单一真相源。

输入 (platform, version)，输出 RepoResolution（源码路径 / 子仓 / GitHub 仓 /
符号化 profile / family）。纯函数，零副作用（path_exists 可注入便于测试）。

配置形态见 config.yaml `repo_routing` 段。切换线：3.x=flutter，4.0.0 起=native。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("jarvis.repo_router")

_VER_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


@dataclass
class RepoResolution:
    family: str
    platform: str
    wrapper_path: str
    sub_repo_path: str
    logical_name: str
    github_repo: str
    symbol_profile: str
    confidence: str  # "high" | "low"


def parse_version(v: Optional[str]) -> Optional[tuple[int, int, int]]:
    """'3.16.0-634' → (3,16,0)；无法解析 → None。"""
    if not v:
        return None
    m = _VER_RE.match(str(v))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))


def normalize_platform(raw: str, os_name: str = "") -> Optional[str]:
    """把 jarvis 的 'app' / crashguard 的 'flutter' 按 os_name 细分到 android/ios；
    web/desktop/android/ios 原样小写。无法归一 → None。"""
    p = (raw or "").strip().lower()
    if p in ("android", "ios", "web", "desktop"):
        return p
    if p in ("app", "flutter"):
        o = (os_name or "").strip().lower()
        if "android" in o:
            return "android"
        if "ios" in o or "iphone" in o or "ipad" in o:
            return "ios"
        return None  # app/flutter 但拿不到 os → 调用方降级
    return None


def select_band(bands: list[dict], version: Optional[str]) -> Optional[tuple[dict, str]]:
    """按 min_version 降序取第一个 version >= min_version 的 band。
    version 缺失 → 最新 band（min_version 最大）+ confidence='low'。"""
    if not bands:
        return None
    ordered = sorted(bands, key=lambda b: parse_version(b.get("min_version", "0")) or (0, 0, 0), reverse=True)
    pv = parse_version(version)
    if pv is None:
        return ordered[0], "low"
    for b in ordered:
        mv = parse_version(b.get("min_version", "0")) or (0, 0, 0)
        if pv >= mv:
            return b, "high"
    return ordered[-1], "high"


def resolve(
    platform: str,
    version: Optional[str],
    routing: dict,
    *,
    sub_hint: str = "",
    stack_text: str = "",
    os_name: str = "",
    path_exists: Callable[[str], bool] = os.path.exists,
) -> Optional[RepoResolution]:
    norm = normalize_platform(platform, os_name=os_name)
    if not norm:
        logger.info("repo_router: cannot normalize platform=%r os=%r", platform, os_name)
        return None
    cfg = routing.get(norm)
    if not cfg or not cfg.get("bands"):
        logger.info("repo_router: platform %s not configured", norm)
        return None
    picked = select_band(cfg["bands"], version)
    if not picked:
        return None
    band, confidence = picked

    wrapper = os.path.expanduser(band.get("wrapper", "") or "")
    sub = (band.get("sub", "") or "").strip()
    if not wrapper or not path_exists(wrapper):
        logger.warning("repo_router: wrapper missing for %s: %s", norm, wrapper)
        return None
    if sub:
        sub_path = os.path.join(wrapper, sub)
        logical = sub
    else:
        sub_path = wrapper
        logical = os.path.basename(wrapper.rstrip("/"))
    if not path_exists(sub_path):
        logger.warning("repo_router: sub_repo missing for %s: %s", norm, sub_path)
        return None

    return RepoResolution(
        family=band.get("family", ""),
        platform=norm,
        wrapper_path=wrapper,
        sub_repo_path=sub_path,
        logical_name=logical,
        github_repo=band.get("github_repo", "") or "",
        symbol_profile=band.get("symbol_profile", "none") or "none",
        confidence=confidence,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_repo_router.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/repo_router.py backend/tests/test_repo_router.py
git commit -m "feat(repo-router): add version-aware repo resolution core + tests"
```

---

## Task 2: 配置加载 + backfill + `get_code_repo_for_platform` 降级

**Files:**
- Modify: `config.yaml`（新增 `repo_routing` 段 + `crashguard.datadog.service_filter` 注释样例）
- Modify: `backend/app/config.py`（`:438` 区域）
- Test: `backend/tests/test_repo_routing_config.py`

**Interfaces:**
- Consumes: Task 1 的 `repo_router`
- Produces:
  - `config.get_repo_routing() -> dict`（合并 yaml `repo_routing` + 旧 env backfill；返回 Task 1 期望的 routing dict）
  - `get_code_repo_for_platform(platform, version=None)`（降级为调 router，无 version 时取最新 band/flutter 回落）

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_repo_routing_config.py
from app import config

def test_get_repo_routing_has_android_ios_bands():
    routing = config.get_repo_routing()
    assert "android" in routing and "ios" in routing
    a = {b["family"] for b in routing["android"]["bands"]}
    assert {"flutter", "native"} <= a

def test_native_band_cutover_is_4():
    routing = config.get_repo_routing()
    native = [b for b in routing["android"]["bands"] if b["family"] == "native"][0]
    assert native["min_version"] == "4.0.0"
    assert native["github_repo"] == "Plaud-AI/plaud-native-android"
    assert native["symbol_profile"] == "native_android"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_repo_routing_config.py -v`
Expected: FAIL（`AttributeError: module 'app.config' has no attribute 'get_repo_routing'`）

- [ ] **Step 3a: 在 `config.yaml` 新增 `repo_routing` 段**

在 `config.yaml` 顶层（紧接现有「源码仓库路径」注释块后）加入：

```yaml
# ===========================
# 源码仓库路由（按平台 + 版本）—— repo_router 单一真相源
# 切换线：3.x.x 及以下=flutter 旧仓，4.0.0 起=native 新仓（android/ios 同线）
# 路径可被 env / 设置页 DB override 覆盖；服务器路径在 /Users/mac/Downloads/
# ===========================
repo_routing:
  android:
    bands:
      - {min_version: "0",     family: flutter, wrapper: "/Users/mac/Downloads/plaud_ai",        sub: "plaud-android",        github_repo: "Plaud-AI/Plaud-App",            symbol_profile: "flutter_android"}
      - {min_version: "4.0.0", family: native,  wrapper: "/Users/mac/Downloads/plaud-native-app", sub: "plaud-native-android", github_repo: "Plaud-AI/plaud-native-android", symbol_profile: "native_android"}
  ios:
    bands:
      - {min_version: "0",     family: flutter, wrapper: "/Users/mac/Downloads/plaud_ai",        sub: "plaud-ios",            github_repo: "Plaud-AI/Plaud-App",        symbol_profile: "flutter_ios"}
      - {min_version: "4.0.0", family: native,  wrapper: "/Users/mac/Downloads/plaud-native-app", sub: "plaud-native-ios",     github_repo: "Plaud-AI/plaud-native-ios",    symbol_profile: "native_ios"}
  web:
    bands:
      - {min_version: "0", family: web,     wrapper: "/Users/mac/Downloads/plaud-web", sub: "", github_repo: "Plaud-AI/plaud-web", symbol_profile: "none"}
  desktop:
    bands:
      - {min_version: "0", family: desktop, wrapper: "/Users/mac/Downloads/fe-nexus",  sub: "", github_repo: "Plaud-AI/fe-nexus",  symbol_profile: "none"}
```

- [ ] **Step 3b: 在 `backend/app/config.py` 加载 + backfill**

`Settings` 类已有 `code_repo_app/web/desktop/path`。新增一个字段承接 yaml `repo_routing`（紧邻 `code_repo_*` 字段，约 `:250` 后）：

```python
    repo_routing: dict = {}              # repo_router bands（yaml repo_routing 段）
```

在 `get_code_repo_for_platform` 上方新增（约 `:436`）：

```python
def get_repo_routing() -> dict:
    """返回 repo_router 用的 routing dict。
    优先 yaml `repo_routing`；其缺失的平台用旧 env (code_repo_app/web/desktop)
    backfill 出一个 flutter-family band（min_version "0"），保证现有部署不炸。"""
    s = get_settings()
    routing = dict(s.repo_routing or {})

    def _flutter_band(wrapper: str, sub: str, gh: str, prof: str) -> dict:
        return {"min_version": "0", "family": "flutter", "wrapper": wrapper,
                "sub": sub, "github_repo": gh, "symbol_profile": prof}

    app_repo = s.code_repo_app or s.code_repo_path
    if "android" not in routing and app_repo:
        routing["android"] = {"bands": [_flutter_band(app_repo, "plaud-android", "Plaud-AI/Plaud-App", "flutter_android")]}
    if "ios" not in routing and app_repo:
        routing["ios"] = {"bands": [_flutter_band(app_repo, "plaud-ios", "Plaud-AI/Plaud-App", "flutter_ios")]}
    if "web" not in routing and s.code_repo_web:
        routing["web"] = {"bands": [{"min_version": "0", "family": "web", "wrapper": s.code_repo_web, "sub": "", "github_repo": "Plaud-AI/plaud-web", "symbol_profile": "none"}]}
    if "desktop" not in routing and s.code_repo_desktop:
        routing["desktop"] = {"bands": [{"min_version": "0", "family": "desktop", "wrapper": s.code_repo_desktop, "sub": "", "github_repo": "Plaud-AI/fe-nexus", "symbol_profile": "none"}]}
    return routing
```

并把 `get_code_repo_for_platform` 改为降级 thin wrapper（保留旧签名 + 可选 version）：

```python
def get_code_repo_for_platform(platform: str, version: Optional[str] = None,
                               os_name: str = "") -> Optional[str]:
    """[DEPRECATED] 旧接口降级：调 repo_router。无 version → 取最新 band 回落。
    新代码请直接用 app.services.repo_router.resolve。"""
    from app.services import repo_router
    res = repo_router.resolve(platform, version, get_repo_routing(), os_name=os_name)
    if res:
        return res.sub_repo_path
    # 兜底：旧静态映射（platform 无法归一 / 未配置时）
    s = get_settings()
    return (s.code_repo_app or s.code_repo_path) or None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_repo_routing_config.py tests/test_repo_router.py -v`
Expected: PASS（全绿）

- [ ] **Step 5: Commit**

```bash
git add config.yaml backend/app/config.py backend/tests/test_repo_routing_config.py
git commit -m "feat(config): load repo_routing bands + backfill legacy env + deprecate get_code_repo_for_platform"
```

---

## Task 3: 源码出口接入（analysis_worker + eval_runner）

**Files:**
- Modify: `backend/app/workers/analysis_worker.py:19,581`
- Modify: `backend/app/services/eval_runner.py:13,227`

**Interfaces:**
- Consumes: `repo_router.resolve`、`config.get_repo_routing`、issue 上的 `app_version`
- Produces: 无新接口（行为变更）

- [ ] **Step 1: 改 `analysis_worker.py`**

`:19` 的 import 改为：

```python
from app.config import get_settings, get_repo_routing
from app.services import repo_router
```

`:581` 处：

```python
    # --- Step 6: Prepare workspace ---
    version = (getattr(issue, "app_version", "") or "").strip()
    res = repo_router.resolve(platform, version, get_repo_routing())
    code_repo = res.sub_repo_path if res else None
    if res is None:
        logger.info("repo_router: no repo for platform=%s version=%s — logs-only", platform, version)
    else:
        logger.info("repo_router: %s v%s → %s (%s, conf=%s)",
                    platform, version or "?", res.logical_name, res.family, res.confidence)
    engine.prepare_workspace(workspace, rules, workspace_log_paths, code_repo=code_repo)
```

- [ ] **Step 2: 改 `eval_runner.py`**

`:13` import：

```python
from app.config import get_repo_routing, get_settings
from app.services import repo_router
```

`:227`：

```python
                res = repo_router.resolve(
                    (issue.platform or "").strip().lower(),
                    (getattr(issue, "app_version", "") or "").strip(),
                    get_repo_routing(),
                )
                code_repo = res.sub_repo_path if res else None
                engine.prepare_workspace(workspace, rules, log_paths, code_repo=code_repo)
```

- [ ] **Step 3: 冒烟验证 import 无误**

Run: `cd backend && python -c "import app.workers.analysis_worker, app.services.eval_runner; print('ok')"`
Expected: 打印 `ok`，无 ImportError

- [ ] **Step 4: 跑既有相关测试不回归**

Run: `cd backend && python -m pytest tests/ -k "analysis or eval or worker" -q`
Expected: 不新增失败（无相关测试则 `no tests ran`，可接受）

- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/analysis_worker.py backend/app/services/eval_runner.py
git commit -m "feat(analysis): route source repo via repo_router (platform+version)"
```

---

## Task 4: repo_updater —— submodule 壳更新分支 + 每仓 lock + 从 routing 收集 wrappers

**Files:**
- Modify: `backend/app/services/repo_updater.py:18,61-130,133`
- Test: `backend/tests/test_repo_updater_submodule.py`

**Interfaces:**
- Consumes: `config.get_repo_routing`
- Produces: `_is_submodule_shell(path) -> bool`；`get_all_code_repos` 行为改为从 routing 收集 distinct wrapper

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_repo_updater_submodule.py
from pathlib import Path
from app.services import repo_updater as ru

def test_submodule_shell_detected(tmp_path: Path):
    # 顶层 .git + .gitmodules → submodule 壳
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitmodules").write_text('[submodule "x"]\n')
    assert ru._is_submodule_shell(tmp_path) is True

def test_plain_git_not_shell(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    assert ru._is_submodule_shell(tmp_path) is False

def test_mt_workspace_not_shell(tmp_path: Path):
    sub = tmp_path / "common"; sub.mkdir(); (sub / ".git").mkdir()
    assert ru._is_submodule_shell(tmp_path) is False
    assert ru._is_mt_workspace(tmp_path) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_repo_updater_submodule.py -v`
Expected: FAIL（`AttributeError: ... '_is_submodule_shell'`）

- [ ] **Step 3: 实现**

在 `repo_updater.py` 的 `_is_mt_workspace` 旁新增：

```python
def _is_submodule_shell(path: Path) -> bool:
    """有顶层 .git 且有 .gitmodules → git submodule 壳（plaud-native-app / fe-nexus）。
    需 recursive 更新；普通 git pull 不会动 submodule。"""
    return (path / ".git").exists() and (path / ".gitmodules").exists()
```

在 `_update_repo` 里，`_is_mt_workspace` 分支**之后、普通 git 分支之前**插入 submodule 壳分支（带每仓 lock）：

```python
    # submodule 壳分支（plaud-native-app / fe-nexus）：recursive 更新
    if _is_submodule_shell(path):
        try:
            with workspace_lock(path, timeout_sec=120):
                for cmd in (
                    ["git", "fetch", "origin"],
                    ["git", "checkout", "main"],
                    ["git", "pull", "origin"],
                    ["git", "submodule", "sync", "--recursive"],
                    ["git", "submodule", "update", "--init", "--remote", "--recursive"],
                ):
                    r = subprocess.run(cmd, cwd=str(path), capture_output=True, text=True, timeout=300)
                    if r.returncode != 0 and cmd[1] in ("submodule",):
                        logger.warning("[repo:%s] %s failed: %s", name, " ".join(cmd), r.stderr.strip())
                logger.info("[repo:%s] submodule shell updated at %s", name, repo_path)
                return True
        except TimeoutError:
            logger.warning("[repo:%s] workspace busy — skipping this tick", name)
            return False
        except Exception as e:
            logger.error("[repo:%s] submodule update failed: %s", name, e)
            return False
```

> 注：`workspace_lock` 已是 per-path 文件锁（`$path/.jarvis.lock`），天然每仓独立——只要每个 wrapper 各自 `workspace_lock(path)`，多壳就不互相阻塞。普通 git 单仓分支也用 `workspace_lock(path)` 包一层（防与未来并发冲突）。

把 `get_all_code_repos`（`config.py`）或 `update_all_repos` 改为从 routing 收集 distinct wrapper。在 `config.py` 的 `get_all_code_repos` 末尾改为：

```python
def get_all_code_repos() -> dict[str, str]:
    """所有需要定时更新的 distinct wrapper（含 flutter/native/web/desktop）。"""
    routing = get_repo_routing()
    repos: dict[str, str] = {}
    for platform, cfg in routing.items():
        for band in cfg.get("bands", []):
            w = os.path.expanduser(band.get("wrapper", "") or "")
            if w:
                repos[w] = w   # key=去重后的 wrapper 路径
    return repos
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_repo_updater_submodule.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/repo_updater.py backend/app/config.py backend/tests/test_repo_updater_submodule.py
git commit -m "feat(repo-updater): recursive submodule shell update + collect wrappers from routing"
```

---

## Task 5: crashguard PR 出口 —— router 选仓 + family 门控 + github_repo 目标

**Files:**
- Modify: `backend/app/crashguard/services/pr_drafter.py`
- Test: `backend/tests/crashguard/test_pr_drafter_routing.py`

**Interfaces:**
- Consumes: `repo_router.resolve`、`config.get_repo_routing`、issue 的 `representative_stack.sample_app_version` + `platform`
- Produces: `_resolve_repo_for_issue(platform, version) -> RepoResolution | None`（pr_drafter 内私有）

**前置（隔离合约）**：crashguard 调 `app.services.repo_router` 是新增耦合点——本 Task 必须先做 Task 7 的 importlinter/ADR 改动，否则 `lint-imports` 红。执行顺序见末尾「实施顺序」（Task 7 在 Task 5 前）。

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/crashguard/test_pr_drafter_routing.py
from app.crashguard.services import pr_drafter

def test_resolve_native_repo_for_v4(monkeypatch):
    routing = {"android": {"bands": [
        {"min_version": "0", "family": "flutter", "wrapper": "/r/plaud_ai", "sub": "plaud-android",
         "github_repo": "Plaud-AI/Plaud-App", "symbol_profile": "flutter_android"},
        {"min_version": "4.0.0", "family": "native", "wrapper": "/r/plaud-native-app", "sub": "plaud-native-android",
         "github_repo": "Plaud-AI/plaud-native-android", "symbol_profile": "native_android"},
    ]}}
    monkeypatch.setattr(pr_drafter, "get_repo_routing", lambda: routing)
    monkeypatch.setattr(pr_drafter.repo_router, "os", pr_drafter.repo_router.os)  # path_exists
    from app.services import repo_router as rr
    monkeypatch.setattr(rr.os.path, "exists", lambda p: True)
    res = pr_drafter._resolve_repo_for_issue("android", "4.1.0-720")
    assert res.family == "native"
    assert res.github_repo == "Plaud-AI/plaud-native-android"

def test_flutter_subrepo_detection_gated_to_flutter(monkeypatch):
    # native family 不应触发 global/cn blob 探测
    assert pr_drafter._should_run_flutter_subrepo_detection("native") is False
    assert pr_drafter._should_run_flutter_subrepo_detection("flutter") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/crashguard/test_pr_drafter_routing.py -v`
Expected: FAIL（`AttributeError: ... '_resolve_repo_for_issue'`）

- [ ] **Step 3: 实现**

在 `pr_drafter.py` 顶部 import 增加：

```python
from app.config import get_repo_routing
from app.services import repo_router
```

新增私有解析 + family 门控：

```python
def _resolve_repo_for_issue(platform: str, version: str):
    """crashguard PR 选仓：按 (platform, sample_app_version) 经 repo_router 路由。"""
    return repo_router.resolve(platform, version, get_repo_routing())


def _should_run_flutter_subrepo_detection(family: str) -> bool:
    """只有 flutter family 才跑 global/cn/common blob 探测；native/desktop 单 submodule 跳过。"""
    return (family or "").strip().lower() == "flutter"
```

改 `_resolve_candidate_repos`：在函数开头接收 `family`（由调用方传入 `res.family`），用 `_should_run_flutter_subrepo_detection(family)` 门控现有 `_detect_flutter_subrepo_*` 三段（native/desktop family 时 `flutter_hint = ""` 且不追加跨仓 flutter 候选）。当 `res` 已确定具体子仓时，候选列表直接用 `[(res.logical_name, res.sub_repo_path)]`，不再走 `_platform_repo_path` 旧静态映射。

PR 创建处（约 `:1626` 的 `repo_path = _platform_repo_path(platform)`）改为：

```python
        res = _resolve_repo_for_issue(platform, sample_app_version)
        if res is None:
            return {"ok": False, "error": f"repo_router: no repo for platform={platform} version={sample_app_version}"}
        repo_path = res.sub_repo_path
        github_slug = res.github_repo   # gh 命令统一用它，不再从 git remote 反解
```

把后续所有 `gh pr list/create/edit --repo <slug>` 的 slug 来源统一改为 `res.github_repo`（替换原从 git remote 推断的逻辑）。`sample_app_version` 取自 issue 的 `representative_stack` JSON：

```python
import json as _json
def _sample_version(issue) -> str:
    try:
        rep = _json.loads(getattr(issue, "representative_stack", "") or "{}")
        return (rep.get("sample_app_version") or "").strip()
    except Exception:
        return (getattr(issue, "app_version", "") or "").strip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/crashguard/test_pr_drafter_routing.py tests/crashguard/ -q`
Expected: PASS（新测试通过；既有 crashguard 测试不回归）

- [ ] **Step 5: Commit**

```bash
git add backend/app/crashguard/services/pr_drafter.py backend/tests/crashguard/test_pr_drafter_routing.py
git commit -m "feat(crashguard): route PR repo via repo_router; gate flutter subrepo detection by family"
```

---

## Task 6: 符号化 family 路由（github_symbols + symbolication + analyzer 调用点）

**Files:**
- Modify: `backend/app/crashguard/services/github_symbols.py:39` + 公共 getter 签名
- Modify: `backend/app/crashguard/services/symbolication.py:70,101`
- Modify: `backend/app/crashguard/services/analyzer.py`（符号化调用点）
- Test: `backend/tests/crashguard/test_symbol_profile.py`

**Interfaces:**
- Consumes: `repo_router`（取 `symbol_profile` + `github_repo`）
- Produces:
  - `github_symbols` 各 getter 增 `repo: str = _DEFAULT_REPO` 入参
  - `symbolicate_stack(stack, binary_images, platform, app_version="", *, symbol_profile="", github_repo="")`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/crashguard/test_symbol_profile.py
from app.crashguard.services import symbolication

def test_native_android_skips_dart_symbols(monkeypatch):
    calls = {"dart": 0, "mapping": 0}
    async def fake_dart(v, repo=""): calls["dart"] += 1; return None
    async def fake_mapping(v, repo=""): calls["mapping"] += 1; return None
    async def fake_native(v, repo=""): return None
    monkeypatch.setattr(symbolication, "get_dart_symbols_dir", fake_dart, raising=False)
    # native_android profile：不应调 dart 符号（Flutter 专属）
    prof = symbolication._profile_strategy("native_android")
    assert prof["use_dart_symbols"] is False
    assert prof["use_proguard"] is True

def test_native_ios_skips_flutter_dsym():
    prof = symbolication._profile_strategy("native_ios")
    assert prof["use_flutter_dsym"] is False
    assert prof["use_app_dsym"] is True

def test_flutter_android_uses_dart():
    prof = symbolication._profile_strategy("flutter_android")
    assert prof["use_dart_symbols"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/crashguard/test_symbol_profile.py -v`
Expected: FAIL（`AttributeError: ... '_profile_strategy'`）

- [ ] **Step 3: 实现**

`github_symbols.py`：把模块级 `_REPO = "Plaud-AI/Plaud-App"` 重命名为 `_DEFAULT_REPO`，并给 `find_release_tag` / `get_ios_dsyms_dir` / `get_android_mapping` / `get_android_native_symbols_dir` / `get_dart_symbols_dir` 各加 `repo: str = _DEFAULT_REPO` 入参，内部用 `repo` 替换写死的 `_REPO`（GitHub API URL 拼接处）。

`symbolication.py`：新增 profile 策略表 + 改签名：

```python
_SYMBOL_PROFILES = {
    "flutter_android": {"use_dart_symbols": True,  "use_proguard": True,  "use_native_so": True,  "use_flutter_dsym": True,  "use_app_dsym": False},
    "flutter_ios":     {"use_dart_symbols": True,  "use_proguard": False, "use_native_so": False, "use_flutter_dsym": True,  "use_app_dsym": False},
    "native_android":  {"use_dart_symbols": False, "use_proguard": True,  "use_native_so": True,  "use_flutter_dsym": False, "use_app_dsym": False},
    "native_ios":      {"use_dart_symbols": False, "use_proguard": False, "use_native_so": False, "use_flutter_dsym": False, "use_app_dsym": True},
    "none":            {"use_dart_symbols": False, "use_proguard": False, "use_native_so": False, "use_flutter_dsym": False, "use_app_dsym": False},
}

def _profile_strategy(symbol_profile: str) -> dict:
    return _SYMBOL_PROFILES.get((symbol_profile or "none").strip().lower(), _SYMBOL_PROFILES["none"])
```

`symbolicate_stack` 加 `*, symbol_profile="", github_repo=""` 两个 kw 参数；把 `_symbolicate_with_github` 改为接收 `strategy` + `github_repo`，按 strategy 决定调哪些 getter（native_android 不调 `get_dart_symbols_dir`；native_ios 不调 Flutter.dSYM 分支），所有 getter 传 `repo=github_repo or _DEFAULT_REPO`。`github_repo` 为空 → 回落 `_DEFAULT_REPO`（向后兼容 Flutter）。

`analyzer.py` 调用 `symbolicate_stack` 处：先解析 res，再传 profile：

```python
from app.config import get_repo_routing
from app.services import repo_router
res = repo_router.resolve(platform, sample_app_version, get_repo_routing())
stack = await symbolicate_stack(
    stack, binary_images, platform, app_version=sample_app_version,
    symbol_profile=(res.symbol_profile if res else ""),
    github_repo=(res.github_repo if res else ""),
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/crashguard/test_symbol_profile.py tests/crashguard/ -q`
Expected: PASS（新测试通过；既有 crashguard 测试不回归）

- [ ] **Step 5: Commit**

```bash
git add backend/app/crashguard/services/github_symbols.py backend/app/crashguard/services/symbolication.py backend/app/crashguard/services/analyzer.py backend/tests/crashguard/test_symbol_profile.py
git commit -m "feat(crashguard): route symbolication source repo + asset strategy by symbol_profile"
```

---

## Task 7: Datadog service filter 两代覆盖 + 隔离合约（ADR + importlinter）

**Files:**
- Modify: `backend/app/crashguard/config.py:137-140`
- Modify: `config.yaml`（`crashguard.datadog.service_filter` 样例）
- Modify: `backend/.importlinter`（白名单 `app.services.repo_router`）
- Modify: `docs/adr/0001-crashguard-isolation.md`（记录新增耦合点）

**Interfaces:** 无新接口（配置 + 合约）

> ⚠️ 本 Task 必须在 Task 5/6（crashguard 引入 repo_router import）**之前或同批**合入，否则 `lint-imports` / 启动自检红。

- [ ] **Step 1: 改 `crashguard/config.py` 默认 service filter**

`:140` 改为（注释保留，提示需 Datadog 实测确认真实 tag）：

```python
    # 共存期：Flutter + native 两代 service 全拉进同一池，落仓靠 repo_router (platform,version)
    # ⚠️ 上线前在 Datadog 实测确认原生真实 service tag（可能非 plaud-native-android）
    datadog_service_filter: str = "(service:plaud-flutter OR service:plaud-native-android OR service:plaud-native-ios)"
```

- [ ] **Step 2: 改 `backend/.importlinter` 白名单**

在 crashguard `forbidden_modules` 的 allowlist（允许从 crashguard import 的 jarvis 模块）中加入 `app.services.repo_router`。定位现有 `app.services.repo_updater` / `app.services.agent_orchestrator` 白名单条目，按同样格式追加一行 `app.services.repo_router`。

- [ ] **Step 3: 改 ADR-0001**

在 `docs/adr/0001-crashguard-isolation.md` 的「允许的对外耦合点」表新增一行：

```markdown
| `app.services.repo_router.resolve` | 按 (platform, version) 解析源码/PR/符号化目标仓（Flutter→native 版本切换）|
```

并在决策记录补一句：2026-06-26 因 native 迁移按版本路由仓库，新增 repo_router 为第 5 个允许耦合点。

- [ ] **Step 4: 跑隔离自检 + 单测**

Run: `cd backend && lint-imports && python -m scripts.check_crash_decoupling`
Expected: 两者均 PASS（无 forbidden import 违规）

- [ ] **Step 5: Commit**

```bash
git add backend/app/crashguard/config.py config.yaml backend/.importlinter docs/adr/0001-crashguard-isolation.md
git commit -m "feat(crashguard): cover flutter+native datadog services; allow repo_router coupling (ADR-0001)"
```

---

## Task 8: 设置页后端 —— repo_routing GET/PUT + DB override + 启动 apply

**Files:**
- Modify: `backend/app/api/settings.py`
- Modify: `backend/app/main.py`（lifespan 调 apply）
- Test: `backend/tests/test_settings_repo_routing.py`

**Interfaces:**
- Consumes: `db.get_oncall_config/set_oncall_config`、`config.get_repo_routing`、`repo_router.resolve`
- Produces:
  - `GET /api/settings/repo-routing` → `{routing, service_filter}`
  - `PUT /api/settings/repo-routing` → 写 DB override + merge 内存
  - `POST /api/settings/repo-routing/preview` → `{platform, version}` → 解析预览 + 路径校验
  - `apply_repo_routing_overrides_from_db()`（启动调用）

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_settings_repo_routing.py
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app

@pytest.mark.asyncio
async def test_preview_resolves(monkeypatch):
    from app.api import settings as st
    monkeypatch.setattr(st, "get_repo_routing", lambda: {"android": {"bands": [
        {"min_version": "4.0.0", "family": "native", "wrapper": "/tmp", "sub": "",
         "github_repo": "Plaud-AI/plaud-native-android", "symbol_profile": "native_android"}]}})
    from app.services import repo_router as rr
    monkeypatch.setattr(rr.os.path, "exists", lambda p: True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/settings/repo-routing/preview", json={"platform": "android", "version": "4.2.0"})
    assert r.status_code == 200
    assert r.json()["family"] == "native"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_settings_repo_routing.py -v`
Expected: FAIL（404 / route 不存在）

- [ ] **Step 3: 实现**

在 `settings.py` 加（仿 `AGENT_OVERRIDE_KEY` 模式）：

```python
REPO_ROUTING_OVERRIDE_KEY = "repo_routing_overrides"

class RepoRoutingUpdate(BaseModel):
    routing: dict
    service_filter: str | None = None

@router.get("/repo-routing")
async def get_repo_routing_cfg():
    from app.config import get_repo_routing
    from app.crashguard.config import get_crashguard_settings
    return {"routing": get_repo_routing(),
            "service_filter": get_crashguard_settings().datadog_service_filter}

@router.put("/repo-routing")
async def update_repo_routing(req: RepoRoutingUpdate):
    override = {"routing": req.routing}
    if req.service_filter is not None:
        override["service_filter"] = req.service_filter
    await db.set_oncall_config(REPO_ROUTING_OVERRIDE_KEY, json.dumps(override, ensure_ascii=False))
    _apply_repo_routing(override)   # 立即 merge 内存
    return {"ok": True}

class PreviewReq(BaseModel):
    platform: str
    version: str | None = None

@router.post("/repo-routing/preview")
async def preview_repo_routing(req: PreviewReq):
    from app.config import get_repo_routing
    from app.services import repo_router
    res = repo_router.resolve(req.platform, req.version, get_repo_routing())
    if not res:
        return {"resolved": False, "reason": "platform 未配置 / 路径不存在 / 版本无法归一"}
    return {"resolved": True, "family": res.family, "platform": res.platform,
            "sub_repo_path": res.sub_repo_path, "github_repo": res.github_repo,
            "symbol_profile": res.symbol_profile, "confidence": res.confidence}

def _apply_repo_routing(override: dict) -> None:
    """把 override 写回内存 Settings.repo_routing + crashguard service_filter。"""
    from app.config import get_settings
    s = get_settings()
    if override.get("routing"):
        s.repo_routing = override["routing"]
    if override.get("service_filter"):
        from app.crashguard.config import get_crashguard_settings
        get_crashguard_settings().datadog_service_filter = override["service_filter"]

async def apply_repo_routing_overrides_from_db() -> dict:
    raw = await db.get_oncall_config(REPO_ROUTING_OVERRIDE_KEY, "")
    if not raw:
        return {}
    override = json.loads(raw)
    _apply_repo_routing(override)
    return override
```

> 路径校验：preview 已隐含校验（`resolve` 内 `path_exists` 不过 → `resolved: False`）。`PUT` 不强制路径存在（允许先配后 clone），但响应里回带 preview 结果供前端提示。

`main.py` lifespan 在 `apply_agent_overrides_from_db()` 旁加：

```python
    from app.api.settings import apply_repo_routing_overrides_from_db
    await apply_repo_routing_overrides_from_db()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_settings_repo_routing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/settings.py backend/app/main.py backend/tests/test_settings_repo_routing.py
git commit -m "feat(settings): repo-routing GET/PUT/preview + DB override + startup apply"
```

---

## Task 9: 设置页前端 ——「源码仓库路由」卡片

**Files:**
- Modify: `frontend/src/app/settings/page.tsx`（或现有设置页组件；先 Grep 定位）
- Modify: `frontend/src/lib/api.ts`（加 repo-routing 调用）

**Interfaces:**
- Consumes: `GET/PUT /api/settings/repo-routing`、`POST /api/settings/repo-routing/preview`
- Produces: UI 组件（无下游消费）

- [ ] **Step 1: 定位设置页结构**

Run: `cd frontend && grep -rln "settings" src/app/settings 2>/dev/null; grep -n "api\." src/lib/api.ts | head`
Expected: 找到设置页主组件 + api.ts 模式

- [ ] **Step 2: api.ts 加方法**

```typescript
// frontend/src/lib/api.ts
export const getRepoRouting = () => api.get('/api/settings/repo-routing');
export const updateRepoRouting = (body: { routing: any; service_filter?: string }) =>
  api.put('/api/settings/repo-routing', body);
export const previewRepoRouting = (platform: string, version: string) =>
  api.post('/api/settings/repo-routing/preview', { platform, version });
```

- [ ] **Step 3: 加「源码仓库路由」卡片组件**

在设置页新增一张卡片（沿用页面现有卡片/表格样式，勿自创风格）：
- 每平台（android/ios/web/desktop）一组 bands 表格行：`min_version`、`family`、`wrapper`、`sub`、`github_repo`、`symbol_profile`，可增删行。
- 顶部一个「解析预览」小工具：输入 platform 下拉 + version 文本框 → 调 `previewRepoRouting` → 显示命中 `family / sub_repo_path / github_repo / symbol_profile / confidence`，未命中显示 reason（红色）。
- `service_filter` 文本框（带「需 Datadog 实测确认」灰字提示）。
- 「保存」按钮 → `updateRepoRouting`，成功 toast。
- i18n：新增文案走项目现有 i18n 机制（先看 `frontend/CLAUDE.md` 的 i18n 约定，勿硬编码中文）。

- [ ] **Step 4: 构建验证**

Run: `cd frontend && npm run build`
Expected: 构建通过，无 type error

- [ ] **Step 5: Commit**

```bash
git add frontend/src/app/settings frontend/src/lib/api.ts frontend/src/i18n 2>/dev/null
git commit -m "feat(frontend): repo-routing settings card with bands editor + resolution preview"
```

---

## Task 10: 可观测 —— 解析 audit 日志

**Files:**
- Modify: `backend/app/services/repo_router.py`（resolve 成功/降级统一结构化日志）

**Interfaces:** 无新接口

- [ ] **Step 1: 在 `resolve` 返回前补结构化日志**

`resolve` 成功 return 前加：

```python
    logger.info("repo_router.resolved platform=%s version=%s family=%s repo=%s sub=%s symbol_profile=%s confidence=%s",
                norm, version or "?", band.get("family"), band.get("github_repo"), logical,
                band.get("symbol_profile"), confidence)
```

（降级 None 的各分支已有 logger.info/warning，确认覆盖：platform 无法归一 / 未配置 / wrapper 缺失 / sub 缺失。）

- [ ] **Step 2: 验证日志不破坏既有测试**

Run: `cd backend && python -m pytest tests/test_repo_router.py -v`
Expected: PASS（8 passed）

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/repo_router.py
git commit -m "feat(repo-router): structured audit log on resolve/fallback"
```

---

## 实施顺序（依赖）

1. **Task 1**（router 核心，地基）
2. **Task 2**（配置 + backfill）
3. **Task 3**（源码出口）—— 此时工单分析源码路由已端到端可用
4. **Task 4**（repo_updater，让新仓可更新）
5. **Task 7**（隔离合约 + service filter）—— **必须先于 Task 5/6**（crashguard 引入 repo_router import）
6. **Task 5**（PR 出口）
7. **Task 6**（符号化）—— 此时 crashguard 端到端可用
8. **Task 8**（设置页后端）
9. **Task 9**（设置页前端）
10. **Task 10**（可观测）

> ⚠️ 上线前外部待办（非本计划代码范围，需协调）：① Datadog 实测原生真实 service tag；② 向 App 团队确认原生 release 仓 + 符号资产命名（native android mapping/.so、native ios dSYM 包名）；③ 102/100 服务器 `git clone --recursive` 三个新仓到 `/Users/mac/Downloads/` + 磁盘评估。

## Self-Review 记录

- **Spec coverage**：§1 数据模型→Task 1/2；§1 Datadog→Task 7；§2 router→Task 1；§3 四出口→Task 3(源码)/5(PR)/6(符号化)/7(Datadog)；§4 配置+UI→Task 2/8/9；§5 边界降级→Task 1(None 降级)+各出口判空；§6 backfill→Task 2、submodule 壳→Task 4、隔离合约→Task 7、provision→实施顺序末尾外部待办；§7 测试→各 Task 内置。
- **Placeholder scan**：无 TBD/TODO；外部待办明确标注「非本计划代码范围」。
- **Type consistency**：`RepoResolution` 字段在 Task 1 定义，Task 3/5/6/8 引用一致（`sub_repo_path`/`github_repo`/`symbol_profile`/`family`/`confidence`）；`resolve(platform, version, routing, *, ...)` 签名跨 Task 一致；`get_repo_routing()` 在 Task 2 定义，Task 3/4/5/6/8 引用一致。
