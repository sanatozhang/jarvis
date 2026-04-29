"""
Crashguard AI 分析器 (Plan 2 MVP)。

输入: CrashIssue (title + representative_stack + 版本范围)
输出: CrashAnalysis 一条记录 (root_cause, fix_suggestion, feasibility, confidence)

复用 jarvis 的 agent_orchestrator → ClaudeCodeAgent (subprocess + workspace)，
但 prompt 自建（不走工单分析 build_prompt）。

异步模式（推荐）：
    run_id = await start_analysis(issue_id)        # 立即返回
    while True:
        st = await get_analysis_status(run_id)     # 轮询
        if st["status"] in ("success","failed","empty"): break

同步模式（兼容旧调用）：
    output = await analyze_issue(issue_id)         # 等到跑完
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import select

from app.crashguard.models import CrashIssue, CrashAnalysis
from app.crashguard.config import get_crashguard_settings
from app.db.database import get_session

logger = logging.getLogger("crashguard.analyzer")


@dataclass
class AnalysisOutput:
    scenario: str
    root_cause: str
    fix_suggestion: str
    feasibility_score: float
    confidence: str
    reproducibility: str
    raw_output: str
    agent_name: str
    agent_model: str = ""
    possible_causes: list = None  # type: ignore[assignment]
    complexity_kind: str = ""
    solution: str = ""
    hint: str = ""
    answer: str = ""
    fix_diff: str = ""
    error: Optional[str] = None

    def __post_init__(self):
        if self.possible_causes is None:
            self.possible_causes = []


_PROMPT_TEMPLATE = """你是 Plaud 移动端崩溃分析专家。基于下方崩溃信息 + 真实源码给出深度分析。

## 待分析的崩溃

- **平台**: {platform}
- **服务**: {service}
- **标题**: {title}
- **版本范围**: {first_seen_version} – {last_seen_version}
- **首次出现**: {first_seen_at}
- **最近出现**: {last_seen_at}
- **总事件数**: {total_events}
- **代表性堆栈**:

```
{stack_trace}
```
{enrichment_block}
## 源码导航（极重要）

{code_hint}
- **优先用 Read / Glob / Grep 工具**到上述目录里查阅真实代码，再下结论
- 堆栈里的 `package:plaud_flutter_common/...dart:行号` 一律去 `code/plaud-flutter-common/lib/` 找对应文件
- 看到 file:line 必须验证那一行真的存在；不存在则在 root_cause 里说明
{followup_block}
## 任务（输出 JSON）

完成分析后将 JSON 写入 `output/result.json`：

```json
{{
  "scenario": "崩溃发生的具体场景（用户在做什么、设备状态、3-5 句话）",
  "possible_causes": [
    {{
      "title": "原因 A 的简短标题（10-20 字）",
      "evidence": "证据：堆栈第 N 帧 / 分布信号 / 源码文件:行号",
      "confidence": "high | medium | low",
      "code_pointer": "如能定位则给 file_path:line，否则空串"
    }},
    {{ "title": "原因 B（可选）", "evidence": "...", "confidence": "...", "code_pointer": "..." }}
  ],
  "complexity": "simple | complex",
  "solution": "complexity=simple 时填：直接给 patch 或可执行步骤，含具体代码改动（file_path:line + diff）",
  "hint": "complexity=complex 时填：给开发者的排查思路（不直接改代码），分点列出",
  "root_cause": "把 possible_causes 中最可能那条扩展为 5-10 句的根因总结（保留以兼容旧字段）",
  "fix_suggestion": "complexity=simple 直接复制 solution；complexity=complex 复制 hint",
  "fix_diff": "**unified diff 格式的真实 patch**（complexity=simple 时强烈建议；下方有详细规则）",
  "feasibility_score": 0.0,
  "confidence": "high | medium | low",
  "reproducibility": "reproducible | likely | unknown"
}}
```

### fix_diff 输出规则（极重要 —— 决定能否自动开 PR）

- **complexity=simple 时必须给**；complexity=complex 或缺数据时给空串 `""`
- 必须是**可被 `git apply --3way` 应用的 unified diff**，文件头格式：
  ```
  --- a/<相对子仓库根的路径>
  +++ b/<相对子仓库根的路径>
  @@ -原行,数 +新行,数 @@ 上下文
  -原代码
  +新代码
  ```
