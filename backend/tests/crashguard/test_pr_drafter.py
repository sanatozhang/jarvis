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


# ---------------- P1: 自愈 cleanup + 宽松 dirty 检查 ----------------

def _git_init_repo(tmp_path):
    """在 tmp_path 里建一个最小 git repo（含一个 init commit）。"""
    import subprocess as _sp
    _sp.run(["git", "init", "-q", "-b", "main"], cwd=str(tmp_path), check=True)
    _sp.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True)
    _sp.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True)
    (tmp_path / "README").write_text("init\n", encoding="utf-8")
    _sp.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    _sp.run(["git", "commit", "-qm", "init"], cwd=str(tmp_path), check=True)


def test_worktree_dirty_returns_false_when_clean(tmp_path):
    from app.crashguard.services.pr_drafter import _worktree_dirty
    _git_init_repo(tmp_path)
    dirty, _ = _worktree_dirty(str(tmp_path))
    assert dirty is False


def test_worktree_dirty_returns_true_for_tracked_file_change(tmp_path):
    """真文件改动必须阻塞 auto-PR（保护工程师工作）。"""
    from app.crashguard.services.pr_drafter import _worktree_dirty
    _git_init_repo(tmp_path)
    (tmp_path / "README").write_text("dirty change\n", encoding="utf-8")
    dirty, detail = _worktree_dirty(str(tmp_path))
    assert dirty is True
    assert "README" in detail


def test_worktree_dirty_ignores_submodule_pointer_change(tmp_path):
    """关键场景：只有 submodule pointer 漂移时不应阻塞——auto-PR 不动 submodule。"""
    import subprocess as _sp
    from app.crashguard.services.pr_drafter import _worktree_dirty
    # 主 repo
    _git_init_repo(tmp_path)
    # 在另一个临时目录里造一个"被引用"的 submodule
    sub = tmp_path.parent / f"{tmp_path.name}-sub"
    sub.mkdir()
    _sp.run(["git", "init", "-q", "-b", "main"], cwd=str(sub), check=True)
    _sp.run(["git", "config", "user.email", "t@t"], cwd=str(sub), check=True)
    _sp.run(["git", "config", "user.name", "t"], cwd=str(sub), check=True)
    (sub / "x.txt").write_text("v1\n")
    _sp.run(["git", "add", "-A"], cwd=str(sub), check=True)
    _sp.run(["git", "commit", "-qm", "v1"], cwd=str(sub), check=True)
    # 把它加为 submodule
    _sp.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub"],
        cwd=str(tmp_path), check=True,
    )
    _sp.run(["git", "commit", "-qm", "add sub"], cwd=str(tmp_path), check=True)
    # 在 submodule 里多一个 commit → 父 repo 的 submodule pointer 出现 dirty
    (sub / "x.txt").write_text("v2\n")
    _sp.run(["git", "add", "-A"], cwd=str(sub), check=True)
    _sp.run(["git", "commit", "-qm", "v2"], cwd=str(sub), check=True)
    # 父 repo 的 sub 目录指针不更新，仍指 v1——pull 一下让 worktree 看到漂移
    _sp.run(["git", "-C", "sub", "fetch", "-q"], cwd=str(tmp_path), check=True)
    _sp.run(["git", "-C", "sub", "checkout", "-q", "main"], cwd=str(tmp_path), check=True)
    # 现在 git status 应该报 "M sub"（submodule pointer 改动）
    dirty, detail = _worktree_dirty(str(tmp_path))
    # 期望：忽略 submodule pointer 漂移 → 不算 dirty
    assert dirty is False, f"submodule pointer dirty should be ignored, got: {detail}"


