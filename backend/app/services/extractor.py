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

logger = logging.getLogger("jarvis.extractor")


def grep_log(
    log_path: Path,
    pattern: str,
    date_filter: Optional[str] = None,
    max_lines: int = 30,
) -> List[str]:
    """
    Run grep on a log file with optional date prefix filter.
    Returns matching lines (up to max_lines).
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
        return lines[:max_lines]
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
                "matches": all_matches[:20],  # cap to keep prompt under 50KB
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

    return extraction
