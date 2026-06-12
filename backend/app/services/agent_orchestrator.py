"""
Agent Orchestrator - selects and runs the appropriate agent for an analysis task.

Fallback logic: if the primary agent's token quota is exhausted, automatically
switch to the other agent.  Priority: claude_code → codex.
If both are exhausted, return a message asking the user to contact sanato.
"""

from __future__ import annotations

import json
import logging
import os
import random
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.agents.base import AgentConfig, BaseAgent
from app.agents.claude_api import ClaudeApiAgent
from app.agents.claude_code import ClaudeCodeAgent
from app.agents.codex import CodexAgent
from app.config import get_settings
from app.models.schemas import AnalysisResult, Issue, Rule

logger = logging.getLogger("jarvis.orchestrator")

# Registry of agent implementations
AGENT_REGISTRY: Dict[str, type[BaseAgent]] = {
    "claude_code": ClaudeCodeAgent,
    "claude_api": ClaudeApiAgent,
    "codex": CodexAgent,
}

# problem_type values that indicate token quota exhaustion
_QUOTA_EXHAUSTED_TYPES = {
    # 英文（当前 problem_type 用的）
    "Claude API Quota Exhausted", "OpenAI API Quota Exhausted",
    # 兼容历史中文（旧数据 / 老镜像）
    "Claude 额度不足", "OpenAI 额度不足",
}

# Fallback order: primary → fallback
_FALLBACK_MAP: Dict[str, str] = {
    "claude_code": "codex",
    "claude_api": "codex",
    "codex": "claude_api",
}