def test_cleanup_resets_dirty_worktree_to_base(tmp_path):
    """流程失败后留下脏文件 + 非主分支 → cleanup 应回到 main + 工作树干净 + 删未推送分支。"""
    import subprocess as _sp
    from app.crashguard.services.pr_drafter import _cleanup_repo_after_pr, _worktree_dirty
    _git_init_repo(tmp_path)
    # 模拟流程：切到临时分支，写脏文件
    _sp.run(["git", "checkout", "-q", "-b", "crashguard/test/dummy"], cwd=str(tmp_path), check=True)
    (tmp_path / "README").write_text("auto-PR dirty\n", encoding="utf-8")
    (tmp_path / ".crashguard").mkdir(exist_ok=True)
    (tmp_path / ".crashguard" / "tmp.md").write_text("garbage\n", encoding="utf-8")
    # 进入 cleanup
    _cleanup_repo_after_pr(
        repo_path=str(tmp_path),
        base_ref="main",
        initial_branch="main",
        branch_to_delete="crashguard/test/dummy",
        pushed_to_remote=False,
    )
    # 验证：当前在 main、worktree 干净、临时分支已删
    rc = _sp.run(["git", "branch", "--show-current"], cwd=str(tmp_path), capture_output=True, text=True)
    assert rc.stdout.strip() == "main", f"should be on main, got: {rc.stdout}"
    dirty, detail = _worktree_dirty(str(tmp_path))
    assert dirty is False, f"worktree should be clean, got: {detail}"
    branches = _sp.run(["git", "branch"], cwd=str(tmp_path), capture_output=True, text=True).stdout
    assert "crashguard/test/dummy" not in branches


def test_cleanup_keeps_pushed_branch(tmp_path):
    """已 push 到远端的分支不能删——远端 PR 还指向它。"""
    import subprocess as _sp
    from app.crashguard.services.pr_drafter import _cleanup_repo_after_pr
    _git_init_repo(tmp_path)
    _sp.run(["git", "checkout", "-q", "-b", "crashguard/test/pushed"], cwd=str(tmp_path), check=True)
    (tmp_path / "patch.txt").write_text("patch\n", encoding="utf-8")
    _sp.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    _sp.run(["git", "commit", "-qm", "patch"], cwd=str(tmp_path), check=True)
    _cleanup_repo_after_pr(
        repo_path=str(tmp_path),
        base_ref="main",
        initial_branch="main",
        branch_to_delete="crashguard/test/pushed",
        pushed_to_remote=True,  # 标记已推送
    )
    # 已推送的分支必须保留
    branches = _sp.run(["git", "branch"], cwd=str(tmp_path), capture_output=True, text=True).stdout
    assert "crashguard/test/pushed" in branches


# ---------------- P2: 进入前自愈（防进程被 SIGKILL 留下残骸） ----------------

def test_pre_enter_heal_resets_stale_crashguard_branch(tmp_path):
    """模拟"上次进程被 SIGKILL，三库卡在 crashguard/* 分支带脏 prompt.md"。
    本次进入前 hook 必须自动清回 main，让 _worktree_dirty 检查通过。
    """
    import subprocess as _sp
    from pathlib import Path
    from app.crashguard.services.pr_drafter import _worktree_dirty

    _git_init_repo(tmp_path)
    # 模拟上次残骸：切到 crashguard 分支 + 写脏文件 + 留 prompt.md
    _sp.run(["git", "checkout", "-q", "-b", "crashguard/flutter/stale-zombie"], cwd=str(tmp_path), check=True)
    (tmp_path / "README").write_text("zombie change\n", encoding="utf-8")
    (tmp_path / "prompt.md").write_text("agent leftover\n", encoding="utf-8")
    (tmp_path / ".crashguard").mkdir()
    (tmp_path / ".crashguard" / "tmp.json").write_text("{}", encoding="utf-8")

    # 跑进入前自愈逻辑（提取的内联实现）—— 模拟 draft_pr_for_analysis 入口
    def _heal(repo_path: str, branch: str):
        from app.crashguard.services.pr_drafter import _run_git
        if branch.startswith("crashguard/"):
            _run_git(["git", "checkout", "--", "."], repo_path, timeout=15)
            _run_git(["git", "clean", "-fd", "--", ".crashguard"], repo_path, timeout=10)
            _run_git(["git", "clean", "-fd", "--", "prompt.md"], repo_path, timeout=10)
            (Path(repo_path) / "prompt.md").unlink(missing_ok=True)
            _run_git(["git", "checkout", "main"], repo_path, timeout=30)
            _run_git(["git", "branch", "-D", branch], repo_path, timeout=10)

    _heal(str(tmp_path), "crashguard/flutter/stale-zombie")

    # 验证：回到 main + worktree 干净 + 脏分支没了 + prompt.md 没了
    cur = _sp.run(["git", "branch", "--show-current"], cwd=str(tmp_path), capture_output=True, text=True).stdout.strip()
    assert cur == "main"
    dirty, _ = _worktree_dirty(str(tmp_path))
    assert dirty is False
    assert not (tmp_path / "prompt.md").exists()
    branches = _sp.run(["git", "branch"], cwd=str(tmp_path), capture_output=True, text=True).stdout
    assert "crashguard/flutter/stale-zombie" not in branches


