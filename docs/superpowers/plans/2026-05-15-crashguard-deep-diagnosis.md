# Crashguard 深度诊断系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 crashguard auto-PR pipeline 基础上，新增两阶段深度诊断系统：Phase 1（AI 主动调取工具、输出多假设诊断报告）→ 人工确认假设 → Phase 2（生成高质量修复 PR）。

**Architecture:** 新增 `deep_analyzer.py` 实现 Phase 1；在现有 `crash_analyses` 表追加 7 列区分诊断/修复两阶段；workspace 内置 5 个可被 AI agent 调用的调查工具脚本；前端 issue 详情面板新增深度诊断区块。Phase 2 复用现有 `analyzer.py` + `pr_drafter.py` + 14 道质量闸，仅追加"已确认假设"上下文。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy async / SQLite / Next.js 15 / React 19 / Tailwind CSS 4 / Claude Code CLI agent

**Spec:** `docs/superpowers/specs/2026-05-15-crashguard-deep-diagnosis-design.md`

---

## File Map

### Backend — New Files
- `backend/app/crashguard/services/crash_type_classifier.py` — crash_type 判定纯函数
- `backend/app/crashguard/services/diagnosis_tools/__init__.py` — 包声明
- `backend/app/crashguard/services/diagnosis_tools/datadog_query.py` — Datadog DQL 查询
- `backend/app/crashguard/services/diagnosis_tools/git_blame.py` — git blame 单行
- `backend/app/crashguard/services/diagnosis_tools/git_pickaxe.py` — git log -S 引入时机
- `backend/app/crashguard/services/diagnosis_tools/find_similar.py` — 历史相似 crash 查询
- `backend/app/crashguard/services/diagnosis_tools/get_session.py` — RUM session 事件流
- `backend/app/crashguard/services/deep_analyzer.py` — Phase 1 深度诊断主服务

### Backend — Modified Files
- `backend/app/crashguard/models.py` — CrashAnalysis 追加 7 列
- `backend/app/crashguard/migrations.py` — `_REQUIRED_COLUMNS` 追加 7 条
- `backend/app/crashguard/config.py` — 追加 4 个 deep_analysis 配置项
- `backend/app/crashguard/api/crash.py` — 3 个新端点
- `backend/app/crashguard/services/analyzer.py` — Phase 2 接受 confirmed_hypothesis 注入

### Tests — New Files
- `backend/tests/crashguard/test_crash_type_classifier.py`
- `backend/tests/crashguard/test_deep_analyzer_parse.py`
- `backend/tests/crashguard/test_diagnosis_tools_unit.py`

### Frontend — Modified Files
- `frontend/src/lib/api.ts` — 新增 3 个 API wrapper + 类型
- `frontend/src/app/crashguard/page.tsx` — IssueDetailPanel 追加深度诊断区块

---

## Task 1: DB Model — 新增 7 列到 CrashAnalysis

**Files:**
- Modify: `backend/app/crashguard/models.py` (CrashAnalysis class 末尾)
- Modify: `backend/app/crashguard/migrations.py` (_REQUIRED_COLUMNS 末尾)
- Test: `backend/tests/crashguard/test_deep_analyzer_parse.py` (仅检查列存在)

- [ ] **Step 1: 在 models.py 的 CrashAnalysis 末尾追加 7 列**

打开 `backend/app/crashguard/models.py`，在 `CrashAnalysis` 类的 `created_at` 列之前插入：

```python
    # Phase 1 深度诊断专用列（phase="diagnosis"）
    phase = Column(String(16), default="fix")               # "diagnosis" | "fix"
    crash_type = Column(String(16), default="")             # crash|anr|freeze|oom|native_crash
    hypotheses = Column(Text, default="[]")                  # JSON: List[Hypothesis]
    data_gaps = Column(Text, default="[]")                   # JSON: List[DataGap]
    confirmed_hypothesis_id = Column(String(16), default="")
    investigation_log = Column(Text, default="[]")           # JSON: List[str]，AI 调查步骤
    parent_diagnosis_run_id = Column(String(64), default="") # Phase2 行 → Phase1 run_id
```

- [ ] **Step 2: 在 migrations.py 的 _REQUIRED_COLUMNS 末尾追加 7 条**

```python
    # Phase 1 深度诊断列
    ("crash_analyses", "phase", "VARCHAR(16)", "'fix'"),
    ("crash_analyses", "crash_type", "VARCHAR(16)", "''"),
    ("crash_analyses", "hypotheses", "TEXT", "'[]'"),
    ("crash_analyses", "data_gaps", "TEXT", "'[]'"),
    ("crash_analyses", "confirmed_hypothesis_id", "VARCHAR(16)", "''"),
    ("crash_analyses", "investigation_log", "TEXT", "'[]'"),
    ("crash_analyses", "parent_diagnosis_run_id", "VARCHAR(64)", "''"),
```

- [ ] **Step 3: 写迁移测试（验证新列可以被写入读出）**

创建 `backend/tests/crashguard/test_deep_analyzer_parse.py`：

```python
"""Unit tests for deep_analyzer Phase 1 parsing utilities."""
from __future__ import annotations
import json
import pytest


def test_diagnosis_json_schema():
    """diagnosis.json 输出结构必须包含 hypotheses + data_gaps + crash_type."""
    raw = {
        "crash_type": "anr",
        "investigation_log": ["读了 foo.dart"],
        "hypotheses": [
            {
                "id": "h1",
                "title": "主线程 IO 阻塞",
                "evidence": ["堆栈第3帧"],
                "confidence": 0.85,
                "fix_direction": "移到 isolate",
                "code_pointers": ["lib/foo.dart:42"],
                "can_fix_now": True,
                "complexity": "simple",
            }
        ],
        "data_gaps": [],
        "overall_confidence": 0.85,
        "recommended_hypothesis": "h1",
        "auto_proceed_to_fix": False,
    }
    # all required keys present
    for key in ("crash_type", "hypotheses", "data_gaps", "recommended_hypothesis",
                "auto_proceed_to_fix", "overall_confidence"):
        assert key in raw

    hyp = raw["hypotheses"][0]
    for key in ("id", "title", "evidence", "confidence", "fix_direction",
                "can_fix_now", "complexity"):
        assert key in hyp


def test_auto_proceed_conditions():
    """auto_proceed_to_fix=True 当且仅当单假设 confidence>=0.9 + can_fix_now + no data_gaps."""
    from app.crashguard.services.deep_analyzer import _should_auto_proceed

    # all conditions met
    assert _should_auto_proceed(
        hypotheses=[{"id": "h1", "confidence": 0.92, "can_fix_now": True}],
        data_gaps=[],
        threshold=0.9,
    ) is True

    # multiple hypotheses
    assert _should_auto_proceed(
        hypotheses=[
            {"id": "h1", "confidence": 0.95, "can_fix_now": True},
            {"id": "h2", "confidence": 0.80, "can_fix_now": True},
        ],
        data_gaps=[],
        threshold=0.9,
    ) is False

    # confidence below threshold
    assert _should_auto_proceed(
        hypotheses=[{"id": "h1", "confidence": 0.85, "can_fix_now": True}],
        data_gaps=[],
        threshold=0.9,
    ) is False

    # has data_gaps
    assert _should_auto_proceed(
        hypotheses=[{"id": "h1", "confidence": 0.95, "can_fix_now": True}],
        data_gaps=[{"description": "缺数据"}],
        threshold=0.9,
    ) is False
```

- [ ] **Step 4: 运行测试（预期 FAIL，因为 deep_analyzer 还不存在）**

```bash
cd /Users/sanato/Desktop/code/newplaud/jarvis/backend
source .venv/bin/activate
pytest tests/crashguard/test_deep_analyzer_parse.py::test_diagnosis_json_schema -v
```

Expected: `PASSED`（第一个测试不依赖 import，应通过）

```bash
pytest tests/crashguard/test_deep_analyzer_parse.py::test_auto_proceed_conditions -v
```

Expected: `FAILED` with `ModuleNotFoundError: No module named 'app.crashguard.services.deep_analyzer'`

- [ ] **Step 5: Commit**

```bash
git add backend/app/crashguard/models.py \
        backend/app/crashguard/migrations.py \
        backend/tests/crashguard/test_deep_analyzer_parse.py
git commit -m "feat(crashguard): Phase 1 诊断 — DB 模型新增 7 列 + 测试骨架"
```

---

## Task 2: Config — 新增 4 个 deep_analysis 配置项

**Files:**
- Modify: `backend/app/crashguard/config.py`

- [ ] **Step 1: 在 CrashguardSettings 类末尾（`gate_draft_pollution_min_age_hours` 之后）追加**

```python
    # === Phase 1 深度诊断 ===
    deep_analysis_enabled: bool = True
    deep_analysis_timeout_seconds: int = 1800          # 30 分钟，可调
    deep_analysis_dedup_hours: int = 6                 # 6h 内不重复跑
    deep_analysis_auto_proceed_threshold: float = 0.9  # 快车道置信度门槛
```

