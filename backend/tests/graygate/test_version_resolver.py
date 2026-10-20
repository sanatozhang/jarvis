"""graygate.services.version_resolver 单测。

全部 mock httpx，不打真实 Datadog API。核心锁定点（详见 task-3-brief.md）：

- `top_version` 必须是 events 最大的 build——不是版本号数值最大的、也不是 buckets
  列表里第一个。构造的 mock 数据里刻意让"版本号最大"和"events 最大"是不同的
  build，且 events 最大的 build 不放在 buckets 列表第一位，确保测试真的锁住
  "按 events 取" 这个口径，而不是巧合通过。
- `total_events` 是该平台全部 build events 之和（含长尾），用于"大盘"占比分母。
- 单平台 HTTP 失败 → 返回空 PlatformVersions，不抛异常，且不影响另一个平台。
- buckets 为空列表 → top_version=None, top_version_events=0, total_events=0。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.graygate.services import version_resolver as vr


# ---------------------------------------------------------------------------
# Fakes for httpx.AsyncClient
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int = 200, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text

    def json(self) -> dict:
        return self._json_data


def _buckets_response(pairs: list[tuple[str, int]]) -> _FakeResponse:
    """Build a fake /v2/rum/analytics/aggregate response from (version, events) pairs."""
    return _FakeResponse(
        status_code=200,
        json_data={
            "data": {
                "buckets": [
                    {"by": {"version": version}, "computes": {"c0": events}}
                    for version, events in pairs
                ]
            }
        },
    )


def _make_fake_async_client_by_service(responses_by_service: dict[str, _FakeResponse]):
    """Route the fake POST response by the `@service:` token in the request query,
    so ios/android can be mocked independently regardless of call order."""

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            query = json["filter"]["query"]
            for service, response in responses_by_service.items():
                if f"@service:{service}" in query:
                    return response
            raise AssertionError(f"no fake response configured for query: {query}")

    return _FakeAsyncClient


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    fake = SimpleNamespace(
        datadog_api_key="test-key",
        datadog_app_key="test-app-key",
        datadog_site="datadoghq.com",
        version_pattern="4.0.3*",
    )
    monkeypatch.setattr(vr, "get_graygate_settings", lambda: fake)


# ---------------------------------------------------------------------------
# Normal case: top_version picked by events, not by version string, not by
# list order.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_versions_top_version_is_events_max_not_version_max_not_first(monkeypatch):
    # iOS: buckets deliberately NOT sorted by events, and the highest-numbered
    # version string (4.0.399-9999) has the fewest events — if the code picked
    # "first in list" or "max version string" it would get the wrong answer.
    ios_pairs = [
        ("4.0.302-1050", 500),      # first in list, but not the events max
        ("4.0.301-1038", 176866),  # events max — should be top_version
        ("4.0.399-9999", 3),       # highest version string, negligible events
    ]
    android_pairs = [
        ("4.0.305-2000", 10),
        ("4.0.301-1043", 28054),  # events max
        ("4.0.301-1040", 500),
    ]

    fake_client_cls = _make_fake_async_client_by_service(
        {
            "plaud_ios": _buckets_response(ios_pairs),
            "plaud_android": _buckets_response(android_pairs),
        }
    )
    monkeypatch.setattr(vr.httpx, "AsyncClient", fake_client_cls)

    result = await vr.resolve_versions(from_ms=1000, to_ms=2000)

    assert set(result.keys()) == {"ios", "android"}

    ios = result["ios"]
    assert ios.platform == "ios"
    assert ios.top_version == "4.0.301-1038"
    assert ios.top_version_events == 176866
    assert ios.total_events == 500 + 176866 + 3
    # versions sorted descending by events
    assert ios.versions == [
        ("4.0.301-1038", 176866),
        ("4.0.302-1050", 500),
        ("4.0.399-9999", 3),
    ]

    android = result["android"]
    assert android.platform == "android"
    assert android.top_version == "4.0.301-1043"
    assert android.top_version_events == 28054
    assert android.total_events == 10 + 28054 + 500
    assert android.versions == [
        ("4.0.301-1043", 28054),
        ("4.0.301-1040", 500),
        ("4.0.305-2000", 10),
    ]


# ---------------------------------------------------------------------------
# HTTP failure on one platform must not affect the other.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_versions_ios_failure_does_not_affect_android(monkeypatch):
    android_pairs = [("4.0.301-1043", 28054), ("4.0.301-1040", 500)]
    fake_client_cls = _make_fake_async_client_by_service(
        {
            "plaud_ios": _FakeResponse(status_code=500, text="internal error"),
            "plaud_android": _buckets_response(android_pairs),
        }
    )
    monkeypatch.setattr(vr.httpx, "AsyncClient", fake_client_cls)

    result = await vr.resolve_versions(from_ms=1000, to_ms=2000)

    ios = result["ios"]
    assert ios.platform == "ios"
    assert ios.versions == []
    assert ios.top_version is None
    assert ios.top_version_events == 0
    assert ios.total_events == 0

    android = result["android"]
    assert android.top_version == "4.0.301-1043"
    assert android.top_version_events == 28054
    assert android.total_events == 28054 + 500


@pytest.mark.asyncio
async def test_resolve_versions_android_failure_does_not_affect_ios(monkeypatch):
    ios_pairs = [("4.0.301-1038", 176866), ("4.0.302-1050", 500)]
    fake_client_cls = _make_fake_async_client_by_service(
        {
            "plaud_ios": _buckets_response(ios_pairs),
            "plaud_android": _FakeResponse(status_code=400, text="bad request"),
        }
    )
    monkeypatch.setattr(vr.httpx, "AsyncClient", fake_client_cls)

    result = await vr.resolve_versions(from_ms=1000, to_ms=2000)

    ios = result["ios"]
    assert ios.top_version == "4.0.301-1038"
    assert ios.top_version_events == 176866
    assert ios.total_events == 176866 + 500

    android = result["android"]
    assert android.versions == []
    assert android.top_version is None
    assert android.top_version_events == 0
    assert android.total_events == 0


# ---------------------------------------------------------------------------
# Empty buckets.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_versions_empty_buckets(monkeypatch):
    fake_client_cls = _make_fake_async_client_by_service(
        {
            "plaud_ios": _buckets_response([]),
            "plaud_android": _buckets_response([]),
        }
    )
    monkeypatch.setattr(vr.httpx, "AsyncClient", fake_client_cls)

    result = await vr.resolve_versions(from_ms=1000, to_ms=2000)

    for platform in ("ios", "android"):
        pv = result[platform]
        assert pv.versions == []
        assert pv.top_version is None
        assert pv.top_version_events == 0
        assert pv.total_events == 0


# ---------------------------------------------------------------------------
# resolve_extra_version：人工指定的 build 不在 version_pattern 内时单独补查
# （2026-10-20 pattern=4.0.3*、指定 4.0.1-1268 → 会话数取成 0 全部样本不足）
# ---------------------------------------------------------------------------

def _make_recording_client(response: _FakeResponse, queries: list):
    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            queries.append(json["filter"]["query"])
            return response

    return _FakeAsyncClient


@pytest.mark.asyncio
async def test_resolve_extra_version_fills_out_of_pattern_version(monkeypatch):
    queries: list = []
    monkeypatch.setattr(vr.httpx, "AsyncClient",
                        _make_recording_client(_buckets_response([("4.0.1-1268", 812)]), queries))
    pv = vr.PlatformVersions(platform="ios", versions=[("4.0.301-1043", 1000)],
                             top_version="4.0.301-1043", top_version_events=1000, total_events=1000)

    await vr.resolve_extra_version(pv, "4.0.1-1268", 0, 1)

    assert pv.sessions("4.0.1-1268") == 812
    assert pv.est_sessions("4.0.1-1268") == 812
    assert 'version:"4.0.1-1268"' in queries[0]
    # 不污染大盘：versions / total / 全部版本合计都不变
    assert pv.versions == [("4.0.301-1043", 1000)]
    assert pv.total_events == 1000
    assert pv.est_sessions() == 1000


@pytest.mark.asyncio
async def test_resolve_extra_version_noop_when_already_enumerated(monkeypatch):
    queries: list = []
    monkeypatch.setattr(vr.httpx, "AsyncClient",
                        _make_recording_client(_buckets_response([]), queries))
    pv = vr.PlatformVersions(platform="ios", versions=[("4.0.301-1043", 1000)])

    await vr.resolve_extra_version(pv, "4.0.301-1043", 0, 1)

    assert queries == []
    assert pv.sessions("4.0.301-1043") == 1000


@pytest.mark.asyncio
async def test_resolve_extra_version_failure_keeps_zero(monkeypatch):
    queries: list = []
    monkeypatch.setattr(vr.httpx, "AsyncClient",
                        _make_recording_client(_FakeResponse(status_code=500, text="boom"), queries))
    pv = vr.PlatformVersions(platform="ios", versions=[("4.0.301-1043", 1000)])

    await vr.resolve_extra_version(pv, "4.0.1-1268", 0, 1)

    assert pv.sessions("4.0.1-1268") == 0
    assert pv.extra == {}


@pytest.mark.asyncio
async def test_total_sessions_restores_sampling_without_version_grouping(monkeypatch):
    """占比分母：不按 version 分组（4.* 可能超过 30 个 build），总数 + 按采样率还原。"""
    seen: list = []

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            seen.append(json)
            if "group_by" not in json:
                return _FakeResponse(200, {"data": {"buckets": [{"by": {}, "computes": {"c0": 130}}]}})
            return _FakeResponse(200, {"data": {"buckets": [
                {"by": {"@_dd.configuration.session_sample_rate": 20}, "computes": {"c0": 100}},
                {"by": {"@_dd.configuration.session_sample_rate": 100}, "computes": {"c0": 10}},
            ]}})

    monkeypatch.setattr(vr.httpx, "AsyncClient", _FakeAsyncClient)
    n = await vr.total_sessions("@type:session Q", 0, 1)
    assert n == 500 + 10 + 20          # 100/0.2 + 10 + 未带 rate 的 20 按 100%
    assert seen[0]["filter"]["query"] == "@type:session Q"
    assert all(g["facet"] != "version" for j in seen for g in j.get("group_by", []))
