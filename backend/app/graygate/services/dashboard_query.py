"""Graygate 取数内核 — Datadog 看板 JSON + 模板变量替换 + scalar 查询。

移植自 `~/.claude/skills/weekly-report-core-metrics-sync/scripts/sync.py`
（`strip_template_vars` / `build_scalar_payload` / `build_title_index`，均已在该
skill 用真实凭证验证过），并修复该文件的一个真实 bug（见 `strip_template_vars`
的 `process_tags` 注释）：Metrics 类型 widget（tag-list 语法，如
`p75:rum.measure.view.memory{service:...,env:...,$version}`）里独占一个 tag 片段
的裸 `$var`，原逻辑只替换值不补 `key:` 前缀，产出非法的 `{...,4.0.3*}` → Datadog
`/v2/query/scalar` 返回 400 `unable to parse`。Refresh Rate / Memory Usage /
Android ANR 三个 widget 都是这个语法，不修这个 bug 这三项每天会静默取数失败。

不要改动 `app.coreguard.services.datadog_scalar` —— hourly_watch 告警正在用它，
这里是独立的一份实现，改动互不影响。

`load_metrics_config()` 是 `graygate/metrics.yaml` 的唯一消费入口——把 11 项
指标的 cell_format / scale / not_applicable_platform 等字段解析成
`MetricsConfig`/`MetricSpec`，供 Step5（report_builder）渲染报告用。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import yaml

from app.graygate.config import get_graygate_settings

logger = logging.getLogger("graygate.dashboard_query")

_VAR_RE = re.compile(r"\$\w+(?:\.\w+)?")

# Bare RUM-search-style template vars ($var, not {tag:$var.value}) each map to
# a specific Datadog facet prefix — not always `@<name>`. 实测确认（见 brief）：
# version→"version", usr.id→"@usr.id", env→"env", service→"@service",
# os_version→"@os.version"（点，不是下划线）。看板没有 os_name 变量，平台完全靠
# $service 区分，这里保留 os_name 条目只是为了跟原 skill 的表整体对齐，不指望用到。
_BARE_TEMPLATE_PREFIX = {
    "env": "env",
    "version": "version",
    "service": "@service",
    "usr.id": "@usr.id",
    "os_version": "@os.version",
    "os_name": "@os.name",
}


def strip_template_vars(query: str, tv: Dict[str, str]) -> str:
    """Drop or substitute $var / $var.value placeholders.

    Two grammars seen in dashboards:
      RUM   search query:  '... env:production $os_name $version ...'
      Metric tag list:     'sum:foo{application.id:abc,os.name:$os_name.value,$version}'

    Strategy: when the variable's value is '*' or unset, remove any
    'key:value' fragment containing it; otherwise substitute.
    """

    def resolved(name: str) -> Optional[str]:
        val = tv.get(name, "*")
        return None if (val == "*" or not val) else val

    # Inside { ... } tag list: handle 'key:$var.value' or ',$var' or '$var,'
    def process_tags(tag_str: str) -> str:
        parts = [p.strip() for p in tag_str.split(",")]
        kept = []
        for p in parts:
            m = _VAR_RE.search(p)
            if not m:
                kept.append(p)
                continue
            var_token = m.group(0)
            var_name = var_token[1:].split(".")[0]
            r = resolved(var_name)
            if r is None:
                continue  # drop fragment entirely (wildcard/unset)
            if p.strip() == var_token:
                # 裸 $var 独占一个 tag 片段（Metrics tag-list 语法，如 `,$version`）—
                # 必须补回 key: 前缀，否则产出的 "4.0.3*" 是裸值，Datadog 400。
                # tag-list 里的 key 就是变量名本身（version/env/...），不是 RUM 的
                # `@`-前缀 facet 名，所以这里用 var_name 而非 _BARE_TEMPLATE_PREFIX。
                kept.append(f"{var_name}:{r}")
            else:
                # 片段本身已带 key（如 "os.name:$os_name.value"），原样替换值即可
                kept.append(p.replace(var_token, r))
        return ",".join([p for p in kept if p])

    # Replace tag-list interiors first. If every fragment was a template var
    # that resolved to "drop" (wildcard), the tag list is empty — emit `{*}`
    # rather than `{}` (rejected) or no braces (also rejected: Datadog's
    # metrics query grammar requires a scope expression when the metric name
    # is followed by `.as_count()`/similar modifiers).
    def repl_tags(m: re.Match) -> str:
        inner = process_tags(m.group(1))
        return "{" + (inner or "*") + "}"

    query = re.sub(r"\{([^{}]*)\}", repl_tags, query)

    # Then any remaining bare $var tokens (RUM-style). Each dashboard template
    # variable has its own Datadog facet prefix — it is NOT always `@<name>`.
    def repl_bare(m: re.Match) -> str:
        raw = m.group(0)[1:]
        var_name = raw[:-6] if raw.endswith(".value") else raw
        r = resolved(var_name)
        if r is None:
            return ""
        prefix = _BARE_TEMPLATE_PREFIX.get(var_name, f"@{var_name}")
        return f"{prefix}:{r}"

    query = _VAR_RE.sub(repl_bare, query)

    return re.sub(r"\s+", " ", query).strip()


def build_scalar_payload(
    widget_request: Dict[str, Any],
    tv: Dict[str, str],
    from_ms: int,
    to_ms: int,
) -> Dict[str, Any]:
    """Convert a dashboard widget's `requests[0]` into a /v2/query/scalar body."""
    queries: List[Dict[str, Any]] = []
    for q in widget_request.get("queries", []):
        q2 = json.loads(json.dumps(q))  # deep copy
        if "search" in q2 and "query" in q2["search"]:
            q2["search"]["query"] = strip_template_vars(q2["search"]["query"], tv)
        if isinstance(q2.get("query"), str):  # metrics-type query
            q2["query"] = strip_template_vars(q2["query"], tv)
        queries.append(q2)

    formulas = [{"formula": f["formula"]} for f in widget_request.get("formulas", [])]
    if not formulas and queries:
        formulas = [{"formula": queries[0]["name"]}]

    return {
        "data": {
            "type": "scalar_request",
            "attributes": {
                "formulas": formulas,
                "queries": queries,
                "from": from_ms,
                "to": to_ms,
            }
        }
    }


