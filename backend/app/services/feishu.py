"""
Feishu (Lark) Open API client.

Handles:
- Tenant access token management
- Bitable record listing / fetching (with in-memory cache)
- Drive file downloading
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import get_settings
from app.models.schemas import Issue, IssueStatus, LogFile

logger = logging.getLogger("jarvis.feishu")

# We disable SSL verification to match the original scripts' behaviour.
SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

# ---------------------------------------------------------------------------
# Module-level cache for records (shared across all FeishuClient instances)
# ---------------------------------------------------------------------------
_records_cache: List[Dict] = []
_cache_ts: float = 0.0
_cache_lock: Optional[asyncio.Lock] = None
CACHE_TTL = 300  # 5 minutes


def _get_cache_lock() -> asyncio.Lock:
    """Lazy-init lock (must be created inside an event loop)."""
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


class FeishuClient:
    """Async Feishu Open API client with record caching."""

    TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"

    def __init__(self):
        settings = get_settings()
        self._app_id = settings.feishu.app_id
        self._app_secret = settings.feishu.app_secret
        self._app_token = settings.feishu.app_token
        self._table_id = settings.feishu.table_id
        self._view_id = settings.feishu.view_id
        self._base_url = settings.feishu.base_url

        self._token: Optional[str] = None
        self._token_expire: int = 0
        self._http = httpx.AsyncClient(verify=False, timeout=120)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    async def _get_token(self) -> str:
        now = int(datetime.now().timestamp())
        if self._token and now < self._token_expire - 60:
            return self._token

        resp = await self._http.post(
            self.TOKEN_URL,
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["tenant_access_token"]
        self._token_expire = now + data.get("expire", 7200)
        logger.debug("Feishu token refreshed, expires in %ds", data.get("expire", 7200))
        return self._token

    async def _request(self, method: str, url: str, **kwargs) -> Dict:
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        headers.update(kwargs.pop("headers", {}))
        resp = await self._http.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Bitable records
    # ------------------------------------------------------------------
    async def list_records(self, page_size: int = 100, force_refresh: bool = False) -> List[Dict]:
        """
        Fetch all records from the configured Bitable (paginated).
        Uses an in-memory cache (5 min TTL) to avoid repeated 20s+ API calls.
        Concurrent callers wait for a single in-flight fetch (dedup).
        """
        global _records_cache, _cache_ts

        now = time.monotonic()
        if not force_refresh and _records_cache and (now - _cache_ts) < CACHE_TTL:
            logger.debug("Returning %d cached records (age %.0fs)", len(_records_cache), now - _cache_ts)
            return _records_cache

        lock = _get_cache_lock()
        async with lock:
            # Double-check after acquiring lock (another coroutine may have refreshed)
            now = time.monotonic()
            if not force_refresh and _records_cache and (now - _cache_ts) < CACHE_TTL:
                return _records_cache

            logger.info("Fetching records from Feishu (cache miss)...")
            all_records: List[Dict] = []
            page_token: Optional[str] = None

            while True:
                url = (
                    f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
                    f"{self._app_token}/tables/{self._table_id}/records"
                    f"?view_id={self._view_id}&page_size={page_size}"
                )
                if page_token:
                    url += f"&page_token={page_token}"

                result = await self._request("GET", url)
                if result.get("code") != 0:
                    raise RuntimeError(f"Feishu API error: {result}")

                data = result.get("data", {})
                all_records.extend(data.get("items", []))
                page_token = data.get("page_token")
                if not page_token:
                    break

            _records_cache = all_records
            _cache_ts = time.monotonic()
            logger.info("Fetched and cached %d records from Feishu", len(all_records))
            return all_records

    @staticmethod
    def invalidate_cache():
        """Force next list_records call to re-fetch from Feishu."""
        global _records_cache, _cache_ts
        _records_cache = []
        _cache_ts = 0.0
        logger.info("Feishu records cache invalidated")

    async def get_record(self, record_id: str) -> Dict:
        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
            f"{self._app_token}/tables/{self._table_id}/records/{record_id}"
        )
        result = await self._request("GET", url)
        if result.get("code") != 0:
            raise RuntimeError(f"Feishu API error: {result}")
        return result.get("data", {}).get("record", {})

    async def download_file(self, file_token: str, save_path: str) -> str:
        """Download a file from Feishu Drive and save to disk."""
        import aiofiles
        from pathlib import Path

        token = await self._get_token()
        url = f"https://open.feishu.cn/open-apis/drive/v1/medias/{file_token}/download"

        async with self._http.stream("GET", url, headers={"Authorization": f"Bearer {token}"}) as resp:
            resp.raise_for_status()
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(save_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    await f.write(chunk)

        logger.info("Downloaded file to %s", save_path)
        return save_path

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _get_text(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, bool):
            return "是" if v else "否"
        if isinstance(v, list) and len(v) > 0:
            first = v[0]
            if isinstance(first, dict):
                return first.get("text", first.get("name", str(first)))
            return str(first)
        if isinstance(v, dict):
            return v.get("text", v.get("link", str(v)))
        return str(v)

    def parse_record(self, record: Dict) -> Issue:
        fields = record.get("fields", {})
        record_id = record.get("record_id", "")
        log_files = []
        for f in (fields.get("日志文件") or []):
            if isinstance(f, dict) and f.get("file_token"):
                log_files.append(LogFile(
                    name=f.get("name", ""),
                    token=f.get("file_token", ""),
                    size=f.get("size", 0),
                ))

        zendesk_raw = self._get_text(fields.get("Zendesk 工单链接", ""))
        zendesk_id = self._extract_zendesk_id(zendesk_raw)
        zendesk_url = self._normalize_zendesk_url(zendesk_raw)

        # Determine status from Feishu fields
        started = bool(fields.get("开始处理"))
        confirmed = bool(fields.get("确认提交"))
        if confirmed:
            feishu_status = IssueStatus.DONE
        elif started:
            feishu_status = IssueStatus.IN_PROGRESS
        else:
            feishu_status = IssueStatus.PENDING

        # Result / root cause from Feishu
        result_summary = self._get_text(fields.get("处理结果", ""))
        root_cause_summary = self._get_text(fields.get("一句话归因", ""))

        # Created time
        created_at_ms = 0
        raw_ts = fields.get("创建日期")
        if isinstance(raw_ts, (int, float)) and raw_ts > 0:
            created_at_ms = int(raw_ts)

        return Issue(
            record_id=record_id,
            description=self._get_text(fields.get("问题描述", ""))[:500],
            device_sn=self._get_text(fields.get("设备 SN", "")),
            firmware=self._get_text(fields.get("固件版本号", "")),
            app_version=self._get_text(fields.get("APP 版本", "")),
            priority=self._get_text(fields.get("问题等级", "")),
            zendesk=zendesk_url,
            zendesk_id=zendesk_id,
            feishu_link=self.get_feishu_link(record_id),
            feishu_status=feishu_status,
            result_summary=result_summary,
            root_cause_summary=root_cause_summary,
            created_at_ms=created_at_ms,
            log_files=log_files,
        )

    @staticmethod
    def is_pending(record: Dict) -> bool:
        return not record.get("fields", {}).get("开始处理", False)

    def filter_by_assignee(self, records: List[Dict], assignee: str) -> List[Dict]:
        if not assignee:
            return records
        result = []
        assignee_lower = assignee.lower()
        for record in records:
            fields = record.get("fields", {})
            for a in (fields.get("问题指派人") or []):
                name = (a.get("name", "") or "").lower()
                en_name = (a.get("en_name", "") or "").lower()
                if assignee_lower in name or assignee_lower in en_name:
                    result.append(record)
                    break
        return result

    # ------------------------------------------------------------------
    # High-level: list issues
    # ------------------------------------------------------------------
    async def list_pending_issues(self, assignee: str = "") -> List[Issue]:
        records = await self.list_records()
        pending = [r for r in records if self.is_pending(r)]
        if assignee:
            pending = self.filter_by_assignee(pending, assignee)
        issues = [self.parse_record(r) for r in pending]
        # Sort: H first, then by created_at desc
        priority_order = {"H": 0, "L": 1, "": 2}
        issues.sort(key=lambda i: (priority_order.get(i.priority, 2), -i.created_at_ms))
        return issues

    async def list_issues_by_status(
        self,
        status: str,
        assignee: str = "",
        limit: int = 30,
    ) -> List[Issue]:
        """
        List issues filtered by feishu_status.
        status: 'pending' | 'in_progress' | 'done' | 'all'
        Sorted by created_at_ms desc (newest first), limited.
        """
        records = await self.list_records()

        # Filter by assignee first
        if assignee:
            records = self.filter_by_assignee(records, assignee)

        # Parse all records
        all_issues = [self.parse_record(r) for r in records]

        # Filter by status
        if status == "pending":
            filtered = [i for i in all_issues if i.feishu_status == IssueStatus.PENDING]
            # Pending: H first, then by created_at desc
            priority_order = {"H": 0, "L": 1, "": 2}
            filtered.sort(key=lambda i: (priority_order.get(i.priority, 2), -i.created_at_ms))
        elif status == "in_progress":
            filtered = [i for i in all_issues if i.feishu_status == IssueStatus.IN_PROGRESS]
            filtered.sort(key=lambda i: -i.created_at_ms)
        elif status == "done":
            filtered = [i for i in all_issues if i.feishu_status == IssueStatus.DONE]
            filtered.sort(key=lambda i: -i.created_at_ms)
        else:  # all
            filtered = all_issues
            filtered.sort(key=lambda i: -i.created_at_ms)

        return filtered[:limit]

    async def get_issue(self, record_id: str) -> Issue:
        record = await self.get_record(record_id)
        return self.parse_record(record)

    def get_feishu_link(self, record_id: str) -> str:
        return f"{self._base_url}?table={self._table_id}&record={record_id}"

    ZENDESK_BASE = "https://nicebuildllc.zendesk.com/agent/tickets"

    @staticmethod
    def _extract_zendesk_id(zendesk_str: str) -> str:
        """Extract Zendesk ticket number from URL or text, e.g. '#378794'."""
        if not zendesk_str:
            return ""
        m = re.search(r"tickets/(\d+)", zendesk_str)
        if m:
            return f"#{m.group(1)}"
        m = re.search(r"#?(\d{4,})", zendesk_str)
        if m:
            return f"#{m.group(1)}"
        return ""

    @classmethod
    def _normalize_zendesk_url(cls, zendesk_str: str) -> str:
        """Ensure zendesk field is always a valid clickable URL."""
        if not zendesk_str:
            return ""
        # Already a proper URL — just clean up any # in the ticket path
        if zendesk_str.startswith("http"):
            return zendesk_str.replace("tickets/#", "tickets/")
        # Just a number or #number — build the full URL
        m = re.search(r"#?(\d{4,})", zendesk_str)
        if m:
            return f"{cls.ZENDESK_BASE}/{m.group(1)}"
        return ""

    async def close(self):
        await self._http.aclose()
