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
                problem_type="Agent Unavailable",
                problem_type_en="Agent Unavailable",
                root_cause="Codex CLI is not installed. Install on the server: npm install -g @openai/codex",
                root_cause_en="Codex CLI is not installed. Install on the server: npm install -g @openai/codex",
                confidence="low", needs_engineer=False, system_failure=True, agent_type="codex",
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
                problem_type="Agent Unavailable",
                problem_type_en="Agent Unavailable",
                root_cause="OPENAI_API_KEY is not configured. Set it in the .env file.",
                root_cause_en="OPENAI_API_KEY is not configured. Set it in the .env file.",
                confidence="low", needs_engineer=False, system_failure=True, agent_type="codex",
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

            # Exit code 1 = OpenAI quota exhausted
            if proc.returncode == 1:
                logger.error("Codex exit code 1: OpenAI quota exhausted. stderr: %s", stderr[:300])
                return AnalysisResult(
                    task_id="", issue_id="",
                    problem_type="OpenAI API Quota Exhausted",
                    problem_type_en="OpenAI API Quota Exhausted",
                    root_cause=(
                        "OpenAI API quota has been exhausted; analysis could not complete.\n\n"
                        "Please check OpenAI account balance or upgrade the plan, then retry."
                    ),
                    root_cause_en=(
                        "OpenAI API quota has been exhausted; analysis could not complete.\n\n"
                        "Please check OpenAI account balance or upgrade the plan, then retry."
                    ),
                    confidence="low", needs_engineer=False, system_failure=True, agent_type="codex",
                )

            # Filesystem sync: ensure result.json is visible before parsing
            result_path = workspace / "output" / "result.json"
            if not result_path.exists():
                import os
                os.sync()
                await asyncio.sleep(1)
                if result_path.exists():
                    logger.info("result.json appeared after sync+wait")
                else:
                    logger.warning("result.json still missing after sync+wait")

            result = self.parse_result(workspace, stdout)
            result.agent_type = "codex"

            # If parse failed, log diagnostics but don't expose internals to user
            if result.problem_type == "未知" and not result.user_reply:
                diag = [f"Codex exit code: {proc.returncode}"]
                diag.append(f"result.json exists: {result_path.exists()}")
                if result_path.exists():
                    try:
                        raw_json = result_path.read_text(encoding="utf-8")[:500]
                        diag.append(f"result.json content (first 500): {raw_json}")
                    except Exception as re:
                        diag.append(f"result.json read error: {re}")
                diag.append(f"stdout (first 500): {stdout[:500] if stdout else '(empty)'}")
                logger.error("Parse failed diagnostics:\n%s", "\n".join(diag))

                result.root_cause = "Analysis did not produce a structured result. Please retry later or contact an admin to inspect logs."
                result.root_cause_en = result.root_cause

            return result

        except asyncio.TimeoutError:
            logger.error("Codex timed out after %ds", self.config.timeout)
            return AnalysisResult(
                task_id="", issue_id="",
                problem_type="Analysis Timeout",
                problem_type_en="Analysis Timeout",
                root_cause=f"Codex analysis exceeded {self.config.timeout}s timeout.",
                root_cause_en=f"Codex analysis exceeded {self.config.timeout}s timeout.",
                confidence="low", needs_engineer=False, system_failure=True, agent_type="codex",
            )
        except FileNotFoundError:
            logger.error("Codex CLI not found. Is it installed?")
            return AnalysisResult(
                task_id="", issue_id="",
                problem_type="Agent Unavailable",
                problem_type_en="Agent Unavailable",
                root_cause="Codex CLI is not installed or not on PATH.",
                root_cause_en="Codex CLI is not installed or not on PATH.",
                confidence="low", needs_engineer=False, system_failure=True, agent_type="codex",
            )

    def _build_command(self, prompt_file: Path) -> list[str]:
        cmd = [
            "codex", "exec",
            "--full-auto",
            f"First read AGENTS.md for behavioral rules, then read {prompt_file.name} and follow all instructions in it.",
        ]
        return cmd


async def _maybe_await(val):
    if asyncio.iscoroutine(val):
        await val
