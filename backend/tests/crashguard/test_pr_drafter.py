"""pr_drafter 服务单测（仅测纯函数 / 校验逻辑，不实际调 git/gh）"""
from __future__ import annotations

import pytest


def test_safe_branch_name_format():
    from app.crashguard.services.pr_drafter import _safe_branch_name
    b = _safe_branch_name("a79f3eb2-067e-11f1-9770-da7ad0900002", "android")
    assert b.startswith("crashguard/android/")
    # 包含时间戳
    parts = b.split("-")
    assert len(parts[-1]) >= 12  # YYYYMMDDHHMM


def test_safe_branch_name_strips_special_chars():
    from app.crashguard.services.pr_drafter import _safe_branch_name
    b = _safe_branch_name("!!!@@@", "ios")
    assert "!!!" not in b
    assert "@@@" not in b
    assert b.startswith("crashguard/ios/noid-")


def test_run_git_blocks_forbidden_flags():
    from app.crashguard.services.pr_drafter import _run_git
    rc, out, err = _run_git(["git", "merge", "main"], cwd="/tmp")
    assert rc != 0
    assert "forbidden" in err
