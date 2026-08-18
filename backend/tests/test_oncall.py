"""Tests for /api/oncall endpoints."""
from unittest.mock import AsyncMock, patch

from tests.conftest import seed_admin, seed_issue, seed_user


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
    改的，只有本周及历史不能变），且必须紧接着冻结周顺延轮转，不能跳变。

    2026-07-27 修复前：下周对绝对周数取模现算（15 % 8 == 7），会从冻结周
    的 g0 直接跳到 g7，跳过 g1~g6——这正是线上复现的 bug（8 组时本周被冻结
    为 group 0，下一周却跳成 group 7）。修复后：下周必须接着冻结周的
    group_index 顺延一位（g0 之后是 g1），不再跳变。"""
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

    # 本周（week_num=14）冻结用的是旧 7 组公式：14 % 7 == 0 → g0
    current_week_start = start + timedelta(weeks=14)
    current_snap = await db_mod.get_week_assignment(current_week_start)
    assert current_snap is not None
    assert current_snap["group_index"] == 0

    # 下周（week_num=15）必须接着冻结周的 g0 顺延到 g1，不能跳到 g7
    next_week_start = start + timedelta(weeks=15)
    snap = await db_mod.get_week_assignment(next_week_start)
    assert snap is not None
    assert snap["members"] == ["g1@x.com"]
    assert snap["group_index"] == 1

    # 再下一周（week_num=16）继续顺延到 g2
    snap2 = await db_mod.get_week_assignment(start + timedelta(weeks=16))
    assert snap2 is not None
    assert snap2["group_index"] == 2


async def test_current_week_falls_back_to_anchor_without_regenerate(client):
    """核心回归：复现线上真实场景——排班快照表里只有冻结周那一行（未来 52 周
    的预生成因为某种原因没有落地，比如本次编辑之外的历史遗留状态），本周
    没有对应快照时，`resolve_week_group` 现算兜底必须接着最近一次冻结的锚点
    顺延，而不是对绝对周数取模。这是 2026-07-27 生产环境实测的 bug：8 组时
    第 14 周被冻结为 group 0，第 15 周（当前周）没有快照，现算兜底
    `15 % 8 == 7` 直接跳到 group 7（本该顺延到 group 1）。"""
    import app.db.database as db_mod
    from datetime import date, timedelta

    start = date.today() - timedelta(weeks=15)
    groups = [{"group_index": i, "members": [f"g{i}@x.com"]} for i in range(8)]

    # 只手工写入第 14 周的冻结快照，不触发 _regenerate_week_assignments
    # 的未来周预生成——模拟"快照表只有一行"的线上实况。
    week14_start = start + timedelta(weeks=14)
    await db_mod.upsert_week_assignment(
        week14_start, week14_start + timedelta(days=6), 0, ["g0@x.com"],
    )

    # 第 15 周（当前周）查不到快照，现算兜底必须续轮到 group 1，不能是 15%8=7
    info = await db_mod.resolve_week_group(15, groups, start)
    assert info["group_index"] == 1
    assert info["members"] == ["g1@x.com"]

    # 更远的第 20 周同样顺延（14 + 6 = 20 → group_index (0+6)%8=6）
    info2 = await db_mod.resolve_week_group(20, groups, start)
    assert info2["group_index"] == 6


async def test_resolve_week_group_pure_historical_gap_falls_back_to_modulo(client):
    """快照表完全为空（本功能上线前的纯历史空洞）时，找不到任何锚点，必须
    退回原来的绝对取模兜底，行为与修复前一致——这个分支不应该被本次修复
    影响。"""
    import app.db.database as db_mod
    from datetime import date, timedelta

    start = date.today() - timedelta(weeks=30)
    groups = [{"group_index": i, "members": [f"g{i}@x.com"]} for i in range(5)]

    info = await db_mod.resolve_week_group(12, groups, start)
    assert info["group_index"] == 12 % 5
    assert info["members"] == [f"g{12 % 5}@x.com"]


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


async def test_sync_from_feishu_requires_admin(client):
    await seed_user(client, "regular")
    resp = await client.post("/api/oncall/sync-from-feishu", params={"username": "regular"})
    assert resp.status_code == 403


async def test_sync_from_feishu_admin_triggers_sync(client, monkeypatch):
    from app.services import oncall_feishu_sync

    calls = []

    async def fake_sync(today=None, dry_run=False):
        calls.append(dry_run)
        return {"skipped": False, "updated": [], "unchanged": []}

    monkeypatch.setattr(oncall_feishu_sync, "sync_oncall_from_feishu", fake_sync)
    await seed_admin(client, "sanato")
    resp = await client.post(
        "/api/oncall/sync-from-feishu", params={"username": "sanato", "dry_run": "false"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"skipped": False, "updated": [], "unchanged": []}
    assert calls == [False]


async def test_sync_from_feishu_defaults_to_dry_run(client, monkeypatch):
    """端点本身不受 ENABLE_ONCALL_FEISHU_SYNC 开关约束——它自己的安全闸是
    dry_run 默认 True,省略这个 query param 绝不能真的写库。"""
    from app.services import oncall_feishu_sync

    calls = []

    async def fake_sync(today=None, dry_run=False):
        calls.append(dry_run)
        return {"skipped": False, "updated": [], "unchanged": []}

    monkeypatch.setattr(oncall_feishu_sync, "sync_oncall_from_feishu", fake_sync)
    await seed_admin(client, "sanato")
    resp = await client.post("/api/oncall/sync-from-feishu", params={"username": "sanato"})
    assert resp.status_code == 200
    assert calls == [True]


async def test_weekly_greeting_requires_admin(client):
    await seed_user(client, "regular")
    resp = await client.post("/api/oncall/weekly-greeting", params={"username": "regular"})
    assert resp.status_code == 403


async def test_weekly_greeting_admin_defaults_to_dry_run(client, monkeypatch):
    from app.services import oncall_weekly_greeting

    calls = []

    async def fake_send(*, today=None, dry_run=False, force=False, to_email="", max_attempts=3, retry_delay_s=300.0):
        calls.append(dict(dry_run=dry_run, force=force, to_email=to_email, max_attempts=max_attempts))
        return {"sent": False, "skipped": False}

    monkeypatch.setattr(oncall_weekly_greeting, "send_weekly_greeting", fake_send)
    await seed_admin(client, "sanato")
    resp = await client.post("/api/oncall/weekly-greeting", params={"username": "sanato"})
    assert resp.status_code == 200
    assert calls == [{"dry_run": True, "force": False, "to_email": "", "max_attempts": 1}]


async def test_weekly_greeting_admin_passes_through_params_and_forces_max_attempts_1(client, monkeypatch):
    """max_attempts=1 是这条端点不该被跳过的一条断言——没有它，一次真实发送
    失败会让这次 HTTP 请求在 retry_delay_s（默认 300s）里被挂住重试。"""
    from app.services import oncall_weekly_greeting

    calls = []

    async def fake_send(*, today=None, dry_run=False, force=False, to_email="", max_attempts=3, retry_delay_s=300.0):
        calls.append(dict(dry_run=dry_run, force=force, to_email=to_email, max_attempts=max_attempts))
        return {"sent": True, "skipped": False}

    monkeypatch.setattr(oncall_weekly_greeting, "send_weekly_greeting", fake_send)
    await seed_admin(client, "sanato")
    resp = await client.post(
        "/api/oncall/weekly-greeting",
        params={"username": "sanato", "dry_run": "false", "force": "true", "to_email": "x@plaud.ai"},
    )
    assert resp.status_code == 200
    assert calls == [{"dry_run": False, "force": True, "to_email": "x@plaud.ai", "max_attempts": 1}]


# ---------------------------------------------------------------------------
# PUT /tickets/{issue_id}/resolve — mark-complete unification (4-entry-point fix).
# Previously this endpoint only resolved the escalation; it never wrote
# issues.status='done', never logged the event, and never synced the Feishu
# bitable confirmation field — the ticket looked unresolved everywhere else
# in the UI. This is the regression test for that bug, now fixed by routing
# through the same update_issue_resolution() call as api/local.py.
# ---------------------------------------------------------------------------

async def test_resolve_ticket_requires_reason(client, db_session):
    from datetime import datetime
    await seed_issue(db_session, "esc1", source="local", escalated_at=datetime.utcnow())
    resp = await client.put("/api/oncall/tickets/esc1/resolve", json={"username": "tester", "reason": ""})
    assert resp.status_code == 400


async def test_resolve_ticket_404_when_not_escalated(client, db_session):
    await seed_issue(db_session, "not_esc", source="local", escalated_at=None)
    resp = await client.put("/api/oncall/tickets/not_esc/resolve", json={"username": "tester", "reason": "fixed"})
    assert resp.status_code == 404


async def test_resolve_ticket_stamps_resolution_and_status_done(client, db_session):
    """Regression: status must become 'done' — this used to silently stay
    whatever it was before (the bug this endpoint is being fixed for)."""
    from datetime import datetime
    from app.db.database import IssueRecord

    await seed_issue(
        db_session, "esc2", source="local", status="analyzing",
        escalated_at=datetime.utcnow(), escalation_chat_id="oc_chat_x", escalation_status="in_progress",
    )

    with patch("app.services.feishu_cli.send_message", new_callable=AsyncMock, return_value=True):
        resp = await client.put(
            "/api/oncall/tickets/esc2/resolve",
            json={"username": "tester", "reason": "已修复并验证", "fix_target": "app", "fix_version": "3.16.0"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["fix_target"] == "app"
    assert body["fix_version"] == "3.16.0"

    async with db_session() as s:
        issue = await s.get(IssueRecord, "esc2")
        assert issue.status == "done"
        assert issue.resolve_reason == "已修复并验证"
        assert issue.fix_target == "app"
        assert issue.fix_version == "3.16.0"
        assert issue.resolved_at is not None
        assert issue.escalation_status == "resolved"


async def test_resolve_ticket_invalid_fix_target_is_400(client, db_session):
    from datetime import datetime
    await seed_issue(db_session, "esc3", source="local", escalated_at=datetime.utcnow())
    resp = await client.put(
        "/api/oncall/tickets/esc3/resolve",
        json={"username": "tester", "reason": "fixed", "fix_target": "bogus"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PUT /feishu-tickets/{record_id}/resolve — best-effort local write
# ---------------------------------------------------------------------------

async def test_resolve_feishu_ticket_requires_reason(client):
    with patch("app.services.feishu.FeishuClient.mark_completed", new_callable=AsyncMock, return_value=True):
        resp = await client.put("/api/oncall/feishu-tickets/rec123/resolve", json={"reason": ""})
    assert resp.status_code == 400


async def test_resolve_feishu_ticket_local_hit_updates_issue(client, db_session):
    """record_id happens to match a local IssueRecord — stamp the same
    structured fields as the other two mark-complete paths."""
    from app.db.database import IssueRecord

    await seed_issue(db_session, "rec_hit", source="feishu", status="analyzing")

    with patch("app.services.feishu.FeishuClient.mark_completed", new_callable=AsyncMock, return_value=True):
        resp = await client.put(
            "/api/oncall/feishu-tickets/rec_hit/resolve",
            json={"username": "tester", "reason": "已在飞书处理完成"},
        )
    assert resp.status_code == 200
    assert resp.json()["local_issue_updated"] is True

    async with db_session() as s:
        issue = await s.get(IssueRecord, "rec_hit")
        assert issue.status == "done"
        assert issue.resolve_reason == "已在飞书处理完成"


async def test_resolve_feishu_ticket_local_miss_still_logs_event(client):
    """record_id has no matching local IssueRecord (handled purely in
    Feishu) — mark_completed still runs, local_issue_updated is False, but
    the audit trail (events table) still gets a row."""
    from app.db import database as db

    with patch("app.services.feishu.FeishuClient.mark_completed", new_callable=AsyncMock, return_value=True) as mock_mc:
        resp = await client.put(
            "/api/oncall/feishu-tickets/no_such_local_issue/resolve",
            json={"username": "tester", "reason": "纯飞书侧处理"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["local_issue_updated"] is False
    mock_mc.assert_awaited_once()

    async with db.get_session() as session:
        from sqlalchemy import select
        stmt = select(db.EventRecord).where(
            db.EventRecord.issue_id == "no_such_local_issue",
            db.EventRecord.event_type == "mark_complete",
        )
        rows = (await session.execute(stmt)).scalars().all()
    assert len(rows) == 1


async def test_resolve_feishu_ticket_feishu_failure_is_500(client):
    with patch("app.services.feishu.FeishuClient.mark_completed", new_callable=AsyncMock, return_value=False):
        resp = await client.put(
            "/api/oncall/feishu-tickets/whatever/resolve",
            json={"username": "tester", "reason": "x"},
        )
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# get_current_oncall_info() — kill the groups[0] fallback branch.
#
# Old behavior: unset/unparseable start_date silently fell back to
# groups[0]["members"] — a plausible-looking but WRONG on-call list, not a
# signal that oncall is unconfigured. Fixed to return {"members": [],
# "group_index": -1} instead so downstream call sites can tell "unknown"
# apart from "group 0 is really on call".
# ---------------------------------------------------------------------------

async def test_get_current_oncall_info_no_start_date_returns_empty_not_group_zero(client):
    """Regression: start_date unset + groups non-empty must NOT fall back to
    groups[0] — it must signal 'unknown' via an empty members list."""
    import app.db.database as db_mod

    await db_mod.save_oncall_groups([["a@x.com", "b@x.com"], ["c@x.com"]])
    # start_date deliberately left unset (no set_oncall_config call at all).

    info = await db_mod.get_current_oncall_info()
    assert info == {"members": [], "group_index": -1}


async def test_get_current_oncall_info_invalid_start_date_returns_empty_not_group_zero(client):
    """Regression: an unparseable start_date must hit the except branch and
    return empty, not groups[0] — same defect, different trigger."""
    import app.db.database as db_mod

    await db_mod.save_oncall_groups([["a@x.com"], ["b@x.com"]])
    await db_mod.set_oncall_config("start_date", "not-a-date")

    info = await db_mod.get_current_oncall_info()
    assert info == {"members": [], "group_index": -1}


async def test_get_current_oncall_info_happy_path_unchanged(client):
    """Guard rail: a valid start_date on a normal week must resolve exactly
    as before this fix — the happy path is untouched by the groups[0] fix."""
    import app.db.database as db_mod
    from datetime import date, timedelta

    today = date.today()
    start = today - timedelta(weeks=9)  # pure historical gap: no snapshot rows exist
    groups = [["a@x.com"], ["b@x.com"]]
    await db_mod.save_oncall_groups(groups)
    await db_mod.set_oncall_config("start_date", start.isoformat())

    info = await db_mod.get_current_oncall_info()

    week_num = (today - start).days // 7
    expected_index = week_num % len(groups)
    assert info["group_index"] == expected_index
    assert info["members"] == groups[expected_index]


# ---------------------------------------------------------------------------
# GET /api/oncall/feishu-tickets — semantic-flip fix.
#
# oncall_only=True + empty resolved oncall list used to fall through to
# "unfiltered" (assignee_emails=[] is falsy inside list_issues_by_status),
# silently returning EVERY ticket instead of none. Fixed via an explicit
# oncall_configured flag that short-circuits before ever calling FeishuClient.
# ---------------------------------------------------------------------------

async def test_feishu_tickets_oncall_only_unconfigured_skips_feishu_call(client):
    """Core regression assertion: when oncall_configured is False, the
    endpoint must not call FeishuClient.list_issues_by_status at all — not
    just happen to return an empty list for some other reason."""
    with patch(
        "app.services.feishu.FeishuClient.list_issues_by_status",
        new_callable=AsyncMock,
        side_effect=AssertionError("FeishuClient must not be called when oncall is unconfigured"),
    ) as mock_call:
        resp = await client.get("/api/oncall/feishu-tickets", params={"oncall_only": "true"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["tickets"] == []
    assert body["count"] == 0
    assert body["oncall_configured"] is False
    mock_call.assert_not_called()


# ---------------------------------------------------------------------------
# escalation_reminder._scan_and_remind() — don't mark a no-op reminder as sent.
#
# When oncall resolves to [], nobody can be reminded this run (group <at>
# block gated on a truthy id map; DM loop iterates zero oncall emails). The
# old code still called mark_escalation_reminded for every candidate, so a
# misconfiguration silently suppressed retries until the next day. Fixed to
# log + notify the admin + return 0 before touching any candidate.
# ---------------------------------------------------------------------------

async def test_scan_and_remind_oncall_unconfigured_notifies_admin_and_skips_marking(client, db_session):
    from datetime import datetime, timedelta
    from app.services import escalation_reminder
    from app.db.database import IssueRecord

    await seed_issue(
        db_session, "stale1", source="local",
        escalated_at=datetime.utcnow() - timedelta(hours=30),
        escalation_status="in_progress", escalation_chat_id="oc_chat_1",
    )
    # oncall config/groups left empty — db.get_current_oncall() resolves to [].

    with patch("app.services.feishu_cli.send_message", new_callable=AsyncMock, return_value=True) as mock_send:
        reminded = await escalation_reminder._scan_and_remind()

    assert reminded == 0
    # Exactly one send_message call total: the admin notification. If the
    # group <at> or per-person DM path had fired, this would be > 1.
    mock_send.assert_awaited_once()
    _, kwargs = mock_send.call_args
    assert kwargs.get("email") == "sanato.zhang@plaud.ai"
    assert "email" in kwargs and "chat_id" not in kwargs

    async with db_session() as s:
        issue = await s.get(IssueRecord, "stale1")
        assert issue.escalation_reminded_at is None


# ---------------------------------------------------------------------------
# create_escalation_group() — confirmation that this call site was already
# safe (Task 1 doesn't change it): escalation_fixed_members fallback + the
# triggering user keep the member list non-empty even when oncall is empty.
# ---------------------------------------------------------------------------

async def test_create_escalation_group_oncall_empty_still_notifies_fixed_members(client):
    from app.services import feishu_cli

    with patch.object(feishu_cli, "_feishu_api", new=AsyncMock(return_value={"data": {"chat_id": "oc_new_chat"}})), \
         patch.object(feishu_cli, "_emails_to_open_ids", new=AsyncMock(return_value=["ou_1", "ou_2"])), \
         patch.object(feishu_cli, "create_chat_link", new=AsyncMock(return_value="https://feishu.link/abc")), \
         patch.object(feishu_cli, "send_message", new=AsyncMock(return_value=True)):
        result = await feishu_cli.create_escalation_group(
            user_email="reporter@plaud.ai",
            issue_id="issue_x",
            description="蓝牙连接问题",
        )

    assert result["chat_id"] == "oc_new_chat"
    assert result["members"]  # non-empty despite oncall_emails == []
    assert "reporter@plaud.ai" in result["members"]
    assert "sanato.zhang@plaud.ai" in result["members"]  # default escalation_fixed_members entry
