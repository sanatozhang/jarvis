"""PR Review 自动响应——检测 + 调度层（Stage B + C）。

底层逻辑：crashguard 自动 PR 提交后，Copilot / Codex / Claude 等 reviewer bot 会
给评论，部分是真 bug、部分是"风格建议/虚警"。让 LLM 二次自反思评判：
- 真存在 → 修复 + commit + 回 PR 评论"已在 <commit> 修复"
- 不存在 → 回 PR 评论解释"原代码意图如此 / 此场景不适用 / ..."

Stage B 只负责：
- `fetch_pr_reviews(slug, num)` 拉真实 review GraphQL ID + 全文
- `collect_actionable_reviews(pr, reviews, session)` 过滤已响应 / cooldown / max_iter / 短评论 / 非白名单 author

Stage C 负责真实派 agent + Gate 复用 + commit + 评论；Stage D 在 pr_sync tick 内接线。
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, desc, select

from app.crashguard.config import get_crashguard_settings
from app.crashguard.models import CrashPrReviewIteration, CrashPullRequest

logger = logging.getLogger("crashguard.pr_review_responder")


# === GraphQL（拿真实 review id；gh pr view --json latestReviews 的 id 是空串） ===
_GH_REVIEW_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviews(last: 20) {
        nodes {
          id
          state
          submittedAt
          bodyText
          author { login }
        }
      }
    }
  }
}
""".strip()


@dataclass
class ReviewItem:
    review_id: str            # PRR_xxx (PR-level review) / REST_C_<id> (行级 review comment)
    author: str               # login 名（小写化对比时）
    state: str                # COMMENTED / CHANGES_REQUESTED / APPROVED / DISMISSED / PENDING
    submitted_at: datetime    # UTC naive
    body: str                 # 完整 review body 文本
    # 行级 review comment（REST id）— 有值时 reply 挂到该 thread 下；
    # PR-level review 没有可 reply 的目标，仍用 fallback 顶层 issue comment。
    source_comment_id: Optional[int] = None
    # 行级 comment 才有：用于 prompt 上下文显示
    path: str = ""             # 改动文件路径（如 lib/foo.dart）
    line: Optional[int] = None # 行号（None 代表 PR-level review）


