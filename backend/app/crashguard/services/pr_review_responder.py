"""PR Review 自动响应——检测 + 调度层（Stage B + C）。

底层逻辑：crashguard 自动 PR 提交后，Copilot / Codex / Claude 等 AI reviewer 会
在代码行上留 review comment（形成 review thread），部分是真 bug、部分是"风格建议/虚警"。
让 LLM 二次自反思评判：
- 合理 → 修复 + commit + push，在 thread 下回复"已采纳，fix commit xxx"
- 不合理 → 在 thread 下回复拒绝理由，不改代码
无论哪种都把 thread 标记 resolved——PR 上不留悬空的 AI comment。

单元是 **review thread**（不是 PR-level review）：只有 thread 能 reply + resolve；
PR-level review 正文多是"PR overview"摘要，不可 resolve，2026-10-21 起不再处理。

Stage B：
- `fetch_pr_review_threads(slug, num)` GraphQL 拉未 resolve 的 thread
- `collect_actionable_reviews(pr, reviews, session)` 过滤非 AI 作者 / 已处理 / 人工已介入 / cooldown / max_iter

Stage C：`dispatch_review_round` 一轮处理一个 PR 的全部待处理 thread（一次 agent、一个 commit）。
Stage D：pr_sync tick 内接线（`pr_sync._try_run_review_responder`）。
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

# 我们自己发的回复都带这个标记——用于识别"已回复过"的 thread（DB 丢失 / 多实例兜底）
RESPONDER_MARKER = "crashguard review-responder"


# === GraphQL：review threads（含 thread id 用于 resolve，databaseId 用于 REST reply）===
_GH_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          path
          line
          originalLine
          comments(first: 50) {
            nodes {
              databaseId
              body
              createdAt
              author { __typename login }
            }
          }
        }
      }
    }
  }
}
""".strip()