- [ ] **Step 2: 在 `_merge_crashguard_yaml` 函数的 flat 字典映射段，追加解析逻辑**

在 `get_crashguard_settings_from_yaml` 或对应 flat 映射处（搜索 `"analysis_dedup_hours"` 附近）加：

```python
    if "deep_analysis_enabled" in cfg:
        flat["deep_analysis_enabled"] = bool(cfg["deep_analysis_enabled"])
    if "deep_analysis_timeout_seconds" in cfg:
        flat["deep_analysis_timeout_seconds"] = int(cfg["deep_analysis_timeout_seconds"])
    if "deep_analysis_dedup_hours" in cfg:
        flat["deep_analysis_dedup_hours"] = int(cfg["deep_analysis_dedup_hours"])
    if "deep_analysis_auto_proceed_threshold" in cfg:
        flat["deep_analysis_auto_proceed_threshold"] = float(
            cfg["deep_analysis_auto_proceed_threshold"]
        )
```

- [ ] **Step 3: 验证 config 可以正常实例化（无报错）**

```bash
cd /Users/sanato/Desktop/code/newplaud/jarvis/backend
source .venv/bin/activate
python3 -c "
from app.crashguard.config import get_crashguard_settings
s = get_crashguard_settings()
print('deep_analysis_enabled:', s.deep_analysis_enabled)
print('deep_analysis_timeout_seconds:', s.deep_analysis_timeout_seconds)
print('deep_analysis_auto_proceed_threshold:', s.deep_analysis_auto_proceed_threshold)
"
```

Expected output:
```
deep_analysis_enabled: True
deep_analysis_timeout_seconds: 1800
deep_analysis_auto_proceed_threshold: 0.9
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/crashguard/config.py
git commit -m "feat(crashguard): Phase 1 诊断 — 新增 deep_analysis 配置项"
```

---

## Task 3: crash_type_classifier.py — crash 类型预判纯函数

**Files:**
- Create: `backend/app/crashguard/services/crash_type_classifier.py`
- Create: `backend/tests/crashguard/test_crash_type_classifier.py`

- [ ] **Step 1: 写测试**

创建 `backend/tests/crashguard/test_crash_type_classifier.py`：

```python
"""Tests for crash_type_classifier."""
from __future__ import annotations
import pytest
from app.crashguard.services.crash_type_classifier import classify_crash_type


def test_anr_from_title():
    assert classify_crash_type("ANR in ai.plaud.android", "", {}) == "anr"
    assert classify_crash_type("Application Not Responding - MainActivity", "", {}) == "anr"


def test_anr_from_stack():
    stack = "android.os.Process.sendSignal\nandroid.os.Process.killProcess"
    assert classify_crash_type("Some crash", stack, {}) == "anr"


def test_freeze_from_title():
    assert classify_crash_type("App freeze detected", "", {}) == "freeze"
    assert classify_crash_type("卡顿 60s on HomeScreen", "", {}) == "freeze"
    assert classify_crash_type("Watchdog terminated app", "", {}) == "freeze"


def test_oom_from_title():
    assert classify_crash_type("OutOfMemoryError in bitmap", "", {}) == "oom"
    assert classify_crash_type("OOM crash on image load", "", {}) == "oom"


def test_native_crash_from_stack():
    stack = "SIGSEGV at 0x0000dead\n  #00 flutter::dart::..."
    assert classify_crash_type("Fatal signal", stack, {}) == "native_crash"
    stack2 = "EXC_BAD_ACCESS (SIGSEGV)"
    assert classify_crash_type("crash", stack2, {}) == "native_crash"


def test_default_crash():
    assert classify_crash_type("NullPointerException in foo", "at com.foo.Bar.baz(Bar.java:12)", {}) == "crash"


def test_anr_beats_default():
    """ANR title + normal stack → still anr."""
    assert classify_crash_type("ANR in Service", "java.lang.Thread.sleep", {}) == "anr"
```

- [ ] **Step 2: 运行测试（预期全 FAIL）**

```bash
pytest tests/crashguard/test_crash_type_classifier.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.crashguard.services.crash_type_classifier'`

- [ ] **Step 3: 实现 crash_type_classifier.py**

创建 `backend/app/crashguard/services/crash_type_classifier.py`：

```python
"""崩溃类型预分类 — 纯函数，无 IO。
在 deep_analyzer 调用 agent 之前预判 crash_type，注入 prompt 指引专项调查路径。
"""
from __future__ import annotations
import re
from typing import Dict

_ANR_TITLE_RE = re.compile(
    r"\bANR\b|Application Not Responding|appNotResponding", re.IGNORECASE
)
_ANR_STACK_RE = re.compile(
    r"android\.app\.ActivityManagerNative|android\.os\.Process\.(sendSignal|killProcess)"
    r"|ActivityThread\.handleBindApplication|ANRError",
    re.IGNORECASE,
)
_FREEZE_RE = re.compile(
    r"\bfreeze\b|卡顿|hang\b|Watchdog|WatchDog|CADisplayLink|runloop.*stall",
    re.IGNORECASE,
)
_OOM_RE = re.compile(
    r"\bOOM\b|OutOfMemory|out.of.memory|low.memory|MemoryError", re.IGNORECASE
)
_NATIVE_STACK_RE = re.compile(
    r"SIGSEGV|SIGABRT|SIGBUS|EXC_BAD_ACCESS|EXC_CRASH|fatal signal",
    re.IGNORECASE,
)


def classify_crash_type(title: str, stack: str, tags: Dict) -> str:
    """返回 anr | freeze | oom | native_crash | crash。

    优先级：anr > freeze > oom > native_crash > crash（默认）。
    title 和 stack 都检查，title 权重略高（先检查）。
    """
    text_title = title or ""
    text_stack = stack or ""

    if _ANR_TITLE_RE.search(text_title) or _ANR_STACK_RE.search(text_stack):
        return "anr"
    if _FREEZE_RE.search(text_title) or _FREEZE_RE.search(text_stack):
        return "freeze"
    if _OOM_RE.search(text_title) or _OOM_RE.search(text_stack):
        return "oom"
    if _NATIVE_STACK_RE.search(text_title) or _NATIVE_STACK_RE.search(text_stack):
        return "native_crash"
    return "crash"
```

- [ ] **Step 4: 运行测试（预期全 PASS）**

```bash
pytest tests/crashguard/test_crash_type_classifier.py -v
```

Expected:
```
PASSED tests/crashguard/test_crash_type_classifier.py::test_anr_from_title
PASSED tests/crashguard/test_crash_type_classifier.py::test_anr_from_stack
PASSED tests/crashguard/test_crash_type_classifier.py::test_freeze_from_title
PASSED tests/crashguard/test_crash_type_classifier.py::test_oom_from_title
PASSED tests/crashguard/test_crash_type_classifier.py::test_native_crash_from_stack
PASSED tests/crashguard/test_crash_type_classifier.py::test_default_crash
PASSED tests/crashguard/test_crash_type_classifier.py::test_anr_beats_default
7 passed
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/crashguard/services/crash_type_classifier.py \
        backend/tests/crashguard/test_crash_type_classifier.py
git commit -m "feat(crashguard): crash_type_classifier — ANR/freeze/OOM/native 分类纯函数"
```

---

## Task 4: diagnosis_tools/ — 5 个 Agent 可调用的调查工具脚本

**Files:**
- Create: `backend/app/crashguard/services/diagnosis_tools/__init__.py`
- Create: `backend/app/crashguard/services/diagnosis_tools/datadog_query.py`
- Create: `backend/app/crashguard/services/diagnosis_tools/git_blame.py`
- Create: `backend/app/crashguard/services/diagnosis_tools/git_pickaxe.py`
- Create: `backend/app/crashguard/services/diagnosis_tools/find_similar.py`
- Create: `backend/app/crashguard/services/diagnosis_tools/get_session.py`
- Create: `backend/tests/crashguard/test_diagnosis_tools_unit.py`

- [ ] **Step 1: 写工具单元测试（测试 CLI 接口 + 输出 JSON）**

创建 `backend/tests/crashguard/test_diagnosis_tools_unit.py`：

