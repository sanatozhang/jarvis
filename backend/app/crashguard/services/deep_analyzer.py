"""
Phase 1 深度诊断主服务（deep_analyzer）。

与 Phase 2 (analyzer.py 现存) 的关系：
    - Phase 1: 调查 + 提出假设（hypotheses）+ 标注缺数据点（data_gaps），**不出 fix_diff**
    - Phase 2: 在确认假设之后，针对单一根因写真实 patch（复用 analyzer.start_analysis）

入口（异步）：
    run_id = await start_deep_analysis(issue_id)
    status = await get_diagnosis_status(run_id)
    phase2_run = await confirm_hypothesis(run_id, "h1")  # 人工确认 → 跑 Phase 2

工具：诊断 prompt 会引导 AI 通过 Bash 调用 workspace/tools/ 下的 5 个 Python 脚本
（datadog_query / git_blame / git_pickaxe / find_similar / get_session）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.crashguard.models import CrashAnalysis, CrashIssue
from app.crashguard.config import get_crashguard_settings
from app.db.database import get_session

logger = logging.getLogger("crashguard.deep_analyzer")

# fire-and-forget 强引用 set——避免 Python GC 在任务运行中回收
_DIAGNOSIS_TASKS: set = set()


# ---------------------------------------------------------------------------
# 纯函数：快车道判定（独立可测）
# ---------------------------------------------------------------------------

def _should_auto_proceed(
    hypotheses: list, data_gaps: list, threshold: float = 0.9,
) -> bool:
    """快车道条件：单假设 + confidence >= threshold + can_fix_now + no data_gaps。"""
    if len(hypotheses) != 1:
        return False
    h = hypotheses[0]
    try:
        if float(h.get("confidence", 0)) < threshold:
            return False
    except (TypeError, ValueError):
        return False
    if not h.get("can_fix_now", False):
        return False
    if data_gaps:
        return False
    return True


# ---------------------------------------------------------------------------
# Prompt 模板（与 analyzer._PROMPT_TEMPLATE 完全独立）
# ---------------------------------------------------------------------------

_ANR_FREEZE_BLOCK = """
## ⚠️ ANR / Freeze 专项调查指引

此崩溃类型为 **{crash_type}**，堆栈告诉你"卡在哪"但不说"为什么卡"。**必须执行**：

1. 检查主线程调用栈是否含 IO / 网络 / 数据库 / 锁等待操作
2. 用 `python tools/datadog_query.py` 查询同 session 的帧率数据
3. 检查是否有跨线程数据竞争（shared state without synchronization）
4. 如果需要更多数据，在 data_gaps 里给出埋点建议
"""

_DIAGNOSIS_PROMPT_TEMPLATE = """你是 Plaud 移动端崩溃调查专家。你的目标是**深度调查并提出假设**，不是立即给修复代码。

## 待调查的崩溃

- **平台**: {platform}
- **崩溃类型**: {crash_type}
- **标题**: {title}
- **版本范围**: {first_seen_version} – {last_seen_version}
- **首次出现**: {first_seen_at}
- **总事件数**: {total_events}
- **代表性堆栈**:

```
{stack_trace}
```
{enrichment_block}
## 源码导航

{code_hint}
{stack_paths_block}{anr_freeze_block}
## 可用调查工具（通过 Bash 调用，输出 JSON）

```bash
# Datadog RUM 查询（任意 DQL）
python tools/datadog_query.py --dql "<查询语句>" --limit 50

# git blame 单行
python tools/git_blame.py --file "<相对 repo 根的路径>" --line <行号> --repo-path code/<子仓库名>

# 搜索关键词被哪次 commit 引入
python tools/git_pickaxe.py --keyword "<方法名或字符串>" --repo-path code/<子仓库名>

# 查历史相似 crash 的修复经验
python tools/find_similar.py --fingerprint "{stack_fingerprint}"

# 拉完整 RUM session 事件流（崩溃前用户操作路径）
python tools/get_session.py --session-id "<session_id from Datadog>" --limit 100
```

