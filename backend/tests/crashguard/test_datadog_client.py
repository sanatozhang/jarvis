"""DatadogClient 测试（mock SDK 调用层）"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import List, Optional

import pytest

from datadog_api_client.exceptions import ApiException


def _make_response(items: List[dict]) -> SimpleNamespace:
    """构造一个模拟 IssuesSearchResponse:
    - data: list of search_result（attributes=metric, relationships.issue.data.id 指向 issue）
    - included: list of Issue 对象
    """
    data = []
    included = []
    for it in items:
        issue_id = it["id"]
        data.append(SimpleNamespace(
            id=f"sr-{issue_id}",
            type="error_tracking_search_result",
            attributes=SimpleNamespace(
                total_count=it.get("events_count", 0),
                impacted_users=it.get("users_affected", 0),
                impacted_sessions=it.get("impacted_sessions", 0),
            ),
            relationships=SimpleNamespace(
                issue=SimpleNamespace(data=SimpleNamespace(id=issue_id))
            ),
        ))
        included.append(SimpleNamespace(
            id=issue_id,
            type="issue",
            attributes=SimpleNamespace(
                error_type=it.get("error_type", ""),
                error_message=it.get("error_message", ""),
                file_path=it.get("file_path", ""),
                function_name=it.get("function_name", ""),
                first_seen=it.get("first_seen", 0),
                last_seen=it.get("last_seen", 0),
                first_seen_version=it.get("first_seen_version", ""),
                last_seen_version=it.get("last_seen_version", ""),
                platform=it.get("platform", ""),
                service=it.get("service", ""),
            ),
        ))
    return SimpleNamespace(data=data, included=included)


def _make_api_exception(status: int, reason: str = "") -> ApiException:
    e = ApiException(status=status, reason=reason)
    return e


@pytest.mark.asyncio
async def test_list_issues_returns_normalized_dicts(monkeypatch):
    """单次 search 调用：返回展平后的 dict（id + attributes 平铺）"""
    from app.crashguard.services.datadog_client import DatadogClient

    response = _make_response([{
        "id": "abc123",
        "events_count": 145,
        "users_affected": 23,
        "error_type": "NullPointerException",
        "error_message": "null reference at AudioPlayer",
        "function_name": "AudioPlayer.play",
        "file_path": "lib/audio/player.dart",
        "first_seen": 1714003200000,
        "last_seen": 1714176000000,
        "first_seen_version": "1.4.7",
        "last_seen_version": "1.4.7",
        "platform": "flutter",
        "service": "plaud_ai",
    }])

    def fake_sync(self, body):
        return response

    monkeypatch.setattr(DatadogClient, "_sync_search", fake_sync)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    issues = await client.list_issues(window_hours=24)
    assert len(issues) == 1
    raw = issues[0]
    assert raw["id"] == "abc123"
    assert raw["type"] == "error_tracking_issue"
    attrs = raw["attributes"]
    assert attrs["title"] == "NullPointerException @ AudioPlayer.play"
    assert attrs["service"] == "plaud_ai"
    assert attrs["platform"] == "flutter"
    assert attrs["events_count"] == 145
    assert attrs["users_affected"] == 23
    assert attrs["first_seen_version"] == "1.4.7"
    assert attrs["first_seen_timestamp"] == 1714003200000
    assert "AudioPlayer.play" in attrs["stack_trace"]


@pytest.mark.asyncio
async def test_list_issues_handles_multiple_results(monkeypatch):
    """多个 search_result 都正确合并 included"""
    from app.crashguard.services.datadog_client import DatadogClient

    response = _make_response([
        {"id": "abc123", "events_count": 100, "error_type": "NPE", "platform": "flutter", "service": "plaud_ai"},
        {"id": "def456", "events_count": 50, "error_type": "OOM", "platform": "ios", "service": "plaud_ai"},
    ])

    def fake_sync(self, body):
        return response

    monkeypatch.setattr(DatadogClient, "_sync_search", fake_sync)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    issues = await client.list_issues(window_hours=24)
    assert [i["id"] for i in issues] == ["abc123", "def456"]
    assert issues[0]["attributes"]["events_count"] == 100
    assert issues[1]["attributes"]["platform"] == "ios"


@pytest.mark.asyncio
async def test_list_issues_retries_on_5xx(monkeypatch):
    """5xx 重试 3 次后成功"""
    from app.crashguard.services.datadog_client import DatadogClient
    from app.crashguard.services import datadog_client as dd

    call_count = {"n": 0}
    final_response = _make_response([{
        "id": "ok",
        "events_count": 1,
        "platform": "flutter",
        "service": "plaud_ai",
    }])

    def fake_sync(self, body):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise dd._RetryableSDKError(_make_api_exception(503, "Service Unavailable"))
        return final_response

    async def fake_sleep(_):
        return

    monkeypatch.setattr(DatadogClient, "_sync_search", fake_sync)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    issues = await client.list_issues(window_hours=24)
    assert len(issues) == 1
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_rate_limit_circuit_breaker(monkeypatch):
    """5 次 429 → 第 6 次抛 CircuitBreakerOpen"""
    from app.crashguard.services.datadog_client import (
        DatadogClient,
        DatadogRateLimitError,
        CircuitBreakerOpen,
    )

    def fake_sync(self, body):
        self._record_rate_limit_event()
        raise DatadogRateLimitError("429")

    monkeypatch.setattr(DatadogClient, "_sync_search", fake_sync)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")

    for _ in range(5):
        with pytest.raises(DatadogRateLimitError):
            await client.list_issues(window_hours=24)

    with pytest.raises(CircuitBreakerOpen):
        await client.list_issues(window_hours=24)


@pytest.mark.asyncio
async def test_list_issues_propagates_unhandled_status(monkeypatch):
    """非 429 / 非 5xx 错误（例如 401 鉴权）不重试，直接抛"""
    from app.crashguard.services.datadog_client import DatadogClient

    def fake_sync(self, body):
        raise _make_api_exception(401, "Unauthorized")

    async def fake_sleep(_):
        return

    monkeypatch.setattr(DatadogClient, "_sync_search", fake_sync)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    with pytest.raises(ApiException) as exc_info:
        await client.list_issues(window_hours=24)
    assert exc_info.value.status == 401


def test_normalize_issue_payload():
    """Datadog 原始响应 → 内部统一结构"""
    from app.crashguard.services.datadog_client import normalize_issue
    raw = {
        "id": "abc123",
        "type": "error_tracking_issue",
        "attributes": {
            "title": "NullPointerException @ play",
            "service": "plaud_ai",
            "platform": "flutter",
            "first_seen_timestamp": 1714003200000,
            "last_seen_timestamp": 1714176000000,
            "first_seen_version": "1.4.7",
            "last_seen_version": "1.4.7",
            "events_count": 145,
            "users_affected": 23,
            "stack_trace": "NullPointerException\n  at A.x\n  at B.y",
            "tags": {"env": "prod"},
        },
    }
    norm = normalize_issue(raw)
    assert norm["datadog_issue_id"] == "abc123"
    assert norm["title"] == "NullPointerException @ play"
    assert norm["platform"] == "flutter"
    assert norm["service"] == "plaud_ai"
    assert norm["events_count"] == 145
    assert norm["users_affected"] == 23
    assert norm["first_seen_version"] == "1.4.7"
    assert norm["stack_trace"].startswith("NullPointerException")
    assert norm["tags"] == {"env": "prod"}
    assert norm["first_seen_at"].year == 2024  # 2024-04-25 unix ms


def test_normalize_handles_missing_fields():
    """缺失字段不报错，给默认值"""
    from app.crashguard.services.datadog_client import normalize_issue
    raw = {"id": "xxx", "attributes": {}}
    norm = normalize_issue(raw)
    assert norm["datadog_issue_id"] == "xxx"
    assert norm["title"] == ""
    assert norm["platform"] == ""
    assert norm["events_count"] == 0


# ---------------------------------------------------------------------------
# top_user_version_by_platform — RUM cardinality(@usr.id) group_by (os, version)
# ---------------------------------------------------------------------------


def _make_aggregate_response(buckets: List[dict]) -> SimpleNamespace:
    """模拟 RUMAnalyticsAggregateResponse:
    buckets[i] = {"by": {"@os.name": "...", "version": "..."}, "users": int}
    """
    bucket_objs = []
    for b in buckets:
        bucket_objs.append(SimpleNamespace(
            by=b["by"],
            computes={"c0": b["users"]},
        ))
    return SimpleNamespace(data=SimpleNamespace(buckets=bucket_objs))


@pytest.mark.asyncio
async def test_top_user_version_buckets_android_and_ios(monkeypatch):
    """基本场景：Android / iOS 各取自己平台用户量最大的版本"""
    from app.crashguard.services.datadog_client import DatadogClient

    resp = _make_aggregate_response([
        {"by": {"@os.name": "Android", "version": "3.17.0"}, "users": 1000},
        {"by": {"@os.name": "Android", "version": "3.16.0"}, "users": 500},
        {"by": {"@os.name": "iOS",     "version": "3.17.0"}, "users": 800},
        {"by": {"@os.name": "iOS",     "version": "3.15.0"}, "users": 900},
    ])
    monkeypatch.setattr(
        DatadogClient, "_sync_top_user_version_by_platform",
        lambda self, wh: DatadogClient._sync_top_user_version_by_platform.__wrapped__(self, wh) if False else _parse_resp(resp),
    )

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    out = await client.top_user_version_by_platform(window_hours=24)
    assert out["android"] == {"version": "3.17.0", "users": 1000}
    assert out["ios"] == {"version": "3.15.0", "users": 900}


def _parse_resp(resp: SimpleNamespace) -> dict:
    """复制 _sync_top_user_version_by_platform 的解析逻辑，便于纯 mock 不打 SDK。
    facet 名必须和 production 代码一致：@os.name × version。"""
    agg = {"android": {}, "ios": {}}
    for b in resp.data.buckets:
        os_name = (b.by.get("@os.name") or "").strip().lower()
        version = (b.by.get("version") or "").strip()
        if not version:
            continue
        try:
            users = int(next(iter(b.computes.values())))
        except (StopIteration, TypeError, ValueError):
            users = 0
        if users <= 0:
            continue
        if os_name.startswith("ipados") or os_name.startswith("ios") or "iphone" in os_name:
            key = "ios"
        elif os_name.startswith("android"):
            key = "android"
        else:
            continue
        agg[key][version] = agg[key].get(version, 0) + users
    out = {}
    for platform, versions in agg.items():
        if not versions:
            continue
        top_ver, top_users = max(versions.items(), key=lambda kv: kv[1])
        out[platform] = {"version": top_ver, "users": top_users}
    return out


@pytest.mark.asyncio
async def test_top_user_version_normalizes_ipados_and_iphone_as_ios(monkeypatch):
    """iPadOS / iPhone OS 字面都归 iOS 桶"""
    from app.crashguard.services.datadog_client import DatadogClient

    resp = _make_aggregate_response([
        {"by": {"@os.name": "iPadOS",   "version": "3.17.0"}, "users": 300},
        {"by": {"@os.name": "iPhone OS","version": "3.17.0"}, "users": 500},
        {"by": {"@os.name": "iOS",      "version": "3.16.0"}, "users": 100},
    ])
    monkeypatch.setattr(
        DatadogClient, "_sync_top_user_version_by_platform",
        lambda self, wh: _parse_resp(resp),
    )

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    out = await client.top_user_version_by_platform(window_hours=24)
    # iOS 桶应聚合：3.17.0 = 300+500=800，3.16.0 = 100；取 3.17.0
    assert out["ios"] == {"version": "3.17.0", "users": 800}
    assert "android" not in out


@pytest.mark.asyncio
async def test_top_user_version_skips_unknown_os(monkeypatch):
    """Windows / Linux 之类 OS 不入桶，结果只含 android/ios（如存在）"""
    from app.crashguard.services.datadog_client import DatadogClient

    resp = _make_aggregate_response([
        {"by": {"@os.name": "Windows", "version": "1.0.0"}, "users": 100},
        {"by": {"@os.name": "Linux",   "version": "1.0.0"}, "users": 200},
        {"by": {"@os.name": "Android", "version": "3.17.0"}, "users": 50},
    ])
    monkeypatch.setattr(
        DatadogClient, "_sync_top_user_version_by_platform",
        lambda self, wh: _parse_resp(resp),
    )

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    out = await client.top_user_version_by_platform(window_hours=24)
    assert "android" in out and out["android"]["version"] == "3.17.0"
    assert "ios" not in out
    assert "other" not in out


@pytest.mark.asyncio
async def test_top_user_version_returns_empty_when_sync_fails(monkeypatch):
    """SDK 异常应被异步包装层吞掉返回 {}（不致命）"""
    from app.crashguard.services.datadog_client import DatadogClient

    def boom(self, wh):
        raise RuntimeError("simulated SDK failure")

    monkeypatch.setattr(DatadogClient, "_sync_top_user_version_by_platform", boom)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    out = await client.top_user_version_by_platform(window_hours=24)
    assert out == {}


@pytest.mark.asyncio
async def test_top_user_version_skips_empty_version_and_zero_users(monkeypatch):
    """version 为空或 users<=0 的 bucket 不计入"""
    from app.crashguard.services.datadog_client import DatadogClient

    resp = _make_aggregate_response([
        {"by": {"@os.name": "Android", "version": ""},        "users": 999},   # 空版本，跳过
        {"by": {"@os.name": "Android", "version": "3.17.0"},  "users": 0},     # 0 用户，跳过
        {"by": {"@os.name": "Android", "version": "3.16.0"},  "users": 10},
    ])
    monkeypatch.setattr(
        DatadogClient, "_sync_top_user_version_by_platform",
        lambda self, wh: _parse_resp(resp),
    )

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    out = await client.top_user_version_by_platform(window_hours=24)
    assert out["android"] == {"version": "3.16.0", "users": 10}


# ---------------------------------------------------------------------------
# Service filter injection — env:production whitelist for native platforms
# ---------------------------------------------------------------------------


def test_inject_service_prepends_env_production_filter():
    """env:production filter 在注入 query 时被正确包含"""
    from app.crashguard.services.datadog_client import DatadogClient

    client = DatadogClient(
        api_key="x", app_key="y",
        service_filter=(
            "(service:plaud-flutter OR (service:plaud_android AND env:production) "
            "OR (service:plaud_ios AND env:production))"
        ),
    )
    injected = client._inject_service("@error.is_crash:true")
    assert "env:production" in injected
    assert injected.startswith("(service:plaud-flutter")
    assert injected.endswith("@error.is_crash:true")


def test_inject_service_empty_filter_is_debug_escape_hatch():
    """空的 service_filter 是 debug 逃生口，不注入"""
    from app.crashguard.services.datadog_client import DatadogClient

    client = DatadogClient(api_key="x", app_key="y", service_filter="")
    assert client._inject_service("@type:error") == "@type:error"


# ---------------------------------------------------------------------------
# get_issue_detail — 符号化失败不应被静默吞掉、也不应被误判为"已完成"
#
# 2026-07-20 背景：102 上实测确认，即便找到了 RUM 事件（frame_buckets 非空、
# 分布字段能正常写入），如果末尾 symbolicate_stack() 抛异常（比如 GH_TOKEN 403），
# 原代码 `except Exception: symbolicated = best_stack` 会静默吞掉、不留日志；
# 而且返回的 stack_quality 是用符号化前的 best_stack 算的，永远不反映符号化
# 是否真的生效。下游 distribution_prewarmer 只看 top_os 是否有值就判定"已完成"，
# 于是这批"找到事件但符号化失败"的 issue 被永久标记为成功，代表性堆栈却停留在
# 原始地址，再也没有重新符号化的机会。
# ---------------------------------------------------------------------------

def _make_rum_event(stack: str, *, os_name: str = "ios", app_version: str = "3.18.0-708",
                     binary_images: Optional[list] = None) -> SimpleNamespace:
    inner = {
        "error": {"stack": stack, "binary_images": binary_images or []},
        "os": {"name": os_name},
        "application": {"version": app_version},
    }
    return SimpleNamespace(
        attributes=SimpleNamespace(attributes=inner, _data_store={}, timestamp=1700000000000),
    )


@pytest.mark.asyncio
async def test_get_issue_detail_logs_and_reports_when_symbolication_raises(monkeypatch, caplog):
    from app.crashguard.services.datadog_client import DatadogClient

    raw_stack = "0   App   0x0000000112fec700 0x11214c000 + 15337216"
    event = _make_rum_event(raw_stack)

    client = DatadogClient(api_key="x", app_key="y")
    monkeypatch.setattr(DatadogClient, "_sync_search_rum_events", lambda self, *a, **k: [event])

    async def _boom(*a, **kw):
        raise RuntimeError("GH_TOKEN 403 (simulated)")

    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_stack", _boom,
    )

    import logging
    caplog.set_level(logging.WARNING, logger="crashguard.datadog_client")

    detail = await client.get_issue_detail("issue-raw-ios")

    assert detail is not None
    # 符号化失败时原样返回未解析栈——这是既有容错行为，不应改变
    assert detail["full_stack"] == raw_stack
    # 但必须留痕，不能静默吞掉
    assert any("symbolicat" in rec.message.lower() for rec in caplog.records)
    # stack_quality 反映符号化后的真实质量（原始地址栈 → "raw"），而不是符号化前的快照
    assert detail["stack_quality"] == "raw"


@pytest.mark.asyncio
async def test_get_issue_detail_stack_quality_reflects_successful_symbolication(monkeypatch):
    """对照组：符号化成功时 stack_quality 应反映符号化后的结果（symbolicated_native）。"""
    from app.crashguard.services.datadog_client import DatadogClient

    raw_stack = "0   App   0x0000000112fec700 0x11214c000 + 15337216"
    symbolicated_stack = "0   App   -[PLRecordManager stopRecording] PLRecordManager.swift:120"
    event = _make_rum_event(raw_stack)

    client = DatadogClient(api_key="x", app_key="y")
    monkeypatch.setattr(DatadogClient, "_sync_search_rum_events", lambda self, *a, **k: [event])

    async def _fake_symbolicate(*a, **kw):
        return symbolicated_stack

    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_stack", _fake_symbolicate,
    )

    detail = await client.get_issue_detail("issue-fixed-ios")

    assert detail["full_stack"] == symbolicated_stack
    assert detail["stack_quality"] == "symbolicated_native"