def _parse_iso(s: str) -> Optional[datetime]:
    """GitHub ISO 时间 → naive UTC datetime。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(tz=None).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def fetch_pr_review_comments(
    repo_slug: str, pr_number: int, timeout: int = 30
) -> tuple[bool, List[ReviewItem], str]:
    """拉 PR 的**行级** review comment（绑代码行的，可 reply 到 thread 下面）。

    底层逻辑：crashguard 之前只拉 PR-level review，只能写顶层 issue comment；
    reviewer 完全感觉不到回应挂在哪。改用行级 comment 后，每条 reply 都能
    通过 in_reply_to 形成 GH thread——reviewer 在自己评论下看到具体回应。

    返回 (ok, items, error)。每项 source_comment_id 是 REST id，可用于 reply。
    """
    if "/" not in repo_slug:
        return False, [], f"invalid repo_slug: {repo_slug}"
    import os as _os
    sub_env = dict(_os.environ)
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        sub_env.pop(k, None)
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{repo_slug}/pulls/{pr_number}/comments",
             "--paginate"],
            capture_output=True, text=True, timeout=timeout, env=sub_env,
        )
        if r.returncode != 0:
            return False, [], (r.stderr or "").strip()[:300]
        try:
            data = json.loads(r.stdout or "[]")
        except json.JSONDecodeError as e:
            return False, [], f"json decode failed: {e}"
        if not isinstance(data, list):
            return False, [], f"unexpected payload type: {type(data).__name__}"
        out: List[ReviewItem] = []
        for c in data:
            cid = c.get("id")
            if not cid:
                continue
            # 跳过已是 reply 的（in_reply_to_id != null）— 只对原 comment 做 reply
            if c.get("in_reply_to_id"):
                continue
            sat = _parse_iso(c.get("created_at") or "")
            out.append(ReviewItem(
                review_id=f"REST_C_{cid}",
                author=((c.get("user") or {}).get("login") or "").strip(),
                state="COMMENTED",  # 行级 comment 默认 COMMENTED
                submitted_at=sat or datetime.utcnow(),
                body=(c.get("body") or ""),
                source_comment_id=int(cid),
                path=(c.get("path") or ""),
                line=c.get("line") or c.get("original_line"),
            ))
        return True, out, ""
    except subprocess.TimeoutExpired:
        return False, [], f"gh api timeout after {timeout}s"
    except FileNotFoundError:
        return False, [], "gh CLI not installed"
    except Exception as e:
        return False, [], f"gh api error: {e}"


def fetch_pr_reviews(
    repo_slug: str, pr_number: int, timeout: int = 30
) -> tuple[bool, List[ReviewItem], str]:
    """走 `gh api graphql` 拉 PR 的 reviews 列表（含真实 GraphQL id）。

    返回 (ok, reviews, error_str)。
    剥 GH_TOKEN/GITHUB_TOKEN 让 gh 走 OAuth（和 pr_drafter / pr_sync 同款）。

    注意：拉的是 PR-level review（整条 review 容器），如需 reply 到具体代码行
    的 thread，用 fetch_pr_review_comments。两者 actionable 单元都是 ReviewItem。
    """
    if "/" not in repo_slug:
        return False, [], f"invalid repo_slug: {repo_slug}"
    owner, repo = repo_slug.split("/", 1)
    import os as _os
    sub_env = dict(_os.environ)
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        sub_env.pop(k, None)
    try:
        r = subprocess.run(
            [
                "gh", "api", "graphql",
                "-F", f"owner={owner}",
                "-F", f"repo={repo}",
                "-F", f"number={pr_number}",
                "-f", f"query={_GH_REVIEW_QUERY}",
            ],
            capture_output=True, text=True, timeout=timeout, env=sub_env,
        )
        if r.returncode != 0:
            return False, [], (r.stderr or "").strip()[:300]
        try:
            data = json.loads(r.stdout or "{}")
        except json.JSONDecodeError as e:
            return False, [], f"json decode failed: {e}"
        nodes = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviews", {})
            .get("nodes", [])
        ) or []
        out: List[ReviewItem] = []
        for n in nodes:
            rid = n.get("id") or ""
            if not rid:
                continue
            sat = _parse_iso(n.get("submittedAt") or "")
            out.append(ReviewItem(
                review_id=rid,
                author=((n.get("author") or {}).get("login") or "").strip(),
                state=(n.get("state") or "").upper(),
                submitted_at=sat or datetime.utcnow(),
                body=(n.get("bodyText") or ""),
            ))
        return True, out, ""
    except subprocess.TimeoutExpired:
        return False, [], f"gh graphql timeout after {timeout}s"
    except FileNotFoundError:
        return False, [], "gh CLI not installed"
    except Exception as e:
        return False, [], f"gh graphql error: {e}"


@dataclass
class ActionableReview:
    """通过过滤的 review，可以派给 agent 评判 + 修复 / 解释。"""
    pr_id: int                # DB 内部 ID (crash_pull_requests.id)
    pr_url: str
    repo_slug: str
    pr_number: int
    review: ReviewItem
    iter_count: int           # 这条 PR 已经响应过几轮（含本轮 = N+1）


async def collect_actionable_reviews(
    pr: CrashPullRequest, reviews: List[ReviewItem], session,
) -> tuple[List[ActionableReview], Dict[str, int]]:
    """从 PR 的 reviews 列表中过滤出"应该派 agent 处理"的子集。

    过滤链（按序）：
    1. enabled kill switch
    2. author 在白名单内
    3. body 长度 ≥ min_body_chars（"LGTM" 等噪声跳过）
    4. 不是已经在 DB 里有记录的 review_id（UNIQUE 去重）
    5. cooldown：同 PR 最近 N min 内已派过 → 跳过
    6. iter_count < max_iterations

    返回 (actionable_list, counters)，counters 用于 audit / heartbeat summary。
    """
    s = get_crashguard_settings()
    counters = {
        "fetched": len(reviews),
        "kill_switch": 0,
        "non_whitelist_author": 0,
        "body_too_short": 0,
        "already_processed": 0,
        "cooldown": 0,
        "max_iter": 0,
        "actionable": 0,
    }

    if not getattr(s, "pr_review_response_enabled", False):
        counters["kill_switch"] = len(reviews)
        return [], counters

    allowed_authors = {
        (a or "").strip().lower()
        for a in (getattr(s, "pr_review_response_allowed_authors", []) or [])
    }
    min_body = int(getattr(s, "pr_review_response_min_body_chars", 50) or 50)
    max_iter = int(getattr(s, "pr_review_response_max_iterations", 3) or 3)
    cooldown = timedelta(
        minutes=int(getattr(s, "pr_review_response_cooldown_minutes", 30) or 30)
    )

    # 该 PR 的所有历史 iteration（一次查）
    history = (await session.execute(
        select(CrashPrReviewIteration)
        .where(CrashPrReviewIteration.pr_id == pr.id)
        .order_by(desc(CrashPrReviewIteration.dispatched_at))
    )).scalars().all()
    processed_review_ids = {h.review_id for h in history if h.review_id}
    last_dispatched_at: Optional[datetime] = (
        history[0].dispatched_at if history else None
    )
    iter_count_so_far = len(history)
    in_cooldown = (
        last_dispatched_at is not None
        and (datetime.utcnow() - last_dispatched_at) < cooldown
    )

    # 构建 repo_slug / pr_number（从 pr_url 解出来）
    repo_slug, pr_number = _parse_pr_url(pr.pr_url or "")
    if not repo_slug or pr_number <= 0:
        counters["actionable"] = 0
        return [], counters

    out: List[ActionableReview] = []
    for r in reviews:
        author_l = (r.author or "").lower()
        if allowed_authors and author_l not in allowed_authors:
            counters["non_whitelist_author"] += 1
            continue
        if len(r.body or "") < min_body:
            counters["body_too_short"] += 1
            continue
        if r.review_id in processed_review_ids:
            counters["already_processed"] += 1
            continue
        # iter_count 在加完已有历史之后再判断
        if iter_count_so_far >= max_iter:
            counters["max_iter"] += 1
            continue
        if in_cooldown:
            counters["cooldown"] += 1
            continue
        out.append(ActionableReview(
            pr_id=int(pr.id),
            pr_url=pr.pr_url or "",
            repo_slug=repo_slug,
            pr_number=pr_number,
            review=r,
            iter_count=iter_count_so_far + 1,
        ))
        # 一次只取一条，避免单 tick 内对同 PR 派多次（顺序处理更稳）
        break

    counters["actionable"] = len(out)
    return out, counters


def _parse_pr_url(pr_url: str) -> tuple[str, int]:
    """https://github.com/owner/repo/pull/123 → (owner/repo, 123)。

    解析失败返回 ("", 0)。
    """
    if not pr_url:
        return "", 0
    try:
        # 简单拆，避免引入 urlparse 依赖
        parts = pr_url.rstrip("/").split("/")
        if len(parts) < 5 or "github.com" not in pr_url:
            return "", 0
        pr_idx = parts.index("pull")
        owner = parts[pr_idx - 2]
        repo = parts[pr_idx - 1]
        num = int(parts[pr_idx + 1])
        return f"{owner}/{repo}", num
    except (ValueError, IndexError):
        return "", 0


# ============================================================
# Stage C: Dispatcher（派 agent + Gate 复用 + commit + 评论 PR）
# ============================================================
import asyncio
import os
from pathlib import Path

REVIEW_RESPONSE_FILE = ".crashguard/review_response.json"


def _build_review_prompt(
    actionable: ActionableReview,
    pr_diff_text: str,
    issue_title: str = "",
    datadog_issue_id: str = "",
    max_iter: int = 3,
) -> str:
    """组装 review-responder agent prompt。

    底层逻辑：先判定再动作的二段式 + 信心度三态。
    - 高信心问题真实 → addressed（修代码）
    - 高信心问题不存在 → explained（写解释，不改代码）
    - 信心不足 → needs_human_review（ping 工程师，不改代码不发误判结论）
    """
    rv = actionable.review
    diff_clip = (pr_diff_text or "")[:8000]
    issue_line = (
        f"- **修复对象**: crashguard 上游 issue {datadog_issue_id}（{issue_title}）"
        if (datadog_issue_id or issue_title) else "- **修复对象**: crashguard 自动修复 PR"
    )
    return f"""你是 crashguard 的 PR review responder。一个 reviewer 给你的 PR 留了评论，
