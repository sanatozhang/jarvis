"""Shared headless `claude -p` CLI JSON-output caller.

Extracted from voc_classifier.classify_ticket() so other one-shot
structured-output LLM calls (voc_digest's weekly narrative generation, and
classify_ticket() itself) can reuse the same subprocess-invocation, timeout,
and envelope-parsing logic without duplicating it.

Why this goes through the local CLI (headless, OAuth-authenticated) rather
than a direct Messages API call: production runs `agent.call_mode: cli` with
no ANTHROPIC_API_KEY provisioned — see voc_classifier.py's module docstring
for the full explanation (an API key present in the CLI's env triggers an
interactive prompt that hangs a non-TTY subprocess).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from app.agents.claude_code import _make_cli_env, parse_cli_result_envelope

logger = logging.getLogger("jarvis.claude_headless")


async def run_json(
    system_prompt: str,
    user_input: str,
    schema: Dict[str, Any],
    model: str,
    timeout: float,
    *,
    log_prefix: str = "claude_headless",
) -> Optional[Dict[str, Any]]:
    """Run one headless `claude -p --output-format json` turn constrained to
    `schema` (piped via --json-schema), and return the parsed JSON object.

    Returns None — never raises — on any failure: CLI binary missing, call
    timed out, non-zero exit code, non-JSON `.result` text, or JSON that
    isn't an object. Callers apply their own fallback policy on None.
    """
    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--model", model,
        "--tools", "",
        "--no-session-persistence",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--system-prompt", system_prompt,
        "--json-schema", json.dumps(schema),
    ]

    scratch = Path(tempfile.mkdtemp(prefix=f"{log_prefix}_"))
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(scratch),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_make_cli_env(),
            )
        except FileNotFoundError:
            logger.warning("%s: claude CLI binary not found", log_prefix)
            return None

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=user_input.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("%s: CLI call timed out after %ss", log_prefix, timeout)
            return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if proc.returncode != 0:
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        logger.warning("%s: CLI exited %d: %s", log_prefix, proc.returncode, stderr[:300])
        return None

    stdout_raw = stdout_bytes.decode("utf-8", errors="replace")
    text, _usage, _cost, _source = parse_cli_result_envelope(stdout_raw)

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("%s: CLI returned non-JSON output: %s", log_prefix, text[:300])
        return None

    if not isinstance(data, dict):
        logger.warning("%s: CLI JSON output was not an object", log_prefix)
        return None

    return data
