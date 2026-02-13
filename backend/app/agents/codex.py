"""
Codex (OpenAI) agent implementation.

Invokes `codex exec` in non-interactive mode (--full-auto).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from app.agents.base import AgentConfig, BaseAgent
from app.models.schemas import AnalysisResult

logger = logging.getLogger("jarvis.agent.codex")


class CodexAgent(BaseAgent):
    """Agent that delegates to the Codex CLI (codex exec)."""

    async def analyze(
        self,
        workspace: Path,
        prompt: str,
        on_progress: Optional[Callable[[int, str], Any]] = None,
    ) -> AnalysisResult:
        if on_progress:
            await _maybe_await(on_progress(60, "Codex 分析中..."))

        # Write prompt to file
        prompt_file = workspace / "prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")

        # Ensure workspace is a git repo (codex requires it)
        git_dir = workspace / ".git"
        if not git_dir.exists():
            subprocess.run(["git", "init", "-q"], cwd=str(workspace), capture_output=True)

        # Pre-flight: check codex is available
        import shutil
        if not shutil.which("codex"):
            logger.error("Codex CLI not found in PATH")
            return AnalysisResult(
                task_id="", issue_id="",
                problem_type="Agent 不可用",
                root_cause="Codex CLI 未安装。请在服务器上安装: npm install -g @openai/codex",
                confidence="low", needs_engineer=True, agent_type="codex",
            )

        # Check OPENAI_API_KEY
        import os
        if not os.environ.get("OPENAI_API_KEY"):
            logger.error("OPENAI_API_KEY not set")
            return AnalysisResult(
                task_id="", issue_id="",
                problem_type="Agent 不可用",
                root_cause="OPENAI_API_KEY 未配置。请在 .env 文件中设置。",
                confidence="low", needs_engineer=True, agent_type="codex",
            )

        cmd = self._build_command(prompt_file)
        logger.info("Running Codex in %s (prompt: %d chars)", workspace, len(prompt))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.config.timeout,
            )

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                logger.warning("Codex exited with code %d. stderr: %s", proc.returncode, stderr[:500])

            if on_progress:
                await _maybe_await(on_progress(90, "解析分析结果..."))

            result = self.parse_result(workspace, stdout)
            result.agent_type = "codex"
            return result

        except asyncio.TimeoutError:
            logger.error("Codex timed out after %ds", self.config.timeout)
            return AnalysisResult(
                task_id="", issue_id="",
                problem_type="分析超时",
                root_cause=f"Codex 分析超过 {self.config.timeout}s 超时",
                confidence="low", needs_engineer=True, agent_type="codex",
            )
        except FileNotFoundError:
            logger.error("Codex CLI not found. Is it installed?")
            return AnalysisResult(
                task_id="", issue_id="",
                problem_type="Agent 不可用",
                root_cause="Codex CLI 未安装或不在 PATH 中",
                confidence="low", needs_engineer=True, agent_type="codex",
            )

    def _build_command(self, prompt_file: Path) -> list[str]:
        cmd = [
            "codex", "exec",
            "--full-auto",
            f"Read the file {prompt_file.name} and follow all instructions in it.",
        ]
        return cmd


async def _maybe_await(val):
    if asyncio.iscoroutine(val):
        await val
