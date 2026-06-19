"""Tests for /api/local endpoints."""
from tests.conftest import seed_issue, seed_task, seed_analysis


async def test_list_in_progress(client, db_session):
    await seed_issue(db_session, "issue_ip", status="analyzing")
    resp = await client.get("/api/local/in-progress")
    assert resp.status_code == 200
    assert "issues" in resp.json()


async def test_list_completed(client, db_session):
    await seed_issue(db_session, "issue_done", status="done")
    resp = await client.get("/api/local/completed")
    assert resp.status_code == 200
    assert "issues" in resp.json()


async def test_list_failed(client, db_session):
    await seed_issue(db_session, "issue_fail", status="failed")
    resp = await client.get("/api/local/failed")
    assert resp.status_code == 200


async def test_tracking_with_filters(client, db_session):
    await seed_issue(db_session, "issue_track", status="done", platform="APP", source="local")
    resp = await client.get("/api/local/tracking", params={"platform": "APP", "source": "local"})
    assert resp.status_code == 200
    assert "total_pages" in resp.json()


async def test_tracking_pagination(client, db_session):
    for i in range(5):
        await seed_issue(db_session, f"pg_{i}", status="done")
    resp = await client.get("/api/local/tracking", params={"page": 1, "page_size": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["page"] == 1
    assert data["page_size"] == 2


async def test_issue_detail(client, db_session):
    await seed_issue(db_session, "det_1", status="done")
    await seed_task(db_session, "task_det", "det_1")
    await seed_analysis(db_session, "task_det", "det_1")
    resp = await client.get("/api/local/det_1/detail")
    assert resp.status_code == 200


async def test_issue_detail_not_found(client):
    resp = await client.get("/api/local/nonexistent/detail")
    assert resp.status_code == 404


async def test_issue_analyses(client, db_session):
    await seed_issue(db_session, "ana_1")
    await seed_analysis(db_session, "task_a1", "ana_1")
    resp = await client.get("/api/local/ana_1/analyses")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_mark_inaccurate(client, db_session):
    await seed_issue(db_session, "inacc_1", status="done")
    resp = await client.post("/api/local/inacc_1/inaccurate")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_mark_inaccurate_not_found(client):
    resp = await client.post("/api/local/missing/inaccurate")
    assert resp.status_code == 404


async def test_list_inaccurate(client, db_session):
    await seed_issue(db_session, "inacc_list", status="inaccurate")
    resp = await client.get("/api/local/inaccurate")
    assert resp.status_code == 200


async def test_soft_delete(client, db_session):
    await seed_issue(db_session, "del_1", status="done")
    resp = await client.delete("/api/local/del_1")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"


async def test_soft_delete_not_found(client):
    resp = await client.delete("/api/local/no_such")
    assert resp.status_code == 404


async def test_reimport_undeletes_soft_deleted_issue(client, db_session):
    """重新导入/触发已软删的工单 → deleted 必须复位 False，否则看板永久隐藏。

    Repro (102 rec27CyKMwcZ5l)：A 分析工单并导出飞书 → B 删除该工单 → 重新导入飞书工单，
    因同一 record_id 走 upsert existing 分支，旧 deleted=True 未复位 → 看板「找不到工单」。
    """
    from app.db import database as db
    from app.db.database import IssueRecord

    await seed_issue(db_session, "reimport_1", status="done")
    assert await db.soft_delete_issue("reimport_1") is True

    # 重新导入：同 record_id 命中 upsert 的 existing 分支
    await db.upsert_issue({"record_id": "reimport_1", "description": "重新导入"}, status="analyzing")

    async with db_session() as s:
        rec = await s.get(IssueRecord, "reimport_1")
    assert rec is not None
    assert rec.deleted is False, "重新导入后 deleted 应复位为 False，否则工单永久隐藏在看板外"


async def test_mark_complete_resolves_and_notifies_escalation_group(client, db_session, monkeypatch):
    """标记完成：已 escalate 的工单应同时 resolve + 通知飞书群（接线缺口回归测试）。"""
    from datetime import datetime
    import app.services.feishu_cli as feishu_cli
    from app.db.database import IssueRecord

    sent: list[dict] = []

    async def _fake_send_message(chat_id="", email="", text="", markdown=""):
        sent.append({"chat_id": chat_id, "text": text})
        return True

    monkeypatch.setattr(feishu_cli, "send_message", _fake_send_message)

    await seed_issue(
        db_session, "fb_esc_done", source="local", status="done",
        escalated_at=datetime.utcnow(), escalation_chat_id="oc_chat_123",
        escalation_status="in_progress",
    )

    resp = await client.post("/api/local/fb_esc_done/complete", json={"username": "tester"})
    assert resp.status_code == 200
    assert resp.json()["feishu_notified"] is True

    # 群里收到了完成通知
    assert any(m["chat_id"] == "oc_chat_123" for m in sent), f"未通知飞书群: {sent}"

    # escalation_status 落库为 resolved
    async with db_session() as s:
        issue = await s.get(IssueRecord, "fb_esc_done")
        assert issue.escalation_status == "resolved"


async def test_mark_complete_non_escalated_no_notify(client, db_session, monkeypatch):
    """非 escalate 工单：标记完成不应触发群通知，feishu_notified=False。"""
    import app.services.feishu_cli as feishu_cli

    sent: list[dict] = []

    async def _fake_send_message(chat_id="", email="", text="", markdown=""):
        sent.append({"chat_id": chat_id})
        return True

    monkeypatch.setattr(feishu_cli, "send_message", _fake_send_message)

    await seed_issue(db_session, "fb_plain_done", source="local", status="done")

    resp = await client.post("/api/local/fb_plain_done/complete", json={"username": "tester"})
    assert resp.status_code == 200
    assert resp.json()["feishu_notified"] is False
    assert sent == []


# ---- download-logs three-tier fallback ----

def _patch_endpoint_settings(monkeypatch, workspace_dir):
    """Patch the get_settings reference inside app.api.local to use a known workspace."""
    import app.api.local as local_mod
    from types import SimpleNamespace
    fake = SimpleNamespace(storage=SimpleNamespace(workspace_dir=str(workspace_dir)))
    monkeypatch.setattr(local_mod, "get_settings", lambda: fake)


async def test_download_logs_404_when_nothing_exists(client, db_session, tmp_path, monkeypatch):
    _patch_endpoint_settings(monkeypatch, tmp_path)
    await seed_issue(db_session, "dl_none", status="done")
    resp = await client.get("/api/local/dl_none/download-logs")
    assert resp.status_code == 404


async def test_download_logs_serves_decrypted_log(client, db_session, tmp_path, monkeypatch):
    _patch_endpoint_settings(monkeypatch, tmp_path)
    await seed_issue(db_session, "dl_dec", status="done")
    await seed_task(db_session, "task_dl_dec", "dl_dec")
    logs_dir = tmp_path / "task_dl_dec" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "app.log").write_text("hello world")

    resp = await client.get("/api/local/dl_dec/download-logs")
    assert resp.status_code == 200
    assert "app.log" in resp.headers.get("content-disposition", "")
    assert resp.content == b"hello world"


async def test_download_logs_falls_back_to_raw_plaud(client, db_session, tmp_path, monkeypatch):
    """Old tasks have decrypted dirs cleaned; raw/*.plaud is retained — must serve it."""
    _patch_endpoint_settings(monkeypatch, tmp_path)
    await seed_issue(db_session, "dl_raw", status="done")
    await seed_task(db_session, "task_dl_raw", "dl_raw")
    raw_dir = tmp_path / "task_dl_raw" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "log_2026.plaud").write_bytes(b"\x03\xfdV\xff\x7b\xfc\x28kBINARY_PLAUD_BODY")

    resp = await client.get("/api/local/dl_raw/download-logs")
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("application/octet-stream")
    assert "log_2026.plaud" in resp.headers.get("content-disposition", "")
    assert resp.content.startswith(b"\x03\xfdV\xff")


async def test_download_logs_decrypted_takes_priority_over_raw(client, db_session, tmp_path, monkeypatch):
    """If both decrypted and raw exist, prefer decrypted (smaller, ready-to-read)."""
    _patch_endpoint_settings(monkeypatch, tmp_path)
    await seed_issue(db_session, "dl_both", status="done")
    await seed_task(db_session, "task_dl_both", "dl_both")
    (tmp_path / "task_dl_both" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "task_dl_both" / "logs" / "app.log").write_text("decrypted")
    (tmp_path / "task_dl_both" / "raw").mkdir(parents=True, exist_ok=True)
    (tmp_path / "task_dl_both" / "raw" / "src.plaud").write_bytes(b"raw")

    resp = await client.get("/api/local/dl_both/download-logs")
    assert resp.status_code == 200
    assert resp.content == b"decrypted"
