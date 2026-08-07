"""VOC weekly insight digest — deterministic stats layer (this file) plus
LLM narrative generation (generate_weekly_digest, added by a later task).

Pure functions in this section take rows shaped like
app.db.database.get_voc_analysis_rows()'s return value and do no I/O — kept
separate from the DB/LLM-calling orchestration below so they're trivially
unit-testable and reusable from both the /api/voc/trend and /api/voc/movers
endpoints and the weekly digest generator.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from app.config import get_settings
from app.services import claude_headless

logger = logging.getLogger("jarvis.voc_digest")


def default_week_start(today: Optional[date] = None) -> str:
    """Most recent COMPLETE week's Monday (ISO date string). If `today` is
    itself mid-week, that week isn't done yet, so this points at the week
    before it — e.g. on Wed 2026-08-12, returns 2026-08-03 (last Monday),
    not 2026-08-10 (this week's Monday, still in progress). On a Monday,
    "today" is the very start of a new week, so this still returns the
    prior Monday — the week that just finished."""
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    return last_monday.isoformat()


def _primary_tag(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The primary VOC tag dict for a row, or None if untagged/malformed."""
    try:
        tags = json.loads(row.get("voc_tags_json") or "[]")
    except (ValueError, TypeError):
        return None
    for t in tags:
        if isinstance(t, dict) and t.get("role") == "primary":
            return t
    return None


def _level_key(tag: Dict[str, Any], level: str) -> str:
    group = tag.get("level_1_category") or "未分类"
    if level == "group":
        return group
    label = tag.get("level_2_label") or ""
    return f"{group} › {label}" if label else group


def _row_date(row: Dict[str, Any]) -> str:
    v = row.get("created_at")
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return str(v)[:10]


def _count_by_key(rows: List[Dict[str, Any]], level: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        tag = _primary_tag(row)
        if not tag:
            continue
        key = _level_key(tag, level)
        counts[key] = counts.get(key, 0) + 1
    return counts


def aggregate_trend(rows: List[Dict[str, Any]], level: str = "group") -> Dict[str, Dict[str, int]]:
    """`{date_str: {key: count}}` for a multi-line trend chart. Rows with no
    primary VOC tag (never classified, or classifier fell back) are skipped
    — they carry no group/label to bucket by."""
    trend: Dict[str, Dict[str, int]] = {}
    for row in rows:
        tag = _primary_tag(row)
        if not tag:
            continue
        d = _row_date(row)
        key = _level_key(tag, level)
        trend.setdefault(d, {})
        trend[d][key] = trend[d].get(key, 0) + 1
    return trend


def aggregate_movers(
    cur_rows: List[Dict[str, Any]], prev_rows: List[Dict[str, Any]],
    level: str = "label", min_base: int = 3,
) -> List[Dict[str, Any]]:
    """Week-over-week movers, sorted by |delta| descending. A key is
    excluded unless `max(cur, prev) >= min_base` — at low ticket volumes,
    e.g. 1 -> 3 tickets is a +200% swing that's pure noise; min_base keeps
    the movers list meaningful rather than dominated by tiny denominators.

    delta_pct is None when prev == 0 (no baseline to compare against —
    frontend should render this as "new" rather than a percentage).
    """
    cur_counts = _count_by_key(cur_rows, level)
    prev_counts = _count_by_key(prev_rows, level)
    keys = set(cur_counts) | set(prev_counts)

    movers = []
    for key in keys:
        cur = cur_counts.get(key, 0)
        prev = prev_counts.get(key, 0)
        if max(cur, prev) < min_base:
            continue
        delta = cur - prev
        delta_pct = round(delta / prev * 100, 1) if prev else None
        movers.append({"key": key, "cur": cur, "prev": prev, "delta": delta, "delta_pct": delta_pct})

    movers.sort(key=lambda m: abs(m["delta"]), reverse=True)
    return movers


def compute_weekly_stats(
    cur_rows: List[Dict[str, Any]], prev_rows: List[Dict[str, Any]], min_base: int = 3,
) -> Dict[str, Any]:
    """Full deterministic stats package for one week: group distribution,
    total volume + week-over-week delta, top movers (label level), the
    needs_engineer rate, and device distribution. This is the ONLY source
    of numbers the LLM narrative (Task 8) is allowed to cite — see that
    task's system prompt."""
    group_counts = _count_by_key(cur_rows, level="group")
    groups = [{"group": g, "count": c} for g, c in sorted(group_counts.items(), key=lambda x: -x[1])]

    total_cur = len(cur_rows)
    total_prev = len(prev_rows)
    total_delta = total_cur - total_prev
    total_delta_pct = round(total_delta / total_prev * 100, 1) if total_prev else None

    top_movers = aggregate_movers(cur_rows, prev_rows, level="label", min_base=min_base)[:10]

    needs_engineer_cur = sum(1 for r in cur_rows if r.get("needs_engineer"))
    needs_engineer_rate = round(needs_engineer_cur / total_cur * 100, 1) if total_cur else 0.0

    device_counts: Dict[str, int] = {}
    for r in cur_rows:
        d = r.get("device_type") or "未知"
        device_counts[d] = device_counts.get(d, 0) + 1
    devices = [{"device_type": d, "count": c} for d, c in sorted(device_counts.items(), key=lambda x: -x[1])]

    return {
        "total_cur": total_cur,
        "total_prev": total_prev,
        "total_delta": total_delta,
        "total_delta_pct": total_delta_pct,
        "groups": groups,
        "top_movers": top_movers,
        "needs_engineer_rate": needs_engineer_rate,
        "devices": devices,
    }


def sample_root_causes(
    rows: List[Dict[str, Any]], top_n_groups: int = 5, max_per_group: int = 8,
) -> Dict[str, List[str]]:
    """Sample real root_cause text per top-volume VOC group — the only raw
    material the LLM narrative (Task 8) has for spotting "this is a product
    problem, not a user error" patterns (e.g. the timestamp/power-loss
    example from the design doc). Deduplicates identical root_cause strings
    within a group and truncates each to 500 chars. Groups with zero
    non-empty root_cause samples are omitted from the result."""
    group_counts = _count_by_key(rows, level="group")
    top_groups = [g for g, _ in sorted(group_counts.items(), key=lambda x: -x[1])[:top_n_groups]]

    samples: Dict[str, List[str]] = {g: [] for g in top_groups}
    seen: Dict[str, set] = {g: set() for g in top_groups}

    for row in rows:
        tag = _primary_tag(row)
        if not tag:
            continue
        group = tag.get("level_1_category") or "未分类"
        if group not in samples:
            continue
        rc = (row.get("root_cause") or "").strip()
        if not rc or rc in seen[group] or len(samples[group]) >= max_per_group:
            continue
        samples[group].append(rc[:500])
        seen[group].add(rc)

    return {g: v for g, v in samples.items() if v}


_DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "key_findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string"},
                    "finding": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["scope", "finding"],
            },
        },
        "product_opportunities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "area": {"type": "string"},
                    "problem": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["area", "problem", "suggestion"],
            },
        },
        "movers_commentary": {"type": "string"},
    },
    "required": ["headline", "key_findings", "product_opportunities"],
}

