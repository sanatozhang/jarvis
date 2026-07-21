"""深度分析：deep_analysis 标志贯穿 + 跳窗 + 结果 tag。"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.schemas import TaskCreate
from tests.conftest import seed_issue, seed_task


# ── Fix 1: 深度分析绕过超时冷却 ────────────────────────────────────────────────

async def test_deep_analysis_bypasses_timeout_cooldown(client, db_session):
    """deep_analysis=True 应绕过 10min 超时冷却；non-deep 仍被 429 拦截。"""
    await seed_issue(db_session, "issue_tmo")

    # 构造一个"最近超时失败"的假对象（monkeypatch 数据库查询）
    fake_timeout_task = MagicMock()
    fake_timeout_task.id = "task_tmo_old"
    fake_timeout_task.error = "task_timeout_exceeded: wall clock 600s"

    with patch(
        "app.api.tasks.db.get_recent_timeout_task_for_issue",
        new_callable=AsyncMock,
        return_value=fake_timeout_task,
    ), patch("app.api.tasks.run_analysis_pipeline", new_callable=AsyncMock):

        # deep=True → 不应受 cooldown 拦截
        resp_deep = await client.post(
            "/api/tasks",
            json={"issue_id": "issue_tmo", "deep_analysis": True, "username": "testuser"},
        )
        assert resp_deep.status_code == 200, (
            f"deep_analysis=True 不应被 429 拦截，实际: {resp_deep.status_code} {resp_deep.text}"
        )
        assert resp_deep.json()["status"] == "queued"

        # deep=False（默认）→ 应被 429 拦截
        resp_normal = await client.post(
            "/api/tasks",
            json={"issue_id": "issue_tmo", "deep_analysis": False, "username": "testuser"},
        )
        assert resp_normal.status_code == 429, (
            f"deep_analysis=False 应被 timeout_cooldown 429 拦截，实际: {resp_normal.status_code} {resp_normal.text}"
        )
        assert resp_normal.json()["detail"]["error"] == "timeout_cooldown"


def test_taskcreate_has_deep_analysis_default_false():
    tc = TaskCreate(issue_id="fb_x")
    assert tc.deep_analysis is False
    tc2 = TaskCreate(issue_id="fb_x", deep_analysis=True)
    assert tc2.deep_analysis is True


import inspect
from app.workers import analysis_worker


def test_pipeline_and_condensation_accept_deep_flag():
    assert "deep_analysis" in inspect.signature(
        analysis_worker.run_analysis_pipeline).parameters
    assert "deep_analysis" in inspect.signature(
        analysis_worker._run_context_condensation).parameters


from app.agents.base import AgentConfig


def test_agentconfig_has_log_read_cap():
    cfg = AgentConfig(agent_type="claude_code")
    assert cfg.log_read_cap is None
    cfg2 = AgentConfig(agent_type="claude_code", log_read_cap=30)
    assert cfg2.log_read_cap == 30


def test_build_prompt_accepts_deep_analysis():
    import inspect
    from app.agents.base import BaseAgent
    assert "deep_analysis" in inspect.signature(BaseAgent.build_prompt).parameters


def test_tag_deep_agent_type():
    from app.services.agent_orchestrator import tag_deep_agent_type
    assert tag_deep_agent_type("claude_code", deep=True) == "claude_code_deep"
    assert tag_deep_agent_type("claude_code", deep=False) == "claude_code"
    assert tag_deep_agent_type("claude_code_deep", deep=True) == "claude_code_deep"  # 幂等
    assert tag_deep_agent_type("", deep=True) == ""  # 空不加后缀


# ── Fix 2: system_failure（如撞 max_turns 超时）也自动升级一次深度分析 ─────────
# 2026-07-21：之前只有「分析成功但 confidence=low」会自动升级，agent 自报
# system_failure（is_real_failure 强制判失败）走的是另一条分支，永远不会触发，
# 哪怕最终 confidence 也是 low。用户反馈一个撞 max_turns 的工单卡在这里没人管。

from app.models.schemas import AnalysisResult, Confidence


def _make_system_failure_result(issue_id: str, task_id: str, deep: bool = False) -> AnalysisResult:
    return AnalysisResult(
        task_id=task_id,
        issue_id=issue_id,
        problem_type="分析超时",
        problem_type_en="Analysis Timeout",
        root_cause="本次工单的日志分析在得出结论前就因达到最大对话轮数（30轮）而被系统终止",
        confidence=Confidence.LOW,
        confidence_reason="分析过程未完成，没有获得任何日志证据支撑的结论",
        system_failure=True,
        is_deep_analysis=deep,
    )


async def test_system_failure_triggers_auto_deep_analysis_once(client, db_session):
    """非深度分析跑撞 system_failure → 应该自动重跑一次深度分析（同 confidence=low 那条分支对称）。"""
    await seed_issue(db_session, "issue_sysfail")

    with patch(
        "app.api.tasks.run_analysis_pipeline",
        new_callable=AsyncMock,
        side_effect=lambda **kw: _make_system_failure_result(
            "issue_sysfail", kw["task_id"], deep=kw.get("deep_analysis", False),
        ),
    ), patch(
        "app.api.tasks._maybe_trigger_auto_deep_analysis", new_callable=AsyncMock,
    ) as mock_auto_deep:
        resp = await client.post(
            "/api/tasks",
            json={"issue_id": "issue_sysfail", "deep_analysis": False, "username": "testuser"},
        )
        assert resp.status_code == 200
        mock_auto_deep.assert_awaited_once()
        assert mock_auto_deep.await_args.kwargs["issue_id"] == "issue_sysfail"


async def test_system_failure_does_not_retrigger_during_deep_analysis(client, db_session):
    """深度分析本身又撞 system_failure → 不再自动重跑，避免无限重试。"""
    await seed_issue(db_session, "issue_sysfail_deep")

    with patch(
        "app.api.tasks.run_analysis_pipeline",
        new_callable=AsyncMock,
        side_effect=lambda **kw: _make_system_failure_result(
            "issue_sysfail_deep", kw["task_id"], deep=kw.get("deep_analysis", False),
        ),
    ), patch(
        "app.api.tasks._maybe_trigger_auto_deep_analysis", new_callable=AsyncMock,
    ) as mock_auto_deep:
        resp = await client.post(
            "/api/tasks",
            json={"issue_id": "issue_sysfail_deep", "deep_analysis": True, "username": "testuser"},
        )
        assert resp.status_code == 200
        mock_auto_deep.assert_not_awaited()
