"""_resolve_allowed_tools() 单测（2026-08-25）。

背景：docs/modules/multi-platform-onboarding.md §10 归因2——deep_analysis 模式给了
40 轮/30 条日志的预算，但工具白名单没有同步放宽，agent 反复尝试受限命令直到轮次耗尽，
最后诚实报低置信度而非编结论。修复是仅在 deep_analysis=True 时追加
provider.deep_analysis_extra_tools，非 deep 模式的白名单必须保持完全不变。

这个函数本身只做字符串列表合并，不关心工具名的具体拼法——但 config.yaml 里这些字符串
曾经全部写成 "Shell(cmd:*)"，而 claude CLI 认的工具名是 "Bash"（`claude --help` 的
官方示例是 `Bash(git *) Edit`）。已改成 "Bash(...)"，这里的测试值同步跟着改，避免示例
本身继续沿用错误语法误导后人。
"""
from __future__ import annotations

from app.config import AgentProviderConfig
from app.services.agent_orchestrator import _resolve_allowed_tools


def _provider(**overrides) -> AgentProviderConfig:
    base = dict(
        enabled=True,
        allowed_tools=["Read", "Grep", "Bash(grep:*)"],
        deep_analysis_extra_tools=["Bash(jq:*)"],
    )
    base.update(overrides)
    return AgentProviderConfig(**base)


def test_non_deep_analysis_keeps_base_whitelist_unchanged():
    provider = _provider()
    assert _resolve_allowed_tools(provider, deep_analysis=False) == ["Read", "Grep", "Bash(grep:*)"]


def test_deep_analysis_appends_extra_tools():
    provider = _provider()
    result = _resolve_allowed_tools(provider, deep_analysis=True)
    assert result == ["Read", "Grep", "Bash(grep:*)", "Bash(jq:*)"]


def test_deep_analysis_no_extra_tools_configured_is_noop():
    provider = _provider(deep_analysis_extra_tools=[])
    assert _resolve_allowed_tools(provider, deep_analysis=True) == ["Read", "Grep", "Bash(grep:*)"]


def test_deep_analysis_does_not_duplicate_already_allowed_tool():
    provider = _provider(allowed_tools=["Read", "Bash(jq:*)"], deep_analysis_extra_tools=["Bash(jq:*)"])
    result = _resolve_allowed_tools(provider, deep_analysis=True)
    assert result == ["Read", "Bash(jq:*)"]
