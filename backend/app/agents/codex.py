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

        # Check OPENAI_API_KEY (check env + .env file)
        import os
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            # Try reading from .env file directly
            try:
                from app.config import PROJECT_ROOT
                env_path = PROJECT_ROOT / ".env"
                if env_path.exists():
                    for line in env_path.read_text().splitlines():
                        if line.startswith("OPENAI_API_KEY=") and line.split("=", 1)[1].strip():
                            api_key = line.split("=", 1)[1].strip()
                            os.environ["OPENAI_API_KEY"] = api_key
                            break
            except Exception:
                pass
        if not api_key:
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

            # Save raw output for debugging
            debug_dir = workspace / "output"
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / "codex_stdout.txt").write_text(stdout, encoding="utf-8")
            (debug_dir / "codex_stderr.txt").write_text(stderr, encoding="utf-8")
            (debug_dir / "codex_exitcode.txt").write_text(str(proc.returncode), encoding="utf-8")

            logger.info("Codex finished: exit=%d stdout=%d bytes stderr=%d bytes", proc.returncode, len(stdout), len(stderr))
            if stdout:
                logger.info("Codex stdout (first 500 chars): %s", stdout[:500])
            if stderr:
                logger.warning("Codex stderr (first 500 chars): %s", stderr[:500])
            if proc.returncode != 0:
                logger.error("Codex exited with code %d", proc.returncode)

            if on_progress:
                await _maybe_await(on_progress(90, "解析分析结果..."))

            result = self.parse_result(workspace, stdout)
            result.agent_type = "codex"

            # If parse failed, include raw output in error for debugging
            if result.problem_type == "未知" and not result.user_reply:
                hint = stdout[:300] if stdout else "(empty stdout)"
                if stderr:
                    hint += f"\n\nstderr: {stderr[:300]}"
                result.root_cause = f"分析未产出结构化结果。\n\nCodex 退出码: {proc.returncode}\nCodex 原始输出:\n{hint}"

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
