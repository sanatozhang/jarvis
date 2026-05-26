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
async def test_single_review_per_tick(session, pr_row, monkeypatch):
    """同 tick 内同 PR 即使有 2 条 actionable review，也只取 1 条派（顺序处理）。"""
    await _override_settings(
        monkeypatch,
        pr_review_response_enabled=True,
        pr_review_response_allowed_authors=["copilot-pull-request-reviewer", "claude"],
    )
    reviews = [
        _make_review("PRR_a", "copilot-pull-request-reviewer", "x" * 200),
        _make_review("PRR_b", "claude", "y" * 200),
    ]
    out, counters = await collect_actionable_reviews(pr_row, reviews, session)
    assert len(out) == 1  # 只取第一条


# ============================================================
# Stage C tests: prompt + response parser + comment formatter
# ============================================================
import json as _json
import os
import tempfile
from pathlib import Path
from app.crashguard.services.pr_review_responder import (
    _build_review_prompt, _read_review_response, _format_response_comment,
)


def test_build_review_prompt_contains_essentials():
    rv = _make_review("PRR_1", "copilot-pull-request-reviewer", "x" * 60)
    a = ActionableReview(
        pr_id=42, pr_url="https://github.com/foo/bar/pull/1",
        repo_slug="foo/bar", pr_number=1, review=rv, iter_count=1,
    )
    p = _build_review_prompt(a, "diff line a\ndiff line b", issue_title="No Network", datadog_issue_id="dd-1")
    # 必须包含三态 verdict + 三档 confidence + 红线
    assert "addressed" in p and "explained" in p and "needs_human_review" in p
    assert "high" in p and "medium" in p and "low" in p
    assert "Gate#13" in p or "版本号" in p
    assert "review_response.json" in p
    # 上下文渲染
    assert "foo/bar" in p
    assert "No Network" in p
    assert "dd-1" in p


def test_read_review_response_addressed_ok():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".crashguard").mkdir()
        Path(tmp, ".crashguard/review_response.json").write_text(_json.dumps({
            "verdict": "addressed",
            "confidence": "high",
            "explanation": "fix done",
            "changed_files": ["lib/foo.dart"],
            "evidence_files": [],
            "reviewer_quote": "q",
        }))
        ok, data, err = _read_review_response(tmp)
        assert ok, err
        assert data["verdict"] == "addressed"


def test_read_review_response_low_must_be_human_review():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".crashguard").mkdir()
        # confidence=low 但 verdict=addressed → reject
        Path(tmp, ".crashguard/review_response.json").write_text(_json.dumps({
            "verdict": "addressed",
            "confidence": "low",
            "explanation": "x",
            "changed_files": ["a.dart"],
        }))
        ok, _, err = _read_review_response(tmp)
        assert not ok
        assert "low" in err


def test_read_review_response_explained_must_have_no_changes():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".crashguard").mkdir()
        Path(tmp, ".crashguard/review_response.json").write_text(_json.dumps({
            "verdict": "explained",
            "confidence": "high",
            "explanation": "x",
            "changed_files": ["a.dart"],  # 违规
        }))
        ok, _, err = _read_review_response(tmp)
        assert not ok
        assert "explained" in err


def test_read_review_response_addressed_must_have_changes():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".crashguard").mkdir()
        Path(tmp, ".crashguard/review_response.json").write_text(_json.dumps({
            "verdict": "addressed",
            "confidence": "high",
            "explanation": "x",
            "changed_files": [],  # 违规
        }))
        ok, _, err = _read_review_response(tmp)
        assert not ok
        assert "empty" in err


def test_read_review_response_missing_file():
    with tempfile.TemporaryDirectory() as tmp:
        ok, _, err = _read_review_response(tmp)
        assert not ok
        assert "missing" in err


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
    sha, err = await prr._commit_and_push_review_fix("/repo", actionable, data)
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
    sha, err = await prr._commit_and_push_review_fix("/repo", actionable, data)
    assert sha == ""
    assert "rebase failed" in err
    # 必须 abort 才安全
    assert any(c[:2] == ("git", "rebase") and "--abort" in c for c in call_log)


def test_format_response_comment_three_verdicts():
    rv = _make_review("PRR_1", "claude", "y" * 100)
    a = ActionableReview(
        pr_id=1, pr_url="https://github.com/x/y/pull/9",
        repo_slug="x/y", pr_number=9, review=rv, iter_count=2,
    )
    # addressed
    c = _format_response_comment(a, {
        "verdict": "addressed", "confidence": "high",
        "explanation": "real bug fixed", "reviewer_quote": "duplicate toast",
        "evidence_files": ["lib/foo.dart:120"],
    }, fix_commit_sha="abc123def0xyz")
    assert "已修复" in c and "abc123def0" in c and "iter 2" in c
    assert "duplicate toast" in c
    # explained
    c2 = _format_response_comment(a, {
        "verdict": "explained", "confidence": "high",
        "explanation": "intended behavior", "reviewer_quote": "vpn",
    })
    assert "不是 bug" in c2
    # needs_human_review
    c3 = _format_response_comment(a, {
        "verdict": "needs_human_review", "confidence": "low",
        "explanation": "ambiguous", "reviewer_quote": "qqq",
    })
    assert "工程师裁决" in c3


# ---------- treatment A: source review quote in body ----------