- **路径规则**：移除 `code/<sub-repo>/` 前缀。例：源码在 `code/plaud-flutter-common/lib/foo.dart`，diff 里写 `lib/foo.dart`。每个端的子仓库都是独立 git repo，patch 是相对它们各自根的
- **必须真去 Read 那个文件确认行号和上下文**，行号错了 patch 就 apply 不了
- **宁缺毋滥**：不确定行号/不确定该改哪行 → 把 fix_diff 留空，让工程师手动 patch；编造 diff 比留空伤害更大
- 单个 diff 控制在 30 行以内，跨多文件可拼接（连续多个 `--- a/...` 块）

### possible_causes 输出规则

- **必须给 1-3 条**；只有 1 条时也要用数组形式
- 按 confidence 从高到低排序
- 每条 evidence 要具体——"堆栈第 X 帧 → 文件:行" / "App 版本分布 100% 在 3.15.x → 此版本引入" / "100% 触发于 /home → 与首页有关"
- 如能 file_path:line 定位则填 code_pointer

### complexity 判定

- **simple**：根因明确（单文件单函数级），改动少（≤30 行），直接给 patch
- **complex**：跨模块 / 涉及生命周期竞态 / 缺乏数据无法 100% 定位 → 给排查思路

### feasibility_score 评分标准

- 0.9-1.0：源码中明确定位到 bug，单测可覆盖
- 0.7-0.9：根因明确，但需进一步验证
- 0.5-0.7：合理猜测，缺乏直接代码证据
- 0.0-0.5：信息不足，仅能给方向

### 输出要求

