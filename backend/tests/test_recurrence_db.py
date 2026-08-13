"""DB-touching tests for app.services.recurrence: candidate loading, version
resolution priority, detect_and_record idempotency, and detect_and_alert's
three anti-spam layers. Feishu calls are always mocked — no real network,
no risk of spamming the shared production Feishu app (see repo memory: the
backend must never be run locally against real Feishu credentials)."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from app.db import database as db
from app.services.recurrence import _resolve_ticket_versions, detect_and_alert, detect_and_record


async def _make_issue(session_factory, issue_id, **kwargs):
    defaults = dict(
        description="蓝牙连接总是断开重连失败", rule_type="bluetooth", status="pending",
        deleted=False, app_version="", firmware="",
    )
    defaults.update(kwargs)
    async with session_factory() as s:
        s.add(db.IssueRecord(id=issue_id, **defaults))
        await s.commit()


async def _make_analysis(session_factory, issue_id, task_id, log_metadata=None):
    import json
    async with session_factory() as s:
        s.add(db.AnalysisRecord(
            task_id=task_id, issue_id=issue_id,
            log_metadata_json=json.dumps(log_metadata or {}),
        ))
        await s.commit()


# ---------------------------------------------------------------------------
# load_resolved_candidates
# ---------------------------------------------------------------------------

async def test_load_resolved_candidates_excludes_non_done(client, db_session):
    await _make_issue(db_session, "i1", status="pending", resolved_at=None)
    candidates = await db.load_resolved_candidates("bluetooth")
    assert candidates == []


async def test_load_resolved_candidates_excludes_deleted(client, db_session):
    await _make_issue(db_session, "i1", status="done", deleted=True, resolved_at=datetime.utcnow())
    candidates = await db.load_resolved_candidates("bluetooth")
    assert candidates == []


async def test_load_resolved_candidates_filters_by_rule_type(client, db_session):
    await _make_issue(db_session, "i1", status="done", rule_type="cloud-sync", resolved_at=datetime.utcnow())
    candidates = await db.load_resolved_candidates("bluetooth")
    assert candidates == []


async def test_load_resolved_candidates_excludes_before_since(client, db_session):
    old = datetime.utcnow() - timedelta(days=400)
    await _make_issue(db_session, "i1", status="done", resolved_at=old)
    candidates = await db.load_resolved_candidates("bluetooth", since=datetime.utcnow() - timedelta(days=365))
    assert candidates == []


async def test_load_resolved_candidates_respects_limit(client, db_session):
    for i in range(5):
        await _make_issue(db_session, f"i{i}", status="done", resolved_at=datetime.utcnow())
    candidates = await db.load_resolved_candidates("bluetooth", limit=2)
    assert len(candidates) == 2


async def test_load_resolved_candidates_excludes_given_issue_id(client, db_session):
    await _make_issue(db_session, "i1", status="done", resolved_at=datetime.utcnow())
    candidates = await db.load_resolved_candidates("bluetooth", exclude_issue_id="i1")
    assert candidates == []


async def test_load_resolved_candidates_returns_matching(client, db_session):
    now = datetime.utcnow()
    await _make_issue(db_session, "i1", status="done", resolved_at=now,
                       fix_target="app", fix_version="3.16.0", resolve_reason="fixed it")
    candidates = await db.load_resolved_candidates("bluetooth")
    assert len(candidates) == 1
    assert candidates[0]["issue_id"] == "i1"
    assert candidates[0]["fix_version"] == "3.16.0"
    assert candidates[0]["resolve_reason"] == "fixed it"


# ---------------------------------------------------------------------------
# _resolve_ticket_versions priority: log_metadata beats issues.app_version
# ---------------------------------------------------------------------------

async def test_version_priority_log_metadata_over_issue_field(client, db_session):
    await _make_issue(db_session, "i1", app_version="3.10.0", firmware="1.0.0")
    await _make_analysis(db_session, "i1", "t1", log_metadata={"app_version": "3.17.0"})
    app_v, fw, source = await _resolve_ticket_versions("i1")
    assert app_v == "3.17.0"
    assert fw == "1.0.0"
    assert source == "log_metadata"


async def test_version_priority_falls_back_to_issue_field_without_analysis(client, db_session):
    await _make_issue(db_session, "i1", app_version="3.10.0", firmware="1.0.0")
    app_v, fw, source = await _resolve_ticket_versions("i1")
    assert app_v == "3.10.0"
    assert source == "issue_field"


async def test_version_priority_empty_when_nothing_available(client, db_session):
    await _make_issue(db_session, "i1", app_version="", firmware="")
    app_v, fw, source = await _resolve_ticket_versions("i1")
    assert app_v == ""
    assert source == ""


# ---------------------------------------------------------------------------
# detect_and_record — idempotency
# ---------------------------------------------------------------------------

async def test_detect_and_record_is_idempotent(client, db_session):
    now = datetime.utcnow()
    await _make_issue(db_session, "prior1", status="done", resolved_at=now,
                       fix_target="app", fix_version="3.16.0")
    await _make_issue(db_session, "new1", rule_type="bluetooth", app_version="3.17.0")

    hits1 = await detect_and_record("new1")
    hits2 = await detect_and_record("new1")
    assert len(hits1) == 1
    assert len(hits2) == 1

    rows = await db.list_recurrences_for_issues(["new1"])
    assert len(rows["new1"]) == 1  # not duplicated


async def test_detect_and_record_no_rule_type_returns_empty(client, db_session):
    await _make_issue(db_session, "new1", rule_type="")
    hits = await detect_and_record("new1")
    assert hits == []


async def test_detect_and_record_missing_issue_returns_empty(client, db_session):
    hits = await detect_and_record("does-not-exist")
    assert hits == []


# ---------------------------------------------------------------------------
# detect_and_alert — anti-spam layers
# ---------------------------------------------------------------------------

async def test_alert_disabled_by_default_never_calls_feishu(client, db_session):
    now = datetime.utcnow()
    await _make_issue(db_session, "prior1", status="done", resolved_at=now,
                       fix_target="app", fix_version="3.16.0")
    await _make_issue(db_session, "new1", rule_type="bluetooth", app_version="3.17.0")

    with patch("app.services.feishu_cli.notify_oncall", new_callable=AsyncMock) as mock_notify:
        await detect_and_alert("new1")
    mock_notify.assert_not_awaited()


async def test_alert_enabled_sends_once_and_marks_alerted(client, db_session):
    from app.config import get_settings
    get_settings().recurrence.alert_enabled = True

    now = datetime.utcnow()
    await _make_issue(db_session, "prior1", status="done", resolved_at=now,
                       fix_target="app", fix_version="3.16.0")
    await _make_issue(db_session, "new1", rule_type="bluetooth", app_version="3.17.0")

    with patch("app.services.feishu_cli.notify_oncall", new_callable=AsyncMock) as mock_notify:
        await detect_and_alert("new1")
    mock_notify.assert_awaited_once()

    rows = await db.list_recurrences_for_issues(["new1"])
    assert rows["new1"][0]["prior_issue_id"] == "prior1"
    assert await db.is_recurrence_alerted("new1", "prior1") is True


async def test_alert_pair_lifetime_cap_prevents_second_notification(client, db_session):
    from app.config import get_settings
    get_settings().recurrence.alert_enabled = True

    now = datetime.utcnow()
    await _make_issue(db_session, "prior1", status="done", resolved_at=now,
                       fix_target="app", fix_version="3.16.0")
    await _make_issue(db_session, "new1", rule_type="bluetooth", app_version="3.17.0")

    with patch("app.services.feishu_cli.notify_oncall", new_callable=AsyncMock) as mock_notify:
        await detect_and_alert("new1")
        await detect_and_alert("new1")  # re-run (e.g. re-analysis) must not re-notify
    assert mock_notify.await_count == 1


async def test_alert_12h_rate_cap_per_prior_suppresses_excess(client, db_session):
    from app.config import get_settings
    settings = get_settings().recurrence
    settings.alert_enabled = True
    settings.max_alerts_per_prior_12h = 2

    now = datetime.utcnow()
    await _make_issue(db_session, "prior1", status="done", resolved_at=now,
                       fix_target="app", fix_version="3.16.0")
    # 3 distinct new tickets all recurring against the SAME prior issue —
    # only the first 2 should alert, the 3rd is suppressed by the rate cap.
    for i in range(3):
        await _make_issue(db_session, f"new{i}", rule_type="bluetooth", app_version="3.17.0")

    with patch("app.services.feishu_cli.notify_oncall", new_callable=AsyncMock) as mock_notify:
        for i in range(3):
            await detect_and_alert(f"new{i}")
    assert mock_notify.await_count == 2
