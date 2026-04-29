"""
半自动 PR 草稿生成器。

闭环：人工 ✋ approve → 调本服务 → git checkout 新分支 →
     首选：git apply AI 产出的 fix_diff（真代码改动）
     回退：apply 失败时写 .crashguard/fixes/<id>.md（修复说明文档）
     → commit → push → gh pr create --draft → 写回 crash_pull_requests

🚫 严禁调用：gh pr merge / git merge / gh pr ready —— 永远只创建 draft。
"""
from __future__ import annotations

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


# 仅当命令是 git 时才需检查这些子命令；只匹配 args[1] 的精确子命令名，
# 不再扫整个 cmd 数组（避免 PR body / commit message 中的自然词触发误判）。
_FORBIDDEN_GIT_SUBCOMMANDS = {"merge", "rebase"}
_FORBIDDEN_GIT_FLAGS = {"--merge", "--rebase", "--squash"}
# gh 子命令：永远禁止 merge / ready（draft → ready 不允许）
_FORBIDDEN_GH_SUBCOMMANDS = {"merge", "ready"}


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
    """从 fix_suggestion 文本里 grep 文件扩展名/路径，验证至少有一个匹配该平台的文件。

    防止误把 fix 完全没碰该平台代码的分析拿去 PR。
    返回 False 时调用方应拒绝建 PR。
    """
    if not fix_text:
        # 空 fix 谈不上匹配，但也不阻断（pr_drafter 上游已检查过 fix_suggestion 非空）
        return True
    text = fix_text.lower()
    p = (platform or "").strip().lower()
    if p == "android":
        return any(kw in text for kw in (".kt", ".java", ".gradle", "androidmanifest", "app/src/main"))
    if p == "ios":
        return any(kw in text for kw in (".swift", ".m\n", ".mm", ".plist", "appdelegate", "podfile", "runner/"))
    if p == "flutter":
        return any(kw in text for kw in (".dart", "pubspec", "lib/"))
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
) -> Dict[str, Any]:
    """
    入口：根据 analysis_id 生成 draft PR。

    返回：{ok, pr_url?, branch_name?, dry_run?, error?}
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

        # 30 天去重
        since = datetime.utcnow() - timedelta(days=s.pr_dedup_days)
        existing = (await session.execute(
            select(CrashPullRequest).where(
                CrashPullRequest.datadog_issue_id == ana.datadog_issue_id,
                CrashPullRequest.repo == (issue.platform or "").lower(),
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
    repo_path = _platform_repo_path(platform)
    if not repo_path or not Path(repo_path).exists():
        return {"ok": False, "error": f"repo_path not configured/found for platform={platform}"}

    # Stack 验证：fix_suggestion 必须含至少一个该平台的文件标识，否则拒发
    fix_text = (ana.fix_suggestion or "") + "\n" + (ana.solution or "")
    if not _stack_matches_platform(platform, fix_text):
        return {
            "ok": False,
            "error": f"stack_mismatch: fix_suggestion 中找不到 {platform} 平台的文件路径",
        }

    branch = _safe_branch_name(ana.datadog_issue_id, platform)
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

    # === 尝试 git apply 真实 patch；失败回退到 md 文档 ===
    sub_repo_dirname = os.path.basename(repo_path.rstrip("/"))
    patch_applied, apply_err = _try_apply_fix_diff(
        repo_path, ana.fix_diff or "", sub_repo_dirname,
    )
    if patch_applied:
        logger.info("crashguard fix_diff applied to %s on %s", sub_repo_dirname, branch)
        rc, _, err = _run_git(["git", "add", "-A"], repo_path)
        if rc != 0:
            return {"ok": False, "error": f"git add (post-apply) failed: {err}"}
        commit_message = (
            f"fix(crashguard): {(issue.title or ana.datadog_issue_id)[:60]}\n\n"
            f"AI-generated patch for crash issue {ana.datadog_issue_id}.\n"
            f"Confidence: {ana.confidence or 'low'} · Feasibility: {ana.feasibility_score:.2f}\n"
            f"Reviewer must verify diff correctness before merge."
        )
        pr_title = f"[Crashguard][{platform}] {(issue.title or 'crash fix')[:80]}"
    else:
        if (ana.fix_diff or "").strip():
            logger.warning(
                "crashguard fix_diff apply failed (falling back to md doc): %s", apply_err,
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
            repo=platform,
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
                "patch_applied": patch_applied,
                "apply_error": apply_err if not patch_applied else "",
            },
        )
    except Exception:
        pass
    return {
        "ok": True,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "branch_name": branch,
        "patch_applied": patch_applied,
    }