_GH_RESOLVE_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
""".strip()


@dataclass
class ReviewItem:
    review_id: str            # REST_C_<root comment databaseId>（与历史行级记录同口径）
    author: str               # login 名（已去掉 "[bot]" 后缀）
    state: str                # 行级 comment 固定 COMMENTED
    submitted_at: datetime    # UTC naive
    body: str                 # root comment 全文
    # root comment 的 REST id — reply 挂到该 thread 下
    source_comment_id: Optional[int] = None
    path: str = ""             # 改动文件路径（如 lib/foo.dart）
    line: Optional[int] = None
    thread_id: str = ""        # GraphQL PRRT_xxx — resolve 用
    author_is_bot: bool = False
    # thread 内已有人工（非 bot、非本 responder）回复 → 人在跟进，agent 不插手
    human_replied: bool = False
    # thread 内已有本 responder 的回复（DB 外的去重兜底）
    responder_replied: bool = False


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


def _normalize_login(login: str) -> str:
    """REST 给 'claude[bot]'，GraphQL 给 'claude'——统一去掉后缀再比较。"""
    login = (login or "").strip()
    if login.lower().endswith("[bot]"):
        login = login[: -len("[bot]")]
    return login


def _gh_env() -> Dict[str, str]:
    """剥 GH_TOKEN/GITHUB_TOKEN 让 gh 走 OAuth（和 pr_drafter / pr_sync 同款）。"""
    import os as _os
    sub_env = dict(_os.environ)
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        sub_env.pop(k, None)
    return sub_env


def fetch_pr_review_threads(
    repo_slug: str, pr_number: int, timeout: int = 30
) -> tuple[bool, List[ReviewItem], str]:
    """拉 PR 上**未 resolve** 的 review thread，每个 thread 以 root comment 为单元。

    返回 (ok, items, error)。
    """
    if "/" not in repo_slug:
        return False, [], f"invalid repo_slug: {repo_slug}"
    owner, repo = repo_slug.split("/", 1)
    try:
        r = subprocess.run(
            [
                "gh", "api", "graphql",
                "-F", f"owner={owner}",
                "-F", f"repo={repo}",
                "-F", f"number={pr_number}",
                "-f", f"query={_GH_THREADS_QUERY}",
            ],
            capture_output=True, text=True, timeout=timeout, env=_gh_env(),
        )
        if r.returncode != 0:
            return False, [], (r.stderr or "").strip()[:300]
        try:
            data = json.loads(r.stdout or "{}")
        except json.JSONDecodeError as e:
            return False, [], f"json decode failed: {e}"
        threads = (
            ((data.get("data") or {}).get("repository") or {})
            .get("pullRequest", {}) or {}
        ).get("reviewThreads", {}) or {}
        out: List[ReviewItem] = []
        for t in threads.get("nodes") or []:
            if t.get("isResolved"):
                continue
            comments = ((t.get("comments") or {}).get("nodes")) or []
            if not comments:
                continue
            root = comments[0]
            cid = root.get("databaseId")
            if not cid:
                continue
            author = root.get("author") or {}
            human_replied = False
            responder_replied = False
            for c in comments[1:]:
                body = c.get("body") or ""
                if RESPONDER_MARKER in body:
                    responder_replied = True
                elif ((c.get("author") or {}).get("__typename") or "") != "Bot":
                    human_replied = True
            out.append(ReviewItem(
                review_id=f"REST_C_{cid}",
                author=_normalize_login(author.get("login") or ""),
                state="COMMENTED",
                submitted_at=_parse_iso(root.get("createdAt") or "") or datetime.utcnow(),
                body=root.get("body") or "",
                source_comment_id=int(cid),
                path=t.get("path") or "",
                line=t.get("line") or t.get("originalLine"),
                thread_id=t.get("id") or "",
                author_is_bot=(author.get("__typename") == "Bot"),
                human_replied=human_replied,
                responder_replied=responder_replied,
            ))
        return True, out, ""
    except subprocess.TimeoutExpired:
        return False, [], f"gh graphql timeout after {timeout}s"
    except FileNotFoundError:
        return False, [], "gh CLI not installed"
    except Exception as e:
        return False, [], f"gh graphql error: {e}"


def resolve_review_thread(thread_id: str, timeout: int = 30) -> tuple[bool, str]:
    """把 review thread 标记 resolved。返回 (ok, error)。"""
    if not thread_id:
        return False, "thread_id missing"
    try:
        r = subprocess.run(
            [
                "gh", "api", "graphql",
                "-f", f"threadId={thread_id}",
                "-f", f"query={_GH_RESOLVE_MUTATION}",
            ],
            capture_output=True, text=True, timeout=timeout, env=_gh_env(),
        )
        if r.returncode != 0:
            return False, (r.stderr or "").strip()[:300]
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"gh graphql resolve timeout ({timeout}s)"
    except Exception as e:
        return False, f"gh graphql resolve error: {e}"


@dataclass
class ActionableReview:
    """通过过滤的 review thread，可以派给 agent 评判 + 修复 / 拒绝。"""
    pr_id: int                # DB 内部 ID (crash_pull_requests.id)
    pr_url: str
    repo_slug: str
    pr_number: int
    review: ReviewItem
    iter_count: int           # 本轮是该 PR 的第几轮自动响应


def _is_ai_reviewer(r: ReviewItem, allowed_authors: set[str], accept_any_bot: bool) -> bool:
    if accept_any_bot and r.author_is_bot:
        return True
    return _normalize_login(r.author).lower() in allowed_authors


async def collect_actionable_reviews(
    pr: CrashPullRequest, reviews: List[ReviewItem], session,
) -> tuple[List[ActionableReview], Dict[str, int]]:
    """从 PR 的未 resolve thread 中过滤出"本轮应派 agent 处理"的子集。

    过滤链（按序）：
    1. enabled kill switch
    2. 作者是 AI reviewer（GitHub Bot 账号，或在白名单内）
    3. body 长度 ≥ min_body_chars（"LGTM" 等噪声跳过）
    4. 未处理过（DB 里无非 failed 记录，且 thread 里没有本 responder 的回复）
    5. thread 里没有人工回复（人在跟进时 agent 不插手）
    6. 轮数 < max_iterations（防 fix → bot 再评 → 再 fix 循环）
    7. cooldown：距上轮 ≥ N min（给 bot 对新 push 重新 review 的时间）

    一轮会带上该 PR 全部待处理 thread（上限 max_threads_per_round），由一次 agent
    统一评判、一个 commit 统一修复。返回 (actionable_list, counters)。
    """
    s = get_crashguard_settings()
    counters = {
        "fetched": len(reviews),
        "kill_switch": 0,
        "non_whitelist_author": 0,
        "body_too_short": 0,
        "already_processed": 0,
        "human_engaged": 0,
        "cooldown": 0,
        "max_iter": 0,
        "over_round_cap": 0,
        "actionable": 0,
    }

    if not getattr(s, "pr_review_response_enabled", False):
        counters["kill_switch"] = len(reviews)
        return [], counters

    allowed_authors = {
        _normalize_login(a).lower()
        for a in (getattr(s, "pr_review_response_allowed_authors", []) or [])
    }
    accept_any_bot = bool(getattr(s, "pr_review_response_accept_any_bot", True))
    min_body = int(getattr(s, "pr_review_response_min_body_chars", 50) or 50)
    max_iter = int(getattr(s, "pr_review_response_max_iterations", 3) or 3)
    max_per_round = int(getattr(s, "pr_review_response_max_threads_per_round", 10) or 10)
    cooldown = timedelta(
        minutes=int(getattr(s, "pr_review_response_cooldown_minutes", 20) or 20)
    )

    history = (await session.execute(
        select(CrashPrReviewIteration)
        .where(CrashPrReviewIteration.pr_id == pr.id)
        .order_by(desc(CrashPrReviewIteration.dispatched_at))
    )).scalars().all()
    # failed 的允许下轮重试（消耗轮数，max_iter 兜底不会无限重试）
    processed_review_ids = {
        h.review_id for h in history if h.review_id and h.verdict != "failed"
    }
    last_dispatched_at: Optional[datetime] = (
        history[0].dispatched_at if history else None
    )
    rounds_so_far = len({h.iter_count for h in history})
    in_cooldown = (
        last_dispatched_at is not None
        and (datetime.utcnow() - last_dispatched_at) < cooldown
    )

    repo_slug, pr_number = _parse_pr_url(pr.pr_url or "")
    if not repo_slug or pr_number <= 0:
        return [], counters

    out: List[ActionableReview] = []
    for r in reviews:
        if not _is_ai_reviewer(r, allowed_authors, accept_any_bot):
            counters["non_whitelist_author"] += 1
            continue
        if len(r.body or "") < min_body:
            counters["body_too_short"] += 1
            continue
        if r.review_id in processed_review_ids or r.responder_replied:
            counters["already_processed"] += 1
            continue
        if r.human_replied:
            counters["human_engaged"] += 1
            continue
        if rounds_so_far >= max_iter:
            counters["max_iter"] += 1
            continue
        if in_cooldown:
            counters["cooldown"] += 1
            continue
        if len(out) >= max_per_round:
            counters["over_round_cap"] += 1
            continue
        out.append(ActionableReview(
            pr_id=int(pr.id),
            pr_url=pr.pr_url or "",
            repo_slug=repo_slug,
            pr_number=pr_number,
            review=r,
            iter_count=rounds_so_far + 1,
        ))

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
# Stage C: Dispatcher（派 agent + Gate 复用 + commit + 回复 + resolve）
# ============================================================
import asyncio
import os
from pathlib import Path

REVIEW_RESPONSE_FILE = ".crashguard/review_response.json"


def _build_review_prompt(
    actionables: List[ActionableReview],
    pr_diff_text: str,
    issue_title: str = "",
    datadog_issue_id: str = "",
    max_iter: int = 3,
) -> str:
    """组装 review-responder agent prompt（一轮多条 comment）。

    底层逻辑：先判定再动作——每条 comment 二选一：
    - 合理 → addressed（修代码）
    - 不合理 / 证据不足 → explained（回复拒绝理由，不改代码）
    """
    first = actionables[0]
    diff_clip = (pr_diff_text or "")[:8000]
    issue_line = (
        f"- **修复对象**: crashguard 上游 issue {datadog_issue_id}（{issue_title}）"
        if (datadog_issue_id or issue_title) else "- **修复对象**: crashguard 自动修复 PR"
    )
    comment_blocks = []
    for a in actionables:
        rv = a.review
        loc = f"{rv.path}:{rv.line}" if rv.path else "(PR 级)"
        comment_blocks.append(
            f"### comment_id={rv.source_comment_id}\n\n"
            f"- **作者**: {rv.author}\n"
            f"- **位置**: {loc}\n\n"
            f"{rv.body}\n"
        )
    comments_md = "\n".join(comment_blocks)
    return f"""你是 crashguard 的 PR review responder。AI reviewer 在你的 PR 上留了 {len(actionables)} 条 review comment，
