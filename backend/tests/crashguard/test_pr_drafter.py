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


def test_run_git_blocks_git_merge_subcommand():
    from app.crashguard.services.pr_drafter import _run_git
    rc, out, err = _run_git(["git", "merge", "main"], cwd="/tmp")
    assert rc != 0
    assert "forbidden" in err


def test_run_git_blocks_git_rebase_subcommand():
    from app.crashguard.services.pr_drafter import _run_git
    rc, out, err = _run_git(["git", "rebase", "main"], cwd="/tmp")
    assert rc != 0
    assert "forbidden" in err


def test_run_git_blocks_git_squash_flag():
    from app.crashguard.services.pr_drafter import _run_git
    rc, out, err = _run_git(["git", "rebase", "--squash"], cwd="/tmp")
    assert rc != 0
    assert "forbidden" in err


def test_run_git_blocks_gh_pr_merge():
    from app.crashguard.services.pr_drafter import _run_git
    rc, out, err = _run_git(["gh", "pr", "merge", "123"], cwd="/tmp")
    assert rc != 0
    assert "forbidden" in err


def test_run_git_blocks_gh_pr_ready():
    from app.crashguard.services.pr_drafter import _run_git
    rc, out, err = _run_git(["gh", "pr", "ready", "123"], cwd="/tmp")
    assert rc != 0
    assert "forbidden" in err


def test_run_git_allows_pr_body_with_merge_word():
    """关键修复：PR body / commit message 中含「merge / rebase」自然词不应被拦截"""
    from app.crashguard.services.pr_drafter import _run_git
    # 用一个不存在的 git 子命令避免实际执行；只验证 forbidden flag 检查不触发
    rc, out, err = _run_git(
        ["git", "log", "-1", "--pretty=%s",
         "-m", "discuss merge strategy and rebase order"],
        cwd="/tmp",
    )
    # 不应该是 "forbidden"，可能是别的错（cwd 不是 git repo）但不能被 flag check 拦
    assert "forbidden" not in err


def test_run_git_allows_gh_pr_create_with_merge_in_body():
    from app.crashguard.services.pr_drafter import _run_git
    rc, out, err = _run_git(
        ["gh", "pr", "create", "--draft",
         "--title", "fix bug",
         "--body", "Don't merge yet, need to rebase against main first"],
        cwd="/tmp",
    )
    # 关键断言：不能是 forbidden，因为「merge」「rebase」在 --body 后面是自然语言
    assert "forbidden" not in err


def test_stack_matches_android():
    from app.crashguard.services.pr_drafter import _stack_matches_platform
    assert _stack_matches_platform("android",
        "建议修改 app/src/main/java/com/example/MainActivity.kt 第 42 行") is True
    assert _stack_matches_platform("android", "纯文字描述无文件路径") is False


def test_stack_matches_ios():
    from app.crashguard.services.pr_drafter import _stack_matches_platform
    assert _stack_matches_platform("ios",
        "在 ios/Runner/AppDelegate.swift 中重写 didReceive...") is True
    assert _stack_matches_platform("ios", "完全没碰 iOS 代码") is False


def test_stack_matches_flutter():
    from app.crashguard.services.pr_drafter import _stack_matches_platform
    assert _stack_matches_platform("flutter",
        "lib/main.dart 第 10 行加 try-catch") is True
    assert _stack_matches_platform("flutter", "no path here") is False


def test_stack_matches_empty_fix_returns_true():
    """empty fix → 不阻断（上游已检查过 fix_suggestion 非空）"""
    from app.crashguard.services.pr_drafter import _stack_matches_platform
    assert _stack_matches_platform("android", "") is True


# ---------------- diff normalization & apply ----------------

def test_normalize_diff_strips_known_subrepo_prefix():
    from app.crashguard.services.pr_drafter import _normalize_diff_for_apply
    raw = (
        "--- a/code/plaud-flutter-common/lib/foo.dart\n"
        "+++ b/code/plaud-flutter-common/lib/foo.dart\n"
        "@@ -1,2 +1,2 @@\n"
        "-old\n+new\n"
    )
    out = _normalize_diff_for_apply(raw, "plaud-flutter-common")
    assert "--- a/lib/foo.dart" in out
    assert "+++ b/lib/foo.dart" in out
    assert "code/plaud-flutter-common" not in out


def test_normalize_diff_strips_unknown_subrepo_prefix_as_fallback():
    """AI 写错子仓库名时，仍把 code/<x>/ 前缀拿掉，让 apply 试一次"""
    from app.crashguard.services.pr_drafter import _normalize_diff_for_apply
    raw = "--- a/code/plaud-flutter/lib/foo.dart\n+++ b/code/plaud-flutter/lib/foo.dart\n"
    out = _normalize_diff_for_apply(raw, "plaud-flutter-common")
    assert "code/" not in out
    assert "--- a/lib/foo.dart" in out


def test_normalize_diff_passthrough_when_already_relative():
    from app.crashguard.services.pr_drafter import _normalize_diff_for_apply
    raw = "--- a/lib/foo.dart\n+++ b/lib/foo.dart\n"
    out = _normalize_diff_for_apply(raw, "plaud-flutter-common")
    assert out == raw


def test_try_apply_empty_diff_returns_false():
    from app.crashguard.services.pr_drafter import _try_apply_fix_diff
    ok, err = _try_apply_fix_diff("/tmp", "", "any")
    assert ok is False
    assert "empty" in err


def test_try_apply_garbage_diff_returns_false_safely(tmp_path):
    """随便给个不是 patch 的字符串：必须返回 False，不能抛异常"""
    from app.crashguard.services.pr_drafter import _try_apply_fix_diff
    import subprocess as _sp
    _sp.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    ok, err = _try_apply_fix_diff(str(tmp_path), "this is not a diff", "x")
    assert ok is False
    assert err  # 有错误信息


def test_try_apply_real_diff_succeeds(tmp_path):
    """端到端：在临时 git repo 里应用一个真实 patch，验证 apply 成功且文件被改"""
    import subprocess as _sp
    from app.crashguard.services.pr_drafter import _try_apply_fix_diff
    _sp.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    _sp.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True)
    _sp.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True)
    f = tmp_path / "a.txt"
    f.write_text("hello\nworld\n", encoding="utf-8")
    _sp.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    _sp.run(["git", "commit", "-qm", "init"], cwd=str(tmp_path), check=True)
    diff = (
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " hello\n"
        "-world\n"
        "+WORLD\n"
    )
    ok, err = _try_apply_fix_diff(str(tmp_path), diff, "any")
    assert ok is True, err
    assert "WORLD" in f.read_text()