## 调查纪律（**严格执行**）

1. **至少调用 2 个工具**后才能写出结论，不允许空手下结论
2. **必须给 1-5 个假设**，每个假设必须包含来自工具调用或堆栈的具体证据
3. **禁止编造证据**——工具没有返回的信息不能当作证据
4. 若所有假设 confidence < 0.5，**必须**在 data_gaps 里说明缺什么数据、怎么收集
5. 不要在 diagnosis.json 里写 fix_diff——只需要 fix_direction（文字描述修复方向）

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


# ---------------------------------------------------------------------------
# Workspace 准备（独立目录，避免和 Phase 2 冲突）
# ---------------------------------------------------------------------------

def _prepare_diagnosis_workspace(issue_id: str) -> Path:
    """准备 Phase 1 工作目录：

    - 与 Phase 2 工作目录隔离（`_crashguard_diagnosis` vs `_crashguard`）
    - 复制 5 个调查工具脚本到 workspace/tools/
    - 软链 code repo（同 analyzer.py 做法）
    """
    base = Path(os.environ.get("WORKSPACE_DIR", "workspaces")).resolve()
    root = base / "_crashguard_diagnosis"
    safe_id = issue_id.replace("/", "_")
    ws = root / safe_id
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    (ws / "output").mkdir(parents=True, exist_ok=True)

    # 复制 5 个工具脚本
    tools_src = Path(__file__).parent / "diagnosis_tools"
    tools_dst = ws / "tools"
    tools_dst.mkdir(exist_ok=True)
    for script in (
        "datadog_query.py",
        "git_blame.py",
        "git_pickaxe.py",
        "find_similar.py",
        "get_session.py",
    ):
        src = tools_src / script
        if src.exists():
            try:
                shutil.copy2(src, tools_dst / script)
            except OSError as exc:
                logger.warning("copy diagnosis tool %s failed: %s", script, exc)

    # 软链 code repo（与 analyzer 共用约定）
    code_repo = os.environ.get("CODE_REPO_PATH") or os.environ.get("CODE_REPO_APP")
    if code_repo:
        code_repo = os.path.expanduser(code_repo)
        if Path(code_repo).exists():
            try:
                os.symlink(code_repo, ws / "code")
            except OSError as exc:
                logger.warning("diagnosis symlink code repo failed: %s", exc)
    return ws


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_diagnosis_prompt(snapshot: Dict[str, Any]) -> str:
    """构建 Phase 1 prompt。snapshot 已包含 enrichment_block / code_hint /
    stack_paths_block / crash_type / stack_fingerprint 等键。"""
    data = dict(snapshot)
    data.setdefault("enrichment_block", "")
    data.setdefault("code_hint", "")
    data.setdefault("stack_paths_block", "")
    data.setdefault("crash_type", "crash")
    data.setdefault("stack_fingerprint", "")

    crash_type = data.get("crash_type", "crash")
    if crash_type in ("anr", "freeze"):
        data["anr_freeze_block"] = _ANR_FREEZE_BLOCK.format(crash_type=crash_type)
    else:
        data["anr_freeze_block"] = ""

    return _DIAGNOSIS_PROMPT_TEMPLATE.format(**data)


# ---------------------------------------------------------------------------
# diagnosis.json 解析
# ---------------------------------------------------------------------------

def _parse_diagnosis_json(workspace: Path) -> Dict[str, Any]:
    """解析 workspace/output/diagnosis.json。失败/不存在返回 {"_raw": ""}。"""
    target = workspace / "output" / "diagnosis.json"
    if target.exists():
        try:
            text = target.read_text(encoding="utf-8").lstrip("﻿")
            d = json.loads(text)
            d["_raw"] = text[:8000]
            return d
        except Exception as e:
            logger.warning("parse diagnosis.json failed: %s", e)

    # fallback：rglob 兜底（agent 可能写到子目录）
    for cand in workspace.rglob("diagnosis.json"):
        if cand == target:
            continue
        try:
            text = cand.read_text(encoding="utf-8").lstrip("﻿")
            d = json.loads(text)
            d["_raw"] = text[:8000]
            logger.info("found diagnosis.json at fallback path: %s", cand)
            return d
        except Exception:
            continue
    return {"_raw": ""}


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------

