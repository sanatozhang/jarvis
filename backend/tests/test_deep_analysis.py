"""深度分析：deep_analysis 标志贯穿 + 跳窗 + 结果 tag。"""
from app.models.schemas import TaskCreate


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