```python
"""Unit tests for diagnosis_tools CLI scripts — 仅测 argparse + JSON 输出格式，不调真实 API。"""
from __future__ import annotations
import json
import subprocess
import sys
import os
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent.parent / "app" / "crashguard" / "services" / "diagnosis_tools"


def _run_tool(script: str, args: list[str], env_override: dict = None) -> dict:
    env = {**os.environ, **(env_override or {})}
    r = subprocess.run(
        [sys.executable, str(TOOLS_DIR / script)] + args,
        capture_output=True, text=True, timeout=10, env=env,
    )
    return json.loads(r.stdout)


def test_git_blame_missing_args():
    """git_blame.py 缺少 --file 时返回 error JSON。"""
    r = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "git_blame.py")],
        capture_output=True, text=True, timeout=5,
    )
    assert r.returncode != 0  # argparse error


def test_git_pickaxe_no_repo(tmp_path):
    """git_pickaxe.py 在不存在的 repo 路径时输出 error JSON。"""
    result = _run_tool("git_pickaxe.py", [
        "--keyword", "readFile",
        "--repo-path", str(tmp_path / "nonexistent"),
    ])
    assert "error" in result


def test_find_similar_no_db():
    """find_similar.py 在无 DB 配置时返回 error JSON（不 crash）。"""
    result = _run_tool(
        "find_similar.py",
        ["--fingerprint", "abc123"],
        env_override={"DATABASE_URL": ""},
    )
    # 应返回 JSON，可能是 error 或 empty results
    assert isinstance(result, dict)


def test_datadog_query_no_key():
    """datadog_query.py 无 API key 时返回 error JSON，不 crash。"""
    result = _run_tool(
        "datadog_query.py",
        ["--dql", "SELECT * FROM rum_events LIMIT 1"],
        env_override={"CRASHGUARD_DATADOG_API_KEY": ""},
    )
    assert "error" in result


def test_get_session_no_key():
    """get_session.py 无 API key 时返回 error JSON，不 crash。"""
    result = _run_tool(
        "get_session.py",
        ["--session-id", "fakesession123"],
        env_override={"CRASHGUARD_DATADOG_API_KEY": ""},
    )
    assert "error" in result
```

- [ ] **Step 2: 运行测试（预期 FAIL — 文件不存在）**

```bash
pytest tests/crashguard/test_diagnosis_tools_unit.py -v
```

Expected: 所有 test FAIL with FileNotFoundError 或 ModuleNotFoundError

- [ ] **Step 3: 创建包声明**

创建 `backend/app/crashguard/services/diagnosis_tools/__init__.py`（空文件）：

```python
"""Diagnosis tool scripts — standalone Python CLIs called by the deep analysis agent via Bash."""
```

- [ ] **Step 4: 创建 datadog_query.py**

创建 `backend/app/crashguard/services/diagnosis_tools/datadog_query.py`：