async def start_deep_analysis(
    issue_id: str,
    triggered_by: str = "manual",
    force: bool = False,
    dedup_hours: Optional[int] = None,
) -> str:
    """触发 Phase 1 深度诊断，立即返回 run_id，后台异步执行。

    Args:
        issue_id: Datadog issue id
        triggered_by: manual / scheduled / auto
        force: True 跳过 dedup（UI 重跑按钮）
        dedup_hours: 去重窗口（小时）；None = 读 config deep_analysis_dedup_hours

    Raises:
        ValueError: issue 不存在
    """
    async with get_session() as session:
        issue = (await session.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
        )).scalar_one_or_none()
        if issue is None:
            raise ValueError(f"issue {issue_id} not found")

    # dedup：force=False 时复用近窗口内的 success 诊断
    if not force:
        if dedup_hours is None:
            try:
                dedup_hours = int(
                    getattr(get_crashguard_settings(), "deep_analysis_dedup_hours", 6) or 6
                )
            except Exception:
                dedup_hours = 6
        if dedup_hours > 0:
            cutoff = datetime.utcnow() - timedelta(hours=dedup_hours)
            async with get_session() as session:
                latest = (await session.execute(
                    select(CrashAnalysis)
                    .where(CrashAnalysis.datadog_issue_id == issue_id)
                    .where(CrashAnalysis.phase == "diagnosis")
                    .where(CrashAnalysis.status == "success")
                    .where(CrashAnalysis.created_at >= cutoff)
                    .order_by(CrashAnalysis.created_at.desc())
                    .limit(1)
                )).scalar_one_or_none()
                if latest is not None:
                    logger.info(
                        "start_deep_analysis dedup hit: issue=%s reusing run_id=%s (age=%.1fh)",
                        issue_id, latest.analysis_run_id,
                        (datetime.utcnow() - latest.created_at).total_seconds() / 3600.0,
                    )
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
            crash_type="",
            hypotheses="[]",
            data_gaps="[]",
            investigation_log="[]",
            confirmed_hypothesis_id="",
            parent_diagnosis_run_id="",
            created_at=datetime.utcnow(),
        ))
        await session.commit()

    task = asyncio.create_task(_run_diagnosis_in_background(issue_id, run_id))
    _DIAGNOSIS_TASKS.add(task)
    task.add_done_callback(_DIAGNOSIS_TASKS.discard)
    return run_id


async def get_diagnosis_status(run_id: str) -> Optional[Dict[str, Any]]:
    """查 Phase 1 诊断状态。返回 None 表示 run_id 不存在。"""
    async with get_session() as session:
        row = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == run_id)
        )).scalar_one_or_none()
        if row is None:
            return None

        def _safe_json(s: str, default):
            try:
                v = json.loads(s or "")
                return v
            except (ValueError, TypeError):
                return default

        hypotheses = _safe_json(row.hypotheses, [])
        if not isinstance(hypotheses, list):
            hypotheses = []
        data_gaps = _safe_json(row.data_gaps, [])
        if not isinstance(data_gaps, list):
            data_gaps = []
        investigation_log = _safe_json(row.investigation_log, [])
        if not isinstance(investigation_log, list):
            investigation_log = []

        return {
            "run_id": row.analysis_run_id,
            "datadog_issue_id": row.datadog_issue_id,
            "phase": row.phase or "diagnosis",
            "status": row.status or "pending",
            "crash_type": row.crash_type or "",
            "hypotheses": hypotheses,
            "data_gaps": data_gaps,
            "investigation_log": investigation_log,
            "confirmed_hypothesis_id": row.confirmed_hypothesis_id or "",
            "parent_diagnosis_run_id": row.parent_diagnosis_run_id or "",
            "feasibility_score": float(row.feasibility_score or 0.0),
            "overall_confidence": float(row.feasibility_score or 0.0),    # 与 diagnosis.json 命名一致
            "recommended_hypothesis": getattr(row, "recommended_hypothesis", "") or "",
            "confidence": row.confidence or "",
            "agent_name": row.agent_name or "",
            "agent_model": row.agent_model or "",
            "error": row.error or "",
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


async def confirm_hypothesis(run_id: str, hypothesis_id: str) -> str:
    """人工确认假设——回写 confirmed_hypothesis_id，触发 Phase 2。

    Returns:
        phase2_run_id: 新建的 Phase 2 分析 run_id
    """
    async with get_session() as session:
        row = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == run_id)
        )).scalar_one_or_none()
        if row is None:
            raise ValueError(f"diagnosis run {run_id} not found")
        if (row.phase or "") != "diagnosis":
            raise ValueError(f"run {run_id} is not a diagnosis run (phase={row.phase})")
        row.confirmed_hypothesis_id = hypothesis_id
        await session.commit()

    return await start_fix_analysis(
        diagnosis_run_id=run_id, hypothesis_id=hypothesis_id, approver="human",
    )


