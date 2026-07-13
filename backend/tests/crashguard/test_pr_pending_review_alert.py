"""pr_pending_review_alert 单测：工作日 10:00 积压提醒。"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
async def patched_session(db_engine):
    """复用 conftest db_engine。"""
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)
    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


def _make_settings(monkeypatch, **overrides):
    s = MagicMock()
    s.pr_pending_review_enabled = True
    s.feishu_alert_email = "sanato.zhang@plaud.ai"
    s.pr_reviewer_fallback_email = "sanato.zhang@plaud.ai"
    for k, v in overrides.items():
        setattr(s, k, v)
    monkeypatch.setattr(
        "app.crashguard.config.get_crashguard_settings",
        lambda: s,
    )
    # 让 weekday() 永远=周二（避免测试在周末跑失败）
    monkeypatch.setattr(
        "app.crashguard.services.pr_pending_review_alert._now_local",
        lambda: datetime(2026, 5, 26, 10, 0),
    )
    return s


# ---------- card builder ----------

def test_build_pending_review_card_groups_by_repo_and_sorts_by_age():
    from app.crashguard.services.pr_pending_review_alert import build_pending_review_card
    prs = [
        {"pr_url": "u1", "pr_number": 1, "repo": "flutter", "pr_status": "open",
         "reviewer_emails": ["a@plaud.ai"], "age_days": 1},
        {"pr_url": "u2", "pr_number": 2, "repo": "flutter", "pr_status": "draft",
         "reviewer_emails": ["b@plaud.ai", "c@plaud.ai"], "age_days": 5},
        {"pr_url": "u3", "pr_number": 3, "repo": "ios", "pr_status": "open",
         "reviewer_emails": [], "age_days": 0},
    ]
    card = build_pending_review_card(prs, stats={
        "yesterday_merged": 2, "yesterday_closed": 1, "yesterday_created": 4, "total_pending": 3,
    })
    # 新标题：日报样式（含 merged / 待 merge / pending 数）
    title = card["header"]["title"]["content"]
    assert "日报" in title or "merged" in title.lower()
    assert "+2 merged" in title
    assert "pending 3" in title or "3 pending" in title
    assert card["header"]["template"] in ("blue", "orange", "red", "green")

    # 全文应该包含 PR# 和 link
    import json as _json
    body = _json.dumps(card, ensure_ascii=False)
    assert "#1" in body and "#2" in body and "#3" in body
    assert "u1" in body and "u2" in body and "u3" in body
    # repo 分组：flutter 出现一次（聚合），ios 出现一次
    assert body.count("📦 flutter") == 1
    assert body.count("📦 ios") == 1
    # 未指派的 PR 显示"(未指派)"
    assert "(未指派)" in body


def test_build_pending_review_card_template_escalates_with_pending():
    from app.crashguard.services.pr_pending_review_alert import build_pending_review_card
    few = [{"pr_url": "u", "pr_number": i, "repo": "r", "pr_status": "open",
            "reviewer_emails": [], "age_days": 0} for i in range(3)]
    medium = [{"pr_url": "u", "pr_number": i, "repo": "r", "pr_status": "open",
               "reviewer_emails": [], "age_days": 0} for i in range(7)]
    many = [{"pr_url": "u", "pr_number": i, "repo": "r", "pr_status": "open",
             "reviewer_emails": [], "age_days": 0} for i in range(12)]
    base_stats = {"yesterday_merged": 0, "yesterday_closed": 0, "yesterday_created": 0}
    assert build_pending_review_card(
        few, stats={**base_stats, "total_pending": 3})["header"]["template"] == "blue"
    assert build_pending_review_card(
        medium, stats={**base_stats, "total_pending": 7})["header"]["template"] == "orange"
    assert build_pending_review_card(
        many, stats={**base_stats, "total_pending": 12})["header"]["template"] == "red"
    # green: 今日 merged 多 + pending 少 → 流速好
    assert build_pending_review_card(
        few, stats={"yesterday_merged": 5, "yesterday_closed": 0,
                    "yesterday_created": 0, "total_pending": 3})["header"]["template"] == "green"


def test_build_pending_review_card_includes_today_stats():
    """日报顶部必须含今日 merged/closed/新建 统计行。"""
    from app.crashguard.services.pr_pending_review_alert import build_pending_review_card
    import json as _json
    card = build_pending_review_card(
        prs=[{"pr_url": "u", "pr_number": 100, "repo": "flutter",
              "pr_status": "open", "reviewer_emails": [], "age_days": 0}],
        stats={"yesterday_merged": 3, "yesterday_closed": 1, "yesterday_created": 5, "total_pending": 1},
    )
    body = _json.dumps(card, ensure_ascii=False)
    # 数字必须出现且 label 正确
    assert "merged" in body.lower()
    assert "3" in body  # yesterday_merged
    assert "closed" in body.lower()
    assert "1" in body
    assert "新建" in body or "created" in body.lower()
    assert "5" in body


def test_build_card_renders_approved_section():
    """已 approve 待 merge 必须单独成节，PR 号与链接都要出现，且与积压清单分离。"""
    from app.crashguard.services.pr_pending_review_alert import build_pending_review_card
    import json as _json
    pending = [{"pr_url": "uP", "pr_number": 11, "repo": "flutter",
                "pr_status": "open", "reviewer_emails": [], "age_days": 3}]
    approved = [
        {"pr_url": "uA1", "pr_number": 22, "repo": "flutter",
         "pr_status": "open", "reviewer_emails": [], "age_days": 1},
        {"pr_url": "uA2", "pr_number": 33, "repo": "ios",
         "pr_status": "open", "reviewer_emails": [], "age_days": 2},
    ]
    card = build_pending_review_card(
        pending,
        stats={"yesterday_merged": 0, "yesterday_closed": 0, "yesterday_created": 0,
               "total_pending": 1, "total_approved": 2},
        approved_prs=approved,
    )
    body = _json.dumps(card, ensure_ascii=False)
    # 标题含「待 merge 2」
    title = card["header"]["title"]["content"]
    assert "待 merge 2" in title
    # 顶部 stats 含 approved 行
    assert "已 approve 待 merge" in body
    # approved 清单出现 PR# 与 URL
    assert "#22" in body and "uA1" in body
    assert "#33" in body and "uA2" in body
    # 积压清单也在
    assert "#11" in body and "uP" in body


def test_build_card_shows_empty_sections_with_placeholder():
    """新语义：4 个清单始终渲染，0 条时显示「（昨日无）」/「（暂无...）」占位。"""
    from app.crashguard.services.pr_pending_review_alert import build_pending_review_card
    import json as _json
    card = build_pending_review_card(
        prs=[{"pr_url": "u", "pr_number": 1, "repo": "r", "pr_status": "open",
              "reviewer_emails": [], "age_days": 0}],
        stats={"yesterday_merged": 0, "yesterday_closed": 0, "yesterday_created": 0,
               "total_pending": 1, "total_approved": 0},
    )
    body = _json.dumps(card, ensure_ascii=False)
    # 4 个小节标题必须出现（用户期望对称性）
    assert "昨日 merged" in body
    assert "昨日 closed" in body
    assert "昨日新建" in body
    assert "已 approve 待 merge" in body
    # 空时显示占位
    assert "昨日无" in body
    assert "暂无 approved" in body


def test_yesterday_utc_window_returns_24h_range_ending_before_today():
    """北京"昨日" 应该返回 UTC 24 小时窗口，且 end 必须早于今日北京 0:00。"""
    from datetime import datetime as _dt, timedelta as _td
    from app.crashguard.services.pr_pending_review_alert import (
        _yesterday_utc_window, _now_local,
    )
    start, end = _yesterday_utc_window()
    # 范围正好 24 小时
    assert (end - start).total_seconds() == 86400
    assert start < end
    # end 必须 ≤ 今日北京 0:00 对应的 UTC（= 今日北京 0:00 - 8h）
    today_local_midnight = _now_local().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    today_utc_midnight = today_local_midnight - _td(hours=8)
    assert end <= today_utc_midnight, (
        f"end {end} must not bleed into today, today_utc_midnight={today_utc_midnight}"
    )


# ---------- main entry ----------

@pytest.mark.asyncio
async def test_run_alert_disabled_short_circuits(patched_session, monkeypatch):
    from app.crashguard.services.pr_pending_review_alert import run_pending_review_alert
    _make_settings(monkeypatch, pr_pending_review_enabled=False)
    res = await run_pending_review_alert()
    assert res["sent"] is False
    assert res["skip_reason"] == "disabled"


@pytest.mark.asyncio
async def test_run_alert_no_target_email(patched_session, monkeypatch):
    from app.crashguard.services.pr_pending_review_alert import run_pending_review_alert
    _make_settings(monkeypatch, feishu_alert_email="", pr_reviewer_fallback_email="")
    res = await run_pending_review_alert()
    assert res["sent"] is False
    assert res["skip_reason"] == "no_target_email"


@pytest.mark.asyncio
async def test_run_alert_no_pending_no_activity_skips_send(patched_session, monkeypatch):
    """库里没有等 review 的 PR + 今日也无 merged/closed/created 时不发送、不打扰。"""
    from app.crashguard.services.pr_pending_review_alert import run_pending_review_alert
    _make_settings(monkeypatch)
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.feishu_cli.send_interactive_card", send_mock)
    res = await run_pending_review_alert()
    assert res["pending_count"] == 0
    assert res["sent"] is False
    assert res["skip_reason"] == "no_pending_no_activity"
    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_alert_sends_when_yesterday_has_merge_even_if_no_pending(
    patched_session, monkeypatch
):
    """无 pending 但昨日有 merged → 仍发日报（让管理者看到昨日流速）。"""
    from app.crashguard.services.pr_pending_review_alert import (
        run_pending_review_alert, _yesterday_utc_window,
    )
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    _make_settings(monkeypatch)  # _now_local 已 patch 为 2026-05-26 10:00
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.feishu_cli.send_interactive_card", send_mock)

    # 落在昨日 UTC 窗口的中间点（保证不会跨边界）
    start_utc, end_utc = _yesterday_utc_window()
    mid_utc = start_utc + (end_utc - start_utc) / 2

    async with get_session() as s:
        s.add(CrashPullRequest(
            analysis_id=1, datadog_issue_id="i1", repo="flutter",
            pr_number=999, pr_url="https://example.com/999",
            pr_status="merged",
            merged_at=mid_utc,
            created_at=mid_utc - timedelta(hours=2),
        ))
        await s.commit()

    res = await run_pending_review_alert()
    assert res["pending_count"] == 0
    assert res["yesterday_merged"] >= 1
    assert res["sent"] is True
    send_mock.assert_called_once()


@pytest.mark.asyncio
async def test_run_alert_sends_with_pending_prs(patched_session, monkeypatch):
    """有等 review 的 PR 时发送卡片，merged/closed/已 reviewed 的不计入。"""
    from app.crashguard.services.pr_pending_review_alert import run_pending_review_alert
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    _make_settings(monkeypatch)
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.feishu_cli.send_interactive_card", send_mock)

    async with get_session() as s:
        # 等 review（应该被列出）
        s.add(CrashPullRequest(
            analysis_id=1, datadog_issue_id="i1", repo="flutter",
            pr_number=100, pr_url="https://example.com/100",
            pr_status="open", reviewer_emails='["alice@plaud.ai"]',
            created_at=datetime.utcnow() - timedelta(days=2),
        ))
        s.add(CrashPullRequest(
            analysis_id=2, datadog_issue_id="i2", repo="ios",
            pr_number=200, pr_url="https://example.com/200",
            pr_status="open", reviewer_emails='[]',
            created_at=datetime.utcnow(),
        ))
        # 已合入（应不在 pending 清单；也不在"昨日 merged" — merged_at 是真实当下，不在 patched 昨日窗口）
        s.add(CrashPullRequest(
            analysis_id=3, datadog_issue_id="i3", repo="flutter",
            pr_number=300, pr_url="https://example.com/300",
            pr_status="merged", merged_at=datetime.utcnow(),
            created_at=datetime.utcnow() - timedelta(days=30),
        ))
        # 已关闭（同上推理）
        s.add(CrashPullRequest(
            analysis_id=4, datadog_issue_id="i4", repo="ios",
            pr_number=400, pr_url="https://example.com/400",
            pr_status="closed", closed_at=datetime.utcnow(),
            created_at=datetime.utcnow() - timedelta(days=30),
        ))
        # 已被 review（pending 清单不收；review_decision 空也不收 approved）
        s.add(CrashPullRequest(
            analysis_id=5, datadog_issue_id="i5", repo="flutter",
            pr_number=500, pr_url="https://example.com/500",
            pr_status="open", reviewed_at=datetime.utcnow(),
            created_at=datetime.utcnow() - timedelta(days=30),
        ))
        await s.commit()

    res = await run_pending_review_alert()
    assert res["pending_count"] == 2, f"应仅列出 #100 和 #200，结果 {res}"
    assert res["sent"] is True
    send_mock.assert_called_once()

    # 校验卡片内容
    call_kwargs = send_mock.call_args.kwargs
    assert call_kwargs.get("email") == "sanato.zhang@plaud.ai"
    import json as _json
    card_body = _json.dumps(call_kwargs.get("card"), ensure_ascii=False)
    assert "#100" in card_body
    assert "#200" in card_body
    # 已合入/已关闭/已 review 的不能出现
    assert "#300" not in card_body
    assert "#400" not in card_body
    assert "#500" not in card_body


@pytest.mark.asyncio
async def test_run_alert_lists_yesterday_merged_closed_created(patched_session, monkeypatch):
    """昨日 merged / closed / 新建 PR 必须各自成节，PR# 与 URL 出现在卡片里。"""
    from app.crashguard.services.pr_pending_review_alert import (
        run_pending_review_alert, _yesterday_utc_window,
    )
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    _make_settings(monkeypatch)  # _now_local = 2026-05-26 10:00
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.feishu_cli.send_interactive_card", send_mock)

    start_utc, end_utc = _yesterday_utc_window()
    mid_utc = start_utc + (end_utc - start_utc) / 2  # 昨日窗口中点

    async with get_session() as s:
        # 昨日 merged
        s.add(CrashPullRequest(
            analysis_id=20, datadog_issue_id="ya", repo="flutter",
            pr_number=2001, pr_url="https://example.com/2001",
            pr_status="merged", merged_at=mid_utc,
            created_at=mid_utc - timedelta(hours=2),
        ))
        # 昨日 closed (未合)
        s.add(CrashPullRequest(
            analysis_id=21, datadog_issue_id="yb", repo="ios",
            pr_number=2002, pr_url="https://example.com/2002",
            pr_status="closed", closed_at=mid_utc,
            created_at=mid_utc - timedelta(hours=3),
        ))
        # 昨日新建（仍 open，无 merged/closed/reviewed）
        s.add(CrashPullRequest(
            analysis_id=22, datadog_issue_id="yc", repo="flutter",
            pr_number=2003, pr_url="https://example.com/2003",
            pr_status="open",
            created_at=mid_utc,
        ))
        await s.commit()

    res = await run_pending_review_alert()
    assert res["yesterday_merged"] == 1
    assert res["yesterday_closed"] == 1
    # 三个 PR 的 created_at 都落在昨日窗口
    assert res["yesterday_created"] == 3
    assert res["sent"] is True

    import json as _json
    body = _json.dumps(send_mock.call_args.kwargs["card"], ensure_ascii=False)
    # 三个 PR 号都要出现 + URL 都要出现
    for n in (2001, 2002, 2003):
        assert f"#{n}" in body, f"missing #{n}"
        assert f"https://example.com/{n}" in body, f"missing URL for #{n}"
    # 小节标题
    assert "昨日 merged" in body
    assert "昨日 closed" in body
    assert "昨日新建" in body


