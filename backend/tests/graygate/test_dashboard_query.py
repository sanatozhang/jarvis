"""graygate.services.dashboard_query 单测。

全部 mock httpx，不打真实 Datadog API。重点锁定 brief 里点名的核心回归：Metrics
类型 widget（tag-list 语法）里独占一个 tag 片段的裸 $version 必须补回 key: 前缀，
否则 Datadog 会 400（Refresh Rate / Memory Usage / Android ANR 三个 widget 都是
这个语法）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.graygate.services import dashboard_query as dq


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

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _make_fake_async_client(response: _FakeResponse):
    """Build a fake httpx.AsyncClient class whose get/post always return `response`."""

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            return response

        async def get(self, url, headers=None):
            return response

    return _FakeAsyncClient


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    """Dummy Datadog creds so query_scalar / get_dashboard_json don't short-circuit
    on 'not configured' before we even get to exercise the httpx mocking."""
    fake = SimpleNamespace(
        datadog_api_key="test-key",
        datadog_app_key="test-app-key",
        datadog_site="datadoghq.com",
    )
    monkeypatch.setattr(dq, "get_graygate_settings", lambda: fake)


@pytest.fixture(autouse=True)
def clear_dashboard_cache():
    dq._cached_dashboards.clear()
    yield
    dq._cached_dashboards.clear()


# ---------------------------------------------------------------------------
# strip_template_vars
# ---------------------------------------------------------------------------

def test_strip_template_vars_rum_bare_version_replaced():
    """RUM search.query 类型：裸 $version 在花括号外 → 直接替换为 version:<val>。"""
    query = "env:production $service $version"
    tv = {"env": "production", "service": "plaud_ios", "version": "4.0.3*"}
    out = dq.strip_template_vars(query, tv)
    assert "version:4.0.3*" in out
    assert "@service:plaud_ios" in out
    assert "$version" not in out
    assert "$service" not in out


def test_strip_template_vars_metrics_taglist_bare_version_gets_key_prefix():
    """Metrics tag-list 类型：裸 $version 独占一个 tag 片段 → 必须补 key 前缀。

    这是本任务修复的核心 bug（Refresh Rate / Memory Usage / Android ANR 三个
    widget 都是这个语法），回归必须锁定，否则这三项会静默变成取数失败。
    """
    query = "p75:rum.measure.view.memory{service:plaud_ios,env:production,$version}"
    tv = {"env": "production", "service": "plaud_ios", "version": "4.0.3*"}
    out = dq.strip_template_vars(query, tv)
    assert "version:4.0.3*" in out
    # the original bug: bare value with no key, e.g. "...,4.0.3*}"
    assert ",4.0.3*}" not in out
    assert "$version" not in out


def test_strip_template_vars_metrics_taglist_wildcard_dropped_cleanly():
    """裸变量解析为 '*'（未指定该维度过滤）→ 整个 tag 片段被丢弃，不留逗号残留。"""
    query = "sum:foo{service:plaud_ios,$os_version}"
    tv = {"service": "plaud_ios", "os_version": "*"}
    out = dq.strip_template_vars(query, tv)
    assert "os_version" not in out
    assert "os.version" not in out
    assert ",," not in out
    assert ",}" not in out


def test_strip_template_vars_keyed_fragment_substitutes_value_only():
    """片段已带 key（如 os.name:$os_name.value）→ 只替换值，不重复补 key。"""
    query = "sum:foo{os.name:$os_name.value,$version}"
    tv = {"os_name": "iOS", "version": "4.0.3*"}
    out = dq.strip_template_vars(query, tv)
    assert "os.name:iOS" in out
    assert "version:4.0.3*" in out
    assert "os.name:os.name:" not in out


# ---------------------------------------------------------------------------
# build_title_index
# ---------------------------------------------------------------------------

def test_build_title_index_duplicate_title_maps_to_none():
    widgets = [
        {"definition": {"title": "Crash-free sessions"}},
        {"definition": {"title": "Android ANR"}},
        {"definition": {"title": "Crash-free sessions"}},  # duplicate
    ]
    idx = dq.build_title_index(widgets)
    assert idx["Android ANR"] == 1
    assert idx["Crash-free sessions"] is None


def test_build_title_index_skips_untitled_widgets():
    widgets = [{"definition": {}}, {"definition": {"title": "FPS"}}]
    idx = dq.build_title_index(widgets)
    assert idx == {"FPS": 1}


# ---------------------------------------------------------------------------
# get_metric_scalar
# ---------------------------------------------------------------------------

def _rum_widget(title: str) -> dict:
    return {
        "definition": {
            "title": title,
            "requests": [{
                "queries": [{
                    "name": "query1",
                    "data_source": "rum",
                    "search": {"query": "env:production $service $version"},
                }],
                "formulas": [{"formula": "query1"}],
            }],
        }
    }


def _metrics_widget(title: str) -> dict:
    return {
        "definition": {
            "title": title,
            "requests": [{
                "queries": [{
                    "name": "query1",
                    "data_source": "metrics",
                    "query": "p75:rum.measure.view.memory{service:$service,env:$env,$version}",
                }],
                "formulas": [{"formula": "query1"}],
            }],
        }
    }


_TV = {"env": "production", "service": "plaud_ios", "version": "4.0.3*"}


@pytest.mark.asyncio
async def test_get_metric_scalar_title_not_found_raises_value_error():
    dashboard_json = {"widgets": [_rum_widget("Crash-free sessions")]}
    with pytest.raises(ValueError):
        await dq.get_metric_scalar(dashboard_json, "Nonexistent Widget", _TV, 0, 1000)


@pytest.mark.asyncio
async def test_get_metric_scalar_ambiguous_title_raises_value_error():
    dashboard_json = {"widgets": [
        _rum_widget("Crash-free sessions"),
        _rum_widget("Crash-free sessions"),
    ]}
    with pytest.raises(ValueError):
        await dq.get_metric_scalar(dashboard_json, "Crash-free sessions", _TV, 0, 1000)


@pytest.mark.asyncio
async def test_get_metric_scalar_http_400_returns_none_not_raises(monkeypatch):
    dashboard_json = {"widgets": [_metrics_widget("Refresh Rate")]}
    fake_client_cls = _make_fake_async_client(
        _FakeResponse(status_code=400, text="unable to parse")
    )
    monkeypatch.setattr(dq.httpx, "AsyncClient", fake_client_cls)

    result = await dq.get_metric_scalar(dashboard_json, "Refresh Rate", _TV, 0, 1000)
    assert result is None


@pytest.mark.asyncio
async def test_get_metric_scalar_success_returns_float_for_metrics_widget(monkeypatch):
    """端到端：Metrics tag-list widget + 修复后的模板变量替换 → 正常返回 float。"""
    dashboard_json = {"widgets": [_metrics_widget("Refresh Rate")]}
    fake_client_cls = _make_fake_async_client(_FakeResponse(
        status_code=200,
        json_data={"data": {"attributes": {"columns": [{"values": [59.9239]}]}}},
    ))
    monkeypatch.setattr(dq.httpx, "AsyncClient", fake_client_cls)

    result = await dq.get_metric_scalar(dashboard_json, "Refresh Rate", _TV, 0, 1000)
    assert result == pytest.approx(59.9239)


@pytest.mark.asyncio
async def test_get_metric_scalar_no_data_returns_none(monkeypatch):
    dashboard_json = {"widgets": [_rum_widget("Crash-free sessions")]}
    fake_client_cls = _make_fake_async_client(_FakeResponse(
        status_code=200,
        json_data={"data": {"attributes": {"columns": []}}},
    ))
    monkeypatch.setattr(dq.httpx, "AsyncClient", fake_client_cls)

    result = await dq.get_metric_scalar(dashboard_json, "Crash-free sessions", _TV, 0, 1000)
    assert result is None


@pytest.mark.asyncio
async def test_get_metric_scalar_no_requests_returns_none():
    dashboard_json = {"widgets": [{"definition": {"title": "Empty Widget", "requests": []}}]}
    result = await dq.get_metric_scalar(dashboard_json, "Empty Widget", _TV, 0, 1000)
    assert result is None


# ---------------------------------------------------------------------------
# get_dashboard_json
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# load_metrics_config (graygate/metrics.yaml loader)
# ---------------------------------------------------------------------------

def test_load_metrics_config_parses_all_11_metrics():
    cfg = dq.load_metrics_config()
    assert len(cfg.metrics) == 11
    keys = [m.key for m in cfg.metrics]
    assert keys == [
        "crash_free", "android_anr", "hang_rate", "refresh_rate", "fps",
        "jank", "cold_startup_p90", "memory_usage", "home_render",
        "detail_render_p90", "summary_render_p90",
    ]


def test_load_metrics_config_not_applicable_platform_read_correctly():
    cfg = dq.load_metrics_config()
    assert cfg.by_key("android_anr").not_applicable_platform == "ios"
    assert cfg.by_key("hang_rate").not_applicable_platform == "android"
    # metrics without the field default to None (not e.g. missing/KeyError)
    assert cfg.by_key("crash_free").not_applicable_platform is None


def test_load_metrics_config_dual_widget_metrics_carry_p75_p90_titles():
    cfg = dq.load_metrics_config()
    jank = cfg.by_key("jank")
    assert jank.title is None
    assert jank.title_p75 == "APP单次使用的卡顿次数（p75）"
    assert jank.title_p90 == "APP单次使用的卡顿次数（p90）"
    assert jank.cell_format == "{p75:.1f}/{p90:.1f}"


def test_load_metrics_config_single_widget_metric_fields():
    cfg = dq.load_metrics_config()
    memory = cfg.by_key("memory_usage")
    assert memory.title == "Memory Usage"
    assert memory.scale == pytest.approx(9.5367431640625e-7)
    assert memory.cell_format == "{v:.2f}MiB"


def test_load_metrics_config_defaults_dashboard_id_and_template_variables():
    cfg = dq.load_metrics_config()
    assert cfg.dashboard_id == "mbn-8h9-m2p"
    assert cfg.template_variables == {
        "env": "production",
        "os_version": "*",
        "usr.id": "*",
    }


@pytest.mark.asyncio
async def test_get_dashboard_json_caches_per_dashboard_id(monkeypatch):
    calls = {"count": 0}
    response = _FakeResponse(status_code=200, json_data={"widgets": []})

    class _CountingFakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            calls["count"] += 1
            return response

    monkeypatch.setattr(dq.httpx, "AsyncClient", _CountingFakeAsyncClient)

    dj1 = await dq.get_dashboard_json("test-dash-id")
    dj2 = await dq.get_dashboard_json("test-dash-id")
    assert dj1 == dj2
    assert calls["count"] == 1
