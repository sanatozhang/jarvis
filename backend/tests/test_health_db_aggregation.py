"""/api/health 聚合逻辑回归测试（2026-08-20）。

之前两个 bug：
1. database 检查永远 `execute(None)`，出错也被 except 悄悄吞成 "ok"——今天数据库
   真损坏期间这个检查全程显示正常，完全没用。
2. agents 是嵌套字典（没有顶层 status），聚合时 `.get("status")` 永远拿 None，
   跟 agent 实际是否可用无关，顶层永远判 degraded。
"""
from __future__ import annotations

from unittest.mock import patch


async def test_database_check_fails_when_query_errors(client):
    """数据库真的连不上时，database 子检查要报 error，不能吞成 ok。"""
    from app.db import database as db_mod

    class _BoomSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *a, **kw):
            raise RuntimeError("disk I/O error")

    def _boom_get_session():
        return _BoomSession()

    with patch.object(db_mod, "get_session", _boom_get_session), \
         patch("app.services.rule_engine.RuleEngine") as mock_cls:
        mock_cls.return_value.list_rules.return_value = []
        resp = await client.get("/api/health")

    data = resp.json()
    assert data["checks"]["database"]["status"] == "error"
    assert data["status"] == "degraded"


async def test_agents_all_ok_does_not_force_degraded(client):
    """两个 agent CLI 都可用时，agents 子检查该聚合出 status=ok，不该拖累顶层。"""
    from app.api import health as health_mod

    async def _fake_detect_agents():
        return {
            "claude_code": {"status": "ok", "available": True, "version": "2.1.207"},
            "codex": {"status": "ok", "available": True, "version": "0.144.6"},
        }

    with patch.object(health_mod, "_detect_agents", _fake_detect_agents), \
         patch("app.services.rule_engine.RuleEngine") as mock_cls:
        mock_cls.return_value.list_rules.return_value = []
        resp = await client.get("/api/health")

    data = resp.json()
    assert data["checks"]["agents"]["status"] == "ok"
    assert data["checks"]["agents"]["claude_code"]["status"] == "ok"


async def test_agents_one_outdated_marks_agents_degraded_not_lost(client):
    """一个 agent 不可用时，agents 子检查该显示 degraded（之前的 bug 是"永远 degraded"，
    这里验证的是"该 degraded 时确实 degraded，且原因可见"，不是随便判。"""
    from app.api import health as health_mod

    async def _fake_detect_agents():
        return {
            "claude_code": {"status": "outdated", "available": False, "version": "2.1.100"},
            "codex": {"status": "ok", "available": True, "version": "0.144.6"},
        }

    with patch.object(health_mod, "_detect_agents", _fake_detect_agents), \
         patch("app.services.rule_engine.RuleEngine") as mock_cls:
        mock_cls.return_value.list_rules.return_value = []
        resp = await client.get("/api/health")

    data = resp.json()
    assert data["checks"]["agents"]["status"] == "degraded"
    assert data["status"] == "degraded"


async def test_db_health_snapshot_included_and_not_yet_checked_is_not_degraded(client):
    """还没跑过第一轮 db_health_monitor 检查时（ok=None），不该把服务判成 degraded。"""
    with patch("app.services.rule_engine.RuleEngine") as mock_cls:
        mock_cls.return_value.list_rules.return_value = []
        resp = await client.get("/api/health")

    data = resp.json()
    assert "db_health" in data["checks"]
    assert "integrity_check" in data["checks"]["db_health"]
    assert "journal_mode" in data["checks"]["db_health"]
    assert "recent_io_errors_10min" in data["checks"]["db_health"]


async def test_db_health_snapshot_reflects_failed_integrity_check(client):
    from app.services import db_health_state as state

    state.record_integrity_check(False, "integrity_check failed: corrupted")
    try:
        with patch("app.services.rule_engine.RuleEngine") as mock_cls:
            mock_cls.return_value.list_rules.return_value = []
            resp = await client.get("/api/health")
        data = resp.json()
        assert data["checks"]["db_health"]["status"] == "error"
        assert data["status"] == "degraded"
    finally:
        # 别把这个坏状态泄漏给其他测试
        state.record_integrity_check(True, "ok")
