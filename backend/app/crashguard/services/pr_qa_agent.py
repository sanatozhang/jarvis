"""
PR Quality Assessment Agent — 事后体检（post-create QA）。

闭环：crashguard PR 创建成功 → fire-and-forget 调本服务 → 取 PR diff →
     调便宜 LLM 给质量打分 → 写 audit log → 低分时飞书通知 reviewer。

与 Gate#9 (judge_diff_with_llm) 区别：
  - Gate#9 在 PR 创建**之前**跑（拦垃圾，abort 失败 PR）
  - 本服务在 PR 创建**之后**跑（帮 reviewer 提速 + 兜底 Gate#9 漏放）
  - Gate#9 reject → abort PR；本服务低分 → 只通知，不自动 close（MVP 阶段先观察）

🚫 严禁触发任何写动作（不调 gh pr close / merge / ready）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("crashguard.pr_qa_agent")

# 飞书通知阈值：质量分 < 此值 → 主动通知 reviewer 重点关注
DEFAULT_NOTIFY_BELOW = 60

# do_not_merge verdict 直接通知，不看分数
_BAD_VERDICTS = frozenset(("do_not_merge", "reject"))


def _build_qa_prompt(
    diff_text: str, root_cause: str, fix_suggestion: str, issue_stack: str,
) -> str:
    """构造 post-PR QA prompt。

    与 Gate#9 prompt 的差异：
      - Gate#9 只评 diff 是否解决根因（二分 approve/reject）
      - 本 prompt 要 0-100 quality_score + 维度细评 + reviewer_summary
      - 输出更多 actionable 字段供 reviewer 快速决策
    """
    diff_snip = (diff_text or "")[:8000]
    cause_snip = (root_cause or "")[:1500]
    fix_snip = (fix_suggestion or "")[:2000]
    stack_snip = (issue_stack or "")[:1500]

    return f"""你是 Plaud Senior Code Reviewer，现在审查一个 crashguard AI 自动生成的修复 PR。
你的目标是给这个 PR 一份**reviewer 体检报告**，帮人审者快速决策合还是不合。

## 输入

### Crash Root Cause（崩溃根因）
{cause_snip}

### Crash Stack（堆栈节选）
```
{stack_snip}
```

### AI Fix Suggestion（AI 给出的修复方案）
{fix_snip}

### Actual PR Diff（PR 真实改动）
```diff
{diff_snip}
```

## 评估维度

1. **addresses_root_cause** — diff 是否真触及 root_cause 描述的关键代码点？
2. **scope_appropriate** — 改动范围是否合适？有无无关改动 / 占位代码 / 跨模块修改？
3. **regression_risk** — 改动是否可能引入 regression（潜在调用方、并发场景、边界条件）？
4. **code_quality** — 命名、可读性、注释、异常处理是否合理？
5. **test_coverage** — 是否补了对应单测？（如果原修复无需测试可标 N/A）

## 输出格式（严格 JSON 单行，**无任何其它字符**）

{{
  "quality_score": <0-100 整数>,
  "verdict": "approve_ready" | "needs_revision" | "do_not_merge",
  "addresses_root_cause": <true|false>,
  "scope_issues": ["<≤50 字描述>", ...],
  "regression_risks": ["<≤50 字描述>", ...],
  "reviewer_summary": "<2-3 句话给 reviewer 看的总结，≤200 字>"
}}

