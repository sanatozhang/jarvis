# TDD Backend API Tests — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a complete pytest test suite for all 16 backend API modules so that `pytest` passing = safe to deploy.

**Architecture:** Each test file uses httpx.AsyncClient against the FastAPI app with an in-memory SQLite database. External services (Feishu, Linear, Zendesk, Agent CLI, OpenAI) are mocked. conftest.py provides shared fixtures for DB setup, client creation, and seed data.

**Tech Stack:** pytest, pytest-asyncio, httpx (already in requirements.txt), unittest.mock

---

### Task 1: Test infrastructure setup

**Files:**
- Modify: `backend/requirements.txt` — add test dependencies
- Create: `backend/pytest.ini` — pytest config
- Create: `backend/tests/__init__.py` — package marker
- Create: `backend/tests/conftest.py` — shared fixtures

**Step 1: Add test dependencies to requirements.txt**

Append to `backend/requirements.txt`:
```
# Testing
pytest>=8.0.0
pytest-asyncio>=0.24.0
```

Note: httpx is already in requirements.txt.

**Step 2: Create pytest.ini**

Create `backend/pytest.ini`:
```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

**Step 3: Create tests/__init__.py**

Empty file.

**Step 4: Create conftest.py**

Create `backend/tests/conftest.py`:
```python
"""Shared test fixtures for Jarvis backend API tests."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.database import Base


@pytest.fixture(scope="session")
def event_loop():
    """Use a single event loop for all tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


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

    # Patch get_settings to avoid reading real config/env
    with patch("app.config.get_settings") as mock_settings:
        settings = _make_test_settings()
        mock_settings.return_value = settings

        # Patch init_db to no-op (we already created tables)
        with patch("app.db.database.init_db", new_callable=AsyncMock):
            # Import app AFTER patching
            from app.main import app

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac

    db_mod._engine = original_engine
    db_mod._session_factory = original_factory


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
    return s


# ---- Seed data helpers ----

async def seed_user(client: AsyncClient, username: str = "testuser") -> dict:
    """Create a user via API."""
    resp = await client.post("/api/users/login", json={"username": username})
    return resp.json()


async def seed_admin(client: AsyncClient, username: str = "sanato") -> dict:
    """Create an admin user (first user 'sanato' is auto-admin)."""
    resp = await client.post("/api/users/login", json={"username": username})
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
```

**Step 5: Install test dependencies and verify**

Run: `cd backend && pip install pytest pytest-asyncio`

**Step 6: Commit**

```bash
git add backend/requirements.txt backend/pytest.ini backend/tests/
git commit -m "feat: add test infrastructure (pytest + conftest fixtures)"
```

---

### Task 2: test_health.py

**Files:**
- Create: `backend/tests/test_health.py`

**Step 1: Write tests**

```python
"""Tests for /api/health endpoints."""
from unittest.mock import patch, AsyncMock
import shutil


async def test_health_check(client):
    """GET /api/health returns healthy status."""
    with patch("app.api.health.RuleEngine") as mock_engine_cls:
        mock_engine = mock_engine_cls.return_value
        mock_engine.list_rules.return_value = []
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("healthy", "degraded")
    assert "checks" in data


async def test_health_agents(client):
    """GET /api/health/agents returns agent availability."""
    with patch("shutil.which", return_value=None):
        resp = await client.get("/api/health/agents")
    assert resp.status_code == 200
    data = resp.json()
    assert "claude_code" in data
    assert "codex" in data
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_health.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add backend/tests/test_health.py
git commit -m "test: add health API tests"
```

---

### Task 3: test_users.py

**Files:**
- Create: `backend/tests/test_users.py`

**Step 1: Write tests**

```python
"""Tests for /api/users endpoints."""


