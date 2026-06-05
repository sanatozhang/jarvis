"""Datadog Monitors API v1 薄封装（同步 httpx，CLI 用）。

鉴权复用 coreguard 的 CRASHGUARD_DATADOG_* key（见 config.py 回落逻辑）。
失败抛 RuntimeError（含响应体），由调用方处理 —— 与 datadog_scalar 的"宽容返回 None"
不同：建监控是写操作，必须显式失败。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from app.coreguard.config import get_coreguard_settings

DEFAULT_TIMEOUT = 30.0


class DatadogMonitorClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        app_key: Optional[str] = None,
        site: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        s = get_coreguard_settings()
        self.api_key = api_key or s.datadog_api_key
        self.app_key = app_key or s.datadog_app_key
        self.site = site or s.datadog_site
        self._base = f"https://api.{self.site}/api/v1/monitor"
        self._client = httpx.Client(timeout=DEFAULT_TIMEOUT, transport=transport)

    def _headers(self) -> Dict[str, str]:
        return {
            "DD-API-KEY": self.api_key,
            "DD-APPLICATION-KEY": self.app_key,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, url: str, json_body: Optional[dict] = None) -> Any:
        resp = self._client.request(method, url, headers=self._headers(), json=json_body)
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"Datadog Monitors API {method} {url} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", self._base, payload)

    def update(self, monitor_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", f"{self._base}/{monitor_id}", payload)

    def get(self, monitor_id: int) -> Dict[str, Any]:
        return self._request("GET", f"{self._base}/{monitor_id}")

    def list(self, monitor_tags: Optional[str] = None) -> List[Dict[str, Any]]:
        url = self._base
        if monitor_tags:
            url = f"{self._base}?monitor_tags={monitor_tags}"
        return self._request("GET", url)

    def mute(self, monitor_id: int) -> Dict[str, Any]:
        return self._request("POST", f"{self._base}/{monitor_id}/mute")

    def delete(self, monitor_id: int) -> Dict[str, Any]:
        return self._request("DELETE", f"{self._base}/{monitor_id}")
