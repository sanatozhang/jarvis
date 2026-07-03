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


def _select_log_era(log_paths: List[Path], pats: Dict[str, Any]) -> Dict[str, str]:
    """Resolve app_version / platform / os_version / device_model / engine from
    the NEWEST era present in the logs.

    Why not head-500: logs are append-only, so the first cold-start is the
    OLDEST state. A device upgraded 3.x flutter → 4.x native keeps writing the
    same plaud.log, so one file holds both eras with flutter first. The ticket's
    crash is recent → the native era is the relevant one.

    Strategy: grep the whole file for flutter (HTTP-header) and native
    (bracketed startup) markers, keep only the last N *matching* lines (grep
    streams; `tail` gives the most recent markers). Flutter markers recur on
    every request while native markers appear only at cold start — restricting
    to matching lines keeps native startup lines even when they're sparse.
    Within a window we take the last (most-recent) assignment; a >=4.0 native
    marker classifies the log as native and overrides any flutter values from
    older lines. The >=4.0 guard means a flutter build's own Datadog line
    ("version: 3.x+n") never mis-triggers native.
    """
    grep_re = (
        "app-version:|app-platform:|User-Agent: PLAUD/|"
        "AppBuildInfo:|DatadogConfig initialized|DeviceInfoManager"
    )
    fl = {"ver": "", "plat": "", "os": "", "dev": ""}
    nat = {"ver": "", "plat": "", "os": "", "dev": ""}
    for lp in log_paths:
        try:
            r = subprocess.run(
                f'grep -aE "{grep_re}" "{lp}" | tail -n 400',
                shell=True, capture_output=True, text=True, timeout=20,
            )
            lines = (r.stdout or "").split("\n")
        except Exception:
            continue
        for line in lines:  # chronological → last assignment wins (most recent)
            m = pats["ver"].search(line)
            if m:
                fl["ver"] = m.group(1).strip()
            m = pats["plat"].search(line)
            if m:
                fl["plat"] = m.group(1).strip().lower()
            m = pats["ua"].search(line)
            if m:
                fl["os"] = m.group(1).strip()
                fl["dev"] = f"{m.group(2).strip()} {m.group(3).strip()}"
            m = pats["nver_ios"].search(line)
            if m:
                nat["ver"] = m.group(1).strip()
                nat["plat"] = "ios"
            m = pats["nver_and"].search(line)
            if m:
                nat["ver"] = m.group(1).strip()
                nat["plat"] = "android"
            m = pats["nos_ios"].search(line)
            if m:
                nat["dev"] = m.group(1).strip()
                nat["os"] = f"iOS {m.group(2).strip()}"
                nat["plat"] = nat["plat"] or "ios"
            m = pats["nos_and"].search(line)
            if m:
                nat["dev"] = m.group(1).strip()
                nat["os"] = f"Android {m.group(2).strip()}"
                nat["plat"] = nat["plat"] or "android"
            if not nat["plat"] and pats["nbundle_ios"].search(line):
                nat["plat"] = "ios"

    # Era = major of the NEWEST app version, from whichever marker carried it.
    # Ground truth (real native log): the native app hits the same backend and
    # DOES emit "app-version: 4.0.100 (822)" / "User-Agent: PLAUD/4.0.100(..;iOS 26.5;..)"
    # headers — so the flutter-style path alone already yields the 4.x version.
    # flutter is 3.x, native is 4.x, so the version number is the reliable era
    # signal; native startup markers (AppBuildInfo/DeviceInfoManager) are only
    # preferred when present, for a clean version ("4.0.100" vs "4.0.100 (822)")
    # and an explicit OS name. The >=4 test also means a flutter build's own
    # Datadog line ("version: 3.x+n") never mis-triggers native.
    def _major(v: str) -> int:
        try:
            return int(v.split(".")[0].strip())
        except Exception:
            return 0

    if _major(nat["ver"]) >= 4 or _major(fl["ver"]) >= 4:
        ver = nat["ver"] or fl["ver"]
        plat = nat["plat"] or fl["plat"]
        os_v = nat["os"] or fl["os"]
        dev = nat["dev"] or fl["dev"]
        if not os_v and plat:
            os_v = "iOS" if plat == "ios" else "Android"
        return {
            "app_version": ver, "platform": plat,
            "os_version": os_v, "device_model": dev, "engine": "native",
        }
    return {
        "app_version": fl["ver"], "platform": fl["plat"],
        "os_version": fl["os"], "device_model": fl["dev"],
        "engine": "flutter" if (fl["ver"] or fl["plat"] or fl["os"]) else "",
    }


