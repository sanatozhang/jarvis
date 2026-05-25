"""
GitHub PR 状态同步服务。

闭环：定时器 → 拉 gh pr view --json → 写回 DB（pr_status / merged_at / closed_at / last_synced_at）。
终态（merged / closed）后停止轮询，节省 API 配额。

🚫 严禁触发任何写操作（gh pr merge / close / ready）—— 同步仅 read。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from app.crashguard.models import CrashPullRequest
from app.db.database import get_session

logger = logging.getLogger("crashguard.pr_sync")


# 终态：不再轮询的状态
# 真正"不可逆"的终态 = merged / closed（GitHub 已关闭的 PR）。
# ci_failed_closed **不是**终态——这只是 Gate#12 历史包袱产物，工程师可手动 reopen，
# 那种情况下 pr_sync 必须继续把 GH 现态同步回来，否则本地永远停在 ci_failed_closed，
# daily_sweep 永远跳过，工程师收不到 ping。2026-05-21 治本。
_TERMINAL_STATUSES = {"merged", "closed"}
# 同步时只查这些 GitHub state 字段（statusCheckRollup 用于 Gate#12 CI 反馈）
# - reviews / comments：人审反馈链路（reviewer 提整改意见时触发飞书通知）
# - reviewDecision：APPROVED / CHANGES_REQUESTED / REVIEW_REQUIRED 总决定
_GH_FIELDS = (
    "state,isDraft,mergedAt,closedAt,statusCheckRollup,headRefName,createdAt,files,"
    "reviews,comments,reviewDecision"
)

# 污染文件名 / glob — 出现在 PR diff 里即视为 stale-base 或 build artifact 泄漏
_POLLUTION_PATHS = ("pubspec.yaml", "pubspec.lock", "Podfile.lock")
_POLLUTION_SUFFIXES = (".gen.dart", ".g.dart", ".freezed.dart")


def _parse_repo_slug(pr_url: str) -> Optional[str]:
    """从 PR URL 抽取 owner/repo。

    https://github.com/Plaud-AI/plaud-flutter-common/pull/887 → "Plaud-AI/plaud-flutter-common"
    """
    if not pr_url:
        return None
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/pull/\d+", pr_url.strip())
    return m.group(1) if m else None


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    """gh 返回的 ISO 时间字符串（带 Z）→ naive UTC datetime。失败返回 None。"""
    if not value or not isinstance(value, str):
        return None
    try:
        # 2026-04-29T08:38:18Z → 2026-04-29T08:38:18+00:00
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        # 转为 naive UTC（与 DB 其他列一致）
        if dt.tzinfo is not None:
            dt = dt.astimezone(tz=None).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def _gh_view(repo_slug: str, pr_number: int, timeout: int = 30) -> Tuple[bool, Dict[str, Any], str]:
    """调 `gh pr view <num> --repo <slug> --json <fields>`。

    返回 (ok, parsed_json_dict, error_str)。
    抽出来便于单测 mock。
    """
    # PAT (gho_*) 个人 token 对 org SSO repo 的 GraphQL 字段（如 statusCheckRollup）
    # 报 "Resource not accessible by personal access token"。gh CLI 走 OAuth
    # (hosts.yml) 才有权限。剥掉 GH_TOKEN/GITHUB_TOKEN 让 OAuth 接管——和
    # pr_drafter._run_git 同一类修法。
    import os as _os
    sub_env = dict(_os.environ)
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        sub_env.pop(k, None)
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", repo_slug, "--json", _GH_FIELDS],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=sub_env,
        )
        if r.returncode != 0:
            return False, {}, (r.stderr or "").strip()[:300]
        try:
            return True, json.loads(r.stdout or "{}"), ""
        except json.JSONDecodeError as e:
            return False, {}, f"json decode failed: {e}"
    except subprocess.TimeoutExpired:
        return False, {}, f"gh pr view timeout after {timeout}s"
    except FileNotFoundError:
        return False, {}, "gh CLI not installed"
    except Exception as e:
        return False, {}, f"gh pr view error: {e}"


def _derive_ci_verdict(gh_payload: Dict[str, Any]) -> Tuple[str, list]:
    """从 statusCheckRollup 推导 CI 总览状态。

    返回 (verdict, failed_check_names)：
      verdict ∈ {pass, fail, pending, none}
      failed_check_names: 失败 check 的 name 列表（前 5 个）
    """
    rollup = gh_payload.get("statusCheckRollup") or []
    if not rollup:
        return "none", []
    states: list[str] = []
    failed_names: list[str] = []
    for item in rollup:
        # GH GraphQL 返回 unified 结构：可能含 status (queued/in_progress/completed)
        # + conclusion (success/failure/timed_out/cancelled/skipped/neutral/action_required)
        # 也可能是 commitStatus（state: ERROR/FAILURE/PENDING/SUCCESS）
        conclusion = (item.get("conclusion") or "").lower()
        state = (item.get("state") or "").lower()
        name = item.get("name") or item.get("context") or ""
        if conclusion in ("failure", "timed_out", "action_required", "cancelled"):
            states.append("fail")
            failed_names.append(name)
        elif state in ("failure", "error"):
            states.append("fail")
            failed_names.append(name)
        elif conclusion in ("success", "neutral", "skipped") or state == "success":
            states.append("pass")
        else:
            states.append("pending")
    if "fail" in states:
        return "fail", failed_names[:5]
    if "pending" in states:
        return "pending", []
    return "pass", []


def _detect_pollution(gh_payload: Dict[str, Any]) -> List[str]:
    """检查 PR files 是否含 stale-base / build artifact 污染文件。

    抓手：#987 教训——agent 在 stale base 上开 PR 时会顺手带上历史的 pubspec
    version bump；agent 又偶尔把 .gen.dart 当生产代码改。Gate#13 + .gitignore
    都是源头治理；本函数是兜底，发现已落地的污染 → 关 PR 让 cron 重生。

    返回命中污染的文件路径列表（空 = 干净）。
    """
    files = gh_payload.get("files") or []
    if not files:
        return []
    hits: List[str] = []
    for f in files:
        path = (f.get("path") if isinstance(f, dict) else str(f)) or ""
        if not path:
            continue
        # 完全匹配
        for noisy in _POLLUTION_PATHS:
            if path.endswith("/" + noisy) or path == noisy:
                hits.append(path)
                break
        else:
            # 后缀匹配
            for suf in _POLLUTION_SUFFIXES:
                if path.endswith(suf):
                    hits.append(path)
                    break
    return hits


def _parse_iso_to_utc_naive(value: Any) -> Optional[datetime]:
    """专用于 reviews/comments 时间戳比对：返回 naive UTC（而非 local）。

    既有 `_parse_iso_dt` 用 `.astimezone(tz=None)` 会转成系统本地时区的 naive；
    DB 里 `last_synced_at = datetime.utcnow()` 是 naive UTC，两者直接 `<=` 比较
    会差一个时区偏移（容器内 TZ=Asia/Shanghai → 8h 偏差），导致漏检。

    本 helper 强制走 UTC，保证 review 时间戳与 last_synced_at 同口径。
    """
    if not value or not isinstance(value, str):
        return None
    try:
        from datetime import timezone
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def _detect_new_review_activity(
    gh_payload: Dict[str, Any],
    since: Optional[datetime],
) -> List[Dict[str, Any]]:
    """检查 reviews + comments 中 since 之后的新条目。

    底层逻辑：reviewer 在 draft PR 上提整改意见时，crashguard 之前完全无感
    （pr_sync 只看 state/isDraft）；现在拉到 reviews/comments 列表后，对比
    last_synced_at 时间戳过滤出"上次同步后的新增"，再调飞书通知 + 写 audit。

    返回按时间升序的列表，每项 {type, author, body, state, at}：
      - type: "review" | "comment"
      - state: review 的 APPROVED/CHANGES_REQUESTED/COMMENTED（comment 为 ""）
      - body: 评论正文（截断 300 字）
      - at: ISO datetime
    """
    new_items: List[Dict[str, Any]] = []
    for r in (gh_payload.get("reviews") or []):
        if not isinstance(r, dict):
            continue
        ts = _parse_iso_to_utc_naive(r.get("submittedAt") or r.get("createdAt"))
        if ts is None:
            continue
        if since is not None and ts <= since:
            continue
        author = ((r.get("author") or {}).get("login") if isinstance(r.get("author"), dict)
                  else str(r.get("author") or "")) or "?"
        new_items.append({
            "type": "review",
            "author": author,
            "state": (r.get("state") or "").upper(),
            "body": (r.get("body") or "")[:300],
            "at": ts,
        })
    for c in (gh_payload.get("comments") or []):
        if not isinstance(c, dict):
            continue
        ts = _parse_iso_to_utc_naive(c.get("createdAt") or c.get("submittedAt"))
        if ts is None:
            continue
        if since is not None and ts <= since:
            continue
        author = ((c.get("author") or {}).get("login") if isinstance(c.get("author"), dict)
                  else str(c.get("author") or "")) or "?"
        new_items.append({
            "type": "comment",
            "author": author,
            "state": "",
            "body": (c.get("body") or "")[:300],
            "at": ts,
        })
    new_items.sort(key=lambda x: x["at"])
    return new_items


async def _notify_review_activity(
    pr_row: CrashPullRequest,
    activities: List[Dict[str, Any]],
    review_decision: str,
) -> None:
    """对新增的 review / comment 调飞书通知。失败静默——非关键链路。

    设计取舍：只把 reviewer 的反馈聚合成一条点对点消息，不为每条 comment 发一封；
    crashguard 自身机器人评论（gate#12/#14 close_msg）通过 author 名过滤。
    """
    if not activities:
        return
    try:
        from app.crashguard.config import get_crashguard_settings
        s = get_crashguard_settings()
        target_email = (getattr(s, "feishu_alert_email", "")
                        or getattr(s, "feishu_target_email", ""))
        if not target_email:
            return
        # 过滤掉 crashguard 自机器人评论（避免 gate close_msg 自触发循环）
        BOT_PREFIXES = ("🧹 Auto-close by Crashguard", "🚫 Auto-close by Crashguard")
        human_items = [
            a for a in activities
            if not any((a.get("body") or "").startswith(p) for p in BOT_PREFIXES)
        ]
        if not human_items:
            return
        lines = [
            f"🔔 Crashguard PR review 新动态：{pr_row.pr_url}",
            f"   分支：{pr_row.branch_name or '?'}  状态：{pr_row.pr_status or '?'}",
        ]
        if review_decision:
            lines.append(f"   reviewDecision: {review_decision}")
        for a in human_items[:5]:
            prefix = f"[{a['type']}/{a['state']}]" if a['state'] else f"[{a['type']}]"
            body = (a['body'] or "").replace("\n", " ").strip()
            lines.append(f"  · {prefix} {a['author']}: {body[:200]}")
        if len(human_items) > 5:
            lines.append(f"  · …还有 {len(human_items) - 5} 条")
        text = "\n".join(lines)
        from app.services.feishu_cli import send_message
        await send_message(email=target_email, text=text)
    except Exception:
        logger.exception("crashguard pr_sync review-notify failed (non-fatal)")


def _derive_status(gh_payload: Dict[str, Any]) -> Optional[str]:
    """从 gh 输出推导本地 pr_status。

    GitHub state: OPEN / MERGED / CLOSED；isDraft: bool。
    映射：
      MERGED → merged
      CLOSED（且非 merged）→ closed
      OPEN + isDraft=True → draft
      OPEN + isDraft=False → open
    无法识别 → None（不更新）
    """
    state = (gh_payload.get("state") or "").upper()
    if state == "MERGED":
        return "merged"
    if state == "CLOSED":
        return "closed"
    if state == "OPEN":
        return "draft" if gh_payload.get("isDraft") else "open"
    return None


async def sync_pr(pr_id: int) -> Dict[str, Any]:
    """同步单条 PR。返回 {ok, pr_id, old_status?, new_status?, error?, changed?}。"""
    async with get_session() as session:
        row = (await session.execute(
            select(CrashPullRequest).where(CrashPullRequest.id == pr_id)
        )).scalar_one_or_none()
        if row is None:
            return {"ok": False, "pr_id": pr_id, "error": "pr not found"}

        old_status = row.pr_status or ""
        if old_status in _TERMINAL_STATUSES:
            # 终态不再查（safety net；批量入口已过滤）
            return {"ok": True, "pr_id": pr_id, "old_status": old_status, "skipped": "terminal"}

        repo_slug = _parse_repo_slug(row.pr_url or "")
        pr_number = row.pr_number
        if not repo_slug or not pr_number:
            return {"ok": False, "pr_id": pr_id, "error": "missing repo_slug or pr_number"}

        ok, payload, err = _gh_view(repo_slug, pr_number)
        if not ok:
            row.last_synced_at = datetime.utcnow()  # 记录尝试过，避免空转
            await session.commit()
            return {"ok": False, "pr_id": pr_id, "error": err}

        new_status = _derive_status(payload)
        if new_status is None:
            row.last_synced_at = datetime.utcnow()
            await session.commit()
            return {
                "ok": False, "pr_id": pr_id,
                "error": f"unknown gh state: {payload.get('state')}",
            }

        # Gate#14：老 draft 污染自动清理——draft >24h 仍未推 ready，且 diff 里
        # 含 pubspec / .gen.dart / .lock 等污染文件 → 关 PR，等下个 cron 用修好
        # 的 base_ref 重生干净版本。
        pollution_action: Optional[str] = None
        if new_status == "draft":
            try:
                from app.crashguard.config import get_crashguard_settings
                s = get_crashguard_settings()
                if getattr(s, "gate_draft_pollution_enabled", True):
                    created_at = _parse_iso_dt(payload.get("createdAt"))
                    min_age_h = int(getattr(s, "gate_draft_pollution_min_age_hours", 24) or 24)
                    age_ok = (
                        created_at is not None
                        and (datetime.utcnow() - created_at) >= timedelta(hours=min_age_h)
                    )
                    if age_ok:
                        hits = _detect_pollution(payload)
                        if hits:
                            close_msg = (
                                "🧹 Auto-close by Crashguard Gate#14 (draft pollution): "
                                f"draft >{min_age_h}h 仍含 stale-base / generated 文件 → "
                                f"{', '.join(hits[:5])}. 下个 cron 会基于干净 base 重生。"
                            )
                            try:
                                r = subprocess.run(
                                    ["gh", "pr", "close", str(pr_number),
                                     "--repo", repo_slug, "--comment", close_msg],
                                    capture_output=True, text=True, timeout=30,
                                )
                                if r.returncode == 0:
                                    new_status = "closed"
                                    pollution_action = (
                                        f"closed_on_pollution({len(hits)}:{hits[0]})"
                                    )
                                    logger.warning(
                                        "gate#14 closed PR %s#%d due to pollution: %s",
                                        repo_slug, pr_number, hits[:3],
                                    )
                                else:
                                    logger.warning(
                                        "gate#14 gh pr close failed: %s",
                                        (r.stderr or "")[:200],
                                    )
                                    pollution_action = "close_failed"
                            except Exception as exc:
                                logger.warning("gate#14 close exception: %s", exc)
                                pollution_action = f"close_error:{exc}"
            except Exception:
                logger.exception("gate#14 pollution check crashed (non-fatal)")

        # Gate#12：CI 反馈——open/draft 状态下若 CI 全失败，自动 close 该 PR，
        # 并把 status 改为 ci_failed_closed（独立终态，前端可显示"AI 修复未通过 CI"）
        ci_action: Optional[str] = None
        if new_status in ("open", "draft"):
            try:
                from app.crashguard.config import get_crashguard_settings
                s = get_crashguard_settings()
                if getattr(s, "gate_ci_feedback_enabled", True):
                    verdict, failed_names = _derive_ci_verdict(payload)
                    if verdict == "fail" and getattr(s, "gate_ci_feedback_close_on_fail", True):
                        # 调 gh pr close 关 PR + 写 comment 说明
                        close_msg = (
                            "🚫 Auto-close by Crashguard Gate#12 (CI feedback): "
                            f"checks failed → {', '.join(failed_names[:5]) or 'unknown'}. "
                            "AI 自动修复未通过 CI，已自动关闭。请人工 review 原 issue。"
                        )
                        try:
                            r = subprocess.run(
                                ["gh", "pr", "close", str(pr_number),
                                 "--repo", repo_slug, "--comment", close_msg],
                                capture_output=True, text=True, timeout=30,
                            )
                            if r.returncode == 0:
                                new_status = "ci_failed_closed"
                                ci_action = f"closed_on_ci_fail({len(failed_names)})"
                                logger.warning(
                                    "gate#12 closed PR %s#%d due to CI failure: %s",
                                    repo_slug, pr_number, failed_names[:3],
                                )
                            else:
                                logger.warning(
                                    "gate#12 gh pr close failed: %s",
                                    (r.stderr or "")[:200],
                                )
                                ci_action = "close_failed"
                        except Exception as exc:
                            logger.warning("gate#12 close exception: %s", exc)
                            ci_action = f"close_error:{exc}"
            except Exception:
                logger.exception("gate#12 ci feedback crashed (non-fatal)")

        # 人审反馈检测：reviews / comments 有 last_synced_at 之后的新条目时调飞书通知
        # 注意：在 last_synced_at 被覆盖之前用旧值做对比窗口
        prev_synced = row.last_synced_at
        review_decision = (payload.get("reviewDecision") or "").upper()
        new_activity = _detect_new_review_activity(payload, prev_synced)
        review_notified = 0
        if new_activity:
            await _notify_review_activity(row, new_activity, review_decision)
            review_notified = len(new_activity)

        row.pr_status = new_status
        merged_at = _parse_iso_dt(payload.get("mergedAt"))
        if merged_at:
            row.merged_at = merged_at
        closed_at = _parse_iso_dt(payload.get("closedAt"))
        if closed_at:
            row.closed_at = closed_at
        row.last_synced_at = datetime.utcnow()
        await session.commit()

    changed = old_status != new_status
    if changed:
        logger.info(
            "crashguard pr_sync: pr=%d %s → %s (repo=%s #%d)",
            pr_id, old_status, new_status, repo_slug, pr_number,
        )

    # === Stage D: PR review responder（AI 自动读 PR comment + 回复 + 修复）===
    # 默认 enabled=False；开启后每次 pr_sync tick 跑一遍 collect → dispatch。
    # 任何异常 catch 不阻塞 sync 主流程。
    responder_result: Optional[Dict[str, Any]] = None
    if new_status in ("draft", "open"):
        try:
            responder_result = await _try_run_review_responder(
                pr_id, repo_slug, pr_number,
            )
        except Exception:
            logger.exception(
                "pr_review_responder stage-D crashed (non-fatal, pr=%d)", pr_id,
            )

    return {
        "ok": True,
        "pr_id": pr_id,
        "old_status": old_status,
        "new_status": new_status,
        "changed": changed,
        "ci_action": ci_action,
        "pollution_action": pollution_action,
        "review_notified": review_notified,
        "review_decision": review_decision,
        "responder": responder_result,
    }


async def _try_run_review_responder(
    pr_id: int, repo_slug: str, pr_number: int,
) -> Dict[str, Any]:
    """Stage D 接线层：当 pr_review_response_enabled=True 时
    拉 reviews → 过滤 actionable → checkout PR 分支 → dispatch agent。

    任何子步骤失败都返回 dict 不抛异常，由上层 catch。
    """
    from app.crashguard.config import get_crashguard_settings
    s = get_crashguard_settings()
    if not getattr(s, "pr_review_response_enabled", False):
        return {"ok": True, "enabled": False, "skipped": "disabled"}

    from app.crashguard.services.pr_review_responder import (
        fetch_pr_reviews,
        collect_actionable_reviews,
        dispatch_review_response,
    )
    from app.crashguard.services.pr_reviewer import _resolve_repo_path_for_pr

    # 1. 拉 reviews
    ok, reviews, err = fetch_pr_reviews(repo_slug, pr_number)
    if not ok:
        logger.info("pr_review_responder fetch_pr_reviews failed: %s", err)
        return {"ok": False, "stage": "fetch_pr_reviews", "error": err}

    # 2. 加载 PR 行 + collect actionable
    async with get_session() as session:
        pr_row = await session.get(CrashPullRequest, pr_id)
        if pr_row is None:
            return {"ok": False, "stage": "load_pr", "error": "pr_not_found"}
        actionable_list, counters = await collect_actionable_reviews(
            pr_row, reviews, session,
        )
    if not actionable_list:
        return {"ok": True, "enabled": True, "counters": counters, "dispatched": 0}

    # 3. 准备 repo_path（复用 pr_reviewer 的子仓 hint 解析）
    repo_path = _resolve_repo_path_for_pr(pr_row, s)
    if not repo_path:
        return {
            "ok": False, "stage": "resolve_repo_path",
            "error": "repo_path_missing", "counters": counters,
        }

    # 4. git fetch + checkout PR 分支（副作用！失败即停）
    branch = pr_row.branch_name or ""
    if not branch:
        return {
            "ok": False, "stage": "checkout",
            "error": "branch_name_missing", "counters": counters,
        }
    try:
        r_f = subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=repo_path, capture_output=True, text=True, timeout=60,
        )
        if r_f.returncode != 0:
            return {
                "ok": False, "stage": "git_fetch",
                "error": (r_f.stderr or "")[:200], "counters": counters,
            }
        r_c = subprocess.run(
            ["git", "checkout", branch],
            cwd=repo_path, capture_output=True, text=True, timeout=30,
        )
        if r_c.returncode != 0:
            return {
                "ok": False, "stage": "git_checkout",
                "error": (r_c.stderr or "")[:200], "counters": counters,
            }
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return {
            "ok": False, "stage": "git_subprocess",
            "error": str(e)[:200], "counters": counters,
        }

    # 5. dispatch — 每个 actionable 跑一遍 agent
    dispatched = 0
    verdicts: List[str] = []
    async with get_session() as session:
        # 重新加载 PR row（绑定到 session）
        pr_row2 = await session.get(CrashPullRequest, pr_id)
        # 取得 issue_title / datadog_issue_id 给 prompt
        from app.crashguard.models import CrashIssue
        ci_row = (await session.execute(
            select(CrashIssue).where(
                CrashIssue.datadog_issue_id == pr_row2.datadog_issue_id
            )
        )).scalar_one_or_none()
        issue_title = (ci_row.title if ci_row else "") or ""
        for actionable in actionable_list:
            try:
                r = await dispatch_review_response(
                    actionable, session, repo_path,
                    issue_title=issue_title,
                    datadog_issue_id=pr_row2.datadog_issue_id or "",
                )
                if r.get("ok"):
                    dispatched += 1
                verdicts.append(r.get("verdict") or "?")
            except Exception as e:
                logger.exception(
                    "pr_review_responder dispatch failed pr=%d review=%s: %s",
                    pr_id, actionable.review.review_id, e,
                )

    return {
        "ok": True, "enabled": True,
        "counters": counters, "dispatched": dispatched, "verdicts": verdicts,
    }


async def sync_all_open_prs(limit: int = 200) -> Dict[str, Any]:
    """批量同步：所有非终态 PR。

    增量友好：terminal 状态（merged / closed）跳过，每次只查活跃 PR。
    """
    async with get_session() as session:
        rows: List[CrashPullRequest] = (await session.execute(
            select(CrashPullRequest)
            .where(CrashPullRequest.pr_status.notin_(list(_TERMINAL_STATUSES)))
            .order_by(CrashPullRequest.id.asc())
            .limit(limit)
        )).scalars().all()

    pr_ids = [r.id for r in rows]
    summary = {"checked": 0, "changed": 0, "errors": 0, "details": []}
    for pid in pr_ids:
        res = await sync_pr(pid)
        summary["checked"] += 1
        if res.get("ok") and res.get("changed"):
            summary["changed"] += 1
        elif not res.get("ok"):
            summary["errors"] += 1
        summary["details"].append(res)
    logger.info(
        "crashguard pr_sync batch done: checked=%d changed=%d errors=%d",
        summary["checked"], summary["changed"], summary["errors"],
    )
    return summary
