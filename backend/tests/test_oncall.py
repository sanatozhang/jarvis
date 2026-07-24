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


async def test_resolve_duty_week_current_week(client):
    """client fixture 只是为了让 db.get_session() 指向测试内存库(resolve_duty_week
    2026-07-24 起会查排班快照表)；这里没有任何快照行，全部现算兜底，行为与改造前一致。"""
    from app.api.oncall import resolve_duty_week
    groups = [{"members": ["a@x.com"]}, {"members": ["b@x.com"]}]
    # start 2026-06-01, today 2026-06-25 → 24 天 → week 3 → 3%2=1 → b 当周值周
    info = await resolve_duty_week(groups, "2026-06-01", "B@x.com", date(2026, 6, 25))
    assert info is not None
    assert info["group_index"] == 1
    assert info["week_num"] == 3
    assert info["is_current"] is True
    assert info["week_start"] == date(2026, 6, 22)
    assert info["week_end"] == date(2026, 6, 28)
    assert info["partners"] == []


async def test_resolve_duty_week_most_recent_past(client):
    from app.api.oncall import resolve_duty_week
    groups = [{"members": ["a@x.com", "c@x.com"]}, {"members": ["b@x.com"]}]
    # a 在 group 0；today week 3 → a 最近值周是 week 2（2026-06-15）
    info = await resolve_duty_week(groups, "2026-06-01", "a@x.com", date(2026, 6, 25))
    assert info["group_index"] == 0
    assert info["week_num"] == 2
    assert info["is_current"] is False
    assert info["week_start"] == date(2026, 6, 15)
    assert info["partners"] == ["c@x.com"]


async def test_resolve_duty_week_not_member(client):
    from app.api.oncall import resolve_duty_week
    groups = [{"members": ["a@x.com"]}]
    assert await resolve_duty_week(groups, "2026-06-01", "nobody@x.com", date(2026, 6, 25)) is None
    assert await resolve_duty_week([], "2026-06-01", "a@x.com", date(2026, 6, 25)) is None
    assert await resolve_duty_week(groups, "", "a@x.com", date(2026, 6, 25)) is None


from unittest.mock import patch, AsyncMock


# ── 排班快照表：2026-07-24 核心回归 ──────────────────────────────────────────

async def test_adding_group_does_not_change_current_week(client):
    """核心回归：复现线上 bug——新增一个值班组不应该改变本周已经在进行中的值班
    归属。原 bug：7 组时本周是 chance/sanato.zhang（14 周 % 7 == 0），管理员新增
    第 8 组后，本周瞬间变成 jason.shao/victor（14 % 8 == 6），因为"当前组数"是
    实时现算的分母。"""
    import app.db.database as db_mod
    from datetime import date, timedelta

    await db_mod.upsert_user("sanato", feishu_email="sanato@plaud.ai", role="admin")

    today = date.today()
    # 14 周前的 start_date，与线上复现数据同构：14%7=0（g0），14%8=6（g6）
    start = today - timedelta(weeks=14)
    groups_7 = [{"members": [f"g{i}@x.com"]} for i in range(7)]

    resp = await client.put("/api/oncall/schedule", params={"username": "sanato"}, json={
        "groups": groups_7, "start_date": start.isoformat(),
    })
    assert resp.status_code == 200

    before = await client.get("/api/oncall/current")
    assert before.status_code == 200
    before_members = before.json()["members"]
    assert before_members == ["g0@x.com"]  # 14 % 7 == 0，回归基线

    # 新增第 8 组（追加到末尾），start_date 不变——这正是触发线上 bug 的操作
    groups_8 = groups_7 + [{"members": ["g7@x.com"]}]
    resp2 = await client.put("/api/oncall/schedule", params={"username": "sanato"}, json={
        "groups": groups_8, "start_date": start.isoformat(),
    })
    assert resp2.status_code == 200

    after = await client.get("/api/oncall/current")
    assert after.status_code == 200
    # 关键断言：本周归属必须保持不变，不能因为组数变化跳到别的组（旧 bug 会跳到 g6）
    assert after.json()["members"] == before_members == ["g0@x.com"]
    assert after.json()["group_index"] == 0


async def test_future_weeks_regenerated_with_new_group_count(client):
    """本周之后的未来周次，组配置变化后应该按新组数重新生成映射（未来是可以
    改的，只有本周及历史不能变）。"""
    import app.db.database as db_mod
    from datetime import date, timedelta

    await db_mod.upsert_user("sanato", feishu_email="sanato@plaud.ai", role="admin")

    today = date.today()
    start = today - timedelta(weeks=14)
    groups_7 = [{"members": [f"g{i}@x.com"]} for i in range(7)]
    await client.put("/api/oncall/schedule", params={"username": "sanato"}, json={
        "groups": groups_7, "start_date": start.isoformat(),
    })

    groups_8 = groups_7 + [{"members": ["g7@x.com"]}]
    resp = await client.put("/api/oncall/schedule", params={"username": "sanato"}, json={
        "groups": groups_8, "start_date": start.isoformat(),
    })
    assert resp.status_code == 200

    # 下周（week_num=15）按 8 组计算：15 % 8 == 7 → g7
    next_week_start = start + timedelta(weeks=15)
    snap = await db_mod.get_week_assignment(next_week_start)
    assert snap is not None
    assert snap["members"] == ["g7@x.com"]
    assert snap["group_index"] == 7


async def test_already_frozen_week_not_overwritten_by_later_edit(client):
    """已经冻结过的"本周"快照，同一周内再次编辑组配置不应该被覆盖
    （only_if_missing 语义——防止多次编辑互相打架）。"""
    import app.db.database as db_mod
    from datetime import date, timedelta

    await db_mod.upsert_user("sanato", feishu_email="sanato@plaud.ai", role="admin")

    today = date.today()
    start = today - timedelta(weeks=14)
    groups_7 = [{"members": [f"g{i}@x.com"]} for i in range(7)]
    await client.put("/api/oncall/schedule", params={"username": "sanato"}, json={
        "groups": groups_7, "start_date": start.isoformat(),
    })
    current_week_start = start + timedelta(weeks=14)
    frozen = await db_mod.get_week_assignment(current_week_start)
    assert frozen is not None
    assert frozen["members"] == ["g0@x.com"]

    # 同一周内再编辑一次（加第 8、9 组），本周快照不应该变
    groups_9 = groups_7 + [{"members": ["g7@x.com"]}, {"members": ["g8@x.com"]}]
    resp = await client.put("/api/oncall/schedule", params={"username": "sanato"}, json={
        "groups": groups_9, "start_date": start.isoformat(),
    })
    assert resp.status_code == 200

    still_frozen = await db_mod.get_week_assignment(current_week_start)
    assert still_frozen is not None
    assert still_frozen["members"] == ["g0@x.com"]


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
