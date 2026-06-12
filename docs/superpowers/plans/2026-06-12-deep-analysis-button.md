# 深度分析（Deep Analysis）按钮 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 失败工单加「深度分析」按钮，跳过 windowing 把完整原始日志交给 claude_code 自由 grep，用 PreToolUse hook 精确限制 30 次日志读取防失控，结果另存一条分析记录。

**Architecture:** 一个 `deep_analysis` 布尔标志从 `TaskCreate` 贯穿 `_run_task → run_analysis_pipeline → _run_context_condensation`（跳过窗口）与 `orchestrator.run_analysis`（强制 claude_code + 放宽 max_turns/timeout + 注入读取上限 hook）。读取上限靠 claude CLI 的 `.claude/settings.json` PreToolUse hook 实现，跨轮计数、fail-open。

**Tech Stack:** FastAPI + SQLAlchemy（后端）、Claude Code CLI hooks（`.claude/settings.json`）、Next.js/React（前端）、pytest。

> **提交约定**：项目铁律是「默认不主动 commit/push」。下面每个 Task 末尾的 commit 步骤仅在用户明确同意后执行；执行时若未授权，跳过 commit、保留改动等待统一提交。

**Spec:** `docs/superpowers/specs/2026-06-12-deep-analysis-button-design.md`

---

## File Structure

| 文件 | 职责 | 动作 |
|------|------|------|
| `backend/app/models/schemas.py` | `TaskCreate` 加 `deep_analysis` 字段 | Modify |
| `backend/app/api/tasks.py` | `create_task` / `_run_task` 透传标志；deep 时 timeout=1200、agent 强制 claude_code | Modify |
| `backend/app/workers/analysis_worker.py` | `run_analysis_pipeline` / `_run_context_condensation` 透传并跳过窗口 | Modify |
| `backend/app/config.py` | `AgentConfig` 加 `log_read_cap` 字段 | Modify |
| `backend/app/services/agent_orchestrator.py` | `run_analysis` 加 `deep_analysis`，deep 时设 log_read_cap/max_turns/timeout | Modify |
| `backend/app/agents/claude_code.py` | deep 时写 PreToolUse 读取上限 hook + 用 deep 的 max_turns | Modify |
| `backend/app/agents/base.py` | `build_prompt` 加 deep 分支提示词 | Modify |
| `backend/tests/test_deep_analysis.py` | 标志贯穿 + 跳窗 + 结果 tag 测试 | Create |
| `backend/tests/test_log_read_cap_hook.py` | hook 计数脚本单测 | Create |
| `frontend/src/lib/api.ts` | `createTask` 加 `deepAnalysis` 参数 | Modify |
| `frontend/src/app/page.tsx` | 失败态加「深度分析」按钮 | Modify |
| `frontend/src/app/tracking/page.tsx` | 失败态加「深度分析」按钮 | Modify |
| `frontend/src/lib/i18n.ts` | 新增文案 key | Modify |

---

## Task 1: `deep_analysis` 标志贯穿后端 API

**Files:**
- Modify: `backend/app/models/schemas.py:86`（TaskCreate）
- Modify: `backend/app/api/tasks.py:41`（create_task）、`:463`（_run_task）、`:504`（timeout 解析）

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_deep_analysis.py`:

```python
"""深度分析：deep_analysis 标志贯穿 + 跳窗 + 结果 tag。"""
from app.models.schemas import TaskCreate


def test_taskcreate_has_deep_analysis_default_false():
    tc = TaskCreate(issue_id="fb_x")
    assert tc.deep_analysis is False
    tc2 = TaskCreate(issue_id="fb_x", deep_analysis=True)
    assert tc2.deep_analysis is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_deep_analysis.py::test_taskcreate_has_deep_analysis_default_false -v`
Expected: FAIL（`TaskCreate` 无 `deep_analysis` 字段 / 不接受该 kwarg）

- [ ] **Step 3: schemas.py 加字段**

`backend/app/models/schemas.py` 的 `class TaskCreate`：

```python
class TaskCreate(BaseModel):
    issue_id: str               # Feishu record_id
    agent_type: Optional[AgentType] = None  # Override agent selection
    username: str = ""          # Who triggered this analysis
    followup_question: str = "" # Follow-up question for re-analysis
    deep_analysis: bool = False # 深度分析：跳过窗口给全量日志 + 读取上限
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_deep_analysis.py::test_taskcreate_has_deep_analysis_default_false -v`
Expected: PASS

- [ ] **Step 5: create_task 透传**

`backend/app/api/tasks.py` 的 `create_task`（约 :41-121），找到 `background_tasks.add_task(_run_task, ...)` 调用，加入 `deep_analysis=req.deep_analysis`。例如：

```python
    background_tasks.add_task(
        _run_task,
        task.task_id,
        req.issue_id,
        agent_override=agent_type_str or None,
        username=req.username,
        followup_question=req.followup_question or "",
        deep_analysis=req.deep_analysis,
    )
