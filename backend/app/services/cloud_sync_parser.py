"""
Deterministic parser for cloud sync / upload failures.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("jarvis.cloud_sync_parser")

_APP_TS_RE = re.compile(r"INFO:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?")
_UPLOAD_SUCCESS_RE = re.compile(r"上传文件完成:\s+keyId:\s*(\d+)")
_UPLOAD_FAILURE_RE = re.compile(r"(Upload file error: .+|上传文件失败:\[[^\]]+\])")
_CHUNK_START_RE = re.compile(r"uploadChunk partNumber/partCount = (\d+)/(\d+)")
_CHUNK_PROGRESS_RE = re.compile(r"chunk\.partNumber = (\d+), totalUploaded / totalSize = (\d+) / (\d+)")
_CHUNK_SUCCESS_RE = re.compile(r"chunk\.partNumber = (\d+) Success")
_FILE_NOTIFY_RE = re.compile(r'"sub_type":"file_notify".*"file_id":"([^"]+)"')
_UPLOAD_CONTEXT_TOKENS = (
    "upload file",
    "上传文件",
    "uploadchunk",
    "chunk.partnumber",
    "multipart",
    "presign",
    "/upload",
    "file/upload",
    "s3",
    "oss",
)


def _parse_dt(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _extract_app_time(line: str) -> Optional[datetime]:
    match = _APP_TS_RE.search(line)
    if not match:
        return None
    return _parse_dt(match.group(1))


def _classify_failure_kind(line: str) -> str:
    lower = line.lower()
    if "connection reset by peer" in lower:
        return "s3_connection_reset"
    if "receive timeout" in lower or "took longer than" in lower:
        return "upload_receive_timeout"
    if "uploadchunk" in lower or "上传文件失败" in line:
        return "chunk_retry_exhausted"
    return "upload_error"


def _has_upload_context(line: str) -> bool:
    lower = line.lower()
    return any(token in lower for token in _UPLOAD_CONTEXT_TOKENS)


def _extract_generic_failure_kind(line: str, recent_upload_context: bool) -> Optional[str]:
    lower = line.lower()
    if "connection reset by peer" in lower:
        if recent_upload_context or _has_upload_context(line):
            return "s3_connection_reset"
        return None
    if "receive timeout" in lower or "took longer than" in lower:
        if recent_upload_context or _has_upload_context(line):
            return "upload_receive_timeout"
        return None
    if "dio error unknown" in lower and _has_upload_context(line):
        return "upload_error"
    return None


def _append_limited(items: List[Dict[str, Any]], item: Dict[str, Any], limit: int = 10) -> None:
    if len(items) < limit:
        items.append(item)


def parse_cloud_sync_summary(
    log_paths: List[Path],
    problem_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract structured cloud-sync/upload signals from logs."""
    events: List[Dict[str, Any]] = []
    failure_modes: Dict[str, int] = {}
    stats = {
        "cloud_sync_started": 0,
        "cloud_sync_completed": 0,
        "websocket_disconnects": 0,
        "websocket_reconnect_scheduled": 0,
        "websocket_connectivity_restored": 0,
        "file_notify_events": 0,
        "upload_successes": 0,
        "upload_failures": 0,
        "chunk_progress_events": 0,
        "chunk_success_events": 0,
    }
    successes: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    chunk_samples: List[Dict[str, Any]] = []

    for log_path in log_paths:
        try:
            with log_path.open("r", encoding="utf-8", errors="ignore") as handle:
                recent_upload_context = 0
                for line in handle:
                    app_time = _extract_app_time(line)
                    event_time = app_time.strftime("%Y-%m-%d %H:%M:%S") if app_time else ""
                    has_recent_upload_context = recent_upload_context > 0

                    if "CloudSyncTrigger:" in line and "开始执行云同步" in line:
                        stats["cloud_sync_started"] += 1
                        _append_limited(events, {"time": event_time, "kind": "cloud_sync_start", "line": line.strip()})
                        recent_upload_context = 6
                        continue
                    if "CloudSyncTrigger:" in line and "云同步执行完成" in line:
                        stats["cloud_sync_completed"] += 1
                        _append_limited(events, {"time": event_time, "kind": "cloud_sync_complete", "line": line.strip()})
                        continue
                    if "NotificationWS: connection closed" in line:
                        stats["websocket_disconnects"] += 1
                        _append_limited(events, {"time": event_time, "kind": "ws_closed", "line": line.strip()})
                        continue
                    if "NotificationWS: schedule reconnect" in line:
                        stats["websocket_reconnect_scheduled"] += 1
                        _append_limited(events, {"time": event_time, "kind": "ws_reconnect", "line": line.strip()})
                        continue
                    if "NotificationWS: connectivity restored" in line:
                        stats["websocket_connectivity_restored"] += 1
                        _append_limited(events, {"time": event_time, "kind": "ws_restored", "line": line.strip()})
                        continue

                    file_notify = _FILE_NOTIFY_RE.search(line)
                    if file_notify:
                        stats["file_notify_events"] += 1
                        _append_limited(events, {
                            "time": event_time,
                            "kind": "file_notify",
                            "file_id": file_notify.group(1),
                            "line": line.strip(),
                        })
                        recent_upload_context = 6
                        continue

                    upload_success = _UPLOAD_SUCCESS_RE.search(line)
                    if upload_success:
                        stats["upload_successes"] += 1
                        _append_limited(successes, {
                            "time": event_time,
                            "key_id": upload_success.group(1),
                            "line": line.strip(),
                        })
                        recent_upload_context = 6
                        continue

                    upload_failure = _UPLOAD_FAILURE_RE.search(line)
                    if upload_failure:
                        stats["upload_failures"] += 1
                        failure_kind = _classify_failure_kind(upload_failure.group(1))
                        failure_modes[failure_kind] = failure_modes.get(failure_kind, 0) + 1
                        _append_limited(failures, {
                            "time": event_time,
                            "failure_kind": failure_kind,
                            "line": line.strip(),
                        })
                        recent_upload_context = 6
                        continue

                    failure_kind = _extract_generic_failure_kind(line, has_recent_upload_context)
                    if failure_kind:
                        failure_modes[failure_kind] = failure_modes.get(failure_kind, 0) + 1
                        _append_limited(failures, {
                            "time": event_time,
                            "failure_kind": failure_kind,
                            "line": line.strip(),
                        })
                        recent_upload_context = 6
                        continue

                    chunk_start = _CHUNK_START_RE.search(line)
                    if chunk_start:
                        _append_limited(chunk_samples, {
                            "time": event_time,
                            "kind": "chunk_start",
                            "part_number": int(chunk_start.group(1)),
                            "part_count": int(chunk_start.group(2)),
                            "line": line.strip(),
                        }, limit=20)
                        recent_upload_context = 6
                        continue

                    chunk_progress = _CHUNK_PROGRESS_RE.search(line)
                    if chunk_progress:
                        stats["chunk_progress_events"] += 1
                        _append_limited(chunk_samples, {
                            "time": event_time,
                            "kind": "chunk_progress",
                            "part_number": int(chunk_progress.group(1)),
                            "total_uploaded": int(chunk_progress.group(2)),
                            "total_size": int(chunk_progress.group(3)),
                            "line": line.strip(),
                        }, limit=20)
                        recent_upload_context = 6
                        continue

                    chunk_success = _CHUNK_SUCCESS_RE.search(line)
                    if chunk_success:
                        stats["chunk_success_events"] += 1
                        _append_limited(chunk_samples, {
                            "time": event_time,
                            "kind": "chunk_success",
                            "part_number": int(chunk_success.group(1)),
                            "line": line.strip(),
                        }, limit=20)
                        recent_upload_context = 6
                        continue

                    if recent_upload_context > 0:
                        recent_upload_context -= 1
        except Exception as exc:
            logger.warning("Failed to parse cloud sync signals from %s: %s", log_path, exc)

    cutoff = _parse_dt(f"{problem_date} 00:00:00") if problem_date else None

    def _after_cutoff(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not cutoff:
            return items
        filtered = []
        for item in items:
            t = _parse_dt(item.get("time", ""))
            if t and t >= cutoff:
                filtered.append(item)
        return filtered or items

    filtered_events = _after_cutoff(events)
    filtered_failures = _after_cutoff(failures)
    filtered_successes = _after_cutoff(successes)

    if filtered_failures:
        dominant_failure_mode = max(
            {item["failure_kind"]: 0 for item in filtered_failures},
            key=lambda kind: sum(1 for item in filtered_failures if item["failure_kind"] == kind),
        )
    elif filtered_successes:
        dominant_failure_mode = "upload_successful"
    elif stats["websocket_disconnects"] > 0:
        dominant_failure_mode = "websocket_unstable"
    else:
        dominant_failure_mode = "no_clear_upload_signal"

    summary_lines = [
        f"mode={dominant_failure_mode}",
        f"upload_successes={len(filtered_successes)} upload_failures={len(filtered_failures)}",
        f"ws_disconnects={stats['websocket_disconnects']} ws_reconnects={stats['websocket_reconnect_scheduled']}",
    ]
    summary_lines.extend(
        f"{item.get('time') or 'unknown'} {item['failure_kind']}: {item['line'][:180]}"
        for item in filtered_failures[:3]
    )

    return {
        "stats": stats,
        "failure_modes": failure_modes,
        "dominant_failure_mode": dominant_failure_mode,
        "timeline": filtered_events[:10],
        "upload_failures": filtered_failures[:10],
        "upload_successes": filtered_successes[:10],
        "chunk_samples": chunk_samples[:20],
        "summary_lines": summary_lines,
    }