async def start_fix_analysis(
    diagnosis_run_id: str, hypothesis_id: str, approver: str = "human",
) -> str:
    """基于已确认假设触发 Phase 2 修复分析（调用现有 analyzer.start_analysis）。

    Phase 2 复用现有 fix 流程；通过 parent_diagnosis_run_id 关联回 Phase 1。
    Task 6 会在 analyzer 里读这个字段，把假设 context 注入 fix prompt。
    """
    from app.crashguard.services.analyzer import start_analysis

    async with get_session() as session:
        diag_row = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == diagnosis_run_id)
        )).scalar_one_or_none()
        if diag_row is None:
            raise ValueError(f"diagnosis run {diagnosis_run_id} not found")
        issue_id = diag_row.datadog_issue_id

    # force=True 防止 Phase 2 复用了别的 fix 分析（dedup 在 fix 阶段需要绕过）
    triggered_by = f"phase1_{approver}"
    phase2_run_id = await start_analysis(
        issue_id=issue_id,
        triggered_by=triggered_by,
        force=True,
    )

    # 关联 Phase 2 → Phase 1
    async with get_session() as session:
        p2 = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == phase2_run_id)
        )).scalar_one_or_none()
        if p2 is not None:
            p2.parent_diagnosis_run_id = diagnosis_run_id
            await session.commit()

    logger.info(
        "fix analysis triggered: diagnosis=%s hypothesis=%s phase2_run=%s by=%s",
        diagnosis_run_id, hypothesis_id, phase2_run_id, approver,
    )
    return phase2_run_id


# ---------------------------------------------------------------------------
# 后台任务
# ---------------------------------------------------------------------------

async def _update_status(run_id: str, **fields) -> None:
    """泛用字段更新（status / crash_type / error / ...）。"""
    async with get_session() as session:
        row = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == run_id)
        )).scalar_one_or_none()
        if row is None:
            return
        for k, v in fields.items():
            if hasattr(row, k):
                setattr(row, k, v)
        await session.commit()


async def _update_failed(run_id: str, err: str) -> None:
    await _update_status(run_id, status="failed", error=err[:1000])


def _issue_to_snapshot(issue: CrashIssue) -> Dict[str, Any]:
    return {
        "platform": issue.platform or "—",
        "service": issue.service or "—",
        "title": issue.title or "—",
        "first_seen_version": issue.first_seen_version or "—",
        "last_seen_version": issue.last_seen_version or "—",
        "first_seen_at": issue.first_seen_at.isoformat() if issue.first_seen_at else "—",
        "last_seen_at": issue.last_seen_at.isoformat() if issue.last_seen_at else "—",
        "total_events": issue.total_events or 0,
        "stack_trace": (issue.representative_stack or "")[:32000],
        "stack_fingerprint": getattr(issue, "stack_fingerprint", "") or "",
    }