@pytest.mark.asyncio
async def test_run_alert_lists_approved_prs_separately(patched_session, monkeypatch):
    """review_decision='APPROVED' 且未 merge 的 PR 应进 approved 清单，不进 pending。"""
    from app.crashguard.services.pr_pending_review_alert import run_pending_review_alert
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    _make_settings(monkeypatch)
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.feishu_cli.send_interactive_card", send_mock)

    async with get_session() as s:
        # approved 但未 merge —— 必须进 approved 清单
        # created_at 远早于 patched 昨日窗口，避免误进"昨日新建"清单
        s.add(CrashPullRequest(
            analysis_id=10, datadog_issue_id="ia", repo="flutter",
            pr_number=777, pr_url="https://example.com/777",
            pr_status="open",
            reviewed_at=datetime.utcnow(),
            review_decision="APPROVED",
            created_at=datetime.utcnow() - timedelta(days=30),
        ))
        # pending —— reviewed_at IS NULL，review_decision 也未填
        s.add(CrashPullRequest(
            analysis_id=11, datadog_issue_id="ib", repo="ios",
            pr_number=888, pr_url="https://example.com/888",
            pr_status="open",
            review_decision="",
            created_at=datetime.utcnow() - timedelta(days=30),
        ))
        # changes_requested —— reviewed_at 已写但 decision 不是 APPROVED → 都不进
        s.add(CrashPullRequest(
            analysis_id=12, datadog_issue_id="ic", repo="flutter",
            pr_number=999, pr_url="https://example.com/999",
            pr_status="open",
            reviewed_at=datetime.utcnow(),
            review_decision="CHANGES_REQUESTED",
            created_at=datetime.utcnow() - timedelta(days=30),
        ))
        await s.commit()

    res = await run_pending_review_alert()
    assert res["pending_count"] == 1, f"only #888 should be pending: {res}"
    assert res["approved_count"] == 1, f"only #777 should be approved: {res}"
    assert res["sent"] is True
    send_mock.assert_called_once()

    import json as _json
    body = _json.dumps(send_mock.call_args.kwargs["card"], ensure_ascii=False)
    assert "#777" in body  # approved 出现
    assert "#888" in body  # pending 出现
    # changes_requested 既不在 pending 也不在 approved
    assert "#999" not in body


