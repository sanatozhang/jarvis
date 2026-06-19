"""Tests for app.services.cost — token→USD computation for API-based agent paths."""
import pytest

# 每 Mtok 单价（USD）；与 config.yaml pricing 段同口径
PRICING = {
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
}


def test_compute_cost_basic_input_output():
    from app.services.cost import compute_cost
    # haiku: 1,000,000 input @ $1 + 200,000 output @ $5 = 1.0 + 1.0 = 2.0
    usage = {"input_tokens": 1_000_000, "output_tokens": 200_000}
    assert compute_cost("claude-haiku-4-5", usage, pricing=PRICING) == pytest.approx(2.0)


def test_compute_cost_includes_cache_tokens():
    from app.services.cost import compute_cost
    # opus: 1M cache_read @ $0.5 + 1M cache_creation @ $6.25 = 6.75
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
    }
    assert compute_cost("claude-opus-4-8", usage, pricing=PRICING) == pytest.approx(6.75)


def test_compute_cost_unknown_model_returns_zero():
    from app.services.cost import compute_cost
    # 未知模型无定价 → 0.0（best-effort，不抛错）
    assert compute_cost("gpt-5.3-codex", {"input_tokens": 999}, pricing=PRICING) == 0.0


def test_compute_cost_empty_usage_is_zero():
    from app.services.cost import compute_cost
    assert compute_cost("claude-haiku-4-5", {}, pricing=PRICING) == 0.0


# ── build_usage_record：聚合 agent + condenser → 落库字段 ──
def test_build_usage_record_cli_agent_plus_condenser():
    from app.services.cost import build_usage_record
    rec = build_usage_record(
        agent_usage={"input_tokens": 1000, "output_tokens": 500},
        agent_cost_usd=0.02,           # claude_code CLI 直接给
        agent_cost_source="cli_reported",
        agent_model="claude-code-cli",
        condenser_usage={"input_tokens": 1_000_000, "output_tokens": 0},  # haiku
        condenser_model="claude-haiku-4-5",
        pricing=PRICING,
    )
    # total_tokens = 1000+500 (agent) + 1_000_000 (condenser) = 1_001_500
    assert rec["total_tokens"] == 1_001_500
    # total_cost = 0.02 (cli) + 1.0 (1M haiku input @ $1) = 1.02
    assert rec["total_cost_usd"] == pytest.approx(1.02)
    assert rec["cost_source"] == "cli_reported"
    assert rec["usage_breakdown"]["agent"]["cost_usd"] == 0.02
    assert rec["usage_breakdown"]["condenser"]["cost_usd"] == pytest.approx(1.0)


def test_build_usage_record_api_agent_computes_cost():
    from app.services.cost import build_usage_record
    rec = build_usage_record(
        agent_usage={"input_tokens": 1_000_000, "output_tokens": 200_000},  # opus
        agent_cost_usd=None,            # claude_api 没现成 cost → 按定价表算
        agent_cost_source="",
        agent_model="claude-opus-4-8",
        condenser_usage={},
        condenser_model="claude-haiku-4-5",
        pricing=PRICING,
    )
    # opus: 1M@5 + 0.2M@25 = 5 + 5 = 10
    assert rec["total_cost_usd"] == pytest.approx(10.0)
    assert rec["cost_source"] == "computed"


def test_build_usage_record_no_usage_is_partial():
    from app.services.cost import build_usage_record
    rec = build_usage_record(
        agent_usage={}, agent_cost_usd=None, agent_cost_source="",
        agent_model="gpt-5.3-codex", condenser_usage={}, condenser_model="",
        pricing=PRICING,
    )
    assert rec["total_tokens"] == 0
    assert rec["total_cost_usd"] == 0.0
    assert rec["cost_source"] == "partial"
