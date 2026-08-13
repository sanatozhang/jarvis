"""Tests for app.services.recurrence — the pure detect_recurrence() function
(no DB, no Feishu). DB-touching orchestration (detect_and_record,
detect_and_alert, load_resolved_candidates) is tested separately in
test_recurrence_db.py."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.services.recurrence import NewTicket, ResolvedCandidate, detect_recurrence

NOW = datetime(2026, 8, 12, 12, 0, 0)


def _ticket(
    issue_id="new1", description="蓝牙连接总是断开重连失败", rule_type="bluetooth",
    app_version="", firmware="", version_source="", created_at=NOW,
):
    return NewTicket(
        issue_id=issue_id, description=description, rule_type=rule_type,
        app_version=app_version, firmware=firmware, version_source=version_source,
        created_at=created_at,
    )


def _candidate(
    issue_id="prior1", description="蓝牙连接总是断开重连失败", rule_type="bluetooth",
    fix_target="", fix_version="", resolved_at=NOW, resolve_reason="已修复蓝牙重连逻辑",
):
    return ResolvedCandidate(
        issue_id=issue_id, description=description, rule_type=rule_type,
        fix_target=fix_target, fix_version=fix_version,
        resolved_at=resolved_at, resolve_reason=resolve_reason,
    )


# ---------------------------------------------------------------------------
# Version gate — the core mechanism (fix cadence is weekly, ~2-3 weeks to
# reach users, so the gate is what separates "stale pre-fix noise" from
# "genuinely still broken")
# ---------------------------------------------------------------------------

def test_new_version_equal_to_fix_version_is_red():
    ticket = _ticket(app_version="3.16.0")
    candidate = _candidate(fix_target="app", fix_version="3.16.0")
    hits = detect_recurrence(ticket, [candidate], now=NOW)
    assert len(hits) == 1
    assert hits[0].severity == "red"
    assert hits[0].reason_code == "version_gte_fix"


def test_new_version_above_fix_version_is_red():
    ticket = _ticket(app_version="3.17.2")
    candidate = _candidate(fix_target="app", fix_version="3.16.0")
    hits = detect_recurrence(ticket, [candidate], now=NOW)
    assert hits[0].severity == "red"


def test_build_suffix_is_ignored_by_version_parsing():
    ticket = _ticket(app_version="3.16.0-634")
    candidate = _candidate(fix_target="app", fix_version="3.16.0")
    hits = detect_recurrence(ticket, [candidate], now=NOW)
    assert hits[0].severity == "red"


def test_new_version_below_fix_version_produces_no_hit_at_all():
    """The crux of the whole feature: a straggler on an older, pre-fix
    version is NOT a recurrence — not even a yellow one. If this ever
    starts returning a yellow hit, the version gate has been defeated and
    every old-version user will spuriously "recur" every fixed issue."""
    ticket = _ticket(app_version="3.15.9")
    candidate = _candidate(fix_target="app", fix_version="3.16.0")
    hits = detect_recurrence(ticket, [candidate], now=NOW)
    assert hits == []


def test_firmware_target_compares_firmware_not_app_version():
    ticket = _ticket(app_version="9.9.9", firmware="1.1.0")
    candidate = _candidate(fix_target="firmware", fix_version="1.2.0")
    hits = detect_recurrence(ticket, [candidate], now=NOW)
    assert hits == []  # firmware 1.1.0 < fix 1.2.0 — app_version is irrelevant here


def test_firmware_target_red_when_firmware_meets_fix():
    ticket = _ticket(app_version="1.0.0", firmware="1.2.0")
    candidate = _candidate(fix_target="firmware", fix_version="1.2.0")
    hits = detect_recurrence(ticket, [candidate], now=NOW)
    assert hits[0].severity == "red"


def test_fix_target_other_is_always_yellow_regardless_of_version():
    ticket = _ticket(app_version="99.0.0")
    candidate = _candidate(fix_target="other", fix_version="1.0.0")
    hits = detect_recurrence(ticket, [candidate], now=NOW)
    assert hits[0].severity == "yellow"
    assert hits[0].reason_code == "no_version_target"


def test_missing_fix_version_is_yellow():
    ticket = _ticket(app_version="3.16.0")
    candidate = _candidate(fix_target="app", fix_version="")
    hits = detect_recurrence(ticket, [candidate], now=NOW)
    assert hits[0].severity == "yellow"
    assert hits[0].reason_code == "no_fix_version"


def test_unparseable_fix_version_is_yellow():
    ticket = _ticket(app_version="3.16.0")
    candidate = _candidate(fix_target="app", fix_version="beta")
    hits = detect_recurrence(ticket, [candidate], now=NOW)
    assert hits[0].severity == "yellow"
    assert hits[0].reason_code == "version_unparseable"


def test_new_ticket_version_missing_is_yellow_not_red():
    """A new ticket with no parseable version can't PROVE it's on/after the
    fix — degrade to yellow. Escalating to red here would be a false "still
    broken" alert with no evidence behind it."""
    ticket = _ticket(app_version="")
    candidate = _candidate(fix_target="app", fix_version="3.16.0")
    hits = detect_recurrence(ticket, [candidate], now=NOW)
    assert hits[0].severity == "yellow"
    assert hits[0].reason_code == "version_unparseable"