@pytest.mark.asyncio
async def test_run_pending_review_alert_tags_generation(monkeypatch, patched_session):
    """待审核 PR 列表里每条应该带 generation 字段（反查 CrashIssue.service 分类）。"""
    from app.crashguard.models import CrashIssue, CrashPullRequest
    from app.crashguard.services.pr_pending_review_alert import (
        _build_generation_lookup,
        run_pending_review_alert,
    )

    _make_settings(monkeypatch)
    monkeypatch.setattr(
        "app.services.feishu_cli.send_interactive_card",
        AsyncMock(return_value=True),
    )

    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="native-1", platform="ANDROID", service="plaud_android",
            last_seen_version="4.0.100", title="native crash", stack_fingerprint="fp1",
        ))
        session.add(CrashIssue(
            datadog_issue_id="flutter-1", platform="ANDROID", service="plaud-flutter",
            last_seen_version="3.20.0", title="flutter crash", stack_fingerprint="fp2",
        ))
        session.add(CrashPullRequest(
            analysis_id=1, datadog_issue_id="native-1", repo="plaud-native-android",
            pr_url="https://github.com/x/y/pull/1", pr_number=1, pr_status="draft",
        ))
        session.add(CrashPullRequest(
            analysis_id=2, datadog_issue_id="flutter-1", repo="plaud-android",
            pr_url="https://github.com/x/y/pull/2", pr_number=2, pr_status="draft",
        ))
        await session.commit()

    result = await run_pending_review_alert()
    assert result["sent"] is True
    assert result["pending_count"] == 2

    from sqlalchemy import select as _select
    async with patched_session() as session:
        stmt = _select(CrashPullRequest)
        rows = (await session.execute(stmt)).scalars().all()
        gen_map = await _build_generation_lookup(session, [r.datadog_issue_id for r in rows])
    assert gen_map["native-1"] == "native"
    assert gen_map["flutter-1"] == "flutter"


