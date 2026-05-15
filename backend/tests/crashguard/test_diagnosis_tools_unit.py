"""Unit tests for diagnosis_tools CLI scripts — 仅测 argparse + JSON 输出格式，不调真实 API。"""
from __future__ import annotations
import json
import subprocess
import sys
import os
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent.parent / "app" / "crashguard" / "services" / "diagnosis_tools"


def _run_tool(script: str, args: list, env_override: dict = None) -> dict:
    env = {**os.environ, **(env_override or {})}
    r = subprocess.run(
        [sys.executable, str(TOOLS_DIR / script)] + args,
        capture_output=True, text=True, timeout=10, env=env,
    )
    return json.loads(r.stdout)


def test_git_blame_missing_args():
    """git_blame.py 缺少必需参数时退出码非 0。"""
    r = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "git_blame.py")],
        capture_output=True, text=True, timeout=5,
    )
    assert r.returncode != 0


def test_git_pickaxe_no_repo(tmp_path):
    """git_pickaxe.py 在不存在的 repo 路径时输出 error JSON。"""
    result = _run_tool("git_pickaxe.py", [
        "--keyword", "readFile",
        "--repo-path", str(tmp_path / "nonexistent"),
    ])
    assert "error" in result


def test_find_similar_no_db():
    """find_similar.py 在无 DB 配置时返回包含 error 或 results 的 JSON（不 crash）。"""
    result = _run_tool(
        "find_similar.py",
        ["--fingerprint", "abc123"],
        env_override={"DATABASE_URL": "", "WORKSPACE_DIR": "/nonexistent_xyz"},
    )
    assert isinstance(result, dict)
    # 应该包含 error 或 results（空列表）
    assert "error" in result or "results" in result


def test_datadog_query_no_key():
    """datadog_query.py 无 API key 时返回 error JSON，不 crash。"""
    result = _run_tool(
        "datadog_query.py",
        ["--dql", "SELECT * FROM rum_events LIMIT 1"],
        env_override={"CRASHGUARD_DATADOG_API_KEY": ""},
    )
    assert "error" in result


def test_get_session_no_key():
    """get_session.py 无 API key 时返回 error JSON，不 crash。"""
    result = _run_tool(
        "get_session.py",
        ["--session-id", "fakesession123"],
        env_override={"CRASHGUARD_DATADOG_API_KEY": ""},
    )
    assert "error" in result
