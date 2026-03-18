"""
Lost File Finder — port of LostFileAnalyzer.swift.

Analyzes a Plaud device log to find recordings that may have been synced
but are not visible to the user due to device-clock anomalies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional


ANOMALY_THRESHOLD_SECONDS = 6 * 3600  # 6 hours


# ---------------------------------------------------------------------------
# Patterns (mirroring the Swift regexes)
# ---------------------------------------------------------------------------
_RE_SYNC = re.compile(r"_syncFinish:(\d+)")
_RE_KEY = re.compile(r"keyId=(\d+)", re.IGNORECASE)
_RE_DURATION = re.compile(r"文件时长=(\d+)")
_RE_DURATION_EN = re.compile(r"file_duration=(\d+)", re.IGNORECASE)
_RE_TRANSPORT = re.compile(r"传输时长=(\d+)")
_RE_APP_DISPLAY = re.compile(r"开始校验文件:\[(\d+)\]\s*\[(.*?)\]")
_RE_TIMESTAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)"
)


@dataclass
class LostFileRecord:
    key_id: str
    sync_time: Optional[datetime] = None
    app_display_time: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    transport_seconds: Optional[int] = None
    session_time: Optional[datetime] = None

    @property
    def display_time(self) -> Optional[datetime]:
        return self.app_display_time or self.session_time

    @property
    def primary_timestamp(self) -> Optional[datetime]:
        return self.sync_time or self.display_time

    @property
    def sync_drift(self) -> Optional[float]:
        if self.sync_time and self.display_time:
            return abs((self.sync_time - self.display_time).total_seconds())
        return None

    def remark(self) -> str:
        if self.sync_time is None or self.display_time is None:
            return "ℹ️ 信息不全"
        delta = abs((self.sync_time - self.display_time).total_seconds())
        if delta >= ANOMALY_THRESHOLD_SECONDS:
            return "⚠️ 时间戳异常"
        return "✅ 正常"

    def is_anomaly(self) -> bool:
        return self.remark() == "⚠️ 时间戳异常"

    def duration_description(self) -> str:
        if self.duration_seconds is None:
            return "-"
        if self.duration_seconds >= 90:
            minutes = round(self.duration_seconds / 60)
            return f"{minutes}分钟"
        return f"{self.duration_seconds}秒"

    def difference_description(self, tz: timezone) -> Optional[str]:
        if self.sync_time is None or self.display_time is None:
            return None
        delta = (self.sync_time - self.display_time).total_seconds()
        direction = "晚" if delta >= 0 else "早"
        seconds = abs(delta)
        if seconds >= 86_400:
            days = round(seconds / 86_400)
            return f"比同步时间{direction}{days}天"
        elif seconds >= 3_600:
            hours = round(seconds / 3_600)
            return f"比同步时间{direction}{hours}小时"
        elif seconds >= 60:
            minutes = round(seconds / 60)
            return f"比同步时间{direction}{minutes}分钟"
        fmt_sync = _fmt_dt(self.sync_time, tz)
        return f"同步时间为 {fmt_sync}"


@dataclass
class LostFileAnalysisResult:
    problem_date_text: str
    timezone_label: str
    total_records: int
    anomaly_count: int
    markdown: str


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def analyze_log(
    log_content: str,
    problem_date: datetime,
    tz_offset_hours: float = 8.0,
) -> LostFileAnalysisResult:
    """
    Analyze a plaud log file for lost recordings.

    :param log_content: The raw text content of the decrypted .log file.
    :param problem_date: The date from which to search (start of day in given tz).
    :param tz_offset_hours: UTC offset in hours (default +8 China Standard Time).
    :raises ValueError: If the log has no parseable lines or no records after the date.
    """
    tz = timezone(timedelta(hours=tz_offset_hours))
    tz_label = _tz_label(tz_offset_hours)

    # Anchor the problem start to midnight in the given tz
    problem_start = problem_date.replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=tz
    )
    problem_date_text = problem_start.strftime("%Y-%m-%d")

    records: dict[str, LostFileRecord] = {}
    matched_lines = 0

    for line in log_content.splitlines():
        line = line.strip()
        if not line:
            continue

        log_time = _parse_line_timestamp(line, tz)

        # 1. Sync finish event — carries keyId as the captured group
        m = _RE_SYNC.search(line)
        if m:
            key = m.group(1)
            rec = records.setdefault(key, LostFileRecord(key_id=key))
            if rec.sync_time is None:
                rec.sync_time = log_time
            if rec.session_time is None:
                rec.session_time = _session_date(key, tz)
            matched_lines += 1
            continue

        # 2. File transfer completion / duration lines
        if "文件传输完成埋点" in line or "file_duration" in line.lower():
            km = _RE_KEY.search(line)
            if not km:
                continue
            key = km.group(1)
            rec = records.setdefault(key, LostFileRecord(key_id=key))
            if rec.sync_time is None:
                rec.sync_time = log_time
            if rec.session_time is None:
                rec.session_time = _session_date(key, tz)
            if rec.duration_seconds is None:
                dm = _RE_DURATION.search(line) or _RE_DURATION_EN.search(line)
                if dm:
                    rec.duration_seconds = int(dm.group(1))
            if rec.transport_seconds is None:
                tm = _RE_TRANSPORT.search(line)
                if tm:
                    rec.transport_seconds = int(tm.group(1))
            matched_lines += 1
            continue

        # 3. App display verification event
        if "开始校验文件" in line:
            am = _RE_APP_DISPLAY.search(line)
            if not am:
                continue
            key = am.group(1).strip()
            app_raw = am.group(2).strip()
            if not key:
                continue
            rec = records.setdefault(key, LostFileRecord(key_id=key))
            if rec.session_time is None:
                rec.session_time = _session_date(key, tz)
            if rec.app_display_time is None and app_raw:
                rec.app_display_time = _parse_display_time(app_raw, tz)
            if rec.sync_time is None:
                rec.sync_time = log_time
            matched_lines += 1
            continue

    if matched_lines == 0:
        raise ValueError("日志内容为空或无法解析，请确认上传了正确的设备日志文件。")

    # Fill missing app_display_time from session_time
    for rec in records.values():
        if rec.app_display_time is None:
            rec.app_display_time = rec.session_time

    # Filter to records after problem_start
    filtered = [
        rec for rec in records.values()
        if rec.primary_timestamp is not None and rec.primary_timestamp >= problem_start
    ]
    filtered.sort(key=lambda r: r.primary_timestamp or datetime.min.replace(tzinfo=tz))

    if not filtered:
        raise ValueError(f"未找到 {problem_date_text} 之后的同步记录。请尝试选择更早的日期，或确认日志文件来自正确的设备。")

    anomalies = [r for r in filtered if r.is_anomaly()]
    markdown = _build_markdown(filtered, anomalies, problem_date_text, tz_label, tz)

    return LostFileAnalysisResult(
        problem_date_text=problem_date_text,
        timezone_label=tz_label,
        total_records=len(filtered),
        anomaly_count=len(anomalies),
        markdown=markdown,
    )


# ---------------------------------------------------------------------------
# Markdown builder
# ---------------------------------------------------------------------------

def _build_markdown(
    records: list[LostFileRecord],
    anomalies: list[LostFileRecord],
    problem_date_text: str,
    tz_label: str,
    tz: timezone,
) -> str:
    lines: list[str] = []
    lines.append(
        f"您好，经过日志分析，{problem_date_text} 之后共同步了 **{len(records)}** 个文件"
        f"（参考时区 {tz_label}）：\n"
    )
    lines.append("| 同步时间 | APP 显示时间 | 文件时长 | 备注 |")
    lines.append("|---------|-------------|---------|------|")
    for rec in records:
        sync_text = _fmt_dt(rec.sync_time, tz)
        app_text = _fmt_dt(rec.app_display_time, tz)
        lines.append(f"| {sync_text} | {app_text} | {rec.duration_description()} | {rec.remark()} |")

    lines.append("")
    lines.append("**分析最可能的目标文件：**")

    candidates = _highlight_candidates(records, anomalies)
    if not candidates:
        lines.append(
            "- 暂无可用于比对的同步/显示时间，请确认日志是否包含 `_syncFinish` 与 `开始校验文件` 记录。"
        )
    elif not anomalies:
        lines.append("未检测到明显的时间戳异常，可结合下方漂移最大的文件继续手动排查：")
        for rec in candidates:
            lines.append(_describe_candidate(rec, tz))
    else:
        lines.append("其中，**最可能**是您要找的录音：")
        for rec in candidates:
            lines.append(_describe_candidate(rec, tz))
        lines.append(
            "\n这些文件的同步时间与 APP 显示时间差距较大，符合时间戳错误特征，"
            "请在 APP 中按上述显示时间搜索。"
        )

    lines.append(
        "\n> 根据排查指引：请告知用户这是设备时间戳异常导致的显示偏差，"
        "文件实际已同步成功，可按上表中的 APP 显示时间搜索。"
    )
    return "\n".join(lines)


def _highlight_candidates(
    records: list[LostFileRecord],
    anomalies: list[LostFileRecord],
    limit: int = 3,
) -> list[LostFileRecord]:
    source = anomalies if anomalies else records
    return sorted(source, key=lambda r: r.sync_drift or 0, reverse=True)[:limit]


def _describe_candidate(rec: LostFileRecord, tz: timezone) -> str:
    app_text = _fmt_dt(rec.display_time, tz)
    sync_text = _fmt_dt(rec.sync_time, tz)
    parts: list[str] = []
    if sync_text != "-":
        parts.append(f"同步时间 {sync_text}")
    parts.append(f"时长 {rec.duration_description()}")
    if rec.transport_seconds is not None:
        parts.append(f"传输用时 {rec.transport_seconds} 秒")
    diff = rec.difference_description(tz)
    if diff:
        parts.append(diff)
    parts.append(f"keyId={rec.key_id}")
    return f"- **{app_text}**（{', '.join(parts)}）"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_line_timestamp(line: str, tz: timezone) -> Optional[datetime]:
    m = _RE_TIMESTAMP.search(line)
    if not m:
        return None
    raw = m.group(1).replace(",", ".")
    # Strip fractional seconds for initial parse
    dot_idx = raw.find(".")
    if dot_idx != -1:
        base = raw[:dot_idx]
        frac = float("0." + raw[dot_idx + 1 :dot_idx + 7])
    else:
        base = raw
        frac = 0.0
    try:
        dt = datetime.strptime(base, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        if frac:
            dt = dt.replace(microsecond=int(frac * 1_000_000))
        return dt
    except ValueError:
        return None


def _parse_display_time(raw: str, tz: timezone) -> Optional[datetime]:
    """Parse the app-display time string captured from the log line."""
    raw = raw.strip().replace(",", ".")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def _session_date(key: str, tz: timezone) -> Optional[datetime]:
    """Derive approximate recording start time from a 10-digit unix timestamp prefix in keyId."""
    digits = key.strip()
    if len(digits) < 10:
        return None
    try:
        ts = int(digits[:10])
        return datetime.fromtimestamp(ts, tz=tz)
    except (ValueError, OverflowError, OSError):
        return None


def _fmt_dt(dt: Optional[datetime], tz: timezone) -> str:
    if dt is None:
        return "-"
    # Convert to target tz if the datetime is tz-aware
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _tz_label(offset_hours: float) -> str:
    h = int(offset_hours)
    m = int(abs(offset_hours - h) * 60)
    if m == 0:
        return f"UTC{h:+d}"
    return f"UTC{h:+d}:{m:02d}"
