"""Graygate 用户数（distinct `@usr.id`）—— 大盘 / 主要版本 / 各版本。

2026-10-19 加：灰度日报原来只有会话数（`version_resolver` 顺手算的），没有总用户量。
用户数要 `cardinality(@usr.id)`，必须走 Formulas & Functions 的 scalar API
（`/api/v2/query/scalar`）——RUM aggregate API 的 cardinality 不收敛 filter，
crashguard 2026-05 实测过（见 `crashguard/services/datadog_client.py` User 维度一节）。

口径与 `version_resolver` 一致：`@type:session env:production @service:plaud_<平台>`。
用户数是**采样内**去重：RUM 端上按 session 采样，同一用户多个 session 被采中的概率
不是线性的，没法像会话数那样按采样率还原，采样 <100% 时展示端注明「偏低」。

任何失败都返回 None / 空 dict，不影响日报其余部分。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import httpx

from app.graygate.config import get_graygate_settings

logger = logging.getLogger("graygate.user_counts")

_PLATFORM_SERVICE = {"ios": "plaud_ios", "android": "plaud_android"}


def _version_clause(version: str) -> str:
    # 通配符不能加引号（version_resolver 实测）；具体 build 号加引号防 `-` 被当运算符
    if "*" in version:
        return f"version:{version}"
    return f'version:"{version}"'


async def _scalar(query: str, from_ms: int, to_ms: int,
                  group_by: Optional[List[str]] = None) -> Optional[Dict[str, int]]:
    s = get_graygate_settings()
    if not s.datadog_api_key or not s.datadog_app_key:
        return None
    body = {"data": {"type": "scalar_request", "attributes": {
        "formulas": [{"formula": "query1"}],
        "queries": [{
            "name": "query1",
            "data_source": "rum",
            "search": {"query": query},
            "indexes": ["*"],
            "group_by": [
                {"facet": f, "limit": 50,
                 "sort": {"order": "desc", "aggregation": "cardinality", "metric": "@usr.id"}}
                for f in (group_by or [])
            ],
            "compute": {"aggregation": "cardinality", "metric": "@usr.id"},
        }],
        "from": int(from_ms),
        "to": int(to_ms),
    }}}
    headers = {
        "DD-API-KEY": s.datadog_api_key,
        "DD-APPLICATION-KEY": s.datadog_app_key,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(f"https://api.{s.datadog_site}/api/v2/query/scalar",
                                     json=body, headers=headers)
        if resp.status_code != 200:
            logger.warning("user scalar HTTP %s: %s", resp.status_code, resp.text[:300])
            return None
        cols = resp.json().get("data", {}).get("attributes", {}).get("columns", []) or []
    except Exception as e:
        logger.warning("user scalar failed: %s", e)
        return None

    val_col = next((c for c in cols if c.get("type") == "number"), None)
    if not val_col:
        return {}
    values = val_col.get("values") or []
    group_cols = [c for c in cols if c.get("type") == "group"]
    if not group_cols:
        return {"": int(values[0])} if values else {}
    out: Dict[str, int] = {}
    gv = group_cols[0].get("values") or []
    for i, raw in enumerate(values):
        cell = gv[i] if i < len(gv) else None
        key = (cell[0] if cell else "") if isinstance(cell, list) else (cell or "")
        try:
            out[str(key)] = int(raw)
        except (TypeError, ValueError):
            continue
    return out


# 占比分母：同平台**全部 App 用户**（Flutter 3.x + Native 4.x，不限版本）。Flutter 是
# 单 service `plaud-flutter`，平台靠 @os.name 分（iOS 侧含 iPadOS）；2026-10-20 实测
# Flutter 也是 env:production、采样 100%。
_ALL_APP_OS = {"ios": "(iOS OR iPadOS)", "android": "Android"}


def all_app_query(platform: str) -> str:
    return (f"@type:session env:production ((@service:plaud-flutter @os.name:{_ALL_APP_OS[platform]})"
            f" OR @service:{_PLATFORM_SERVICE[platform]})")


async def count_all_app_users(platform: str, from_ms: int, to_ms: int) -> Optional[int]:
    """同平台 3.x + 4.x 去重用户数——一条查询统一去重，当天两代都用过的用户只算一次。"""
    rows = await _scalar(all_app_query(platform), from_ms, to_ms)
    return None if rows is None else rows.get("", 0)


def _base_query(platform: str) -> str:
    return f"@type:session env:production @service:{_PLATFORM_SERVICE[platform]}"


async def count_users(platform: str, version: str, from_ms: int, to_ms: int) -> Optional[int]:
    """某平台 + 版本（具体 build 或通配符如 `4.0.3*`）的 distinct 用户数。失败返回 None。"""
    rows = await _scalar(f"{_base_query(platform)} {_version_clause(version)}", from_ms, to_ms)
    if rows is None:
        return None
    return int(rows.get("", 0))


async def count_users_by_version(platform: str, version_pattern: str,
                                 from_ms: int, to_ms: int) -> Dict[str, int]:
    """`version_pattern` 下各版本的 distinct 用户数 {version: users}。失败返回 {}。"""
    rows = await _scalar(f"{_base_query(platform)} {_version_clause(version_pattern)}",
                         from_ms, to_ms, group_by=["version"])
    return {k: v for k, v in (rows or {}).items() if k}