你的任务是**逐条判定该 comment 是否合理**，合理就修改代码采纳，不合理就给出拒绝理由。

## 任务上下文

- **仓库**: {first.repo_slug}
- **PR**: #{first.pr_number} ({first.pr_url})
- **当前分支**: 已 checkout 到 PR 分支（你在仓库根目录）
{issue_line}
- **本轮**: 第 {first.iter_count} / {max_iter} 轮自动响应

## 待处理的 review comment

{comments_md}

## PR 当前 diff（vs base，最多 8000 字符）

{diff_clip}

---

## ⚠️ 判定原则（owner 意识）

1. **不盲从 bot**——AI review 高频提风格建议、误报；要靠 diff/源码上下文判断
2. **不盲拒**——高质量发现（真 bug、真竞态、真资源泄漏）必须修
3. **合理性判定标准**（基于代码证据，不要凭直觉）：
   - comment 指出的 bug 在当前代码中能成立 → 合理
   - comment 引用的行号/文件不存在、或描述与代码不符 → 不合理
   - 纯风格偏好（命名、注释格式），无 functional 影响 → 不合理
   - 要求补单测 / 大范围重构，超出本次崩溃修复的最小变更范围 → 不合理（说明原因）
   - comment 混淆语义边界（如 stream listener 内 vs 外的异常传播） → 通常不合理