你的任务是**先判定该评论是否指出了真实问题 + 你对判定的信心**，再决定动作。

## 任务上下文

- **仓库**: {actionable.repo_slug}
- **PR**: #{actionable.pr_number} ({actionable.pr_url})
- **当前分支**: 已 checkout 到 PR 分支（你在仓库根目录）
{issue_line}
- **本轮**: 第 {actionable.iter_count} / {max_iter} 轮自动响应

## Reviewer 信息

- **作者**: {rv.author}
  - copilot-pull-request-reviewer / chatgpt-codex-connector / claude → bot
  - 其它 → 人工
- **状态**: {rv.state}（COMMENTED / CHANGES_REQUESTED / ...）

## Review 全文

{rv.body}

## PR 当前 diff（vs origin/main，最多 8000 字符）

{diff_clip}

---

## ⚠️ 判定原则（owner 意识）

1. **不盲从 bot**——bot review 高频提风格建议、误报；要靠 diff/源码上下文判断
2. **不盲拒**——人工 reviewer 提的问题大概率真实；bot 高质量发现也要修
3. **真实性判定标准**（基于代码证据，不要凭直觉）：
   - 评论指出的 bug 在当前 diff/上下文中能复现 → 真
   - 评论引用的行号/文件不存在 → 假
   - 评论是风格偏好（命名、注释格式），无 functional 影响 → 假
   - 评论混淆语义边界（如 stream listener 内 vs 外的异常传播） → 通常假
