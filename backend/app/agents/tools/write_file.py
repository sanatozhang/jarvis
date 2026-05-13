"""write_file tool: write a file under workspace output/ subdirectory."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .base import ToolError, ToolResult, resolve_safe_path

SCHEMA: Dict[str, Any] = {
    "name": "write_file",
    "description": (
        "Write a file under the workspace `output/` subdirectory. "
        "Used primarily for `output/result.json`. Overwrites existing files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path under output/ (e.g. 'output/result.json'). MUST start with 'output/'.",
            },
            "content": {
                "type": "string",
                "description": "File content to write (UTF-8).",
            },
        },
        "required": ["path", "content"],
    },
}

_MAX_BYTES = 5_000_000


async def execute(workspace: Path, inp: Dict[str, Any]) -> ToolResult:
    path = inp.get("path")
    if not isinstance(path, str) or not path:
        raise ToolError("write_file: 'path' is required and must be a string")
    content = inp.get("content")
    if not isinstance(content, str):
        raise ToolError("write_file: 'content' is required and must be a string")

    # Enforce output/ prefix to prevent the model overwriting logs/, rules/, etc.
    normalized = path.lstrip("./")
    if not (normalized == "output" or normalized.startswith("output/")):
        raise ToolError(
            f"write_file: path must be under 'output/', got {path!r}"
        )

    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_BYTES:
        raise ToolError(
            f"write_file: content too large ({len(encoded)} bytes, max {_MAX_BYTES})"
        )

    target = resolve_safe_path(workspace, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encoded)

    return ToolResult(
        content=f"Wrote {len(encoded)} bytes to {path}",
        result_summary=f"ok, wrote {len(encoded)} bytes",
    )