4. **拿不准时倾向 explained**——错拒好建议（人还能看到回复再跟进）比错改坏代码更可逆

## 执行步骤

1. 逐条读 comment + diff
2. 用 Read / Grep 验证每条 comment 的指控是否在代码里成立
3. 每条 comment 二选一：
   - `addressed`：**用 Edit 工具修改文件**采纳（禁碰版本号字段，Gate#13 会拦）
   - `explained`：写拒绝理由，**不为它改任何文件**
4. **必须写 `.crashguard/review_response.json`**（用 Write 工具，否则视为失败；必须是合法 JSON，字符串里的双引号要转义）：

```json
{{
  "items": [
    {{
      "comment_id": <上面的 comment_id 数字>,
      "verdict": "addressed" | "explained",
      "confidence": "high" | "medium" | "low",
      "explanation": "<1-3 段中文解释；引用真实代码行号/片段>",
      "changed_files": ["path/to/file", "..."],
      "evidence_files": ["path/to/other:120-135"],
      "reviewer_quote": "<从 comment 摘 1-2 句你正在响应的核心诉求>"
    }}
  ]
}}
```

约束：
- 每条 comment_id 都必须有且只有一个 item
- `verdict=addressed` → changed_files 必须非空且都是真改过的文件（路径相对仓库根目录）
- `verdict=explained` → changed_files 必须为 `[]`
- `confidence=low` → verdict 必须是 `explained`

## 🚫 红线