```python
#!/usr/bin/env python3
"""Datadog RUM 事件查询工具，供 AI agent 通过 Bash 调用。

用法: python tools/datadog_query.py --dql "<DQL 语句>" [--limit 50]
输出: JSON（事件列表或 error）
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dql", required=True, help="Datadog search query")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    api_key = os.environ.get("CRASHGUARD_DATADOG_API_KEY", "")
    app_key = os.environ.get("CRASHGUARD_DATADOG_APP_KEY", "")
    site = os.environ.get("CRASHGUARD_DATADOG_SITE", "datadoghq.com")

    if not api_key:
        print(json.dumps({"error": "CRASHGUARD_DATADOG_API_KEY not set"}))
        return

    url = f"https://api.{site}/api/v2/rum/events/search"
    payload = json.dumps({
        "filter": {"query": args.dql},
        "page": {"limit": min(args.limit, 100)},
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("DD-API-KEY", api_key)
    req.add_header("DD-APPLICATION-KEY", app_key or api_key)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            events = data.get("data", [])
            print(json.dumps({
                "count": len(events),
                "events": events[:args.limit],
            }, ensure_ascii=False))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        print(json.dumps({"error": f"HTTP {e.code}", "detail": body}))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 创建 git_blame.py**

创建 `backend/app/crashguard/services/diagnosis_tools/git_blame.py`：

```python
#!/usr/bin/env python3
"""git blame 单行工具，供 AI agent 查询代码行的提交历史。

用法: python tools/git_blame.py --file <相对路径> --line <行号> [--repo-path <绝对路径>]
输出: JSON {commit, author, date, summary, line_content}
"""
from __future__ import annotations
import argparse
import json
import subprocess
import os


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="相对 repo 根的文件路径")
    parser.add_argument("--line", type=int, required=True, help="行号（1-based）")
    parser.add_argument("--repo-path", default=".", help="git repo 根目录")
    args = parser.parse_args()

    cwd = os.path.abspath(args.repo_path)
    try:
        r = subprocess.run(
            ["git", "blame", "-L", f"{args.line},{args.line}", "--porcelain", args.file],
            cwd=cwd, capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            print(json.dumps({"error": r.stderr.strip()[:300]}))
            return
        lines = r.stdout.splitlines()
        if not lines:
            print(json.dumps({"error": "no output from git blame"}))
            return
        commit = lines[0].split()[0] if lines else ""
        info: dict = {"commit": commit, "author": "", "date": "", "summary": "", "line_content": ""}
        for ln in lines[1:]:
            if ln.startswith("author "):
                info["author"] = ln[7:].strip()
            elif ln.startswith("author-time "):
                import datetime
                info["date"] = datetime.datetime.fromtimestamp(
                    int(ln[12:].strip())
                ).strftime("%Y-%m-%d")
            elif ln.startswith("summary "):
                info["summary"] = ln[8:].strip()
            elif ln.startswith("\t"):
                info["line_content"] = ln[1:]
        print(json.dumps(info, ensure_ascii=False))
    except FileNotFoundError:
        print(json.dumps({"error": "git not found in PATH"}))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: 创建 git_pickaxe.py**

创建 `backend/app/crashguard/services/diagnosis_tools/git_pickaxe.py`：

```python
#!/usr/bin/env python3
"""git log -S 搜索关键词引入时机，供 AI agent 查明"是谁/何时引入了这段代码"。

用法: python tools/git_pickaxe.py --keyword <字符串> [--repo-path <路径>] [--limit 20]
输出: JSON {commits: [{hash, author, date, subject}]}
"""
from __future__ import annotations
import argparse
import json
import subprocess
import os


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--repo-path", default=".")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    cwd = os.path.abspath(args.repo_path)
    try:
        r = subprocess.run(
            [
                "git", "log", f"-S{args.keyword}",
                f"--max-count={args.limit}",
                "--pretty=format:%H|%an|%ad|%s",
                "--date=short",
            ],
            cwd=cwd, capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            print(json.dumps({"error": r.stderr.strip()[:300]}))
            return
        commits = []
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({
                    "hash": parts[0][:8],
                    "author": parts[1],
                    "date": parts[2],
                    "subject": parts[3],
                })
        print(json.dumps({"keyword": args.keyword, "commits": commits}, ensure_ascii=False))
    except FileNotFoundError:
        print(json.dumps({"error": "git not found in PATH"}))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: 创建 find_similar.py**

创建 `backend/app/crashguard/services/diagnosis_tools/find_similar.py`：

```python
#!/usr/bin/env python3
"""查询历史相似 crash 的根因分析和修复方案，供 AI agent 复用经验。

用法: python tools/find_similar.py --fingerprint <sha1> [--limit 5]
输出: JSON {results: [{datadog_issue_id, root_cause, fix_suggestion, fix_diff, confidence, created_at}]}

依赖：DATABASE_URL 环境变量（或 WORKSPACE_DIR 派生出的默认 SQLite 路径）。
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    # 解析 DB 路径
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url.startswith("sqlite:///"):
        db_path = db_url[10:]
    else:
        ws_dir = os.environ.get("WORKSPACE_DIR", "workspaces")
        # 猜测 DB 路径：workspaces 的父父目录 / data / appllo.db
        parent = os.path.abspath(os.path.join(ws_dir, "..", "data", "appllo.db"))
        if os.path.exists(parent):
            db_path = parent
        else:
            print(json.dumps({"error": "cannot locate database", "results": []}))
            return

    if not os.path.exists(db_path):
        print(json.dumps({"error": f"db not found: {db_path}", "results": []}))
        return

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        # 先找同 fingerprint 的 issue，再找其 success 分析
        cur = conn.execute(
            "SELECT datadog_issue_id FROM crash_issues WHERE stack_fingerprint = ? LIMIT 10",
            (args.fingerprint,),
        )
        issue_ids = [r[0] for r in cur.fetchall()]
        if not issue_ids:
            print(json.dumps({"fingerprint": args.fingerprint, "results": []}))
            conn.close()
            return
        placeholders = ",".join("?" * len(issue_ids))
        cur = conn.execute(
            f"""SELECT datadog_issue_id, root_cause, fix_suggestion, fix_diff,
                       confidence, created_at
                FROM crash_analyses
                WHERE datadog_issue_id IN ({placeholders})
                  AND status = 'success'
                  AND root_cause != ''
                ORDER BY created_at DESC
                LIMIT ?""",
            issue_ids + [args.limit],
        )
        results = [dict(r) for r in cur.fetchall()]
        # 截断长字段避免 token 爆炸
        for r in results:
            r["root_cause"] = (r.get("root_cause") or "")[:500]
            r["fix_suggestion"] = (r.get("fix_suggestion") or "")[:500]
            r["fix_diff"] = (r.get("fix_diff") or "")[:800]
        conn.close()
        print(json.dumps({"fingerprint": args.fingerprint, "results": results}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": str(exc), "results": []}))


if __name__ == "__main__":
    main()
```

- [ ] **Step 8: 创建 get_session.py**

创建 `backend/app/crashguard/services/diagnosis_tools/get_session.py`：

```python
#!/usr/bin/env python3
"""拉取 RUM session 完整事件流，供 AI agent 分析崩溃前用户行为。

用法: python tools/get_session.py --session-id <id> [--limit 100]
输出: JSON {session_id, event_count, events: [...]}
"""
from __future__ import annotations
import argparse
import json
import os
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    api_key = os.environ.get("CRASHGUARD_DATADOG_API_KEY", "")
    app_key = os.environ.get("CRASHGUARD_DATADOG_APP_KEY", "")
    site = os.environ.get("CRASHGUARD_DATADOG_SITE", "datadoghq.com")

    if not api_key:
        print(json.dumps({"error": "CRASHGUARD_DATADOG_API_KEY not set"}))
        return

    dql = f"@session.id:{args.session_id}"
    url = f"https://api.{site}/api/v2/rum/events/search"
    payload = json.dumps({
        "filter": {"query": dql},
        "page": {"limit": min(args.limit, 200)},
        "sort": "timestamp",
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("DD-API-KEY", api_key)
    req.add_header("DD-APPLICATION-KEY", app_key or api_key)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            events = data.get("data", [])
            # 精简每个 event，只保留关键字段
            slim = []
            for ev in events:
                attrs = ev.get("attributes", {})
                slim.append({
                    "timestamp": attrs.get("timestamp", ""),
                    "type": attrs.get("type", ""),
                    "action": attrs.get("action", {}).get("type", ""),
                    "view": attrs.get("view", {}).get("name", ""),
                    "error": attrs.get("error", {}).get("message", ""),
                    "duration_ms": attrs.get("duration", 0),
                })
            print(json.dumps({
                "session_id": args.session_id,
                "event_count": len(slim),
                "events": slim,
            }, ensure_ascii=False))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        print(json.dumps({"error": f"HTTP {e.code}", "detail": body}))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))


if __name__ == "__main__":
    main()
```

- [ ] **Step 9: 运行工具单元测试（预期通过）**

```bash
pytest tests/crashguard/test_diagnosis_tools_unit.py -v
```

Expected:
```
PASSED test_git_blame_missing_args
PASSED test_git_pickaxe_no_repo
PASSED test_find_similar_no_db
PASSED test_datadog_query_no_key
PASSED test_get_session_no_key
5 passed
```

- [ ] **Step 10: Commit**

```bash
git add backend/app/crashguard/services/diagnosis_tools/ \
        backend/tests/crashguard/test_diagnosis_tools_unit.py
git commit -m "feat(crashguard): Phase 1 诊断工具注册表 — 5 个 agent 可调用 CLI 脚本"
```

---

## Task 5: deep_analyzer.py — Phase 1 核心服务

**Files:**
- Create: `backend/app/crashguard/services/deep_analyzer.py`
- Test: `backend/tests/crashguard/test_deep_analyzer_parse.py` (补全 test_auto_proceed_conditions)

- [ ] **Step 1: 创建 deep_analyzer.py（核心逻辑）**

创建 `backend/app/crashguard/services/deep_analyzer.py`：

```python
"""
Crashguard Phase 1 深度诊断服务。

目标：AI 主动调查（工具调用 + 代码阅读）→ 多假设诊断报告 → 人工确认 → Phase 2 修复。

与 analyzer.py 的关键区别：
- 不强制输出 fix_diff；鼓励 AI 诚实说"不确定"
- workspace 内提供 5 个调查工具脚本（tools/）
- 输出 diagnosis.json（多假设 + data_gaps），而非 result.json
- 超时 1800s（30 分钟），远长于 Phase 2 的 600s
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.crashguard.models import CrashAnalysis, CrashIssue
from app.crashguard.config import get_crashguard_settings
from app.db.database import get_session

logger = logging.getLogger("crashguard.deep_analyzer")

_DIAGNOSIS_TASKS: set = set()

# ANR/freeze 专项调查补充说明
_ANR_FREEZE_BLOCK = """
## ⚠️ ANR / Freeze 专项调查指引

此崩溃类型为 **{crash_type}**，堆栈告诉你"卡在哪"但不说"为什么卡"。**必须执行**：

1. 检查主线程调用栈是否含 IO / 网络 / 数据库 / 锁等待操作
2. 用 `python tools/datadog_query.py` 查询同 session 的帧率数据：
   ```
   python tools/datadog_query.py --dql "@session.id:<session_id> @type:action" --limit 50
   ```
3. 检查是否有跨线程数据竞争（shared state without synchronization）
4. 如果需要更多数据，在 data_gaps 里给出 `Timeline.startSync()` 或 `Performance.mark()` 埋点建议
"""


def _should_auto_proceed(
    hypotheses: List[Dict], data_gaps: List[Dict], threshold: float = 0.9,
) -> bool:
    """快车道条件：单假设 + confidence >= threshold + can_fix_now + no data_gaps。"""
    if len(hypotheses) != 1:
        return False
    h = hypotheses[0]
    if float(h.get("confidence", 0)) < threshold:
        return False
    if not h.get("can_fix_now", False):
        return False
    if data_gaps:
        return False
    return True


def _safe_workspace_root() -> Path:
    base = Path(os.environ.get("WORKSPACE_DIR", "workspaces")).resolve()
    return base / "_crashguard_diagnosis"


def _prepare_diagnosis_workspace(issue_id: str) -> Path:
    """创建 Phase 1 workspace，复制 tool scripts，软链 code repo。"""
    root = _safe_workspace_root()
    safe_id = issue_id.replace("/", "_")
    ws = root / safe_id
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    (ws / "output").mkdir(parents=True, exist_ok=True)

    # 复制 5 个工具脚本到 workspace/tools/
    tools_src = Path(__file__).parent / "diagnosis_tools"
    tools_dst = ws / "tools"
    tools_dst.mkdir(exist_ok=True)
    for script in ("datadog_query.py", "git_blame.py", "git_pickaxe.py",
                   "find_similar.py", "get_session.py"):
        src = tools_src / script
        if src.exists():
            shutil.copy2(src, tools_dst / script)

    # 软链 code repo（同 analyzer.py）
    code_repo = os.environ.get("CODE_REPO_PATH") or os.environ.get("CODE_REPO_APP")
    if code_repo:
        code_repo = os.path.expanduser(code_repo)
        if Path(code_repo).exists():
            try:
                os.symlink(code_repo, ws / "code")
            except OSError as exc:
                logger.warning("diagnosis symlink code repo failed: %s", exc)

    return ws


def _build_diagnosis_prompt(snapshot_data: Dict[str, Any]) -> str:
    """构建 Phase 1 专用 prompt（与 analyzer._PROMPT_TEMPLATE 完全独立）。"""
    crash_type = snapshot_data.get("crash_type", "crash")
    anr_block = (
        _ANR_FREEZE_BLOCK.format(crash_type=crash_type)
        if crash_type in ("anr", "freeze")
        else ""
    )
    stack_paths_block = snapshot_data.get("stack_paths_block", "")
    code_hint = snapshot_data.get("code_hint", "")
    enrichment = snapshot_data.get("enrichment_block", "")

    return f"""你是 Plaud 移动端崩溃调查专家。你的目标是**深度调查并提出假设**，不是立即给修复代码。

## 待调查的崩溃

- **平台**: {snapshot_data.get("platform", "—")}
- **崩溃类型**: {crash_type}
- **标题**: {snapshot_data.get("title", "—")}
- **版本范围**: {snapshot_data.get("first_seen_version", "—")} – {snapshot_data.get("last_seen_version", "—")}
- **首次出现**: {snapshot_data.get("first_seen_at", "—")}
- **总事件数**: {snapshot_data.get("total_events", 0)}
- **代表性堆栈**:

```
{snapshot_data.get("stack_trace", "")}
```
{enrichment}
## 源码导航

{code_hint}
{stack_paths_block}{anr_block}
## 可用调查工具（通过 Bash 调用，输出 JSON）

```bash
# Datadog RUM 查询（任意 DQL）
python tools/datadog_query.py --dql "<查询语句>" --limit 50

# git blame 单行
python tools/git_blame.py --file "<相对 repo 根的路径>" --line <行号> --repo-path code/<子仓库名>

# 搜索关键词被哪次 commit 引入
python tools/git_pickaxe.py --keyword "<方法名或字符串>" --repo-path code/<子仓库名>

# 查历史相似 crash 的修复经验
python tools/find_similar.py --fingerprint "{snapshot_data.get("stack_fingerprint", "")}"

# 拉完整 RUM session 事件流（崩溃前用户操作路径）
python tools/get_session.py --session-id "<session_id from Datadog>" --limit 100
```

## 调查纪律（**严格执行**）

1. **至少调用 2 个工具**后才能写出结论，不允许空手下结论
2. **必须给 1-5 个假设**，每个假设必须包含来自工具调用或堆栈的具体证据
3. **禁止编造证据**——工具没有返回的信息不能当作"证据"
4. 若所有假设 confidence < 0.5，**必须**在 data_gaps 里说明缺什么数据、怎么收集
5. 不要在 diagnosis.json 里写 fix_diff——那是 Phase 2 的工作；这里只需要 fix_direction（文字描述修复方向）

## 输出（写入 output/diagnosis.json）

```json
{{
  "crash_type": "{crash_type}",
  "investigation_log": ["步骤1: 用 git_blame 查了 xxx", "步骤2: datadog 查询返回..."],
  "hypotheses": [
    {{
      "id": "h1",
      "title": "简短标题（10-20字）",
      "evidence": ["具体证据1", "具体证据2"],
      "confidence": 0.0,
      "fix_direction": "修复方向描述（不要给代码，只描述修什么、怎么改）",
      "code_pointers": ["file_path:line 或空串"],
      "can_fix_now": true,
      "complexity": "simple"
    }}
  ],
  "data_gaps": [
    {{
      "description": "缺少什么数据",
      "collection_method": "如何收集",
      "instrumentation_code": "建议的埋点代码片段（可为空串）",
      "datadog_query": "建议的 DQL 查询（可为空串）"
    }}
  ],
  "overall_confidence": 0.0,
  "recommended_hypothesis": "h1",
  "auto_proceed_to_fix": false
}}
```

**重要**：必须用 Write 工具将 JSON 写入 `output/diagnosis.json`，不写文件 = 调查失败。
"""


def _parse_diagnosis_json(workspace: Path) -> Dict[str, Any]:
    """解析 output/diagnosis.json，失败返回空 dict。"""
    target = workspace / "output" / "diagnosis.json"
    if target.exists():
        try:
            text = target.read_text(encoding="utf-8").lstrip("﻿")
            d = json.loads(text)
            d["_raw"] = text[:8000]
            return d
        except Exception as exc:
            logger.warning("parse diagnosis.json failed: %s", exc)
    # fallback: rglob
    for cand in workspace.rglob("diagnosis.json"):
        if cand == target:
            continue
        try:
            text = cand.read_text(encoding="utf-8").lstrip("﻿")
            d = json.loads(text)
            d["_raw"] = text[:8000]
            logger.info("found diagnosis.json at fallback: %s", cand)
            return d
        except Exception:
            continue
    return {"_raw": ""}


async def _update_diagnosis_status(run_id: str, status: str, error: str = "") -> None:
    async with get_session() as session:
        row = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == run_id)
        )).scalar_one_or_none()
        if row:
            row.status = status
            if error:
                row.error = error[:1000]
            await session.commit()