# ─────────────────── multi-repo flutter routing (commit a0ac9f0+) ───────────────────

def test_detect_flutter_subrepo_hint_global():
    """root_cause 或 fix_suggestion 提到 plaud-flutter-global → 路由到 global sub-repo"""
    from app.crashguard.services.pr_drafter import _detect_flutter_subrepo_hint
    assert _detect_flutter_subrepo_hint(
        "根因在 code/plaud-flutter-global/lib/shared/puth_helper/push_helper.dart"
    ) == "global"
    assert _detect_flutter_subrepo_hint("PLAUD-FLUTTER-GLOBAL/lib/...") == "global"


def test_detect_flutter_subrepo_hint_cn():
    """plaud-flutter-cn / plaud-flutter-china 提示 → 路由到 cn sub-repo"""
    from app.crashguard.services.pr_drafter import _detect_flutter_subrepo_hint
    assert _detect_flutter_subrepo_hint("plaud-flutter-cn/lib/x.dart") == "cn"
    assert _detect_flutter_subrepo_hint("plaud-flutter-china/...") == "cn"


def test_detect_flutter_subrepo_hint_service_label():
    """service / bundle id 提示也算 hint"""
    from app.crashguard.services.pr_drafter import _detect_flutter_subrepo_hint
    assert _detect_flutter_subrepo_hint("service: com.plaud.global") == "global"
    assert _detect_flutter_subrepo_hint("ai.plaud.cn application") == "cn"


def test_detect_flutter_subrepo_hint_default_empty():
    """没明显提示 → 返回空字符串（默认 common）"""
    from app.crashguard.services.pr_drafter import _detect_flutter_subrepo_hint
    assert _detect_flutter_subrepo_hint("") == ""
    assert _detect_flutter_subrepo_hint("FileSystemException") == ""


def test_platform_repo_path_flutter_routes_by_hint(monkeypatch, tmp_path):
    """_platform_repo_path 根据 sub_hint 切换 sub-repo"""
    from app.crashguard.services import pr_drafter
    # 准备三个伪 sub-repo 目录（每个含 .git 文件）
    wrapper = tmp_path / "plaud_ai"
    wrapper.mkdir()
    for sub in ("plaud-flutter-common", "plaud-flutter-global", "plaud-flutter-cn"):
        d = wrapper / sub
        d.mkdir()
        (d / ".git").write_text("gitdir: fake")

    # 绕过 yaml direct override + mock wrapper resolver
    monkeypatch.setattr(
        pr_drafter, "get_crashguard_settings",
        lambda: type("S", (), {"repo_path_flutter": "", "repo_path_android": "",
                                "repo_path_ios": ""})(),
    )
    import app.config as cfg
    monkeypatch.setattr(cfg, "get_code_repo_for_platform", lambda _: str(wrapper))

    common = pr_drafter._platform_repo_path("flutter")
    global_ = pr_drafter._platform_repo_path("flutter", "global")
    cn = pr_drafter._platform_repo_path("flutter", "cn")

    assert common and common.endswith("plaud-flutter-common")
    assert global_ and global_.endswith("plaud-flutter-global")
    assert cn and cn.endswith("plaud-flutter-cn")


# ─────────── 治本 v2：blob 探测自动 fallback ───────────

def test_extract_diff_target_paths_basic():
    from app.crashguard.services.pr_drafter import _extract_diff_target_paths
    diff = (
        "--- a/lib/x.dart\n"
        "+++ b/lib/x.dart\n"
        "@@ -1 +1 @@\n"
        "-a\n+b\n"
    )
    assert _extract_diff_target_paths(diff) == ["lib/x.dart"]


def test_extract_diff_target_paths_multi_file_dedup():
    from app.crashguard.services.pr_drafter import _extract_diff_target_paths
    diff = (
        "--- a/lib/a.dart\n"
        "+++ b/lib/a.dart\n"
        "@@ -1 +1 @@\n-x\n+y\n"
        "--- a/lib/b.dart\n"
        "+++ b/lib/b.dart\n"
        "@@ -1 +1 @@\n-x\n+y\n"
        "--- a/lib/a.dart\n"  # 同文件重复出现
        "+++ b/lib/a.dart\n"
        "@@ -2 +2 @@\n-x\n+y\n"
    )
    assert _extract_diff_target_paths(diff) == ["lib/a.dart", "lib/b.dart"]


