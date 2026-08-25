"""RuleEngine 的 platform 感知匹配单测（2026-08-25）。

背景：docs/modules/multi-platform-onboarding.md §6.1 发现的缺陷——issue.platform
从未传入 match_rules()/classify()，导致像 rules/mcp.md 这种"用户选了平台但正文
不一定提关键词"的规则，只能靠碰运气命中关键词。这里验证 triggers.platforms 能
在零关键词命中时独立驱动匹配，且不影响原有纯关键词匹配的行为。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models.schemas import Rule, RuleMeta, RuleTrigger
from app.services.rule_engine import RuleEngine


@pytest.fixture
def engine(tmp_path: Path) -> RuleEngine:
    # 用空目录构造，避免读取真实 backend/rules/*.md，规则手动注入 _rules。
    eng = RuleEngine(rules_dir=tmp_path)
    eng._rules = {
        "mcp": Rule(
            meta=RuleMeta(
                id="mcp",
                triggers=RuleTrigger(keywords=["list_files", "get_file"], platforms=["mcp"], priority=9),
            ),
            content="mcp rule",
        ),
        "bluetooth": Rule(
            meta=RuleMeta(
                id="bluetooth",
                triggers=RuleTrigger(keywords=["蓝牙", "bluetooth"], priority=5),
            ),
            content="bluetooth rule",
        ),
    }
    return eng


def test_platform_alone_matches_rule_with_zero_keyword_hits(engine: RuleEngine):
    """用户选了 MCP 平台，正文完全没提关键词——今天会落到 general，应该命中 mcp。"""
    rules = engine.match_rules("为什么最近拉取的数量不对", platform="mcp")
    assert [r.meta.id for r in rules] == ["mcp"]
    assert engine.classify("为什么最近拉取的数量不对", platform="mcp") == "mcp"


def test_platform_mismatch_does_not_force_match(engine: RuleEngine):
    """平台是 app（非 mcp），不应该被 mcp 规则的 platforms 字段误命中。"""
    rules = engine.match_rules("蓝牙设备连不上", platform="app")
    assert [r.meta.id for r in rules] == ["bluetooth"]


def test_keyword_matching_still_works_without_platform(engine: RuleEngine):
    """不传 platform（默认 ""）时，行为跟改动前一致——纯关键词匹配。"""
    assert engine.classify("蓝牙断连了") == "bluetooth"
    assert engine.classify("随便写点什么") == "general"


def test_platform_and_keyword_hits_combine_for_ranking(engine: RuleEngine):
    """平台命中和关键词命中会一起计入 hit_keywords 数量，用于跟其他规则比排名。"""
    rules = engine.match_rules("list_files 返回空", platform="mcp", max_rules=1)
    assert rules[0].meta.id == "mcp"