def build_title_index(widgets: List[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    """Map widget title → top-level index, for title-based widget lookup.

    A title that appears on 2+ widgets maps to None (ambiguous) instead of
    silently picking one — the caller must raise rather than guess. Untitled
    widgets are skipped."""
    idx: Dict[str, Optional[int]] = {}
    for i, w in enumerate(widgets):
        title = (w.get("definition") or {}).get("title")
        if not title:
            continue
        idx[title] = None if title in idx else i
    return idx


async def query_scalar(payload: Dict[str, Any]) -> Optional[float]:
    """POST /v2/query/scalar and extract the single scalar value.

    成功返回 float；失败（凭证缺失 / HTTP 非 200 / 无数据）返回 None，不抛异常
    （对齐 `coreguard/services/datadog_scalar.py` 的容错策略——单个指标取数失败
    不该打断整份报告的其它 87 次请求）。
    """
    s = get_graygate_settings()
    if not s.datadog_api_key or not s.datadog_app_key:
        logger.warning("datadog keys not configured")
        return None
    url = f"https://api.{s.datadog_site}/api/v2/query/scalar"
    headers = {
        "DD-API-KEY": s.datadog_api_key,
        "DD-APPLICATION-KEY": s.datadog_app_key,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.warning("scalar query HTTP %s: %s", resp.status_code, resp.text[:300])
                return None
            cols = resp.json().get("data", {}).get("attributes", {}).get("columns", [])
            if not cols or not cols[0].get("values"):
                return None
            v = cols[0]["values"][0]
            return float(v) if v is not None else None
    except Exception as e:
        logger.warning("scalar query failed: %s", e)
        return None


# 进程内缓存：同一 dashboard_id 一次运行只拉一次（照 coreguard/dashboard_loader.py
# 的 `_cached` 模式，这里按 dashboard_id 分 key，因为 graygate 未来可能同时服务
# 多个看板）。
_cached_dashboards: Dict[str, Dict[str, Any]] = {}


async def get_dashboard_json(dashboard_id: str) -> Dict[str, Any]:
    """GET /api/v1/dashboard/{id}，进程内缓存（同一 dashboard_id 只拉一次）。"""
    if dashboard_id in _cached_dashboards:
        return _cached_dashboards[dashboard_id]

    s = get_graygate_settings()
    if not s.datadog_api_key or not s.datadog_app_key:
        raise RuntimeError(
            "Datadog credentials not configured "
            "(GRAYGATE_DATADOG_API_KEY/APP_KEY or CRASHGUARD_DATADOG_API_KEY/APP_KEY)"
        )
    url = f"https://api.{s.datadog_site}/api/v1/dashboard/{dashboard_id}"
    headers = {
        "DD-API-KEY": s.datadog_api_key,
        "DD-APPLICATION-KEY": s.datadog_app_key,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        dj = resp.json()

    _cached_dashboards[dashboard_id] = dj
    return dj


async def get_metric_scalar(
    dashboard_json: Dict[str, Any],
    widget_title: str,
    template_vars: Dict[str, str],
    from_ms: int,
    to_ms: int,
) -> Optional[float]:
    """按标题定位 widget（标题不存在或歧义 → raise ValueError），组装 payload
    （含 tag-list 修复版 strip_template_vars），调 /v2/query/scalar，成功返回
    float，失败（HTTP 非 200 / 无数据）返回 None，不抛异常。

    函数名/参数顺序是 Step3（版本枚举后取数）/Step4/Step5 共同的取数入口，不要
    更改——后续任务的 dispatch brief 假定这个签名存在。

    标题匹配：先精确匹配；精确匹配不到时回退成"前缀匹配"——2026-08-23 事故：
    看板 owner 在 Datadog 上给 "Hang Rate (iOS only)" 追加了一段说明性后缀
    （"— 已排除 Background 挂起误报（...）"），metrics.yaml 里配的还是旧的
    精确标题，导致连续两天日报直接崩溃、飞书群什么都没收到。看板标题是
    Datadog owner 手动维护的，会被随时追加描述文字，前缀匹配能吸收这类编辑，
    metrics.yaml 只需要配"核心那一段"不用死记随时可能变的后缀。
    """
    widgets = dashboard_json.get("widgets", [])
    title_index = build_title_index(widgets)

    idx = title_index.get(widget_title)
    if idx is None and widget_title not in title_index:
        # 精确匹配不到 —— 试前缀匹配（只在恰好一个候选时才采用，避免瞎猜）。
        prefix_matches = [
            i for i, w in enumerate(widgets)
            if ((w.get("definition") or {}).get("title") or "").startswith(widget_title)
        ]
        if len(prefix_matches) == 1:
            idx = prefix_matches[0]
            logger.warning(
                "widget title %r matched by prefix only (dashboard title has drifted; "
                "consider updating metrics.yaml to the full current title)",
                widget_title,
            )
        elif len(prefix_matches) > 1:
            raise ValueError(
                f"widget title is ambiguous by prefix match (appears on {len(prefix_matches)} widgets): {widget_title!r}"
            )
        else:
            raise ValueError(f"widget title not found on dashboard: {widget_title!r}")
    if idx is None:
        raise ValueError(
            f"widget title is ambiguous (appears on 2+ widgets): {widget_title!r}"
        )

    widget_def = widgets[idx].get("definition") or {}
    requests_arr = widget_def.get("requests", [])
    if not requests_arr:
        return None

    payload = build_scalar_payload(requests_arr[0], template_vars, from_ms, to_ms)
    if not payload["data"]["attributes"]["queries"]:
        return None

    return await query_scalar(payload)


# ---------------------------------------------------------------------------
# metrics.yaml loader
# ---------------------------------------------------------------------------
#
# 11 项指标映射的白名单（cell_format / scale / not_applicable_platform 等），
# 风格照 `coreguard/services/dashboard_loader.py` 的 MetricConfig/MetricsConfig
# dataclass 做法，字段按 graygate/metrics.yaml 的实际 schema 命名（不是照搬
# coreguard 那套 tier/threshold/direction 字段——那是告警阈值用的，graygate 这份
# 是 Step5（report_builder）渲染 Markdown/飞书卡片要用的 cell_format 白名单）。


@dataclass
class MetricSpec:
    key: str
    # 单 widget 指标用 title；jank / home_render 这种双 widget 合一行的用
    # title_p75 + title_p90（两者与 title 互斥，由 metrics.yaml 决定用哪一组）。
    title: Optional[str] = None
    title_p75: Optional[str] = None
    title_p90: Optional[str] = None
    cell_format: str = ""
    scale: float = 1.0
    # "ios" / "android" —— 该指标概念在这个平台上不适用（如 Android ANR 之于
    # iOS），report_builder 渲染时应显示 "—（不适用）" 而不是查回来的 0.0000。
    not_applicable_platform: Optional[str] = None
    # SOTA 目标阈值，用于卡片颜色点判定（🟩达标/🟨接近/🟥未达标）；{op: ">="/"<=" , value: float}
    # 单 widget 指标用 target；双 widget（jank/home_render）用 target_p75/target_p90。
    # 无 target 的指标（如未设定的场景）不参与颜色判定，只显示数值。
    target: Optional[Dict[str, Any]] = None
    target_p75: Optional[Dict[str, Any]] = None
    target_p90: Optional[Dict[str, Any]] = None


@dataclass
class MetricsConfig:
    dashboard_id: str
    template_variables: Dict[str, str] = field(default_factory=dict)
    metrics: List[MetricSpec] = field(default_factory=list)

    def by_key(self, key: str) -> Optional[MetricSpec]:
        for m in self.metrics:
            if m.key == key:
                return m
        return None


def _metrics_yaml_path() -> Path:
    return Path(__file__).resolve().parent.parent / "metrics.yaml"


def load_metrics_config() -> MetricsConfig:
    """读取 `graygate/metrics.yaml`，解析 `defaults`（dashboard_id +
    template_variables）与 `metrics` 列表，每个 metric 的字段原样透传成
    `MetricSpec`（不丢字段）。

    这是 metrics.yaml 唯一的消费入口——Step5（report_builder，尚未实现）用它
    知道每个指标该用什么 `cell_format`、要不要标记"不适用"。
    """
    raw = yaml.safe_load(_metrics_yaml_path().read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}

    metrics = [
        MetricSpec(
            key=m["key"],
            title=m.get("title"),
            title_p75=m.get("title_p75"),
            title_p90=m.get("title_p90"),
            cell_format=m.get("cell_format", ""),
            scale=float(m.get("scale", 1.0)),
            not_applicable_platform=m.get("not_applicable_platform"),
            target=m.get("target"),
            target_p75=m.get("target_p75"),
            target_p90=m.get("target_p90"),
        )
        for m in raw.get("metrics", [])
    ]

    return MetricsConfig(
        dashboard_id=defaults.get("dashboard_id", ""),
        template_variables=dict(defaults.get("template_variables") or {}),
        metrics=metrics,
    )
