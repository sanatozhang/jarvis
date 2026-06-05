import json
import httpx
import pytest
from app.coreguard.monitors.client import DatadogMonitorClient


def _client_with(handler):
    transport = httpx.MockTransport(handler)
    return DatadogMonitorClient(api_key="k", app_key="a", site="datadoghq.com", transport=transport)


def test_create_posts_payload_and_returns_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 123, "name": seen["body"]["name"]})

    c = _client_with(handler)
    result = c.create({"name": "m1", "type": "metric alert", "query": "x > 1"})

    assert result["id"] == 123
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/v1/monitor")
    assert seen["headers"]["dd-api-key"] == "k"
    assert seen["headers"]["dd-application-key"] == "a"
    assert seen["body"]["name"] == "m1"


def test_update_puts_to_id_url():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": 123})

    c = _client_with(handler)
    c.update(123, {"name": "m1"})
    assert seen["method"] == "PUT"
    assert seen["url"].endswith("/api/v1/monitor/123")


def test_mute_posts_to_mute_url():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": 123})

    c = _client_with(handler)
    c.mute(123)
    assert seen["url"].endswith("/api/v1/monitor/123/mute")


def test_non_2xx_raises_with_body():
    def handler(request):
        return httpx.Response(400, json={"errors": ["bad query"]})

    c = _client_with(handler)
    with pytest.raises(RuntimeError) as e:
        c.create({"name": "m1"})
    assert "bad query" in str(e.value)
