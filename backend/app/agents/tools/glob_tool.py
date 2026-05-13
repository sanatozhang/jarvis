"""glob tool: list files matching a glob pattern under the workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .base import ToolError, ToolResult

SCHEMA: Dict[str, Any] = {
    "name": "glob",
    "description": (
        "List files under the workspace that match a glob pattern. "
        "Use '**/' for recursive matching."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern relative to workspace, e.g. 'logs/**/*.log' or 'rules/*.md'.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum file paths to return (default 500).",
                "default": 500,
            },
        },
        "required": ["pattern"],
    },
}


async def execute(workspace: Path, inp: Dict[str, Any]) -> ToolResult:
    pattern = inp.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolError("glob: 'pattern' is required and must be a non-empty string")
    max_results = int(inp.get("max_results", 500) or 500)
    if max_results <= 0:
        max_results = 500

    # pathlib.Path.glob does not allow absolute patterns; the relative pattern
    # is what we want here. workspace acts as the chroot.
    workspace_resolved = workspace.resolve()
    results = []
    for p in workspace_resolved.glob(pattern):
        try:
            rel = p.relative_to(workspace_resolved)
        except ValueError:
            # Defensive: glob shouldn't escape workspace, but skip if it does
            continue
        kind = "d" if p.is_dir() else "f"
        results.append(f"{kind} {rel}")
        if len(results) >= max_results:
            break

    if not results:
        return ToolResult(content="(no matches)", result_summary="0 paths")

    truncated = len(results) == max_results
    body = "\n".join(results)
    if truncated:
        body += f"\n[... truncated at {max_results} results ...]"
    return ToolResult(
        content=body,
        result_summary=f"{len(results)} paths" + (" (truncated)" if truncated else ""),
    )