# ---------------------------------------------------------------------------
# Similarity gate
# ---------------------------------------------------------------------------

def test_different_rule_type_never_hits_even_at_perfect_similarity():
    ticket = _ticket(description="蓝牙连接总是断开重连失败", rule_type="bluetooth")
    candidate = _candidate(description="蓝牙连接总是断开重连失败", rule_type="cloud-sync",
                            fix_target="app", fix_version="1.0.0")
    hits = detect_recurrence(ticket, [candidate], now=NOW)
    assert hits == []


def test_similarity_below_threshold_is_dropped():
    ticket = _ticket(description="蓝牙连接总是断开", rule_type="bluetooth")
    candidate = _candidate(description="应用崩溃闪退白屏无法打开", rule_type="bluetooth")
    hits = detect_recurrence(ticket, [candidate], threshold=0.30, now=NOW)
    assert hits == []


def test_general_rule_type_uses_higher_threshold():
    # similarity ≈0.46 (measured): passes the specific-rule threshold (0.30)
    # but is meant to fail the stricter general-bucket threshold (0.45 default,
    # tightened to 0.50 here so the margin is unambiguous either way).
    ticket = _ticket(description="蓝牙连接总是断开重连失败无法配对", rule_type="general")
    candidate = _candidate(description="手机蓝牙连接经常断开重连总是失败", rule_type="general",
                            fix_target="app", fix_version="1.0.0")
    hits = detect_recurrence(ticket, [candidate], threshold=0.30, general_threshold=0.50, now=NOW)
    assert hits == []

    ticket_specific = _ticket(description="蓝牙连接总是断开重连失败无法配对", rule_type="bluetooth")
    candidate_specific = _candidate(description="手机蓝牙连接经常断开重连总是失败", rule_type="bluetooth",
                                     fix_target="app", fix_version="1.0.0")
    hits2 = detect_recurrence(ticket_specific, [candidate_specific], threshold=0.30, general_threshold=0.50, now=NOW)
    assert len(hits2) == 1


