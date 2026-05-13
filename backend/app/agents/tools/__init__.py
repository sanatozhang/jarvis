"""
Tool layer for the API-based Claude agent.

Each tool defines:
- A JSON schema (TOOL_SCHEMAS) exposed to the model
- An async execute() that runs inside the workspace sandbox

The agent loop dispatches tool_use blocks here via `execute_tool`.
"""

from __future__ import annotations

from .base import ToolError, ToolResult
from .dispatcher import TOOL_SCHEMAS, execute_tool

__all__ = ["TOOL_SCHEMAS", "execute_tool", "ToolError", "ToolResult"]