class AgentOrchestrator:
    """Select and invoke the right agent for a given analysis."""

    def __init__(self):
        self._settings = get_settings()

    def select_agent(
        self,
        rule_type: str,
        override: Optional[str] = None,
        prompt_chars: Optional[int] = None,
        deep_analysis: bool = False,
    ) -> BaseAgent:
        """
        Select the agent to use based on:
        1. Explicit override (e.g. from API request)
        2. **Large-prompt route (方案 C 抓手)**: prompt_chars 超过 cli_route_above_chars
           阈值（默认 500K bytes ≈ 140K tokens）时强制走 claude_code (CLI 1M)。
           API claude-sonnet-4-6 context 仅 200K tokens；超大日志工单（1.2% 长尾）
           走 API 会装不下 → 直走 CLI 1M 兜底。
        3. call_mode toggle: "api" → claude_api, "cli" → routing/default Claude resolves to claude_code
        4. Routing config (rule_type → agent)
        5. Default agent
        """
        agent_cfg = self._settings.agent

        # Determine which agent to use
        agent_name = override or agent_cfg.routing.get(rule_type) or agent_cfg.default

        # Probabilistic API/CLI split — only when no explicit override.
        # api_traffic_ratio=0.0 → 100% CLI; 0.2 → 20% API; 1.0 → 100% API.
        # Backward compat: call_mode=="api" treated as ratio=1.0.
        if not override and agent_name == "claude_code":
            ratio = agent_cfg.api_traffic_ratio
            if agent_cfg.call_mode == "api" and ratio == 0.0:
                ratio = 1.0
            if ratio > 0.0 and random.random() < ratio:
                api_provider = agent_cfg.providers.get("claude_api")
                if api_provider and api_provider.enabled:
                    agent_name = "claude_api"
        elif not override and agent_name == "claude_api" and agent_cfg.call_mode == "cli" and agent_cfg.api_traffic_ratio == 0.0:
            agent_name = "claude_code"

        # ── 方案 C 路由（最后一闸）：大 prompt 强制走 CLI 1M ─────────────────
        # 顶层设计抓手：放在 prob split **之后**，确保即使 prob 把工单分到 API，
        # 大 prompt 仍能被拉回 CLI 1M——避免 API 200K context 装不下。
        # override 优先级最高（手动指定 agent 时尊重，不强制）。
        if not override and prompt_chars is not None:
            threshold = int(getattr(agent_cfg, "cli_route_above_chars", 500_000) or 500_000)
            if threshold > 0 and prompt_chars > threshold and agent_name != "claude_code":
                cc_provider = agent_cfg.providers.get("claude_code")
                if cc_provider and cc_provider.enabled:
                    logger.info(
                        "Large-prompt route: chars=%d > threshold=%d → forcing claude_code (was %s)",
                        prompt_chars, threshold, agent_name,
                    )
                    agent_name = "claude_code"

        # Get provider config
        provider = agent_cfg.providers.get(agent_name)
        if not provider or not provider.enabled:
            # Fallback to default
            agent_name = agent_cfg.default
            provider = agent_cfg.providers.get(agent_name)
            if not provider or not provider.enabled:
                raise RuntimeError(
                    f"No enabled agent found. Tried '{agent_name}'. "
                    f"Available: {list(agent_cfg.providers.keys())}"
                )

        # Build config
        config = AgentConfig(
            agent_type=agent_name,
            model=provider.model,
            effort=provider.effort,
            fallback_model=provider.fallback_model,
            betas=provider.betas,
            timeout=provider.timeout or agent_cfg.timeout,
            max_turns=40 if deep_analysis else agent_cfg.max_turns,
            allowed_tools=provider.allowed_tools,
            approval_mode=provider.approval_mode,
            base_url=provider.base_url,
            per_turn_timeout=provider.per_turn_timeout,
            max_tokens=provider.max_tokens,
            enable_cache=provider.enable_cache,
            api_key=os.environ.get("ANTHROPIC_API_KEY", "") if agent_name == "claude_api" else "",
            log_read_cap=30 if deep_analysis else None,
        )

        # Instantiate
        agent_cls = AGENT_REGISTRY.get(agent_name)
        if not agent_cls:
            raise RuntimeError(
                f"Unknown agent type: '{agent_name}'. "
                f"Registered: {list(AGENT_REGISTRY.keys())}"
            )

        logger.info("Selected agent: %s (model=%s, call_mode=%s) for rule_type=%s",
                    agent_name, config.model, agent_cfg.call_mode, rule_type)
        return agent_cls(config)

    def _try_create_agent(self, agent_name: str, deep_analysis: bool = False) -> Optional[BaseAgent]:
        """Try to create an agent by name. Returns None if unavailable."""
        agent_cfg = self._settings.agent
        provider = agent_cfg.providers.get(agent_name)
        if not provider or not provider.enabled:
            return None
        agent_cls = AGENT_REGISTRY.get(agent_name)
        if not agent_cls:
            return None
        config = AgentConfig(
            agent_type=agent_name,
            model=provider.model,
            effort=provider.effort,
            fallback_model=provider.fallback_model,
            betas=provider.betas,
            timeout=provider.timeout or agent_cfg.timeout,
            max_turns=40 if deep_analysis else agent_cfg.max_turns,
            allowed_tools=provider.allowed_tools,
            approval_mode=provider.approval_mode,
            base_url=provider.base_url,
            per_turn_timeout=provider.per_turn_timeout,
            max_tokens=provider.max_tokens,
            enable_cache=provider.enable_cache,
            api_key=os.environ.get("ANTHROPIC_API_KEY", "") if agent_name == "claude_api" else "",
            log_read_cap=30 if deep_analysis else None,
        )
        return agent_cls(config)

    async def run_analysis(
        self,
        workspace: Path,
        issue: Issue,
        rules: List[Rule],
        extraction: Dict[str, Any],
        rule_type: str = "",
        agent_override: Optional[str] = None,
        problem_date: Optional[str] = None,
        has_logs: bool = True,
        language: str = "en",
        on_progress: Optional[Callable[[int, str], Any]] = None,
        previous_analysis: Optional[Dict[str, Any]] = None,
        followup_question: str = "",
        condensation_context: Optional[Dict[str, Any]] = None,
        logs_corrupted: bool = False,
        pipeline_timeout: Optional[int] = None,
        deep_analysis: bool = False,
    ) -> AnalysisResult:
        """
        Full analysis pipeline with automatic model fallback:
        1. Select agent (priority: claude_code)
        2. Build prompt
        3. Run agent
        4. If token quota exhausted → auto-switch to fallback agent
        5. If both exhausted → tell user to contact sanato
        """
        # 方案 C 顶层设计：先 build prompt → 拿到 prompt_chars → 再 select_agent。
        # 这样能让 select_agent 根据 prompt 大小决定走 CLI 1M（超大）还是 API 200K（常规）。
        # 原顺序是先 select 再 build，无法做 size-aware routing。

        # Retrieve similar golden samples for few-shot injection
        few_shot_examples = []
        try:
            from app.services.golden_samples import find_similar_samples
            few_shot_examples = await find_similar_samples(
                issue.description, rule_type=rule_type or None, top_k=3,
            )
            if few_shot_examples:
                logger.info("Injecting %d few-shot examples for issue %s", len(few_shot_examples), issue.record_id)
        except Exception as e:
            logger.warning("Failed to retrieve golden samples: %s", e)

        context_files = _materialize_analysis_context(
            workspace=workspace,
            issue=issue,
            extraction=extraction,
            rules=rules,
            problem_date=problem_date,
            has_logs=has_logs,
            previous_analysis=previous_analysis,
            followup_question=followup_question,
            few_shot_examples=few_shot_examples,
        )

        # Materialize L1.5 condensation context if available
        if condensation_context:
            cc_path = workspace / "context" / "llm_extraction.json"
            if not cc_path.exists():
                cc_path.parent.mkdir(parents=True, exist_ok=True)
                cc_path.write_text(
                    json.dumps(condensation_context, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            context_files["condensation"] = str(cc_path.relative_to(workspace))

        prompt, prompt_meta = BaseAgent.build_prompt_with_meta(
            issue=issue,
            rules=rules,
            extraction=extraction,
            problem_date=problem_date,
            has_logs=has_logs,
            language=language,
            previous_analysis=previous_analysis,
            followup_question=followup_question,
            few_shot_examples=few_shot_examples,
            context_files=context_files,
            condensation_context=condensation_context,
            logs_corrupted=logs_corrupted,
            deep_analysis=deep_analysis,
        )
        _write_prompt_meta(workspace, prompt_meta)
        logger.info(
            "Prompt meta issue=%s rule_type=%s chars=%s compact=%s hard_trimmed=%s contexts=%s",
            issue.record_id,
            rule_type or "(none)",
            prompt_meta.get("final_prompt_chars"),
            prompt_meta.get("compact_mode"),
            prompt_meta.get("hard_trimmed"),
            ",".join(sorted(prompt_meta.get("context_files", {}).keys())),
        )

        # 现在 prompt 已 build，可以根据大小选 agent
        prompt_chars = int(prompt_meta.get("final_prompt_chars") or len(prompt))
        agent = self.select_agent(rule_type, override=agent_override, prompt_chars=prompt_chars, deep_analysis=deep_analysis)

        # RC1/RC3 兜底合约：让 agent 自身超时严格小于外层 pipeline 硬墙（task_timeout）。
        # 否则二者相等（都 600s）时外层 wait_for 先 cancel → agent 走 CancelledError 直接 raise，
        # 已落盘的 result.json 被丢弃（fb_b47f129711 实测：10:44 已写部分结果，仍被当彻底失败）。
        # 让 agent 早 salvage_margin 秒触发自己的 TimeoutError → 走 salvage → 在硬墙前正常 return。
        # 同时让大日志档（pipeline=1200s）真正把额外时间给到 agent（agent=1140s），而非卡在静态 600s。
        if pipeline_timeout and pipeline_timeout > 0:
            margin = getattr(self._settings.concurrency, "salvage_margin", 60) or 60
            new_timeout = max(60, pipeline_timeout - margin)
            if new_timeout != agent.config.timeout:
                logger.info(
                    "Agent timeout coupled to pipeline: %ds → %ds (pipeline=%ds, margin=%ds)",
                    agent.config.timeout, new_timeout, pipeline_timeout, margin,
                )
                agent.config.timeout = new_timeout

        result = await agent.analyze(
            workspace=workspace,
            prompt=prompt,
            on_progress=on_progress,
        )

        # ── Auto-fallback on quota exhaustion ──
        if result.problem_type in _QUOTA_EXHAUSTED_TYPES:
            primary_name = agent.config.agent_type
            fallback_name = _FALLBACK_MAP.get(primary_name)

            if fallback_name:
                fallback_agent = self._try_create_agent(fallback_name, deep_analysis=deep_analysis)
                if fallback_agent:
                    logger.warning(
                        "Agent %s quota exhausted, falling back to %s",
                        primary_name, fallback_name,
                    )
                    if on_progress:
                        import asyncio
                        val = on_progress(55, f"{primary_name} quota exhausted; auto-switching to {fallback_name}...")
                        if asyncio.iscoroutine(val):
                            await val

                    result = await fallback_agent.analyze(
                        workspace=workspace,
                        prompt=prompt,
                        on_progress=on_progress,
                    )
                    result.agent_model = fallback_agent.config.model

                    # If fallback also exhausted → both models down
                    if result.problem_type in _QUOTA_EXHAUSTED_TYPES:
                        logger.error("Both agents quota exhausted!")
                        result = AnalysisResult(
                            task_id="",
                            issue_id="",
                            problem_type="All Model Quotas Exhausted",
                            problem_type_en="All Model Quotas Exhausted",
                            root_cause=(
                                "Both Claude and OpenAI API quotas have been exhausted; analysis could not complete.\n\n"
                                "Please contact sanato to top up or rotate the API key."
                            ),
                            root_cause_en=(
                                "Both Claude and OpenAI API quotas have been exhausted; analysis could not complete.\n\n"
                                "Please contact sanato to top up or rotate the API key."
                            ),
                            confidence="low",
                            needs_engineer=False,
                            system_failure=True,
                            agent_type=f"{primary_name}+{fallback_name}",
                        )

        result.issue_id = issue.record_id
        result.rule_type = rule_type
        result.agent_model = result.agent_model or agent.config.model

        # T2: 二次 LLM 复核——破解 AI 自相矛盾，把"已给完整 user_reply 还说要工程师"翻回 false
        # 只对 needs_engineer=true 的工单调用，约 30% 流量，安全降级（失败保持原判）
        try:
            from app.services.engineer_label_adjudicator import apply_adjudication
            result = await apply_adjudication(result)
        except Exception as e:
            logger.warning("Adjudicator unexpected error (保持原判): %s", e)

        return result


def _write_json_file(path: Path, payload: Dict[str, Any] | List[Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path.relative_to(path.parent.parent))


def _write_prompt_meta(workspace: Path, payload: Dict[str, Any]) -> None:
    output_dir = workspace / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "prompt_meta.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _materialize_analysis_context(
    workspace: Path,
    issue: Issue,
    extraction: Dict[str, Any],
    rules: List[Rule],
    problem_date: Optional[str],
    has_logs: bool,
    previous_analysis: Optional[Dict[str, Any]],
    followup_question: str,
    few_shot_examples: List[Dict[str, Any]],
) -> Dict[str, str]:
    context_dir = workspace / "context"
    context_dir.mkdir(parents=True, exist_ok=True)

    context_files: Dict[str, str] = {}
    context_files["issue"] = _write_json_file(
        context_dir / "issue_context.json",
        {
            "record_id": issue.record_id,
            "description": issue.description,
            "device_sn": issue.device_sn,
            "firmware": issue.firmware,
            "app_version": issue.app_version,
            "zendesk": issue.zendesk,
            "problem_date": problem_date,
            "has_logs": has_logs,
            "matched_rules": [rule.meta.id for rule in rules],
        },
    )

    context_files["extraction"] = _write_json_file(
        context_dir / "extraction_full.json",
        extraction,
    )

    if few_shot_examples:
        context_files["few_shot"] = _write_json_file(
            context_dir / "few_shot_examples.json",
            few_shot_examples,
        )

    if previous_analysis:
        context_files["previous_analysis"] = _write_json_file(
            context_dir / "previous_analysis.json",
            previous_analysis,
        )

    if followup_question:
        question_path = context_dir / "followup_question.txt"
        question_path.write_text(followup_question, encoding="utf-8")
        context_files["followup_question"] = str(question_path.relative_to(workspace))

    # Classification taxonomy — AI reads this file to fill problem_categories + device_type
    from app.classification_taxonomy import CLASSIFICATION_TAXONOMY
    context_files["classification"] = _write_json_file(
        context_dir / "classification_taxonomy.json",
        CLASSIFICATION_TAXONOMY,
    )

    return context_files