- 🚫 禁碰版本号字段（pubspec.yaml `version:` / build.gradle `versionCode|versionName` / Info.plist `CFBundleVersion*`）
- 🚫 禁 commit / push（框架处理）
- 🚫 不许编造 explanation——必须引用真实代码
- 🚫 comment 没要求改的东西不要顺手改（最小变更）
"""


def _run_git(
    cmd: list[str], cwd: str, timeout: int = 30,
) -> tuple[int, str, str]:
    """轻量 git 调用，剥 GH_TOKEN（防 PAT 走默认凭证）。"""
    try:
        r = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env=_gh_env(),
        )
        return r.returncode, r.stdout.rstrip("\n\r"), r.stderr.rstrip("\n\r")
    except subprocess.TimeoutExpired:
        return 1, "", f"timeout after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


async def _run_review_agent(
    repo_path: str, prompt: str, timeout_sec: int = 900,
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


def _validate_item(data: Dict[str, Any]) -> str:
    """校验单条 comment 的判定。返回空串=合法，否则为错误原因。"""
    verdict = (data.get("verdict") or "").strip().lower()
    confidence = (data.get("confidence") or "").strip().lower()
    if verdict not in ("addressed", "explained"):
        return f"invalid verdict: {verdict!r}"
    if confidence not in ("high", "medium", "low"):
        return f"invalid confidence: {confidence!r}"
    if confidence == "low" and verdict != "explained":
        return "confidence=low must pair with verdict=explained"
    if verdict == "explained" and (data.get("changed_files") or []):
        return "verdict=explained but changed_files is non-empty"
    if verdict == "addressed" and not (data.get("changed_files") or []):
        return "verdict=addressed but changed_files is empty"
    return ""


def _read_review_response(
    repo_path: str, expected_ids: List[int],
) -> tuple[bool, Dict[int, Dict[str, Any]], str]:
    """读 agent 写的 .crashguard/review_response.json。

    返回 (ok, {comment_id: item}, error)。ok=False 表示整个文件不可用；
    单条 item 的合法性由调用方用 `_validate_item` 判。
    """
    p = Path(repo_path) / REVIEW_RESPONSE_FILE
    if not p.exists():
        return False, {}, f"missing {REVIEW_RESPONSE_FILE}"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return False, {}, f"review_response.json decode failed: {e}"
    if not isinstance(data, dict):
        return False, {}, "review_response.json is not an object"
    items = data.get("items")
    if items is None and "verdict" in data and len(expected_ids) == 1:
        # 单条时 agent 偶尔直接写单个对象——兼容
        items = [{**data, "comment_id": expected_ids[0]}]
    if not isinstance(items, list):
        return False, {}, "review_response.json missing items[]"
    out: Dict[int, Dict[str, Any]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            cid = int(it.get("comment_id"))
        except (TypeError, ValueError):
            continue
        it["verdict"] = (it.get("verdict") or "").strip().lower()
        it["confidence"] = (it.get("confidence") or "").strip().lower()
        out[cid] = it
    return True, out, ""


def _post_pr_comment(
    repo_slug: str, pr_number: int, body: str,
    in_reply_to: Optional[int] = None, timeout: int = 30,
) -> tuple[bool, str]:
    """发 PR 评论。剥 GH_TOKEN 走 OAuth。

    in_reply_to 非 None 时走 `gh api POST repos/{slug}/pulls/{n}/comments`
    with `in_reply_to=<id>`——挂到对应 review comment 的 thread 下面。

    in_reply_to=None 时 fallback 到 `gh pr comment`（写 PR 顶层 issue comment）。
    """
    try:
        if in_reply_to is not None and in_reply_to > 0:
            r = subprocess.run(
                ["gh", "api", "-X", "POST",
                 f"repos/{repo_slug}/pulls/{pr_number}/comments",
                 "-F", f"in_reply_to={int(in_reply_to)}",
                 "-f", f"body={body}"],
                capture_output=True, text=True, timeout=timeout, env=_gh_env(),
            )
        else:
            r = subprocess.run(
                ["gh", "pr", "comment", str(pr_number),
                 "--repo", repo_slug, "--body", body],
                capture_output=True, text=True, timeout=timeout, env=_gh_env(),
            )
        if r.returncode != 0:
            return False, (r.stderr or "").strip()[:300]
        return True, (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return False, f"gh pr comment timeout ({timeout}s)"
    except Exception as e:
        return False, f"gh pr comment error: {e}"


def _format_response_comment(
    actionable: ActionableReview,
    data: Dict[str, Any],
    fix_commit_sha: str = "",
) -> str:
    """组装回到 thread 下的评论 markdown。"""
    verdict = data.get("verdict", "")
    confidence = data.get("confidence", "")
    explanation = data.get("explanation", "")
    quote = data.get("reviewer_quote", "")
    evidence = data.get("evidence_files", []) or []
    iter_count = actionable.iter_count

    head = f"🤖 **{RESPONDER_MARKER}** (iter {iter_count}, confidence={confidence})\n\n"
    if verdict == "addressed":
        head += "✅ **已采纳**，已按建议修改。\n\n"
        if fix_commit_sha:
            head += f"Fix commit: `{fix_commit_sha[:10]}`\n\n"
    else:
        head += "❎ **不采纳**，理由见下方分析。\n\n"
    body = head
    if quote:
        body += f"> 你提到：「{quote}」\n\n"
    body += f"**分析**: {explanation}\n"
    if evidence:
        body += f"\n**证据文件**: {', '.join(evidence[:5])}\n"
    body += "\n---\n_本评论由 crashguard 自动生成并已将该 thread 标记为 resolved；如有异议请 unresolve 并 @ 工程师手动跟进。_"
    return body


async def _record_iteration(
    session,
    actionable: ActionableReview,
    verdict: str,
    fix_commit_sha: str = "",
    response_comment: str = "",
    error: str = "",
) -> None:
    """写 crash_pr_review_iterations 行；同 (pr_id, review_id) 的 failed 旧行原地覆盖（重试）。"""
    rv = actionable.review
    now = datetime.utcnow()
    existing = (await session.execute(
        select(CrashPrReviewIteration).where(and_(
            CrashPrReviewIteration.pr_id == actionable.pr_id,
            CrashPrReviewIteration.review_id == rv.review_id,
        ))
    )).scalar_one_or_none()
    row = existing or CrashPrReviewIteration(
        pr_id=actionable.pr_id, review_id=rv.review_id,
    )
    row.iter_count = actionable.iter_count
    row.review_author = rv.author or ""
    row.review_body_excerpt = (rv.body or "")[:2000]
    row.dispatched_at = now
    row.completed_at = now
    row.verdict = verdict
    row.fix_commit_sha = fix_commit_sha or ""
    row.response_comment = (response_comment or "")[:2000]
    row.error = (error or "")[:500]
    if existing is None:
        session.add(row)
    await session.commit()


def _pr_diff(repo_path: str, base_ref: str) -> str:
    """PR 全分支 diff：origin/<base>...HEAD → origin/main...HEAD → HEAD~1...HEAD。"""
    bases = [b for b in (base_ref, "main") if b]
    for b in dict.fromkeys(bases):
        rc, out, _ = _run_git(["git", "diff", f"origin/{b}...HEAD"], repo_path, timeout=30)
        if rc == 0:
            return out
    _, out, _ = _run_git(["git", "diff", "HEAD~1...HEAD"], repo_path, timeout=30)
    return out


def _discard_worktree_changes(repo_path: str) -> None:
    """丢弃 agent 残留改动，保证下次 checkout 不被 dirty worktree 卡住。"""
    _run_git(["git", "reset", "--hard", "HEAD"], repo_path, timeout=30)
    _run_git(["git", "clean", "-fd"], repo_path, timeout=30)


async def dispatch_review_round(
    actionables: List[ActionableReview],
    session,
    repo_path: str,
    issue_title: str = "",
    datadog_issue_id: str = "",
    base_ref: str = "",
) -> Dict[str, Any]:
    """Stage C 主流程（一轮 = 一个 PR 的全部待处理 thread）：

    派 agent 逐条评判 → 采纳项过 Gate#13 后统一 commit + push →
    每条 thread 回复（采纳附 commit / 拒绝附理由）→ resolve thread → 写 DB。

    repo_path：宿主已 checkout 到 PR 分支的 sub-repo 绝对路径（由调用方准备）。
    """
    if not actionables:
        return {"ok": True, "verdicts": []}
    s = get_crashguard_settings()
    max_iter = int(getattr(s, "pr_review_response_max_iterations", 3) or 3)
    first = actionables[0]

    try:
        (Path(repo_path) / REVIEW_RESPONSE_FILE).unlink(missing_ok=True)
    except Exception:
        pass

    prompt = _build_review_prompt(
        actionables, _pr_diff(repo_path, base_ref),
        issue_title, datadog_issue_id, max_iter,
    )

    try:
        ok_run, err_run = await _run_review_agent(repo_path, prompt)
        ok_read, items, err_read = (
            _read_review_response(repo_path, [a.review.source_comment_id for a in actionables])
            if ok_run else (False, {}, err_run)
        )
        if not ok_read:
            for a in actionables:
                await _record_iteration(session, a, "failed", error=err_read)
            return {"ok": False, "verdicts": ["failed"] * len(actionables), "error": err_read}

        # 逐条归类：合法的 addressed / explained；其余 failed
        decided: List[tuple[ActionableReview, Dict[str, Any]]] = []
        failures: Dict[str, str] = {}
        for a in actionables:
            it = items.get(int(a.review.source_comment_id or 0))
            if it is None:
                failures[a.review.review_id] = "agent returned no item for this comment"
                continue
            why = _validate_item(it)
            if why:
                failures[a.review.review_id] = why
                continue
            decided.append((a, it))

        addressed = [(a, it) for a, it in decided if it["verdict"] == "addressed"]
        fix_sha = ""
        fix_err = ""
        fix_verdict = "failed"
        if addressed:
            fix_sha, fix_err, fix_verdict = await _apply_fix(
                repo_path, first, [it for _, it in addressed], base_ref,
            )

        verdicts: List[str] = []
        for a, it in decided:
            if it["verdict"] == "addressed" and not fix_sha:
                # 修复没落地：不回复"已采纳"、不 resolve，记录后下一轮重试
                await _record_iteration(session, a, fix_verdict, error=fix_err)
                verdicts.append(fix_verdict)
                continue
            body_md = _format_response_comment(
                a, it, fix_sha if it["verdict"] == "addressed" else "",
            )
            ok_comm, comm_err = _post_pr_comment(
                a.repo_slug, a.pr_number, body_md,
                in_reply_to=a.review.source_comment_id,
            )
            err = ""
            if not ok_comm:
                err = f"post reply failed: {comm_err}"
                logger.warning(
                    "pr_review_responder: %s (PR %s#%d)", err, a.repo_slug, a.pr_number,
                )
            else:
                ok_res, res_err = resolve_review_thread(a.review.thread_id)
                if not ok_res:
                    err = f"resolve thread failed: {res_err}"
                    logger.warning(
                        "pr_review_responder: %s (PR %s#%d)", err, a.repo_slug, a.pr_number,
                    )
            await _record_iteration(
                session, a, it["verdict"],
                fix_commit_sha=fix_sha if it["verdict"] == "addressed" else "",
                response_comment=body_md, error=err,
            )
            verdicts.append(it["verdict"])
        for a in actionables:
            if a.review.review_id in failures:
                await _record_iteration(
                    session, a, "failed", error=failures[a.review.review_id],
                )
                verdicts.append("failed")
        return {"ok": True, "verdicts": verdicts, "fix_commit_sha": fix_sha}
    finally:
        _discard_worktree_changes(repo_path)


async def _apply_fix(
    repo_path: str,
    actionable: ActionableReview,
    items: List[Dict[str, Any]],
    base_ref: str,
) -> tuple[str, str, str]:
    """Gate#13 + commit + push 本轮全部采纳项。返回 (sha, error, verdict_on_failure)。"""
    _, wt_diff, _ = _run_git(["git", "diff", "HEAD"], repo_path, timeout=30)
    all_diff = _pr_diff(repo_path, base_ref) + "\n" + (wt_diff or "")
    try:
        from app.crashguard.services.pr_quality_gates import verify_no_version_bump
        ok_g13, why_g13, _ = verify_no_version_bump(all_diff)
    except Exception:
        ok_g13, why_g13 = True, ""
    if not ok_g13:
        return "", f"gate_version_bump: {why_g13}", "gate_blocked"
    sha, push_err = await _commit_and_push_review_fix(repo_path, actionable, items)
    if not sha:
        return "", f"commit/push failed: {push_err}", "failed"
    return sha, "", ""


