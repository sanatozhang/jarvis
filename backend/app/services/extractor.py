"""
Pre-extraction service: runs grep patterns from rules against log files.

This is the deterministic layer (L1) that reduces multi-MB logs to
structured KB-sized data before sending to the LLM Agent.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.models.schemas import PreExtractPattern, Rule
from app.services.cloud_sync_parser import parse_cloud_sync_summary
from app.services.recording_missing_parser import parse_recording_missing_timeline

logger = logging.getLogger("jarvis.extractor")


def grep_log(
    log_path: Path,
    pattern: str,
    date_filter: Optional[str] = None,
    max_lines: int = 200,
) -> List[str]:
    """
    Run grep on a log file with optional date prefix filter.
    Returns matching lines (up to max_lines).
    If date_filter yields no results, automatically falls back to no date filter.
    """
    try:
        if date_filter:
            # Two-stage grep: filter by date, then by pattern
            cmd = (
                f'grep "{date_filter}" "{log_path}" | grep -E "{pattern}" | head -n {max_lines}'
            )
        else:
            cmd = f'grep -E "{pattern}" "{log_path}" | head -n {max_lines}'

        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        lines = lines[:max_lines]

        # Fallback: if date_filter returned nothing, retry without it
        if date_filter and not lines:
            logger.debug(
                "date_filter '%s' returned no results for pattern '%s', retrying without filter",
                date_filter, pattern,
            )
            cmd_fallback = f'grep -E "{pattern}" "{log_path}" | head -n {max_lines}'
            result2 = subprocess.run(
                cmd_fallback, shell=True, capture_output=True, text=True, timeout=30
            )
            lines = result2.stdout.strip().split("\n") if result2.stdout.strip() else []
            lines = lines[:max_lines]

        return lines
    except subprocess.TimeoutExpired:
        logger.warning("grep timed out for pattern '%s' on %s", pattern, log_path)
        return [f"[TIMEOUT] grep timed out for pattern: {pattern}"]
    except Exception as e:
        logger.error("grep failed: %s", e)
        return [f"[ERROR] {e}"]


def count_matches(log_path: Path, pattern: str) -> int:
    """Count occurrences of a pattern in a log file."""
    try:
        result = subprocess.run(
            f'grep -cE "{pattern}" "{log_path}"',
            shell=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return int(result.stdout.strip()) if result.stdout.strip() else 0
    except Exception:
        return 0


def get_log_info(log_path: Path) -> Dict[str, Any]:
    """Get basic info about a log file (size, line count, date range)."""
    info: Dict[str, Any] = {
        "path": str(log_path),
        "size_bytes": 0,
        "line_count": 0,
        "first_date": "",
        "last_date": "",
    }
    try:
        info["size_bytes"] = log_path.stat().st_size

        result = subprocess.run(
            f'wc -l < "{log_path}"',
            shell=True, capture_output=True, text=True, timeout=10,
        )
        info["line_count"] = int(result.stdout.strip()) if result.stdout.strip() else 0

        # First date
        result = subprocess.run(
            f'head -5 "{log_path}" | grep -oE "\\d{{4}}-\\d{{2}}-\\d{{2}}" | head -1',
            shell=True, capture_output=True, text=True, timeout=10,
        )
        info["first_date"] = result.stdout.strip()

        # Last date
        result = subprocess.run(
            f'tail -5 "{log_path}" | grep -oE "\\d{{4}}-\\d{{2}}-\\d{{2}}" | tail -1',
            shell=True, capture_output=True, text=True, timeout=10,
        )
        info["last_date"] = result.stdout.strip()
    except Exception as e:
        logger.warning("Failed to get log info for %s: %s", log_path, e)

    return info


def extract_log_metadata(log_paths: List[Path]) -> Dict[str, Any]:
    """
    Extract basic device/user metadata from log files.

    Parses the first ~200 lines of each log for structured fields like
    app version, OS version, device model, UID, file IDs, etc.
    """
    meta: Dict[str, Any] = {
        "app_version": "",
        "build_info": "",
        "os_version": "",
        "platform": "",
        "device_model": "",
        "uid": "",
        "locale": "",
        "api_region": "",
        "file_ids": [],
    }

    # Patterns (compiled once)
    re_user_agent = re.compile(
        r"User-Agent:\s*PLAUD/[\d.]+\(build:\d+;([^;]+);([^;]+);([^)]+)\)"
    )
    re_app_version = re.compile(r"app-version:\s*(.+)")
    re_build_info = re.compile(r"buildInfo:\s*(.+)")
    re_platform = re.compile(r"app-platform:\s*(\w+)")
    re_uid = re.compile(r'"uid"\s*:\s*"([a-f0-9]{20,})"')
    re_locale = re.compile(r"deviceLocale:\s*\[([^\]]+)\]")
    re_region = re.compile(r"RegionManager:.*api\s*=\s*(https?://[^\s]+)")
    re_file_id = re.compile(r'"file_id"\s*:\s*"([a-f0-9]{16,})"')

    seen_file_ids: set[str] = set()

    for lp in log_paths:
        try:
            result = subprocess.run(
                f'head -n 500 "{lp}"',
                shell=True, capture_output=True, text=True, timeout=10,
            )
            head_text = result.stdout or ""
        except Exception:
            continue

        for line in head_text.split("\n"):
            if not meta["build_info"]:
                m = re_build_info.search(line)
                if m:
                    meta["build_info"] = m.group(1).strip()

            if not meta["app_version"]:
                m = re_app_version.search(line)
                if m:
                    meta["app_version"] = m.group(1).strip()

            if not meta["platform"]:
                m = re_platform.search(line)
                if m:
                    meta["platform"] = m.group(1).strip().lower()

            if not meta["os_version"] or not meta["device_model"]:
                m = re_user_agent.search(line)
                if m:
                    meta["os_version"] = m.group(1).strip()
                    meta["device_model"] = f"{m.group(2).strip()} {m.group(3).strip()}"

            if not meta["uid"]:
                m = re_uid.search(line)
                if m:
                    meta["uid"] = m.group(1)

            if not meta["locale"]:
                m = re_locale.search(line)
                if m:
                    meta["locale"] = m.group(1).strip()

            if not meta["api_region"]:
                m = re_region.search(line)
                if m:
                    meta["api_region"] = m.group(1).strip()

        # uid: may appear beyond head 500 — grep the whole file
        if not meta["uid"]:
            try:
                uid_result = subprocess.run(
                    f'grep -oE \'"uid"\\s*:\\s*"[a-f0-9]{{20,}}"\' "{lp}" | head -1',
                    shell=True, capture_output=True, text=True, timeout=10,
                )
                uid_line = (uid_result.stdout or "").strip()
                if uid_line:
                    m = re_uid.search(uid_line)
                    if m:
                        meta["uid"] = m.group(1)
            except Exception:
                pass

        # file_ids: scan entire file (they can appear anywhere)
        try:
            result2 = subprocess.run(
                f'grep -oE \'"file_id"\\s*:\\s*"[a-f0-9]{{16,}}"\' "{lp}" | head -n 50',
                shell=True, capture_output=True, text=True, timeout=15,
            )
            for line in (result2.stdout or "").split("\n"):
                m = re_file_id.search(line)
                if m and m.group(1) not in seen_file_ids:
                    seen_file_ids.add(m.group(1))
        except Exception:
            pass

    meta["file_ids"] = sorted(seen_file_ids)
    # Strip empty values for cleaner output
    return {k: v for k, v in meta.items() if v}


def extract_for_rules(
    rules: List[Rule],
    log_paths: List[Path],
    problem_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run all pre_extract patterns from matched rules against the log files.
    Returns a structured dict ready for the LLM.
    """
    extraction: Dict[str, Any] = {
        "log_info": [],
        "patterns": {},
        "error_summary": {},
        "deterministic": {},
    }

    # Basic log info
    for lp in log_paths:
        extraction["log_info"].append(get_log_info(lp))

    # Run patterns from each rule
    for rule in rules:
        for pat in rule.meta.pre_extract:
            key = f"{rule.meta.id}.{pat.name}"
            all_matches: List[str] = []
            for lp in log_paths:
                date_f = problem_date if pat.date_filter else None
                matches = grep_log(lp, pat.pattern, date_filter=date_f)
                all_matches.extend(matches)
            extraction["patterns"][key] = {
                "pattern": pat.pattern,
                "date_filter": pat.date_filter,
                "match_count": len(all_matches),
                "matches": all_matches[:200],
            }

    # Always extract error summary
    for lp in log_paths:
        error_count = count_matches(lp, r"error|ERROR|Error")
        exception_count = count_matches(lp, r"exception|Exception|EXCEPTION")
        fail_count = count_matches(lp, r"fail|失败|FAIL")

        extraction["error_summary"][str(lp)] = {
            "errors": error_count,
            "exceptions": exception_count,
            "failures": fail_count,
        }

    rule_ids = {rule.meta.id for rule in rules}
    if rule_ids & {"recording-missing", "timestamp-drift"}:
        extraction["deterministic"]["recording_missing_timeline"] = (
            parse_recording_missing_timeline(log_paths, problem_date=problem_date)
        )
    if rule_ids & {"cloud-sync", "file-transfer"}:
        extraction["deterministic"]["cloud_sync_summary"] = (
            parse_cloud_sync_summary(log_paths, problem_date=problem_date)
        )

    return extraction
