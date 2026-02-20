"""
Agent Orchestrator - selects and runs the appropriate agent for an analysis task.
"""

from __future__ import annotations

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
        on_progress: Optional[Callable[[int, str], Any]] = None,
    ) -> AnalysisResult:
        """
        Full analysis pipeline:
        1. Select agent
        2. Build prompt
        3. Run agent
        4. Parse result
        """
        agent = self.select_agent(rule_type, override=agent_override)

        prompt = BaseAgent.build_prompt(
            issue=issue,
            rules=rules,
            extraction=extraction,
            problem_date=problem_date,
            has_logs=has_logs,
        )

        result = await agent.analyze(
            workspace=workspace,
            prompt=prompt,
            on_progress=on_progress,
        )

        result.issue_id = issue.record_id
        result.rule_type = rule_type
        return result