- **必须**用 Write 工具将 JSON 写入 `output/result.json`，不写文件 = 分析失败
- 中文输出，技术细节准确，避免空话
- 优先读源码再做判断，不要凭印象编造文件路径
"""


def _safe_workspace_root() -> Path:
    s = get_crashguard_settings()
    base = Path(os.environ.get("WORKSPACE_DIR", "workspaces")).resolve()
    return base / "_crashguard"


# ---------------------------------------------------------------------------
# 异步入口（推荐）
# ---------------------------------------------------------------------------

async def start_analysis(
    issue_id: str,
    triggered_by: str = "manual",
    followup_question: str = "",
    parent_run_id: str = "",
) -> str:
    """
    异步启动一次分析。立即返回 run_id；后台 task 跑完后更新同一行。

    Args:
        followup_question: 非空时进入"追问模式"——prompt 会拼上前序所有分析作为 context
        parent_run_id: 追问基于的 run_id（默认取最新成功的）

    Raises:
        ValueError: issue 不存在
    """
    async with get_session() as session:
        issue = (await session.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
        )).scalar_one_or_none()
        if issue is None:
            raise ValueError(f"issue {issue_id} not found")

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
            followup_question=followup_question or "",
            parent_run_id=parent_run_id or "",
            answer="",
            created_at=datetime.utcnow(),
        ))
        await session.commit()

    asyncio.create_task(_run_in_background(issue_id, run_id))
    return run_id


async def get_analysis_status(run_id: str) -> Optional[Dict[str, Any]]:
    """按 run_id 查最新状态。返回 None 表示 run_id 不存在。"""
    async with get_session() as session:
        row = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == run_id)
        )).scalar_one_or_none()
        if row is None:
            return None
        try:
            causes = json.loads(row.possible_causes or "[]")
            if not isinstance(causes, list):
                causes = []
        except (ValueError, TypeError):
            causes = []
        return {
            "run_id": row.analysis_run_id,
            "datadog_issue_id": row.datadog_issue_id,
            "status": row.status or "pending",
            "scenario": row.scenario or "",
            "root_cause": row.root_cause or "",
            "fix_suggestion": row.fix_suggestion or "",
            "feasibility_score": float(row.feasibility_score or 0.0),
            "confidence": row.confidence or "",
            "reproducibility": row.reproducibility or "",
            "agent_name": row.agent_name or "",
            "agent_model": row.agent_model or "",
            "possible_causes": causes,
            "complexity_kind": row.complexity_kind or "",
            "solution": row.solution or "",
            "hint": row.hint or "",
            "followup_question": row.followup_question or "",
            "parent_run_id": row.parent_run_id or "",
            "answer": row.answer or "",
            "is_followup": bool((row.followup_question or "").strip()),
            "error": row.error or "",
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


async def _run_in_background(issue_id: str, run_id: str) -> None:
    """后台 asyncio.Task：跑 agent → 更新 DB 同一行。

    支持两种模式：
        - 首次分析（followup_question 为空）：完整 prompt + 多根因 schema
        - 追问（followup_question 非空）：拼上前序所有 success 分析的精华作为 context，要求 AI 回答用户具体问题
    """
    try:
        await _update_status(run_id, status="running")

        # 取本轮的 row 确定 mode
        async with get_session() as session:
            run = (await session.execute(
                select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == run_id)
            )).scalar_one_or_none()
            issue = (await session.execute(
                select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
            )).scalar_one_or_none()
            if run is None or issue is None:
                await _update_failed(run_id, "issue or run not found")
                return
            snapshot_data = _issue_to_dict(issue)
            is_followup = bool((run.followup_question or "").strip())
            followup_q = run.followup_question or ""

        snapshot_data["enrichment_block"] = await _build_enrichment_block(issue_id)
        workspace = _prepare_workspace(issue_id)
        snapshot_data["code_hint"] = _platform_code_hint(snapshot_data.get("platform", ""), workspace)

        if is_followup:
            snapshot_data["followup_block"] = await _build_followup_block(issue_id, followup_q)
            prompt = _build_followup_prompt(snapshot_data)
        else:
            snapshot_data["followup_block"] = ""
            prompt = _build_prompt(snapshot_data)

        try:
            (workspace / "prompt.md").write_text(prompt, encoding="utf-8")
        except Exception:
            pass

        output = await _run_agent(workspace, prompt, is_followup=is_followup)
        output.raw_output = output.raw_output[:8000] if output.raw_output else ""

        await _persist_to_run(run_id, output, is_followup=is_followup)
    except Exception as e:
        logger.exception("background analysis failed run_id=%s", run_id)
        try:
            await _update_failed(run_id, str(e))
        except Exception:
            pass


async def _update_status(run_id: str, status: str) -> None:
    async with get_session() as session:
        row = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == run_id)
        )).scalar_one_or_none()
        if row is None:
            return
        row.status = status
        await session.commit()


async def _update_failed(run_id: str, err: str) -> None:
    async with get_session() as session:
        row = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == run_id)
        )).scalar_one_or_none()
        if row is None:
            return
        row.status = "failed"
        row.error = err[:1000]
        await session.commit()


async def _persist_to_run(run_id: str, output: AnalysisOutput, is_followup: bool = False) -> None:
    analysis_id_for_pr: Optional[int] = None
    feasibility_for_pr: float = 0.0
    async with get_session() as session:
        row = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.analysis_run_id == run_id)
        )).scalar_one_or_none()
        if row is None:
            return
        if is_followup:
            row.answer = output.answer or output.root_cause or ""
            row.feasibility_score = output.feasibility_score
            row.confidence = output.confidence
            row.agent_raw_output = output.raw_output
            row.agent_name = output.agent_name or row.agent_name or ""
            row.agent_model = output.agent_model or row.agent_model or ""
            if output.error:
                row.status = "failed"
                row.error = output.error[:1000]
            else:
                row.status = "success" if (row.answer or "").strip() else "empty"
        else:
            row.scenario = output.scenario
            row.root_cause = output.root_cause
            row.fix_suggestion = output.fix_suggestion
            row.feasibility_score = output.feasibility_score
            row.confidence = output.confidence
            row.reproducibility = output.reproducibility
            row.agent_raw_output = output.raw_output
            row.agent_name = output.agent_name or row.agent_name or ""
            row.agent_model = output.agent_model or row.agent_model or ""
            row.possible_causes = json.dumps(output.possible_causes, ensure_ascii=False)
            row.complexity_kind = output.complexity_kind or ""
            row.solution = output.solution or ""
            row.hint = output.hint or ""
            row.fix_diff = output.fix_diff or ""
            if output.error:
                row.status = "failed"
                row.error = output.error[:1000]
            else:
                row.status = "success" if output.root_cause else "empty"
            # 根分析成功 + 可行性达标 → 触发自动 draft PR（fire-and-forget）
            if row.status == "success":
                analysis_id_for_pr = row.id
                feasibility_for_pr = float(output.feasibility_score or 0.0)
        await session.commit()

    if analysis_id_for_pr is not None:
        try:
            asyncio.create_task(_maybe_auto_draft_pr(analysis_id_for_pr, feasibility_for_pr))
        except RuntimeError:
            # 没有事件循环（同步入口），直接同步跑一次
            try:
                await _maybe_auto_draft_pr(analysis_id_for_pr, feasibility_for_pr)
            except Exception:
                logger.exception("auto draft PR fallback failed")


async def _maybe_auto_draft_pr(analysis_id: int, feasibility: float) -> None:
    """根分析成功后的自动 PR 勾子。检查阈值 + 写 audit log，PR 失败不影响分析主流程。"""
    from app.crashguard.config import get_crashguard_settings
    from app.crashguard.services.audit import write_audit
    s = get_crashguard_settings()
    if not s.pr_enabled:
        return
    threshold = float(getattr(s, "feasibility_pr_threshold", 0.7) or 0.7)
    if feasibility < threshold:
        await write_audit(
            op="auto_draft_pr",
            target_id=str(analysis_id),
            success=False,
            detail=f"feasibility={feasibility:.2f} < threshold={threshold:.2f}",
            error="below_threshold",
        )
        return
    try:
        from app.crashguard.services.pr_drafter import draft_pr_for_analysis
        result = await draft_pr_for_analysis(analysis_id, approver="auto")
        await write_audit(
            op="auto_draft_pr",
            target_id=str(analysis_id),
            success=bool(result.get("ok")),
            detail=str({k: v for k, v in result.items() if k != "raw"})[:500],
            error=result.get("error", "") if not result.get("ok") else None,
        )
    except Exception as exc:
        logger.exception("auto_draft_pr crashed")
        try:
            await write_audit(
                op="auto_draft_pr",
                target_id=str(analysis_id),
                success=False,
                error=str(exc)[:300],
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 同步入口（兼容旧调用 / CLI 用）
# ---------------------------------------------------------------------------

async def analyze_issue(issue_id: str) -> AnalysisOutput:
    """同步：拉一条 issue → 跑 agent → 写 CrashAnalysis 表 → 返回结果。"""
    async with get_session() as session:
        issue = (await session.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
        )).scalar_one_or_none()
        if issue is None:
            raise ValueError(f"issue {issue_id} not found")
        snapshot_data = _issue_to_dict(issue)

    snapshot_data["enrichment_block"] = await _build_enrichment_block(issue_id)
    workspace = _prepare_workspace(issue_id)
    snapshot_data["code_hint"] = _platform_code_hint(snapshot_data.get("platform", ""), workspace)
    prompt = _build_prompt(snapshot_data)
    try:
        (workspace / "prompt.md").write_text(prompt, encoding="utf-8")
    except Exception:
        pass

    output = await _run_agent(workspace, prompt)
    output.raw_output = output.raw_output[:8000] if output.raw_output else ""

    await _persist_analysis_legacy(issue_id, output)
    return output


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _issue_to_dict(issue: CrashIssue) -> Dict[str, Any]:
    return {
        "platform": issue.platform or "—",
        "service": issue.service or "—",
        "title": issue.title or "—",
        "first_seen_version": issue.first_seen_version or "—",
        "last_seen_version": issue.last_seen_version or "—",
        "first_seen_at": issue.first_seen_at.isoformat() if issue.first_seen_at else "—",
        "last_seen_at": issue.last_seen_at.isoformat() if issue.last_seen_at else "—",
        "total_events": issue.total_events or 0,
        "stack_trace": (issue.representative_stack or "")[:8000],
    }


def _build_prompt(d: Dict[str, Any]) -> str:
    data = dict(d)
    data.setdefault("enrichment_block", "")
    data.setdefault("code_hint", "")
    data.setdefault("followup_block", "")
    return _PROMPT_TEMPLATE.format(**data)


_FOLLOWUP_PROMPT_TEMPLATE = """你是 Plaud 移动端崩溃分析专家。**这是一次追问**——之前已有完整分析，用户基于现有结论提出新问题，请简洁聚焦地回答。

