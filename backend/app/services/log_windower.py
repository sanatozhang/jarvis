"""
Log windower: extract time-relevant portions of large log files.

This is the first step of L1.5 — a deterministic, zero-cost operation that
reduces multi-MB logs to the time window around the problem date.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("jarvis.log_windower")

# Matches both iOS ("2026-02-01 03:52:53689") and Android ("INFO: 2026-03-13 18:14:24.926329:")
_TS_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})")

# Minimum log size (in bytes) before windowing kicks in.
# Smaller logs are fast to grep — no need to window them.
DEFAULT_SIZE_THRESHOLD = 5 * 1024 * 1024  # 5 MB

# Error/warning keywords to detect "interesting" time periods
_ERROR_KEYWORDS = re.compile(
    r"(?:error|exception|fail|crash|fatal|timeout|disconnect|断开|失败|异常|超时)",
    re.IGNORECASE,
)


def parse_timestamp(line: str) -> Optional[datetime]:
    """Extract a datetime from a log line. Returns None if no timestamp found."""
    m = _TS_PATTERN.search(line)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def get_log_time_range(log_path: Path, sample_lines: int = 50) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Get the first and last timestamps from a log file by reading head/tail."""
    first_ts = None
    last_ts = None

    # Read first N lines for start time
    try:
        with open(log_path, "r", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= sample_lines:
                    break
                ts = parse_timestamp(line)
                if ts and first_ts is None:
                    first_ts = ts
    except Exception as e:
        logger.warning("Failed to read head of %s: %s", log_path, e)

    # Read last N lines for end time (using tail approach)
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()
            # Read last ~100KB
            read_size = min(file_size, 100 * 1024)
            f.seek(file_size - read_size)
            tail_content = f.read().decode("utf-8", errors="replace")
            for line in reversed(tail_content.split("\n")):
                ts = parse_timestamp(line)
                if ts:
                    last_ts = ts
                    break
    except Exception as e:
        logger.warning("Failed to read tail of %s: %s", log_path, e)

    return first_ts, last_ts


def window_log_file(
    log_path: Path,
    output_dir: Path,
    center_time: Optional[datetime] = None,
    hours_before: int = 4,
    hours_after: int = 2,
    size_threshold: int = DEFAULT_SIZE_THRESHOLD,
    max_output_lines: int = 200_000,
) -> Tuple[Path, dict]:
    """
    Extract lines from a log file within a time window.

    Args:
        log_path: Path to the original log file.
        output_dir: Directory to write the windowed log into.
        center_time: Center of the time window (usually problem_date).
                     If None, uses the last 6 hours of the log.
        hours_before: Hours before center_time to include.
        hours_after: Hours after center_time to include.
        size_threshold: Only window files larger than this (bytes).
        max_output_lines: Safety cap on output lines.

    Returns:
        (path_to_windowed_log, metadata_dict)
        If the file is small enough or no timestamps found, returns original path.
    """
    file_size = log_path.stat().st_size
    metadata = {
        "original_path": str(log_path),
        "original_size_bytes": file_size,
        "windowed": False,
    }

    # Small file — no windowing needed
    if file_size < size_threshold:
        metadata["reason"] = "below_size_threshold"
        return log_path, metadata

    # Determine time window
    if center_time is None:
        _, last_ts = get_log_time_range(log_path)
        if last_ts is None:
            logger.warning("No timestamps found in %s, skipping windowing", log_path.name)
            metadata["reason"] = "no_timestamps_found"
            return log_path, metadata
        center_time = last_ts
        metadata["center_time_source"] = "log_tail"
    else:
        metadata["center_time_source"] = "problem_date"

    window_start = center_time - timedelta(hours=hours_before)
    window_end = center_time + timedelta(hours=hours_after)

    metadata["window_start"] = window_start.isoformat()
    metadata["window_end"] = window_end.isoformat()
    metadata["center_time"] = center_time.isoformat()

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / log_path.name

    kept_lines = 0
    total_lines = 0
    last_known_ts: Optional[datetime] = None
    # Lines without timestamps are included if the previous timestamped line was in-window
    in_window = False

    try:
        with open(log_path, "r", errors="replace") as fin, \
             open(output_path, "w", encoding="utf-8") as fout:

            for line in fin:
                total_lines += 1
                ts = parse_timestamp(line)

                if ts is not None:
                    last_known_ts = ts
                    in_window = window_start <= ts <= window_end

                if in_window:
                    fout.write(line)
                    kept_lines += 1
                    if kept_lines >= max_output_lines:
                        fout.write(
                            f"\n... [log_windower: truncated at {max_output_lines} lines] ...\n"
                        )
                        break

    except Exception as e:
        logger.error("Failed to window %s: %s", log_path.name, e)
        metadata["reason"] = f"error: {e}"
        return log_path, metadata

    metadata["windowed"] = True
    metadata["total_lines"] = total_lines
    metadata["kept_lines"] = kept_lines
    metadata["reduction_pct"] = round((1 - kept_lines / max(total_lines, 1)) * 100, 1)
    metadata["output_size_bytes"] = output_path.stat().st_size

    logger.info(
        "Windowed %s: %d → %d lines (%.1f%% reduction), %s → %s",
        log_path.name,
        total_lines,
        kept_lines,
        metadata["reduction_pct"],
        _fmt_size(file_size),
        _fmt_size(metadata["output_size_bytes"]),
    )

    # If windowed output is empty or reduction < 20%, return original
    if kept_lines == 0:
        logger.info("Windowing produced 0 lines (time window mismatch), using original file")
        output_path.unlink(missing_ok=True)
        metadata["windowed"] = False
        metadata["reason"] = "no_lines_in_window"
        return log_path, metadata

    if metadata["reduction_pct"] < 20:
        logger.info("Windowing reduction < 20%%, using original file")
        output_path.unlink(missing_ok=True)
        metadata["windowed"] = False
        metadata["reason"] = "insufficient_reduction"
        return log_path, metadata

    return output_path, metadata


def window_log_files(
    log_paths: List[Path],
    output_dir: Path,
    center_time: Optional[datetime] = None,
    hours_before: int = 4,
    hours_after: int = 2,
    size_threshold: int = DEFAULT_SIZE_THRESHOLD,
) -> Tuple[List[Path], List[dict]]:
    """Window multiple log files. Returns (windowed_paths, metadata_list)."""
    windowed_paths = []
    all_metadata = []

    for lp in log_paths:
        if not lp.exists():
            continue
        path, meta = window_log_file(
            lp, output_dir,
            center_time=center_time,
            hours_before=hours_before,
            hours_after=hours_after,
            size_threshold=size_threshold,
        )
        windowed_paths.append(path)
        all_metadata.append(meta)

    return windowed_paths, all_metadata


def infer_center_time_from_extraction(extraction: dict) -> Optional[datetime]:
    """Infer the problem time from L1 extraction results.

    Strategy: find timestamps in matched log lines. The median timestamp
    of high-signal matches (errors, warnings, anomalies) is our best guess
    for when the problem occurred.
    """
    timestamps: List[datetime] = []

    patterns = extraction.get("patterns", {})
    for _name, value in patterns.items():
        if not isinstance(value, dict):
            continue
        for match_line in value.get("matches", []):
            ts = parse_timestamp(str(match_line))
            if ts:
                timestamps.append(ts)

    # Also check deterministic sections (e.g., recording timelines)
    deterministic = extraction.get("deterministic", {})
    for _key, det_data in deterministic.items():
        if isinstance(det_data, str):
            for line in det_data.split("\n"):
                ts = parse_timestamp(line)
                if ts:
                    timestamps.append(ts)
        elif isinstance(det_data, dict):
            for v in det_data.values():
                if isinstance(v, str):
                    ts = parse_timestamp(v)
                    if ts:
                        timestamps.append(ts)

    if not timestamps:
        return None

    # Use median timestamp as center
    timestamps.sort()
    median_idx = len(timestamps) // 2
    center = timestamps[median_idx]
    logger.info(
        "Inferred center_time from L1 extraction: %s (from %d timestamps, range: %s → %s)",
        center, len(timestamps), timestamps[0], timestamps[-1],
    )
    return center


def find_error_dense_window(
    log_path: Path,
    sample_every_n: int = 100,
) -> Optional[datetime]:
    """Scan a log file for error-dense periods and return the center of the densest hour.

    This is a fallback for when no problem_date and no L1 extraction timestamps.
    Samples every Nth line for speed on large files.
    """
    hour_errors: dict[str, int] = {}

    try:
        with open(log_path, "r", errors="replace") as f:
            for i, line in enumerate(f):
                if i % sample_every_n != 0:
                    continue
                if not _ERROR_KEYWORDS.search(line):
                    continue
                ts = parse_timestamp(line)
                if ts:
                    hour_key = ts.strftime("%Y-%m-%d %H")
                    hour_errors[hour_key] = hour_errors.get(hour_key, 0) + 1
    except Exception:
        return None

    if not hour_errors:
        return None

    # Find the hour with the most errors
    densest_hour = max(hour_errors, key=hour_errors.get)  # type: ignore[arg-type]
    try:
        center = datetime.strptime(densest_hour, "%Y-%m-%d %H").replace(minute=30)
        logger.info(
            "Found error-dense window: %s (%d sampled errors), total hours with errors: %d",
            densest_hour, hour_errors[densest_hour], len(hour_errors),
        )
        return center
    except Exception:
        return None


def _fmt_size(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes}B"
    if nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f}KB"
    return f"{nbytes / 1024 / 1024:.1f}MB"