def test_build_pending_review_card_sorts_native_first_and_shows_badge():
    """当前积压清单里 native (4.0) PR 应排在 flutter (3.x) 前面，且带 🆕4.0 角标。"""
    from app.crashguard.services.pr_pending_review_alert import build_pending_review_card

    prs = [
        {"pr_url": "u1", "pr_number": 1, "repo": "same-repo", "pr_status": "draft",
         "reviewer_emails": [], "age_days": 1, "generation": "flutter"},
        {"pr_url": "u2", "pr_number": 2, "repo": "same-repo", "pr_status": "draft",
         "reviewer_emails": [], "age_days": 0, "generation": "native"},
    ]
    card = build_pending_review_card(prs, stats={})
    # 找到「当前积压」清单区块，确认 native (#2) 的行在 flutter (#1) 之前
    all_text = "\n".join(
        el.get("text", {}).get("content", "")
        for el in card["elements"] if el.get("tag") == "div"
    )
    idx_native = all_text.find("#2")
    idx_flutter = all_text.find("#1")
    assert idx_native != -1 and idx_flutter != -1
    assert idx_native < idx_flutter
    assert "🆕4.0" in all_text


@pytest.mark.asyncio
async def test_run_alert_send_failure_returns_reason(patched_session, monkeypatch):
    from app.crashguard.services.pr_pending_review_alert import run_pending_review_alert
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    _make_settings(monkeypatch)
    monkeypatch.setattr(
        "app.services.feishu_cli.send_interactive_card",
        AsyncMock(return_value=False),
    )

    async with get_session() as s:
        s.add(CrashPullRequest(
            analysis_id=1, datadog_issue_id="i1", repo="flutter",
            pr_number=100, pr_url="https://example.com/100",
            pr_status="open", reviewer_emails='["alice@plaud.ai"]',
            created_at=datetime.utcnow(),
        ))
        await s.commit()

    res = await run_pending_review_alert()
    assert res["pending_count"] == 1
    assert res["sent"] is False
    assert res["skip_reason"] == "send_failed"