```

- [ ] **Step 6: _run_task 透传 + deep 强制 timeout/agent**

`backend/app/api/tasks.py:463` 改签名并在 timeout 解析处对 deep 放宽：

```python
async def _run_task(task_id: str, issue_id: str, agent_override: Optional[str] = None,
                    username: str = "", followup_question: str = "",
                    deep_analysis: bool = False):
    ...
    _cc = _get_settings().concurrency
    _task_timeout = _resolve_task_timeout(issue_id, _cc)
    if deep_analysis:
        # 完整日志 + 自由探索需要更多时间；强制走支持 hook 的 claude_code
        _task_timeout = max(_task_timeout, getattr(_cc, "task_timeout_large", 1200) or 1200)
        agent_override = "claude_code"
    ...
            result = await asyncio.wait_for(
                run_analysis_pipeline(
                    issue_id=issue_id,
                    task_id=task_id,
                    agent_override=agent_override,
                    on_progress=on_progress,
                    followup_question=followup_question,
                    pipeline_timeout=_task_timeout,
                    deep_analysis=deep_analysis,
                ),
                timeout=_task_timeout,
            ...
```

- [ ] **Step 7: 提交（需授权）**

```bash
git add backend/app/models/schemas.py backend/app/api/tasks.py backend/tests/test_deep_analysis.py
git commit -m "feat(deep): TaskCreate.deep_analysis 标志贯穿 API 层"
```

---

## Task 2: `run_analysis_pipeline` 透传 + 跳过 windowing

**Files:**
- Modify: `backend/app/workers/analysis_worker.py:219`（pipeline 签名）、`:540`（调用 _run_context_condensation）、`:806`（_run_context_condensation 签名）、窗口调用处（约 :920）

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_deep_analysis.py`：

```python
import inspect
from app.workers import analysis_worker


def test_pipeline_and_condensation_accept_deep_flag():
    assert "deep_analysis" in inspect.signature(
        analysis_worker.run_analysis_pipeline).parameters
    assert "deep_analysis" in inspect.signature(
        analysis_worker._run_context_condensation).parameters
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_deep_analysis.py::test_pipeline_and_condensation_accept_deep_flag -v`
Expected: FAIL（参数不存在）

- [ ] **Step 3: 加参数 + 跳窗逻辑**

`run_analysis_pipeline`（:219）签名末尾加 `deep_analysis: bool = False`。

调用 `_run_context_condensation` 处（约 :540）加 `deep_analysis=deep_analysis`。

`_run_context_condensation`（:806）签名加 `deep_analysis: bool = False`；在调用 `window_log_files` 之前短路：

```python
    # 深度分析：跳过 windowing，把完整原始日志交给 agent 自由探索
    if deep_analysis:
        logger.info("Deep analysis: skipping windowing, using full raw logs")
        return {
            "log_paths": log_paths,
            "structured_context": None,
            "windowing_metadata": [{"deep_mode": True, "windowed": False}],
        }
```

（放在函数体内、`window_log_files` 调用之前；保持其余返回结构一致。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_deep_analysis.py -v`
Expected: PASS（两条都过）

- [ ] **Step 5: 回归 windower/worker 导入**

Run: `cd backend && python -c "import app.workers.analysis_worker" && python -m pytest tests/test_log_windower_completeness.py -q`
Expected: import OK + windower 测试全过

- [ ] **Step 6: 提交（需授权）**

```bash
git add backend/app/workers/analysis_worker.py backend/tests/test_deep_analysis.py
git commit -m "feat(deep): pipeline 透传 deep_analysis，深度模式跳过 windowing"
```

---

## Task 3: agent 配置 — `log_read_cap` 字段 + orchestrator deep 档

**Files:**
- Modify: `backend/app/config.py:131-140`（AgentConfig）
- Modify: `backend/app/services/agent_orchestrator.py:180`（run_analysis 签名）、:123-131/:161-169（AgentConfig 构造处）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_deep_analysis.py`：

```python
from app.config import AgentConfig


def test_agentconfig_has_log_read_cap():
    cfg = AgentConfig()
    assert cfg.log_read_cap is None
    cfg2 = AgentConfig(log_read_cap=30)
    assert cfg2.log_read_cap == 30
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_deep_analysis.py::test_agentconfig_has_log_read_cap -v`
Expected: FAIL

- [ ] **Step 3: AgentConfig 加字段**

`backend/app/config.py` 的 `AgentConfig`（约 :131-140）加：

```python
    log_read_cap: Optional[int] = None   # deep 模式：日志读取次数上限（PreToolUse hook 执行）
```

（确认文件顶部已 `from typing import Optional`。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_deep_analysis.py::test_agentconfig_has_log_read_cap -v`
Expected: PASS

- [ ] **Step 5: orchestrator.run_analysis 加 deep 档**

`agent_orchestrator.py` `run_analysis`（:180）签名末尾加 `deep_analysis: bool = False`。

在构造 `AgentConfig`（:123-131 与 :161-169 两处）时，按 deep 覆盖：

```python
            cfg = AgentConfig(
                ...,
                timeout=provider.timeout or agent_cfg.timeout,
                max_turns=40 if deep_analysis else agent_cfg.max_turns,
                allowed_tools=provider.allowed_tools,
                log_read_cap=30 if deep_analysis else None,
                ...
            )
```

（两处构造点都改；deep 时 max_turns=40、log_read_cap=30。timeout 由 Task 1 的 pipeline_timeout=1200 在外层 wait_for 兜，agent 内部 timeout 也可在此设 `max(provider.timeout, 1200)`，按现有 `timeout=` 行就近改。）

并在 `run_analysis_pipeline` 调 `orchestrator.run_analysis(...)` 处（analysis_worker.py 约 :576）传 `deep_analysis=deep_analysis`。

- [ ] **Step 6: 回归 + 提交（需授权）**

Run: `cd backend && python -m pytest tests/test_deep_analysis.py -q`
Expected: PASS

```bash
git add backend/app/config.py backend/app/services/agent_orchestrator.py backend/app/workers/analysis_worker.py backend/tests/test_deep_analysis.py
git commit -m "feat(deep): AgentConfig.log_read_cap + orchestrator deep 档（max_turns=40/cap=30）"
```

---

## Task 4: PreToolUse 读取上限 hook（核心防失控）

**Files:**
- Create: `backend/tests/test_log_read_cap_hook.py`
- Modify: `backend/app/agents/claude_code.py`（新增 `_LOG_READ_CAP_SCRIPT` 常量 + `_write_log_read_cap_hook`，在 `analyze` 内 deep 时调用；合并进现有 `.claude/settings.json`）

> 说明：hook 计数脚本逻辑独立、可单测。为可测试，把"判定+计数"核心写成一个纯函数 `_classify_and_count`，hook 脚本和单测都调它。简化做法：单测直接对脚本以子进程喂 stdin JSON 验证 deny。下面用**纯函数单测**（更快更稳）。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_log_read_cap_hook.py`：

```python
"""深度模式 PreToolUse 读取上限 hook：第 N+1 次读 logs/ 被 deny，计数跨调用累加，异常 fail-open。"""
import json
from pathlib import Path
from app.agents.log_read_cap import classify_and_count  # 纯函数，hook 脚本与测试共用


def _ev(tool, **inp):
    return {"tool_name": tool, "tool_input": inp}


def test_read_under_cap_allows(tmp_path: Path):
    counter = tmp_path / ".log_read_count"
    for i in range(1, 31):  # 前 30 次放行
        decision = classify_and_count(_ev("Grep", path="logs/plaud.log", pattern="x"),
                                      counter=counter, cap=30)
        assert decision["allow"] is True, f"read #{i} should pass"
    assert counter.read_text().strip() == "30"


def test_read_over_cap_denies(tmp_path: Path):
    counter = tmp_path / ".log_read_count"
    counter.write_text("30")
    decision = classify_and_count(_ev("Read", file_path="logs/plaud.log"),
                                  counter=counter, cap=30)
    assert decision["allow"] is False
    assert "上限" in decision["reason"]


def test_non_log_tool_not_counted(tmp_path: Path):
    counter = tmp_path / ".log_read_count"
    # 写 result.json / 读 rules 不算日志读取
    assert classify_and_count(_ev("Write", file_path="output/result.json"),
                              counter=counter, cap=30)["allow"] is True
    assert classify_and_count(_ev("Read", file_path="rules/bluetooth.md"),
                              counter=counter, cap=30)["allow"] is True
    assert not counter.exists() or counter.read_text().strip() == "0"


def test_bash_grep_on_logs_counted(tmp_path: Path):
    counter = tmp_path / ".log_read_count"
    d = classify_and_count(_ev("Bash", command="grep -n boot logs/plaud.log"),
                           counter=counter, cap=30)
    assert d["allow"] is True
    assert counter.read_text().strip() == "1"


def test_corrupt_counter_fails_open(tmp_path: Path):
    counter = tmp_path / ".log_read_count"
    counter.write_text("not-a-number")
    d = classify_and_count(_ev("Read", file_path="logs/plaud.log"), counter=counter, cap=30)
    assert d["allow"] is True  # fail-open
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_log_read_cap_hook.py -v`
Expected: FAIL（`app.agents.log_read_cap` 不存在）

- [ ] **Step 3: 写纯函数模块**

Create `backend/app/agents/log_read_cap.py`：

```python
"""深度模式日志读取上限：判定一个工具调用是否在读 logs/，并跨调用累加计数。

hook 脚本（注入到 workspace/.claude/）与单测共用本模块的 classify_and_count。
一切异常一律 fail-open（allow=True），绝不因计数逻辑卡死 agent。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

_LOGS_RE = re.compile(r"(^|[\s/'\"])logs/")


def _is_log_read(event: Dict[str, Any]) -> bool:
    tool = (event.get("tool_name") or "")
    inp = event.get("tool_input") or {}
    if tool in ("Read", "Grep"):
        target = str(inp.get("file_path") or inp.get("path") or "")
        return "logs/" in target or target.strip() in ("logs", "./logs")
    if tool == "Bash":
        return bool(_LOGS_RE.search(str(inp.get("command") or "")))
    return False


def classify_and_count(event: Dict[str, Any], counter: Path, cap: int) -> Dict[str, Any]:
    """返回 {'allow': bool, 'reason': str}。只对"读 logs/"计数；超过 cap 则 deny。"""
    try:
        if not _is_log_read(event):
            return {"allow": True, "reason": ""}
        try:
            n = int(counter.read_text().strip()) if counter.exists() else 0
        except Exception:
            n = 0  # 计数文件损坏 → fail-open，从 0 重数
        n += 1
        counter.write_text(str(n))
        if n > cap:
            return {
                "allow": False,
                "reason": (f"已达日志读取上限（{cap} 次）。请立即基于已有证据写出 "
                           f"output/result.json，不要再 grep 日志。"),
            }
        return {"allow": True, "reason": ""}
    except Exception:
        return {"allow": True, "reason": ""}  # fail-open
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_log_read_cap_hook.py -v`
Expected: PASS（5 条全过）

- [ ] **Step 5: claude_code 注入 PreToolUse hook（deep 时）**

在 `backend/app/agents/claude_code.py`：

(a) 新增 hook 脚本常量（读 stdin → 调 classify_and_count → 输出 deny JSON）。注意脚本在容器内独立运行，需自带 import：

```python
    _LOG_READ_CAP_SCRIPT = r'''import json, os, sys
from pathlib import Path
sys.path.insert(0, "/app")  # 容器内 app 包路径
try:
    from app.agents.log_read_cap import classify_and_count
    event = json.load(sys.stdin)
    counter = Path(os.path.join(os.path.dirname(__file__), ".log_read_count"))
    cap = int(os.environ.get("LOG_READ_CAP", "30"))
    d = classify_and_count(event, counter=counter, cap=cap)
    if not d["allow"]:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": d["reason"],
        }}, ensure_ascii=False))
    sys.exit(0)
except Exception:
    sys.exit(0)  # fail-open
'''
```

(b) 新增 `_write_log_read_cap_hook(workspace, cap)`：把脚本写到 `workspace/.claude/check_log_read.py`，并把 PreToolUse hook **合并进**现有 `.claude/settings.json`（不要覆盖 Stop hook）。matcher 用 `"Read|Grep|Bash"`：

```python
    @staticmethod
    def _write_log_read_cap_hook(workspace: Path, cap: int) -> None:
        try:
            settings_dir = workspace / ".claude"
            settings_dir.mkdir(parents=True, exist_ok=True)
            (settings_dir / "check_log_read.py").write_text(
                ClaudeCodeAgent._LOG_READ_CAP_SCRIPT, encoding="utf-8")
            hook_cmd = f'LOG_READ_CAP={cap} python3 "$(pwd)/.claude/check_log_read.py"'
            settings_path = settings_dir / "settings.json"
            settings = {}
            if settings_path.exists():
                try:
                    settings = json.loads(settings_path.read_text(encoding="utf-8"))
                except Exception:
                    settings = {}
            hooks = settings.setdefault("hooks", {})
            hooks["PreToolUse"] = [{
                "matcher": "Read|Grep|Bash",
                "hooks": [{"type": "command", "command": hook_cmd}],
            }]
            settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to write log-read-cap hook (non-fatal): %s", e)
```

(c) 在 `analyze()` 内、`_write_stop_hook(workspace)` 之后，按 config 调用：

```python
        self._write_stop_hook(workspace)
        if getattr(self.config, "log_read_cap", None):
            self._write_log_read_cap_hook(workspace, int(self.config.log_read_cap))
```

- [ ] **Step 6: ⚠️ 验证 PreToolUse deny JSON 契约**

用当前 CLI 实测 hook 输出格式是否生效（设计已标注的待确认点）。最小验证：在一个临时 workspace 写上述 settings.json + 一个永远 deny 的脚本，跑 `claude -p` 让它 `grep logs/`，看是否被拦。

Run（示例，按实际环境调整）：
```bash
cd backend && python -m pytest tests/test_log_read_cap_hook.py -v   # 纯函数已绿
# 契约验证为人工/集成步骤：若当前 CLI 用旧格式，把脚本输出改为
#   {"decision": "block", "reason": d["reason"]}
```
Expected: 纯函数测试 PASS；契约以实测为准（旧版回退 `decision:block`）。

- [ ] **Step 7: 提交（需授权）**

```bash
git add backend/app/agents/log_read_cap.py backend/app/agents/claude_code.py backend/tests/test_log_read_cap_hook.py
git commit -m "feat(deep): PreToolUse 日志读取上限 hook（默认 30 次，fail-open）"
```

---

## Task 5: 深度模式提示词分支

**Files:**
- Modify: `backend/app/agents/base.py`（build_prompt，约 :143/:192 的 has_logs 分支附近）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_deep_analysis.py`：

```python
def test_deep_prompt_mentions_full_log_and_read_budget():
    from app.agents.base import BaseAgent
    # build_prompt 支持 deep_analysis 参数，且 deep 文案含"完整"和读取预算提示
    import inspect
    assert "deep_analysis" in inspect.signature(BaseAgent.build_prompt).parameters
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_deep_analysis.py::test_deep_prompt_mentions_full_log_and_read_budget -v`
Expected: FAIL（build_prompt 无 deep_analysis 参数）

- [ ] **Step 3: build_prompt 加 deep 分支**

`base.py` `build_prompt` 签名加 `deep_analysis: bool = False`。在 `has_logs` 为真时，若 deep，用如下 role/principles 覆盖标准分支：

```python
        if has_logs and deep_analysis:
            role_and_principles = """你是 Plaud 设备日志分析专家。**深度分析模式**。

你拿到的是**完整、未截断的原始日志**（logs/），不是时间窗口切片。请自由 grep 定位根因。

**读取预算**：你最多可读取日志 30 次（Grep/Read/Bash on logs/）。超出会被系统强制阻止。
因此：先用宽 grep 锁定可疑时间段/关键字，再聚焦细看，**尽快收敛**，不要漫无目的地翻。

分析流程：宽 grep 定位 → 聚焦细看 → 交叉印证 → **必须**写 output/result.json（含中英双语字段）。
证据不足就如实给 low confidence + needs_engineer，禁止编造。"""
            workspace_section = """## 工作空间结构

```
logs/         ← 完整原始日志（未截断），可直接 grep；读取次数有上限（30）
images/       ← 用户截图（如有）
rules/        ← 规则文件
code/         ← 代码仓库（如有）
output/       ← 把 result.json 写到这里
```"""
            extraction_section = "## 说明\n\n深度模式不提供预提取窗口，请自行从完整日志定位证据。"
```

并把 `build_prompt` 的实际调用方（orchestrator 构 prompt 处）传入 `deep_analysis`。若 orchestrator 不直接知道 deep，可经 `condensation_context`/新增参数透传——就近用已透传到 orchestrator 的 `deep_analysis` 传给 build_prompt。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_deep_analysis.py -v`
Expected: PASS

- [ ] **Step 5: 提交（需授权）**

```bash
git add backend/app/agents/base.py backend/app/services/agent_orchestrator.py backend/tests/test_deep_analysis.py
git commit -m "feat(deep): 深度模式提示词（完整日志 + 30 次读取预算）"
```

---

## Task 6: 结果 tag（区分 deep 记录）

**Files:**
- Modify: `backend/app/agents/claude_code.py` 或 `agent_orchestrator.py`：deep 跑出的结果 `agent_type` 标 `claude_code_deep`

> 现有 `save_analysis` 本就先存再判失败（`tasks.py:592` / `:640`），深度结果不会丢，只需让它在记录里可区分。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_deep_analysis.py`（轻量：验证 orchestrator 在 deep 时把结果 agent_type 后缀 `_deep`）：

```python
def test_deep_result_tagged(monkeypatch):
    # 单元级：直接验证打 tag 的小函数
    from app.services.agent_orchestrator import tag_deep_agent_type
    assert tag_deep_agent_type("claude_code", deep=True) == "claude_code_deep"
    assert tag_deep_agent_type("claude_code", deep=False) == "claude_code"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_deep_analysis.py::test_deep_result_tagged -v`
Expected: FAIL

- [ ] **Step 3: 实现 tag 函数 + 应用**

在 `agent_orchestrator.py` 加：

```python
def tag_deep_agent_type(agent_type: str, deep: bool) -> str:
    if deep and agent_type and not agent_type.endswith("_deep"):
        return f"{agent_type}_deep"
    return agent_type
```

在 `run_analysis` 拿到 `result` 后、返回前，若 `deep_analysis`：`result.agent_type = tag_deep_agent_type(result.agent_type, True)`。

- [ ] **Step 4: 跑测试确认通过 + 提交（需授权）**

Run: `cd backend && python -m pytest tests/test_deep_analysis.py -v`
Expected: PASS

```bash
git add backend/app/services/agent_orchestrator.py backend/tests/test_deep_analysis.py
git commit -m "feat(deep): 深度结果 agent_type 标记 _deep 便于对比"
```

---

## Task 7: 前端「深度分析」按钮

**Files:**
- Modify: `frontend/src/lib/api.ts:371`（createTask）
- Modify: `frontend/src/app/page.tsx`（失败态按钮区，约 :1079 「重试」旁）
- Modify: `frontend/src/app/tracking/page.tsx`（约 :547 「重试」旁）
- Modify: `frontend/src/lib/i18n.ts`（新增 key）

- [ ] **Step 1: api.ts createTask 加 deepAnalysis**

```typescript
export const createTask = (issueId: string, agentType?: string, username?: string,
                           followupQuestion?: string, deepAnalysis?: boolean) =>
  request<TaskProgress>(`/tasks`, {
    method: "POST",
    body: JSON.stringify({
      issue_id: issueId,
      agent_type: agentType,
      username,
      followup_question: followupQuestion,
      deep_analysis: deepAnalysis ?? false,
    }),
  });
```

（按现有 createTask body 字段就近补 `deep_analysis`，其余保持。）

- [ ] **Step 2: i18n 加文案**

`frontend/src/lib/i18n.ts` 加：

```typescript
  "深度分析": "Deep Analysis",
  "深度分析中...": "Deep analyzing...",
  "深度分析已启动": "Deep analysis started",
```

- [ ] **Step 3: page.tsx 失败态加按钮**

在 `page.tsx` 失败态「重试」按钮（约 :1079）旁，加一个调用 deep 的按钮。复用现有重试 handler，传 deep=true：

```tsx
<button
  onClick={async () => {
    const task = await createTask(issueId, undefined, username || "", undefined, true);
    setActiveTasks((p) => ({ ...p, [issueId]: task }));
    subscribeTaskProgress(task.task_id, (pr) => setActiveTasks((p) => ({ ...p, [issueId]: pr })));
    setToast(t("深度分析已启动"));
  }}
  className="..."  // 复用重试按钮样式，换个强调色
>
  {t("深度分析")}
</button>
```

（精确套用该文件现有重试按钮的 handler/样式写法，仅 createTask 第 5 参传 true。）

- [ ] **Step 4: tracking/page.tsx 同样加按钮**

在 `tracking/page.tsx` 约 :547「重试」旁，按该文件现有重试逻辑加「深度分析」按钮，`createTask(issueId, undefined, username, undefined, true)`。

- [ ] **Step 5: 前端类型/构建检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无类型错误

- [ ] **Step 6: 提交（需授权）**

```bash
git add frontend/src/lib/api.ts frontend/src/lib/i18n.ts frontend/src/app/page.tsx frontend/src/app/tracking/page.tsx
git commit -m "feat(deep): 失败工单加「深度分析」按钮"
```

---

## Task 8: 端到端回归

- [ ] **Step 1: 后端全量（非 crashguard）**

Run: `cd backend && python -m pytest tests/ -q --ignore=tests/crashguard -p no:cacheprovider`
Expected: 新增的 deep/hook 测试全过；失败数 = 改动前基线（已知 8 个 auth 测试间污染，无新增）。

- [ ] **Step 2: 前端构建**

Run: `cd frontend && npx tsc --noEmit && npm run lint`
Expected: 通过

- [ ] **Step 3: 集成验证（部署后，需授权）**

部署到 102 后，对一个失败工单点「深度分析」，观察：完整日志进 logs/、读取到 30 次被 hook 拦、产出新分析记录。**prod 写动作 + 高峰期禁部署，需明确授权。**

---

## Self-Review

**Spec coverage**：日志输入=全量(Task2 跳窗) ✓；限流=PreToolUse hook 30 次(Task4) ✓；max_turns=40/timeout=1200(Task1/3) ✓；结果新记录+tag(Task6) ✓；门控所有人(前端按钮无角色判断，Task7) ✓；强制 claude_code(Task1 _run_task) ✓；不豁免门禁(未改 tasks.py 判定，沿用) ✓；不新开接口(Task1 复用 TaskCreate) ✓；hook 契约验证(Task4 Step6) ✓；deep 提示词(Task5) ✓。

**Placeholder scan**：无 TBD/TODO；每个代码步骤含真实代码。Task7 Step3/4 引用"现有重试按钮 handler/样式"——执行者需读该文件对齐，已点明行号锚点。

**Type consistency**：`deep_analysis`(后端 bool) / `deepAnalysis`(前端) / `log_read_cap`(int|None) / `classify_and_count(event, counter, cap)` / `tag_deep_agent_type(agent_type, deep)` 在各 Task 间命名一致。

**风险**：PreToolUse JSON 契约(Task4 Step6 已设回退)；hook 脚本 `sys.path.insert("/app")` 依赖容器布局——若本地非 /app，脚本 import 失败会 fail-open(放行)，即上限失效但不崩；执行 Step6 时需确认容器内 app 包路径，必要时把 classify_and_count 逻辑内联进脚本以免依赖 import。
