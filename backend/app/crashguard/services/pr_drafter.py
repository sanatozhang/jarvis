"""
半自动 PR 草稿生成器。

闭环：人工 ✋ approve → 调本服务 → git checkout 新分支 →
     首选：git apply AI 产出的 fix_diff（真代码改动）
     回退：apply 失败时写 .crashguard/fixes/<id>.md（修复说明文档）
     → commit → push → gh pr create --draft → 写回 crash_pull_requests

🚫 严禁调用：gh pr merge / git merge / gh pr ready —— 永远只创建 draft。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import select

from app.crashguard.config import get_crashguard_settings
from app.crashguard.models import CrashAnalysis, CrashIssue, CrashPullRequest
from app.db.database import get_session

logger = logging.getLogger("crashguard.pr_drafter")

# Per-repo 锁：同一 repo_path 的 git 操作必须串行（防止并发触发 daily auto + UI 手动同时 push 撞 index）
_repo_locks: Dict[str, asyncio.Lock] = {}
_repo_locks_guard = asyncio.Lock()


async def _acquire_repo_lock(repo_path: str) -> asyncio.Lock:
    """获取（或新建）针对 repo_path 的 asyncio.Lock。
    用法: `async with await _acquire_repo_lock(path):` —— 注意 await 在 async with 内。
    """
    async with _repo_locks_guard:
        lock = _repo_locks.get(repo_path)
        if lock is None:
            lock = asyncio.Lock()
            _repo_locks[repo_path] = lock
        return lock


# 仅当命令是 git 时才需检查这些子命令；只匹配 args[1] 的精确子命令名，
# 不再扫整个 cmd 数组（避免 PR body / commit message 中的自然词触发误判）。
_FORBIDDEN_GIT_SUBCOMMANDS = {"merge", "rebase"}
_FORBIDDEN_GIT_FLAGS = {"--merge", "--rebase", "--squash"}
# gh 子命令：永远禁止 merge / ready（draft → ready 不允许）
_FORBIDDEN_GH_SUBCOMMANDS = {"merge", "ready"}


def _resolve_candidate_repos(
    platform: str, fix_text: str, issue_stack: str = "",
) -> list[tuple[str, str]]:
    """根据 platform + fix_suggestion 文本，返回所有候选 sub-repo 列表。

    Plaud 顶层设计：Flutter 双端 app + 原生包装。一个崩溃可能跨多仓库——
    比如 Android ANR 的修复同时在 plaud-flutter-common（dart 层暂停动画）和
    plaud-android（原生 Activity 配置）。这里通过文本特征探测涉及的所有仓库。

    返回 [(logical_name, abs_path), ...]，已去重，platform 默认仓库始终在第一位。
    """
    out: list[tuple[str, str]] = []
    seen: set = set()
    text = ((fix_text or "") + " " + (issue_stack or "")).lower()

    def add(name: str) -> None:
        path = _platform_repo_path(name)
        if path and path not in seen and Path(path).exists():
            out.append((name, path))
            seen.add(path)

    p = (platform or "").strip().lower()
    if p in ("android", "ios", "flutter"):
        add(p)

    has_dart = (".dart" in text) or ("pubspec" in text) or ("flutter" in text)
    has_kotlin_java = (
        ".kt" in text or ".java" in text or "androidmanifest" in text
        or "mainactivity" in text or ".gradle" in text or "fragmentactivity" in text
    )
    has_swift_objc = (
        ".swift" in text or "appdelegate" in text or "viewcontroller" in text
        or "podfile" in text or ".plist" in text
    )
    if has_dart and p != "flutter":
        add("flutter")
    if has_kotlin_java and p != "android":
        add("android")
    if has_swift_objc and p != "ios":
        add("ios")
    return out


def _platform_repo_path(platform: str) -> Optional[str]:
    """根据 platform 返回真实 sub-repo 绝对路径（不是 monorepo wrapper）。

    Plaud2 是 git submodule 壳，不接 PR。每个端有独立 sub-repo：
      android  → CODE_REPO_PATH/plaud-android
      ios      → CODE_REPO_PATH/plaud-ios
      flutter  → CODE_REPO_PATH/plaud-flutter-common（默认；可通过 yaml override）

    优先级：
      1. crashguard.repo_paths.<platform> 显式配置（绝对路径）
      2. CODE_REPO_PATH 下的标准 sub-repo 子目录
      3. None（拒绝创建 PR）
    """
    s = get_crashguard_settings()
    p = (platform or "").strip().lower()
    direct = {
        "flutter": s.repo_path_flutter,
        "android": s.repo_path_android,
        "ios": s.repo_path_ios,
    }.get(p, "")
    if direct:
        return os.path.expanduser(direct)

    from app.config import get_code_repo_for_platform
    wrapper = get_code_repo_for_platform("app") or ""
    if not wrapper:
        return None
    wrapper = os.path.expanduser(wrapper)

    sub_default = {
        "android": "plaud-android",
        "ios": "plaud-ios",
        "flutter": "plaud-flutter-common",
    }.get(p)
    if not sub_default:
        return None
    candidate = os.path.join(wrapper, sub_default)
    # submodule 里 .git 是文件不是目录，用 os.path.exists 兼容两种形态
    return candidate if os.path.exists(os.path.join(candidate, ".git")) else None


def _stack_matches_platform(platform: str, fix_text: str) -> bool:
    """从 fix_suggestion/diff/stack 拼接文本里 grep 文件扩展名/类名/路径，验证至少
    有一个匹配该平台的代码标识。

    底层逻辑：issue.platform 来自 Datadog @platform tag 已可信，本校验是兜底防 AI
    跨平台串台。Plaud 是 Flutter 双端 app——platform=android/ios 的 issue 经常需要
    改 dart 代码，所以 Android/iOS 关键字白名单纳入 Flutter（.dart/pubspec/lib/）
    与常见类名（mainactivity/appdelegate 等无扩展名形式）。

    返回 False 时调用方应拒绝建 PR。
    """
    if not fix_text:
        return True
    text = fix_text.lower()
    p = (platform or "").strip().lower()
    if p == "android":
        return any(kw in text for kw in (
            # 原生 android 文件 / manifest / 目录
            ".kt", ".java", ".gradle", "androidmanifest", "app/src/main",
            # 类名（无扩展名形式，AI 描述常用）
            "mainactivity", "fragmentactivity", "fragment ", "activity.", "application.",
            "kotlin", "java ",
            # Plaud 是 Flutter app，platform=android 的崩溃常需改 dart 层
            ".dart", "pubspec", "lib/", "flutter",
        ))
    if p == "ios":
        return any(kw in text for kw in (
            ".swift", ".m\n", ".mm", ".plist", "appdelegate", "podfile", "runner/",
            "viewcontroller", "uiview", "nsobject", "objective-c", "swift ",
            ".dart", "pubspec", "lib/", "flutter",
        ))
    if p == "flutter":
        return any(kw in text for kw in (".dart", "pubspec", "lib/", "flutter"))
    return True


def _safe_branch_name(issue_id: str, platform: str) -> str:
    short = re.sub(r"[^a-zA-Z0-9]", "", issue_id)[:8] or "noid"
    ts = datetime.utcnow().strftime("%Y%m%d%H%M")
    return f"crashguard/{platform.lower()}/{short}-{ts}"


def _run_git(cmd: list[str], cwd: str, timeout: int = 60) -> tuple[int, str, str]:
    """运行 git 或 gh 命令，仅检查命令本身的子命令/选项部分，避免 PR body 文本误判。

    安全策略：
    - cmd[0] == "git"  → 拒绝 merge/rebase 子命令、--merge/--rebase/--squash 选项
    - cmd[0] == "gh"   → 拒绝 merge / ready 子命令（保 draft 状态）
    - 其他二进制不检查（保持灵活）
    - 只扫 cmd 自身参数，不扫 -m/--body 后面的消息内容（自然语言可包含敏感词）
    """
    if cmd:
        program = cmd[0]
        # 跳过 -m / --body / --title 等 flag 后面的实参（那是 message，不是 cmd 自身）
        skip_next = False
        message_flags = {"-m", "--message", "--body", "--title", "--body-file"}
        for i, arg in enumerate(cmd[1:], start=1):
            if skip_next:
                skip_next = False
                continue
            if arg in message_flags:
                skip_next = True
                continue
            if arg.startswith("--") and "=" in arg:
                # 形如 --body=xxx 也跳过
                key = arg.split("=", 1)[0]
                if key in message_flags:
                    continue
            # 检查
            if program == "git":
                if i == 1 and arg in _FORBIDDEN_GIT_SUBCOMMANDS:
                    return 1, "", f"forbidden git subcommand: {arg}"
                if arg in _FORBIDDEN_GIT_FLAGS:
                    return 1, "", f"forbidden git flag: {arg}"
            elif program == "gh":
                if arg in _FORBIDDEN_GH_SUBCOMMANDS:
                    return 1, "", f"forbidden gh subcommand: {arg}"
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"timeout after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


def _resolve_remote_name(repo_path: str) -> str:
    """解析当前 repo 的"主"远端名。默认 origin；不存在则取 git remote 第一个。

    底层逻辑：仓库不一定叫 origin（102 上 Plaud Flutter 仓库的 remote 叫 'merge'）。
    硬编码 origin 会让 fetch/push 全挂——这是 owner 意识缺位的硬编码，自适应才对。
    """
    rc, stdout, _ = _run_git(
        ["git", "remote"], repo_path, timeout=10,
    )
    if rc != 0:
        return "origin"
    remotes = [r.strip() for r in (stdout or "").splitlines() if r.strip()]
    if not remotes:
        return "origin"
    if "origin" in remotes:
        return "origin"
    return remotes[0]


def _default_base_ref(repo_path: str) -> str:
    """解析远端默认分支，避免把所有移动端仓库都硬编码成 origin/main。

    远端名通过 _resolve_remote_name 自适应（102 Plaud 仓库 remote 叫 'merge'）。
    """
    remote = _resolve_remote_name(repo_path)
    rc, stdout, _ = _run_git(
        ["git", "rev-parse", "--abbrev-ref", f"{remote}/HEAD"],
        repo_path,
        timeout=15,
    )
    ref = stdout.strip()
    if rc == 0 and ref.startswith(f"{remote}/") and ref != f"{remote}/HEAD":
        return ref

    for fallback in (f"{remote}/main", f"{remote}/master"):
        rc, _, _ = _run_git(
            ["git", "rev-parse", "--verify", fallback],
            repo_path,
            timeout=15,
        )
        if rc == 0:
            return fallback
    return f"{remote}/main"


def _worktree_dirty(repo_path: str) -> tuple[bool, str]:
    """返回工作树是否有**已跟踪文件**未提交改动；自动 PR 不应覆盖工程师本地改动。

    口径（owner 三板斧砍掉假阻塞）：
    - 忽略 submodule pointer 改动（`--ignore-submodules=all`）
    - 忽略 untracked 文件（`-uno`）—— auto-PR 不动 untracked 路径，
      102 上仓库常残留 `.DS_Store / .cursor/ / .jenkins_*/` 等系统垃圾，
      不属于"工程师在改"的信号，不该阻塞 PR
    - 只看：modified (M)、staged (A/D/R/C) 等跟踪文件状态
    """
    rc, stdout, stderr = _run_git(
        ["git", "status", "--porcelain", "--ignore-submodules=all", "-uno"],
        repo_path, timeout=15,
    )
    if rc != 0:
        return True, f"git status failed: {stderr}"
    return bool(stdout.strip()), stdout.strip()


def _cleanup_repo_after_pr(
    repo_path: str,
    base_ref: str,
    initial_branch: str,
    branch_to_delete: str,
    pushed_to_remote: bool,
) -> None:
    """自愈：流程结束后把 sub-repo 恢复到 base_ref 干净态。

    底层逻辑：本流程失败后，工作树会残留 AI agent 写的代码改动 / .crashguard/ 临时文件 /
    新建分支。若不清理，下次触发 `_worktree_dirty` 直接拒绝——一次失败永久失败。

    保守策略：
      - 只 reset 已跟踪文件（保留 submodule pointer 等用户改动）
      - 清理 .crashguard/ 临时文件
      - 切回原分支（流程进入前所在分支），如失败则回退 base_ref
      - 仅删除本流程**未推送到远端**的临时分支；已 push 的保留（PR 用得着）
    """
    try:
        # 1. 回滚跟踪文件改动（不动 untracked + submodule pointer 由 --ignore-submodules 自然不动）
        _run_git(["git", "checkout", "--", "."], repo_path, timeout=15)
        # 2. 清 .crashguard/ 临时文件（保留其它 untracked）
        _run_git(["git", "clean", "-fd", "--", ".crashguard"], repo_path, timeout=10)
        # 3. 切回原 branch；失败回退 base_ref
        target = initial_branch or base_ref.replace("origin/", "")
        rc, _, _ = _run_git(["git", "checkout", target], repo_path, timeout=30)
        if rc != 0:
            _run_git(
                ["git", "checkout", base_ref.replace("origin/", "")],
                repo_path, timeout=30,
            )
        # 4. 删本次创建的临时分支（已 push 的保留——远端 PR 仍指向它）
        if branch_to_delete and not pushed_to_remote:
            _run_git(["git", "branch", "-D", branch_to_delete], repo_path, timeout=10)
    except Exception:
        logger.exception("post-pr cleanup failed (non-fatal, manual reset may be needed)")


def _parse_gitmodules(repo_path: str) -> list[dict]:
    """解析 .gitmodules → [{"path": "<sub_rel>", "url": "<remote>"}, ...]。

    底层逻辑：crashguard agent 在父 repo 工作树 Edit 文件时，可能落在 submodule 路径下
    （如 plaud-android 的 nicebuildSDK/、plaud-ios 的 PLAUD/PenSubmodules/）。需要识别
    submodule 边界，把改动路由到 submodule 自己的 GitHub repo 开独立 PR，而不是把
    submodule 源码 commit 进父 repo。
    """
    gm_path = os.path.join(repo_path, ".gitmodules")
    if not os.path.isfile(gm_path):
        return []
    out: list[dict] = []
    cur: dict = {}
    try:
        with open(gm_path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if line.startswith("[submodule"):
                    if cur.get("path"):
                        out.append(cur)
                    cur = {}
                elif "=" in line:
                    k, _, v = line.partition("=")
                    k, v = k.strip(), v.strip()
                    if k in ("path", "url"):
                        cur[k] = v
        if cur.get("path"):
            out.append(cur)
    except Exception as exc:
        logger.warning("parse .gitmodules failed: %s", exc)
        return []
    return [x for x in out if x.get("path")]


def _submodule_init_state(repo_path: str, sub_rel_path: str) -> tuple[bool, str]:
    """检查 <repo_path>/<sub_rel_path> 处的 submodule 是否已 init。

    init 判定：submodule 目录内有 `.git`（文件指针或目录都行）且 `git rev-parse --git-dir`
    在该目录内成功。未 init 时父 repo 容易把它当普通目录，导致 `git add -A` 把 submodule
    源码当普通文件 commit 进父 repo——这是用户反馈的核心 bug。
    """
    abs_sub = os.path.join(repo_path, sub_rel_path)
    if not os.path.isdir(abs_sub):
        return False, f"{sub_rel_path}: directory missing (submodule not checked out)"
    git_marker = os.path.join(abs_sub, ".git")
    if not os.path.exists(git_marker):
        return False, f"{sub_rel_path}/.git not found (run: git submodule update --init {sub_rel_path})"
    rc, _, err = _run_git(["git", "rev-parse", "--git-dir"], abs_sub, timeout=10)
    if rc != 0:
        return False, f"{sub_rel_path}: not a git repo ({err[:80]})"
    return True, ""


def _classify_changed_files(
    repo_path: str, changed_files: list[str], submodules: list[dict],
) -> dict:
    """把 changed_files 按 submodule 边界分桶。

    返回:
      {
        "parent": [<相对 repo_path>],
        "submodules": {
          "<sub_rel>": {"abs_path", "url", "initialized", "init_detail", "files": [<相对 submodule 根>]},
          ...
        }
      }

    长 path 优先匹配（如 PLAUD/PenSubmodules）防嵌套误分。
    """
    sm_sorted = sorted(submodules, key=lambda x: len(x.get("path", "")), reverse=True)
    parent: list[str] = []
    sub_map: dict[str, dict] = {}
    for raw in changed_files:
        # 注意：`lstrip("./")` 是 strip 字符集（任何 . 或 /），不是 strip prefix！
        # 之前误用导致 `.DS_Store` → `DS_Store`、`.gitignore` → `gitignore`，
        # 后续 `git add` 拿不到原始文件名 → "pathspec did not match any files"
        f = raw.replace("\\", "/")
        while f.startswith("./"):
            f = f[2:]
        placed = False
        for sm in sm_sorted:
            sp = sm.get("path", "").rstrip("/")
            if not sp:
                continue
            if f == sp or f.startswith(sp + "/"):
                rel = f[len(sp) + 1:] if f.startswith(sp + "/") else ""
                entry = sub_map.setdefault(sp, {
                    "abs_path": os.path.join(repo_path, sp),
                    "url": sm.get("url", ""),
                    "files": [],
                })
                if rel:
                    entry["files"].append(rel)
                placed = True
                break
        if not placed:
            parent.append(f)
    for sm_path, info in sub_map.items():
        ok, detail = _submodule_init_state(repo_path, sm_path)
        info["initialized"] = ok
        info["init_detail"] = detail
    return {"parent": parent, "submodules": sub_map}


def _build_commit_msg(
    issue: "CrashIssue", ana: "CrashAnalysis", impl_source: str, files: list[str],
) -> str:
    via = "implementation agent" if impl_source == "agent" else "fix_diff text"
    file_summary = ", ".join(files[:5]) if files else "see diff"
    return (
        f"fix(crashguard): {(issue.title or ana.datadog_issue_id)[:60]}\n\n"
        f"AI-generated patch via {via} for crash issue {ana.datadog_issue_id}.\n"
        f"Files: {file_summary}\n"
        f"Confidence: {ana.confidence or 'low'} · Feasibility: {ana.feasibility_score:.2f}\n"
        f"Reviewer must verify diff correctness before merge."
    )


async def _create_one_draft_pr(
    *,
    cwd: str,
    branch: str,
    files_to_add: Optional[list[str]],
    commit_message: str,
    pr_title: str,
    pr_body: str,
    analysis_id: int,
    repo_logical: str,
    approver: str,
    change_kind: str,
    prep_branch: bool,
) -> tuple[dict, bool]:
    """单仓库 PR 内核：在 cwd 里 add → commit → push → gh pr create --draft → 写 DB。

    prep_branch=True 时（submodule 场景），先把脏工作树 stash 起来、fetch origin、checkout
    到 base_ref 的新临时分支、再 pop stash——把改动迁移到一个干净的 PR base 上。

    返回 (result_dict, pushed_bool)。pushed_bool 用于上层 cleanup 决定是否删本地分支。
    """
    pushed = False

    if prep_branch:
        rc, status_out, _ = _run_git(["git", "status", "--porcelain"], cwd, timeout=10)
        has_dirty = bool((status_out or "").strip())
        stash_pushed = False
        if has_dirty:
            rc, _, err = _run_git(
                ["git", "stash", "push", "-u", "-m", f"crashguard-{branch}"], cwd, timeout=30,
            )
            if rc != 0:
                return {"ok": False, "error": f"submodule git stash failed: {err}",
                        "repo": repo_logical, "branch_name": branch}, pushed
            stash_pushed = True
        remote_name = _resolve_remote_name(cwd)
        rc, _, err = _run_git(["git", "fetch", remote_name], cwd, timeout=180)
        if rc != 0:
            if stash_pushed:
                _run_git(["git", "stash", "pop"], cwd, timeout=15)
            return {"ok": False, "error": f"submodule git fetch failed: {err}",
                    "repo": repo_logical, "branch_name": branch}, pushed
        sub_base = _default_base_ref(cwd)
        rc, _, err = _run_git(["git", "checkout", "--detach", sub_base], cwd, timeout=30)
        if rc != 0:
            if stash_pushed:
                _run_git(["git", "stash", "pop"], cwd, timeout=15)
            return {"ok": False, "error": f"submodule checkout {sub_base} failed: {err}",
                    "repo": repo_logical, "branch_name": branch}, pushed
        _run_git(["git", "branch", "-D", branch], cwd, timeout=10)
        rc, _, err = _run_git(["git", "checkout", "-b", branch, sub_base], cwd, timeout=30)
        if rc != 0:
            if stash_pushed:
                _run_git(["git", "stash", "pop"], cwd, timeout=15)
            return {"ok": False, "error": f"submodule checkout -b {branch} failed: {err}",
                    "repo": repo_logical, "branch_name": branch}, pushed
        if stash_pushed:
            rc, _, err = _run_git(["git", "stash", "pop"], cwd, timeout=30)
            if rc != 0:
                _run_git(["git", "stash", "drop"], cwd, timeout=10)
                return {"ok": False, "error": (
                    f"submodule stash pop conflict (agent edits diverge from submodule's "
                    f"origin/main, manual merge needed): {err[:200]}"),
                    "repo": repo_logical, "branch_name": branch}, pushed

    # `git add -A` 在大仓库（Flutter 主仓含 5 个 submodule）默认 60s 不够，提到 180s
    if files_to_add is None:
        rc, _, err = _run_git(["git", "add", "-A"], cwd, timeout=180)
    else:
        if not files_to_add:
            return {"ok": False, "error": "no files to add", "repo": repo_logical,
                    "branch_name": branch}, pushed
        rc, _, err = _run_git(["git", "add", "--"] + files_to_add, cwd, timeout=180)
    if rc != 0:
        return {"ok": False, "error": f"git add failed: {err}", "repo": repo_logical,
                "branch_name": branch}, pushed

    rc, _, err = _run_git(
        ["git", "commit", "-m", commit_message, "--no-verify"], cwd, timeout=30,
    )
    if rc != 0:
        return {"ok": False, "error": f"git commit failed: {err}", "repo": repo_logical,
                "branch_name": branch}, pushed

    push_remote = _resolve_remote_name(cwd)
    rc, _, err = _run_git(["git", "push", "-u", push_remote, branch], cwd, timeout=120)
    if rc != 0:
        return {"ok": False, "error": f"git push failed: {err}", "repo": repo_logical,
                "branch_name": branch}, pushed
    pushed = True

    rc, stdout, err = _run_git(
        ["gh", "pr", "create", "--draft", "--title", pr_title, "--body", pr_body,
         "--head", branch],
        cwd, timeout=120,
    )
    if rc != 0:
        return {"ok": False, "error": f"gh pr create failed: {err}",
                "branch_name": branch, "repo": repo_logical, "pushed": pushed}, pushed
    pr_url = stdout.strip().splitlines()[-1] if stdout else ""
    pr_number = None
    m = re.search(r"/pull/(\d+)", pr_url)
    if m:
        pr_number = int(m.group(1))

    triggered_by = "auto_verified" if (approver or "").lower() in {"auto", "backfill"} else "human_approved"
    async with get_session() as session:
        ana_row = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.id == analysis_id)
        )).scalar_one_or_none()
        if ana_row is not None:
            row = CrashPullRequest(
                analysis_id=analysis_id,
                datadog_issue_id=ana_row.datadog_issue_id,
                repo=repo_logical,
                branch_name=branch,
                pr_url=pr_url,
                pr_number=pr_number,
                pr_status="draft",
                triggered_by=triggered_by,
                approved_by=approver or "human",
                approved_at=datetime.utcnow(),
                created_at=datetime.utcnow(),
            )
            session.add(row)
            await session.commit()

    logger.info(
        "crashguard draft PR created: %s (repo=%s analysis=%d kind=%s)",
        pr_url, repo_logical, analysis_id, change_kind,
    )
    return {
        "ok": True, "pr_url": pr_url, "pr_number": pr_number, "branch_name": branch,
        "repo": repo_logical, "triggered_by": triggered_by, "patch_applied": True,
        "change_kind": change_kind, "pushed": pushed,
    }, pushed


def _post_pr_cleanup_submodule(sm_abs: str, branch: str, pushed: bool) -> None:
    """submodule 流程结束后把 worktree 拉回 base_ref；已 push 的 branch 保留。"""
    try:
        _run_git(["git", "reset", "--hard", "HEAD"], sm_abs, timeout=15)
        _run_git(["git", "clean", "-fd", "--", ".crashguard"], sm_abs, timeout=10)
        base_ref = _default_base_ref(sm_abs)
        _run_git(["git", "checkout", "--detach", base_ref], sm_abs, timeout=30)
        if branch and not pushed:
            _run_git(["git", "branch", "-D", branch], sm_abs, timeout=10)
    except Exception:
        logger.exception("submodule cleanup failed for %s (non-fatal)", sm_abs)


def _normalize_diff_for_apply(raw_diff: str, sub_repo_dirname: str) -> str:
    """把 AI 输出的 diff 路径前缀统一掉，避免 apply 找不到文件。

    AI 在 prompt 指引下"应该"产出相对 sub-repo 根的路径（如 `lib/foo.dart`），
    但模型偶尔会写 `code/plaud-flutter-common/lib/foo.dart` 或 `a/code/.../lib/foo.dart`。
    这里做一道安全垫：把 `(a|b)/code/<sub_repo>/` 前缀拿掉，回到子仓库根相对路径。
    """
    if not raw_diff or not sub_repo_dirname:
        return raw_diff
    pat_a = re.compile(rf"^(--- a/)(?:code/)?{re.escape(sub_repo_dirname)}/", re.MULTILINE)
    pat_b = re.compile(rf"^(\+\+\+ b/)(?:code/)?{re.escape(sub_repo_dirname)}/", re.MULTILINE)
    pat_a_root = re.compile(r"^(--- a/)code/[^/]+/", re.MULTILINE)
    pat_b_root = re.compile(r"^(\+\+\+ b/)code/[^/]+/", re.MULTILINE)
    out = pat_a.sub(r"\1", raw_diff)
    out = pat_b.sub(r"\1", out)
    # 兜底：若 sub_repo 名字 AI 写错（比如 plaud-flutter 而不是 plaud-flutter-common），
    # 仍然把 code/<anything>/ 前缀拿掉，让 apply 试一次
    out = pat_a_root.sub(r"\1", out)
    out = pat_b_root.sub(r"\1", out)
    return out


async def _run_implementation_agent(
    repo_path: str, ana: CrashAnalysis, issue: CrashIssue,
) -> tuple[bool, list[str], str]:
    """在 sub-repo 工作树里跑 Claude Code agent，让它根据 fix_suggestion 直接 Edit 真文件。

    底层逻辑：让 LLM 写 unified diff 是反人性的（行号/escape 错就 apply 失败）；
    更稳的做法是让 agent 直接在真 repo 改文件，最后用 `git diff HEAD --stat` 抽出
    实际改动——文件改动是原子事实，比文本 diff 可信。

    Returns: (changed, changed_files, error)
    """
    from app.services.agent_orchestrator import AgentOrchestrator

    fix_text = (ana.fix_suggestion or ana.solution or "").strip()
    if not fix_text:
        return False, [], "no fix_suggestion to implement"

    sub_name = os.path.basename(repo_path.rstrip("/"))
    prompt = f"""你是 Plaud senior 工程师。当前 cwd 是 git 仓库 `{sub_name}` 的工作树（已 checkout 到一个临时分支）。

