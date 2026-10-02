"""BuildRunner port on jarvis' Jenkins client. The job is module-kit's `module-publish` (publish-job.md)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from app.modulehub.ports import BuildHandle, BuildStatus

RESULT_FILE = "publish-result.json"


def _rewrite(server: str, url: str) -> str:
    """Jenkins reports its own configured base URL; always talk to the server we actually hit."""
    if not url:
        return url
    parts = urlsplit(url)
    tail = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    return server.rstrip("/") + tail


class JenkinsBuildRunner:
    def __init__(self, client: Any, job: str):
        self._jk, self._job = client, job

    async def trigger(self, *, repo: str, branch: str, platforms: List[str], major: bool, dry_run: bool, resume: bool) -> BuildHandle:
        server = await self._jk.pick_least_busy_server()
        params: Dict[str, str] = {"REPO": repo, "BRANCH": branch, "PLATFORMS": ",".join(platforms),
                                  "MAJOR": str(major).lower(), "DRY_RUN": str(dry_run).lower(), "RESUME": str(resume).lower()}
        queue_id, _ = await self._jk.trigger_build(server, self._job, params)
        return BuildHandle(ref="%s|%d" % (server, queue_id))

    async def status(self, ref: str) -> BuildStatus:
        server, queue_id = ref.rsplit("|", 1)
        item = await self._jk.fetch_queue_item(server, int(queue_id))
        if item.get("_gone"):
            return BuildStatus("failure", log_tail="queue item disappeared before the build started")
        executable: Optional[Dict] = item.get("executable")
        if not executable:
            return BuildStatus("queued")
        build_url = _rewrite(server, executable.get("url", ""))
        data = await self._jk.fetch_build_status(server, build_url)
        if data.get("building"):
            return BuildStatus("running", url=build_url)
        outcome = (data.get("result") or "").upper()
        tail = await self._safe_tail(server, build_url)
        if outcome != "SUCCESS":
            return BuildStatus("aborted" if outcome == "ABORTED" else "failure", url=build_url, log_tail=tail)
        result_json = None
        for art in data.get("artifacts", []) or []:
            if art.get("fileName") == RESULT_FILE:
                result_json = await self._jk.fetch_artifact_text(server, build_url, art["relativePath"])
                break
        return BuildStatus("success", url=build_url, log_tail=tail, result_json=result_json)

    async def _safe_tail(self, server: str, build_url: str) -> str:
        try:
            return await self._jk.fetch_console_tail(server, build_url)
        except Exception:  # the console is only used to classify failures; losing it is not fatal
            return ""