def test_extract_diff_target_paths_skips_dev_null():
    from app.crashguard.services.pr_drafter import _extract_diff_target_paths
    diff = (
        "--- a/lib/removed.dart\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n-a\n-b\n-c\n"
        "--- /dev/null\n"
        "+++ b/lib/added.dart\n"
        "@@ -0,0 +1,1 @@\n+new\n"
    )
    assert _extract_diff_target_paths(diff) == ["lib/added.dart"]


def test_extract_diff_target_paths_empty():
    from app.crashguard.services.pr_drafter import _extract_diff_target_paths
    assert _extract_diff_target_paths("") == []
    assert _extract_diff_target_paths("no diff here") == []


def _make_real_repo(repo_dir, files: dict[str, str]):
    """在 repo_dir 建一个真 git 仓，commit files。
    files: {relative_path: content}（content="" 即空 stub）
    """
    import subprocess
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.io"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo_dir, check=True)
    for p, c in files.items():
        f = repo_dir / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(c)
        subprocess.run(["git", "add", "--", p], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def test_is_empty_stub_detects_empty_file(tmp_path):
    """git 空 blob (e69de29b...) 应被识别为空 stub"""
    from app.crashguard.services.pr_drafter import _is_empty_stub_in_repo
    repo = tmp_path / "r"
    _make_real_repo(repo, {"lib/empty.dart": "", "lib/real.dart": "void main(){}\n"})
    assert _is_empty_stub_in_repo(str(repo), "lib/empty.dart") is True
    assert _is_empty_stub_in_repo(str(repo), "lib/real.dart") is False


def test_is_empty_stub_returns_false_for_missing_file(tmp_path):
    """目标文件不在 HEAD → 返回 False（不是空 stub，是不存在）"""
    from app.crashguard.services.pr_drafter import _is_empty_stub_in_repo
    repo = tmp_path / "r"
    _make_real_repo(repo, {"lib/x.dart": "content"})
    assert _is_empty_stub_in_repo(str(repo), "lib/nope.dart") is False


def test_is_empty_stub_returns_none_for_non_git(tmp_path):
    """非 git 目录 → 返回 None，让调用方降级"""
    from app.crashguard.services.pr_drafter import _is_empty_stub_in_repo
    d = tmp_path / "not_a_repo"
    d.mkdir()
    (d / "x.dart").write_text("hi")
    assert _is_empty_stub_in_repo(str(d), "x.dart") is None


def _setup_three_flutter_repos(tmp_path, monkeypatch, common_files, global_files, cn_files):
    """建三个 sub-repo 真 git 仓 + mock 路由"""
    from app.crashguard.services import pr_drafter
    wrapper = tmp_path / "plaud_ai"
    _make_real_repo(wrapper / "plaud-flutter-common", common_files)
    _make_real_repo(wrapper / "plaud-flutter-global", global_files)
    _make_real_repo(wrapper / "plaud-flutter-cn", cn_files)

    monkeypatch.setattr(
        pr_drafter, "get_crashguard_settings",
        lambda: type("S", (), {"repo_path_flutter": "", "repo_path_android": "",
                                "repo_path_ios": ""})(),
    )
    import app.config as cfg
    monkeypatch.setattr(cfg, "get_code_repo_for_platform", lambda _: str(wrapper))
    return wrapper


def test_detect_flutter_subrepo_by_blob_falls_back_to_global(tmp_path, monkeypatch):
    """common 是空 stub + global 有真代码 → 探测到 'global'"""
    from app.crashguard.services.pr_drafter import _detect_flutter_subrepo_by_blob
    _setup_three_flutter_repos(
        tmp_path, monkeypatch,
        common_files={"lib/push_helper.dart": ""},                  # 空 stub
        global_files={"lib/push_helper.dart": "class PushHelper{}"},  # 真实
        cn_files={"lib/push_helper.dart": "class PushHelperCN{}"},
    )
    diff = "--- a/lib/push_helper.dart\n+++ b/lib/push_helper.dart\n@@ -1 +1 @@\n-x\n+y\n"
    assert _detect_flutter_subrepo_by_blob(diff) == "global"


def test_detect_flutter_subrepo_by_blob_falls_back_to_cn_when_global_also_empty(tmp_path, monkeypatch):
    """common 空 + global 也空 + cn 有真代码 → 探测到 'cn'"""
    from app.crashguard.services.pr_drafter import _detect_flutter_subrepo_by_blob
    _setup_three_flutter_repos(
        tmp_path, monkeypatch,
        common_files={"lib/push_helper.dart": ""},
        global_files={"lib/push_helper.dart": ""},  # global 也空
        cn_files={"lib/push_helper.dart": "class PushHelperCN{}"},
    )
    diff = "--- a/lib/push_helper.dart\n+++ b/lib/push_helper.dart\n@@ -1 +1 @@\n-x\n+y\n"
    assert _detect_flutter_subrepo_by_blob(diff) == "cn"


def test_detect_flutter_subrepo_by_blob_returns_empty_when_common_real(tmp_path, monkeypatch):
    """common 已经是真代码 → 不切仓"""
    from app.crashguard.services.pr_drafter import _detect_flutter_subrepo_by_blob
    _setup_three_flutter_repos(
        tmp_path, monkeypatch,
        common_files={"lib/foo.dart": "real code"},
        global_files={"lib/foo.dart": "real code global"},
        cn_files={"lib/foo.dart": "real code cn"},
    )
    diff = "--- a/lib/foo.dart\n+++ b/lib/foo.dart\n@@ -1 +1 @@\n-x\n+y\n"
    assert _detect_flutter_subrepo_by_blob(diff) == ""


def test_detect_flutter_subrepo_by_blob_returns_empty_for_empty_diff():
    from app.crashguard.services.pr_drafter import _detect_flutter_subrepo_by_blob
    assert _detect_flutter_subrepo_by_blob("") == ""
    assert _detect_flutter_subrepo_by_blob("no diff") == ""


def test_resolve_candidate_repos_uses_blob_fallback_when_text_hint_missing(tmp_path, monkeypatch):
    """E2E：AI 没写文本 hint，但 fix_diff 文件在 common 是空 stub → 自动路由到 global"""
    from app.crashguard.services.pr_drafter import _resolve_candidate_repos
    _setup_three_flutter_repos(
        tmp_path, monkeypatch,
        common_files={"lib/push.dart": ""},
        global_files={"lib/push.dart": "real"},
        cn_files={"lib/push.dart": "real cn"},
    )
    fix_diff = "--- a/lib/push.dart\n+++ b/lib/push.dart\n@@ -1 +1 @@\n-x\n+y\n"
    cands = _resolve_candidate_repos(
        platform="flutter", fix_text="some generic fix", issue_stack="",
        fix_diff=fix_diff,
    )
    # 应该路由到 global 仓
    assert len(cands) == 1
    name, path = cands[0]
    assert name == "flutter"
    assert path.endswith("plaud-flutter-global")


def test_resolve_candidate_repos_text_hint_overrides_blob_probe(tmp_path, monkeypatch):
    """AI 明确写 'plaud-flutter-cn' → 不再走 blob 探测，直接路由 cn"""
    from app.crashguard.services.pr_drafter import _resolve_candidate_repos
    _setup_three_flutter_repos(
        tmp_path, monkeypatch,
        common_files={"lib/x.dart": ""},
        global_files={"lib/x.dart": "real"},  # blob 探测会选 global
        cn_files={"lib/x.dart": "real cn"},
    )
    fix_diff = "--- a/lib/x.dart\n+++ b/lib/x.dart\n@@ -1 +1 @@\n-x\n+y\n"
    cands = _resolve_candidate_repos(
        platform="flutter",
        fix_text="see plaud-flutter-cn/lib/x.dart",  # 文本 hint=cn
        fix_diff=fix_diff,
    )
    assert len(cands) == 1
    assert cands[0][1].endswith("plaud-flutter-cn")


def test_resolve_candidate_repos_no_fallback_when_common_real(tmp_path, monkeypatch):
    """common 已有真代码 → 保留 common，不切仓"""
    from app.crashguard.services.pr_drafter import _resolve_candidate_repos
    _setup_three_flutter_repos(
        tmp_path, monkeypatch,
        common_files={"lib/util.dart": "void util(){}"},
        global_files={"lib/util.dart": "void util(){}"},
        cn_files={"lib/util.dart": "void util(){}"},
    )
    fix_diff = "--- a/lib/util.dart\n+++ b/lib/util.dart\n@@ -1 +1 @@\n-x\n+y\n"
    cands = _resolve_candidate_repos(
        platform="flutter", fix_text="", fix_diff=fix_diff,
    )
    assert len(cands) == 1
    assert cands[0][1].endswith("plaud-flutter-common")