@pytest.mark.asyncio
async def test_run_pending_review_alert_hides_flutter_when_flutter_family_paused(
    monkeypatch, patched_session,
):
    """2026-07-13：pr_enabled_flutter=False 时日报只保留 native(4.0) 条目，
    不重新计入 flutter 的 pending/stats——用户明确要求"后续只需要给我 4.0 的 pr"。"""
    from app.crashguard.models import CrashIssue, CrashPullRequest
    from app.crashguard.services.pr_pending_review_alert import run_pending_review_alert

    _make_settings(monkeypatch, pr_enabled_flutter=False, pr_enabled_native=True)
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.feishu_cli.send_interactive_card", send_mock)

    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="native-2", platform="ANDROID", service="plaud_android",
            last_seen_version="4.0.100", title="native crash", stack_fingerprint="fpn2",
        ))
        session.add(CrashIssue(
            datadog_issue_id="flutter-2", platform="ANDROID", service="plaud-flutter",
            last_seen_version="3.20.0", title="flutter crash", stack_fingerprint="fpf2",
        ))
        session.add(CrashPullRequest(
            analysis_id=3, datadog_issue_id="native-2", repo="plaud-native-android",
            pr_url="https://github.com/x/y/pull/3", pr_number=3, pr_status="draft",
        ))
        session.add(CrashPullRequest(
            analysis_id=4, datadog_issue_id="flutter-2", repo="plaud-android",
            pr_url="https://github.com/x/y/pull/4", pr_number=4, pr_status="draft",
        ))
        await session.commit()

    result = await run_pending_review_alert()
    assert result["sent"] is True
    assert result["pending_count"] == 1

    body = str(send_mock.call_args.kwargs["card"])
    assert "3" in body  # native PR #3 present
    assert "pull/4" not in body  # flutter PR #4 excluded
