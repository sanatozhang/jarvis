"""多平台工单阶段 2：AnalysisRecord / EventRecord 打 platform 标签。

只验证「打标」这一层——不碰任何查询/聚合逻辑（那是后续阶段）。
- save_analysis：优先取顶层 data["platform"]，否则退化到 data["issue"]["platform"]
  （tasks.py/queue.py 主路径把 result.issue 挂在 AnalysisResult 上，model_dump() 后即此形状）；
  都没有 → normalize_platform("") == "app"，向后兼容老式调用。
- log_event：显式传入才打标，且不合法/空值一律落空串（"未标注"），不像 AnalysisRecord
  那样兜底成 "app"——很多 EventRecord 是通用埋点，没有平台语境。
"""
from __future__ import annotations

import pytest

from tests.conftest import seed_issue


@pytest.fixture()
async def bound_db(db_session):
    """把生产代码的全局 session factory 绑到测试 in-memory DB（见 test_followup_rewindow.py 同款）。"""
    import app.db.database as db_mod
    original = db_mod._session_factory
    db_mod._session_factory = db_session
    yield db_session
    db_mod._session_factory = original


# ---------------------------------------------------------------------------
# save_analysis
# ---------------------------------------------------------------------------

async def test_save_analysis_platform_from_top_level_key(bound_db):
    from app.db.database import save_analysis

    record = await save_analysis({
        "task_id": "t1", "issue_id": "iss1",
        "problem_type": "蓝牙连接", "root_cause": "BLE 断连",
        "platform": "Web",
    })
    assert record.platform == "web"


async def test_save_analysis_platform_from_nested_issue(bound_db):
    """tasks.py/queue.py 主路径：result.issue = issue（Issue pydantic）→ model_dump() 后
    platform 挂在 data["issue"]["platform"]，没有顶层 platform key。"""
    from app.db.database import save_analysis

    record = await save_analysis({
        "task_id": "t2", "issue_id": "iss2",
        "problem_type": "固件升级", "root_cause": "OTA 失败",
        "issue": {"record_id": "iss2", "platform": "MCP", "description": "x"},
    })
    assert record.platform == "mcp"


async def test_save_analysis_platform_top_level_wins_over_issue(bound_db):
    from app.db.database import save_analysis

    record = await save_analysis({
        "task_id": "t3", "issue_id": "iss3",
        "issue": {"platform": "desktop"},
        "platform": "web",
    })
    assert record.platform == "web"


async def test_save_analysis_defaults_to_app_without_platform_info(bound_db):
    """老式调用（无 issue、无 platform）→ 向后兼容默认 app，零回归。"""
    from app.db.database import save_analysis

    record = await save_analysis({
        "task_id": "t4", "issue_id": "iss4",
        "problem_type": "未知", "root_cause": "",
    })
    assert record.platform == "app"


async def test_save_analysis_defaults_to_app_with_none_issue(bound_db):
    """AnalysisResult.issue: Optional[Issue] = None → model_dump() 产出 {"issue": None}
    （linear_webhook.py / v1_analyze.py 部分路径不显式设置 result.issue）。不应崩溃，应兜底 app。"""
    from app.db.database import save_analysis

    record = await save_analysis({
        "task_id": "t5", "issue_id": "iss5", "issue": None,
    })
    assert record.platform == "app"


async def test_save_analysis_platform_normalized_case_insensitive(bound_db):
    from app.db.database import save_analysis

    record = await save_analysis({
        "task_id": "t6", "issue_id": "iss6", "platform": "APP",
    })
    assert record.platform == "app"


# ---------------------------------------------------------------------------
# log_event
# ---------------------------------------------------------------------------

async def test_log_event_platform_normalized(bound_db):
    from app.db.database import log_event
    from sqlalchemy import select
    from app.db.database import EventRecord

    await log_event("feedback_submit", issue_id="iss1", username="u1", platform="Web")

    async with bound_db() as s:
        row = (await s.execute(select(EventRecord).where(EventRecord.event_type == "feedback_submit"))).scalar_one()
    assert row.platform == "web"


async def test_log_event_without_platform_stays_unmarked(bound_db):
    """老调用点不传 platform → 落空串（"未标注"），不报错、不被强行归一化成 app。"""
    from app.db.database import log_event
    from sqlalchemy import select
    from app.db.database import EventRecord

    await log_event("analysis_start", issue_id="iss2", username="u2")

    async with bound_db() as s:
        row = (await s.execute(select(EventRecord).where(EventRecord.event_type == "analysis_start"))).scalar_one()
    assert row.platform == ""


async def test_log_event_empty_platform_stays_unmarked(bound_db):
    """显式传空字符串同样不归一化——空串不代表 app。"""
    from app.db.database import log_event
    from sqlalchemy import select
    from app.db.database import EventRecord

    await log_event("page_visit", issue_id="", username="u3", platform="")

    async with bound_db() as s:
        row = (await s.execute(select(EventRecord).where(EventRecord.event_type == "page_visit"))).scalar_one()
    assert row.platform == ""


async def test_log_event_unknown_platform_falls_back_to_app_when_provided(bound_db):
    """非空但不在白名单的值：仍走 normalize_platform，兜底到 app（与「未传」的空串区分开）。"""
    from app.db.database import log_event
    from sqlalchemy import select
    from app.db.database import EventRecord

    await log_event("button_click", issue_id="", username="u4", platform="android")

    async with bound_db() as s:
        row = (await s.execute(select(EventRecord).where(EventRecord.event_type == "button_click"))).scalar_one()
    assert row.platform == "app"