## 你的任务
根据下方修复方案，**用 Edit/Write 工具直接修改源码**，让仓库工作树产生真实代码改动。
不要写 markdown 说明、不要写 diff 文本——目标是产出真 patch。

## Issue
- platform: {issue.platform or 'unknown'}
- title: {(issue.title or '')[:200]}

## 修复方案（来自 root cause analysis）
{fix_text[:4500]}

## 严格工作流
1. 用 Glob / Grep 在 cwd 内**定位真实文件路径**（不要凭印象编路径）
2. 用 Read 读关键文件确认行号和上下文
3. 用 Edit 工具精准修改——优先单文件单函数，改动 ≤ 30 行
4. 改完后**必须**用 Write 写一份 `.crashguard/impl_report.json`：
```json
{{"changed_files": ["<相对仓库根>"], "summary": "一句话说明"}}
```

## 红线（违反 = 失败）
- ⚠️ 禁止调用 git / gh / Bash 做任何 commit/push/checkout/branch 操作（外部脚本会做）
- ⚠️ 如果判断本仓库不该改（修复在另一仓库）→ Write impl_report.json 写 changed_files=[] + summary 说明原因，**不要硬改**
- ⚠️ 不要 Read .git 内部文件
- ⚠️ 不要做超出修复方案范围的"顺手优化"
"""
    orch = AgentOrchestrator()
    try:
        agent = orch.select_agent(rule_type="crashguard")
    except Exception as e:
        return False, [], f"agent select failed: {e}"

    # ClaudeCodeAgent 会在 cwd 写 prompt.md 和 output/——必须清理避免污染 sub-repo
    import shutil
    workspace = Path(repo_path)
    pre_existed_prompt = (workspace / "prompt.md").exists()
    pre_existed_output = (workspace / "output").exists()

    try:
        await asyncio.wait_for(
            agent.analyze(workspace=workspace, prompt=prompt),
            timeout=600,
        )
    except asyncio.TimeoutError:
        return False, [], "implementation agent timeout (10min)"
    except Exception as e:
        return False, [], f"implementation agent error: {e}"
    finally:
        # 无论成功失败，都清理 agent 留的临时文件（仅当 sub-repo 之前没有这些文件）
        if not pre_existed_prompt:
            (workspace / "prompt.md").unlink(missing_ok=True)
        if not pre_existed_output:
            shutil.rmtree(workspace / "output", ignore_errors=True)

    # git diff 抽改动事实——untracked 也算（包含 agent 新建的文件）
    rc, stdout, stderr = _run_git(
        ["git", "status", "--porcelain"], repo_path, timeout=15,
    )
    if rc != 0:
        return False, [], f"git status failed: {stderr.strip()[:200]}"
    changed: list[str] = []
    for ln in stdout.splitlines():
        # 形如 " M lib/foo.dart"、"?? new_file.dart"、" D old.dart"
        if len(ln) < 4:
            continue
        path = ln[3:].strip().strip('"')
        if not path:
            continue
        # 过滤掉临时/报告文件
        if (path.startswith(".crashguard/") or path == "prompt.md"
                or path.startswith("output/") or path.endswith(".dump.txt")):
            continue
        changed.append(path)
    if not changed:
        return False, [], "agent produced no source changes"
    # 兜底校验：agent 偶尔在 impl_report.json 写残缺路径（首字母被剥离等模型 hallucination），
    # 这种路径 git add 会炸 "pathspec did not match any files"。落 add 前先确认文件真存在。
    real = [f for f in changed if (Path(repo_path) / f).exists()]
    if not real:
        return False, [], f"agent reported changed files but none exist on disk: {changed[:5]}"
    if len(real) != len(changed):
        missing = [f for f in changed if f not in real]
        logger.warning("filtered out %d non-existent paths from impl_report: %s", len(missing), missing[:3])
    return True, real, ""


def _try_apply_fix_diff(
    repo_path: str, raw_diff: str, sub_repo_dirname: str
) -> tuple[bool, str]:
    """尝试把 fix_diff apply 到 sub-repo。返回 (是否成功, 错误信息)。

    走 stdin，避免在 repo 里留一个临时 .patch 文件污染。
    --3way 允许做 3-way merge fallback；--recount 修一些行数对不齐。
    apply 之前先 --check 一次拦掉明显不对的 patch。
    """
    if not raw_diff or not raw_diff.strip():
        return False, "empty diff"
    normalized = _normalize_diff_for_apply(raw_diff, sub_repo_dirname)
    # 末尾确保有换行——很多 git apply 实现对不带尾行的 patch 报错
    if not normalized.endswith("\n"):
        normalized += "\n"
    try:
        # 1) check
        check = subprocess.run(
            ["git", "apply", "--check", "--3way", "--recount", "-"],
            cwd=repo_path,
            input=normalized,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check.returncode != 0:
            return False, f"git apply --check failed: {check.stderr.strip()[:400]}"
        # 2) real apply
        real = subprocess.run(
            ["git", "apply", "--3way", "--recount", "-"],
            cwd=repo_path,
            input=normalized,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if real.returncode != 0:
            return False, f"git apply failed: {real.stderr.strip()[:400]}"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "git apply timeout"
    except Exception as exc:
        return False, f"git apply exception: {exc}"


def _build_pr_body(
    issue: CrashIssue,
    ana: CrashAnalysis,
    frontend_url: str,
    patch_applied: bool,
) -> str:
    """拼装 PR description（markdown）。"""
    if patch_applied:
        header_note = "✅ **AI 已落 patch 到代码**——本 PR 的 Files changed 即为修复 diff，请工程师 review 后合入。"
    else:
        header_note = "⚠️ **未自动 patch 代码**——本 PR 仅包含修复说明文档，工程师需手动改代码。"
    lines = [
        f"## Crashguard 半自动 PR — {issue.platform or 'unknown'}",
        "",
        header_note,
        "",
        f"**Issue**: `{issue.datadog_issue_id}`",
        f"**Title**: {issue.title or ''}",
        f"**Frontend**: {frontend_url}",
        f"**Confidence**: {ana.confidence or 'low'}  ·  **Feasibility**: {ana.feasibility_score:.2f}",
        "",
        "### 根因",
        ana.root_cause or "_(空)_",
        "",
        "### 修复建议",
        ana.fix_suggestion or ana.solution or "_(空)_",
    ]
    if (ana.fix_diff or "").strip():
        lines += ["", "### AI 提议的 diff（参考）", "```diff", ana.fix_diff.strip(), "```"]
    lines += [
        "",
        "---",
        "🤖 Generated by Crashguard. **DO NOT auto-merge** — manual review + approve required.",
    ]
    return "\n".join(lines)


async def draft_pr_for_analysis(
    analysis_id: int,
    approver: str = "human",
    dry_run: bool = False,
    repo_override: Optional[tuple[str, str]] = None,
) -> Dict[str, Any]:
    """
    入口：根据 analysis_id 生成 draft PR。

    repo_override: 可选 (logical_name, abs_path)。multi-PR wrapper 在循环建多 PR
                  时显式传入；为 None 时按 issue.platform 解析默认仓库。

    返回：{ok, pr_url?, branch_name?, dry_run?, error?, repo?}
    """
    s = get_crashguard_settings()
    if not s.pr_enabled:
        return {"ok": False, "error": "pr_disabled"}

    async with get_session() as session:
        ana = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.id == analysis_id)
        )).scalar_one_or_none()
        if ana is None:
            return {"ok": False, "error": f"analysis {analysis_id} not found"}
        if ana.status != "success":
            return {"ok": False, "error": f"analysis status={ana.status}, not success"}
        if not (ana.fix_suggestion or ana.solution or ana.fix_diff):
            return {"ok": False, "error": "no fix content (fix_suggestion/solution/fix_diff all empty)"}

        issue = (await session.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == ana.datadog_issue_id)
        )).scalar_one_or_none()
        if issue is None:
            return {"ok": False, "error": f"issue {ana.datadog_issue_id} not found"}

        # 30 天去重 — multi-PR 场景下按 (issue, repo) 联合去重，每个 repo 各自有窗
        repo_dedup_key = (
            repo_override[0] if repo_override else (issue.platform or "").lower()
        )
        since = datetime.utcnow() - timedelta(days=s.pr_dedup_days)
        existing = (await session.execute(
            select(CrashPullRequest).where(
                CrashPullRequest.datadog_issue_id == ana.datadog_issue_id,
                CrashPullRequest.repo == repo_dedup_key,
                CrashPullRequest.created_at >= since,
            )
        )).scalars().first()
        if existing is not None and not dry_run:
            return {
                "ok": False,
                "error": f"dup_within_{s.pr_dedup_days}d",
                "existing_pr_url": existing.pr_url,
                "existing_branch": existing.branch_name,
            }

    platform = (issue.platform or "").lower()
    if repo_override:
        repo_logical, repo_path = repo_override
    else:
        repo_logical = platform
        repo_path = _platform_repo_path(platform)
    if not repo_path or not Path(repo_path).exists():
        return {"ok": False, "error": f"repo_path not configured/found for platform={platform} repo={repo_logical}"}

    # Stack 验证：把 fix_suggestion + fix_diff + 该 issue 的崩溃栈一起作为匹配源。
    # 底层逻辑：issue.platform 来自 Datadog @platform tag，本身可信；这道闸是兜底
    # 防 AI 跨平台串台。崩溃栈本身就是该平台，只要 representative_stack 含平台标识
    # 即视为对齐——避免因 AI 给纯中文描述（无 .kt/.java 字面）就误杀。
    match_text = "\n".join([
        ana.fix_suggestion or "",
        ana.solution or "",
        ana.fix_diff or "",
        issue.representative_stack or "",
    ])
    if not _stack_matches_platform(platform, match_text):
        return {
            "ok": False,
            "error": f"stack_mismatch: fix_suggestion/diff/stack 中找不到 {platform} 平台的文件路径",
        }

    # 分支名以 repo_logical 区分，多仓库时不会撞名
    branch = _safe_branch_name(ana.datadog_issue_id, repo_logical or platform)
    frontend_url = f"{s.frontend_base_url.rstrip('/')}/crashguard?issue={ana.datadog_issue_id}"

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "branch_name": branch,
            "pr_title_preview": f"[Crashguard][{platform}] {(issue.title or 'crash fix')[:80]}",
            "has_fix_diff": bool((ana.fix_diff or "").strip()),
            "repo_path": repo_path,
        }

    # === 真实操作：分支 → (尝试 git apply) → commit → push → gh pr create --draft ===
    # Per-repo 锁：防止 daily auto + UI 手动并发触发同一 repo，撞 git index
    repo_lock = await _acquire_repo_lock(repo_path)
    async with repo_lock:
      # 记录进入前所在分支，finally 用来恢复
      rc_init, init_out, _ = _run_git(
          ["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_path, timeout=10,
      )
      initial_branch = (init_out or "").strip() if rc_init == 0 else ""
      if initial_branch in ("HEAD", ""):
          initial_branch = "main"  # detached 兜底
      base_ref = ""
      pushed_to_remote = False
      affected_submodules: list[str] = []

      # === 进入前自愈：上次流程可能被 SIGKILL 杀掉（OS OOM / 长 AI 超时），
      # finally cleanup 来不及跑，留下 crashguard/* 临时分支 + 脏 prompt.md ===
      # 检测：当前在 crashguard/* 分支 OR 有 prompt.md 残留 → 自动清理回 main
      try:
          if initial_branch.startswith("crashguard/"):
              logger.warning(
                  "pre-enter heal: repo %s left on stale branch %s (previous process killed); auto-reset",
                  repo_path, initial_branch,
              )
              _run_git(["git", "checkout", "--", "."], repo_path, timeout=15)
              _run_git(["git", "clean", "-fd", "--", ".crashguard"], repo_path, timeout=10)
              # 删除 implementation agent 留下的 prompt.md（沟槽：根目录残留）
              _run_git(["git", "clean", "-fd", "--", "prompt.md"], repo_path, timeout=10)
              from pathlib import Path as _P
              (_P(repo_path) / "prompt.md").unlink(missing_ok=True)
              # 切回 main，删脏分支
              _run_git(["git", "checkout", "main"], repo_path, timeout=30)
              _run_git(["git", "branch", "-D", initial_branch], repo_path, timeout=10)
              initial_branch = "main"
          else:
              # 仅清残留 prompt.md（即使在 main 上也可能有）
              from pathlib import Path as _P
              (_P(repo_path) / "prompt.md").unlink(missing_ok=True)
      except Exception:
          logger.exception("pre-enter heal failed (non-fatal, continuing)")

      try:
        dirty, dirty_detail = _worktree_dirty(repo_path)
        if dirty:
          return {
              "ok": False,
              "error": "repo worktree is dirty; refuse to auto-create PR",
              "repo": repo_logical or platform,
              "detail": dirty_detail[:500],
          }

        # Bug #3 fix：fetch 提到 180s（首次 fetch 慢）
        # remote 名自适应：102 Plaud 仓库 remote 叫 'merge' 不叫 'origin'
        main_remote = _resolve_remote_name(repo_path)
        rc, _, err = _run_git(["git", "fetch", main_remote], repo_path, timeout=180)
        if rc != 0:
          return {"ok": False, "error": f"git fetch failed: {err}"}
        base_ref = _default_base_ref(repo_path)

        # Bug #2 fix：先把目标分支（本地）清掉，避免「already exists」
        rc, _, err = _run_git(["git", "checkout", "--detach", base_ref], repo_path, timeout=30)
        if rc != 0:
            return {"ok": False, "error": f"git checkout {base_ref} failed: {err}"}
        _run_git(["git", "branch", "-D", branch], repo_path, timeout=10)

        rc, _, err = _run_git(["git", "checkout", "-b", branch, base_ref], repo_path, timeout=30)
        if rc != 0:
            return {"ok": False, "error": f"git checkout {base_ref} failed: {err}"}

        # === 三档优先级：实施 agent（最优）→ git apply ana.fix_diff（旧路径）→ md 兜底 ===
        sub_repo_dirname = os.path.basename(repo_path.rstrip("/"))
        patch_applied = False
        changed_files: list[str] = []
        impl_source = ""  # "agent" / "diff" / "" (md fallback)
        last_failure_reason = ""  # 用于 audit / md fallback 日志

        # 优先 1：实施 agent 直接在真 repo 里 Edit 文件
        try:
            impl_ok, impl_files, impl_err = await _run_implementation_agent(
                repo_path, ana, issue,
            )
        except Exception as exc:
            impl_ok, impl_files, impl_err = False, [], f"impl agent crash: {exc}"
        if impl_ok:
            patch_applied = True
            impl_source = "agent"
            changed_files = impl_files
            logger.info(
                "implementation agent changed %d file(s) in %s on %s: %s",
                len(impl_files), sub_repo_dirname, branch, impl_files[:5],
            )
        else:
            last_failure_reason = impl_err
            logger.info("implementation agent skipped/failed: %s", impl_err)
            # 实施 agent 可能改了部分文件却没产合规 diff——清理工作树回到 origin/main 干净态
            _run_git(["git", "checkout", "."], repo_path, timeout=15)
            _run_git(["git", "clean", "-fd", "--", ":(exclude).crashguard"], repo_path, timeout=15)

            # 优先 2：旧路径——尝试 git apply ana.fix_diff（向后兼容）
            if (ana.fix_diff or "").strip():
                applied2, apply_err2 = _try_apply_fix_diff(
                    repo_path, ana.fix_diff or "", sub_repo_dirname,
                )
                if applied2:
                    patch_applied = True
                    impl_source = "diff"
                    logger.info("fix_diff text applied to %s on %s", sub_repo_dirname, branch)
                else:
                    last_failure_reason = apply_err2
                    logger.warning("fix_diff apply failed: %s", apply_err2)

        # ⚠️ 用户硬性要求：PR 必须是真代码改动，不接受 md 修复方案文档兜底。
        # agent / diff 都失败 = 直接判失败，不建 doc-only PR，进失败审计供前端展示
        if not patch_applied:
            err_msg = f"no_real_patch: {last_failure_reason or 'no source change produced'}"
            logger.warning(
                "crashguard PR creation aborted: %s (repo=%s ana=%d)",
                err_msg, repo_logical or platform, analysis_id,
            )
            try:
                from app.crashguard.services.audit import write_audit
                await write_audit(
                    op="pr_draft",
                    target_id=str(analysis_id),
                    success=False,
                    error=err_msg,
                    detail={
                        "repo": repo_logical or platform,
                        "reason": "patch_failed",
                        "impl_failure": last_failure_reason,
                    },
                )
            except Exception:
                pass
            return {
                "ok": False,
                "error": err_msg,
                "repo": repo_logical or platform,
                "patch_applied": False,
            }

        # === fix_diff 路径下 changed_files 还没填，从 git status 补抽 ===
        if impl_source == "diff" and not changed_files:
            rc_st, out_st, _ = _run_git(
                ["git", "status", "--porcelain"], repo_path, timeout=10,
            )
            if rc_st == 0:
                for ln in out_st.splitlines():
                    if len(ln) < 4:
                        continue
                    pth = ln[3:].strip().strip('"')
                    if not pth:
                        continue
                    if pth.startswith(".crashguard/") or pth == "prompt.md":
                        continue
                    # 系统垃圾过滤：macOS .DS_Store / 编辑器临时文件不进 PR
                    base = os.path.basename(pth)
                    if base == ".DS_Store" or base.endswith(".swp") or base.endswith("~"):
                        continue
                    changed_files.append(pth)

        # === Submodule 分桶：把 submodule 内的改动路由到 submodule 自己的 repo 开 PR ===
        submodules_meta = _parse_gitmodules(repo_path)
        classified = _classify_changed_files(repo_path, changed_files, submodules_meta)
        parent_files = classified["parent"]
        sub_buckets = classified["submodules"]

        # Defense A：submodule 有 edit 但未 init → 立即失败，拒绝把 submodule 源码 commit 进父 repo
        for sm_path, info in sub_buckets.items():
            if info["files"] and not info["initialized"]:
                err_msg = (
                    f"submodule_not_initialized: '{sm_path}' has "
                    f"{len(info['files'])} edited files but submodule is not "
                    f"initialized. Detail: {info['init_detail']}. Refusing to commit "
                    f"submodule source into parent repo."
                )
                logger.warning(
                    "PR aborted: %s (repo=%s ana=%d)",
                    err_msg, repo_logical or platform, analysis_id,
                )
                try:
                    from app.crashguard.services.audit import write_audit
                    await write_audit(
                        op="pr_draft",
                        target_id=str(analysis_id),
                        success=False,
                        error=err_msg,
                        detail={
                            "repo": repo_logical or platform,
                            "submodule": sm_path,
                            "submodule_files_sample": info["files"][:10],
                            "init_detail": info["init_detail"],
                        },
                    )
                except Exception:
                    pass
                return {
                    "ok": False, "error": err_msg,
                    "repo": repo_logical or platform,
                    "submodule_path": sm_path,
                }

        if not parent_files and not any(info["files"] for info in sub_buckets.values()):
            return {
                "ok": False,
                "error": "no_real_patch: no files routed to parent or any submodule",
                "repo": repo_logical or platform,
            }

        pr_title = f"[Crashguard][{platform}] {(issue.title or 'crash fix')[:80]}"
        pr_body = _build_pr_body(issue, ana, frontend_url, patch_applied=patch_applied)
        all_pr_results: list[dict] = []
        # finally 块用：清理这些 submodule worktree
        affected_submodules.clear()

        # === 父 repo PR（如果有 parent_files）===
        if parent_files:
            parent_files_arg = parent_files if impl_source == "agent" else None
            parent_result, parent_pushed = await _create_one_draft_pr(
                cwd=repo_path,
                branch=branch,
                files_to_add=parent_files_arg,
                commit_message=_build_commit_msg(issue, ana, impl_source, parent_files),
                pr_title=pr_title,
                pr_body=pr_body,
                analysis_id=analysis_id,
                repo_logical=repo_logical or platform,
                approver=approver,
                change_kind="parent",
                prep_branch=False,
            )
            if parent_pushed:
                pushed_to_remote = True
            all_pr_results.append(parent_result)
            if not parent_result.get("ok"):
                # 父 PR 失败直接返回——不再尝试 submodule，避免父代码改没推上去却 submodule 已推
                return parent_result

        # === Submodule PR：每个 submodule 一个独立 PR 到 submodule 自己的 GitHub repo ===
        for sm_path, info in sub_buckets.items():
            if not info["files"]:
                continue
            sm_abs = info["abs_path"]
            sm_logical = (
                f"{repo_logical or platform}-{os.path.basename(sm_path.rstrip('/'))}"
            )
            sm_branch = _safe_branch_name(ana.datadog_issue_id, sm_logical)

            # Submodule 级别独立 dedup
            since_sm = datetime.utcnow() - timedelta(days=s.pr_dedup_days)
            async with get_session() as session:
                existing_sm = (await session.execute(
                    select(CrashPullRequest).where(
                        CrashPullRequest.datadog_issue_id == ana.datadog_issue_id,
                        CrashPullRequest.repo == sm_logical,
                        CrashPullRequest.created_at >= since_sm,
                    )
                )).scalars().first()
            if existing_sm is not None:
                logger.info(
                    "submodule PR dedup hit %s: existing=%s",
                    sm_logical, existing_sm.pr_url,
                )
                all_pr_results.append({
                    "ok": False, "error": f"dup_within_{s.pr_dedup_days}d",
                    "repo": sm_logical, "submodule_path": sm_path,
                    "existing_pr_url": existing_sm.pr_url,
                    "existing_branch": existing_sm.branch_name,
                })
                continue

            sm_lock = await _acquire_repo_lock(sm_abs)
            async with sm_lock:
                affected_submodules.append(sm_abs)
                sm_result, _ = await _create_one_draft_pr(
                    cwd=sm_abs,
                    branch=sm_branch,
                    files_to_add=info["files"],
                    commit_message=_build_commit_msg(
                        issue, ana, impl_source, info["files"],
                    ),
                    pr_title=f"[Crashguard][{sm_logical}] {(issue.title or 'crash fix')[:80]}",
                    pr_body=pr_body,
                    analysis_id=analysis_id,
                    repo_logical=sm_logical,
                    approver=approver,
                    change_kind="submodule",
                    prep_branch=True,
                )
                # 给 result 加 submodule_path 字段方便前端/审计
                sm_result.setdefault("submodule_path", sm_path)
                all_pr_results.append(sm_result)

        if not all_pr_results:
            return {
                "ok": False,
                "error": "no PR built after classification",
                "repo": repo_logical or platform,
            }

        primary = all_pr_results[0]
        extras = all_pr_results[1:]

        # 审计
        try:
            from app.crashguard.services.audit import write_audit
            succeeded_n = sum(1 for r in all_pr_results if r.get("ok"))
            await write_audit(
                op="pr_draft",
                target_id=str(analysis_id),
                success=primary.get("ok", False),
                detail={
                    "primary_pr_url": primary.get("pr_url"),
                    "primary_repo": primary.get("repo"),
                    "extras_count": len(extras),
                    "extras_repos": [r.get("repo") for r in extras],
                    "succeeded_total": succeeded_n,
                    "failed_total": len(all_pr_results) - succeeded_n,
                    "approver": approver,
                    "impl_source": impl_source,
                    "parent_files": parent_files,
                    "submodule_buckets": {
                        k: {"files": v["files"], "url": v.get("url", "")}
                        for k, v in sub_buckets.items() if v["files"]
                    },
                },
            )
        except Exception:
            pass

        out = dict(primary)
        out["impl_source"] = impl_source
        out["patch_applied"] = patch_applied
        out["changed_files"] = changed_files
        if extras:
            out["extra_prs"] = extras
        return out
      finally:
        # 自愈：父 repo + 所有受影响 submodule 全部拉回 base_ref + 删除未推送的临时分支
        _cleanup_repo_after_pr(
            repo_path=repo_path,
            base_ref=base_ref or "origin/main",
            initial_branch=initial_branch,
            branch_to_delete=branch,
            pushed_to_remote=pushed_to_remote,
        )
        for sm_abs in affected_submodules:
            # branch 名根据 sm_logical 派生，cleanup 时按目录 reset 即可（branch 名我们没追踪
            # pushed_to_remote 区分；reset 不删 branch，留给下次重跑覆盖）
            _post_pr_cleanup_submodule(sm_abs, "", pushed=True)


async def draft_prs_multi(
    analysis_id: int,
    approver: str = "human",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """跨仓库 PR 入口：检测 fix_suggestion 涉及的所有候选仓库，循环建多 PR。

    底层逻辑：Plaud 是 Flutter 双端 + 原生包装——一个崩溃可能跨多仓库
    （如 Android ANR 修复同时改 dart 层和原生 Activity）。本函数：
    1. 读 analysis + issue
    2. 用 _resolve_candidate_repos 探测涉及的所有仓库
    3. 对每个候选仓库串行调 draft_pr_for_analysis(repo_override=...)
    4. 返回所有 PR 结果——前端 detail.pull_requests 数组天然支持多 PR 展示

    返回：{ok, prs: [...], total: N, succeeded: N, failed: N}
    """
    async with get_session() as session:
        ana = (await session.execute(
            select(CrashAnalysis).where(CrashAnalysis.id == analysis_id)
        )).scalar_one_or_none()
        if ana is None:
            return {"ok": False, "error": f"analysis {analysis_id} not found", "prs": []}
        issue = (await session.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == ana.datadog_issue_id)
        )).scalar_one_or_none()
        if issue is None:
            return {"ok": False, "error": "issue not found", "prs": []}

    fix_text = "\n".join([ana.fix_suggestion or "", ana.solution or "", ana.fix_diff or ""])
    candidates = _resolve_candidate_repos(
        issue.platform or "", fix_text, issue.representative_stack or "",
    )
    def _flatten(primary: Dict[str, Any], repo_name: str) -> list[Dict[str, Any]]:
        """把 draft_pr_for_analysis 的单结果（可能含 extra_prs）摊平成 list。"""
        primary.setdefault("repo", repo_name)
        extras = primary.pop("extra_prs", None) or []
        return [primary] + list(extras)

    if not candidates:
        # 退回单仓库默认逻辑
        single = await draft_pr_for_analysis(analysis_id, approver=approver, dry_run=dry_run)
        flat = _flatten(single, "")
        succeeded = sum(1 for r in flat if r.get("ok"))
        return {
            "ok": succeeded > 0,
            "prs": flat,
            "total": len(flat),
            "succeeded": succeeded,
            "failed": len(flat) - succeeded,
        }

    results: list[Dict[str, Any]] = []
    for repo_name, repo_path in candidates:
        try:
            r = await draft_pr_for_analysis(
                analysis_id, approver=approver, dry_run=dry_run,
                repo_override=(repo_name, repo_path),
            )
        except Exception as exc:
            r = {"ok": False, "error": f"exception: {exc}", "repo": repo_name}
        for sub_r in _flatten(r, repo_name):
            results.append(sub_r)
            logger.info(
                "draft_prs_multi: repo=%s ok=%s pr=%s",
                sub_r.get("repo"), sub_r.get("ok"),
                sub_r.get("pr_url", sub_r.get("error", "")),
            )

    succeeded = sum(1 for r in results if r.get("ok"))
    return {
        "ok": succeeded > 0,
        "prs": results,
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
    }
