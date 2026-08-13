"""Fix-version recurrence detection — "we marked this fixed, did it actually
get fixed?" memory for the mark-complete flow.

Pure functions in this section (detect_recurrence and its helpers) take
plain dataclasses/rows and do no I/O — kept separate from the DB/Feishu
orchestration below so the detection algorithm is trivially unit-testable
(app.services.voc_digest follows the same split).

Core idea: a new ticket "recurs" a prior fixed ticket only if (a) they're
textually similar AND (b) the new ticket's reported version is >= the fix
version recorded on the prior ticket. Release cadence here is weekly, and a
fix takes ~2-3 weeks to reach users, so tickets from users still on an
older version are expected noise, not a recurrence — the version gate is
what tells those apart from a genuine "still broken" report.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

from app.services.issue_text import normalize_description_for_matching
from app.services.repo_router import parse_version
from app.services.text_similarity import jaccard_similarity

logger = logging.getLogger("jarvis.recurrence")


@dataclass(frozen=True)
class ResolvedCandidate:
    """A previously-completed issue that might be recurring."""
    issue_id: str
    description: str
    rule_type: str
    fix_target: str        # "" | "app" | "firmware" | "other"
    fix_version: str
    resolved_at: Optional[datetime]
    resolve_reason: str


@dataclass(frozen=True)
class NewTicket:
    """The incoming issue being checked against the resolved-candidate pool."""
    issue_id: str
    description: str
    rule_type: str
    app_version: str
    firmware: str
    version_source: str     # "log_metadata" | "issue_field" | ""
    created_at: datetime


@dataclass(frozen=True)
class RecurrenceHit:
    prior_issue_id: str
    severity: str            # "red" | "yellow"
    similarity: float
    reason_code: str         # version_gte_fix | no_fix_version | version_unparseable | no_version_target
    fix_target: str
    fix_version: str
    compared_version: str
    version_source: str
    prior_resolved_at: Optional[datetime]
    prior_resolve_reason: str


def _version_gate(ticket: NewTicket, candidate: ResolvedCandidate) -> tuple[Optional[str], str]:
    """Decide severity + reason_code from the fix-version gate, or return
    (None, reason_code) to mean "drop this candidate entirely" — that's the
    case where the new ticket's version is BELOW the fix version: a normal
    pre-fix straggler, not a recurrence. Returning None here (rather than
    "yellow") is the crux of the whole feature; without it every stale
    ticket from users who haven't updated yet would count as a recurrence.
    """
    target = candidate.fix_target
    if target == "app":
        compared_version = ticket.app_version
    elif target == "firmware":
        compared_version = ticket.firmware
    else:
        compared_version = ""

    if target not in ("app", "firmware"):
        return "yellow", "no_version_target"
    if not candidate.fix_version:
        return "yellow", "no_fix_version"

    fix_v = parse_version(candidate.fix_version)
    new_v = parse_version(compared_version)
    if fix_v is None or new_v is None:
        # Can't prove the new ticket is on/after the fix version — degrade
        # to yellow rather than silently dropping OR wrongly escalating to
        # red. A false "still broken" red alert (crying wolf) costs more
        # trust than a missed one.
        return "yellow", "version_unparseable"
    if new_v >= fix_v:
        return "red", "version_gte_fix"
    return None, "below_fix_version"


def _compared_version_for(ticket: NewTicket, fix_target: str) -> str:
    if fix_target == "app":
        return ticket.app_version
    if fix_target == "firmware":
        return ticket.firmware
    return ""


def detect_recurrence(
    ticket: NewTicket,
    candidates: Sequence[ResolvedCandidate],
    *,
    threshold: float = 0.30,
    general_threshold: float = 0.45,
    yellow_window_days: int = 90,
    top_k: int = 3,
    now: Optional[datetime] = None,
) -> list[RecurrenceHit]:
    """Compare `ticket` against a pool of previously-resolved candidates
    (same rule_type; caller is expected to have already filtered by that —
    see recurrence.load_resolved_candidates — but this also re-checks it,
    since it's a correctness invariant, not just a query optimization).

    Returns hits sorted red-first, then by similarity descending, capped at
    top_k. An empty list means "no recurrence" — including the case where a
    similar prior issue exists but the new ticket's version is below its
    fix version (expected pre-fix noise, not a hit at all).
    """
    now = now or datetime.utcnow()
    query = normalize_description_for_matching(ticket.description)
    yellow_cutoff = now - timedelta(days=yellow_window_days)

    hits: list[RecurrenceHit] = []
    for candidate in candidates:
        if candidate.issue_id == ticket.issue_id:
            continue
        if candidate.rule_type != ticket.rule_type:
            continue

        candidate_text = normalize_description_for_matching(candidate.description)
        similarity = jaccard_similarity(query, candidate_text)
        effective_threshold = general_threshold if ticket.rule_type == "general" else threshold
        if similarity < effective_threshold:
            continue

        severity, reason_code = _version_gate(ticket, candidate)
        if severity is None:
            continue  # below fix version — normal pre-fix straggler, not a recurrence

        if severity == "yellow":
            if candidate.resolved_at is None or candidate.resolved_at < yellow_cutoff:
                continue

        hits.append(RecurrenceHit(
            prior_issue_id=candidate.issue_id,
            severity=severity,
            similarity=similarity,
            reason_code=reason_code,
            fix_target=candidate.fix_target,
            fix_version=candidate.fix_version,
            compared_version=_compared_version_for(ticket, candidate.fix_target),
            version_source=ticket.version_source,
            prior_resolved_at=candidate.resolved_at,
            prior_resolve_reason=candidate.resolve_reason,
        ))

    hits.sort(key=lambda h: (h.severity != "red", -h.similarity))
    return hits[:top_k]


def compute_recurrence_stats(recurrence_rows: Sequence[dict]) -> dict:
    """Pure aggregation over a window's issue_recurrences rows (plain dicts,
    e.g. from database.get_recurrence_rows) — the recurrence half of the VOC
    weekly digest. Yellow hits are deliberately excluded from `red_hits`:
    they're a weak, unconfirmed signal (no fix_version to gate on), and
    surfacing them in a weekly report would just be noise — see the "only
    show anomalies" convention the daily crashguard reports already follow.
    """
    red = [r for r in recurrence_rows if r.get("severity") == "red"]
    yellow_count = sum(1 for r in recurrence_rows if r.get("severity") == "yellow")
    return {
        "red_count": len(red),
        "yellow_count": yellow_count,
        "red_hits": [
            {
                "new_issue_id": r.get("new_issue_id", ""),
                "prior_issue_id": r.get("prior_issue_id", ""),
                "fix_target": r.get("fix_target", ""),
                "fix_version": r.get("fix_version", ""),
            }
            for r in red
        ],
    }


# ---------------------------------------------------------------------------
# DB / Feishu orchestration — everything below does I/O.
# ---------------------------------------------------------------------------

async def _resolve_ticket_versions(issue_id: str) -> tuple[str, str, str]:
    """(app_version, firmware, version_source) for the recurrence version
    gate. app_version priority: the real *running* version parsed out of
    the user's logs (analyses.log_metadata_json) beats the version the
    user/support typed into the ticket form (issues.app_version, which is
    often missing or wrong) — same priority order the ticket detail page
    uses when displaying it. Firmware has no log-derived source (the log
    extractor doesn't parse a firmware version out of app logs), so it
    always comes from the issues field."""
    import json as _json
    from app.db import database as db

    async with db.get_session() as session:
        issue = await db.get_ticket_record(session, issue_id)
        issue_app_version = getattr(issue, "app_version", "") or "" if issue else ""
        firmware = getattr(issue, "firmware", "") or "" if issue else ""

    analyses = await db.get_all_analyses_by_issue(issue_id)
    log_app_version = ""
    if analyses:
        raw = getattr(analyses[0], "log_metadata_json", None)
        if raw:
            try:
                log_app_version = (_json.loads(raw) or {}).get("app_version", "") or ""
            except (ValueError, TypeError):
                log_app_version = ""

    if log_app_version:
        return log_app_version, firmware, "log_metadata"
    if issue_app_version:
        return issue_app_version, firmware, "issue_field"
    return "", firmware, ""


async def detect_and_record(issue_id: str) -> list[RecurrenceHit]:
    """Load the ticket + candidate pool, run detect_recurrence(), and
    persist every hit (upsert, so re-running for the same ticket is safe).
    Does not alert — see detect_and_alert for the version that also pushes
    to Feishu for red hits."""
    from app.db import database as db

    async with db.get_session() as session:
        issue = await db.get_ticket_record(session, issue_id)
    if issue is None:
        logger.warning("recurrence: issue %s not found, skipping", issue_id)
        return []

    rule_type = getattr(issue, "rule_type", "") or ""
    if not rule_type:
        return []  # no rule classification yet — can't apply the same-rule_type hard filter

    app_version, firmware, version_source = await _resolve_ticket_versions(issue_id)
    ticket = NewTicket(
        issue_id=issue_id,
        description=getattr(issue, "description", "") or "",
        rule_type=rule_type,
        app_version=app_version,
        firmware=firmware,
        version_source=version_source,
        created_at=getattr(issue, "created_at", None) or datetime.utcnow(),
    )

    candidate_rows = await db.load_resolved_candidates(rule_type, exclude_issue_id=issue_id)
    candidates = [ResolvedCandidate(**row) for row in candidate_rows]

    hits = detect_recurrence(ticket, candidates)
    for hit in hits:
        await db.upsert_issue_recurrence({
            "new_issue_id": issue_id,
            "prior_issue_id": hit.prior_issue_id,
            "severity": hit.severity,
            "similarity": hit.similarity,
            "reason_code": hit.reason_code,
            "rule_type": rule_type,
            "fix_target": hit.fix_target,
            "fix_version": hit.fix_version,
            "compared_version": hit.compared_version,
            "version_source": hit.version_source,
            "prior_resolved_at": hit.prior_resolved_at,
            "prior_resolve_reason": hit.prior_resolve_reason,
        })
    return hits


async def detect_and_alert(issue_id: str) -> None:
    """detect_and_record() plus Feishu alerting for red hits, with three
    layers of anti-spam (see RecurrenceSettings docstring): a config-level
    kill switch, a per-(new,prior) pair lifetime cap of one alert, and a
    per-prior 12h rate cap so one bad regression can't page oncall once per
    duplicate ticket. Swallows all exceptions — this runs inline after
    analysis completion and must never fail that flow."""
    from app.config import get_settings
    from app.db import database as db
    from app.services import feishu_cli

    try:
        hits = await detect_and_record(issue_id)
    except Exception:
        logger.exception("recurrence detection failed for issue %s", issue_id)
        return

    settings = get_settings().recurrence
    if not settings.alert_enabled:
        return

    red_hits = [h for h in hits if h.severity == "red"]
    if not red_hits:
        return

    try:
        async with db.get_session() as session:
            issue = await db.get_ticket_record(session, issue_id)
        description = getattr(issue, "description", "") or "" if issue else ""
        zendesk_id = getattr(issue, "zendesk_id", "") or "" if issue else ""
        base_url = get_settings().frontend_base_url
        link = f"{base_url}/?detail={issue_id}" if base_url else ""

        since = datetime.utcnow() - timedelta(hours=12)
        for hit in red_hits:
            if await db.is_recurrence_alerted(issue_id, hit.prior_issue_id):
                continue  # lifetime cap: this exact (new, prior) pair already alerted once

            recent_alerts = await db.count_recurrence_alerts_since(hit.prior_issue_id, since)
            if recent_alerts >= settings.max_alerts_per_prior_12h:
                logger.info(
                    "recurrence alert suppressed (prior=%s already alerted %d times in 12h)",
                    hit.prior_issue_id, recent_alerts,
                )
                continue

            reason = (
                f"疑似复发：{hit.prior_issue_id} 于 {hit.prior_resolved_at} 标记修复"
                f"（{hit.fix_target} {hit.fix_version}），本单版本 {hit.compared_version} ≥ 修复版本"
            )
            await feishu_cli.notify_oncall(issue_id, description, reason, zendesk_id, link)
            if settings.chat_id:
                await feishu_cli.send_message(chat_id=settings.chat_id, text=f"🔴 {reason}\n{link}")
            await db.mark_recurrence_alerted(issue_id, hit.prior_issue_id)
    except Exception:
        logger.exception("recurrence alert failed for issue %s", issue_id)
