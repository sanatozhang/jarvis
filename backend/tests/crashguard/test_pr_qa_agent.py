"""post-PR QA agent 单测（纯函数 + 解析逻辑，不真调 agent / gh）"""
from __future__ import annotations

import pytest


# ─────────── prompt builder ───────────

def test_build_qa_prompt_includes_all_sections():
    from app.crashguard.services.pr_qa_agent import _build_qa_prompt
    p = _build_qa_prompt(
        diff_text="--- a/x.dart\n+++ b/x.dart\n@@ -1,3 +1,3 @@\n-old\n+new\n",
        root_cause="cause text",
        fix_suggestion="suggestion text",
        issue_stack="frame1\nframe2",
    )
    assert "Crash Root Cause" in p
    assert "cause text" in p
    assert "suggestion text" in p
    assert "frame1" in p
    assert "Actual PR Diff" in p
    assert "quality_score" in p
    assert "approve_ready" in p


def test_build_qa_prompt_truncates_long_inputs():
    """超长输入按上限截断，防 token 爆炸"""
    from app.crashguard.services.pr_qa_agent import _build_qa_prompt
    # 用稀有字符避免与模板里的 'x'/'y' 串台
    big_diff = "Q" * 50000
    big_cause = "Z" * 5000
    p = _build_qa_prompt(big_diff, big_cause, "fix", "stack")
    assert p.count("Q") == 8000  # diff_snip 上限
    assert p.count("Z") == 1500  # root_cause 上限
    # 整体 prompt 控制在合理 token 量（每输入字段都有上限）
    assert len(p) < 20000


# ─────────── JSON parse ───────────

def test_parse_qa_json_clean():
    from app.crashguard.services.pr_qa_agent import _parse_qa_json
    raw = '{"quality_score": 85, "verdict": "approve_ready", "addresses_root_cause": true}'
    out = _parse_qa_json(raw)
    assert out is not None
    assert out["quality_score"] == 85
    assert out["verdict"] == "approve_ready"


def test_parse_qa_json_with_surrounding_text():
    """LLM 经常在 JSON 前后多带 markdown / 解释——抓取 quality_score {...}"""
    from app.crashguard.services.pr_qa_agent import _parse_qa_json
    raw = (
        "Sure, here is my evaluation:\n"
        "```json\n"
        '{"quality_score": 40, "verdict": "do_not_merge", '
        '"reviewer_summary": "diff misses root cause"}\n'
        "```\n"
        "Hope this helps."
    )
    out = _parse_qa_json(raw)
    assert out is not None
    assert out["quality_score"] == 40
    assert out["verdict"] == "do_not_merge"


def test_parse_qa_json_garbage_returns_none():
    from app.crashguard.services.pr_qa_agent import _parse_qa_json
    assert _parse_qa_json("") is None
    assert _parse_qa_json("totally not json") is None


# ─────────── normalize ───────────

def test_normalize_clamps_score_and_validates_verdict():
    from app.crashguard.services.pr_qa_agent import _normalize_parsed
    out = _normalize_parsed({
        "quality_score": 150,  # 超界 → clamp 100
        "verdict": "BOGUS",  # 非法 → needs_revision
        "addresses_root_cause": "true",  # 字符串 truthy → True
        "scope_issues": ["a", "b"] * 10,  # 多于 5 → 截
        "regression_risks": None,  # None → []
        "reviewer_summary": "x" * 500,  # 超长 → 截 300
    })
    assert out["quality_score"] == 100
    assert out["verdict"] == "needs_revision"
    assert out["addresses_root_cause"] is True
    assert len(out["scope_issues"]) == 5
    assert out["regression_risks"] == []
    assert len(out["reviewer_summary"]) == 300


def test_normalize_handles_negative_score():
    from app.crashguard.services.pr_qa_agent import _normalize_parsed
    out = _normalize_parsed({"quality_score": -50, "verdict": "approve_ready"})
    assert out["quality_score"] == 0


def test_normalize_handles_non_numeric_score():
    from app.crashguard.services.pr_qa_agent import _normalize_parsed
    out = _normalize_parsed({"quality_score": "ninety", "verdict": "approve_ready"})
    assert out["quality_score"] == 0  # 非数字 fallback


# ─────────── run_post_pr_quality_check fails open ───────────

@pytest.mark.asyncio
async def test_run_post_pr_quality_empty_diff_returns_ok_false(monkeypatch):
    """gh pr diff 返回空 + 无 fallback → 返回 ok=False 但不抛"""
    from app.crashguard.services import pr_qa_agent
    monkeypatch.setattr(pr_qa_agent, "_gh_pr_diff", lambda *a, **kw: "")
    result = await pr_qa_agent.run_post_pr_quality_check(
        pr_url="https://github.com/x/y/pull/1",
        repo_slug="x/y", pr_number=1,
        root_cause="rc", fix_suggestion="fs", issue_stack="st",
    )
    assert result["ok"] is False
    assert "empty diff" in result["error"]


@pytest.mark.asyncio
async def test_run_post_pr_quality_falls_back_when_gh_diff_fails(monkeypatch):
    """gh pr diff 失败但有 fallback_diff → 用 fallback 继续；agent 仍失败时 fails open"""
    from app.crashguard.services import pr_qa_agent
    monkeypatch.setattr(pr_qa_agent, "_gh_pr_diff", lambda *a, **kw: "")

    # mock AgentOrchestrator → agent.analyze 不产出文件 → empty output 走 ok=False
    class _DummyAgent:
        async def analyze(self, workspace, prompt):
            return None
    class _DummyOrch:
        def select_agent(self, **_):
            return _DummyAgent()
    import app.services.agent_orchestrator as _ao
    monkeypatch.setattr(_ao, "AgentOrchestrator", lambda: _DummyOrch())

    result = await pr_qa_agent.run_post_pr_quality_check(
        pr_url="https://github.com/x/y/pull/2",
        repo_slug="x/y", pr_number=2,
        root_cause="rc", fix_suggestion="fs", issue_stack="st",
        fallback_diff="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
    )
    # diff 取到了 → 跑 agent → agent 没产出 → ok=False 但不抛
    assert result["ok"] is False
    assert "no output" in result["error"]


@pytest.mark.asyncio
async def test_run_post_pr_quality_returns_ok_false_when_internal_crash(monkeypatch):
    """fails open：内部任何异常都不抛，返回 ok=False"""
    from app.crashguard.services import pr_qa_agent

    def _boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(pr_qa_agent, "_gh_pr_diff", _boom)

    result = await pr_qa_agent.run_post_pr_quality_check(
        pr_url="https://github.com/x/y/pull/3",
        repo_slug="x/y", pr_number=3,
        root_cause="rc", fix_suggestion="fs", issue_stack="st",
    )
    assert result["ok"] is False
    assert "crashed" in result["error"]