async def _commit_and_push_review_fix(
    repo_path: str,
    actionable: ActionableReview,
    items: List[Dict[str, Any]],
) -> tuple[str, str]:
    """commit agent 为采纳项改的文件，push 到当前分支。返回 (sha, error)。"""
    changed_files: List[str] = []
    for it in items:
        for f in it.get("changed_files") or []:
            if f and not f.startswith(".crashguard/") and f not in changed_files:
                changed_files.append(f)
    rc1, _, err1 = _run_git(["git", "add", "--"] + changed_files, repo_path, timeout=30)
    if rc1 != 0:
        return "", f"git add: {err1}"
    rc_s, _, _ = _run_git(["git", "diff", "--cached", "--quiet"], repo_path, timeout=30)
    if rc_s == 0:
        return "", "agent claimed addressed but produced no file changes"
    quotes = "\n".join(
        f"- {(it.get('reviewer_quote') or '')[:100]}" for it in items
    )
    msg = (
        f"fix(crashguard-review): address {len(items)} review comment(s) "
        f"on PR #{actionable.pr_number} (iter {actionable.iter_count})\n\n"
        f"reviewer said:\n{quotes}\n"
    )
    rc2, _, err2 = _run_git(["git", "commit", "-m", msg], repo_path, timeout=30)
    if rc2 != 0:
        return "", f"git commit: {err2}"
    rc3, sha, err3 = _run_git(["git", "rev-parse", "HEAD"], repo_path, timeout=10)
    if rc3 != 0:
        return "", f"git rev-parse: {err3}"
    rc4, _, err4 = _run_git(["git", "push"], repo_path, timeout=60)
    # non-fast-forward 兜底：远端已演进 → pull --rebase → 重试 push 一次
    if rc4 != 0 and "non-fast-forward" in (err4 or "").lower():
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
            rc_h, sha2, _ = _run_git(["git", "rev-parse", "HEAD"], repo_path, timeout=10)
            if rc_h == 0:
                sha = sha2
            rc4, _, err4 = _run_git(["git", "push"], repo_path, timeout=60)
    if rc4 != 0:
        return "", f"git push: {err4}"
    return sha.strip(), ""