async def _run_diagnosis_in_background(issue_id: str, run_id: str) -> None:
    """后台 asyncio.Task：跑 Phase 1 agent → 解析 diagnosis.json → 写 DB。"""
    try:
        await _update_diagnosis_status(run_id, "running")

        async with get_session() as session:
            issue = (await session.execute(
                select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
            )).scalar_one_or_none()
            if issue is None:
                await _update_diagnosis_status(run_id, "failed", "issue not found")
                return
            snapshot = {
                "platform": issue.platform or "—",
                "title": issue.title or "—",
                "first_seen_version": issue.first_seen_version or "—",
                "last_seen_version": issue.last_seen_version or "—",
                "first_seen_at": issue.first_seen_at.isoformat() if issue.first_seen_at else "—",
                "total_events": issue.total_events or 0,
                "stack_trace": (issue.representative_stack or "")[:8000],
                "stack_fingerprint": issue.stack_fingerprint or "",
            }

        # crash_type 预判
        from app.crashguard.services.crash_type_classifier import classify_crash_type
        crash_type = classify_crash_type(
            snapshot["title"], snapshot["stack_trace"], {}
        )
        snapshot["crash_type"] = crash_type

        workspace = _prepare_diagnosis_workspace(issue_id)

        # enrichment + code_hint + stack_paths（复用 analyzer 的辅助函数）
        try:
            from app.crashguard.services.analyzer import (
                _build_enrichment_block,
                _platform_code_hint,
                _build_stack_paths_block,
            )
            snapshot["enrichment_block"] = await _build_enrichment_block(issue_id)
            snapshot["code_hint"] = _platform_code_hint(snapshot["platform"], workspace)
            snapshot["stack_paths_block"] = _build_stack_paths_block(
                snapshot["stack_trace"], snapshot["platform"], workspace,
            )
        except Exception as exc:
            logger.warning("diagnosis enrichment failed (non-fatal): %s", exc)
            snapshot.setdefault("enrichment_block", "")
            snapshot.setdefault("code_hint", "")
            snapshot.setdefault("stack_paths_block", "")

        prompt = _build_diagnosis_prompt(snapshot)
        try:
            (workspace / "prompt.md").write_text(prompt, encoding="utf-8")
        except Exception:
            pass

        # 运行 agent
        s = get_crashguard_settings()
        timeout_s = int(getattr(s, "deep_analysis_timeout_seconds", 1800))

        from app.services.agent_orchestrator import AgentOrchestrator
        orch = AgentOrchestrator()
        try:
            agent = orch.select_agent(rule_type="crashguard")
        except RuntimeError as exc:
            await _update_diagnosis_status(run_id, "failed", f"agent unavailable: {exc}")
            return

        import time
        started = time.time()
        try:
            await asyncio.wait_for(
                agent.analyze(workspace=workspace, prompt=prompt),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.error("deep analysis timed out after %ds (run_id=%s)", timeout_s, run_id)
        except Exception as exc:
            logger.exception("deep analysis agent failed run_id=%s", run_id)
            await _update_diagnosis_status(run_id, "failed", f"agent error: {exc}")
            return
        logger.info("deep analysis agent finished in %.1fs (run_id=%s)", time.time() - started, run_id)

        diag = _parse_diagnosis_json(workspace)

        # 如 diagnosis.json 为空，retry 一次
        if not (diag.get("hypotheses") or []):
            logger.warning("diagnosis.json empty/missing after first run — retrying (run_id=%s)", run_id)
            retry_prompt = (
                "⚠️ **上一次执行没有把 diagnosis.json 写到 `output/` 目录**。\n\n"
                "请立即用 Write 工具将调查结论写入 `output/diagnosis.json`。\n"
                "可基于已读取的代码和工具调用结果直接给出最佳猜测。\n\n"
                "原始任务：\n\n"
            ) + prompt
            try:
                await asyncio.wait_for(
                    agent.analyze(workspace=workspace, prompt=retry_prompt),
                    timeout=min(timeout_s, 600),
                )
                retry_diag = _parse_diagnosis_json(workspace)
                if retry_diag.get("hypotheses"):
                    diag = retry_diag
            except Exception:
                logger.exception("deep analysis retry failed (run_id=%s)", run_id)

        # 持久化
        hypotheses = diag.get("hypotheses") or []
        data_gaps = diag.get("data_gaps") or []
        auto_proceed = _should_auto_proceed(
            hypotheses, data_gaps,
            threshold=float(getattr(s, "deep_analysis_auto_proceed_threshold", 0.9)),
        )

        async with get_session() as session:
            row = (await session.execute(
                select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == run_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.crash_type = diag.get("crash_type", crash_type)
            row.hypotheses = json.dumps(hypotheses, ensure_ascii=False)
            row.data_gaps = json.dumps(data_gaps, ensure_ascii=False)
            row.investigation_log = json.dumps(
                diag.get("investigation_log", []), ensure_ascii=False
            )
            row.root_cause = diag.get("recommended_hypothesis", "") or ""
            row.feasibility_score = float(diag.get("overall_confidence", 0.0) or 0.0)
            row.confidence = "high" if row.feasibility_score >= 0.8 else (
                "medium" if row.feasibility_score >= 0.5 else "low"
            )
            row.agent_raw_output = (diag.get("_raw", "") or "")[:8000]
            if hypotheses:
                row.status = "success"
            else:
                row.status = "empty"
                row.error = "no hypotheses in diagnosis.json"
            await session.commit()

        # 快车道
        if auto_proceed and hypotheses:
            logger.info(
                "deep analysis auto_proceed triggered (run_id=%s hypothesis=%s)",
                run_id, hypotheses[0].get("id"),
            )
            try:
                await start_fix_analysis(
                    diagnosis_run_id=run_id,
                    hypothesis_id=hypotheses[0]["id"],
                    approver="auto",
                )
            except Exception as exc:
                logger.warning("auto_proceed start_fix_analysis failed: %s", exc)

    except Exception as exc:
        logger.exception("_run_diagnosis_in_background crashed run_id=%s", run_id)
        try:
            await _update_diagnosis_status(run_id, "failed", str(exc))
        except Exception:
            pass


async def start_deep_analysis(
    issue_id: str,
    triggered_by: str = "manual",
    force: bool = False,
    dedup_hours: Optional[int] = None,
) -> str:
    """触发 Phase 1 深度诊断，立即返回 run_id，后台异步执行。"""
    async with get_session() as session:
        issue = (await session.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
        )).scalar_one_or_none()
        if issue is None:
            raise ValueError(f"issue {issue_id} not found")

    s = get_crashguard_settings()
    if not force:
        hours = dedup_hours
        if hours is None:
            hours = int(getattr(s, "deep_analysis_dedup_hours", 6) or 6)
        if hours > 0:
            cutoff = datetime.utcnow() - timedelta(hours=hours)
            async with get_session() as session:
                latest = (await session.execute(
                    select(CrashAnalysis)
                    .where(
                        CrashAnalysis.datadog_issue_id == issue_id,
                        CrashAnalysis.phase == "diagnosis",
                        CrashAnalysis.status == "success",
                        CrashAnalysis.created_at >= cutoff,
                    )
                    .order_by(CrashAnalysis.created_at.desc())
                    .limit(1)
                )).scalar_one_or_none()
                if latest is not None:
                    logger.info("deep analysis dedup hit: reusing run_id=%s", latest.analysis_run_id)
                    return latest.analysis_run_id

    run_id = str(uuid.uuid4())
    async with get_session() as session:
        session.add(CrashAnalysis(
            datadog_issue_id=issue_id,
            analysis_run_id=run_id,
            agent_name="",
            triggered_by=triggered_by,
            problem_type="",
            scenario="",
            root_cause="",
            fix_suggestion="",
            feasibility_score=0.0,
            confidence="low",
            reproducibility="unknown",
            agent_raw_output="",
            status="pending",
            phase="diagnosis",
            hypotheses="[]",
            data_gaps="[]",
            investigation_log="[]",
            created_at=datetime.utcnow(),
        ))
        await session.commit()

    task = asyncio.create_task(_run_diagnosis_in_background(issue_id, run_id))
    _DIAGNOSIS_TASKS.add(task)
    task.add_done_callback(_DIAGNOSIS_TASKS.discard)
    return run_id


async def get_diagnosis_status(run_id: str) -> Optional[Dict[str, Any]]:
    """按 run_id 查 Phase 1 诊断状态。返回 None 表示不存在。"""
    async with get_session() as session:
        row = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == run_id)
        )).scalar_one_or_none()
        if row is None:
            return None
        try:
            hypotheses = json.loads(row.hypotheses or "[]")
        except Exception:
            hypotheses = []
        try:
            data_gaps = json.loads(row.data_gaps or "[]")
        except Exception:
            data_gaps = []
        try:
            investigation_log = json.loads(row.investigation_log or "[]")
        except Exception:
            investigation_log = []
        return {
            "run_id": row.analysis_run_id,
            "datadog_issue_id": row.datadog_issue_id,
            "phase": row.phase or "diagnosis",
            "status": row.status or "pending",
            "crash_type": getattr(row, "crash_type", "") or "",
            "hypotheses": hypotheses,
            "data_gaps": data_gaps,
            "investigation_log": investigation_log,
            "overall_confidence": float(row.feasibility_score or 0.0),
            "recommended_hypothesis": row.root_cause or "",
            "confirmed_hypothesis_id": getattr(row, "confirmed_hypothesis_id", "") or "",
            "error": row.error or "",
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


async def confirm_hypothesis(run_id: str, hypothesis_id: str) -> str:
    """人工确认假设，触发 Phase 2 修复分析，返回 phase2_run_id。"""
    async with get_session() as session:
        row = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == run_id)
        )).scalar_one_or_none()
        if row is None:
            raise ValueError(f"diagnosis run {run_id} not found")
        if row.phase != "diagnosis":
            raise ValueError(f"run {run_id} is not a diagnosis phase run")
        row.confirmed_hypothesis_id = hypothesis_id
        await session.commit()

    return await start_fix_analysis(run_id, hypothesis_id, approver="human")