_DIGEST_REQUIRED_KEYS = ("headline", "key_findings", "product_opportunities")


def _build_digest_system_prompt() -> str:
    return (
        "You are writing a weekly customer-support insight digest for Plaud's "
        "product team, based on support tickets already classified against a "
        "fixed VOC taxonomy.\n\n"
        "You will receive a JSON `stats` object (already-computed group counts, "
        "week-over-week deltas, and top movers) and a `root_cause_samples` "
        "object (real root-cause text sampled from this week's tickets, "
        "grouped by VOC category).\n\n"
        "Rules:\n"
        "- Every number you mention (counts, percentages, deltas) MUST come "
        "verbatim from `stats`. Never compute, round, or restate a number "
        "that isn't already there.\n"
        "- `key_findings` should call out the categories with the highest "
        "volume or the sharpest movers from `stats`.\n"
        "- `product_opportunities` is the most important output. For each "
        "category in `root_cause_samples`, ask: do these root causes describe "
        "something the PRODUCT could prevent or mitigate (a missing hint, a "
        "confusing flow, a hardware limitation users hit repeatedly) rather "
        "than something the user did wrong? Only include an entry if the "
        "sampled root causes actually support that conclusion — it is fine "
        "to return an empty list if nothing this week supports a product "
        "change. Do not invent a plausible-sounding opportunity.\n"
        "- Write in English. Keep `finding`/`problem`/`suggestion` to 1-2 "
        "sentences each."
    )


