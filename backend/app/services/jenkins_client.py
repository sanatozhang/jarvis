"""
Jenkins HTTP client — trigger builds, query queue, fetch status & artifacts.

Three Jenkins servers are load-balanced by current build queue length.
Each server has its own independent account; the client looks up credentials
per server URL on every call.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("jarvis.jenkins")

DEFAULT_TIMEOUT = 30.0


class JenkinsError(RuntimeError):
    """Wraps non-2xx response or transport error from a Jenkins server."""

    def __init__(self, message: str, *, status: Optional[int] = None, server: str = ""):
        super().__init__(message)
        self.status = status
        self.server = server


class JenkinsServerCreds:
    """Per-server credentials. Mirrors `app.config.JenkinsServerConfig`."""

    __slots__ = ("url", "user", "api_token")

    def __init__(self, url: str, user: str = "", api_token: str = ""):
        self.url = url.rstrip("/")
        self.user = user
        self.api_token = api_token


class JenkinsClient:
    """Stateless multi-server client. Picks credentials per-URL at call time."""

    def __init__(self, servers: List[JenkinsServerCreds], timeout: float = DEFAULT_TIMEOUT):
        if not servers:
            raise ValueError("JenkinsClient requires at least one server config")
        self.servers = servers
        self._by_url: Dict[str, JenkinsServerCreds] = {s.url: s for s in servers}
        self.timeout = timeout

    # ---- creds lookup ----------------------------------------------------
    def _resolve_creds(self, server_url: str) -> JenkinsServerCreds:
        """Match by exact URL first, then by host+port (handle trailing-slash drift)."""
        normalized = server_url.rstrip("/")
        if normalized in self._by_url:
            return self._by_url[normalized]
        target_host = urlsplit(normalized).netloc
        for s in self.servers:
            if urlsplit(s.url).netloc == target_host:
                return s
        raise JenkinsError(f"no credentials configured for server: {server_url}", server=server_url)

    def _auth_header(self, creds: JenkinsServerCreds) -> Dict[str, str]:
        if not creds.user or not creds.api_token:
            # Some Jenkins setups allow anonymous build trigger — let the server
            # decide. We just skip the Authorization header.
            return {}
        token = base64.b64encode(f"{creds.user}:{creds.api_token}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def _client(self, creds: JenkinsServerCreds) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=self._auth_header(creds),
            timeout=self.timeout,
            follow_redirects=False,
        )

    async def _get_crumb(self, client: httpx.AsyncClient, server: str) -> Dict[str, str]:
        """Fetch CSRF crumb. Some Jenkins setups disable crumb — degrade gracefully."""
        url = f"{server}/crumbIssuer/api/json"
        try:
            r = await client.get(url)
        except httpx.HTTPError as e:
            raise JenkinsError(f"crumb fetch transport error: {e}", server=server)
        if r.status_code == 404:
            return {}  # crumb disabled
        if r.status_code != 200:
            raise JenkinsError(
                f"crumb fetch returned {r.status_code}: {r.text[:200]}",
                status=r.status_code, server=server,
            )
        data = r.json()
        return {data.get("crumbRequestField", "Jenkins-Crumb"): data.get("crumb", "")}

    # ---- Queue / load balancing -------------------------------------------
    async def fetch_queue_length(self, server: str) -> int:
        creds = self._resolve_creds(server)
        url = f"{creds.url}/queue/api/json"
        try:
            async with self._client(creds) as client:
                r = await client.get(url)
        except httpx.HTTPError as e:
            raise JenkinsError(f"queue fetch transport error: {e}", server=server)
        if r.status_code != 200:
            raise JenkinsError(
                f"queue fetch {r.status_code}: {r.text[:200]}",
                status=r.status_code, server=server,
            )
        items = r.json().get("items", []) or []
        return len(items)

    async def pick_least_busy_server(self) -> str:
        """Concurrent-fetch queue lengths from all servers; pick smallest.

        Servers that error out are ranked last (sentinel).
        """
        async def _one(s: JenkinsServerCreds) -> Tuple[str, int]:
            try:
                n = await self.fetch_queue_length(s.url)
                return s.url, n
            except Exception as e:
                logger.warning("queue probe failed for %s: %s", s.url, e)
                return s.url, 10**9

        results = await asyncio.gather(*[_one(s) for s in self.servers])
        results.sort(key=lambda x: x[1])
        chosen = results[0][0]
        logger.info("Jenkins load-balance picked %s (queue lengths: %s)", chosen, results)
        return chosen

    # ---- Trigger ----------------------------------------------------------
    async def trigger_build(
        self,
        server: str,
        job: str,
        params: Dict[str, Any],
    ) -> Tuple[int, str]:
        """POST /job/<job>/buildWithParameters. Returns (queue_id, queue_item_url)."""
        creds = self._resolve_creds(server)
        url = f"{creds.url}/job/{job}/buildWithParameters"
        async with self._client(creds) as client:
            crumb = await self._get_crumb(client, creds.url)
            try:
                r = await client.post(url, data=params, headers=crumb)
            except httpx.HTTPError as e:
                raise JenkinsError(f"trigger transport error: {e}", server=server)
            if r.status_code not in (200, 201):
                raise JenkinsError(
                    f"trigger {r.status_code}: {r.text[:400]}",
                    status=r.status_code, server=server,
                )
            loc = r.headers.get("Location", "")
            m = re.search(r"/queue/item/(\d+)/?", loc)
            if not m:
                raise JenkinsError(
                    f"trigger ok but no queue id in Location: {loc!r}",
                    server=server,
                )
            return int(m.group(1)), loc

    # ---- Status -----------------------------------------------------------
    async def fetch_queue_item(self, server: str, queue_id: int) -> Dict[str, Any]:
        creds = self._resolve_creds(server)
        url = f"{creds.url}/queue/item/{queue_id}/api/json"
        async with self._client(creds) as client:
            try:
                r = await client.get(url)
            except httpx.HTTPError as e:
                raise JenkinsError(f"queue item transport error: {e}", server=server)
            if r.status_code == 404:
                return {"_gone": True}
            if r.status_code != 200:
                raise JenkinsError(
                    f"queue item {r.status_code}: {r.text[:200]}",
                    status=r.status_code, server=server,
                )
            return r.json()

    async def fetch_build_status(self, server: str, build_url: str) -> Dict[str, Any]:
        creds = self._resolve_creds(server)
        url = build_url.rstrip("/") + "/api/json"
        async with self._client(creds) as client:
            try:
                r = await client.get(url)
            except httpx.HTTPError as e:
                raise JenkinsError(f"build status transport error: {e}")
            if r.status_code != 200:
                raise JenkinsError(
                    f"build status {r.status_code}: {r.text[:200]}",
                    status=r.status_code,
                )
            return r.json()

    @staticmethod
    def pick_artifact_url(build_url: str, artifacts: List[Dict[str, Any]], platform: str) -> Optional[str]:
        """Pick the matching .apk/.aab (android) or .ipa (ios) artifact."""
        suffixes_for: Dict[str, Tuple[str, ...]] = {
            "android": (".aab", ".apk"),
            "ios": (".ipa",),
        }
        suffixes = suffixes_for.get(platform.lower())
        if not suffixes:
            return None
        for a in artifacts:
            name = (a.get("fileName") or "").lower()
            rel = a.get("relativePath") or ""
            if name.endswith(suffixes) and rel:
                return f"{build_url.rstrip('/')}/artifact/{rel}"
        return None


def build_client_from_settings(settings) -> JenkinsClient:
    """Helper: convert app.config.JenkinsSettings → JenkinsClient."""
    creds: List[JenkinsServerCreds] = []
    for s in settings.jenkins.servers:
        creds.append(JenkinsServerCreds(url=s.url, user=s.user, api_token=s.api_token))
    return JenkinsClient(creds)