async def start_fix_analysis(
    diagnosis_run_id: str,
    hypothesis_id: str,
    approver: str = "human",
) -> str:
    """基于确认的假设触发 Phase 2 修复分析，返回 fix run_id。"""
    # 加载诊断结果
    diag_status = await get_diagnosis_status(diagnosis_run_id)
    if diag_status is None:
        raise ValueError(f"diagnosis {diagnosis_run_id} not found")

    # 找到对应假设
    hypothesis = next(
        (h for h in diag_status["hypotheses"] if h.get("id") == hypothesis_id),
        None,
    )
    if hypothesis is None:
        # fallback：用第一个假设
        hypothesis = (diag_status["hypotheses"] or [{}])[0]

    issue_id = diag_status["datadog_issue_id"]

    # 构建"已确认假设"上下文块，注入 Phase 2 prompt
    hyp_block = _format_confirmed_hypothesis_block(hypothesis, diag_status)

    # 调用现有 analyzer.start_analysis，把假设块作为 followup_question 的特殊载体
    # （避免修改 analyzer 主流程；followup_question 非空时 analyzer 走 followup prompt）
    # 更干净的做法是在 analyzer 里加 confirmed_hypothesis 参数，见 Task 6
    from app.crashguard.services.analyzer import start_analysis as _start_analysis
    fix_run_id = await _start_analysis(
        issue_id=issue_id,
        triggered_by=f"phase2_from_{approver}",
        force=True,
        confirmed_hypothesis_block=hyp_block,  # Task 6 里在 analyzer 里支持这个参数
        parent_diagnosis_run_id=diagnosis_run_id,
    )
    return fix_run_id


def _format_confirmed_hypothesis_block(hypothesis: Dict, diag_status: Dict) -> str:
    """把确认的假设格式化为注入 Phase 2 prompt 的文本块。"""
    lines = [
        "## ✅ 已确认的根因假设（Phase 1 深度诊断结论）\n",
        f"- **假设 ID**: {hypothesis.get('id', '')}",
        f"- **标题**: {hypothesis.get('title', '')}",
        f"- **置信度**: {hypothesis.get('confidence', 0):.0%}",
        f"- **修复方向**: {hypothesis.get('fix_direction', '')}",
    ]
    pointers = hypothesis.get("code_pointers") or []
    if pointers:
        lines.append(f"- **代码定位**: {', '.join(p for p in pointers if p)}")
    evidence = hypothesis.get("evidence") or []
    if evidence:
        lines.append("\n**调查依据**：")
        for ev in evidence[:5]:
            lines.append(f"  - {ev}")
    inv_log = diag_status.get("investigation_log") or []
    if inv_log:
        lines.append(f"\n**Phase 1 调查摘要**（共 {len(inv_log)} 步）：")
        for step in inv_log[:5]:
            lines.append(f"  - {step}")
    lines.append(
        "\n**指令**：请基于以上确认的假设**直接生成 fix_diff**，"
        "不需要重新分析根因。重点放在准确的代码改动上。\n"
    )
    return "\n".join(lines)
```

- [ ] **Step 2: 运行 test_auto_proceed_conditions（应该 PASS 了）**

```bash
cd /Users/sanato/Desktop/code/newplaud/jarvis/backend
source .venv/bin/activate
pytest tests/crashguard/test_deep_analyzer_parse.py -v
```

Expected: `2 passed`

- [ ] **Step 3: Commit**

```bash
git add backend/app/crashguard/services/deep_analyzer.py
git commit -m "feat(crashguard): Phase 1 deep_analyzer — 深度诊断主服务（工具注册表 + diagnosis.json 解析 + 快车道）"
```

---

## Task 6: analyzer.py — Phase 2 接受 confirmed_hypothesis_block

**Files:**
- Modify: `backend/app/crashguard/services/analyzer.py`

在 `start_analysis` 函数签名追加 `confirmed_hypothesis_block` 和 `parent_diagnosis_run_id` 参数，并注入到 `_build_prompt`。

- [ ] **Step 1: 修改 start_analysis 签名**

在 `analyzer.py` 的 `start_analysis` 函数中，追加两个可选参数：

```python
async def start_analysis(
    issue_id: str,
    triggered_by: str = "manual",
    followup_question: str = "",
    parent_run_id: str = "",
    force: bool = False,
    dedup_hours: Optional[int] = None,
    confirmed_hypothesis_block: str = "",   # 新增：Phase 2 注入的确认假设文本
    parent_diagnosis_run_id: str = "",      # 新增：对应 Phase 1 run_id
) -> str:
```

在创建 `CrashAnalysis` 行的那段代码里，把 `parent_diagnosis_run_id` 写入对应列：

```python
        session.add(CrashAnalysis(
            ...
            phase="fix",
            parent_diagnosis_run_id=parent_diagnosis_run_id or "",
            ...
        ))
```

- [ ] **Step 2: 把 confirmed_hypothesis_block 传递到后台任务**

在 `asyncio.create_task(_run_in_background(...))` 前，把 `confirmed_hypothesis_block` 存到新建的 run 行里（用 `followup_question` 字段存储，或新增单独传参）。

最简方案：修改 `_run_in_background` 接受 `confirmed_hypothesis_block` 参数，在 `_build_prompt` 时传入。

在 `_PROMPT_TEMPLATE` 的第一行之后（`## 待分析的崩溃` 之前）添加占位符：

