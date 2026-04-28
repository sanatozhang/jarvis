"""
Datadog Error Tracking API client.

API 文档: https://docs.datadoghq.com/api/latest/error-tracking/
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("crashguard.datadog")

_RETRY_STATUS = {500, 502, 503, 504}
_RATE_LIMIT_STATUS = {429}


class DatadogRateLimitError(Exception):
    """Datadog 触发限流"""


class DatadogClient:
    """异步 Datadog Error Tracking client"""

    def __init__(
        self,
        api_key: str,
        app_key: str,
        site: str = "datadoghq.com",
        timeout: float = 30.0,
    ):
        self.api_key = api_key
        self.app_key = app_key
        self.site = site
        self.timeout = timeout
        self.base_url = f"https://api.{site}/api/v2/error-tracking"

    def _headers(self) -> Dict[str, str]:
        return {
            "DD-API-KEY": self.api_key,
            "DD-APPLICATION-KEY": self.app_key,
            "Content-Type": "application/json",
        }

    async def list_issues(
        self,
        window_hours: int = 24,
        page_size: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        分页拉取所有 error tracking issue。

        失败重试 3 次指数退避（1s/2s/4s），429 抛 DatadogRateLimitError。
        """
        params: Dict[str, Any] = {
            "filter[from]": f"now-{window_hours}h",
            "filter[to]": "now",
            "page[size]": page_size,
        }
        all_issues: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            while True:
                if cursor:
                    params["page[after]"] = cursor

                payload = await self._get_with_retry(
                    client,
                    f"{self.base_url}/issues",
                    params=params,
                )
                all_issues.extend(payload.get("data", []))

                meta = payload.get("meta", {}).get("page", {})
                cursor = meta.get("after")
                if not cursor:
                    break

        return all_issues

    async def _get_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: Dict[str, Any],
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """指数退避重试，429 不重试直接抛"""
        last_error: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                resp = await client.get(url, headers=self._headers(), params=params)
                if resp.status_code in _RATE_LIMIT_STATUS:
                    raise DatadogRateLimitError(
                        f"Datadog 限流 (429), retry-after={resp.headers.get('retry-after')}"
                    )
                if resp.status_code in _RETRY_STATUS:
                    last_error = httpx.HTTPStatusError(
                        f"{resp.status_code}",
                        request=resp._request,  # type: ignore[arg-type]
                        response=resp,
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue
                if not resp.is_success:
                    resp.raise_for_status()
                return resp.json()
            except DatadogRateLimitError:
                raise
            except httpx.HTTPError as e:
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise

        raise last_error if last_error else RuntimeError("未知错误")
