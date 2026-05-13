"""Shared base types and sandbox helpers for agent tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ToolError(Exception):
    """Raised when a tool call fails for a recoverable reason.

    The agent loop converts this into a tool_result with is_error=True so the
    model can read the error message and adjust on the next turn.
    """


@dataclass
class ToolResult:
    """Result of a tool invocation.

    `content` is the text returned to the model.
    `result_summary` is a short string for the trace log (e.g., "3 matches",
    "ok, 4KB"). Must not contain the full content.
    """
    content: str
    result_summary: str


def resolve_safe_path(workspace: Path, user_path: str) -> Path:
    """Resolve user_path within workspace; raise ToolError if it escapes.

    Both `workspace` and the result are absolute, resolved paths.
    """
    if not user_path or user_path == ".":
        return workspace.resolve()

    candidate = (workspace / user_path).resolve()
    workspace_resolved = workspace.resolve()
    try:
        candidate.relative_to(workspace_resolved)
    except ValueError:
        raise ToolError(f"Path escapes workspace: {user_path!r}")
    return candidate
