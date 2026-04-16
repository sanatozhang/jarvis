"""
Feishu (Lark) CLI client — replaces direct API calls with lark-cli subprocess.

Drop-in replacement for feishu.py (FeishuClient) and notify.py (FeishuNotifier).
Adds write capabilities: create/update Bitable records, send messages, create chats.

Requires: npm install -g @larksuite/cli
          lark-cli profile configured with project app credentials
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import get_settings
from app.models.schemas import Issue, IssueStatus, LogFile

logger = logging.getLogger("jarvis.feishu_cli")


def is_feishu_source(issue_id: str) -> bool:
    """Return True if the issue originates from Feishu (not local feedback or Linear)."""
    return not issue_id.startswith("fb_") and not issue_id.startswith("lin_")


# ---------------------------------------------------------------------------
# Module-level cache (same semantics as feishu.py)
# ---------------------------------------------------------------------------
_records_cache: List[Dict] = []
_cache_ts: float = 0.0
_cache_lock: Optional[asyncio.Lock] = None
CACHE_TTL = 900  # 15 minutes — Feishu data changes infrequently


def _get_cache_lock() -> asyncio.Lock:
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


def _patch_cached_record(record_id: str, fields: Dict[str, Any]) -> None:
    """Update a single record's fields in the in-memory cache.

    Avoids invalidating the entire 1500-record cache for single-record
    writes (mark_started, mark_completed, write_analysis_result).
    """
    for record in _records_cache:
        if record.get("record_id") == record_id:
            record.setdefault("fields", {}).update(fields)
            return


# ---------------------------------------------------------------------------
# CLI auto-setup: ensure profile exists so Bitable ops work on fresh deploys
# ---------------------------------------------------------------------------
_cli_initialized = False


async def _ensure_cli_profile():
    """Create the 'jarvis' CLI profile from .env credentials if it doesn't exist."""
    global _cli_initialized
    if _cli_initialized:
        return
    _cli_initialized = True

    import shutil
    if not shutil.which("lark-cli"):
        logger.warning("lark-cli not found — Bitable operations will fail. Run: npm install -g @larksuite/cli")
        return

    # Check if profile already configured
    proc = await asyncio.create_subprocess_exec(
        "lark-cli", "profile", "list",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if b"jarvis" in stdout:
        return

    # Create profile from .env credentials
    settings = get_settings()
    if not settings.feishu.app_id or not settings.feishu.app_secret:
        logger.warning("FEISHU_APP_ID/SECRET not set — cannot init CLI profile")
        return

    proc = await asyncio.create_subprocess_exec(
        "lark-cli", "profile", "add",
        "--name", "jarvis",
        "--app-id", settings.feishu.app_id,
        "--app-secret-stdin",
        "--brand", "feishu",
        "--use",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate(input=settings.feishu.app_secret.encode())
    if proc.returncode == 0:
        logger.info("Auto-created lark-cli profile 'jarvis'")
    else:
        logger.warning("Failed to auto-create lark-cli profile (may already exist)")


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------
async def _run_cli(*args: str, timeout: int = 120, retries: int = 2) -> Dict:
    """Run lark-cli with given arguments and return parsed JSON output.

    Retries on transient failures (timeout, empty output, non-JSON).
    """
    await _ensure_cli_profile()

    cmd = ["lark-cli", *args]
    last_error: Optional[RuntimeError] = None

    for attempt in range(1, retries + 1):
        logger.debug("Running (attempt %d/%d): %s", attempt, retries, " ".join(cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError("lark-cli not found. Run: npm install -g @larksuite/cli")

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            last_error = RuntimeError(f"lark-cli timed out after {timeout}s")
            if attempt < retries:
                logger.warning("lark-cli timeout (attempt %d), retrying...", attempt)
                await asyncio.sleep(1)
                continue
            raise last_error

        output = stdout.decode("utf-8", errors="replace").strip()
        err_output = stderr.decode("utf-8", errors="replace").strip()

        # CLI sometimes writes JSON error responses to stderr
        if not output and err_output:
            output = err_output

        if not output:
            last_error = RuntimeError(f"lark-cli returned empty output (exit code {proc.returncode})")
            if attempt < retries:
                logger.warning("lark-cli empty output (attempt %d), retrying...", attempt)
                await asyncio.sleep(1)
                continue
            raise last_error

        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            last_error = RuntimeError(f"lark-cli returned non-JSON: {output[:300]}")
            if attempt < retries:
                logger.warning("lark-cli non-JSON (attempt %d), retrying...", attempt)
                await asyncio.sleep(1)
                continue
            raise last_error

        if not result.get("ok", False):
            error = result.get("error", {})
            raise RuntimeError(
                f"lark-cli error: {error.get('message', output[:300])}"
            )

        return result

    raise last_error or RuntimeError("lark-cli failed after retries")


# ---------------------------------------------------------------------------
# FeishuCLI — drop-in replacement for FeishuClient
# ---------------------------------------------------------------------------
class FeishuCLI:
    """Feishu client backed by lark-cli subprocess calls."""

    def __init__(self):
        settings = get_settings()
        self._app_token = settings.feishu.app_token
        self._table_id = settings.feishu.table_id
        self._view_id = settings.feishu.view_id
        self._base_url = settings.feishu.base_url

    # ------------------------------------------------------------------
    # Bitable records — READ
    # ------------------------------------------------------------------
    async def list_records(self, page_size: int = 200, force_refresh: bool = False) -> List[Dict]:
        """Fetch all records from Bitable via CLI with pagination (in-memory cache)."""
        global _records_cache, _cache_ts

        now = time.monotonic()
        if not force_refresh and _records_cache and (now - _cache_ts) < CACHE_TTL:
            logger.debug("Returning %d cached records (age %.0fs)", len(_records_cache), now - _cache_ts)
            return _records_cache

        lock = _get_cache_lock()
        async with lock:
            now = time.monotonic()
            if not force_refresh and _records_cache and (now - _cache_ts) < CACHE_TTL:
                return _records_cache

            logger.info("Fetching records from Feishu via CLI (cache miss)...")
            try:
                all_records: List[Dict] = []
                offset = 0

                while True:
                    result = await _run_cli(
                        "base", "+record-list",
                        "--base-token", self._app_token,
                        "--table-id", self._table_id,
                        "--view-id", self._view_id,
                        "--limit", str(page_size),
                        "--offset", str(offset),
                        timeout=180,
                    )

                    data = result.get("data", {})
                    fields = data.get("fields", [])
                    rows = data.get("data", [])
                    record_ids = data.get("record_id_list", [])

                    for i, row in enumerate(rows):
                        record = {"fields": {}, "record_id": record_ids[i] if i < len(record_ids) else ""}
                        for j, val in enumerate(row):
                            if j < len(fields):
                                record["fields"][fields[j]] = val
                        all_records.append(record)

                    if not data.get("has_more", False):
                        break
                    offset += len(rows)

                _records_cache = all_records
                _cache_ts = time.monotonic()
                logger.info("Fetched and cached %d records from Feishu via CLI", len(all_records))
                return all_records
            except Exception as e:
                # Degrade gracefully: return stale cache if available
                if _records_cache:
                    logger.warning(
                        "Feishu CLI fetch failed (%s), returning stale cache (%d records, age %.0fs)",
                        e, len(_records_cache), time.monotonic() - _cache_ts,
                    )
                    return _records_cache
                raise

    @staticmethod
    def invalidate_cache():
        global _records_cache, _cache_ts
        _records_cache = []
        _cache_ts = 0.0
        logger.info("Feishu records cache invalidated")

    async def get_record(self, record_id: str) -> Dict:
        result = await _run_cli(
            "base", "+record-get",
            "--base-token", self._app_token,
            "--table-id", self._table_id,
            "--record-id", record_id,
        )
        data = result.get("data", {})
        # +record-get returns {"record": {"field": value, ...}} format
        raw_record = data.get("record", {})
        if raw_record:
            return {"fields": raw_record, "record_id": record_id}
        # Fallback: columnar format (shouldn't happen for +record-get)
        fields_names = data.get("fields", [])
        row = data.get("data", [[]])[0] if data.get("data") else []
        record = {"fields": {}, "record_id": record_id}
        for j, val in enumerate(row):
            if j < len(fields_names):
                record["fields"][fields_names[j]] = val
        return record

    # ------------------------------------------------------------------
    # Bitable records — WRITE (new capabilities!)
    # ------------------------------------------------------------------
    async def create_record(self, fields: Dict[str, Any]) -> str:
        """Create a single record in Bitable. Returns the new record_id."""
        field_names = list(fields.keys())
        field_values = [fields[k] for k in field_names]
        payload = json.dumps({"fields": field_names, "rows": [field_values]}, ensure_ascii=False)

        result = await _run_cli(
            "base", "+record-batch-create",
            "--base-token", self._app_token,
            "--table-id", self._table_id,
            "--json", payload,
        )
        record_ids = result.get("data", {}).get("record_id_list", [])
        if not record_ids:
            raise RuntimeError("Create record returned no record_id")
        # New records can't be patched in-memory — invalidate the full cache
        # so the next list_records picks them up.
        self.invalidate_cache()
        return record_ids[0]

    async def update_record(self, record_id: str, fields: Dict[str, Any]) -> bool:
        """Update an existing record's fields."""
        payload = json.dumps({
            "record_id_list": [record_id],
            "patch": fields,
        }, ensure_ascii=False)

        result = await _run_cli(
            "base", "+record-batch-update",
            "--base-token", self._app_token,
            "--table-id", self._table_id,
            "--json", payload,
        )
        ok = result.get("ok", False)
        if ok:
            _patch_cached_record(record_id, fields)
        return ok

    # ------------------------------------------------------------------
    # File download
    # ------------------------------------------------------------------
    async def download_file(self, file_token: str, save_path: str) -> str:
        """Download a Bitable attachment from Feishu Drive.

        Bitable attachments use the /medias/ endpoint (not /files/).
        lark-cli `api` with -o flag writes binary responses to disk.
        """
        save = Path(save_path).resolve()
        save.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "lark-cli", "api", "GET",
            f"/open-apis/drive/v1/medias/{file_token}/download",
            "-o", save.name,
        ]
        logger.debug("Downloading %s -> %s", file_token, save)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(save.parent),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"File download timed out: {file_token}")

        if not save.exists():
            err = stderr.decode("utf-8", errors="replace").strip()
            out = stdout.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Download failed for {file_token}: {err or out}")

        logger.info("Downloaded file to %s (%d bytes)", save, save.stat().st_size)
        return str(save)

    # ------------------------------------------------------------------
    # Parsing helpers (unchanged from feishu.py)
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
        for field_name in ("日志文件", "其他附件"):
            for f in (fields.get(field_name) or []):
                if isinstance(f, dict) and f.get("file_token"):
                    log_files.append(LogFile(
                        name=f.get("name", ""),
                        token=f.get("file_token", ""),
                        size=f.get("size", 0),
                    ))

        zendesk_raw = self._get_text(fields.get("Zendesk 工单链接", ""))
        zendesk_id = self._extract_zendesk_id(zendesk_raw)
        zendesk_url = self._normalize_zendesk_url(zendesk_raw)

        started = bool(fields.get("开始处理"))
        confirmed = bool(fields.get("确认提交"))
        if confirmed:
            feishu_status = IssueStatus.DONE
        elif started:
            feishu_status = IssueStatus.IN_PROGRESS
        else:
            feishu_status = IssueStatus.PENDING

        result_summary = self._get_text(fields.get("处理结果", ""))
        root_cause_summary = self._get_text(fields.get("一句话归因", ""))

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

    @staticmethod
    def is_unfinished(record: Dict) -> bool:
        return not record.get("fields", {}).get("确认提交", False)

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
    # High-level: list / get issues
    # ------------------------------------------------------------------
    async def list_pending_issues(self, assignee: str = "") -> List[Issue]:
        records = await self.list_records()
        pending = [r for r in records if self.is_pending(r)]
        if assignee:
            pending = self.filter_by_assignee(pending, assignee)
        issues = [self.parse_record(r) for r in pending]
        priority_order = {"H": 0, "L": 1, "": 2}
        issues.sort(key=lambda i: (priority_order.get(i.priority, 2), -i.created_at_ms))
        return issues

    async def list_unfinished_issues(self, assignee: str = "") -> List[Issue]:
        records = await self.list_records()
        unfinished = [r for r in records if self.is_unfinished(r)]
        if assignee:
            unfinished = self.filter_by_assignee(unfinished, assignee)
        issues = [self.parse_record(r) for r in unfinished]
        priority_order = {"H": 0, "L": 1, "": 2}
        issues.sort(key=lambda i: (priority_order.get(i.priority, 2), -i.created_at_ms))
        return issues

    async def list_issues_by_status(
        self, status: str, assignee: str = "", limit: int = 30,
    ) -> List[Issue]:
        records = await self.list_records()
        if assignee:
            records = self.filter_by_assignee(records, assignee)
        all_issues = [self.parse_record(r) for r in records]

        if status == "pending":
            filtered = [i for i in all_issues if i.feishu_status == IssueStatus.PENDING]
            priority_order = {"H": 0, "L": 1, "": 2}
            filtered.sort(key=lambda i: (priority_order.get(i.priority, 2), -i.created_at_ms))
        elif status == "in_progress":
            filtered = [i for i in all_issues if i.feishu_status == IssueStatus.IN_PROGRESS]
            filtered.sort(key=lambda i: -i.created_at_ms)
        elif status == "done":
            filtered = [i for i in all_issues if i.feishu_status == IssueStatus.DONE]
            filtered.sort(key=lambda i: -i.created_at_ms)
        else:
            filtered = all_issues
            filtered.sort(key=lambda i: -i.created_at_ms)

        return filtered[:limit]

    async def get_issue(self, record_id: str) -> Issue:
        record = await self.get_record(record_id)
        return self.parse_record(record)

    def get_feishu_link(self, record_id: str) -> str:
        return f"{self._base_url}?table={self._table_id}&record={record_id}"

    # ------------------------------------------------------------------
    # Zendesk helpers (unchanged)
    # ------------------------------------------------------------------
    ZENDESK_BASE = "https://nicebuildllc.zendesk.com/agent/tickets"

    @staticmethod
    def _extract_zendesk_id(zendesk_str: str) -> str:
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
        if not zendesk_str:
            return ""
        if zendesk_str.startswith("http"):
            return zendesk_str.replace("tickets/#", "tickets/")
        m = re.search(r"#?(\d{4,})", zendesk_str)
        if m:
            return f"{cls.ZENDESK_BASE}/{m.group(1)}"
        return ""

    # ------------------------------------------------------------------
    # Write-back: update Feishu issue with analysis result
    # ------------------------------------------------------------------
    async def write_analysis_result(
        self,
        record_id: str,
        root_cause: str,
        result_summary: str,
        problem_type: str = "",
    ) -> bool:
        """Write AI analysis result back to Feishu Bitable record."""
        fields: Dict[str, Any] = {}
        if root_cause:
            fields["一句话归因"] = root_cause
        if result_summary:
            fields["处理结果"] = result_summary
        if not fields:
            return False
        return await self.update_record(record_id, fields)

    async def mark_started(self, record_id: str) -> bool:
        """Set 开始处理=true on Feishu Bitable (marks issue as in-progress)."""
        return await self.update_record(record_id, {"开始处理": True})

    async def mark_completed(self, record_id: str, result_summary: str = "", root_cause: str = "") -> bool:
        """Set 确认提交=true on Feishu Bitable (marks issue as done).

        Optionally writes analysis result and root cause summary.
        """
        fields: Dict[str, Any] = {"确认提交": True}
        if result_summary:
            fields["处理结果"] = result_summary
        if root_cause:
            fields["一句话归因"] = root_cause
        return await self.update_record(record_id, fields)

    async def close(self):
        """No-op for CLI client (no persistent connections)."""
        pass


# ---------------------------------------------------------------------------
# IM operations — direct httpx API calls (no CLI dependency)
#
# IM uses httpx instead of CLI because:
# 1. No CLI profile needed → zero config on deployment
# 2. Bot token is already available via .env (same app_id/secret)
# 3. Avoids CLI's bot-visibility issues with chat creation
# ---------------------------------------------------------------------------
import httpx

_im_token: Optional[str] = None
_im_token_expire: float = 0


async def _get_tenant_token() -> str:
    """Get tenant access token for IM operations (uses IM app credentials)."""
    global _im_token, _im_token_expire
    now = time.monotonic()
    if _im_token and now < _im_token_expire:
        return _im_token

    settings = get_settings()
    app_id, app_secret = settings.feishu.im_credentials
    async with httpx.AsyncClient(verify=False, timeout=30) as http:
        resp = await http.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        data = resp.json()
        _im_token = data["tenant_access_token"]
        _im_token_expire = now + data.get("expire", 7200) - 60
        return _im_token


async def _feishu_api(method: str, path: str, params: Optional[Dict] = None, body: Optional[Dict] = None) -> Dict:
    """Call Feishu Open API directly with tenant token."""
    token = await _get_tenant_token()
    url = f"https://open.feishu.cn/open-apis{path}"
    async with httpx.AsyncClient(verify=False, timeout=30) as http:
        resp = await http.request(
            method, url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=body,
        )
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"Feishu API error ({result.get('code')}): {result.get('msg', result)}")
        return result


async def send_message(
    chat_id: str = "",
    email: str = "",
    text: str = "",
    markdown: str = "",
) -> bool:
    """Send a Feishu message via API."""
    if not chat_id and not email:
        raise ValueError("Either chat_id or email required")

    if markdown:
        import json as _json
        content = _json.dumps({"text": markdown}, ensure_ascii=False)
        msg_type = "text"
    elif text:
        import json as _json
        content = _json.dumps({"text": text}, ensure_ascii=False)
        msg_type = "text"
    else:
        raise ValueError("Message content required")

    try:
        if chat_id:
            await _feishu_api("POST", "/im/v1/messages", params={"receive_id_type": "chat_id"},
                              body={"receive_id": chat_id, "msg_type": msg_type, "content": content})
        else:
            await _feishu_api("POST", "/im/v1/messages", params={"receive_id_type": "email"},
                              body={"receive_id": email, "msg_type": msg_type, "content": content})
        return True
    except Exception as e:
        logger.error("Failed to send message: %s", e)
        return False


async def create_escalation_group(
    user_email: str,
    issue_id: str,
    description: str,
    problem_type: str = "",
    issue_link: str = "",
    zendesk_id: str = "",
) -> Dict[str, Any]:
    """Create a Feishu group chat for issue escalation.

    Flow: create group → get invite link → post issue info → notify members via email.
    No contact:user.id permission needed.
    """
    from app.db import database as db_mod

    now = datetime.now().strftime("%Y%m%d%H%M")
    category = problem_type or description[:20].replace(" ", "")
    group_name = f"工单处理--{category}--{now}"

    # 1. Create group (bot-only initially)
    result = await _feishu_api(
        "POST", "/im/v1/chats",
        params={"set_bot_manager": "true"},
        body={"name": group_name, "chat_type": "group"},
    )
    chat_id = result["data"]["chat_id"]
    logger.info("Created Feishu group: %s (chat_id: %s)", group_name, chat_id)

    # 2. Get invite link
    share_link = ""
    try:
        link_result = await _feishu_api(
            "POST", f"/im/v1/chats/{chat_id}/link",
            body={"is_external": False},
        )
        share_link = link_result.get("data", {}).get("share_link", "")
    except Exception as e:
        logger.warning("Failed to get group invite link: %s", e)

    # 3. Post issue info to group
    msg_lines = ["🔔 工单转交工程师处理"]
    msg_lines.append(f"工单ID: {issue_id}")
    msg_lines.append(f"问题描述: {description[:300]}")
    if problem_type:
        msg_lines.append(f"问题分类: {problem_type}")
    if zendesk_id:
        msg_lines.append(f"Zendesk: {zendesk_id}")
    if issue_link:
        msg_lines.append(f"链接: {issue_link}")
    await send_message(chat_id=chat_id, text="\n".join(msg_lines))

    # 4. Notify members via email with invite link
    oncall_emails = await db_mod.get_current_oncall()
    all_emails = list(set(([user_email] if user_email else []) + oncall_emails))

    notify_lines = [f"🔔 工单已转交工程师处理"]
    notify_lines.append(f"工单: {issue_id}")
    notify_lines.append(f"问题: {description[:100]}")
    if share_link:
        notify_lines.append(f"请加入处理群: {share_link}")
    if issue_link:
        notify_lines.append(f"飞书工单: {issue_link}")
    notify_text = "\n".join(notify_lines)

    for email in all_emails:
        try:
            await send_message(email=email, text=notify_text)
            logger.info("Notified %s to join escalation group", email)
        except Exception as e:
            logger.warning("Failed to notify %s: %s", email, e)

    return {
        "chat_id": chat_id,
        "group_name": group_name,
        "share_link": share_link,
        "members": all_emails,
    }


async def notify_oncall(
    issue_id: str,
    description: str,
    reason: str,
    zendesk_id: str = "",
    link: str = "",
) -> bool:
    """Send notification to current oncall engineers by email."""
    from app.db import database as db_mod

    recipients = await db_mod.get_current_oncall()
    if not recipients:
        logger.warning("No oncall members configured, cannot send notification")
        return False

    text_lines = [
        f"🔔 工单需要工程师处理",
        f"工单: {issue_id}",
        f"问题: {description[:200]}",
        f"原因: {reason}",
    ]
    if zendesk_id:
        text_lines.append(f"Zendesk: {zendesk_id}")
    if link:
        text_lines.append(f"详情: {link}")
    text = "\n".join(text_lines)

    sent = 0
    for email in recipients:
        if await send_message(email=email, text=text):
            sent += 1

    return sent > 0