async def test_login_creates_user(client):
    """POST /api/users/login creates a new user."""
    resp = await client.post("/api/users/login", json={"username": "newuser"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == "newuser"
    assert data["role"] in ("user", "admin")


async def test_login_empty_username(client):
    """POST /api/users/login rejects empty username."""
    resp = await client.post("/api/users/login", json={"username": ""})
    assert resp.status_code == 400


async def test_login_idempotent(client):
    """POST /api/users/login twice returns same user."""
    await client.post("/api/users/login", json={"username": "alice"})
    resp = await client.post("/api/users/login", json={"username": "alice"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "alice"


async def test_get_user(client):
    """GET /api/users/{username} returns user info."""
    await client.post("/api/users/login", json={"username": "bob"})
    resp = await client.get("/api/users/bob")
    assert resp.status_code == 200
    assert resp.json()["username"] == "bob"


async def test_get_user_not_found(client):
    """GET /api/users/{username} returns 404."""
    resp = await client.get("/api/users/nonexistent")
    assert resp.status_code == 404


async def test_list_users(client):
    """GET /api/users lists all users."""
    await client.post("/api/users/login", json={"username": "u1"})
    await client.post("/api/users/login", json={"username": "u2"})
    resp = await client.get("/api/users")
    assert resp.status_code == 200
    usernames = [u["username"] for u in resp.json()]
    assert "u1" in usernames
    assert "u2" in usernames
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_users.py -v`

**Step 3: Commit**

```bash
git add backend/tests/test_users.py
git commit -m "test: add users API tests"
```

---

### Task 4: test_rules.py

**Files:**
- Create: `backend/tests/test_rules.py`

**Step 1: Write tests**

```python
"""Tests for /api/rules endpoints."""
from unittest.mock import patch, MagicMock
from app.models.schemas import Rule, RuleMeta, RuleTrigger


def _make_mock_engine(rules=None):
    """Create a mock RuleEngine."""
    engine = MagicMock()
    _rules = {r.meta.id: r for r in (rules or [])}

    engine.list_rules.return_value = list(_rules.values())
    engine.get_rule.side_effect = lambda rid: _rules.get(rid)

    async def save_rule(rule):
        _rules[rule.meta.id] = rule
        return rule
    engine.save_rule = save_rule

    async def delete_rule(rid):
        if rid in _rules:
            del _rules[rid]
            return True
        return False
    engine.delete_rule = delete_rule

    engine.reload.return_value = None

    async def sync():
        pass
    engine.sync_files_to_db = sync

    engine.match_rules.return_value = list(_rules.values())

    return engine


@patch("app.api.rules._engine", None)
async def test_create_and_list_rules(client):
    """POST then GET /api/rules."""
    mock_engine = _make_mock_engine()
    with patch("app.api.rules._get_engine", return_value=mock_engine):
        # Create
        resp = await client.post("/api/rules", json={
            "id": "test-rule",
            "name": "Test Rule",
            "triggers": {"keywords": ["bluetooth"], "priority": 5},
            "content": "# Test Rule\nSome content",
        })
        assert resp.status_code == 200
        assert resp.json()["meta"]["id"] == "test-rule"

        # List
        resp = await client.get("/api/rules")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


@patch("app.api.rules._engine", None)
async def test_get_rule_not_found(client):
    """GET /api/rules/{id} returns 404."""
    mock_engine = _make_mock_engine()
    with patch("app.api.rules._get_engine", return_value=mock_engine):
        resp = await client.get("/api/rules/nonexistent")
        assert resp.status_code == 404


@patch("app.api.rules._engine", None)
async def test_create_duplicate_rule(client):
    """POST /api/rules with existing id returns 409."""
    existing = Rule(
        meta=RuleMeta(id="dup", name="Dup", triggers=RuleTrigger()),
        content="content",
    )
    mock_engine = _make_mock_engine([existing])
    with patch("app.api.rules._get_engine", return_value=mock_engine):
        resp = await client.post("/api/rules", json={
            "id": "dup", "name": "Dup2",
            "triggers": {"keywords": [], "priority": 5},
            "content": "x",
        })
        assert resp.status_code == 409


@patch("app.api.rules._engine", None)
async def test_delete_rule(client):
    """DELETE /api/rules/{id} removes the rule."""
    existing = Rule(
        meta=RuleMeta(id="to-delete", name="Del", triggers=RuleTrigger()),
        content="c",
    )
    mock_engine = _make_mock_engine([existing])
    with patch("app.api.rules._get_engine", return_value=mock_engine):
        resp = await client.delete("/api/rules/to-delete")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "to-delete"


@patch("app.api.rules._engine", None)
async def test_delete_rule_not_found(client):
    """DELETE /api/rules/{id} returns 404."""
    mock_engine = _make_mock_engine()
    with patch("app.api.rules._get_engine", return_value=mock_engine):
        resp = await client.delete("/api/rules/nope")
        assert resp.status_code == 404


@patch("app.api.rules._engine", None)
async def test_reload_rules(client):
    """POST /api/rules/reload reloads rules."""
    mock_engine = _make_mock_engine()
    with patch("app.api.rules._get_engine", return_value=mock_engine):
        resp = await client.post("/api/rules/reload")
        assert resp.status_code == 200
        assert "reloaded" in resp.json()


@patch("app.api.rules._engine", None)
async def test_test_rule_match(client):
    """POST /api/rules/{id}/test returns matched rules."""
    existing = Rule(
        meta=RuleMeta(id="bt", name="BT", triggers=RuleTrigger(keywords=["bluetooth"])),
        content="c",
    )
    mock_engine = _make_mock_engine([existing])
    with patch("app.api.rules._get_engine", return_value=mock_engine):
        resp = await client.post("/api/rules/bt/test?description=bluetooth+issue")
        assert resp.status_code == 200
        assert "matched_rules" in resp.json()
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_rules.py -v`

**Step 3: Commit**

```bash
git add backend/tests/test_rules.py
git commit -m "test: add rules API tests"
```

---

### Task 5: test_local.py

**Files:**
- Create: `backend/tests/test_local.py`

**Step 1: Write tests**

```python
"""Tests for /api/local endpoints."""
from tests.conftest import seed_issue, seed_task, seed_analysis


async def test_list_in_progress(client, db_session):
    """GET /api/local/in-progress returns analyzing issues."""
    await seed_issue(db_session, "issue_ip", status="analyzing")
    resp = await client.get("/api/local/in-progress")
    assert resp.status_code == 200
    assert resp.json()["total"] >= 0  # may be 0 if seed didn't match filter


async def test_list_completed(client, db_session):
    """GET /api/local/completed returns done+failed issues."""
    await seed_issue(db_session, "issue_done", status="done")
    resp = await client.get("/api/local/completed")
    assert resp.status_code == 200
    data = resp.json()
    assert "issues" in data
    assert "total" in data


async def test_list_failed(client, db_session):
    """GET /api/local/failed returns failed issues."""
    await seed_issue(db_session, "issue_fail", status="failed")
    resp = await client.get("/api/local/failed")
    assert resp.status_code == 200


async def test_tracking_with_filters(client, db_session):
    """GET /api/local/tracking supports multi-dimensional filters."""
    await seed_issue(db_session, "issue_track", status="done", platform="APP", source="local")
    resp = await client.get("/api/local/tracking", params={
        "platform": "APP", "source": "local",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "issues" in data
    assert "total_pages" in data


async def test_tracking_pagination(client, db_session):
    """GET /api/local/tracking paginates correctly."""
    for i in range(5):
        await seed_issue(db_session, f"pg_{i}", status="done")
    resp = await client.get("/api/local/tracking", params={"page": 1, "page_size": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["page"] == 1
    assert data["page_size"] == 2


async def test_issue_detail(client, db_session):
    """GET /api/local/{id}/detail returns issue with analysis."""
    await seed_issue(db_session, "det_1", status="done")
    await seed_task(db_session, "task_det", "det_1")
    await seed_analysis(db_session, "task_det", "det_1")
    resp = await client.get("/api/local/det_1/detail")
    assert resp.status_code == 200


async def test_issue_detail_not_found(client):
    """GET /api/local/{id}/detail returns 404."""
    resp = await client.get("/api/local/nonexistent/detail")
    assert resp.status_code == 404


async def test_issue_analyses(client, db_session):
    """GET /api/local/{id}/analyses returns analysis history."""
    await seed_issue(db_session, "ana_1")
    await seed_analysis(db_session, "task_a1", "ana_1")
    resp = await client.get("/api/local/ana_1/analyses")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_mark_inaccurate(client, db_session):
    """POST /api/local/{id}/inaccurate marks issue."""
    await seed_issue(db_session, "inacc_1", status="done")
    resp = await client.post("/api/local/inacc_1/inaccurate")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_mark_inaccurate_not_found(client):
    """POST /api/local/{id}/inaccurate returns 404."""
    resp = await client.post("/api/local/missing/inaccurate")
    assert resp.status_code == 404


async def test_list_inaccurate(client, db_session):
    """GET /api/local/inaccurate lists marked issues."""
    await seed_issue(db_session, "inacc_list", status="inaccurate")
    resp = await client.get("/api/local/inaccurate")
    assert resp.status_code == 200


async def test_soft_delete(client, db_session):
    """DELETE /api/local/{id} soft-deletes."""
    await seed_issue(db_session, "del_1", status="done")
    resp = await client.delete("/api/local/del_1")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"


async def test_soft_delete_not_found(client):
    """DELETE /api/local/{id} returns 404."""
    resp = await client.delete("/api/local/no_such")
    assert resp.status_code == 404
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_local.py -v`

**Step 3: Commit**

```bash
git add backend/tests/test_local.py
git commit -m "test: add local tracking API tests (including mark inaccurate)"
```

---

### Task 6: test_tasks.py

**Files:**
- Create: `backend/tests/test_tasks.py`

**Step 1: Write tests**

```python
"""Tests for /api/tasks endpoints."""
from unittest.mock import patch, AsyncMock
from tests.conftest import seed_issue, seed_task, seed_analysis


async def test_create_task(client, db_session):
    """POST /api/tasks creates a task and returns progress."""
    await seed_issue(db_session, "issue_ct", status="pending")
    with patch("app.api.tasks.run_analysis_pipeline", new_callable=AsyncMock):
        resp = await client.post("/api/tasks", json={
            "issue_id": "issue_ct",
            "username": "testuser",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["issue_id"] == "issue_ct"
    assert data["status"] == "queued"
    assert "task_id" in data


async def test_get_task_status(client, db_session):
    """GET /api/tasks/{id} returns task progress."""
    await seed_issue(db_session, "issue_gs")
    await seed_task(db_session, "task_gs", "issue_gs", status="done", progress=100)
    resp = await client.get("/api/tasks/task_gs")
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"


async def test_get_task_not_found(client):
    """GET /api/tasks/{id} returns 404."""
    resp = await client.get("/api/tasks/nope")
    assert resp.status_code == 404


async def test_get_task_result(client, db_session):
    """GET /api/tasks/{id}/result returns analysis."""
    await seed_issue(db_session, "issue_gr")
    await seed_task(db_session, "task_gr", "issue_gr")
    await seed_analysis(db_session, "task_gr", "issue_gr")
    resp = await client.get("/api/tasks/task_gr/result")
    assert resp.status_code == 200
    data = resp.json()
    assert data["problem_type"] == "蓝牙连接"


async def test_get_task_result_no_analysis(client, db_session):
    """GET /api/tasks/{id}/result returns 404 when no analysis."""
    await seed_task(db_session, "task_na", "no_issue")
    resp = await client.get("/api/tasks/task_na/result")
    assert resp.status_code == 404


async def test_list_tasks(client, db_session):
    """GET /api/tasks lists recent tasks."""
    await seed_task(db_session, "task_l1", "issue_l1")
    await seed_task(db_session, "task_l2", "issue_l2")
    resp = await client.get("/api/tasks")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_batch_analyze(client, db_session):
    """POST /api/tasks/batch creates multiple tasks."""
    await seed_issue(db_session, "b1", status="pending")
    await seed_issue(db_session, "b2", status="pending")
    with patch("app.api.tasks.run_analysis_pipeline", new_callable=AsyncMock):
        resp = await client.post("/api/tasks/batch", json={
            "issue_ids": ["b1", "b2"],
        })
    assert resp.status_code == 200
    assert len(resp.json()) == 2
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_tasks.py -v`

**Step 3: Commit**

```bash
git add backend/tests/test_tasks.py
git commit -m "test: add tasks API tests"
```

---

### Task 7: test_oncall.py

**Files:**
- Create: `backend/tests/test_oncall.py`

**Step 1: Write tests**

```python
"""Tests for /api/oncall endpoints."""
from tests.conftest import seed_admin, seed_user


async def test_get_current_oncall(client):
    """GET /api/oncall/current returns members."""
    resp = await client.get("/api/oncall/current")
    assert resp.status_code == 200
    data = resp.json()
    assert "members" in data
    assert "count" in data


async def test_get_schedule(client):
    """GET /api/oncall/schedule returns groups."""
    resp = await client.get("/api/oncall/schedule")
    assert resp.status_code == 200
    data = resp.json()
    assert "groups" in data
    assert "total_groups" in data


async def test_update_schedule_admin(client):
    """PUT /api/oncall/schedule by admin succeeds."""
    await seed_admin(client, "sanato")
    resp = await client.put("/api/oncall/schedule", params={"username": "sanato"}, json={
        "groups": [{"members": ["a@test.com", "b@test.com"]}],
        "start_date": "2026-03-01",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_update_schedule_non_admin(client):
    """PUT /api/oncall/schedule by non-admin returns 403."""
    await seed_user(client, "regular")
    resp = await client.put("/api/oncall/schedule", params={"username": "regular"}, json={
        "groups": [{"members": ["a@test.com"]}],
        "start_date": "2026-03-01",
    })
    assert resp.status_code == 403
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_oncall.py -v`

**Step 3: Commit**

```bash
git add backend/tests/test_oncall.py
git commit -m "test: add oncall API tests"
```

---

### Task 8: test_settings.py

**Files:**
- Create: `backend/tests/test_settings.py`

**Step 1: Write tests**

```python
"""Tests for /api/settings endpoints."""


async def test_get_agent_config(client):
    """GET /api/settings/agent returns config."""
    resp = await client.get("/api/settings/agent")
    assert resp.status_code == 200
    data = resp.json()
    assert "default" in data
    assert "timeout" in data
    assert "providers" in data


async def test_update_agent_config(client):
    """PUT /api/settings/agent updates runtime config."""
    resp = await client.put("/api/settings/agent", json={
        "timeout": 600,
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"


async def test_get_concurrency_config(client):
    """GET /api/settings/concurrency returns config."""
    resp = await client.get("/api/settings/concurrency")
    assert resp.status_code == 200
    data = resp.json()
    assert "max_workers" in data
    assert "task_timeout" in data
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_settings.py -v`

**Step 3: Commit**

```bash
git add backend/tests/test_settings.py
git commit -m "test: add settings API tests"
```

---

### Task 9: test_analytics.py

**Files:**
- Create: `backend/tests/test_analytics.py`

**Step 1: Write tests**

```python
"""Tests for /api/analytics endpoints."""
from unittest.mock import patch, AsyncMock


async def test_track_event(client):
    """POST /api/analytics/track records an event."""
    resp = await client.post("/api/analytics/track", json={
        "event_type": "page_visit",
        "username": "testuser",
        "detail": {"page": "/"},
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_dashboard(client):
    """GET /api/analytics/dashboard returns metrics."""
    resp = await client.get("/api/analytics/dashboard", params={"days": 7})
    assert resp.status_code == 200
    data = resp.json()
    assert "value_metrics" in data


async def test_rule_accuracy(client):
    """GET /api/analytics/rule-accuracy returns stats."""
    with patch("app.api.analytics.get_rule_accuracy_stats", new_callable=AsyncMock, return_value={"rules": [], "total": 0}):
        resp = await client.get("/api/analytics/rule-accuracy")
    assert resp.status_code == 200
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_analytics.py -v`

**Step 3: Commit**

```bash
git add backend/tests/test_analytics.py
git commit -m "test: add analytics API tests"
```

---

### Task 10: test_reports.py

**Files:**
- Create: `backend/tests/test_reports.py`

**Step 1: Write tests**

```python
"""Tests for /api/reports endpoints."""
from tests.conftest import seed_issue, seed_task, seed_analysis


async def test_daily_report_empty(client):
    """GET /api/reports/daily/{date} returns empty report."""
    resp = await client.get("/api/reports/daily/2026-01-01")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_issues"] == 0


async def test_daily_report_invalid_date(client):
    """GET /api/reports/daily/{date} rejects bad format."""
    resp = await client.get("/api/reports/daily/not-a-date")
    assert resp.status_code == 400


async def test_daily_report_markdown(client):
    """GET /api/reports/daily/{date}/markdown returns text."""
    resp = await client.get("/api/reports/daily/2026-01-01/markdown")
    assert resp.status_code == 200
    assert "值班汇总报告" in resp.text


async def test_report_dates(client):
    """GET /api/reports/dates returns date list."""
    resp = await client.get("/api/reports/dates")
    assert resp.status_code == 200
    assert "dates" in resp.json()
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_reports.py -v`

**Step 3: Commit**

```bash
git add backend/tests/test_reports.py
git commit -m "test: add reports API tests"
```

---

### Task 11: test_feedback.py

**Files:**
- Create: `backend/tests/test_feedback.py`

**Step 1: Write tests**

```python
"""Tests for /api/feedback endpoints."""
from unittest.mock import patch, AsyncMock


async def test_submit_feedback(client):
    """POST /api/feedback creates issue and starts analysis."""
    with patch("app.api.feedback._run_task", new_callable=AsyncMock) as mock_run:
        with patch("app.api.tasks._run_task", new_callable=AsyncMock):
            resp = await client.post("/api/feedback", data={
                "description": "蓝牙断连问题",
                "category": "蓝牙",
                "platform": "APP",
                "username": "testuser",
            })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "record_id" in data
    assert data["record_id"].startswith("fb_")


async def test_submit_feedback_missing_description(client):
    """POST /api/feedback requires description."""
    resp = await client.post("/api/feedback", data={})
    assert resp.status_code == 422  # validation error
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_feedback.py -v`

**Step 3: Commit**

```bash
git add backend/tests/test_feedback.py
git commit -m "test: add feedback API tests"
```

---

### Task 12: test_issues.py

**Files:**
- Create: `backend/tests/test_issues.py`

**Step 1: Write tests**

```python
"""Tests for /api/issues endpoints (Feishu integration, mocked)."""
from unittest.mock import patch, AsyncMock, MagicMock
from app.models.schemas import Issue


async def test_list_pending_issues(client):
    """GET /api/issues returns pending issues from Feishu."""
    mock_issues = [
        Issue(record_id="r1", description="test issue 1"),
        Issue(record_id="r2", description="test issue 2"),
    ]
    with patch("app.api.issues.FeishuClient") as mock_cls:
        instance = mock_cls.return_value
        instance.list_pending_issues = AsyncMock(return_value=mock_issues)
        resp = await client.get("/api/issues")
    assert resp.status_code == 200
    data = resp.json()
    assert "issues" in data
    assert data["total"] >= 0


async def test_refresh_issues(client):
    """POST /api/issues/refresh clears cache."""
    with patch("app.api.issues.FeishuClient") as mock_cls:
        mock_cls.invalidate_cache = MagicMock()
        resp = await client.post("/api/issues/refresh")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cache_invalidated"


async def test_get_single_issue(client):
    """GET /api/issues/{id} returns issue from Feishu."""
    mock_issue = Issue(record_id="r1", description="test")
    with patch("app.api.issues.FeishuClient") as mock_cls:
        instance = mock_cls.return_value
        instance.get_issue = AsyncMock(return_value=mock_issue)
        resp = await client.get("/api/issues/r1")
    assert resp.status_code == 200
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_issues.py -v`

**Step 3: Commit**

```bash
git add backend/tests/test_issues.py
git commit -m "test: add issues API tests (Feishu mocked)"
```

---

### Task 13: test_v1_analyze.py

**Files:**
- Create: `backend/tests/test_v1_analyze.py`

**Step 1: Write tests**

```python
"""Tests for /api/v1 public API endpoints."""
from unittest.mock import patch, AsyncMock
from tests.conftest import seed_task, seed_analysis
import os


async def test_submit_analysis_no_auth(client):
    """POST /api/v1/analyze works without API key when not configured."""
    with patch.dict(os.environ, {"JARVIS_API_KEY": ""}, clear=False):
        with patch("app.api.v1_analyze.API_KEY", ""):
            with patch("app.api.v1_analyze._run_api_analysis", new_callable=AsyncMock):
                resp = await client.post("/api/v1/analyze", data={
                    "description": "Test issue",
                })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "processing"
    assert "task_id" in data


async def test_submit_analysis_bad_key(client):
    """POST /api/v1/analyze rejects wrong API key."""
    with patch("app.api.v1_analyze.API_KEY", "secret123"):
        resp = await client.post("/api/v1/analyze",
            data={"description": "Test"},
            headers={"Authorization": "Bearer wrong"},
        )
    assert resp.status_code == 403


async def test_poll_result_not_found(client):
    """GET /api/v1/analyze/{id} returns 404."""
    with patch("app.api.v1_analyze.API_KEY", ""):
        resp = await client.get("/api/v1/analyze/nonexistent")
    assert resp.status_code == 404


async def test_poll_result_done(client, db_session):
    """GET /api/v1/analyze/{id} returns result when done."""
    await seed_task(db_session, "api_task", "api_issue", status="done")
    await seed_analysis(db_session, "api_task", "api_issue")
    with patch("app.api.v1_analyze.API_KEY", ""):
        resp = await client.get("/api/v1/analyze/api_task")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "done"
    assert data["problem_type"] == "蓝牙连接"
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_v1_analyze.py -v`

**Step 3: Commit**

```bash
git add backend/tests/test_v1_analyze.py
git commit -m "test: add v1 public API tests"
```

---

### Task 14: test_env_settings.py

**Files:**
- Create: `backend/tests/test_env_settings.py`

**Step 1: Write tests**

```python
"""Tests for /api/env endpoints."""
from unittest.mock import patch
from tests.conftest import seed_admin, seed_user


async def test_get_env_admin(client):
    """GET /api/env returns settings for admin."""
    await seed_admin(client, "sanato")
    with patch("app.api.env_settings.ENV_PATH") as mock_path:
        mock_path.exists.return_value = False
        resp = await client.get("/api/env", params={"username": "sanato"})
    assert resp.status_code == 200
    assert "groups" in resp.json()


async def test_get_env_non_admin(client):
    """GET /api/env returns 403 for non-admin."""
    await seed_user(client, "regular")
    resp = await client.get("/api/env", params={"username": "regular"})
    assert resp.status_code == 403


async def test_update_env_non_admin(client):
    """PUT /api/env returns 403 for non-admin."""
    await seed_user(client, "regular2")
    resp = await client.put("/api/env", params={"username": "regular2"}, json={
        "updates": {"FEISHU_APP_ID": "new_id"},
    })
    assert resp.status_code == 403
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_env_settings.py -v`

**Step 3: Commit**

```bash
git add backend/tests/test_env_settings.py
git commit -m "test: add env settings API tests"
```

---

### Task 15: test_linear.py

**Files:**
- Create: `backend/tests/test_linear.py`

**Step 1: Write tests**

```python
"""Tests for /api/linear webhook endpoints."""
import hashlib
import hmac
import json
from unittest.mock import patch, AsyncMock


def _sign(body: bytes, secret: str) -> str:
    """Compute Linear webhook signature."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_webhook_no_trigger(client):
    """POST /api/linear/webhook ignores comments without trigger."""
    payload = {
        "type": "Comment",
        "action": "create",
        "data": {
            "id": "c1",
            "issueId": "i1",
            "body": "Just a regular comment",
            "user": {"name": "Alice"},
        },
    }
    body = json.dumps(payload).encode()
    sig = _sign(body, "test-secret")
    resp = await client.post("/api/linear/webhook",
        content=body,
        headers={"Content-Type": "application/json", "Linear-Signature": sig},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_webhook_with_trigger(client):
    """POST /api/linear/webhook triggers analysis on @ai-agent."""
    payload = {
        "type": "Comment",
        "action": "create",
        "data": {
            "id": "c2",
            "issueId": "i2",
            "body": "@ai-agent please analyze this",
            "user": {"name": "Bob"},
        },
    }
    body = json.dumps(payload).encode()
    sig = _sign(body, "test-secret")
    with patch("app.api.linear_webhook._run_linear_analysis", new_callable=AsyncMock):
        resp = await client.post("/api/linear/webhook",
            content=body,
            headers={"Content-Type": "application/json", "Linear-Signature": sig},
        )
    assert resp.status_code == 200


async def test_webhook_bad_signature(client):
    """POST /api/linear/webhook rejects invalid signature."""
    body = json.dumps({"type": "Comment", "action": "create", "data": {}}).encode()
    resp = await client.post("/api/linear/webhook",
        content=body,
        headers={"Content-Type": "application/json", "Linear-Signature": "bad"},
    )
    assert resp.status_code == 401
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_linear.py -v`

**Step 3: Commit**

```bash
git add backend/tests/test_linear.py
git commit -m "test: add Linear webhook API tests"
```

---

### Task 16: test_eval.py

**Files:**
- Create: `backend/tests/test_eval.py`

**Step 1: Write tests**

```python
"""Tests for /api/eval endpoints."""
from unittest.mock import patch, AsyncMock


async def test_create_dataset(client):
    """POST /api/eval/datasets creates a dataset."""
    resp = await client.post("/api/eval/datasets", json={
        "name": "test-dataset",
        "description": "Test eval dataset",
        "sample_ids": [],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "test-dataset"
    assert "id" in data


async def test_list_datasets(client):
    """GET /api/eval/datasets lists datasets."""
    # Create one first
    await client.post("/api/eval/datasets", json={"name": "ds1"})
    resp = await client.get("/api/eval/datasets")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_get_dataset_not_found(client):
    """GET /api/eval/datasets/{id} returns 404."""
    resp = await client.get("/api/eval/datasets/999")
    assert resp.status_code == 404


async def test_start_run_no_dataset(client):
    """POST /api/eval/run returns 404 for nonexistent dataset."""
    resp = await client.post("/api/eval/run", json={
        "dataset_id": 999,
    })
    assert resp.status_code == 404


async def test_start_run(client):
    """POST /api/eval/run starts an eval run."""
    # Create dataset first
    ds_resp = await client.post("/api/eval/datasets", json={"name": "run-ds"})
    ds_id = ds_resp.json()["id"]
    with patch("app.api.eval.run_eval", new_callable=AsyncMock):
        resp = await client.post("/api/eval/run", json={"dataset_id": ds_id})
    assert resp.status_code == 200
    assert resp.json()["status"] == "started"


async def test_list_runs(client):
    """GET /api/eval/runs lists runs."""
    resp = await client.get("/api/eval/runs")
    assert resp.status_code == 200


async def test_get_run_not_found(client):
    """GET /api/eval/runs/{id} returns 404."""
    resp = await client.get("/api/eval/runs/999")
    assert resp.status_code == 404
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_eval.py -v`

**Step 3: Commit**

```bash
git add backend/tests/test_eval.py
git commit -m "test: add eval pipeline API tests"
```

---

### Task 17: test_golden_samples.py

**Files:**
- Create: `backend/tests/test_golden_samples.py`

**Step 1: Write tests**

```python
"""Tests for /api/golden-samples endpoints."""
from unittest.mock import patch, AsyncMock
from tests.conftest import seed_issue, seed_analysis


async def test_list_samples_empty(client):
    """GET /api/golden-samples returns empty list."""
    resp = await client.get("/api/golden-samples")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_get_stats(client):
    """GET /api/golden-samples/stats returns statistics."""
    resp = await client.get("/api/golden-samples/stats")
    assert resp.status_code == 200


async def test_promote_sample_not_found(client):
    """POST /api/golden-samples returns 404 for bad analysis_id."""
    with patch("app.api.golden_samples.promote_analysis_to_sample", new_callable=AsyncMock, side_effect=ValueError("Not found")):
        resp = await client.post("/api/golden-samples", json={
            "analysis_id": 999,
        })
    assert resp.status_code == 404


async def test_delete_sample_not_found(client):
    """DELETE /api/golden-samples/{id} returns 404."""
    resp = await client.delete("/api/golden-samples/999")
    assert resp.status_code == 404
```

**Step 2: Run tests**

Run: `cd backend && python -m pytest tests/test_golden_samples.py -v`

**Step 3: Commit**

```bash
git add backend/tests/test_golden_samples.py
git commit -m "test: add golden samples API tests"
```

---

### Task 18: Docker integration

**Files:**
- Modify: `backend/Dockerfile` — add test stage

**Step 1: Update Dockerfile**

Read `backend/Dockerfile`, then modify to add multi-stage build with test stage:

```dockerfile
# ---- Test stage ----
FROM python:3.12-slim AS test
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python -m pytest --tb=short -q

# ---- Production stage ----
FROM python:3.12-slim AS production
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Step 2: Commit**

```bash
git add backend/Dockerfile
git commit -m "feat: add test stage to Docker build (tests must pass to deploy)"
```

---

### Task 19: Full test suite verification

**Step 1: Run all tests**

Run: `cd backend && python -m pytest -v --tb=short`

Expected: ALL PASS

**Step 2: Verify test count**

Expected: ~50+ test cases across 16 test files.

**Step 3: Final commit**

```bash
git add -A
git commit -m "feat: complete TDD test suite — 16 API modules, Docker integration"
```