def test_leading_ui_tag_prefix_is_stripped_before_comparing():
    """Regression: two tickets sharing a long `[APP] [蓝牙连接] [安卓] [Note Pro]`
    prefix but describing UNRELATED problems must not match just because the
    shared prefix inflates the raw bigram overlap over threshold. Measured:
    raw similarity ≈0.36 (would incorrectly pass a 0.30 threshold), normalized
    (prefix stripped) similarity = 0.0."""
    ticket = _ticket(description="[APP] [蓝牙连接] [安卓] [Note Pro] 蓝牙一直连接不上设备", rule_type="bluetooth")
    candidate = _candidate(description="[APP] [蓝牙连接] [安卓] [Note Pro] 录音文件同步失败", rule_type="bluetooth",
                            fix_target="app", fix_version="1.0.0")
    hits = detect_recurrence(ticket, [candidate], threshold=0.30, now=NOW)
    assert hits == []


# ---------------------------------------------------------------------------
# Yellow time window (90d default) vs. red having none
# ---------------------------------------------------------------------------

def test_yellow_hit_outside_window_is_suppressed():
    old = NOW - timedelta(days=200)
    ticket = _ticket()
    candidate = _candidate(fix_target="other", fix_version="", resolved_at=old)
    hits = detect_recurrence(ticket, [candidate], yellow_window_days=90, now=NOW)
    assert hits == []


def test_yellow_hit_inside_window_is_kept():
    recent = NOW - timedelta(days=10)
    ticket = _ticket()
    candidate = _candidate(fix_target="other", fix_version="", resolved_at=recent)
    hits = detect_recurrence(ticket, [candidate], yellow_window_days=90, now=NOW)
    assert len(hits) == 1
    assert hits[0].severity == "yellow"


def test_yellow_hit_with_no_resolved_at_is_dropped():
    ticket = _ticket()
    candidate = _candidate(fix_target="other", fix_version="", resolved_at=None)
    hits = detect_recurrence(ticket, [candidate], now=NOW)
    assert hits == []


def test_red_hit_has_no_time_window_even_far_in_the_past():
    ancient = NOW - timedelta(days=300)
    ticket = _ticket(app_version="3.16.0")
    candidate = _candidate(fix_target="app", fix_version="3.16.0", resolved_at=ancient)
    hits = detect_recurrence(ticket, [candidate], yellow_window_days=90, now=NOW)
    assert len(hits) == 1
    assert hits[0].severity == "red"


# ---------------------------------------------------------------------------
# Sorting, top_k, self-exclusion
# ---------------------------------------------------------------------------

def test_red_sorts_before_yellow():
    ticket = _ticket(app_version="3.16.0")
    red_candidate = _candidate(issue_id="p_red", fix_target="app", fix_version="3.16.0")
    yellow_candidate = _candidate(issue_id="p_yellow", fix_target="other", fix_version="")
    hits = detect_recurrence(ticket, [yellow_candidate, red_candidate], now=NOW)
    assert [h.severity for h in hits] == ["red", "yellow"]


def test_same_severity_sorts_by_similarity_descending():
    ticket = _ticket(description="蓝牙连接总是断开重连失败", app_version="3.16.0")
    less_similar = _candidate(issue_id="p_less", description="蓝牙连接偶尔断开",
                               fix_target="app", fix_version="3.16.0")
    more_similar = _candidate(issue_id="p_more", description="蓝牙连接总是断开重连失败",
                               fix_target="app", fix_version="3.16.0")
    hits = detect_recurrence(ticket, [less_similar, more_similar], threshold=0.10, now=NOW)
    assert [h.prior_issue_id for h in hits] == ["p_more", "p_less"]


def test_top_k_truncates():
    ticket = _ticket(app_version="3.16.0")
    candidates = [
        _candidate(issue_id=f"p{i}", fix_target="app", fix_version="3.16.0")
        for i in range(5)
    ]
    hits = detect_recurrence(ticket, candidates, top_k=2, now=NOW)
    assert len(hits) == 2


def test_candidate_matching_own_issue_id_is_excluded():
    ticket = _ticket(issue_id="same_id", app_version="3.16.0")
    candidate = _candidate(issue_id="same_id", fix_target="app", fix_version="3.16.0")
    hits = detect_recurrence(ticket, [candidate], now=NOW)
    assert hits == []
