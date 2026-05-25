"""read_file tool: read a file relative to the workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .base import ToolError, ToolResult, resolve_safe_path

SCHEMA: Dict[str, Any] = {
    "name": "read_file",
    "description": (
        "Read a file relative to the workspace root. Returns the file content as text. "
        "Use `offset` and `limit` (in bytes) for large files. Max 200 KB per call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to workspace root (e.g. 'logs/main.log').",
            },
            "offset": {
                "type": "integer",
                "description": "Byte offset to start reading.",
                "default": 0,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of bytes to read (default 200_000, max 200_000).",
                "default": 200_000,
            },
        },
        "required": ["path"],
    },
}

_MAX_BYTES = 200_000   # 200 KB — reduced from 2 MB to prevent context explosion

# These files are already embedded in the prompt; reading them again bloats
# the messages array with hundreds of thousands of redundant tokens.
_READ_BLACKLIST = frozenset({
    "context/extraction_full.json",
    "prompt.md",
    "fixup_prompt.md",
})


async def execute(workspace: Path, inp: Dict[str, Any]) -> ToolResult:
    path = inp.get("path")
    if not isinstance(path, str) or not path:
        raise ToolError("read_file: 'path' is required and must be a string")

    # Block files that are already provided in the prompt to avoid context bloat
    if path in _READ_BLACKLIST:
        return ToolResult(
            content=(
                f"[{path} 已包含在分析 prompt 中，请直接使用 prompt 里的摘要内容，"
                "无需重复读取此文件（重复读取会导致 context 过大）。]"
            ),
            result_summary=f"blocked: {path} is already embedded in prompt",
        )

    offset = int(inp.get("offset", 0) or 0)
    limit = int(inp.get("limit", _MAX_BYTES) or _MAX_BYTES)
    if offset < 0:
        raise ToolError("read_file: 'offset' must be >= 0")
    if limit <= 0:
        raise ToolError("read_file: 'limit' must be > 0")
    limit = min(limit, _MAX_BYTES)

    target = resolve_safe_path(workspace, path)
    if not target.exists():
        raise ToolError(f"read_file: file not found: {path}")
    if target.is_dir():
        raise ToolError(f"read_file: path is a directory, not a file: {path}")

    with target.open("rb") as f:
        f.seek(offset)
        data = f.read(limit)

    total_size = target.stat().st_size
    truncated = offset + len(data) < total_size
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")

    if truncated:
        text += (
            f"\n[... truncated: read {len(data)} bytes from offset {offset}, "
            f"total file size {total_size} bytes ...]"
        )

    summary = f"ok, {len(data)} bytes"
    if truncated:
        summary += f" (truncated, total {total_size})"
    return ToolResult(content=text, result_summary=summary)
