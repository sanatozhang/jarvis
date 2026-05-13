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
