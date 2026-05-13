"""Tool registry + dispatcher for the agent loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from . import glob_tool, grep, read_file, write_file
from .base import ToolError, ToolResult

# Schemas exposed to the model in messages.create(tools=...)
TOOL_SCHEMAS: List[Dict[str, Any]] = [
    read_file.SCHEMA,
    write_file.SCHEMA,
    grep.SCHEMA,
    glob_tool.SCHEMA,
]

_DISPATCH = {
    "read_file": read_file.execute,
    "write_file": write_file.execute,
    "grep": grep.execute,
    "glob": glob_tool.execute,
}


async def execute_tool(name: str, inp: Dict[str, Any], workspace: Path) -> ToolResult:
    """Dispatch a tool call by name. Raises ToolError on unknown tool or failure."""
    fn = _DISPATCH.get(name)
    if fn is None:
        raise ToolError(f"Unknown tool: {name!r}. Available: {sorted(_DISPATCH)}")
    return await fn(workspace, inp or {})
