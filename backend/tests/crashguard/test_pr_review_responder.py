"""Stage B 单测：review 检测 / 过滤逻辑（不真调 GitHub）。

覆盖：
- URL 解析
- 配置 kill switch
- 白名单过滤
- 短评论过滤
- 已处理去重
- cooldown
- max_iterations
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.crashguard import models as _crashguard_models  # noqa
from app.crashguard.models import CrashPrReviewIteration, CrashPullRequest
from app.crashguard.services.pr_review_responder import (
    ActionableReview,
    ReviewItem,
    _parse_pr_url,
    collect_actionable_reviews,
)
from app.db.database import Base


def _make_review(rid: str, author: str, body: str, when: datetime | None = None) -> ReviewItem:
    return ReviewItem(
        review_id=rid,
        author=author,
        state="COMMENTED",
        submitted_at=when or datetime.utcnow(),
        body=body,
    )


def test_parse_pr_url():
    assert _parse_pr_url("https://github.com/Plaud-AI/plaud-flutter-common/pull/994") == ("Plaud-AI/plaud-flutter-common", 994)
    assert _parse_pr_url("https://github.com/foo/bar/pull/1") == ("foo/bar", 1)
    assert _parse_pr_url("") == ("", 0)
    assert _parse_pr_url("not-a-url") == ("", 0)
    assert _parse_pr_url("https://gitlab.com/foo/bar/pull/1") == ("", 0)  # 非 github


@pytest.fixture
async def session():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Sessionmaker = async_sessionmaker(eng, expire_on_commit=False, class_=AsyncSession)
    async with Sessionmaker() as s:
        yield s
    await eng.dispose()


@pytest.fixture
def pr_row():
    """构造一个 fake CrashPullRequest 不入库直接传给 collect_*。"""
    pr = CrashPullRequest(
        id=42,
        analysis_id=1,
        datadog_issue_id="abc",
        repo="flutter",
        pr_url="https://github.com/Plaud-AI/plaud-flutter-common/pull/994",
        pr_number=994,
    )
    return pr


async def _override_settings(monkeypatch, **overrides):
    """注入临时配置。"""
    from app.crashguard import config as cfg_mod
    cfg_mod.get_crashguard_settings.cache_clear()
    s = cfg_mod.get_crashguard_settings()
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.mark.asyncio
async def test_kill_switch_skips_all(session, pr_row, monkeypatch):
    await _override_settings(monkeypatch, pr_review_response_enabled=False)
    reviews = [_make_review("PRR_1", "copilot-pull-request-reviewer", "x" * 200)]
    out, counters = await collect_actionable_reviews(pr_row, reviews, session)
    assert out == []
    assert counters["kill_switch"] == 1
    assert counters["actionable"] == 0


@pytest.mark.asyncio
async def test_non_whitelist_author_skipped(session, pr_row, monkeypatch):
    await _override_settings(
        monkeypatch,
        pr_review_response_enabled=True,
        pr_review_response_allowed_authors=["copilot-pull-request-reviewer"],
    )
    reviews = [
        _make_review("PRR_1", "random-user", "x" * 200),
        _make_review("PRR_2", "Copilot-Pull-Request-Reviewer", "y" * 200),
    ]
    out, counters = await collect_actionable_reviews(pr_row, reviews, session)
    # 大小写不敏感对比 → Copilot 通过；random-user 被拒
    assert len(out) == 1
    assert out[0].review.review_id == "PRR_2"
    assert counters["non_whitelist_author"] == 1


@pytest.mark.asyncio
async def test_body_too_short_skipped(session, pr_row, monkeypatch):
    await _override_settings(
        monkeypatch,
        pr_review_response_enabled=True,
        pr_review_response_allowed_authors=["copilot-pull-request-reviewer"],
        pr_review_response_min_body_chars=50,
    )
    reviews = [
        _make_review("PRR_1", "copilot-pull-request-reviewer", "LGTM"),  # 4 chars
        _make_review("PRR_2", "copilot-pull-request-reviewer", "x" * 60),
    ]
    out, counters = await collect_actionable_reviews(pr_row, reviews, session)
    assert len(out) == 1
    assert out[0].review.review_id == "PRR_2"
    assert counters["body_too_short"] == 1


@pytest.mark.asyncio
async def test_already_processed_skipped(session, pr_row, monkeypatch):
    await _override_settings(
        monkeypatch,
        pr_review_response_enabled=True,
        pr_review_response_allowed_authors=["copilot-pull-request-reviewer"],
    )
    # 历史插入一条 iteration
    session.add(CrashPrReviewIteration(
        pr_id=42, iter_count=1, review_author="copilot-pull-request-reviewer",
        review_id="PRR_already_done",
        dispatched_at=datetime.utcnow() - timedelta(hours=2),  # 超出 cooldown
        verdict="addressed",
    ))
    await session.commit()
    reviews = [
        _make_review("PRR_already_done", "copilot-pull-request-reviewer", "x" * 200),
        _make_review("PRR_new", "copilot-pull-request-reviewer", "y" * 200),
    ]
    out, counters = await collect_actionable_reviews(pr_row, reviews, session)
    assert len(out) == 1
    assert out[0].review.review_id == "PRR_new"
    assert counters["already_processed"] == 1


@pytest.mark.asyncio
async def test_cooldown_blocks_new_review(session, pr_row, monkeypatch):
    """同 PR 最近 30min 内已派过 → 新 review 也跳过。"""
    await _override_settings(
        monkeypatch,
        pr_review_response_enabled=True,
        pr_review_response_allowed_authors=["copilot-pull-request-reviewer"],
        pr_review_response_cooldown_minutes=30,
    )
    session.add(CrashPrReviewIteration(
        pr_id=42, iter_count=1, review_author="copilot-pull-request-reviewer",
        review_id="PRR_recent",
        dispatched_at=datetime.utcnow() - timedelta(minutes=10),  # cooldown 内
    ))
    await session.commit()
    reviews = [_make_review("PRR_brand_new", "copilot-pull-request-reviewer", "x" * 200)]
    out, counters = await collect_actionable_reviews(pr_row, reviews, session)
    assert len(out) == 0
    assert counters["cooldown"] == 1


@pytest.mark.asyncio
async def test_max_iterations_caps(session, pr_row, monkeypatch):
    """PR 已经派过 3 次 → 第 4 次跳过。"""
    await _override_settings(
        monkeypatch,
        pr_review_response_enabled=True,
        pr_review_response_allowed_authors=["copilot-pull-request-reviewer"],
        pr_review_response_max_iterations=3,
        pr_review_response_cooldown_minutes=30,
    )
    base = datetime.utcnow() - timedelta(hours=10)
    for i in range(3):
        session.add(CrashPrReviewIteration(
            pr_id=42, iter_count=i + 1,
            review_author="copilot-pull-request-reviewer",
            review_id=f"PRR_hist_{i}",
            dispatched_at=base + timedelta(hours=i),
            verdict="addressed",
        ))
    await session.commit()
    reviews = [_make_review("PRR_fourth", "copilot-pull-request-reviewer", "x" * 200)]
    out, counters = await collect_actionable_reviews(pr_row, reviews, session)
    assert len(out) == 0
    assert counters["max_iter"] == 1


@pytest.mark.asyncio
async def test_actionable_review_carries_iter_count(session, pr_row, monkeypatch):
    """新 review 第一次进入 → iter_count = 1。"""
    await _override_settings(
        monkeypatch,
        pr_review_response_enabled=True,
        pr_review_response_allowed_authors=["copilot-pull-request-reviewer"],
    )
    reviews = [_make_review("PRR_first", "copilot-pull-request-reviewer", "x" * 200)]
    out, counters = await collect_actionable_reviews(pr_row, reviews, session)
    assert len(out) == 1
    assert out[0].iter_count == 1
    assert out[0].pr_id == 42
    assert out[0].repo_slug == "Plaud-AI/plaud-flutter-common"
    assert out[0].pr_number == 994
    assert counters["actionable"] == 1


@pytest.mark.asyncio
async def test_all_pending_threads_in_one_round(session, pr_row, monkeypatch):
    """同 PR 多条待处理 thread → 一轮全部带上（一次 agent / 一个 commit）。"""
    await _override_settings(
        monkeypatch,
        pr_review_response_enabled=True,
        pr_review_response_allowed_authors=["copilot-pull-request-reviewer", "claude"],
        pr_review_response_max_threads_per_round=10,
    )
    reviews = [
        _make_review("REST_C_1", "copilot-pull-request-reviewer", "x" * 200),
        _make_review("REST_C_2", "claude", "y" * 200),
    ]
    out, counters = await collect_actionable_reviews(pr_row, reviews, session)
    assert [a.review.review_id for a in out] == ["REST_C_1", "REST_C_2"]
    assert all(a.iter_count == 1 for a in out)


@pytest.mark.asyncio
async def test_round_cap_defers_extra_threads(session, pr_row, monkeypatch):
    await _override_settings(
        monkeypatch,
        pr_review_response_enabled=True,
        pr_review_response_allowed_authors=["claude"],
        pr_review_response_max_threads_per_round=2,
    )
    reviews = [_make_review(f"REST_C_{i}", "claude", "x" * 200) for i in range(3)]
    out, counters = await collect_actionable_reviews(pr_row, reviews, session)
    assert len(out) == 2
    assert counters["over_round_cap"] == 1


@pytest.mark.asyncio
async def test_any_bot_author_accepted(session, pr_row, monkeypatch):
    """github-actions[bot]（Codex action）这类 Bot 账号即使不在白名单也处理；人工账号不处理。"""
    await _override_settings(
        monkeypatch,
        pr_review_response_enabled=True,
        pr_review_response_allowed_authors=[],
        pr_review_response_accept_any_bot=True,
    )
    bot = _make_review("REST_C_1", "github-actions", "x" * 200)
    bot.author_is_bot = True
    human = _make_review("REST_C_2", "some-engineer", "y" * 200)
    out, counters = await collect_actionable_reviews(pr_row, [bot, human], session)
    assert [a.review.review_id for a in out] == ["REST_C_1"]
    assert counters["non_whitelist_author"] == 1


@pytest.mark.asyncio
async def test_allowed_author_matches_bot_suffix(session, pr_row, monkeypatch):
    """白名单写 'claude'，REST 风格 login 'claude[bot]' 也命中。"""
    await _override_settings(
        monkeypatch,
        pr_review_response_enabled=True,
        pr_review_response_allowed_authors=["claude"],
        pr_review_response_accept_any_bot=False,
    )
    out, _ = await collect_actionable_reviews(
        pr_row, [_make_review("REST_C_1", "claude[bot]", "x" * 200)], session,
    )
    assert len(out) == 1


@pytest.mark.asyncio
async def test_human_engaged_or_already_replied_thread_skipped(session, pr_row, monkeypatch):
    await _override_settings(
        monkeypatch,
        pr_review_response_enabled=True,
        pr_review_response_allowed_authors=["claude"],
    )
    a = _make_review("REST_C_1", "claude", "x" * 200)
    a.human_replied = True
    b = _make_review("REST_C_2", "claude", "y" * 200)
    b.responder_replied = True
    out, counters = await collect_actionable_reviews(pr_row, [a, b], session)
    assert out == []
    assert counters["human_engaged"] == 1
    assert counters["already_processed"] == 1


@pytest.mark.asyncio
async def test_failed_thread_retried_next_round(session, pr_row, monkeypatch):
    """上一轮 failed 的 thread 下一轮重试，iter_count 递增。"""
    await _override_settings(
        monkeypatch,
        pr_review_response_enabled=True,
        pr_review_response_allowed_authors=["claude"],
        pr_review_response_cooldown_minutes=20,
    )
    session.add(CrashPrReviewIteration(
        pr_id=42, iter_count=1, review_author="claude", review_id="REST_C_1",
        dispatched_at=datetime.utcnow() - timedelta(hours=1), verdict="failed",
    ))
    await session.commit()
    out, _ = await collect_actionable_reviews(
        pr_row, [_make_review("REST_C_1", "claude", "x" * 200)], session,
    )
    assert len(out) == 1
    assert out[0].iter_count == 2


# ============================================================
# Stage C tests: prompt + response parser + comment formatter
# ============================================================
import json as _json
import tempfile
from pathlib import Path
from app.crashguard.services.pr_review_responder import (
    _build_review_prompt, _read_review_response, _validate_item, _format_response_comment,
)


def _actionable(cid: int = 101, author: str = "claude", iter_count: int = 1) -> ActionableReview:
    rv = _make_review(f"REST_C_{cid}", author, "y" * 100)
    rv.source_comment_id = cid
    rv.thread_id = f"PRRT_{cid}"
    rv.path = "lib/foo.dart"
    rv.line = 12
    return ActionableReview(
        pr_id=42, pr_url="https://github.com/foo/bar/pull/1",
        repo_slug="foo/bar", pr_number=1, review=rv, iter_count=iter_count,
    )


def test_build_review_prompt_lists_every_comment():
    p = _build_review_prompt(
        [_actionable(101), _actionable(102)], "diff line a",
        issue_title="No Network", datadog_issue_id="dd-1",
    )
    assert "comment_id=101" in p and "comment_id=102" in p
    assert "lib/foo.dart:12" in p
    assert "addressed" in p and "explained" in p
    assert "needs_human_review" not in p
    assert "review_response.json" in p
    assert "foo/bar" in p and "dd-1" in p


def _write_resp(tmp: str, obj) -> None:
    Path(tmp, ".crashguard").mkdir()
    Path(tmp, ".crashguard/review_response.json").write_text(_json.dumps(obj))


def test_read_review_response_items_by_comment_id():
    with tempfile.TemporaryDirectory() as tmp:
        _write_resp(tmp, {"items": [
            {"comment_id": 101, "verdict": "Addressed", "confidence": "high",
             "changed_files": ["a.dart"], "explanation": "x"},
            {"comment_id": "102", "verdict": "explained", "confidence": "low",
             "changed_files": [], "explanation": "y"},
        ]})
        ok, items, err = _read_review_response(tmp, [101, 102])
        assert ok, err
        assert set(items) == {101, 102}
        assert items[101]["verdict"] == "addressed"


def test_read_review_response_single_object_compat():
    with tempfile.TemporaryDirectory() as tmp:
        _write_resp(tmp, {"verdict": "explained", "confidence": "high",
                          "changed_files": [], "explanation": "x"})
        ok, items, _ = _read_review_response(tmp, [7])
        assert ok and set(items) == {7}


def test_read_review_response_missing_or_broken():
    with tempfile.TemporaryDirectory() as tmp:
        ok, _, err = _read_review_response(tmp, [1])
        assert not ok and "missing" in err
        Path(tmp, ".crashguard").mkdir()
        Path(tmp, ".crashguard/review_response.json").write_text('{"items": [')
        ok, _, err = _read_review_response(tmp, [1])
        assert not ok and "decode failed" in err


def test_validate_item_rules():
    assert _validate_item({"verdict": "addressed", "confidence": "high", "changed_files": ["a"]}) == ""
    assert _validate_item({"verdict": "explained", "confidence": "low", "changed_files": []}) == ""
    assert "low" in _validate_item({"verdict": "addressed", "confidence": "low", "changed_files": ["a"]})
    assert "non-empty" in _validate_item({"verdict": "explained", "confidence": "high", "changed_files": ["a"]})
    assert "empty" in _validate_item({"verdict": "addressed", "confidence": "high", "changed_files": []})
    assert "invalid verdict" in _validate_item({"verdict": "needs_human_review", "confidence": "low"})


def test_format_response_comment_accept_and_reject():
    a = _actionable(iter_count=2)
    c = _format_response_comment(a, {
        "verdict": "addressed", "confidence": "high",
        "explanation": "real bug fixed", "reviewer_quote": "duplicate toast",
        "evidence_files": ["lib/foo.dart:120"],
    }, fix_commit_sha="abcdef1234567")
    assert "crashguard review-responder" in c
    assert "已采纳" in c and "abcdef1234" in c and "iter 2" in c
    c2 = _format_response_comment(a, {
        "verdict": "explained", "confidence": "medium", "explanation": "intended",
    })
    assert "不采纳" in c2 and "intended" in c2 and "Fix commit" not in c2


# ---------- dispatch_review_round 端到端（mock agent / git / gh） ----------

def _patch_round(monkeypatch, response_obj, push_ok=True):
    from app.crashguard.services import pr_review_responder as prr
    calls = {"replies": [], "resolved": [], "git": []}

    async def fake_agent(repo_path, prompt, timeout_sec=900):
        if response_obj is not None:
            _write_resp(repo_path, response_obj)
        return True, ""

    def fake_git(args, repo_path, timeout=30):
        calls["git"].append(tuple(args))
        if args[:3] == ["git", "diff", "--cached"]:
            return 1, "", ""
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return 0, "f1x5ha0000000", ""
        if args[:2] == ["git", "push"]:
            return (0, "", "") if push_ok else (1, "", "remote rejected")
        return 0, "", ""

    def fake_post(slug, num, body, in_reply_to=None, timeout=30):
        calls["replies"].append((in_reply_to, body))
        return True, ""

    def fake_resolve(thread_id, timeout=30):
        calls["resolved"].append(thread_id)
        return True, ""

    monkeypatch.setattr(prr, "_run_review_agent", fake_agent)
    monkeypatch.setattr(prr, "_run_git", fake_git)
    monkeypatch.setattr(prr, "_post_pr_comment", fake_post)
    monkeypatch.setattr(prr, "resolve_review_thread", fake_resolve)
    return calls


@pytest.mark.asyncio
async def test_dispatch_round_accepts_rejects_and_resolves_all(session, monkeypatch):
    from app.crashguard.services.pr_review_responder import dispatch_review_round
    calls = _patch_round(monkeypatch, {"items": [
        {"comment_id": 101, "verdict": "addressed", "confidence": "high",
         "changed_files": ["lib/foo.dart"], "explanation": "fixed"},
        {"comment_id": 102, "verdict": "explained", "confidence": "medium",
         "changed_files": [], "explanation": "not a bug"},
    ]})
    with tempfile.TemporaryDirectory() as tmp:
        r = await dispatch_review_round([_actionable(101), _actionable(102)], session, tmp)
    assert r["ok"] and sorted(r["verdicts"]) == ["addressed", "explained"]
    assert r["fix_commit_sha"] == "f1x5ha0000000"
    assert [x[0] for x in calls["replies"]] == [101, 102]
    assert "f1x5ha0000" in calls["replies"][0][1]
    assert calls["resolved"] == ["PRRT_101", "PRRT_102"]
    # 只 add 采纳项改的文件
    assert ("git", "add", "--", "lib/foo.dart") in calls["git"]
    rows = (await session.execute(select(CrashPrReviewIteration))).scalars().all()
    assert {(x.review_id, x.verdict) for x in rows} == {
        ("REST_C_101", "addressed"), ("REST_C_102", "explained"),
    }


@pytest.mark.asyncio
async def test_dispatch_round_push_failure_keeps_accepted_thread_open(session, monkeypatch):
    """修复没 push 成功：采纳项不回复、不 resolve，记 failed 下轮重试；拒绝项照常处理。"""
    from app.crashguard.services.pr_review_responder import dispatch_review_round
    calls = _patch_round(monkeypatch, {"items": [
        {"comment_id": 101, "verdict": "addressed", "confidence": "high",
         "changed_files": ["lib/foo.dart"], "explanation": "fixed"},
        {"comment_id": 102, "verdict": "explained", "confidence": "high",
         "changed_files": [], "explanation": "not a bug"},
    ]}, push_ok=False)
    with tempfile.TemporaryDirectory() as tmp:
        r = await dispatch_review_round([_actionable(101), _actionable(102)], session, tmp)
    assert calls["resolved"] == ["PRRT_102"]
    assert [x[0] for x in calls["replies"]] == [102]
    rows = {x.review_id: x for x in (await session.execute(select(CrashPrReviewIteration))).scalars().all()}
    assert rows["REST_C_101"].verdict == "failed"
    assert "push" in rows["REST_C_101"].error
    # 失败也要清理工作区
    assert ("git", "reset", "--hard", "HEAD") in calls["git"]


@pytest.mark.asyncio
async def test_dispatch_round_missing_response_marks_all_failed(session, monkeypatch):
    from app.crashguard.services.pr_review_responder import dispatch_review_round
    calls = _patch_round(monkeypatch, None)
    with tempfile.TemporaryDirectory() as tmp:
        r = await dispatch_review_round([_actionable(101)], session, tmp)
    assert r["ok"] is False
    assert calls["replies"] == [] and calls["resolved"] == []
    # failed 行可被下一轮覆盖重试（UNIQUE(pr_id, review_id) 不冲突）
    calls2 = _patch_round(monkeypatch, {"items": [
        {"comment_id": 101, "verdict": "explained", "confidence": "high",
         "changed_files": [], "explanation": "ok"},
    ]})
    with tempfile.TemporaryDirectory() as tmp:
        await dispatch_review_round([_actionable(101, iter_count=2)], session, tmp)
    rows = (await session.execute(select(CrashPrReviewIteration))).scalars().all()
    assert len(rows) == 1 and rows[0].verdict == "explained" and rows[0].iter_count == 2
    assert calls2["resolved"] == ["PRRT_101"]


# ---------- Task: _commit_and_push_review_fix rebase 兜底 ----------

@pytest.mark.asyncio
async def test_commit_and_push_rebase_fallback_succeeds(monkeypatch):
    """push 失败遇 non-fast-forward → 自动 pull --rebase → 重试 push 成功。"""
    from app.crashguard.services import pr_review_responder as prr

    call_log = []

    def fake_run_git(args, repo_path, timeout=60):
        call_log.append(tuple(args))
        if args[:2] == ["git", "add"]:
            return 0, "", ""
        if args[:3] == ["git", "diff", "--cached"]:
            return 1, "", ""  # 有已暂存改动
        if args[:2] == ["git", "commit"]:
            return 0, "", ""
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return 0, "sha_after_commit", ""
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return 0, "feature/x", ""
        if args[:2] == ["git", "push"]:
            # 第 1 次 push 失败 non-fast-forward；第 2 次成功
            n = sum(1 for c in call_log if c[:2] == ("git", "push"))
            if n == 1:
                return 1, "", "To origin: ! [rejected] non-fast-forward"
            return 0, "", ""
        if args[:3] == ["git", "pull", "--rebase"]:
            return 0, "rebased ok", ""
        return 0, "", ""

    monkeypatch.setattr(prr, "_run_git", fake_run_git)

    rv = ReviewItem(review_id="PRR_1", author="copilot-pull-request-reviewer",
                    state="COMMENTED", submitted_at=datetime.utcnow(),
                    body="y" * 100)
    actionable = ActionableReview(
        pr_id=1, pr_url="https://github.com/x/y/pull/9",
        repo_slug="x/y", pr_number=9, review=rv, iter_count=1,
    )
    data = {
        "verdict": "addressed", "confidence": "high",
        "changed_files": ["a.dart"], "reviewer_quote": "fix this",
        "explanation": "ok",
    }
    sha, err = await prr._commit_and_push_review_fix("/repo", actionable, [data])
    assert err == ""
    # rebase 后 sha 被重读
    assert sha == "sha_after_commit"
    # 确实跑了 pull --rebase
    assert any(c[:3] == ("git", "pull", "--rebase") for c in call_log)
    # 共发生 2 次 push
    assert sum(1 for c in call_log if c[:2] == ("git", "push")) == 2


@pytest.mark.asyncio
async def test_commit_and_push_rebase_conflict_aborts(monkeypatch):
    """push 失败 → pull --rebase 也失败（冲突）→ rebase --abort → 返回 error。"""
    from app.crashguard.services import pr_review_responder as prr

    call_log = []

    def fake_run_git(args, repo_path, timeout=60):
        call_log.append(tuple(args))
        if args[:2] == ["git", "add"]:
            return 0, "", ""
        if args[:3] == ["git", "diff", "--cached"]:
            return 1, "", ""  # 有已暂存改动
        if args[:2] == ["git", "commit"]:
            return 0, "", ""
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return 0, "sha_after_commit", ""
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return 0, "feature/x", ""
        if args[:2] == ["git", "push"]:
            return 1, "", "non-fast-forward"
        if args[:3] == ["git", "pull", "--rebase"]:
            return 1, "", "CONFLICT (content): Merge conflict"
        if args[:2] == ["git", "rebase"]:  # abort
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(prr, "_run_git", fake_run_git)

    rv = ReviewItem(review_id="PRR_2", author="copilot-pull-request-reviewer",
                    state="COMMENTED", submitted_at=datetime.utcnow(),
                    body="y" * 100)
    actionable = ActionableReview(
        pr_id=1, pr_url="https://github.com/x/y/pull/9",
        repo_slug="x/y", pr_number=9, review=rv, iter_count=1,
    )
    data = {
        "verdict": "addressed", "confidence": "high",
        "changed_files": ["a.dart"], "reviewer_quote": "fix this",
        "explanation": "ok",
    }
    sha, err = await prr._commit_and_push_review_fix("/repo", actionable, [data])
    assert sha == ""
    assert "rebase failed" in err
    # 必须 abort 才安全
    assert any(c[:2] == ("git", "rebase") and "--abort" in c for c in call_log)



# ---------- treatment B: reply to review thread via gh api ----------

class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_post_pr_comment_falls_back_to_gh_pr_comment_when_no_reply_target(monkeypatch):
    """in_reply_to=None 时走 gh pr comment 顶层路径（兼容老链路）。"""
    from app.crashguard.services import pr_review_responder as prr
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = list(args)
        return _FakeProc(returncode=0, stdout="https://github.com/o/r/pull/1#issuecomment-1")

    monkeypatch.setattr(prr.subprocess, "run", fake_run)
    ok, out = prr._post_pr_comment("o/r", 1, "hi")
    assert ok is True
    assert captured["args"][:3] == ["gh", "pr", "comment"]
    assert "--body" in captured["args"]


def test_post_pr_comment_uses_gh_api_with_in_reply_to(monkeypatch):
    """in_reply_to 提供时必须走 gh api POST，挂到 thread 下面（治本主路径）。"""
    from app.crashguard.services import pr_review_responder as prr
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = list(args)
        return _FakeProc(returncode=0, stdout='{"id": 999}')

    monkeypatch.setattr(prr.subprocess, "run", fake_run)
    ok, out = prr._post_pr_comment("o/r", 42, "thread reply body",
                                    in_reply_to=3296638067)
    assert ok is True
    a = captured["args"]
    assert a[:4] == ["gh", "api", "-X", "POST"]
    assert "repos/o/r/pulls/42/comments" in a
    # in_reply_to 用 -F（gh 转 number）
    assert "-F" in a
    idx = a.index("-F")
    assert a[idx + 1] == "in_reply_to=3296638067"
    # body 用 -f（string）
    assert "-f" in a


def test_post_pr_comment_in_reply_to_zero_falls_back(monkeypatch):
    """in_reply_to <=0 等价于 None（防御性兜底）。"""
    from app.crashguard.services import pr_review_responder as prr
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = list(args)
        return _FakeProc(returncode=0)

    monkeypatch.setattr(prr.subprocess, "run", fake_run)
    ok, _ = prr._post_pr_comment("o/r", 1, "x", in_reply_to=0)
    assert ok is True
    assert captured["args"][:3] == ["gh", "pr", "comment"]


# ---------- fetch_pr_review_threads / resolve_review_thread ----------

def test_fetch_pr_review_threads_parses_unresolved(monkeypatch):
    from app.crashguard.services import pr_review_responder as prr
    payload = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
        {"id": "PRRT_resolved", "isResolved": True, "path": "a.swift", "line": 1,
         "comments": {"nodes": [{"databaseId": 1, "body": "old", "createdAt": "2026-10-21T00:00:00Z",
                                 "author": {"__typename": "Bot", "login": "github-actions"}}]}},
        {"id": "PRRT_open", "isResolved": False, "path": "b.swift", "line": None, "originalLine": 61,
         "comments": {"nodes": [
             {"databaseId": 2, "body": "bug here", "createdAt": "2026-10-21T00:00:00Z",
              "author": {"__typename": "Bot", "login": "github-actions"}},
             {"databaseId": 3, "body": "I'll look", "createdAt": "2026-10-21T01:00:00Z",
              "author": {"__typename": "User", "login": "dev"}},
         ]}},
        {"id": "PRRT_ours", "isResolved": False, "path": "c.swift", "line": 5,
         "comments": {"nodes": [
             {"databaseId": 4, "body": "race", "createdAt": "2026-10-21T00:00:00Z",
              "author": {"__typename": "Bot", "login": "claude"}},
             {"databaseId": 5, "body": "🤖 **crashguard review-responder** ...", "createdAt": "2026-10-21T01:00:00Z",
              "author": {"__typename": "User", "login": "jarvis-oauth-user"}},
         ]}},
    ]}}}}}

    monkeypatch.setattr(prr.subprocess, "run",
                        lambda args, **kw: _FakeProc(0, _json.dumps(payload)))
    ok, items, err = prr.fetch_pr_review_threads("o/r", 1)
    assert ok, err
    assert [i.thread_id for i in items] == ["PRRT_open", "PRRT_ours"]
    a, b = items
    assert a.review_id == "REST_C_2" and a.source_comment_id == 2
    assert a.author == "github-actions" and a.author_is_bot
    assert a.line == 61 and a.path == "b.swift"
    assert a.human_replied and not a.responder_replied
    assert b.responder_replied and not b.human_replied


def test_fetch_pr_review_threads_error(monkeypatch):
    from app.crashguard.services import pr_review_responder as prr
    monkeypatch.setattr(prr.subprocess, "run",
                        lambda args, **kw: _FakeProc(1, "", "HTTP 401"))
    ok, items, err = prr.fetch_pr_review_threads("o/r", 1)
    assert not ok and items == [] and "401" in err
    ok, _, err = prr.fetch_pr_review_threads("bad-slug", 1)
    assert not ok


def test_resolve_review_thread_calls_mutation(monkeypatch):
    from app.crashguard.services import pr_review_responder as prr
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = list(args)
        return _FakeProc(0, "{}")

    monkeypatch.setattr(prr.subprocess, "run", fake_run)
    ok, err = prr.resolve_review_thread("PRRT_x")
    assert ok and err == ""
    assert "threadId=PRRT_x" in captured["args"]
    assert any("resolveReviewThread" in a for a in captured["args"])
    assert prr.resolve_review_thread("") == (False, "thread_id missing")
