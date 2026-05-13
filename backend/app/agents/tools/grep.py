"""grep tool: search for a regex pattern via ripgrep."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

from .base import ToolError, ToolResult, resolve_safe_path

SCHEMA: Dict[str, Any] = {
    "name": "grep",
    "description": (
        "Search for a regex pattern under the workspace using ripgrep. "
        "Returns matching lines with optional surrounding context. "
        "Pattern follows Rust regex syntax (similar to PCRE)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": "Directory or file relative to workspace. Defaults to whole workspace.",
                "default": ".",
            },
            "glob": {
                "type": "string",
                "description": "Optional file glob filter (e.g. '*.log', '**/main.log').",
            },
            "context_lines": {
                "type": "integer",
                "description": "Lines of context before and after each match (-C N).",
                "default": 0,
            },
            "max_matches": {
                "type": "integer",
                "description": "Maximum matches to return (default 200).",
                "default": 200,
            },
            "ignore_case": {
                "type": "boolean",
                "description": "Case insensitive search.",
                "default": False,
            },
        },
        "required": ["pattern"],
    },
}

_MAX_OUTPUT_BYTES = 1_000_000


async def execute(workspace: Path, inp: Dict[str, Any]) -> ToolResult:
    pattern = inp.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolError("grep: 'pattern' is required and must be a non-empty string")

    path = inp.get("path", ".") or "."
    target = resolve_safe_path(workspace, path)
    if not target.exists():
        raise ToolError(f"grep: path not found: {path}")

    max_matches = int(inp.get("max_matches", 200) or 200)
    if max_matches <= 0:
        max_matches = 200
    context_lines = int(inp.get("context_lines", 0) or 0)
    if context_lines < 0:
        context_lines = 0
    glob = inp.get("glob")
    ignore_case = bool(inp.get("ignore_case", False))

    cmd = [
        "rg",
        "--line-number",
        "--no-heading",
        "--max-count", str(max_matches),
        "--max-columns", "500",
    ]
    if context_lines:
        cmd.extend(["-C", str(context_lines)])
    if ignore_case:
        cmd.append("-i")
    if isinstance(glob, str) and glob:
        cmd.extend(["--glob", glob])
    cmd.extend(["--", pattern, str(target)])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=30)
    except FileNotFoundError:
        raise ToolError("grep: ripgrep (rg) is not installed in this environment")
    except asyncio.TimeoutError:
        raise ToolError("grep: search timed out after 30s")

    # rg exit codes: 0=matches found, 1=no match, 2=error
    if proc.returncode not in (0, 1):
        err = stderr_bytes.decode("utf-8", errors="replace")[:500]
        raise ToolError(f"grep: ripgrep failed (exit {proc.returncode}): {err}")

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    truncated = False
    if len(stdout.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        stdout = stdout.encode("utf-8")[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        truncated = True

    if proc.returncode == 1:
        return ToolResult(content="(no matches)", result_summary="0 matches")

    # Make paths workspace-relative for clarity in the trace
    workspace_str = str(workspace.resolve()) + "/"
    stdout = stdout.replace(workspace_str, "")

    match_count = stdout.count("\n")
    summary = f"{match_count} match lines"
    if truncated:
        summary += " (output truncated to 1MB)"
        stdout += "\n[... output truncated to 1MB ...]"
    return ToolResult(content=stdout, result_summary=summary)
