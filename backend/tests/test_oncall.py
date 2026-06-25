"""Tests for /api/oncall endpoints."""
from tests.conftest import seed_admin, seed_user


async def test_get_current_oncall(client):
    resp = await client.get("/api/oncall/current")
    assert resp.status_code == 200
    assert "members" in resp.json()
    assert "count" in resp.json()


async def test_get_schedule(client):
    resp = await client.get("/api/oncall/schedule")
    assert resp.status_code == 200
    assert "groups" in resp.json()
    assert "total_groups" in resp.json()


async def test_update_schedule_admin(client):
    await seed_admin(client, "sanato")
    resp = await client.put("/api/oncall/schedule", params={"username": "sanato"}, json={
        "groups": [{"members": ["a@test.com", "b@test.com"]}],
        "start_date": "2026-03-01",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_update_schedule_non_admin(client):
    await seed_user(client, "regular")
    resp = await client.put("/api/oncall/schedule", params={"username": "regular"}, json={
        "groups": [{"members": ["a@test.com"]}],
        "start_date": "2026-03-01",
    })
    assert resp.status_code == 403


from datetime import date


def test_resolve_duty_week_current_week():
    from app.api.oncall import resolve_duty_week
    groups = [{"members": ["a@x.com"]}, {"members": ["b@x.com"]}]
    # start 2026-06-01, today 2026-06-25 → 24 天 → week 3 → 3%2=1 → b 当周值周
    info = resolve_duty_week(groups, "2026-06-01", "B@x.com", date(2026, 6, 25))
    assert info is not None
    assert info["group_index"] == 1
    assert info["week_num"] == 3
    assert info["is_current"] is True
    assert info["week_start"] == date(2026, 6, 22)
    assert info["week_end"] == date(2026, 6, 28)
    assert info["partners"] == []


def test_resolve_duty_week_most_recent_past():
    from app.api.oncall import resolve_duty_week
    groups = [{"members": ["a@x.com", "c@x.com"]}, {"members": ["b@x.com"]}]
    # a 在 group 0；today week 3 → a 最近值周是 week 2（2026-06-15）
    info = resolve_duty_week(groups, "2026-06-01", "a@x.com", date(2026, 6, 25))
    assert info["group_index"] == 0
    assert info["week_num"] == 2
    assert info["is_current"] is False
    assert info["week_start"] == date(2026, 6, 15)
    assert info["partners"] == ["c@x.com"]


def test_resolve_duty_week_not_member():
    from app.api.oncall import resolve_duty_week
    groups = [{"members": ["a@x.com"]}]
    assert resolve_duty_week(groups, "2026-06-01", "nobody@x.com", date(2026, 6, 25)) is None
    assert resolve_duty_week([], "2026-06-01", "a@x.com", date(2026, 6, 25)) is None
    assert resolve_duty_week(groups, "", "a@x.com", date(2026, 6, 25)) is None


from unittest.mock import patch, AsyncMock


async def test_my_workload_not_member(client):
    # 未配置排班 → 404
    resp = await client.get("/api/oncall/my-workload", params={"email": "x@x.com"})
    assert resp.status_code == 404


async def test_my_workload_aggregates(client, db_session):
    from tests.conftest import seed_issue, seed_task, seed_analysis
    from datetime import datetime
    import app.db.database as db_mod

    # 排班：单组 a@x.com，start 2026-06-22（本周）
    # seed_admin via direct DB upsert (POST /login requires email for new users)
    await db_mod.upsert_user("sanato", feishu_email="sanato@plaud.ai", role="admin")
    await client.put("/api/oncall/schedule", params={"username": "sanato"}, json={
        "groups": [{"members": ["a@x.com", "b@x.com"]}],
        "start_date": "2026-06-22",
    })

    # 一个窗口内的升级工单（escalated_at 在本周）
    await seed_issue(db_session, issue_id="esc_in", source="feishu", status="done")
    await seed_task(db_session, task_id="t_in", issue_id="esc_in")
    await seed_analysis(db_session, task_id="t_in", issue_id="esc_in", problem_type="蓝牙")
    async with db_session() as s:
        rec = await s.get(db_mod.IssueRecord, "esc_in")
        rec.escalated_at = datetime(2026, 6, 23, 10, 0, 0)
        rec.escalation_status = "in_progress"
        rec.zendesk_id = "#378794"
        await s.commit()

    # mock 飞书工单（一个窗口内、指派给 a@x.com）
    from app.models.schemas import Issue, LogFile, IssueStatus
    fk_issue = Issue(
        record_id="fk1", description="无法连接", assignee_emails=["a@x.com"],
        feishu_link="https://feishu/fk1", created_at_ms=1782172800000,  # 2026-06-23 UTC
        feishu_status=IssueStatus.IN_PROGRESS,
        log_files=[LogFile(name="log.plaud", token="tok", size=123)],
    )

    async def fake_list(status, limit=200, assignee_emails=None):
        return [fk_issue] if status == "in_progress" else []

    with patch("app.services.feishu.FeishuClient.list_issues_by_status", new=AsyncMock(side_effect=fake_list)):
        resp = await client.get("/api/oncall/my-workload", params={"email": "a@x.com"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["duty_week"]["is_current"] is True
    assert data["oncall_partners"] == ["b@x.com"]
    assert data["summary"]["apollo_count"] == 1
    assert data["summary"]["feishu_count"] == 1
    assert data["apollo_tickets"][0]["logs_download_url"] == "/api/local/esc_in/download-logs"
    assert data["apollo_tickets"][0]["zendesk_url"]  # 由 zendesk_id 拼出
    att = data["feishu_tickets"][0]["attachments"][0]
    assert att["download_path"] == "/api/local/fk1/files/log.plaud"
