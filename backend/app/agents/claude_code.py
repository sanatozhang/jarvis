"""
Claude Code agent implementation.

Invokes the `claude` CLI in non-interactive (print) mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from app.agents.base import AgentConfig, BaseAgent
from app.models.schemas import AnalysisResult

logger = logging.getLogger("jarvis.agent.claude_code")


class ClaudeCodeAgent(BaseAgent):
    """Agent that delegates to the Claude Code CLI."""

    async def analyze(
        self,
        workspace: Path,
        prompt: str,
        on_progress: Optional[Callable[[int, str], Any]] = None,
    ) -> AnalysisResult:
        if on_progress:
            await _maybe_await(on_progress(60, "Claude Code 分析中..."))

        # Save prompt to file for debugging/audit, and pipe via stdin
        prompt_file = workspace / "prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")

        cmd = self._build_command()
        logger.info("Running Claude Code in %s (prompt: %d chars, piped via stdin)", workspace, len(prompt))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workspace),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=self.config.timeout,
            )

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                logger.warning(
                    "Claude Code exited with code %d. stderr: %s",
                    proc.returncode,
                    stderr[:500],
                )

                combined = (stderr + stdout).lower()

                # Detect token quota / rate limit exhaustion
                if any(kw in combined for kw in [
                    "rate limit", "quota", "credit", "billing",
                    "overloaded", "token limit", "usage limit",
                    "exceeded your current", "insufficient_quota",
                    "out of credits", "plan limit",
                ]):
                    logger.error("Claude Code token quota exhausted. stderr: %s", stderr[:300])
                    return AnalysisResult(
                        task_id="", issue_id="",
                        problem_type="Claude 额度不足",
                        root_cause=(
                            "Anthropic API 额度已耗尽，无法完成分析。\n\n"
                            "请检查 Anthropic 账户余额或升级套餐后重试。"
                        ),
                        confidence="low", needs_engineer=True, agent_type="claude_code",
                    )

                # Detect max turns exhaustion — but still try to parse result.json
                # because Claude may have written it before hitting the limit
                if "max turns" in combined or "reached max" in combined:
                    logger.warning("Claude Code reached max turns limit")

            if on_progress:
                await _maybe_await(on_progress(90, "解析分析结果..."))

            # Always try to parse — even if returncode != 0, Claude may
            # have written result.json or produced useful stdout before failing
            raw_output = stdout
            result = self._parse_claude_output(workspace, raw_output)
            result.agent_type = "claude_code"
            return result

        except asyncio.TimeoutError:
            logger.error("Claude Code timed out after %ds", self.config.timeout)
            return AnalysisResult(
                task_id="",
                issue_id="",
                problem_type="分析超时",
                root_cause=f"Claude Code 分析超过 {self.config.timeout}s 超时",
                confidence="low",
                needs_engineer=True,
                agent_type="claude_code",
            )
        except FileNotFoundError:
            logger.error("Claude Code CLI not found. Is it installed?")
            return AnalysisResult(
                task_id="",
                issue_id="",
                problem_type="Agent 不可用",
                root_cause="Claude Code CLI 未安装或不在 PATH 中",
                confidence="low",
                needs_engineer=True,
                agent_type="claude_code",
            )

    def _build_command(self) -> list[str]:
        """Build claude CLI command. Prompt is piped via stdin."""
        cmd = [
            "claude",
            "-p",
            "--output-format", "text",
        ]

        if self.config.model:
            cmd.extend(["--model", self.config.model])

        if self.config.max_turns:
            cmd.extend(["--max-turns", str(self.config.max_turns)])

        if self.config.allowed_tools:
            cmd.append("--allowedTools")
            cmd.extend(self.config.allowed_tools)

        return cmd

    def _parse_claude_output(self, workspace: Path, raw_output: str) -> AnalysisResult:
        """Parse Claude Code output - try result.json first, then raw text."""
        return self.parse_result(workspace, raw_output)


async def _maybe_await(val):
    """Await if the value is a coroutine."""
    if asyncio.iscoroutine(val):
        await val