def extract_log_metadata(log_paths: List[Path]) -> Dict[str, Any]:
    """
    Extract basic device/user metadata from log files.

    Parses each log for structured fields like app version, OS version, device
    model, UID, file IDs, etc. Version/OS/platform/engine come from the NEWEST
    era (see _select_log_era); other fields from the head + whole-file greps.
    """
    meta: Dict[str, Any] = {
        "app_version": "",
        "build_info": "",
        "os_version": "",
        "platform": "",
        "engine": "",
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

    # Native (4.x) startup-line markers. The native app writes NO flutter-style
    # "app-version:" / "User-Agent: PLAUD/.." header blocks; its os/version only
    # appear in bracketed startup lines. Consumed by _select_log_era().
    #   iOS     "[ts] [INFO] [Startup] AppBuildInfo: version=4.0.100, build=813, bundleId=ai.plaud.ios.plaud"
    #           "[ts] [INFO] [Startup] DeviceInfoManager: model=iPhone16,2, os=18.5, deviceId=.."
    #   Android "[ts] [I] [PLAUD] DatadogConfig initialized .. version: 4.0.100+813"
    #           "[ts] [I] [PLAUD] DeviceInfoManager init: model=Pixel 8, brand=google, os=14"
    re_native_ver_ios = re.compile(r"AppBuildInfo:\s*version=(\d+\.\d+\.\d+)")
    re_native_ver_android = re.compile(r"DatadogConfig initialized.*?version:\s*(\d+\.\d+\.\d+)\+\d+")
    re_native_os_ios = re.compile(r"DeviceInfoManager:\s*model=(.+?),\s*os=([\d.]+)")
    re_native_os_android = re.compile(r"DeviceInfoManager init:\s*model=(.+?),.*?\bos=([\d.]+)")
    re_native_bundle_ios = re.compile(r"bundleId=ai\.plaud\.ios")

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

        # NOTE: app_version / platform / os_version / device_model / engine are
        # intentionally NOT read here — they are era-sensitive and resolved from
        # the NEWEST markers by _select_log_era() below (head-500 would lock onto
        # the oldest cold-start, i.e. the stale flutter era on an upgraded device).
        for line in head_text.split("\n"):
            if not meta["build_info"]:
                m = re_build_info.search(line)
                if m:
                    meta["build_info"] = m.group(1).strip()

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

    # --- Era-sensitive fields: app_version / platform / os / device / engine ---
    # Logs append chronologically, so the RELEVANT era is the NEWEST. On a device
    # upgraded 3.x flutter → 4.x native the SAME plaud.log holds both eras; the
    # ticket's crash is recent → native. _select_log_era greps the whole file for
    # both flutter (HTTP-header) and native (bracketed startup) markers, keeps the
    # most-recent, and lets a >=4.0 native marker override stale flutter values.
    era = _select_log_era(log_paths, {
        "ua": re_user_agent, "ver": re_app_version, "plat": re_platform,
        "nver_ios": re_native_ver_ios, "nver_and": re_native_ver_android,
        "nos_ios": re_native_os_ios, "nos_and": re_native_os_android,
        "nbundle_ios": re_native_bundle_ios,
    })
    meta["app_version"] = era["app_version"]
    meta["platform"] = era["platform"]
    meta["os_version"] = era["os_version"]
    meta["device_model"] = era["device_model"]
    meta["engine"] = era["engine"]

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