```python
_PROMPT_TEMPLATE = """你是 Plaud 移动端崩溃分析专家。基于下方崩溃信息 + 真实源码给出深度分析。
{confirmed_hypothesis_block}
## 待分析的崩溃

- **平台**: {platform}
...（其余内容不变）
```

`confirmed_hypothesis_block` 默认值为空串 `""`，Phase 1 来的 Phase 2 调用时填入 `_format_confirmed_hypothesis_block()` 的返回值。

在 `_build_prompt` 中：

```python
def _build_prompt(d: Dict[str, Any]) -> str:
    data = dict(d)
    data.setdefault("enrichment_block", "")
    data.setdefault("code_hint", "")
    data.setdefault("followup_block", "")
    data.setdefault("stack_paths_block", "")
    data.setdefault("confirmed_hypothesis_block", "")  # 新增默认值
    return _PROMPT_TEMPLATE.format(**data)
```

- [ ] **Step 3: 验证现有测试仍通过**

```bash
pytest tests/crashguard/ -v --tb=short
```

Expected: 所有已有测试通过，无回归。

- [ ] **Step 4: Commit**

```bash
git add backend/app/crashguard/services/analyzer.py
git commit -m "feat(crashguard): analyzer Phase 2 — 接受 confirmed_hypothesis_block 注入（来自 Phase 1）"
```

---

## Task 7: API — 3 个新端点

**Files:**
- Modify: `backend/app/crashguard/api/crash.py`

- [ ] **Step 1: 在 crash.py 末尾追加 3 个端点**

在 `crash.py` 中追加（在现有路由末尾）：

```python
# ── Phase 1 深度诊断端点 ──────────────────────────────────────────────

class DeepAnalyzeResponse(BaseModel):
    run_id: str
    status: str = "pending"


class ConfirmHypothesisRequest(BaseModel):
    hypothesis_id: str = Field(..., description="用户选择的假设 ID，如 'h1'")


class ConfirmHypothesisResponse(BaseModel):
    diagnosis_run_id: str
    phase2_run_id: str
    hypothesis_id: str


class MarkDataNeededRequest(BaseModel):
    note: str = Field(default="", description="工程师备注，如 '已安排埋点'")


@router.post("/issues/{issue_id}/deep-analyze", response_model=DeepAnalyzeResponse)
async def deep_analyze_issue(issue_id: str) -> Any:
    """触发 Phase 1 深度诊断（异步执行，立即返回 run_id）。
    
    6h 内已有成功诊断记录时复用（dedup）。
    """
    from app.crashguard.services.deep_analyzer import start_deep_analysis
    try:
        run_id = await start_deep_analysis(issue_id=issue_id, triggered_by="manual_ui")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return DeepAnalyzeResponse(run_id=run_id)


@router.post("/analyses/{run_id}/confirm-hypothesis", response_model=ConfirmHypothesisResponse)
async def confirm_hypothesis_endpoint(run_id: str, body: ConfirmHypothesisRequest) -> Any:
    """人工确认诊断假设，触发 Phase 2 修复分析。"""
    from app.crashguard.services.deep_analyzer import confirm_hypothesis
    try:
        phase2_run_id = await confirm_hypothesis(run_id, body.hypothesis_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ConfirmHypothesisResponse(
        diagnosis_run_id=run_id,
        phase2_run_id=phase2_run_id,
        hypothesis_id=body.hypothesis_id,
    )


@router.post("/analyses/{run_id}/mark-data-needed")
async def mark_data_needed(run_id: str, body: MarkDataNeededRequest) -> Any:
    """标记该诊断处于'等待数据'状态（工程师已安排监控埋点）。"""
    from sqlalchemy import select as _select
    from app.crashguard.models import CrashAnalysis
    from app.db.database import get_session as _get_session

    async with _get_session() as session:
        row = (await session.execute(
            _select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == run_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        row.status = "waiting_data"
        if body.note:
            row.error = f"[data_needed] {body.note}"[:500]
        await session.commit()
    return {"run_id": run_id, "status": "waiting_data", "note": body.note}
```

- [ ] **Step 2: 验证 API 文档可正常加载（curl health check）**

启动 backend：
```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload &
sleep 3
curl -s http://localhost:8000/api/crash/health | python3 -m json.tool
```

Expected: `{"status": "ok", ...}`

验证新端点出现在 OpenAPI 文档：
```bash
curl -s http://localhost:8000/openapi.json | python3 -c "
import json, sys
spec = json.load(sys.stdin)
paths = [p for p in spec['paths'] if 'deep-analyze' in p or 'confirm-hypothesis' in p or 'mark-data-needed' in p]
print('New endpoints:', paths)
"
```

Expected:
```
New endpoints: ['/api/crash/issues/{issue_id}/deep-analyze', '/api/crash/analyses/{run_id}/confirm-hypothesis', '/api/crash/analyses/{run_id}/mark-data-needed']
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/crashguard/api/crash.py
git commit -m "feat(crashguard): Phase 1 API — deep-analyze + confirm-hypothesis + mark-data-needed"
```

---

## Task 8: Frontend — api.ts 新增 wrapper 函数

**Files:**
- Modify: `frontend/src/lib/api.ts`

- [ ] **Step 1: 在 api.ts 末尾追加 Phase 1 类型和 wrapper**

```typescript
// ── Phase 1 深度诊断 ───────────────────────────────────────────

export interface DiagnosisHypothesis {
  id: string;
  title: string;
  evidence: string[];
  confidence: number;
  fix_direction: string;
  code_pointers: string[];
  can_fix_now: boolean;
  complexity: "simple" | "complex";
}

export interface DiagnosisDataGap {
  description: string;
  collection_method: string;
  instrumentation_code: string;
  datadog_query: string;
}

export interface DiagnosisStatus {
  run_id: string;
  datadog_issue_id: string;
  phase: "diagnosis" | "fix";
  status: "pending" | "running" | "success" | "failed" | "empty" | "waiting_data";
  crash_type: string;
  hypotheses: DiagnosisHypothesis[];
  data_gaps: DiagnosisDataGap[];
  investigation_log: string[];
  overall_confidence: number;
  recommended_hypothesis: string;
  confirmed_hypothesis_id: string;
  error: string;
  created_at: string | null;
}

export const startDeepAnalysis = (issueId: string) =>
  request<{ run_id: string; status: string }>(
    `/crash/issues/${encodeURIComponent(issueId)}/deep-analyze`,
    { method: "POST", body: JSON.stringify({}) },
  );

export const fetchDiagnosisStatus = (runId: string) =>
  request<DiagnosisStatus>(`/crash/analyses/${encodeURIComponent(runId)}`);

export const confirmDiagnosisHypothesis = (runId: string, hypothesisId: string) =>
  request<{ diagnosis_run_id: string; phase2_run_id: string; hypothesis_id: string }>(
    `/crash/analyses/${encodeURIComponent(runId)}/confirm-hypothesis`,
    { method: "POST", body: JSON.stringify({ hypothesis_id: hypothesisId }) },
  );

export const markDiagnosisDataNeeded = (runId: string, note: string) =>
  request<{ run_id: string; status: string; note: string }>(
    `/crash/analyses/${encodeURIComponent(runId)}/mark-data-needed`,
    { method: "POST", body: JSON.stringify({ note }) },
  );
```

- [ ] **Step 2: 验证 TypeScript 编译无报错**

```bash
cd /Users/sanato/Desktop/code/newplaud/jarvis/frontend
npm run build 2>&1 | tail -20
```

Expected: 无 TypeScript 错误（可能有其他 lint warning，忽略）

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/api.ts
git commit -m "feat(crashguard): frontend api.ts — Phase 1 深度诊断 wrapper 函数 + 类型定义"
```

---

## Task 9: Frontend — IssueDetailPanel 深度诊断区块

**Files:**
- Modify: `frontend/src/app/crashguard/page.tsx`

- [ ] **Step 1: 在 page.tsx 顶部 import 新 API 函数**

在 page.tsx 的 import 行找到现有的 crashguard api imports，追加：

```typescript
import {
  startDeepAnalysis,
  fetchDiagnosisStatus,
  confirmDiagnosisHypothesis,
  markDiagnosisDataNeeded,
  type DiagnosisStatus,
  type DiagnosisHypothesis,
} from "@/lib/api";
```

- [ ] **Step 2: 在 IssueDetailPanel 组件 props 和主组件 state 中追加诊断状态**

在主组件 state 区块（`useState` 集中处）追加：

```typescript
const [diagRunId, setDiagRunId] = useState<string | null>(null);
const [diagStatus, setDiagStatus] = useState<DiagnosisStatus | null>(null);
const [diagLoading, setDiagLoading] = useState(false);
const [diagConfirming, setDiagConfirming] = useState<string | null>(null); // 正在确认的 hypothesis_id
```

- [ ] **Step 3: 添加深度诊断触发 handler**

在主组件的 handler 区块追加：

```typescript
const onStartDeepAnalysis = async (issueId: string) => {
  setDiagLoading(true);
  setDiagStatus(null);
  try {
    const { run_id } = await startDeepAnalysis(issueId);
    setDiagRunId(run_id);
    // 开始轮询状态
    const poll = async () => {
      try {
        const st = await fetchDiagnosisStatus(run_id);
        setDiagStatus(st as DiagnosisStatus);
        if (st.status === "pending" || st.status === "running") {
          setTimeout(poll, 8000);
        }
      } catch {
        // ignore poll error
      }
    };
    setTimeout(poll, 3000);
  } catch (e: any) {
    setToast({ msg: e.message || "deep analysis failed", type: "error" });
  } finally {
    setDiagLoading(false);
  }
};