def _build_digest_user_input(
    stats: Dict[str, Any], root_cause_samples: Dict[str, List[str]],
    week_start: str, week_end: str,
) -> str:
    payload = {
        "week_start": week_start, "week_end": week_end,
        "stats": stats, "root_cause_samples": root_cause_samples,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _render_markdown(
    week_start: str, week_end: str, stats: Dict[str, Any], narrative: Optional[Dict[str, Any]],
) -> str:
    lines = [f"# VOC 周度洞察 · {week_start} ~ {week_end}", ""]
    if narrative and narrative.get("headline"):
        lines += [f"**{narrative['headline']}**", ""]

    total_cur = stats.get("total_cur", 0)
    total_delta_pct = stats.get("total_delta_pct")
    delta_str = f"{total_delta_pct:+.1f}%" if total_delta_pct is not None else "N/A（上周无基线）"
    lines += [f"本周共 {total_cur} 单，环比 {delta_str}。", ""]

    if narrative and narrative.get("key_findings"):
        lines.append("## 关键发现")
        for f in narrative["key_findings"]:
            line = f"- **{f.get('scope', '')}**：{f.get('finding', '')}"
            if f.get("evidence"):
                line += f"（{f['evidence']}）"
            lines.append(line)
        lines.append("")

    if narrative and narrative.get("product_opportunities"):
        lines.append("## 产品优化建议")
        for o in narrative["product_opportunities"]:
            lines.append(f"- **{o.get('area', '')}**：{o.get('problem', '')} → {o.get('suggestion', '')}")
            if o.get("rationale"):
                lines.append(f"  - 依据：{o['rationale']}")
        lines.append("")
    elif narrative is not None:
        lines += ["## 产品优化建议", "", "（本周没有明显可归因为产品问题的重复根因）", ""]

    lines.append("## 分类占比 Top 10")
    for g in stats.get("groups", [])[:10]:
        lines.append(f"- {g['group']}：{g['count']}")
    lines.append("")

    top_movers = stats.get("top_movers", [])
    if top_movers:
        lines.append("## 环比变动 Top 5")
        for m in top_movers[:5]:
            pct = f"{m['delta_pct']:+.0f}%" if m.get("delta_pct") is not None else "new"
            lines.append(f"- {m['key']}：{m['prev']} → {m['cur']}（{pct}）")
        lines.append("")

    if narrative is None:
        lines.append("_洞察生成失败，以上仅为确定性统计。可点击「重新生成」重试。_")

    return "\n".join(lines)


async def generate_weekly_digest(week_start: str, force: bool = False) -> Dict[str, Any]:
    """Generate (or return the cached) weekly digest for the week starting
    `week_start` (Monday, "YYYY-MM-DD"). The deterministic stats half always
    computes; the LLM narrative half degrades to None on any failure
    (disabled, CLI unavailable, timeout, malformed output) — the record is
    ALWAYS written either way, since the numbers alone are useful even
    without a narrative (see _render_markdown's fallback line).
    """
    from app.db import database as db

    if not force:
        existing = await db.get_voc_weekly_digest(week_start)
        if existing:
            return existing

    ws = date.fromisoformat(week_start)
    we = ws + timedelta(days=6)
    prev_ws = ws - timedelta(days=7)
    prev_we = ws - timedelta(days=1)

    cur_rows = await db.get_voc_analysis_rows(ws.isoformat(), we.isoformat())
    prev_rows = await db.get_voc_analysis_rows(prev_ws.isoformat(), prev_we.isoformat())

    stats = compute_weekly_stats(cur_rows, prev_rows)
    root_cause_samples = sample_root_causes(cur_rows)

    settings = get_settings().voc
    narrative: Optional[Dict[str, Any]] = None
    if settings.digest_enabled:
        narrative = await claude_headless.run_json(
            system_prompt=_build_digest_system_prompt(),
            user_input=_build_digest_user_input(stats, root_cause_samples, week_start, we.isoformat()),
            schema=_DIGEST_SCHEMA,
            model=settings.digest_model,
            timeout=float(settings.digest_timeout_seconds),
            log_prefix="voc_digest",
        )
        if narrative is not None:
            missing = [k for k in _DIGEST_REQUIRED_KEYS if k not in narrative]
            if missing:
                logger.warning("VOC digest narrative missing required keys %s — discarding", missing)
                narrative = None

    markdown = _render_markdown(week_start, we.isoformat(), stats, narrative)

    return await db.upsert_voc_weekly_digest(
        week_start=week_start,
        stats=stats,
        narrative=narrative,
        markdown=markdown,
        model=settings.digest_model if narrative else "",
    )
