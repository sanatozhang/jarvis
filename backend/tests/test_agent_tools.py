"""Tests for the agent tool layer (read_file, write_file, grep, glob)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.agents.tools import TOOL_SCHEMAS, ToolError, execute_tool


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "main.log").write_text(
        "2026-05-13 10:00:00 BLE connect start\n"
        "2026-05-13 10:00:05 BLE GATT connection timeout after 30s\n"
        "2026-05-13 10:00:10 BLE reconnect\n",
        encoding="utf-8",
    )
    (tmp_path / "logs" / "secondary.log").write_text(
        "2026-05-13 10:01:00 audio start\n", encoding="utf-8"
    )
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "bluetooth.md").write_text("# bluetooth rule\n", encoding="utf-8")
    return tmp_path


class TestSchemas:
    def test_four_tools_registered(self):
        names = {s["name"] for s in TOOL_SCHEMAS}
        assert names == {"read_file", "write_file", "grep", "glob"}

    def test_schemas_have_required_keys(self):
        for s in TOOL_SCHEMAS:
            assert "name" in s and "description" in s and "input_schema" in s
            assert s["input_schema"]["type"] == "object"


class TestReadFile:
    async def test_reads_file_content(self, workspace):
        result = await execute_tool("read_file", {"path": "logs/main.log"}, workspace)
        assert "BLE GATT connection timeout" in result.content
        assert "bytes" in result.result_summary

    async def test_offset_and_limit(self, workspace):
        result = await execute_tool(
            "read_file", {"path": "logs/main.log", "offset": 20, "limit": 30}, workspace
        )
        assert "truncated" in result.content or "truncated" in result.result_summary

    async def test_missing_file(self, workspace):
        with pytest.raises(ToolError, match="file not found"):
            await execute_tool("read_file", {"path": "logs/nope.log"}, workspace)

    async def test_path_escape(self, workspace):
        with pytest.raises(ToolError, match="escapes workspace"):
            await execute_tool("read_file", {"path": "../etc/passwd"}, workspace)

    async def test_rejects_directory(self, workspace):
        with pytest.raises(ToolError, match="is a directory"):
            await execute_tool("read_file", {"path": "logs"}, workspace)


class TestWriteFile:
    async def test_writes_under_output(self, workspace):
        result = await execute_tool(
            "write_file", {"path": "output/result.json", "content": '{"ok":true}'}, workspace
        )
        assert "ok" in result.result_summary
        assert (workspace / "output" / "result.json").read_text() == '{"ok":true}'

    async def test_rejects_outside_output(self, workspace):
        with pytest.raises(ToolError, match="under 'output/'"):
            await execute_tool(
                "write_file", {"path": "logs/main.log", "content": "x"}, workspace
            )

    async def test_rejects_path_escape(self, workspace):
        with pytest.raises(ToolError, match="under 'output/'"):
            await execute_tool(
                "write_file", {"path": "../evil.txt", "content": "x"}, workspace
            )


class TestGrep:
    async def test_finds_matches(self, workspace):
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
        result = await execute_tool(
            "grep", {"pattern": "BLE.*timeout", "path": "logs"}, workspace
        )
        assert "GATT connection timeout" in result.content
        assert "match" in result.result_summary

    async def test_no_matches(self, workspace):
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
        result = await execute_tool(
            "grep", {"pattern": "no_such_pattern_xyz", "path": "logs"}, workspace
        )
        assert result.result_summary == "0 matches"

    async def test_path_escape(self, workspace):
        with pytest.raises(ToolError, match="escapes workspace"):
            await execute_tool("grep", {"pattern": "x", "path": "../"}, workspace)


class TestGlob:
    async def test_lists_files(self, workspace):
        result = await execute_tool("glob", {"pattern": "logs/*.log"}, workspace)
        assert "logs/main.log" in result.content
        assert "logs/secondary.log" in result.content

    async def test_recursive(self, workspace):
        result = await execute_tool("glob", {"pattern": "**/*.md"}, workspace)
        assert "rules/bluetooth.md" in result.content

    async def test_no_matches(self, workspace):
        result = await execute_tool("glob", {"pattern": "**/*.nope"}, workspace)
        assert result.result_summary == "0 paths"


class TestDispatcher:
    async def test_unknown_tool(self, workspace):
        with pytest.raises(ToolError, match="Unknown tool"):
            await execute_tool("not_a_tool", {}, workspace)