4. **信心判定**：
   - high：你能在代码里指出确切证据支持你的结论
   - medium：能给出推理但有歧义空间
   - low：你不确定（需要工程师裁决，**不要硬给结论**）
5. **冲突时倾向 explained / needs_human_review**——错杀好建议比错改坏代码可逆

## 执行步骤

1. 读 review_body 全文 + pr_diff_text
2. 用 Read / Grep 验证 review 的指控是否在代码里成立
3. 决定 verdict + confidence：

| confidence | verdict | 动作 |
|------------|---------|------|
| high       | addressed | **用 Edit 工具修改文件**（禁碰版本号字段，Gate#13 会拦） |
| high       | explained | 写解释，**不许改任何文件** |
| medium     | addressed | 同 addressed |
| medium     | explained | 写解释，**不许改任何文件** |
| low        | needs_human_review | 写"为什么不确定"，**不许改任何文件** |

4. **必须写 `.crashguard/review_response.json`**（用 Write 工具，否则视为失败）：

```json
{{
  "verdict": "addressed" | "explained" | "needs_human_review",
  "confidence": "high" | "medium" | "low",
  "explanation": "<1-3 段中文解释；引用真实代码行号/片段>",
  "changed_files": ["lib/foo.dart", "..."],
  "evidence_files": ["lib/bar.dart:120-135"],
  "reviewer_quote": "<从 review 摘 1-2 句你正在响应的核心诉求>"
}}
```

约束：
- `verdict=addressed` → changed_files 必须非空且都是真改过的文件
- `verdict=explained` / `needs_human_review` → changed_files 必须为 `[]`
- `confidence=low` → verdict 必须是 `needs_human_review`

## 🚫 红线

