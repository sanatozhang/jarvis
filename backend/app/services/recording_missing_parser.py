"""
Deterministic parser for recording-missing / timestamp-drift cases.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("jarvis.recording_missing_parser")

_APP_TS_RE = re.compile(r"INFO:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?")
_VERIFY_RE = re.compile(r"开始校验文件:\[(\d+)\]\s+\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
_SYNC_FINISH_RE = re.compile(r"_syncFinish:(\d+)\s+执行完成")
_DURATION_RE = re.compile(
    r"文件传输完成埋点：keyId=(\d+),\s*传输类型=([^,]+),\s*文件时长=(\d+)秒,\s*传输时长=(\d+)秒"
)

_LARGE_OFFSET_SECONDS = 14 * 24 * 60 * 60
_MAX_TIMELINE_ROWS = 20
_MAX_CANDIDATE_ROWS = 10


@dataclass
class SyncEntry:
    key_id: str
    session_id: str
    source_log: str
    verify_display_time: Optional[datetime] = None
    verify_app_time: Optional[datetime] = None
    sync_finish_time: Optional[datetime] = None
    file_duration_seconds: Optional[int] = None
    transfer_duration_seconds: Optional[int] = None
    transfer_type: str = ""
    verify_count: int = 0
    sync_finish_count: int = 0
    duration_count: int = 0

    def anchor_time(self) -> Optional[datetime]:
        return self.sync_finish_time or self.verify_app_time or self.verify_display_time


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


def _entry_to_dict(entry: SyncEntry) -> Dict[str, Any]:
    sync_time = entry.sync_finish_time or entry.verify_app_time
    offset_seconds = None
    if sync_time and entry.verify_display_time:
        offset_seconds = int((sync_time - entry.verify_display_time).total_seconds())

    return {
        "key_id": entry.key_id,
        "session_id": entry.session_id,
        "source_log": entry.source_log,
        "sync_time": sync_time.strftime("%Y-%m-%d %H:%M:%S") if sync_time else "",
        "app_display_time": (
            entry.verify_display_time.strftime("%Y-%m-%d %H:%M:%S")
            if entry.verify_display_time else ""
        ),
        "file_duration_seconds": entry.file_duration_seconds,
        "transfer_duration_seconds": entry.transfer_duration_seconds,
        "transfer_type": entry.transfer_type,
        "sync_minus_display_seconds": offset_seconds,
        "sync_minus_display_hours": round(offset_seconds / 3600, 2) if offset_seconds is not None else None,
        "is_large_offset": abs(offset_seconds) >= _LARGE_OFFSET_SECONDS if offset_seconds is not None else False,
        "verify_count": entry.verify_count,
        "sync_finish_count": entry.sync_finish_count,
        "duration_count": entry.duration_count,
    }


def _summary_line(row: Dict[str, Any]) -> str:
    offset_hours = row.get("sync_minus_display_hours")
    offset_text = f"{offset_hours}h" if offset_hours is not None else "unknown"
    duration = row.get("file_duration_seconds")
    duration_text = f"{duration}s" if duration is not None else "unknown"
    return (
        f"sync={row.get('sync_time') or 'unknown'} "
        f"display={row.get('app_display_time') or 'unknown'} "
        f"duration={duration_text} offset={offset_text} key={row.get('key_id')}"
    )


def parse_recording_missing_timeline(
    log_paths: List[Path],
    problem_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a sync timeline from recording-related log markers."""
    entries: Dict[str, SyncEntry] = {}

    for log_path in log_paths:
        try:
            with log_path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    app_time = _extract_app_time(line)

                    verify = _VERIFY_RE.search(line)
                    if verify:
                        key_id = verify.group(1)
                        entry = entries.setdefault(
                            key_id,
                            SyncEntry(
                                key_id=key_id,
                                session_id=key_id[:10],
                                source_log=log_path.name,
                            ),
                        )
                        entry.verify_count += 1
                        entry.verify_display_time = _parse_dt(verify.group(2)) or entry.verify_display_time
                        if app_time and (entry.verify_app_time is None or app_time < entry.verify_app_time):
                            entry.verify_app_time = app_time
                        continue

                    sync_finish = _SYNC_FINISH_RE.search(line)
                    if sync_finish:
                        key_id = sync_finish.group(1)
                        entry = entries.setdefault(
                            key_id,
                            SyncEntry(
                                key_id=key_id,
                                session_id=key_id[:10],
                                source_log=log_path.name,
                            ),
                        )
                        entry.sync_finish_count += 1
                        if app_time:
                            entry.sync_finish_time = app_time
                        continue

                    duration = _DURATION_RE.search(line)
                    if duration:
                        key_id = duration.group(1)
                        entry = entries.setdefault(
                            key_id,
                            SyncEntry(
                                key_id=key_id,
                                session_id=key_id[:10],
                                source_log=log_path.name,
                            ),
                        )
                        entry.duration_count += 1
                        entry.transfer_type = duration.group(2)
                        entry.file_duration_seconds = int(duration.group(3))
                        entry.transfer_duration_seconds = int(duration.group(4))
        except Exception as exc:
            logger.warning("Failed to parse recording timeline from %s: %s", log_path, exc)

    sorted_entries = sorted(
        entries.values(),
        key=lambda item: (item.anchor_time() or datetime.min, item.key_id),
    )
    all_rows = [_entry_to_dict(entry) for entry in sorted_entries]
    timeline_rows = all_rows[-_MAX_TIMELINE_ROWS:]

    large_offset_rows = [
        row for row in all_rows
        if row.get("is_large_offset")
    ][:_MAX_CANDIDATE_ROWS]

    after_problem_rows: List[Dict[str, Any]] = []
    if problem_date:
        cutoff = _parse_dt(f"{problem_date} 00:00:00")
        if cutoff:
            after_problem_rows = [
                row for row in all_rows
                if row.get("sync_time") and _parse_dt(row["sync_time"]) and _parse_dt(row["sync_time"]) >= cutoff
            ][:_MAX_CANDIDATE_ROWS]

    if not after_problem_rows:
        after_problem_rows = timeline_rows[-_MAX_CANDIDATE_ROWS:]

    return {
        "stats": {
            "parsed_entries": len(entries),
            "timeline_rows": len(timeline_rows),
            "after_problem_date_rows": len(after_problem_rows),
            "large_offset_rows": len(large_offset_rows),
        },
        "timeline": timeline_rows,
        "after_problem_date": after_problem_rows,
        "large_offset_candidates": large_offset_rows,
        "summary_lines": [_summary_line(row) for row in after_problem_rows[:5]],
    }
