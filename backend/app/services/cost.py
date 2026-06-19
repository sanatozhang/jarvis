"""Token → USD 成本计算（仅 API 路径：condenser haiku / claude_api agent）。

claude_code CLI 走 `--output-format json` 直接拿 total_cost_usd，不经此模块。
定价表来自 config.yaml `pricing:` 段（每 Mtok USD）；未知模型 best-effort 返回 0.0。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# usage 字段 → 定价 key 映射（Anthropic API usage 字段名）
_TOKEN_FIELD_TO_RATE = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_input_tokens": "cache_read",
    "cache_creation_input_tokens": "cache_write",
}


def _load_pricing() -> Dict[str, Dict[str, float]]:
    """从 crashguard 无关的通用 config 读取 pricing 段；缺失则空表（→ 成本 0）。"""
    try:
        from app.config import get_settings
        return dict(getattr(get_settings(), "pricing", {}) or {})
    except Exception:
        return {}


def _sum_tokens(usage: Dict[str, Any]) -> int:
    return sum(int(usage.get(f, 0) or 0) for f in _TOKEN_FIELD_TO_RATE)


def build_usage_record(
    agent_usage: Dict[str, Any],
    agent_cost_usd: Optional[float],
    agent_cost_source: str,
    agent_model: str,
    condenser_usage: Dict[str, Any],
    condenser_model: str,
    pricing: Optional[Dict[str, Dict[str, float]]] = None,
) -> Dict[str, Any]:
    """聚合 agent + condenser 用量 → analyses 表落库字段。

    返回 {total_tokens, total_cost_usd, cost_source, usage_breakdown}。
    - agent 成本：claude_code CLI 直接给（agent_cost_usd 非空，source=cli_reported）；
      claude_api 等无现成 cost → 按定价表算（source=computed）。
    - condenser 成本：始终按定价表算（haiku，API only）。
    - cost_source：cli_reported > computed > partial（agent 无任何用量/成本时 partial）。
    """
    pricing = pricing if pricing is not None else _load_pricing()

    if agent_cost_usd is not None:
        agent_cost = float(agent_cost_usd)
        source = agent_cost_source or "cli_reported"
    elif agent_usage:
        agent_cost = compute_cost(agent_model, agent_usage, pricing=pricing)
        source = "computed"
    else:
        agent_cost = 0.0
        source = "partial"

    condenser_cost = compute_cost(condenser_model, condenser_usage, pricing=pricing) if condenser_usage else 0.0

    total_tokens = _sum_tokens(agent_usage or {}) + _sum_tokens(condenser_usage or {})
    total_cost = round(agent_cost + condenser_cost, 6)

    breakdown: Dict[str, Any] = {
        "agent": {**(agent_usage or {}), "cost_usd": round(agent_cost, 6), "source": source, "model": agent_model},
    }
    if condenser_usage:
        breakdown["condenser"] = {**condenser_usage, "cost_usd": round(condenser_cost, 6), "model": condenser_model}

    return {
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost,
        "cost_source": source,
        "usage_breakdown": breakdown,
    }


def compute_cost(
    model: str,
    usage: Dict[str, Any],
    pricing: Optional[Dict[str, Dict[str, float]]] = None,
) -> float:
    """按每 Mtok 单价把 usage 折算成 USD。

    usage: {input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens}
    pricing: {model: {input, output, cache_read, cache_write}}（每 Mtok USD）；None 时读 config。
    未知模型 / 空 usage → 0.0（best-effort，不抛错）。
    """
    rates = (pricing if pricing is not None else _load_pricing()).get(model)
    if not rates:
        return 0.0
    total = 0.0
    for field, rate_key in _TOKEN_FIELD_TO_RATE.items():
        tokens = int(usage.get(field, 0) or 0)
        if tokens:
            # cache_read/cache_write 缺定价时回退到 input 单价
            rate = rates.get(rate_key)
            if rate is None and rate_key in ("cache_read", "cache_write"):
                rate = rates.get("input", 0.0)
            total += tokens * float(rate or 0.0)
    return total / 1_000_000.0