## 待分析的崩溃（基础信息）

- **平台**: {platform}
- **服务**: {service}
- **标题**: {title}
- **版本范围**: {first_seen_version} – {last_seen_version}
- **总事件数**: {total_events}
{enrichment_block}
## 源码导航

{code_hint}

{followup_block}

## 任务

读取相关源码（用 Read/Grep）后，**只回答用户追问**，不重做完整根因分析。完成后将 JSON 写入 `output/result.json`：

```json
{{
  "answer": "对用户追问的直接、聚焦回答（200-800 字，markdown 格式，含具体 file:line / 代码片段）",
  "confidence": "high | medium | low",
  "feasibility_score": 0.0
}}
```

### 输出要求

- **必须**用 Write 工具写 `output/result.json`
- answer 字段必须是对追问的**直接回答**，不要复述前序根因
- 中文 + markdown，能列代码片段就列
- 如追问超出可分析范围（如要求复现脚本但缺设备），明确说明
"""


def _build_followup_prompt(d: Dict[str, Any]) -> str:
    data = dict(d)
    data.setdefault("enrichment_block", "")
    data.setdefault("code_hint", "")
    data.setdefault("followup_block", "")
    return _FOLLOWUP_PROMPT_TEMPLATE.format(**data)


async def _build_followup_block(issue_id: str, followup_question: str) -> str:
    """拼接前序所有 success 分析（按时间正序）+ 当前用户追问，作为 prompt 上下文。"""
    async with get_session() as session:
        prior = (await session.execute(
            select(CrashAnalysis)
            .where(
                CrashAnalysis.datadog_issue_id == issue_id,
                CrashAnalysis.status == "success",
            )
            .order_by(CrashAnalysis.created_at)
        )).scalars().all()

    parts = ["## 历史分析记录（按时间正序）\n"]
    for i, r in enumerate(prior, 1):
        if r.followup_question:
            parts.append(f"\n### 第 {i} 轮：用户追问")
            parts.append(f"> {r.followup_question}")
            parts.append(f"\n**AI 回答**：")
            parts.append(r.answer or "(无)")
        else:
            parts.append(f"\n### 第 {i} 轮：首次分析")
            if r.scenario:
                parts.append(f"\n**场景**：{r.scenario[:400]}")
            if r.root_cause:
                parts.append(f"\n**根因摘要**：{r.root_cause[:600]}")
            if r.possible_causes:
                try:
                    causes = json.loads(r.possible_causes or "[]")
                    if causes:
                        parts.append("\n**可能原因**：")
                        for j, c in enumerate(causes[:3], 1):
                            parts.append(f"  {j}. [{c.get('confidence','?')}] {c.get('title','')} — {c.get('code_pointer','')}")
                except Exception:
                    pass
            if r.fix_suggestion:
                parts.append(f"\n**修复建议摘要**：{r.fix_suggestion[:400]}")
        parts.append("")

    parts.append("\n## 用户的本轮追问（请聚焦回答这一条）\n")
    parts.append(f"> {followup_question.strip()}")
    parts.append("")
    return "\n".join(parts)


def _prepare_workspace(issue_id: str) -> Path:
    root = _safe_workspace_root()
    safe_id = issue_id.replace("/", "_")
    ws = root / safe_id
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    (ws / "output").mkdir(parents=True, exist_ok=True)
    # 软链整个 Plaud2 monorepo（含 plaud-flutter-common / plaud-android / plaud-ios 等）
    code_repo = os.environ.get("CODE_REPO_PATH") or os.environ.get("CODE_REPO_APP")
    if code_repo:
        code_repo = os.path.expanduser(code_repo)
        if Path(code_repo).exists():
            try:
                os.symlink(code_repo, ws / "code")
            except OSError as exc:
                logger.warning("symlink code repo failed: %s", exc)
    return ws


def _platform_code_hint(platform: str, workspace: Path) -> str:
    """运行时扫描 workspace/code/ 真实子目录，按 platform 关键字筛出最相关的几个。

    部署环境的目录命名可能与开发环境不同（plaud-* / app-* / mobile-* 等），
    所以只用关键字模糊匹配，不写死。AI 拿到真实子目录后自行用 Glob/Grep 探索。
    """
    code_root = workspace / "code"
    if not code_root.exists():
        return "⚠️ 没有源码目录可用（CODE_REPO_PATH 未配置）。请基于堆栈和分布信号给出最佳猜测。\n"

    try:
        subdirs = sorted(
            [d.name for d in code_root.iterdir() if d.is_dir() and not d.name.startswith(".")],
        )
    except OSError:
        subdirs = []

    if not subdirs:
        return "**源码根目录**：`code/`（顶层未发现子目录，自行用 Glob 探索）\n"

    p = (platform or "").strip().lower()
    keyword_map = {
        "flutter": ("flutter", "dart"),
        "android": ("android", "kotlin"),
        "ios": ("ios", "swift"),
    }
    keywords = keyword_map.get(p, ())

    primary, secondary = [], []
    for name in subdirs:
        lower = name.lower()
        if any(kw in lower for kw in keywords):
            primary.append(name)
        else:
            secondary.append(name)

    lines = ["**源码根目录已软链到** `code/`，运行时探测到的真实子目录："]
    if primary:
        lines.append(f"\n*与平台 `{platform or '未知'}` 高度相关（**优先读这里**）*：")
        for n in primary:
            lines.append(f"- `code/{n}/`")
    if secondary:
        lines.append("\n*其他子目录（可能含跨平台共享代码）*：")
        for n in secondary[:8]:
            lines.append(f"- `code/{n}/`")

    lines.append("")
    lines.append("**强制策略**：")
    lines.append("- 用 `Glob code/<相关目录>/**/*.dart` / `*.kt` / `*.swift` 列文件，再 Read 关键文件")
    lines.append("- 堆栈里的 `package:xxx/yyy.dart:N` → 找包含 `pubspec.yaml` 且 name 匹配的子目录，进入 `lib/` 查 yyy.dart")
    lines.append("- 找不到目录别瞎编路径——在 root_cause 里写明 \"该 package 未在源码树中找到\"")
    lines.append("- `code/CLAUDE.md`（如果有）可能含项目导航说明，值得先读")

    return "\n".join(lines) + "\n"


_CRASHGUARD_AGENT_TIMEOUT = 600  # 10 分钟硬超时——AI 要 Read 源码 + 写 fix_diff，5 分钟太紧；保留 hard cap 防 macOS 子进程被 SIGKILL 后父端干等


async def _run_agent(workspace: Path, prompt: str, is_followup: bool = False) -> AnalysisOutput:
    """复用 jarvis agent_orchestrator 选 ClaudeCodeAgent，调它的 analyze。

    叠加 crashguard 自己的 5 分钟硬超时，避免 claude 子进程在 macOS 被 SIGKILL/SIGSTOP
    时父端 `proc.communicate` 干等到 jarvis 的 600s 主超时。

    Args:
        is_followup: 追问模式时 result.json 只解析 answer/confidence/feasibility，不要求多根因
    """
    from app.services.agent_orchestrator import AgentOrchestrator

    orch = AgentOrchestrator()
    try:
        agent = orch.select_agent(rule_type="crashguard")
    except RuntimeError as e:
        return AnalysisOutput(
            scenario="", root_cause="", fix_suggestion="",
            feasibility_score=0.0, confidence="low", reproducibility="unknown",
            raw_output="", agent_name="",
            error=f"agent unavailable: {e}",
        )

    started = time.time()
    try:
        await asyncio.wait_for(
            agent.analyze(workspace=workspace, prompt=prompt),
            timeout=_CRASHGUARD_AGENT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        elapsed = time.time() - started
        logger.error("crashguard agent timed out after %.0fs (hard cap %ds)", elapsed, _CRASHGUARD_AGENT_TIMEOUT)
        # 即使超时也尝试解析 result.json——claude 可能已经写完但 communicate 卡住
        parsed = _parse_result_json(workspace)
        if parsed.get("root_cause"):
            return AnalysisOutput(
                scenario=parsed.get("scenario", "") or "",
                root_cause=parsed.get("root_cause", "") or "",
                fix_suggestion=parsed.get("fix_suggestion", "") or "",
                feasibility_score=float(parsed.get("feasibility_score") or 0.0),
                confidence=str(parsed.get("confidence", "") or "low").lower(),
                reproducibility=str(parsed.get("reproducibility", "") or "unknown").lower(),
                raw_output=parsed.get("_raw", ""),
                agent_name=getattr(agent.config, "agent_type", "claude_code"),
                fix_diff=parsed.get("fix_diff", "") or "",
            )
        return AnalysisOutput(
            scenario="", root_cause="", fix_suggestion="",
            feasibility_score=0.0, confidence="low", reproducibility="unknown",
            raw_output="", agent_name=getattr(agent.config, "agent_type", "claude_code"),
            error=f"crashguard hard timeout after {_CRASHGUARD_AGENT_TIMEOUT}s (subprocess可能被 macOS 杀掉)",
        )
    except Exception as e:
        logger.exception("agent.analyze raised")
        return AnalysisOutput(
            scenario="", root_cause="", fix_suggestion="",
            feasibility_score=0.0, confidence="low", reproducibility="unknown",
            raw_output="", agent_name=getattr(agent.config, "agent_type", ""),
            agent_model=getattr(agent.config, "model", "") or "",
            error=f"agent failed: {e}",
        )
    elapsed = time.time() - started
    logger.info("crashguard agent finished in %.1fs", elapsed)

    parsed = _parse_result_json(workspace)
    if is_followup:
        # 追问模式：只取 answer / confidence / feasibility；root_cause 留空（兼容老字段：把 answer 也写进去用作 status 判定）
        ans = parsed.get("answer", "") or ""
        return AnalysisOutput(
            scenario="",
            root_cause=ans,                   # 让 status 判定能识别非空
            fix_suggestion="",
            feasibility_score=float(parsed.get("feasibility_score") or 0.0),
            confidence=str(parsed.get("confidence", "") or "low").lower(),
            reproducibility="unknown",
            raw_output=parsed.get("_raw", ""),
            agent_name=getattr(agent.config, "agent_type", ""),
            possible_causes=[],
            complexity_kind="",
            solution="",
            hint="",
            answer=ans,
        )
    causes = parsed.get("possible_causes") or []
    if not isinstance(causes, list):
        causes = []
    return AnalysisOutput(
        scenario=parsed.get("scenario", "") or "",
        root_cause=parsed.get("root_cause", "") or "",
        fix_suggestion=parsed.get("fix_suggestion", "") or "",
        feasibility_score=float(parsed.get("feasibility_score") or 0.0),
        confidence=str(parsed.get("confidence", "") or "low").lower(),
        reproducibility=str(parsed.get("reproducibility", "") or "unknown").lower(),
        raw_output=parsed.get("_raw", ""),
        agent_name=getattr(agent.config, "agent_type", ""),
        agent_model=getattr(agent.config, "model", "") or "",
        possible_causes=causes[:5],  # 限 5 条
        complexity_kind=str(parsed.get("complexity", "") or "").lower(),
        solution=parsed.get("solution", "") or "",
        hint=parsed.get("hint", "") or "",
        fix_diff=parsed.get("fix_diff", "") or "",
    )


def _parse_result_json(workspace: Path) -> Dict[str, Any]:
    target = workspace / "output" / "result.json"
    if target.exists():
        try:
            text = target.read_text(encoding="utf-8").lstrip("\ufeff")
            d = json.loads(text)
            d["_raw"] = text[:8000]
            return d
        except Exception as e:
            logger.warning("parse result.json failed: %s", e)
    for cand in workspace.rglob("result.json"):
        if cand == target:
            continue
        try:
            text = cand.read_text(encoding="utf-8").lstrip("\ufeff")
            d = json.loads(text)
            d["_raw"] = text[:8000]
            logger.info("found result.json at fallback path: %s", cand)
            return d
        except Exception:
            continue
    return {"_raw": ""}


async def _persist_analysis_legacy(issue_id: str, output: AnalysisOutput) -> None:
    """同步入口走的旧路径——直接 add 一条新行。"""
    async with get_session() as session:
        row = CrashAnalysis(
            datadog_issue_id=issue_id,
            analysis_run_id=str(uuid.uuid4()),
            agent_name=output.agent_name or "",
            triggered_by="manual",
            problem_type="",
            scenario=output.scenario,
            root_cause=output.root_cause,
            fix_suggestion=output.fix_suggestion,
            fix_diff=output.fix_diff or "",
            feasibility_score=output.feasibility_score,
            confidence=output.confidence,
            reproducibility=output.reproducibility,
            agent_raw_output=output.raw_output,
            status="failed" if output.error else ("success" if output.root_cause else "failed"),
            error=output.error,
            created_at=datetime.utcnow(),
        )
        session.add(row)
        await session.commit()
        analysis_id = row.id
        is_success = row.status == "success"
    # 同步路径同样挂自动 PR 勾子
    if is_success:
        try:
            await _maybe_auto_draft_pr(analysis_id, float(output.feasibility_score or 0.0))
        except Exception:
            logger.exception("legacy auto draft PR failed (non-fatal)")


# ---------------------------------------------------------------------------
# Datadog enrichment（C：喂 AI 厚 context）
# ---------------------------------------------------------------------------

async def _build_enrichment_block(issue_id: str) -> str:
    """
    用 ErrorTrackingApi.get_issue 拉 Datadog issue 详情（sample event / 完整属性），
    转成 Markdown 段落。失败/无 key 时返回空串，不影响主流程。

    副作用：成功拉到分布数据后，回写 CrashIssue.top_os/top_device/top_app_version
    （用于详情页比"FLUTTER"更具体的展示，如 "Android 14 (40%)"）。
    """
    s = get_crashguard_settings()
    if not s.datadog_api_key:
        return ""
    try:
        from app.crashguard.services.datadog_client import DatadogClient

        client = DatadogClient(
            api_key=s.datadog_api_key,
            app_key=s.datadog_app_key,
            site=s.datadog_site,
        )
        detail = await client.get_issue_detail(issue_id)
    except Exception as exc:
        logger.warning("enrichment failed for %s: %s", issue_id, exc)
        return ""

    if not detail:
        return ""

    # 回写 CrashIssue 缓存字段（供详情页直接展示，无需重新拉 Datadog）
    try:
        await _persist_distribution_to_issue(issue_id, detail)
    except Exception as exc:
        logger.warning("persist distribution failed for %s: %s", issue_id, exc)

    parts = ["\n## 来自 Datadog 的真实事件上下文（最近一次崩溃采样）\n"]

    if detail.get("device"):
        parts.append(f"- **设备**: {detail['device']}")
    if detail.get("os"):
        parts.append(f"- **OS**: {detail['os']}")
    if detail.get("view"):
        parts.append(f"- **崩溃所在页面**: {detail['view']}")
    if detail.get("connectivity"):
        parts.append(f"- **网络**: {detail['connectivity']}")
    if detail.get("geo"):
        parts.append(f"- **地理位置**: {detail['geo']}")
    if detail.get("error_message"):
        parts.append(f"- **错误消息**: {detail['error_message']}")
    if detail.get("error_source_type"):
        parts.append(f"- **错误来源**: {detail['error_source_type']}")
    if detail.get("context_source"):
        parts.append(f"- **采集来源**: {detail['context_source']}")
    if detail.get("session_id"):
        parts.append(f"- **Session**: {detail['session_id']}")
    if detail.get("events_scanned"):
        parts.append(f"- **扫描事件数**: {detail['events_scanned']}（已挑选堆栈最优的一条）")

    # 分布信息（机型/OS/版本/页面/国家）
    def _fmt_dist(items):
        return ", ".join(f"{x['value']}({x['pct']}%)" for x in items)

    dist_lines = []
    if detail.get("version_distribution"):
        dist_lines.append(f"- **App 版本分布 Top5**: {_fmt_dist(detail['version_distribution'])}")
    if detail.get("os_distribution"):
        dist_lines.append(f"- **OS 版本分布 Top5**: {_fmt_dist(detail['os_distribution'])}")
    if detail.get("device_distribution"):
        dist_lines.append(f"- **机型分布 Top5**: {_fmt_dist(detail['device_distribution'])}")
    if detail.get("view_distribution"):
        dist_lines.append(f"- **触发页面分布 Top5**: {_fmt_dist(detail['view_distribution'])}")
    if detail.get("country_distribution"):
        dist_lines.append(f"- **国家分布 Top5**: {_fmt_dist(detail['country_distribution'])}")
    if dist_lines:
        parts.append("")
        parts.append("### 崩溃分布（基于近 7 天采样事件）\n")
        parts.extend(dist_lines)

    user_actions = detail.get("user_actions") or []
    if user_actions:
        parts.append("- **崩溃前用户操作路径**:")
        for act in user_actions[:8]:
            parts.append(f"    - {act}")

    full_stack = detail.get("full_stack") or ""
    if full_stack:
        quality = detail.get("stack_quality", "raw")
        quality_note = {
            "symbolicated_dart": "✅ 含 Dart 文件路径与行号，可直接定位",
            "symbolicated_jvm": "✅ 含 Java/Kotlin 类名与方法，可直接定位",
            "symbolicated_native": "✅ 含 iOS 符号，可直接定位",
            "aot_pointers_unsymbolicated": "⚠️ Flutter AOT 编译后的 hex 指针，需配合 build_id + flutter symbolize 才能解符号",
            "raw": "未识别",
            "empty": "无堆栈",
        }.get(quality, quality)
        parts.append(f"\n### 真实完整堆栈（来自 RUM 事件，挑选自 {detail.get('events_scanned', '?')} 条事件中最优的一条）")
        parts.append(f"\n**栈质量**：{quality_note}\n")
        parts.append("```")
        parts.append(full_stack[:6000])
        parts.append("```")

    if len(parts) <= 1:
        return ""
    parts.append("")
    return "\n".join(parts) + "\n"


def _format_top_dist(items: list, max_n: int = 3) -> str:
    """[{value,count,pct}] → 'Android 14 (40%), Android 13 (20%)'"""
    if not isinstance(items, list) or not items:
        return ""
    parts = []
    for x in items[:max_n]:
        v = x.get("value", "")
        pct = x.get("pct", 0)
        if v:
            parts.append(f"{v} ({pct}%)")
    return ", ".join(parts)


async def _persist_distribution_to_issue(issue_id: str, detail: Dict[str, Any]) -> None:
    """把 RUM 事件分布回写到 CrashIssue 缓存字段"""
    top_os = _format_top_dist(detail.get("os_distribution") or [])
    top_device = _format_top_dist(detail.get("device_distribution") or [])
    top_ver = _format_top_dist(detail.get("version_distribution") or [])
    if not (top_os or top_device or top_ver):
        return
    async with get_session() as session:
        row = (await session.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
        )).scalar_one_or_none()
        if row is None:
            return
        if top_os:
            row.top_os = top_os
        if top_device:
            row.top_device = top_device
        if top_ver:
            row.top_app_version = top_ver
        await session.commit()
