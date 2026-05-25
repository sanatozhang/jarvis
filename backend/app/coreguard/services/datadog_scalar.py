"""Datadog v2 scalar query 薄封装。

底层逻辑：dashboard widget 的 `requests[0]` 里有现成的 `queries` + `formulas` 数组，
直接喂给 `POST /api/v2/query/scalar` 就能拿到 scalar 值，避免自己拼 query string。

返回值：成功 → float；失败/无数据 → None。失败不抛异常（demo 阶段宽容）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from app.coreguard.config import get_coreguard_settings

logger = logging.getLogger("coreguard.datadog_scalar")

DEFAULT_TIMEOUT = 30.0


def _resolve_template_vars(queries: List[Dict[str, Any]], template_vars: Optional[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Dashboard widget 的 search.query 里有 `$os_name` `$version` 等占位符，
    scalar API 不认识这些 — 替换为实际值或删除。

    demo 阶段：默认全部替换为空字符串（即 union 全平台/全版本）。
    """
    tvars = template_vars or {}
    out: List[Dict[str, Any]] = []
    for q in queries:
        q2 = dict(q)
        search = q2.get("search")
        if isinstance(search, dict) and "query" in search:
            qstr = search["query"]
            for k, v in tvars.items():
                qstr = qstr.replace(f"${k}", v)
            # 默认把所有未替换的 $xxx 视为空
            for placeholder in ("$os_name", "$version", "$usr.id"):
                qstr = qstr.replace(placeholder, "")
            # 多空格合并
            qstr = " ".join(qstr.split())
            q2["search"] = {**search, "query": qstr}
        out.append(q2)
    return out


async def query_scalar(
    queries: List[Dict[str, Any]],
    formula: str,
    start_ms: int,
    end_ms: int,
    template_vars: Optional[Dict[str, str]] = None,
) -> Optional[float]:
    """POST /api/v2/query/scalar → 返回 formula 计算后的 scalar 值。

    queries: dashboard widget 的 requests[0].queries 数组
    formula: dashboard widget 的 requests[0].formulas[0].formula 字符串
    """
    s = get_coreguard_settings()
    if not s.datadog_api_key or not s.datadog_app_key:
        logger.warning("datadog keys not configured")
        return None

    resolved_queries = _resolve_template_vars(queries, template_vars)
    body = {
        "data": {
            "type": "scalar_request",
            "attributes": {
                "formulas": [{"formula": formula}],
                "from": int(start_ms),
                "to": int(end_ms),
                "queries": resolved_queries,
            }
        }
    }
    url = f"https://api.{s.datadog_site}/api/v2/query/scalar"
    headers = {
        "DD-API-KEY": s.datadog_api_key,
        "DD-APPLICATION-KEY": s.datadog_app_key,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code != 200:
                logger.warning("datadog scalar HTTP %s: %s", resp.status_code, resp.text[:500])
                return None
            data = resp.json()
    except Exception as e:
        logger.warning("datadog scalar request failed: %s", e)
        return None

    # v2 scalar 响应结构：data.attributes.columns[0].values[0]
    try:
        cols = data.get("data", {}).get("attributes", {}).get("columns", [])
        if not cols:
            return None
        values = cols[0].get("values", [])
        if not values:
            return None
        v = values[0]
        if v is None:
            return None
        return float(v)
    except Exception as e:
        logger.warning("datadog scalar parse failed: %s | data=%s", e, str(data)[:500])
        return None