const onConfirmHypothesis = async (runId: string, hypothesisId: string, issueId: string) => {
  setDiagConfirming(hypothesisId);
  try {
    const { phase2_run_id } = await confirmDiagnosisHypothesis(runId, hypothesisId);
    setToast({ msg: `Phase 2 已触发，run_id: ${phase2_run_id.slice(0, 8)}`, type: "success" });
    // 刷新 analyses 列表
    const list = await fetchCrashAnalyses(issueId).catch(() => ({ analyses: [] }));
    setAnalyses((list as any).analyses || []);
  } catch (e: any) {
    setToast({ msg: e.message || "confirm failed", type: "error" });
  } finally {
    setDiagConfirming(null);
  }
};
```

- [ ] **Step 4: 在 IssueDetailPanel 组件渲染区块追加深度诊断 Tab 内容**

找到 `IssueDetailPanel` 组件定义处（约 line 1880），在 props interface 追加：

```typescript
  diagStatus: DiagnosisStatus | null;
  diagLoading: boolean;
  diagConfirming: string | null;
  onStartDeepAnalysis: (issueId: string) => void;
  onConfirmHypothesis: (runId: string, hypothesisId: string, issueId: string) => void;
```

在 `IssueDetailPanel` 内部渲染，在已有的 analyses 区块之后追加深度诊断区块：

```tsx
{/* 深度诊断区块 */}
<div style={{ marginTop: 24, borderTop: "1px solid #E5E7EB", paddingTop: 16 }}>
  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
    <span style={{ fontWeight: 600, fontSize: 14 }}>🔍 深度诊断</span>
    {!diagStatus && !diagLoading && (
      <button
        onClick={() => onStartDeepAnalysis(detail.datadog_issue_id || "")}
        style={{
          padding: "4px 12px", fontSize: 12, borderRadius: 6,
          background: "#1D4ED8", color: "#fff", border: "none", cursor: "pointer",
        }}
      >
        启动深度诊断（15-30 分钟）
      </button>
    )}
    {diagLoading && <span style={{ fontSize: 12, color: "#6B7280" }}>启动中…</span>}
  </div>

  {diagStatus && (diagStatus.status === "pending" || diagStatus.status === "running") && (
    <div style={{ fontSize: 12, color: "#6B7280" }}>
      ⏳ AI 正在调查中… 请稍候（每 8 秒自动刷新）
      {diagStatus.investigation_log?.length > 0 && (
        <details style={{ marginTop: 6 }}>
          <summary style={{ cursor: "pointer" }}>调查日志（{diagStatus.investigation_log.length} 步）</summary>
          <ul style={{ paddingLeft: 16, marginTop: 4 }}>
            {diagStatus.investigation_log.map((s, i) => (
              <li key={i} style={{ marginBottom: 2 }}>{s}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )}

  {diagStatus?.status === "success" && (
    <div>
      <div style={{ fontSize: 12, color: "#6B7280", marginBottom: 8 }}>
        总体置信度: {(diagStatus.overall_confidence * 100).toFixed(0)}% · 崩溃类型: {diagStatus.crash_type}
      </div>
      {diagStatus.hypotheses.map((h) => (
        <div
          key={h.id}
          style={{
            border: `1px solid ${h.id === diagStatus.recommended_hypothesis ? "#1D4ED8" : "#E5E7EB"}`,
            borderRadius: 8, padding: "10px 14px", marginBottom: 10,
            background: h.id === diagStatus.recommended_hypothesis ? "#EFF6FF" : "#F9FAFB",
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <span style={{ fontWeight: 600, fontSize: 13 }}>{h.title}</span>
              {h.id === diagStatus.recommended_hypothesis && (
                <span style={{
                  marginLeft: 6, fontSize: 10, background: "#1D4ED8", color: "#fff",
                  padding: "1px 6px", borderRadius: 4,
                }}>推荐</span>
              )}
            </div>
            <span style={{ fontSize: 12, color: "#6B7280" }}>{(h.confidence * 100).toFixed(0)}%</span>
          </div>
          <div style={{ fontSize: 11, color: "#6B7280", margin: "4px 0" }}>
            {h.fix_direction}
          </div>
          <div style={{ fontSize: 11, color: "#374151", marginBottom: 6 }}>
            {h.evidence.slice(0, 3).map((ev, i) => (
              <div key={i}>• {ev}</div>
            ))}
          </div>
          <button
            onClick={() => onConfirmHypothesis(diagStatus.run_id, h.id, detail.datadog_issue_id || "")}
            disabled={diagConfirming === h.id}
            style={{
              fontSize: 11, padding: "3px 10px", borderRadius: 5,
              background: "#16A34A", color: "#fff", border: "none", cursor: "pointer",
              opacity: diagConfirming === h.id ? 0.6 : 1,
            }}
          >
            {diagConfirming === h.id ? "触发中…" : "✓ 确认此假设 → 生成修复 PR"}
          </button>
        </div>
      ))}

      {diagStatus.data_gaps?.length > 0 && (
        <div style={{ marginTop: 8, padding: "8px 12px", background: "#FEF3C7", borderRadius: 6 }}>
          <span style={{ fontSize: 12, fontWeight: 600 }}>⚠️ 数据缺口（需要更多监控数据）</span>
          {diagStatus.data_gaps.map((gap, i) => (
            <div key={i} style={{ fontSize: 11, marginTop: 4 }}>
              <div>• {gap.description}</div>
              {gap.collection_method && (
                <div style={{ color: "#6B7280" }}>采集方式：{gap.collection_method}</div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )}

  {diagStatus?.status === "failed" && (
    <div style={{ fontSize: 12, color: "#DC2626" }}>
      诊断失败: {diagStatus.error || "未知错误"}
    </div>
  )}
</div>
```

- [ ] **Step 5: 把新 props 传给 IssueDetailPanel 调用处**

找到 `<IssueDetailPanel` 的调用处（约 line 1390），追加新 props：

```tsx
<IssueDetailPanel
  ...（现有 props）
  diagStatus={diagStatus}
  diagLoading={diagLoading}
  diagConfirming={diagConfirming}
  onStartDeepAnalysis={onStartDeepAnalysis}
  onConfirmHypothesis={onConfirmHypothesis}
/>
```

- [ ] **Step 6: 重置诊断状态（切换 issue 时清空）**

找到 `selectedId` 变化时的 `useEffect`（约 line 360），在加载 issue 详情的同时重置：

```typescript
setDiagRunId(null);
setDiagStatus(null);
setDiagLoading(false);
```

- [ ] **Step 7: TypeScript 编译验证**

```bash
cd /Users/sanato/Desktop/code/newplaud/jarvis/frontend
npm run build 2>&1 | grep -E "error|Error" | head -20
```

Expected: 无 TypeScript 编译错误

- [ ] **Step 8: Commit**

```bash
git add frontend/src/app/crashguard/page.tsx
git commit -m "feat(crashguard): frontend — IssueDetailPanel 深度诊断区块（假设列表 + 确认按钮 + 数据缺口）"
```

---

## 最终验证

- [ ] **Step 1: 运行全量 crashguard 测试**

```bash
cd /Users/sanato/Desktop/code/newplaud/jarvis/backend
source .venv/bin/activate
pytest tests/crashguard/ -v
```

Expected: 所有测试通过，无回归

- [ ] **Step 2: 验证迁移在干净 DB 上正常运行**

```bash
python3 -c "
import asyncio
from app.crashguard.migrations import ensure_columns
asyncio.run(ensure_columns())
print('migration OK')
"
```

Expected: `migration OK`（无报错）

- [ ] **Step 3: 验证 3 个新端点存在**

```bash
curl -s http://localhost:8000/openapi.json | python3 -c "
import json, sys
s = json.load(sys.stdin)
for path in ['/api/crash/issues/{issue_id}/deep-analyze',
             '/api/crash/analyses/{run_id}/confirm-hypothesis',
             '/api/crash/analyses/{run_id}/mark-data-needed']:
    status = '✅' if path in s['paths'] else '❌'
    print(status, path)
"
```

Expected: 3 个 ✅

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat(crashguard): Phase 1 深度诊断系统完整交付 — deep_analyzer + 工具注册表 + API + 前端"
```