## 评分指引
- 80-100: 改动精准且无明显问题 → approve_ready
- 60-79:  改动方向对但有改进空间 / 缺测试 → needs_revision
- <60:    改动跑偏 / 引入 regression / 没解决根因 → do_not_merge
"""


def _gh_pr_diff(repo_slug: str, pr_number: int, timeout: int = 30) -> str:
    """调 gh pr diff 拿 PR 真实 diff。

    刚创建的 PR 可能 indexed 有延迟——失败时返回空串，由调用方决定是否退化。
    与 pr_sync._gh_view 同样做剥 PAT 处理（org SSO repo 需 OAuth）。
    """
    import os as _os
    sub_env = dict(_os.environ)
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        sub_env.pop(k, None)
    try:
        r = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--repo", repo_slug],
            capture_output=True, text=True, timeout=timeout, env=sub_env,
        )
        if r.returncode != 0:
            logger.warning(
                "gh pr diff failed for %s#%d: %s",
                repo_slug, pr_number, (r.stderr or "")[:200],
            )
            return ""
        return r.stdout or ""
    except subprocess.TimeoutExpired:
        logger.warning("gh pr diff timeout for %s#%d", repo_slug, pr_number)
        return ""
    except FileNotFoundError:
        logger.warning("gh CLI not installed")
        return ""
    except Exception as exc:
        logger.warning("gh pr diff error for %s#%d: %s", repo_slug, pr_number, exc)
        return ""


def _parse_qa_json(raw_text: str) -> Optional[Dict[str, Any]]:
    """解析 LLM 输出的 JSON。容忍 BOM / 多余包裹文本。"""
    if not raw_text:
        return None
    try:
        return json.loads(raw_text.lstrip("﻿").strip())
    except json.JSONDecodeError:
        # 尝试抓第一个含 quality_score 的 {...}（贪婪匹配以支持多行）
        m = re.search(r"\{.*?\"quality_score\".*?\}", raw_text, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def _normalize_parsed(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """把 LLM 输出归一化（防类型错乱）。"""
    score = parsed.get("quality_score", 0)
    try:
        score = int(score)
    except (ValueError, TypeError):
        score = 0
    score = max(0, min(100, score))

    verdict = (parsed.get("verdict") or "").strip().lower()
    if verdict not in ("approve_ready", "needs_revision", "do_not_merge"):
        verdict = "needs_revision"  # 默认中性

    return {
        "quality_score": score,
        "verdict": verdict,
        "addresses_root_cause": bool(parsed.get("addresses_root_cause", False)),
        "scope_issues": [str(x)[:100] for x in (parsed.get("scope_issues") or [])][:5],
        "regression_risks": [str(x)[:100] for x in (parsed.get("regression_risks") or [])][:5],
        "reviewer_summary": str(parsed.get("reviewer_summary") or "")[:300],
    }


async def _notify_low_quality(
    pr_url: str, repo_slug: str, pr_number: int, parsed: Dict[str, Any],
) -> None:
    """低分 PR 飞书通知（私聊 reviewer email）。失败静默——非关键链路。"""
    try:
        from app.crashguard.config import get_crashguard_settings
        s = get_crashguard_settings()
        target_email = (
            getattr(s, "feishu_alert_email", "") or getattr(s, "feishu_target_email", "")
        )
        if not target_email:
            return
        verdict_emoji = {
            "do_not_merge": "🚫",
            "needs_revision": "⚠️",
            "approve_ready": "✅",
        }.get(parsed["verdict"], "❓")
        lines = [
            f"{verdict_emoji} Crashguard QA Agent 体检报告：{pr_url}",
            f"   质量分: {parsed['quality_score']}/100   verdict: {parsed['verdict']}",
            f"   root_cause 命中: {'是' if parsed['addresses_root_cause'] else '否'}",
            f"   总结: {parsed['reviewer_summary']}",
        ]
        if parsed["scope_issues"]:
            lines.append(f"   范围问题: {'; '.join(parsed['scope_issues'])}")
        if parsed["regression_risks"]:
            lines.append(f"   回归风险: {'; '.join(parsed['regression_risks'])}")
        text = "\n".join(lines)
        from app.services.feishu_cli import send_message
        await send_message(email=target_email, text=text)
    except Exception:
        logger.exception("pr_qa_agent feishu notify failed (non-fatal)")


async def _write_qa_audit(
    pr_url: str, analysis_id: Optional[int], parsed: Dict[str, Any],
) -> None:
    """写 audit log 流水。Schema 不变——MVP 阶段先存 audit，避免迁移。"""
    try:
        from app.crashguard.services.audit import write_audit
        target_id = str(analysis_id) if analysis_id else (pr_url[-60:] if pr_url else "")
        detail = str({
            "pr_url": pr_url,
            "quality_score": parsed.get("quality_score"),
            "verdict": parsed.get("verdict"),
            "addresses_root_cause": parsed.get("addresses_root_cause"),
            "scope_issues": parsed.get("scope_issues"),
            "regression_risks": parsed.get("regression_risks"),
            "summary": parsed.get("reviewer_summary"),
        })[:1500]
        await write_audit(
            op="pr_qa_agent",
            target_id=target_id,
            success=True,
            detail=detail,
            error=None,
        )
    except Exception:
        logger.exception("pr_qa_agent audit write failed (non-fatal)")


async def run_post_pr_quality_check(
    pr_url: str,
    repo_slug: str,
    pr_number: int,
    root_cause: str,
    fix_suggestion: str,
    issue_stack: str,
    analysis_id: Optional[int] = None,
    timeout_sec: int = 180,
    notify_below: int = DEFAULT_NOTIFY_BELOW,
    fallback_diff: str = "",
) -> Dict[str, Any]:
    """fire-and-forget 主入口：评估刚创建的 PR 质量。

    fails open——任何环节失败都返回 {"ok": False, ...}，不抛异常给上游，
    永远不阻塞 PR 创建主链路。

    fallback_diff: 当 gh pr diff 失败（PR 刚创建未 indexed）时的兜底 diff
                   文本（通常是 ana.fix_diff 或 agent 输出）。
    """
    try:
        # 1. 拉 PR 真实 diff（首选 gh pr diff，失败用 fallback）
        diff_text = _gh_pr_diff(repo_slug, pr_number)
        if not diff_text and fallback_diff:
            diff_text = fallback_diff
            logger.info("pr_qa_agent using fallback_diff for %s#%d", repo_slug, pr_number)
        if not diff_text:
            return {"ok": False, "error": "empty diff", "pr_url": pr_url}

        # 2. 构造 prompt + 调 agent
        prompt = _build_qa_prompt(diff_text, root_cause, fix_suggestion, issue_stack)
        from app.services.agent_orchestrator import AgentOrchestrator
        orch = AgentOrchestrator()
        agent = orch.select_agent(rule_type="crashguard")

        raw_text = ""
        with tempfile.TemporaryDirectory(prefix="crashguard_postqa_") as td:
            workspace = Path(td)
            try:
                await asyncio.wait_for(
                    agent.analyze(workspace=workspace, prompt=prompt),
                    timeout=timeout_sec,
                )
            except asyncio.TimeoutError:
                return {"ok": False, "error": "agent timeout", "pr_url": pr_url}
            # 容忍多种输出位置（同 Gate#9）
            for cand in (workspace / "output" / "result.json",
                         workspace / "result.json"):
                if cand.exists():
                    raw_text = cand.read_text(encoding="utf-8", errors="ignore")
                    break
            if not raw_text:
                for f in workspace.rglob("*.json"):
                    try:
                        t = f.read_text(encoding="utf-8", errors="ignore")
                        if '"quality_score"' in t:
                            raw_text = t
                            break
                    except Exception:
                        continue

        if not raw_text:
            return {"ok": False, "error": "agent produced no output", "pr_url": pr_url}

        # 3. 解析 JSON + 归一化
        parsed_raw = _parse_qa_json(raw_text)
        if parsed_raw is None:
            return {"ok": False, "error": "agent json unparseable",
                    "raw": raw_text[:200], "pr_url": pr_url}
        parsed = _normalize_parsed(parsed_raw)

        # 4. 落地：audit + 低分通知
        await _write_qa_audit(pr_url, analysis_id, parsed)
        should_notify = (
            parsed["quality_score"] < notify_below
            or parsed["verdict"] in _BAD_VERDICTS
        )
        if should_notify:
            await _notify_low_quality(pr_url, repo_slug, pr_number, parsed)
            logger.warning(
                "pr_qa_agent low quality: pr=%s score=%d verdict=%s — notified",
                pr_url, parsed["quality_score"], parsed["verdict"],
            )
        else:
            logger.info(
                "pr_qa_agent ok: pr=%s score=%d verdict=%s",
                pr_url, parsed["quality_score"], parsed["verdict"],
            )

        return {"ok": True, "pr_url": pr_url, **parsed}
    except Exception as exc:
        logger.exception("pr_qa_agent crashed (fails open)")
        return {"ok": False, "error": f"crashed: {exc}", "pr_url": pr_url}