- 🚫 禁碰版本号字段（pubspec.yaml `version:` / build.gradle `versionCode|versionName` / Info.plist `CFBundleVersion*`）
- 🚫 禁 commit / push（drafter 框架处理）
- 🚫 不许编造 explanation——必须引用真实代码
- 🚫 `verdict ≠ addressed` 时禁修改任何文件
- 🚫 reviewer 没说要改的东西不要顺手改（最小变更）
"""


def _run_git(
    cmd: list[str], cwd: str, timeout: int = 30,
) -> tuple[int, str, str]:
    """轻量 git 调用，剥 GH_TOKEN（防 PAT 走默认凭证）。"""
    sub_env = dict(os.environ)
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        sub_env.pop(k, None)
    try:
        r = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env=sub_env,
        )
        return r.returncode, r.stdout.rstrip("\n\r"), r.stderr.rstrip("\n\r")
    except subprocess.TimeoutExpired:
        return 1, "", f"timeout after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


async def _run_review_agent(
    repo_path: str, prompt: str, timeout_sec: int = 600,
) -> tuple[bool, str]:
    """运行 review-responder agent 在 PR repo 内执行 prompt。

    复用 AgentOrchestrator + ClaudeCodeAgent，但用本场景的 prompt + tool 集。
    """
    try:
        from app.services.agent_orchestrator import AgentOrchestrator
    except Exception as e:
        return False, f"agent_orchestrator import failed: {e}"
    orch = AgentOrchestrator()
    try:
        agent = orch.select_agent(rule_type="crashguard")
    except Exception as e:
        return False, f"agent select failed: {e}"
    # 注入工具白名单（Edit 用于修代码；Bash 仅只读）
    try:
        import copy as _copy
        agent.config = _copy.copy(agent.config)
        existing_tools = list(getattr(agent.config, "allowed_tools", []) or [])
        needed = [
            "Edit", "MultiEdit", "Read", "Write", "Glob", "Grep",
            "Bash(git diff:*)", "Bash(git log:*)", "Bash(git status:*)",
            "Bash(ls:*)", "Bash(cat:*)", "Bash(rg:*)",
            "Bash(head:*)", "Bash(tail:*)",
        ]
        merged: list[str] = []
        seen: set[str] = set()
        for t in existing_tools + needed:
            if t not in seen:
                merged.append(t)
                seen.add(t)
        agent.config.allowed_tools = merged
    except Exception:
        logger.exception("review-responder tool injection failed (continuing)")

    workspace = Path(repo_path)
    try:
        await asyncio.wait_for(
            agent.analyze(workspace=workspace, prompt=prompt),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        return False, f"review agent timeout ({timeout_sec}s)"
    except Exception as e:
        return False, f"review agent error: {e}"
    # 清理 agent 临时文件（prompt.md / output/）
    try:
        (workspace / "prompt.md").unlink(missing_ok=True)
        import shutil
        shutil.rmtree(workspace / "output", ignore_errors=True)
    except Exception:
        pass
    return True, ""


def _read_review_response(repo_path: str) -> tuple[bool, Dict[str, Any], str]:
    """读 agent 写的 .crashguard/review_response.json + 校验字段。"""
    p = Path(repo_path) / REVIEW_RESPONSE_FILE
    if not p.exists():
        return False, {}, f"missing {REVIEW_RESPONSE_FILE}"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return False, {}, f"review_response.json decode failed: {e}"
    verdict = (data.get("verdict") or "").strip().lower()
    confidence = (data.get("confidence") or "").strip().lower()
    if verdict not in ("addressed", "explained", "needs_human_review"):
        return False, data, f"invalid verdict: {verdict!r}"
    if confidence not in ("high", "medium", "low"):
        return False, data, f"invalid confidence: {confidence!r}"
    # 信心-verdict 一致性约束
    if confidence == "low" and verdict != "needs_human_review":
        return False, data, "confidence=low must pair with verdict=needs_human_review"
    if verdict != "addressed" and (data.get("changed_files") or []):
        return False, data, f"verdict={verdict} but changed_files is non-empty"
    if verdict == "addressed" and not (data.get("changed_files") or []):
        return False, data, "verdict=addressed but changed_files is empty"
    return True, data, ""


def _post_pr_comment(
    repo_slug: str, pr_number: int, body: str,
    in_reply_to: Optional[int] = None, timeout: int = 30,
) -> tuple[bool, str]:
    """发 PR 评论。剥 GH_TOKEN 走 OAuth。

    in_reply_to 非 None 时走 `gh api POST repos/{slug}/pulls/{n}/comments`
    with `in_reply_to=<id>`——挂到对应行级 review comment 的 thread 下面，
    reviewer 在自己评论下能看到回应（这才是真 "reply"）。

    in_reply_to=None 时 fallback 到 `gh pr comment`（写 PR 顶层 issue comment），
    用于 PR-level review 没有行级 comment 时的兜底。
    """
    sub_env = dict(os.environ)
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        sub_env.pop(k, None)
    try:
        if in_reply_to is not None and in_reply_to > 0:
            r = subprocess.run(
                ["gh", "api", "-X", "POST",
                 f"repos/{repo_slug}/pulls/{pr_number}/comments",
                 "-F", f"in_reply_to={int(in_reply_to)}",
                 "-f", f"body={body}"],
                capture_output=True, text=True, timeout=timeout, env=sub_env,
            )
        else:
            r = subprocess.run(
                ["gh", "pr", "comment", str(pr_number),
                 "--repo", repo_slug, "--body", body],
                capture_output=True, text=True, timeout=timeout, env=sub_env,
            )
        if r.returncode != 0:
            return False, (r.stderr or "").strip()[:300]
        return True, (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return False, f"gh pr comment timeout ({timeout}s)"
    except Exception as e:
        return False, f"gh pr comment error: {e}"


def _format_source_review_quote(rv: ReviewItem) -> str:
    """顶部 quote 块：回应的是哪位 reviewer 的哪条 review。

    抓手：当前 gh pr comment 写到 PR 顶层 issue conversation，无法挂到 review 下面
    形成 GH thread；treatment A = 在 body 顶部贴 source review 摘要，让读者
    立即看清"回应的是谁的哪条 review"。截断 200 字避免刷屏。
    """
    body = (rv.body or "").strip()
    if body:
        if len(body) > 200:
            body = body[:200].rstrip() + "..."
        quoted = "\n".join("> " + ln for ln in body.splitlines())
    else:
        quoted = "> _(empty review body)_"
    submitted = rv.submitted_at.strftime("%Y-%m-%d %H:%M UTC") if rv.submitted_at else "?"
    author = rv.author or "?"
    state = rv.state or "?"
    return (
        f"> 👆 **回应 @{author} 的 review**（{state} · {submitted}）：\n"
        f"{quoted}\n\n"
        f"---\n\n"
    )


def _format_response_comment(
    actionable: ActionableReview,
    data: Dict[str, Any],
    fix_commit_sha: str = "",
) -> str:
    """组装回 PR 的评论 markdown。"""
    verdict = data.get("verdict", "")
    confidence = data.get("confidence", "")
    explanation = data.get("explanation", "")
    quote = data.get("reviewer_quote", "")
    evidence = data.get("evidence_files", []) or []
    iter_count = actionable.iter_count

    source_quote = _format_source_review_quote(actionable.review)

    if verdict == "addressed":
        head = (
            f"🤖 **crashguard review-responder** (iter {iter_count}, confidence={confidence})\n\n"
            f"Issue: 已修复\n\n"
        )
        if fix_commit_sha:
            head += f"Fix commit: `{fix_commit_sha[:10]}`\n\n"
    elif verdict == "explained":
        head = (
            f"🤖 **crashguard review-responder** (iter {iter_count}, confidence={confidence})\n\n"
            f"Issue: 经核对后认为此处不是 bug（详见下方解释）。\n\n"
        )
    else:  # needs_human_review
        head = (
            f"🤖 **crashguard review-responder** (iter {iter_count}, confidence={confidence})\n\n"
            f"⚠️ **信心不足，需要工程师裁决**——以下是我的分析，请人工 review 后定夺。\n\n"
        )
    body = head
    if quote:
        body += f"> 你提到：「{quote}」\n\n"
    body += f"**分析**: {explanation}\n"
    if evidence:
        body += f"\n**证据文件**: {', '.join(evidence[:5])}\n"
    body += "\n---\n_本评论由 crashguard 自动生成；如有异议请 @ 工程师手动跟进。_"
    return source_quote + body


async def _record_iteration(
    session,
    actionable: ActionableReview,
    verdict: str,
    fix_commit_sha: str = "",
    response_comment: str = "",
    error: str = "",
) -> None:
    """写 crash_pr_review_iterations 行。"""
    rv = actionable.review
    session.add(CrashPrReviewIteration(
        pr_id=actionable.pr_id,
        iter_count=actionable.iter_count,
        review_author=rv.author or "",
        review_id=rv.review_id,
        review_body_excerpt=(rv.body or "")[:2000],
        dispatched_at=datetime.utcnow(),
        completed_at=datetime.utcnow(),
        verdict=verdict,
        fix_commit_sha=fix_commit_sha or "",
        response_comment=(response_comment or "")[:2000],
        error=(error or "")[:500],
    ))
    await session.commit()


async def dispatch_review_response(
    actionable: ActionableReview,
    session,
    repo_path: str,
    issue_title: str = "",
    datadog_issue_id: str = "",
) -> Dict[str, Any]:
    """Stage C 主流程：派 agent 评判 + Gate 复用 + commit + 评论 PR + 写 DB。

    repo_path：宿主已 checkout 到 PR 分支的 sub-repo 绝对路径（由调用方准备）。
    返回 result dict 供 audit / heartbeat summary 使用。
    """
    s = get_crashguard_settings()
    max_iter = int(getattr(s, "pr_review_response_max_iterations", 3) or 3)

    # 1. 拉 PR diff（vs origin/main）作为 agent context
    rc_d, diff_text, _ = _run_git(
        ["git", "diff", "origin/main...HEAD"], repo_path, timeout=30,
    )
    if rc_d != 0:
        # 退化方案：HEAD 单 commit diff
        rc_d, diff_text, _ = _run_git(
            ["git", "diff", "HEAD~1...HEAD"], repo_path, timeout=30,
        )

    # 2. 清旧 review_response.json 避免读到上轮残留
    try:
        (Path(repo_path) / REVIEW_RESPONSE_FILE).unlink(missing_ok=True)
    except Exception:
        pass

    prompt = _build_review_prompt(
        actionable, diff_text, issue_title, datadog_issue_id, max_iter,
    )

    # 3. 跑 agent
    ok_run, err_run = await _run_review_agent(repo_path, prompt)
    if not ok_run:
        await _record_iteration(session, actionable, "failed", error=err_run[:500])
        return {"ok": False, "verdict": "failed", "error": err_run}

    # 4. 读 agent 输出
    ok_read, data, err_read = _read_review_response(repo_path)
    if not ok_read:
        await _record_iteration(session, actionable, "failed", error=err_read[:500])
        return {"ok": False, "verdict": "failed", "error": err_read}

    verdict = data.get("verdict", "")

    # 5. addressed → 跑 Gate#13（版本号保护）；通过后 commit + push
    fix_commit_sha = ""
    if verdict == "addressed":
        # 拉所有未 commit + 已 commit 的全分支 diff
        rc_full, full_diff, _ = _run_git(
            ["git", "diff", "origin/main...HEAD"], repo_path, timeout=30,
        )
        # 还有未 commit 的 worktree 改动
        rc_wt, wt_diff, _ = _run_git(
            ["git", "diff", "HEAD"], repo_path, timeout=30,
        )
        all_diff = (full_diff or "") + "\n" + (wt_diff or "")
        # Gate#13 复用
        try:
            from app.crashguard.services.pr_quality_gates import verify_no_version_bump
            ok_g13, why_g13, _ = verify_no_version_bump(all_diff)
        except Exception:
            ok_g13, why_g13 = True, ""
        if not ok_g13:
            await _record_iteration(
                session, actionable, "gate_blocked",
                error=f"gate_version_bump: {why_g13}"[:500],
            )
            return {
                "ok": False, "verdict": "gate_blocked",
                "error": f"gate_version_bump: {why_g13}",
            }
        # commit + push
        sha, push_err = await _commit_and_push_review_fix(
            repo_path, actionable, data,
        )
        if not sha:
            await _record_iteration(
                session, actionable, "failed",
                error=f"commit/push failed: {push_err}"[:500],
            )
            return {"ok": False, "verdict": "failed", "error": push_err}
        fix_commit_sha = sha

    # 6. 发 PR 评论
    body_md = _format_response_comment(actionable, data, fix_commit_sha)
    # 行级 review comment → reply 到 thread；PR-level review → fallback 顶层
    reply_to = actionable.review.source_comment_id
    ok_comm, comm_err = _post_pr_comment(
        actionable.repo_slug, actionable.pr_number, body_md,
        in_reply_to=reply_to,
    )
    if not ok_comm:
        logger.warning(
            "pr_review_responder: post comment failed for PR %s#%d: %s",
            actionable.repo_slug, actionable.pr_number, comm_err,
        )

    # 7. 写 iteration
    await _record_iteration(
        session, actionable, verdict,
        fix_commit_sha=fix_commit_sha,
        response_comment=body_md,
    )
    return {
        "ok": True,
        "verdict": verdict,
        "confidence": data.get("confidence", ""),
        "fix_commit_sha": fix_commit_sha,
        "comment_posted": ok_comm,
    }


async def _commit_and_push_review_fix(
    repo_path: str,
    actionable: ActionableReview,
    data: Dict[str, Any],
) -> tuple[str, str]:
    """commit agent 改的文件，push 到当前分支。返回 (sha, error)。"""
    changed_files = data.get("changed_files") or []
    # 清理 .crashguard/ 不进 commit
    rc1, _, err1 = _run_git(
        ["git", "add"] + [f for f in changed_files if not f.startswith(".crashguard/")],
        repo_path, timeout=30,
    )
    if rc1 != 0:
        return "", f"git add: {err1}"
    quote = (data.get("reviewer_quote") or "")[:100]
    msg = (
        f"fix(crashguard-review): respond to {actionable.review.author} "
        f"on PR #{actionable.pr_number} (iter {actionable.iter_count})\n\n"
        f"reviewer said: {quote}\n\n"
        f"verdict: addressed (confidence={data.get('confidence','')})\n"
        f"reasoning: {(data.get('explanation') or '')[:300]}"
    )
    rc2, _, err2 = _run_git(
        ["git", "commit", "-m", msg], repo_path, timeout=30,
    )
    if rc2 != 0:
        return "", f"git commit: {err2}"
    rc3, sha, err3 = _run_git(
        ["git", "rev-parse", "HEAD"], repo_path, timeout=10,
    )
    if rc3 != 0:
        return "", f"git rev-parse: {err3}"
    rc4, _, err4 = _run_git(
        ["git", "push"], repo_path, timeout=60,
    )
    # non-fast-forward 兜底：远端已演进 → pull --rebase → 重试 push 一次
    if rc4 != 0 and "non-fast-forward" in (err4 or "").lower():
        # 解析当前分支名
        rc_b, branch_name, _ = _run_git(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_path, timeout=10,
        )
        branch = (branch_name or "").strip()
        if branch and branch != "HEAD":
            rc_r, _, err_r = _run_git(
                ["git", "pull", "--rebase", "origin", branch],
                repo_path, timeout=90,
            )
            if rc_r != 0:
                # rebase 冲突 → abort 保留干净 worktree，放弃本次
                _run_git(["git", "rebase", "--abort"], repo_path, timeout=15)
                return "", f"git push then rebase failed: {err_r}"
            # 重读 HEAD sha（rebase 后 sha 会变）
            rc_h, sha2, _ = _run_git(
                ["git", "rev-parse", "HEAD"], repo_path, timeout=10,
            )
            if rc_h == 0:
                sha = sha2
            # 重试 push
            rc4, _, err4 = _run_git(["git", "push"], repo_path, timeout=60)
    if rc4 != 0:
        return "", f"git push: {err4}"
    return sha.strip(), ""