def test_format_response_comment_prepends_source_review_quote():
    """Treatment A：评论顶部必须带 source review quote，让读者看清回应的是哪条 review。"""
    rv = _make_review(
        "PRR_42", "luffyYH",
        "请检查 line 198 的空指针保护是否完整\n额外提示：还要看 line 201",
    )
    a = ActionableReview(
        pr_id=7, pr_url="https://github.com/x/y/pull/99",
        repo_slug="x/y", pr_number=99, review=rv, iter_count=1,
    )
    c = _format_response_comment(a, {
        "verdict": "addressed", "confidence": "high",
        "explanation": "已修复空指针", "reviewer_quote": "line 198",
    }, fix_commit_sha="deadbeef0123")

    # source quote 必须在 body 最前面（在 robot header 之前）
    assert c.index("回应 @luffyYH") < c.index("crashguard review-responder"), \
        "source review quote 应该在 robot header 之前"

    # @ 提到原 reviewer
    assert "@luffyYH" in c
    # state + 时间戳出现
    assert "COMMENTED" in c
    # 原 review body 摘要（用 markdown quote 形式）
    assert "> 请检查 line 198" in c
    # 第二行也带 quote 前缀
    assert "> 额外提示：还要看 line 201" in c
    # 仍然保留原有 verdict 内容
    assert "已修复" in c and "deadbeef01" in c


def test_format_source_review_quote_truncates_long_body():
    """超过 200 字的 review body 应该截断 + 加省略号。"""
    from app.crashguard.services.pr_review_responder import _format_source_review_quote
    long_body = "a" * 300
    rv = _make_review("PRR_1", "alice", long_body)
    out = _format_source_review_quote(rv)
    assert "..." in out
    # 截断到 ~200 字（quote 前缀 + 200 chars body）
    assert "a" * 300 not in out
    assert "a" * 200 in out


def test_format_source_review_quote_handles_empty_body():
    """空 body 不能炸；显示占位符。"""
    from app.crashguard.services.pr_review_responder import _format_source_review_quote
    rv = _make_review("PRR_1", "bot", "")
    out = _format_source_review_quote(rv)
    assert "@bot" in out
    assert "empty review body" in out


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


def test_fetch_pr_review_comments_skips_replies_and_extracts_fields(monkeypatch):
    """行级 review comment 解析：skip 已是 reply 的（in_reply_to_id 非空），提取 path/line/source_comment_id。"""
    from app.crashguard.services import pr_review_responder as prr
    payload = [
        {"id": 100, "user": {"login": "Copilot"}, "body": "issue 1",
         "created_at": "2026-05-25T10:00:00Z",
         "path": "lib/foo.dart", "line": 198, "original_line": 198,
         "in_reply_to_id": None},
        {"id": 200, "user": {"login": "sanatozhang"}, "body": "Accepted",
         "created_at": "2026-05-25T10:05:00Z",
         "path": "lib/foo.dart", "line": 198,
         "in_reply_to_id": 100},  # ← 这条是 reply，应跳过
        {"id": 300, "user": {"login": "chatgpt-codex-connector"}, "body": "issue 3",
         "created_at": "2026-05-25T10:10:00Z",
         "path": "lib/bar.dart", "original_line": 360,
         "in_reply_to_id": None},
    ]
    import json as _json

    def fake_run(args, **kw):
        return _FakeProc(returncode=0, stdout=_json.dumps(payload))

    monkeypatch.setattr(prr.subprocess, "run", fake_run)
    ok, items, err = prr.fetch_pr_review_comments("o/r", 1)
    assert ok is True
    assert err == ""
    # 应该剩 2 条（id=100, id=300）
    ids = sorted(it.source_comment_id for it in items)
    assert ids == [100, 300]
    by_id = {it.source_comment_id: it for it in items}
    assert by_id[100].path == "lib/foo.dart" and by_id[100].line == 198
    assert by_id[100].review_id == "REST_C_100"
    assert by_id[300].path == "lib/bar.dart" and by_id[300].line == 360
    assert by_id[300].author == "chatgpt-codex-connector"


def test_fetch_pr_review_comments_handles_empty_and_error(monkeypatch):
    """空数组 / gh api 失败 / JSON 损坏 都不能炸。"""
    from app.crashguard.services import pr_review_responder as prr

    # 空数组
    monkeypatch.setattr(prr.subprocess, "run",
                        lambda *a, **kw: _FakeProc(returncode=0, stdout="[]"))
    ok, items, _ = prr.fetch_pr_review_comments("o/r", 1)
    assert ok is True and items == []

    # gh api 失败
    monkeypatch.setattr(prr.subprocess, "run",
                        lambda *a, **kw: _FakeProc(returncode=1, stderr="not found"))
    ok2, items2, err2 = prr.fetch_pr_review_comments("o/r", 1)
    assert ok2 is False and "not found" in err2

    # JSON 损坏
    monkeypatch.setattr(prr.subprocess, "run",
                        lambda *a, **kw: _FakeProc(returncode=0, stdout="{not-json"))
    ok3, items3, err3 = prr.fetch_pr_review_comments("o/r", 1)
    assert ok3 is False and "json" in err3.lower()


def test_review_item_default_source_comment_id_is_none():
    """老 fetch_pr_reviews 走的 PR-level review 没有 source_comment_id（reply 走 fallback）。"""
    from app.crashguard.services.pr_review_responder import ReviewItem
    rv = ReviewItem(
        review_id="PRR_x", author="claude", state="COMMENTED",
        submitted_at=datetime.utcnow(), body="...",
    )
    assert rv.source_comment_id is None
    assert rv.path == ""
    assert rv.line is None