async def _run_diagnosis_in_background(issue_id: str, run_id: str) -> None:
    """Phase 1 主流程：分类 → 准备 ws → 跑 agent → 解析 → 写回 DB → 可能触发快车道 Phase 2。"""
    try:
        await _update_status(run_id, status="running")

        async with get_session() as session:
            issue = (await session.execute(
                select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
            )).scalar_one_or_none()
            if issue is None:
                await _update_failed(run_id, "issue not found")
                return
            snapshot = _issue_to_snapshot(issue)

        # 1. 预分类 crash_type
        from app.crashguard.services.crash_type_classifier import classify_crash_type
        crash_type = classify_crash_type(
            snapshot.get("title", ""),
            snapshot.get("stack_trace", ""),
            {"platform": snapshot.get("platform", "")},
        )
        snapshot["crash_type"] = crash_type
        await _update_status(run_id, crash_type=crash_type)

        # 2. 准备 workspace（独立目录）
        workspace = _prepare_diagnosis_workspace(issue_id)

        # 3. 复用 analyzer 的 enrichment / code_hint / stack_paths 辅助
        from app.crashguard.services.analyzer import (
            _build_enrichment_block,
            _platform_code_hint,
            _build_stack_paths_block,
        )
        snapshot["enrichment_block"] = await _build_enrichment_block(issue_id)
        snapshot["code_hint"] = _platform_code_hint(snapshot.get("platform", ""), workspace)
        snapshot["stack_paths_block"] = _build_stack_paths_block(
            snapshot.get("stack_trace", ""),
            snapshot.get("platform", ""),
            workspace,
        )

        # 4. 构建 prompt
        prompt = _build_diagnosis_prompt(snapshot)
        try:
            (workspace / "prompt.md").write_text(prompt, encoding="utf-8")
        except Exception:
            pass

        # 5. 调 agent（带超时 + 一次 retry）
        parsed = await _run_diagnosis_agent(workspace, prompt)
        if not parsed.get("hypotheses"):
            # 第一次没拿到 hypotheses → 显式 reminder 再跑一次
            logger.warning(
                "diagnosis.json missing or empty hypotheses on first run, retrying once with reminder"
            )
            retry_prompt = (
                "⚠️ **上一次执行没有把 diagnosis.json 写到 `output/` 目录**，或假设列表为空。\n\n"
                "本次重试必须**首先用 Write 工具**创建 `output/diagnosis.json`，"
                "且 hypotheses 数组至少 1 条。基于已有上下文产出最佳假设即可。\n\n"
                "完整原始任务如下（再读一遍恢复上下文）：\n\n"
            ) + prompt
            parsed_retry = await _run_diagnosis_agent(workspace, retry_prompt)
            if parsed_retry.get("hypotheses"):
                parsed = parsed_retry

        # 6. 持久化
        hypotheses = parsed.get("hypotheses") or []
        if not isinstance(hypotheses, list):
            hypotheses = []
        data_gaps = parsed.get("data_gaps") or []
        if not isinstance(data_gaps, list):
            data_gaps = []
        investigation_log = parsed.get("investigation_log") or []
        if not isinstance(investigation_log, list):
            investigation_log = []
        overall_conf = float(parsed.get("overall_confidence") or 0.0)
        recommended = parsed.get("recommended_hypothesis") or ""
        agent_name = parsed.get("_agent_name", "")
        agent_model = parsed.get("_agent_model", "")
        raw_output = parsed.get("_raw", "")[:8000]

        # 状态判定：空假设 = empty；有假设 = success
        if not hypotheses:
            new_status = "empty"
        else:
            new_status = "success"

        async with get_session() as session:
            row = (await session.execute(
                select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == run_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.hypotheses = json.dumps(hypotheses[:10], ensure_ascii=False)
            row.data_gaps = json.dumps(data_gaps, ensure_ascii=False)
            row.investigation_log = json.dumps(investigation_log, ensure_ascii=False)
            row.feasibility_score = overall_conf
            row.recommended_hypothesis = recommended  # 使用已有的 recommended 变量
            row.confidence = (
                "high" if overall_conf >= 0.7
                else ("medium" if overall_conf >= 0.4 else "low")
            )
            row.agent_name = agent_name or row.agent_name or ""
            row.agent_model = agent_model or row.agent_model or ""
            row.agent_raw_output = raw_output
            row.status = new_status
            row.crash_type = parsed.get("crash_type") or crash_type
            error_msg = parsed.get("_error")
            if error_msg:
                row.error = str(error_msg)[:1000]
            await session.commit()

        # 7. 快车道：单假设 + 高置信 + can_fix_now + 无 data_gaps → 直接触发 Phase 2
        try:
            s = get_crashguard_settings()
            threshold = float(
                getattr(s, "deep_analysis_auto_proceed_threshold", 0.9) or 0.9
            )
        except Exception:
            threshold = 0.9

        if new_status == "success" and _should_auto_proceed(
            hypotheses, data_gaps, threshold=threshold
        ):
            try:
                phase2_run = await start_fix_analysis(
                    diagnosis_run_id=run_id,
                    hypothesis_id=hypotheses[0].get("id", "h1"),
                    approver="auto",
                )
                logger.info(
                    "auto_proceed triggered: diagnosis=%s phase2=%s confidence=%.2f",
                    run_id, phase2_run, float(hypotheses[0].get("confidence", 0)),
                )
            except Exception:
                logger.exception("auto_proceed_to_fix failed (non-fatal)")

    except Exception as e:
        logger.exception("background diagnosis failed run_id=%s", run_id)
        try:
            await _update_failed(run_id, str(e))
        except Exception:
            pass


async def _run_diagnosis_agent(workspace: Path, prompt: str) -> Dict[str, Any]:
    """调 agent → 解析 diagnosis.json。失败时返回 {"_error": ..., "_raw": ""}。

    agent_name / agent_model 通过 `_agent_name` / `_agent_model` 键回带。
    """
    from app.services.agent_orchestrator import AgentOrchestrator

    try:
        timeout = int(
            getattr(get_crashguard_settings(), "deep_analysis_timeout_seconds", 1800) or 1800
        )
    except Exception:
        timeout = 1800

    orch = AgentOrchestrator()
    try:
        agent = orch.select_agent(rule_type="crashguard")
    except RuntimeError as e:
        return {"_error": f"agent unavailable: {e}", "_raw": ""}

    started = time.time()
    try:
        await asyncio.wait_for(
            agent.analyze(workspace=workspace, prompt=prompt),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        elapsed = time.time() - started
        logger.error("diagnosis agent timed out after %.0fs (cap=%ds)", elapsed, timeout)
        # 即使超时也尝试解析 diagnosis.json
        parsed = _parse_diagnosis_json(workspace)
        if parsed.get("hypotheses"):
            parsed["_agent_name"] = getattr(agent.config, "agent_type", "claude_code")
            parsed["_agent_model"] = getattr(agent.config, "model", "") or ""
            return parsed
        return {
            "_error": f"diagnosis hard timeout after {timeout}s",
            "_raw": "",
            "_agent_name": getattr(agent.config, "agent_type", "claude_code"),
            "_agent_model": getattr(agent.config, "model", "") or "",
        }
    except Exception as e:
        logger.exception("diagnosis agent.analyze raised")
        return {
            "_error": f"agent failed: {e}",
            "_raw": "",
            "_agent_name": getattr(agent.config, "agent_type", ""),
            "_agent_model": getattr(agent.config, "model", "") or "",
        }

    elapsed = time.time() - started
    logger.info("diagnosis agent finished in %.1fs", elapsed)

    parsed = _parse_diagnosis_json(workspace)
    parsed["_agent_name"] = getattr(agent.config, "agent_type", "")
    parsed["_agent_model"] = getattr(agent.config, "model", "") or ""
    return parsed
