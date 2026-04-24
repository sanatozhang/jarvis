"""
Agent Orchestrator - selects and runs the appropriate agent for an analysis task.

Fallback logic: if the primary agent's token quota is exhausted, automatically
switch to the other agent.  Priority: claude_code → codex.
If both are exhausted, return a message asking the user to contact sanato.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.agents.base import AgentConfig, BaseAgent
from app.agents.claude_code import ClaudeCodeAgent
from app.agents.codex import CodexAgent
from app.config import get_settings
from app.models.schemas import AnalysisResult, Issue, Rule

logger = logging.getLogger("jarvis.orchestrator")

# Registry of agent implementations
AGENT_REGISTRY: Dict[str, type[BaseAgent]] = {
    "claude_code": ClaudeCodeAgent,
    "codex": CodexAgent,
}

# problem_type values that indicate token quota exhaustion
_QUOTA_EXHAUSTED_TYPES = {"Claude 额度不足", "OpenAI 额度不足"}

# Fallback order: primary → fallback
_FALLBACK_MAP: Dict[str, str] = {
    "claude_code": "codex",
    "codex": "claude_code",
}


class AgentOrchestrator:
    """Select and invoke the right agent for a given analysis."""

    def __init__(self):
        self._settings = get_settings()

    def select_agent(self, rule_type: str, override: Optional[str] = None) -> BaseAgent:
        """
        Select the agent to use based on:
        1. Explicit override (e.g. from API request)
        2. Routing config (rule_type → agent)
        3. Default agent
        """
        agent_cfg = self._settings.agent

        # Determine which agent to use
        agent_name = override or agent_cfg.routing.get(rule_type) or agent_cfg.default

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
            max_turns=agent_cfg.max_turns,
            allowed_tools=provider.allowed_tools,
            approval_mode=provider.approval_mode,
        )

        # Instantiate
        agent_cls = AGENT_REGISTRY.get(agent_name)
        if not agent_cls:
            raise RuntimeError(
                f"Unknown agent type: '{agent_name}'. "
                f"Registered: {list(AGENT_REGISTRY.keys())}"
            )

        logger.info("Selected agent: %s (model=%s) for rule_type=%s", agent_name, config.model, rule_type)
        return agent_cls(config)

    def _try_create_agent(self, agent_name: str) -> Optional[BaseAgent]:
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
            max_turns=agent_cfg.max_turns,
            allowed_tools=provider.allowed_tools,
            approval_mode=provider.approval_mode,
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
        language: str = "zh",
        on_progress: Optional[Callable[[int, str], Any]] = None,
        previous_analysis: Optional[Dict[str, Any]] = None,
        followup_question: str = "",
        condensation_context: Optional[Dict[str, Any]] = None,
    ) -> AnalysisResult:
        """
        Full analysis pipeline with automatic model fallback:
        1. Select agent (priority: claude_code)
        2. Build prompt
        3. Run agent
        4. If token quota exhausted → auto-switch to fallback agent
        5. If both exhausted → tell user to contact sanato
        """
        agent = self.select_agent(rule_type, override=agent_override)

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
                fallback_agent = self._try_create_agent(fallback_name)
                if fallback_agent:
                    logger.warning(
                        "Agent %s quota exhausted, falling back to %s",
                        primary_name, fallback_name,
                    )
                    if on_progress:
                        import asyncio
                        val = on_progress(55, f"{primary_name} 额度不足，自动切换到 {fallback_name}...")
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
                            problem_type="所有模型额度不足",
                            root_cause=(
                                "Claude 和 OpenAI 的 API 额度均已耗尽，无法完成分析。\n\n"
                                "请联系 sanato 充值或更换 API Key。"
                            ),
                            confidence="low",
                            needs_engineer=True,
                            agent_type=f"{primary_name}+{fallback_name}",
                        )

        result.issue_id = issue.record_id
        result.rule_type = rule_type
        result.agent_model = result.agent_model or agent.config.model
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
