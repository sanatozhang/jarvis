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
    return True, changed, ""


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
      # Bug #3 fix：fetch 提到 180s（首次 fetch 慢）
      rc, _, err = _run_git(["git", "fetch", "origin"], repo_path, timeout=180)
      if rc != 0:
        return {"ok": False, "error": f"git fetch failed: {err}"}

      # Bug #2 fix：先把目标分支（本地 + 远程）清掉，避免「already exists」
      # 失败不致命——分支不存在 git branch -D 就报 not found，忽略即可。
      _run_git(["git", "checkout", "main"], repo_path, timeout=15)
      _run_git(["git", "branch", "-D", branch], repo_path, timeout=10)

      rc, _, err = _run_git(["git", "checkout", "-b", branch, "origin/main"], repo_path, timeout=30)
      if rc != 0:
          return {"ok": False, "error": f"git checkout origin/main failed: {err}"}

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

      if patch_applied:
          # 精准 add：实施 agent 路径下用 changed_files；fix_diff 路径下用 -A（diff 已在 apply）
          if impl_source == "agent" and changed_files:
              rc, _, err = _run_git(
                  ["git", "add", "--"] + changed_files, repo_path,
              )
          else:
              rc, _, err = _run_git(["git", "add", "-A"], repo_path)
          if rc != 0:
              return {"ok": False, "error": f"git add (post-apply) failed: {err}"}
          via = "implementation agent" if impl_source == "agent" else "fix_diff text"
          file_summary = ", ".join(changed_files[:5]) if changed_files else "see diff"
          commit_message = (
              f"fix(crashguard): {(issue.title or ana.datadog_issue_id)[:60]}\n\n"
              f"AI-generated patch via {via} for crash issue {ana.datadog_issue_id}.\n"
              f"Files: {file_summary}\n"
              f"Confidence: {ana.confidence or 'low'} · Feasibility: {ana.feasibility_score:.2f}\n"
              f"Reviewer must verify diff correctness before merge."
          )
          pr_title = f"[Crashguard][{platform}] {(issue.title or 'crash fix')[:80]}"
      else:
          if last_failure_reason:
              logger.warning(
                  "crashguard implementation failed, falling back to md doc: %s",
                  last_failure_reason,
              )
          # 回退：写 .crashguard/fixes/<id>.md（保留旧行为）
          fix_doc_relpath = f".crashguard/fixes/{ana.datadog_issue_id}.md"
          fix_doc_content = _build_pr_body(issue, ana, frontend_url, patch_applied=False) + "\n"
          doc_abs = Path(repo_path) / fix_doc_relpath
          doc_abs.parent.mkdir(parents=True, exist_ok=True)
          doc_abs.write_text(fix_doc_content, encoding="utf-8")
          rc, _, err = _run_git(["git", "add", "-f", fix_doc_relpath], repo_path)
          if rc != 0:
              return {"ok": False, "error": f"git add (md fallback) failed: {err}"}
          commit_message = f"docs(crashguard): draft fix for {ana.datadog_issue_id}"
          pr_title = (
              f"[Crashguard][{platform}][needs manual patch] "
              f"{(issue.title or 'crash fix')[:70]}"
          )

      pr_body = _build_pr_body(issue, ana, frontend_url, patch_applied=patch_applied)

      rc, _, err = _run_git(
          ["git", "commit", "-m", commit_message, "--no-verify"], repo_path, timeout=30,
      )
      if rc != 0:
          return {"ok": False, "error": f"git commit failed: {err}"}
      rc, _, err = _run_git(
          ["git", "push", "-u", "origin", branch], repo_path, timeout=120,
      )
      if rc != 0:
          return {"ok": False, "error": f"git push failed: {err}"}

      # gh pr create --draft（硬编码 --draft）
      rc, stdout, err = _run_git(
          [
              "gh", "pr", "create",
              "--draft",
              "--title", pr_title,
              "--body", pr_body,
              "--head", branch,
          ],
          repo_path,
          timeout=120,
      )
      if rc != 0:
          return {"ok": False, "error": f"gh pr create failed: {err}", "branch_name": branch}
      pr_url = stdout.strip().splitlines()[-1] if stdout else ""
      pr_number = None
      m = re.search(r"/pull/(\d+)", pr_url)
      if m:
          pr_number = int(m.group(1))

      async with get_session() as session:
          row = CrashPullRequest(
              analysis_id=analysis_id,
              datadog_issue_id=ana.datadog_issue_id,
              repo=repo_logical or platform,
              branch_name=branch,
              pr_url=pr_url,
              pr_number=pr_number,
              pr_status="draft",
              triggered_by="human_approved",
              approved_by=approver or "human",
              approved_at=datetime.utcnow(),
              created_at=datetime.utcnow(),
          )
          session.add(row)
          await session.commit()

      logger.info("crashguard draft PR created: %s (analysis=%d)", pr_url, analysis_id)
      try:
          from app.crashguard.services.audit import write_audit
          await write_audit(
              op="pr_draft",
              target_id=str(analysis_id),
              success=True,
              detail={
                  "pr_url": pr_url,
                  "branch": branch,
                  "approver": approver,
                  "repo": repo_logical or platform,
                  "patch_applied": patch_applied,
                  "impl_source": impl_source,
                  "changed_files": changed_files,
                  "fallback_reason": last_failure_reason if not patch_applied else "",
              },
          )
      except Exception:
          pass
      return {
          "ok": True,
          "pr_url": pr_url,
          "pr_number": pr_number,
          "branch_name": branch,
          "repo": repo_logical or platform,
          "patch_applied": patch_applied,
          "impl_source": impl_source,
          "changed_files": changed_files,
      }


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
    if not candidates:
        # 退回单仓库默认逻辑
        single = await draft_pr_for_analysis(analysis_id, approver=approver, dry_run=dry_run)
        return {
            "ok": single.get("ok", False),
            "prs": [single],
            "total": 1,
            "succeeded": 1 if single.get("ok") else 0,
            "failed": 0 if single.get("ok") else 1,
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
        r.setdefault("repo", repo_name)
        results.append(r)
        logger.info(
            "draft_prs_multi: repo=%s ok=%s pr=%s",
            repo_name, r.get("ok"), r.get("pr_url", r.get("error", "")),
        )

    succeeded = sum(1 for r in results if r.get("ok"))
    return {
        "ok": succeeded > 0,
        "prs": results,
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
    }
