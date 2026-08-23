"""Shared test fixtures for Jarvis backend API tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.database import Base


@pytest.fixture()
async def db_engine():
    """Create a fresh in-memory SQLite engine per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture()
async def db_session(db_engine):
    """Create a session factory bound to the test engine."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    yield factory


@pytest.fixture()
async def client(db_engine, db_session):
    """HTTP client with test DB injected into the app."""
    import app.db.database as db_mod

    # Patch the module-level engine and session factory
    original_engine = db_mod._engine
    original_factory = db_mod._session_factory
    db_mod._engine = db_engine
    db_mod._session_factory = db_session

    # Clear the lru_cache on get_settings so our test settings take effect
    from app.config import get_settings
    get_settings.cache_clear()

    # Patch get_settings to avoid reading real config/env
    # 注意：还得 patch 已 import 模块里的 local 名（from app.config import get_settings 是早绑定，
    # 当 crashguard 测试先跑，它已 import linear_webhook 等模块时，那些模块的 get_settings
    # 已绑定到原函数；只 patch app.config.get_settings 不够，必须同步 patch 各 api 模块的 local 名）。
    with patch("app.config.get_settings") as mock_settings:
        settings = _make_test_settings()
        mock_settings.return_value = settings

        # 同步 patch 各模块里的 get_settings local 名（防早绑定测试污染）。
        #
        # 2026-08-23 事故：这份列表原来只有 app.api.* + app.main，漏了
        # app.services.feishu_cli——它自己 `from app.config import get_settings`
        # 早绑定，这个 patch 碰不到它，导致它内部拿到的还是本机 .env 里**真实的
        # 生产飞书凭证**。`tests/test_deep_analysis.py` 里一个故意构造失败结果
        # 的测试（test_system_failure_triggers_auto_deep_analysis_once）没有
        # mock 掉 `notify_analysis_failure`，于是每次跑这个测试都会用真实凭证
        # 给 ANALYSIS_FAILURE_ALERT_EMAILS 兜底收件人（sanato.zhang@plaud.ai）
        # 发一条真实飞书消息。同一类"早绑定 get_settings + 有真实外部副作用"的
        # 模块不止这一个——linear.py/zendesk.py 同样能在测试里真的建 Linear
        # issue / 真的回 Zendesk 工单，一并补进来。
        api_modules_with_local_get_settings = (
            "app.api.linear_webhook",
            "app.api.feedback",
            "app.api.issues",
            "app.api.tasks",
            "app.api.rules",
            "app.api.reports",
            "app.api.oncall",
            "app.api.users",
            "app.api.analytics",
            "app.api.local",
            "app.api.auth",
            "app.api.site_feedback",
            "app.main",
            "app.services.feishu_cli",
            "app.services.linear",
            "app.services.zendesk",
            "app.services.oncall_weekly_greeting",
            "app.services.oncall_feishu_sync",
            "app.services.voc_digest",
            "app.services.voc_client",
            "app.services.voc_classifier",
            "app.services.agent_orchestrator",
        )
        local_patches = []
        import sys
        for mod_name in api_modules_with_local_get_settings:
            if mod_name in sys.modules and hasattr(sys.modules[mod_name], "get_settings"):
                p = patch(f"{mod_name}.get_settings", return_value=settings)
                local_patches.append(p)
                p.start()
        try:
            # Patch init_db to no-op (we already created tables)
            with patch("app.db.database.init_db", new_callable=AsyncMock):
                # Import app AFTER patching
                from app.main import app

                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    yield ac
        finally:
            for p in local_patches:
                p.stop()

    db_mod._engine = original_engine
    db_mod._session_factory = original_factory
    get_settings.cache_clear()


def _make_test_settings():
    """Build a minimal Settings object for tests."""
    from app.config import (
        Settings, FeishuSettings, LinearSettings,
        AgentSettings, AgentProviderConfig, ConcurrencySettings, StorageSettings,
    )
    import tempfile, os
    tmp = tempfile.mkdtemp()
    s = Settings(
        redis_url="redis://localhost:6379/0",
        database_url="sqlite+aiosqlite:///:memory:",
        host="127.0.0.1",
        port=8000,
        log_level="warning",
    )
    s.feishu = FeishuSettings(app_id="test", app_secret="test")
    s.linear = LinearSettings(api_key="test", webhook_secret="test-secret", trigger_keyword="@ai-agent")
    s.agent = AgentSettings(
        default="codex", timeout=10, max_turns=5,
        providers={"codex": AgentProviderConfig(enabled=True, model="test")},
        routing={},
    )
    s.concurrency = ConcurrencySettings(max_workers=1, max_agent_sessions=1, max_downloads=1, task_timeout=30)
    s.storage = StorageSettings(workspace_dir=os.path.join(tmp, "workspaces"), data_dir=os.path.join(tmp, "data"))
    os.makedirs(s.storage.workspace_dir, exist_ok=True)
    os.makedirs(s.storage.data_dir, exist_ok=True)
    from app.config import SSOSettings
    s.sso = SSOSettings()
    s.sso.enabled = False                 # default OFF, individual tests opt-in via monkeypatch
    s.sso.jwt_secret = "x" * 64
    s.sso.feishu_app_id = "cli_test"
    s.sso.feishu_app_secret = "test-app-secret"
    s.sso.admin_emails_raw = ""
    s.sso.allowed_domains_raw = "plaud.ai"
    s.sso.exempt_paths_raw = "/api/health,/api/linear/webhook,/api/v1/,/api/auth/"
    return s


# ---- Seed data helpers ----

async def seed_user(client: AsyncClient, username: str = "testuser") -> dict:
    """Create a user via API."""
    resp = await client.post(
        "/api/users/login", json={"username": username, "email": f"{username}@plaud.ai"},
    )
    return resp.json()


async def seed_admin(client: AsyncClient, username: str = "sanato") -> dict:
    """Create an admin user (first user 'sanato' is auto-admin)."""
    resp = await client.post(
        "/api/users/login", json={"username": username, "email": f"{username}@plaud.ai"},
    )
    return resp.json()


async def seed_issue(db_session, issue_id: str = "test_issue_1", **kwargs):
    """Insert an issue directly into DB."""
    from app.db.database import IssueRecord
    defaults = dict(
        id=issue_id, description="蓝牙连接断开", device_sn="SN123",
        firmware="1.0.0", app_version="2.0.0", priority="L",
        source="local", status="done", platform="APP", category="蓝牙",
        created_by="testuser",
    )
    defaults.update(kwargs)
    async with db_session() as s:
        s.add(IssueRecord(**defaults))
        await s.commit()


async def seed_task(db_session, task_id: str = "task_001", issue_id: str = "test_issue_1", **kwargs):
    """Insert a task directly into DB."""
    from app.db.database import TaskRecord
    defaults = dict(id=task_id, issue_id=issue_id, status="done", progress=100, message="完成")
    defaults.update(kwargs)
    async with db_session() as s:
        s.add(TaskRecord(**defaults))
        await s.commit()


async def seed_analysis(db_session, task_id: str = "task_001", issue_id: str = "test_issue_1", **kwargs):
    """Insert an analysis directly into DB."""
    import json
    from app.db.database import AnalysisRecord
    defaults = dict(
        task_id=task_id, issue_id=issue_id,
        problem_type="蓝牙连接", root_cause="BLE 断连",
        confidence="high", key_evidence_json=json.dumps(["log line 1"]),
        user_reply="建议重新配对", needs_engineer=False,
        rule_type="bluetooth", agent_type="codex",
    )
    defaults.update(kwargs)
    async with db_session() as s:
        s.add(AnalysisRecord(**defaults))
        await s.commit()
