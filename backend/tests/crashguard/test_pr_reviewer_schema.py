"""Schema 校验 — CrashPullRequest 加 reviewer 字段（Task 1）"""
from app.crashguard.models import CrashPullRequest


def test_crash_pr_has_reviewer_fields():
    cols = {c.name for c in CrashPullRequest.__table__.columns}
    assert "reviewer_emails" in cols
    assert "reviewer_open_ids" in cols
    assert "reviewer_assigned_at" in cols
    assert "last_reminder_at" in cols
    assert "reviewed_at" in cols
    assert "reviewer_fallback_reason" in cols


def test_pr_reviewer_settings_defaults():
    from app.crashguard.config import get_crashguard_settings
    s = get_crashguard_settings()
    assert hasattr(s, "pr_reviewer_enabled")
    assert hasattr(s, "pr_reviewer_top_n")
    assert hasattr(s, "pr_reviewer_min_lines_pct")
    assert hasattr(s, "pr_reviewer_blocked_authors")
    assert hasattr(s, "pr_reviewer_daily_cron")
    assert hasattr(s, "pr_reviewer_fallback_email")
    assert "sanato.zhang@plaud.ai" in s.pr_reviewer_blocked_authors
    assert s.pr_reviewer_fallback_email  # 非空
