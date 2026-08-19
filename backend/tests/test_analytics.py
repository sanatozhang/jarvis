"""Tests for /api/analytics endpoints."""
from unittest.mock import patch, AsyncMock

import pytest


async def test_track_event(client):
    resp = await client.post("/api/analytics/track", json={
        "event_type": "page_visit", "username": "testuser", "detail": {"page": "/"},
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_dashboard(client):
    resp = await client.get("/api/analytics/dashboard", params={"days": 7})
    assert resp.status_code == 200
    assert "value_metrics" in resp.json()


async def test_rule_accuracy(client):
    with patch("app.services.rule_accuracy.get_rule_accuracy_stats", new_callable=AsyncMock, return_value={"rules": [], "total": 0}):
        resp = await client.get("/api/analytics/rule-accuracy")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# date_from/date_to window support — backward compat + priority + validation
# ---------------------------------------------------------------------------

WINDOWED_PATHS = [
    "/api/analytics/dashboard",
    "/api/analytics/problem-types",
    "/api/analytics/classification-stats",
    "/api/analytics/engineer-label-accuracy",
    "/api/analytics/fallback-extraction",
    "/api/analytics/escalation-completion",
]


@pytest.mark.parametrize("path", WINDOWED_PATHS)
async def test_days_param_still_works(client, path):
    resp = await client.get(path, params={"days": 7})
    assert resp.status_code == 200


@pytest.mark.parametrize("path", WINDOWED_PATHS)
async def test_explicit_date_range_works(client, path):
    resp = await client.get(path, params={"date_from": "2026-08-01", "date_to": "2026-08-07"})
    assert resp.status_code == 200


@pytest.mark.parametrize("path", WINDOWED_PATHS)
async def test_only_date_from_is_422(client, path):
    resp = await client.get(path, params={"date_from": "2026-08-01"})
    assert resp.status_code == 422


async def test_date_from_after_date_to_is_422(client):
    resp = await client.get("/api/analytics/dashboard", params={"date_from": "2026-08-10", "date_to": "2026-08-03"})
    assert resp.status_code == 422


async def test_explicit_range_takes_priority_over_days(client):
    resp = await client.get("/api/analytics/dashboard", params={"days": 999, "date_from": "2026-08-01", "date_to": "2026-08-07"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["date_from"] == "2026-08-01"
    assert body["date_to"] == "2026-08-07"


async def test_engineer_label_accuracy_includes_end_of_day_record(client, db_session):
    """Regression for the day-boundary unification: a record created at
    23:00 on date_to must still be included (inclusive end-of-day bound)."""
    from datetime import datetime
    from app.db.database import AnalysisRecord, IssueRecord

    async with db_session() as s:
        s.add(IssueRecord(id="i-eod", description="x"))
        s.add(AnalysisRecord(
            task_id="t-eod", issue_id="i-eod",
            created_at=datetime(2026, 8, 7, 23, 0, 0),
            needs_engineer=True, engineer_label_feedback=True,
        ))
        await s.commit()

    resp = await client.get("/api/analytics/engineer-label-accuracy", params={"date_from": "2026-08-01", "date_to": "2026-08-07"})
    assert resp.status_code == 200
    assert resp.json()["labeled_total"] == 1


async def test_fix_effectiveness_empty_db_returns_zeroes_not_error(client):
    resp = await client.get("/api/analytics/fix-effectiveness", params={"date_from": "2026-08-01", "date_to": "2026-08-07"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_count"] == 0
    assert body["cohort_recurrence_rate"] is None
    assert body["recurrence_rate_by_detection"] is None
    assert body["fix_version_fill_rate"] is None
    assert body["top_offenders"] == []


async def test_fix_effectiveness_denominators_and_rates(client, db_session):
    from datetime import datetime
    from app.db.database import IssueRecord, IssueRecurrenceRecord

    in_window = datetime(2026, 8, 5, 10, 0, 0)
    async with db_session() as s:
        # Two issues resolved inside the window; one has a fix_version, one doesn't.
        s.add(IssueRecord(id="r1", description="x", rule_type="bluetooth", status="done",
                           resolved_at=in_window, fix_target="app", fix_version="3.16.0"))
        s.add(IssueRecord(id="r2", description="y", rule_type="bluetooth", status="done",
                           resolved_at=in_window, fix_target="", fix_version=""))
        # Outside the window — must not be counted.
        s.add(IssueRecord(id="r3", description="z", rule_type="bluetooth", status="done",
                           resolved_at=datetime(2026, 9, 1), fix_target="app", fix_version="1.0.0"))
        # A recurrence hit against r1, detected inside the window — inserted
        # directly (not via detect_and_record) so `detected_at` is a fixed,
        # deterministic value rather than the real wall-clock "now".
        s.add(IssueRecurrenceRecord(
            new_issue_id="n1", prior_issue_id="r1", severity="red",
            similarity=1.0, reason_code="version_gte_fix", rule_type="bluetooth",
            fix_target="app", fix_version="3.16.0", compared_version="3.17.0",
            detected_at=datetime(2026, 8, 6, 9, 0, 0),
        ))
        await s.commit()

    resp = await client.get("/api/analytics/fix-effectiveness", params={"date_from": "2026-08-01", "date_to": "2026-08-07"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_count"] == 2  # r1, r2 — NOT r3 (outside window)
    assert body["resolved_with_fix_version"] == 1  # only r1
    assert body["fix_version_fill_rate"] == 50.0
    assert body["red_hits"] == 1
    assert body["recurred_prior_count"] == 1
    assert body["cohort_recurrence_rate"] == 50.0  # 1 of 2 resolved issues has ever recurred
    top = body["top_offenders"][0]
    assert top["prior_issue_id"] == "r1"
    assert top["recurrence_count"] == 1

    rule_type_row = next(r for r in body["by_rule_type"] if r["rule_type"] == "bluetooth")
    assert rule_type_row["resolved"] == 2
    assert rule_type_row["recurred"] == 1


async def test_escalation_completion_empty_db_returns_zeroes_not_error(client):
    resp = await client.get("/api/analytics/escalation-completion", params={"date_from": "2026-08-01", "date_to": "2026-08-07"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_escalated"] == 0
    assert body["resolved"] == 0
    assert body["completion_rate_pct"] == 0.0
    assert body["daily"] == []


async def test_escalation_completion_numerator_denominator(client, db_session):
    from datetime import datetime
    from app.db.database import IssueRecord

    in_window = datetime(2026, 8, 5, 10, 0, 0)
    async with db_session() as s:
        # Escalated + resolved -> counts toward both denominator and numerator.
        s.add(IssueRecord(id="e1", description="x", escalated_at=in_window, escalation_status="resolved"))
        # Escalated, still in progress -> denominator only.
        s.add(IssueRecord(id="e2", description="y", escalated_at=in_window, escalation_status="in_progress"))
        # Never escalated -> must not count at all, even though status='done'.
        s.add(IssueRecord(id="e3", description="z", status="done", escalated_at=None, escalation_status=""))
        # Escalated + resolved but soft-deleted -> excluded.
        s.add(IssueRecord(id="e4", description="w", escalated_at=in_window, escalation_status="resolved", deleted=True))
        # Escalated + resolved but outside the window -> excluded.
        s.add(IssueRecord(id="e5", description="v", escalated_at=datetime(2026, 9, 1), escalation_status="resolved"))
        await s.commit()

    resp = await client.get("/api/analytics/escalation-completion", params={"date_from": "2026-08-01", "date_to": "2026-08-07"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_escalated"] == 2       # e1, e2
    assert body["resolved"] == 1              # e1
    assert body["completion_rate_pct"] == 50.0
    assert body["daily"] == [{"date": "2026-08-05", "total": 2, "resolved": 1, "rate_pct": 50.0}]


async def test_escalation_completion_includes_end_of_day_record(client, db_session):
    from datetime import datetime
    from app.db.database import IssueRecord

    async with db_session() as s:
        s.add(IssueRecord(
            id="e-eod", description="x",
            escalated_at=datetime(2026, 8, 7, 23, 0, 0), escalation_status="resolved",
        ))
        await s.commit()

    resp = await client.get("/api/analytics/escalation-completion", params={"date_from": "2026-08-01", "date_to": "2026-08-07"})
    assert resp.status_code == 200
    assert resp.json()["total_escalated"] == 1


async def test_fallback_extraction_includes_end_of_day_record(client, db_session):
    from datetime import datetime
    from app.db.database import AnalysisRecord, IssueRecord

    async with db_session() as s:
        s.add(IssueRecord(id="i-eod2", description="x"))
        s.add(AnalysisRecord(
            task_id="t-eod2", issue_id="i-eod2",
            created_at=datetime(2026, 8, 7, 23, 0, 0),
        ))
        await s.commit()

    resp = await client.get("/api/analytics/fallback-extraction", params={"date_from": "2026-08-01", "date_to": "2026-08-07"})
    assert resp.status_code == 200
    assert resp.json()["total_analyses"] == 1
